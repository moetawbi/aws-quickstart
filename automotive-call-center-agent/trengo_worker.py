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

Which agent answers a ticket, and how, is decided per inbox by the CRM's
deployments (deployments.py): an inbox with no deployment is left to the
team. Without a CRM there are no deployments and the built-in default agent
answers every ticket, as the template always did.

- **Assist** drafts the reply as an internal note for an advisor to review
  and send - nothing reaches the customer, nothing is handed over, nothing is
  written to the CRM. Each draft starts a fresh read-only agent fed the whole
  ticket, so it sees what the advisors actually sent. Needs
  TRENGO_NOTES_ENABLED=1.
- **AutoPilot** answers the customer. Every turn runs with the ticket in
  agent_context; when the agent hands a ticket over (escalate_to_human
  re-assigns it to the handover team) the worker records it and never answers
  that ticket again - the team owns it. Needs TRENGO_REPLIES_ENABLED=1, and
  runs as Assist without it.

Voice notes, photos, videos and files never reach the model: the agent can
only read text, and a voice note is usually the whole request. In AutoPilot
the worker re-assigns such a ticket to the handover team itself, with a note
saying what was sent, and posts nothing; in Assist it drafts nothing, since
the advisor has to open the file anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import agent_config
import agent_context
import deployments
import trengo
from agent import CallCenterAgent, agent_for, channel_kind, local_now

POLL_SECONDS = float(os.environ.get("TRENGO_POLL_SECONDS", "20"))
TICKET_STATUS = os.environ.get("TRENGO_TICKET_STATUS", "OPEN")
# How much prior ticket history to replay when this worker first sees a ticket.
MAX_TRANSCRIPT_MESSAGES = 30

MODE_ASSIST = "assist"
MODE_AUTOPILOT = "autopilot"
ASSIST_HEADER = "🤖 AI draft (Assist) - review before sending"
ASSIST_INSTRUCTIONS = (
    "Assist mode: an ANB advisor reads your reply and sends it themselves, so write it exactly as "
    "it should reach the customer, on behalf of ANB's team - do not introduce yourself or sign "
    "with a name. You cannot create leads here - when you would, collect the details in your "
    "reply and the advisor will create the lead."
)


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
                 dry_run: bool = False, routing=None):
        self.client = client or trengo.TrengoClient()
        # agent_for(slug) loads that CRM agent (the default when slug is None),
        # falling back to the built-in prompts when the CRM cannot be reached.
        self.agent_factory = agent_factory
        # channel id -> deployment, or None; no CRM means no deployments.
        self.routing = routing if routing is not None else (
            deployments.for_channel if agent_config.available() else None)
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

    def _deployment(self, summary: dict) -> dict | None:
        """How this ticket's inbox is worked, or None when no agent works it.
        AutoPilot runs as Assist unless customer replies are switched on here."""
        if self.routing is None:
            return {"agent": None, "mode": MODE_AUTOPILOT}
        deployment = self.routing(summary["channel_id"])
        if not deployment:
            return None
        if deployment.get("mode") == MODE_AUTOPILOT and not trengo.replies_enabled():
            return {**deployment, "mode": MODE_ASSIST}
        return deployment

    def _ticket_context(self, summary: dict, history: list[dict], assist: bool = False) -> str:
        """First user turn for a new agent session on this ticket: channel
        metadata plus a replay of any conversation that predates this worker."""
        # The channel's name carries its number, so the agent knows which
        # line the customer is already writing on.
        channel = " - ".join(filter(None, [summary["channel_type"], summary["channel_name"]])) or "messaging"
        lines = [
            f"[Ticket metadata - not written by the customer] Now (Lebanon time): {local_now()}. "
            f"Channel: Trengo {channel} (written conversation - reply in the customer's language, "
            "no greetings on every message, keep it brief).",
        ]
        if assist:
            lines.append(ASSIST_INSTRUCTIONS)
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

    def _hand_over_media(self, ticket_id: str, latest: dict, kinds: list[str], slug: str | None) -> str | None:
        """Hand the ticket to the team because the customer sent something
        the agent cannot open. Returns the outcome, or None when the agent
        has no handover team - then the agent answers as usual."""
        agent = self.agents.get(ticket_id) or self.agent_factory(slug)
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

    def _mark_answered(self, ticket_id: str, message_id: int) -> None:
        if self.dry_run:
            return
        if message_id:
            self.replied[ticket_id] = message_id
        self._save_state()

    @staticmethod
    def _assist_note(draft: str, handed_to: str | None, handover_note: str) -> str:
        """The internal note an advisor sees: the reply to send, and where the
        agent would have handed the conversation over, with its reason."""
        parts = [draft.strip()] if draft.strip() else []
        if handed_to:
            first, _, reason = handover_note.partition("\n")
            priority = first[first.find("(") + 1:first.find(")")] if "(" in first else "normal"
            label = f"↪ Would hand over to {handed_to}" + (f" ({priority})" if priority != "normal" else "")
            parts.append(label + (f":\n{reason.strip()}" if reason.strip() else "."))
        return f"{ASSIST_HEADER}\n\n" + "\n\n".join(parts) if parts else ""

    def _assist(self, ticket_id: str, summary: dict, messages: list[dict], deployment: dict) -> str:
        """Draft the reply to the customer's unanswered messages as an internal
        note. Nothing reaches the customer, nothing is handed over, nothing is
        written to the CRM - the advisor decides."""
        visible = [m for m in messages if not trengo.is_internal_note(m)]
        unanswered = self._unanswered(ticket_id, visible)
        if not any(trengo.message_text(m) or trengo.media_kind(m) for m in unanswered):
            return "skipped"  # nothing new from the customer, or an advisor already replied
        latest_id = max(int(m.get("id") or 0) for m in unanswered)

        kinds = list(dict.fromkeys(kind for kind in map(trengo.media_kind, unanswered) if kind))
        if kinds:
            # The advisor has to open it anyway; a draft could only guess.
            self._mark_answered(ticket_id, latest_id)
            _log(f"ticket {ticket_id}: no draft - customer sent {', '.join(kinds)}")
            return "skipped"

        history = [m for m in visible[:len(visible) - len(unanswered)] if trengo.message_text(m)]
        new = "\n".join(text for text in map(trengo.message_text, unanswered) if text)
        try:
            # A fresh agent per draft, fed the whole ticket: between drafts the
            # advisors may have written anything, and that is what it must see.
            agent = self.agent_factory(deployment.get("agent"), read_only=True, handover_rehearsal=True,
                                       channel=channel_kind(summary["channel_type"]))
            user_turn = (f"{self._ticket_context(summary, history, assist=True)}\n\n"
                         f"New message from the customer:\n{new}")
            with agent_context.conversation(
                agent_slug=getattr(agent, "agent_slug", None),
                agent_config=getattr(agent, "config", None),
                trengo_ticket_id=ticket_id,
                trengo_dry_run=True,  # a rehearsal: the ticket is never re-assigned
            ):
                draft = agent.send(user_turn)
                handed_to = agent_context.handed_over()
                handover_note = agent_context.handover_note()
        except Exception as exc:
            _log(f"ticket {ticket_id}: draft failed ({exc.__class__.__name__}: {exc})")
            return "error"

        note = self._assist_note(draft, handed_to, handover_note)
        if not note:
            _log(f"ticket {ticket_id}: agent produced no draft")
            return "error"

        if self.dry_run:
            _log(f"ticket {ticket_id}: DRY RUN - would note: {note[:200]}")
            return "drafted"

        result = self.client.add_note(ticket_id, note)
        if isinstance(result, dict) and "error" in result:
            _log(f"ticket {ticket_id}: could not post the draft: {result['error']}")
            return "error"

        self._mark_answered(ticket_id, latest_id)
        _log(f"ticket {ticket_id}: drafted ({len(note)} chars{', handover suggested' if handed_to else ''})")
        return "drafted"

    def handle_ticket(self, ticket: dict) -> str:
        """Process one ticket; returns what happened (for logs/tests):
        'replied', 'drafted', 'handed_over', 'skipped', or 'error'."""
        summary = trengo.ticket_summary(ticket)
        ticket_id = str(summary["ticket_id"])
        deployment = self._deployment(summary)
        if deployment is None:
            return "skipped"  # no agent works this inbox
        if ticket_id in self.handed_over:
            return "skipped"  # handed to the team - never answered by the agent again

        messages = self.client.get_messages(ticket_id)
        if isinstance(messages, dict):
            _log(f"ticket {ticket_id}: {messages.get('error', 'unexpected response')}")
            return "error"

        if deployment.get("mode") == MODE_ASSIST:
            return self._assist(ticket_id, summary, messages, deployment)

        slug = deployment.get("agent")

        # A voice note or photo goes straight to the team - no model call.
        visible = [m for m in messages if not trengo.is_internal_note(m)]
        unanswered = self._unanswered(ticket_id, visible)
        kinds = list(dict.fromkeys(kind for kind in map(trengo.media_kind, unanswered) if kind))
        if kinds:
            outcome = self._hand_over_media(ticket_id, unanswered[-1], kinds, slug)
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
                agent = self.agent_factory(slug, channel=channel_kind(summary["channel_type"]))
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
        stats = {"replied": 0, "drafted": 0, "handed_over": 0, "skipped": 0, "error": 0}
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
                if stats["replied"] or stats["drafted"] or stats["handed_over"] or stats["error"]:
                    _log(f"cycle: {stats}")
            except Exception as exc:  # a bad cycle must not kill the worker
                _log(f"cycle failed ({exc.__class__.__name__}: {exc})")
            time.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Answer Trengo tickets with the call center agent")
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    args = parser.parse_args()
    if not (trengo.replies_enabled() or trengo.notes_enabled()):
        # Refuse before any model call: a worker that may neither reply nor
        # note must not even draft (it would spend tokens on nothing).
        print(f"{trengo.REPLIES_DISABLED} {trengo.NOTES_DISABLED}", file=sys.stderr)
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
