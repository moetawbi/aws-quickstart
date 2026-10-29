"""Trengo worker: answer customers on Trengo tickets with the call center agent.

Polls open Trengo tickets, and whenever the newest message on a ticket is
from the customer, runs the full conversation through ``CallCenterAgent``
(same tools: CRM, scheduling, recalls, manuals, leads, escalation) and
posts the reply back on the ticket.

    export ANTHROPIC_API_KEY=...
    export TRENGO_API_KEY=...
    python trengo_worker.py            # poll forever
    python trengo_worker.py --once     # one poll cycle (for cron/testing)

Polling needs no public URL, so it runs anywhere. If you prefer push over
poll, point a Trengo webhook for inbound messages at a small HTTP endpoint
that calls ``TrengoWorker.handle_ticket(ticket_id)`` - the per-ticket logic
is the same either way.

Replies are deduplicated two ways: a ticket is only answered when its
latest message is inbound (so it never answers itself), and the id of the
last inbound message answered per ticket is persisted to a state file so
restarts don't double-reply.

The agent is the CRM's default agent (its prompt, skills and handover team),
and every turn runs with the ticket in agent_context. When the agent hands a
ticket over (escalate_to_human re-assigns it to the handover team), the
worker records it and never answers that ticket again - the team owns it.

Voice notes, photos, videos and files never reach the model: the agent can
only read text, and a voice note is usually the whole request. When the
customer's unanswered messages include one, the worker re-assigns the
ticket to the handover team itself, with a note saying what was sent, and
posts nothing - a colleague replies in the same conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import agent_context
import trengo
from agent import CallCenterAgent, agent_for

POLL_SECONDS = float(os.environ.get("TRENGO_POLL_SECONDS", "20"))
TICKET_STATUS = os.environ.get("TRENGO_TICKET_STATUS", "OPEN")
# How much prior ticket history to replay when this worker first sees a ticket.
MAX_TRANSCRIPT_MESSAGES = 30


def default_state_path() -> Path:
    configured = os.environ.get("TRENGO_STATE_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / ".trengo_state.json"


def _log(text: str) -> None:
    print(f"[trengo] {text}", file=sys.stderr)


class TrengoWorker:
    def __init__(self, client: trengo.TrengoClient | None = None,
                 agent_factory=agent_for, state_path: Path | None = None,
                 dry_run: bool = False):
        self.client = client or trengo.TrengoClient()
        # agent_for() loads the CRM's default agent, falling back to the
        # built-in prompts when the CRM cannot be reached.
        self.agent_factory = agent_factory
        self.dry_run = dry_run  # never post replies, assign tickets or update state; log instead
        self.state_path = state_path or default_state_path()
        self.agents: dict[str, CallCenterAgent] = {}  # live sessions, keyed by ticket id
        self.replied: dict[str, int] = {}
        self.handed_over: dict[str, dict] = {}  # ticket id -> {"team", "at"}
        self._load_state()

    # -- state ---------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.replied = {str(k): int(v) for k, v in data.get("replied", {}).items()}
        self.handed_over = {str(k): dict(v) for k, v in data.get("handed_over", {}).items()}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps({"replied": self.replied, "handed_over": self.handed_over}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            _log(f"could not persist state: {exc}")

    # -- per-ticket handling ---------------------------------------------------

    def _ticket_context(self, summary: dict, history: list[dict]) -> str:
        """First user turn for a new agent session on this ticket: channel
        metadata plus a replay of any conversation that predates this worker."""
        # The channel's name carries its number, so the agent knows which
        # line the customer is already writing on.
        channel = " - ".join(filter(None, [summary["channel_type"], summary["channel_name"]])) or "messaging"
        lines = [
            f"[Ticket metadata - not written by the customer] Today's date: {date.today().isoformat()}. "
            f"Channel: Trengo {channel} (written conversation - reply in the customer's language, "
            "no greetings on every message, keep it brief).",
        ]
        if summary["contact_name"]:
            lines.append(f"Customer name on the ticket: {summary['contact_name']}.")
        if summary["contact_phone"]:
            lines.append(f"Phone number on the ticket: {summary['contact_phone']}.")
        if summary["subject"]:
            lines.append(f"Ticket subject: {summary['subject']}.")
        if history:
            lines.append("\nConversation so far (oldest first):")
            for message in history[-MAX_TRANSCRIPT_MESSAGES:]:
                who = "Customer" if trengo.is_inbound(message) else "Us"
                text = trengo.message_text(message)
                if text:
                    lines.append(f"{who}: {text}")
        return "\n".join(lines)

    def _unanswered(self, ticket_id: str, visible: list[dict]) -> list[dict]:
        """The customer's messages since our last one that the agent has not
        answered yet, oldest first."""
        answered = self.replied.get(ticket_id, -1)
        run: list[dict] = []
        for message in reversed(visible):
            message_id = int(message.get("id") or 0)
            if not trengo.is_inbound(message) or (message_id and message_id <= answered):
                break
            run.append(message)
        return run[::-1]

    def _hand_over_media(self, ticket_id: str, latest: dict, kinds: list[str]) -> str | None:
        """Hand the ticket to the team because the customer sent something
        the agent cannot open. Returns the outcome, or None when the agent
        has no handover team - then the agent answers as usual."""
        agent = self.agents.get(ticket_id) or self.agent_factory()
        handover = (getattr(agent, "config", None) or {}).get("handover") or {}
        team_id = handover.get("trengo_team_id")
        if not team_id:
            _log(f"ticket {ticket_id}: customer sent {', '.join(kinds)} but the agent has no handover team")
            return None
        team = handover.get("trengo_team_name") or f"team {team_id}"
        note = ("AI agent handover (normal)\n"
                f"The customer sent: {', '.join(kinds)}. The AI agent can only read text, "
                "so a colleague needs to open it and continue this conversation.")

        if self.dry_run:
            _log(f"ticket {ticket_id}: DRY RUN - would hand over to {team}: {', '.join(kinds)}")
            return "handed_over"

        result = self.client.assign_ticket(ticket_id, int(team_id), note)
        if isinstance(result, dict) and "error" in result:
            _log(f"ticket {ticket_id}: could not hand over: {result['error']}")
            return "error"

        self._record_handover(ticket_id, team, int(latest.get("id") or 0), ", ".join(kinds))
        return "handed_over"

    def handle_ticket(self, ticket: dict) -> str:
        """Process one ticket; returns what happened (for logs/tests):
        'replied', 'handed_over', 'skipped', or 'error'."""
        summary = trengo.ticket_summary(ticket)
        ticket_id = str(summary["ticket_id"])
        if ticket_id in self.handed_over:
            return "skipped"  # handed to the team - never answered by the agent again

        messages = self.client.get_messages(ticket_id)
        if isinstance(messages, dict):
            _log(f"ticket {ticket_id}: {messages.get('error', 'unexpected response')}")
            return "error"

        # A voice note or photo goes straight to the team - no model call.
        visible = [m for m in messages if not trengo.is_internal_note(m)]
        unanswered = self._unanswered(ticket_id, visible)
        kinds = list(dict.fromkeys(kind for kind in map(trengo.media_kind, unanswered) if kind))
        if kinds:
            outcome = self._hand_over_media(ticket_id, unanswered[-1], kinds)
            if outcome:
                return outcome

        spoken = [m for m in messages if not trengo.is_internal_note(m) and trengo.message_text(m)]
        if not spoken or not trengo.is_inbound(spoken[-1]):
            return "skipped"  # nothing new from the customer

        latest = spoken[-1]
        latest_id = int(latest.get("id") or 0)
        if latest_id and self.replied.get(ticket_id, -1) >= latest_id:
            return "skipped"  # already answered this message

        agent = self.agents.get(ticket_id)
        try:
            if agent is None:
                agent = self.agent_factory()
                self.agents[ticket_id] = agent
                context = self._ticket_context(summary, spoken[:-1])
                user_turn = f"{context}\n\nNew message from the customer:\n{trengo.message_text(latest)}"
            else:
                user_turn = trengo.message_text(latest)
            config = getattr(agent, "config", None)
            with agent_context.conversation(
                agent_slug=getattr(agent, "agent_slug", None),
                agent_config=config,
                trengo_ticket_id=ticket_id,
                trengo_dry_run=self.dry_run,
            ):
                reply = agent.send(user_turn)
                handed_to = agent_context.handed_over()
        except Exception as exc:
            _log(f"ticket {ticket_id}: agent failed ({exc.__class__.__name__}: {exc})")
            self.agents.pop(ticket_id, None)  # fresh session next cycle
            return "error"

        if not reply.strip():
            _log(f"ticket {ticket_id}: agent produced no reply text")
            if not handed_to:
                return "error"
            # The ticket is already the team's: record it, or the next poll
            # would have the agent answer a conversation it handed over.
            if not self.dry_run:
                self._record_handover(ticket_id, handed_to, latest_id)
            return "handed_over"

        if self.dry_run:
            _log(f"ticket {ticket_id}: DRY RUN - would reply: {reply[:200]}")
            return "replied"

        result = self.client.send_message(ticket_id, reply)
        if isinstance(result, dict) and "error" in result:
            _log(f"ticket {ticket_id}: could not post reply: {result['error']}")
            return "error"

        if handed_to:
            self._record_handover(ticket_id, handed_to, latest_id)
        else:
            if latest_id:
                self.replied[ticket_id] = latest_id
            self._save_state()
        _log(f"ticket {ticket_id}: replied ({len(reply)} chars)")
        return "replied"

    def _record_handover(self, ticket_id: str, team: str, latest_id: int, reason: str = "") -> None:
        """The team owns the ticket from now on: never answer it again."""
        if latest_id:
            self.replied[ticket_id] = latest_id
        self.handed_over[ticket_id] = {"team": team, "at": datetime.now(timezone.utc).isoformat(),
                                       **({"reason": reason} if reason else {})}
        self.agents.pop(ticket_id, None)
        self._save_state()
        _log(f"ticket {ticket_id}: handed over to {team}" + (f" ({reason})" if reason else ""))

    def handle_ticket_id(self, ticket_id) -> str:
        """Webhook entry point: process one ticket known only by id. Fetches
        the ticket for its metadata (falling back to a bare record if that
        fails - metadata only enriches the first turn) and runs the same
        latest-message-inbound / dedupe logic as polling."""
        ticket = self.client.get_ticket(ticket_id)
        if not isinstance(ticket, dict) or "error" in ticket or ticket.get("id") is None:
            ticket = {"id": ticket_id}
        return self.handle_ticket(ticket)

    # -- polling ---------------------------------------------------------------

    def poll_once(self) -> dict:
        stats = {"replied": 0, "handed_over": 0, "skipped": 0, "error": 0}
        tickets = self.client.list_tickets(status=TICKET_STATUS)
        if isinstance(tickets, dict):
            _log(tickets.get("error", "could not list tickets"))
            stats["error"] += 1
            return stats
        for ticket in tickets:
            outcome = self.handle_ticket(ticket)
            stats[outcome] += 1
        # Drop cached sessions for tickets that are no longer in the queue.
        open_ids = {str((t.get("id"))) for t in tickets}
        for stale in set(self.agents) - open_ids:
            del self.agents[stale]
        return stats

    def run(self) -> None:
        _log(f"watching Trengo tickets (status={TICKET_STATUS}, every {POLL_SECONDS:.0f}s)")
        while True:
            try:
                stats = self.poll_once()
                if stats["replied"] or stats["handed_over"] or stats["error"]:
                    _log(f"cycle: {stats}")
            except Exception as exc:  # a bad cycle must not kill the worker
                _log(f"cycle failed ({exc.__class__.__name__}: {exc})")
            time.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Answer Trengo tickets with the call center agent")
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    args = parser.parse_args()
    if not trengo.replies_enabled():
        # Refuse before any model call: a disabled worker must not even
        # draft replies (it would spend tokens on answers nobody may send).
        print(trengo.REPLIES_DISABLED, file=sys.stderr)
        raise SystemExit(1)
    if not os.environ.get("TRENGO_API_KEY"):
        print("TRENGO_API_KEY is not set.", file=sys.stderr)
        raise SystemExit(1)
    worker = TrengoWorker()
    if args.once:
        print(json.dumps(worker.poll_once()))
    else:
        worker.run()


if __name__ == "__main__":
    main()
