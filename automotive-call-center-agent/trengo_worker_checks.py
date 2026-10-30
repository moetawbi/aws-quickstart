"""Offline checks for the Trengo worker and webhook queue - no network, no model.

    .venv/bin/python trengo_worker_checks.py

Trengo is a fake client and the agent is a stub, so this runs anywhere in a
second. It covers the decisions that decide what reaches a ticket: media
handovers, Assist drafts, the owner gate, the "AI Draft" label, the chatbot's
automatic replies, closed tickets and the webhook queue. PASS/FAIL per check;
exit code 0 = all passed. (trengo_verify.py is the live counterpart against a
real Trengo account.)
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-used")
os.environ.pop("CRM_API_BASE_URL", None)

import agent_context  # noqa: E402
import trengo  # noqa: E402
import trengo_webhook  # noqa: E402
import trengo_worker  # noqa: E402

TEAM = {"trengo_team_id": 423113, "trengo_team_name": "ANB Call Center"}
failures: list[str] = []


def check(label: str, ok) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def msg(i, text, inbound=True, body_type="TEXT", bot=False):
    """A Trengo message as GET /tickets/{id}/messages returns it. An advisor's
    reply carries its author; the chatbot's is an auto-reply with none."""
    m = {"id": i, "message": text, "type": "INBOUND" if inbound else "OUTBOUND", "body_type": body_type}
    if not inbound:
        m.update({"is_auto_reply": 1} if bot else {"agent": {"name": "Pamela"}})
    return m


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.assigned, self.sent, self.notes, self.removed = [], [], [], []

    def get_messages(self, ticket_id):
        return self.messages

    def assign_ticket(self, ticket_id, team_id, note=""):
        self.assigned.append((ticket_id, team_id, note))
        return {}

    def send_message(self, ticket_id, text):
        self.sent.append((ticket_id, text))
        return {}

    def add_note(self, ticket_id, text):
        self.notes.append((ticket_id, text))
        return {}

    def remove_label(self, ticket_id, label_id):
        self.removed.append((ticket_id, label_id))
        return {}


class StubAgent:
    reply = "Happy to help."

    def __init__(self, config):
        self.config = config
        self.agent_slug = (config or {}).get("slug")
        self.turns: list[str] = []

    def send(self, text):
        self.turns.append(text)
        return self.reply


def state_file() -> Path:
    return Path(tempfile.mkdtemp()) / "state.json"


# -- message shapes ----------------------------------------------------------------
check("audio body_type is a voice note", trengo.media_kind({"message": "Audio", "body_type": "AUDIO"}) == "voice note")
check("sticker is a reaction, not media", trengo.media_kind({"message": "Sticker", "body_type": "IMAGE"}) == "")
check("131051 notice is unreadable", trengo.media_kind({"message": "*131051: Message type unknown*", "body_type": "TEXT"}) == "unreadable message")
check("no body_type, placeholder word", trengo.media_kind({"message": "Audio"}) == "voice note")
check("notes are recognised by type NOTE", trengo.is_internal_note({"type": "NOTE", "message": "x"}))
check("is_automated: auto-reply, or outbound with no author",
      trengo.is_automated({"type": "OUTBOUND", "is_auto_reply": 1}) and trengo.is_automated({"type": "OUTBOUND"}))
check("is_automated: an advisor, an API send (the CRM's templates) and the customer are not",
      not trengo.is_automated({"type": "OUTBOUND", "agent": {"name": "Pamela"}})
      and not trengo.is_automated({"type": "OUTBOUND", "user_id": 726749})
      and not trengo.is_automated({"type": "INBOUND"}))

ticket = {"id": 77, "contact": {"full_name": "Test", "phone": "+96170000000"},
          "channel": {"title": "WhatsApp Call Center (+961 79 111 423)", "type": "WA_BUSINESS"}}
check("summary: channel id", trengo.ticket_summary({**ticket, "channel": {"id": 1333697}})["channel_id"] == "1333697")
check("summary: no team, no user -> not owned", not trengo.ticket_summary(ticket)["owned"])
check("summary: a team or a user -> owned",
      trengo.ticket_summary({**ticket, "team_id": 1})["owned"] and trengo.ticket_summary({**ticket, "user_id": 5})["owned"])
check("summary: labels by lower-cased name",
      trengo.ticket_summary({**ticket, "labels": [{"id": 9, "name": "AI Draft"}]})["labels"] == {"ai draft": 9})


# -- AutoPilot (legacy, no CRM routing): media goes to the team -----------------------
def autopilot_worker(messages, config, dry_run=False):
    os.environ["TRENGO_REPLIES_ENABLED"] = "1"
    built: list[StubAgent] = []

    def factory(slug=None, **kwargs):
        built.append(StubAgent(config))
        return built[-1]

    state = state_file()
    return trengo_worker.TrengoWorker(client=FakeClient(messages), agent_factory=factory, state_path=state,
                                      dry_run=dry_run), built, state


w, built, state = autopilot_worker([msg(1, "Hello"), msg(2, "Hi, how can I help?", inbound=False),
                                    msg(3, "Audio", body_type="AUDIO")], {"slug": "call-center", "handover": TEAM})
check("autopilot: voice note -> handed over, no model turn",
      w.handle_ticket(ticket) == "handed_over" and w.client.assigned[0][:2] == ("77", 423113)
      and "voice note" in w.client.assigned[0][2] and all(not a.turns for a in built))
check("autopilot: handed-over ticket is never answered again", w.handle_ticket(ticket) == "skipped")
w, built, _ = autopilot_worker([msg(1, "When do you open on Saturday?")], {"handover": TEAM})
check("autopilot: text -> replied, first turn names the line",
      w.handle_ticket(ticket) == "replied" and "WhatsApp Call Center (+961 79 111 423)" in built[-1].turns[0])
w, built, state = autopilot_worker([msg(1, "Audio", body_type="AUDIO")], {"handover": TEAM}, dry_run=True)
check("autopilot dry run: nothing assigned, nothing saved",
      w.handle_ticket(ticket) == "handed_over" and not w.client.assigned and not state.exists())


# -- Assist ------------------------------------------------------------------------------
class DraftingAgent(StubAgent):
    def send(self, text):
        self.turns.append(text)
        if "recall" in text.lower():
            agent_context.mark_handed_over("ANB Call Center", "AI agent handover (high)\nCustomer wants a recall check.")
        return "Our Jal El Dib showroom is open Saturday 8:00-1:00."


def drafting_worker(messages, routing, dry_run=False):
    os.environ["TRENGO_NOTES_ENABLED"] = "1"
    os.environ.pop("TRENGO_REPLIES_ENABLED", None)
    made = []

    def factory(slug=None, **kwargs):
        made.append((slug, kwargs, DraftingAgent({"slug": slug, "handover": TEAM})))
        return made[-1][2]

    state = state_file()
    return trengo_worker.TrengoWorker(client=FakeClient(messages), agent_factory=factory, state_path=state,
                                      dry_run=dry_run, routing=routing), made, state


def assist_routing(channel_id):
    return {"agent": "call-center", "mode": "assist"} if channel_id == "1333697" else None


def always_routing(channel_id):
    return {"agent": "call-center", "mode": "assist", "work_when": "always"}


# Owned by the team (the chatbot handed over): the default deployment works it.
owned = {**ticket, "channel": {**ticket["channel"], "id": 1333697}, "team_id": 423113}
unowned = {**owned, "team_id": None, "labels": [{"id": 9001, "name": "AI Draft"}, {"id": 12, "name": "Service"}]}

w, made, state = drafting_worker([msg(1, "Hello"), msg(2, "Hi, how can I help?", inbound=False),
                                  {"id": 3, "message": "🤖 AI draft (Assist) - old draft", "type": "NOTE"},
                                  msg(4, "Are you open Saturday?")], assist_routing)
check("assist -> drafted as an internal note, never a reply or a handover",
      w.handle_ticket(owned) == "drafted" and len(w.client.notes) == 1 and not w.client.sent and not w.client.assigned)
check("note carries the header and the draft",
      w.client.notes[0][1].startswith("🤖 AI draft (Assist) - review") and "Saturday 8:00-1:00" in w.client.notes[0][1])
check("assist agent: deployed slug, read-only, rehearsing handovers",
      made[0][0] == "call-center" and made[0][1] == {"read_only": True, "handover_rehearsal": True, "channel": "messaging"})
turn = made[0][2].turns[0]
check("assist context: advisor reply as 'Us', old draft note excluded, no 'since' block",
      "Assist mode" in turn and "Us: Hi, how can I help?" in turn and "old draft" not in turn and "Since then" not in turn)
check("the same message is never drafted twice", w.handle_ticket(owned) == "skipped" and len(w.client.notes) == 1)
check("drafting never marks the ticket handed over", "77" not in json.loads(state.read_text())["handed_over"])

w, made, _ = drafting_worker([msg(1, "Can someone check my recall?")], assist_routing)
w.handle_ticket(owned)
check("a rehearsed handover becomes a suggestion in the note",
      "↪ Would hand over to ANB Call Center (high):\nCustomer wants a recall check." in w.client.notes[0][1]
      and not w.client.assigned)

w, made, _ = drafting_worker([msg(1, "Audio", body_type="AUDIO")], assist_routing)
check("assist: media -> no draft, no model call", w.handle_ticket(owned) == "skipped" and not made and not w.client.notes)
w, made, _ = drafting_worker([msg(1, "Hello"), msg(2, "We open at 8", inbound=False)], assist_routing)
check("assist: an advisor already answered -> nothing to draft", w.handle_ticket(owned) == "skipped" and not made)
w, made, _ = drafting_worker([msg(1, "Hello")], assist_routing)
check("undeployed inbox is left alone", w.handle_ticket(ticket) == "skipped" and not made and not w.client.notes)
w, made, _ = drafting_worker([msg(1, "Hello")], lambda ch: {"agent": "call-center", "mode": "autopilot"})
check("AutoPilot without TRENGO_REPLIES_ENABLED runs as Assist",
      w.handle_ticket(owned) == "drafted" and len(w.client.notes) == 1 and not w.client.sent)
w, made, _ = drafting_worker([msg(1, "Hello")], assist_routing, dry_run=True)
check("assist dry run posts nothing", w.handle_ticket(owned) == "drafted" and not w.client.notes)

# -- the owner gate --------------------------------------------------------------------
w, made, _ = drafting_worker([msg(1, "I need a service for my CX-5")], assist_routing)
check("owned (default): a ticket nobody owns is left alone", w.handle_ticket(unowned) == "skipped" and not made)
w, made, _ = drafting_worker([msg(1, "I need a service for my CX-5")], always_routing)
check("always: a ticket nobody owns is drafted", w.handle_ticket(unowned) == "drafted")

# -- the chatbot handed over -------------------------------------------------------------
flow = [msg(1, "Hi I need a service"), msg(2, "Which car?", inbound=False, bot=True), msg(3, "Mazda 3"),
        msg(4, "Which day?", inbound=False, bot=True), msg(5, "Monday"),
        msg(6, "Thank you! We'll be in touch shortly.", inbound=False, bot=True)]
w, made, _ = drafting_worker(flow, assist_routing)
check("owned: the chatbot's closing auto-reply does not count as an answer", w.handle_ticket(owned) == "drafted")
turn = made[-1][2].turns[0]
check("owned: the whole flow is the request, chatbot lines labelled",
      "chatbot answered automatically in between" in turn and "Customer: Hi I need a service" in turn
      and "Chatbot: Which car?" in turn and "Customer: Monday" in turn)
check("owned: not drafted twice", w.handle_ticket(owned) == "skipped")
w, made, _ = drafting_worker(flow, always_routing)
check("chatbot still running (nobody owns it): its reply counts, nothing to draft",
      w.handle_ticket({**unowned, "labels": []}) == "skipped" and not made)
w, made, _ = drafting_worker([msg(1, "Hi"), msg(2, "We open at 8", inbound=False), msg(3, "Thanks"),
                              msg(4, "Glad to help", inbound=False, bot=True)], assist_routing)
w.handle_ticket(owned)
turn = made[-1][2].turns[0]
check("owned: a person's reply still ends the run, the chatbot's line after it is kept",
      "Customer: Thanks\nChatbot: Glad to help" in turn and "Us: We open at 8" in turn
      and "Customer: Hi" not in turn.split("Us: We open at 8")[1])

closed = {**owned, "status": "CLOSED", "labels": [{"id": 9001, "name": "AI Draft"}]}
w, made, _ = drafting_worker([msg(1, "❤️")], assist_routing)
check("closed ticket: a routine ring is skipped", w.handle_ticket(closed) == "skipped" and not made)
check("closed ticket: the label still drafts", w.handle_ticket(closed, draft_label="AI Draft") == "drafted")

# -- the "AI Draft" label --------------------------------------------------------------------
w, made, _ = drafting_worker([msg(1, "I need a service for my CX-5")], assist_routing)
check("label: drafted although nobody owns the ticket", w.handle_ticket(unowned, draft_label="ai draft") == "drafted")
check("label: removed afterwards, note says it was asked for",
      w.client.removed == [("77", 9001)] and w.client.notes[0][1].startswith("🤖 AI draft (Assist, on request)"))

w, made, _ = drafting_worker([msg(1, "Hello"), msg(2, "Do you have the CX-60?"),
                              msg(3, "Let me check", inbound=False)], assist_routing)
w.replied["77"] = 2
check("routine ring: nothing unanswered", w.handle_ticket(owned) == "skipped" and not made)
check("label: redrafts the customer's last messages", w.handle_ticket(owned, draft_label="AI Draft") == "drafted")
asked = made[-1][2].turns[0]
check("label: the customer's last run is the question, the advisor's reply since is context",
      "New message from the customer:\nHello\nDo you have the CX-60?" in asked and "Since then:\nUs: Let me check" in asked)

w, made, _ = drafting_worker([msg(1, "Audio", body_type="AUDIO")], assist_routing)
check("label + voice note: an explanatory note, no model call, label removed",
      w.handle_ticket(unowned, draft_label="AI Draft") == "skipped" and not made
      and "voice note" in w.client.notes[0][1] and w.client.removed == [("77", 9001)])
w, made, _ = drafting_worker([msg(1, "We open at 8", inbound=False)], assist_routing)
check("label with no customer message: label removed, nothing posted",
      w.handle_ticket(unowned, draft_label="AI Draft") == "skipped" and not w.client.notes and w.client.removed == [("77", 9001)])
w, made, _ = drafting_worker([msg(1, "Hello")], lambda ch: {"agent": "call-center", "mode": "autopilot"})
os.environ["TRENGO_REPLIES_ENABLED"] = "1"
check("label on an AutoPilot inbox drafts a note, never replies",
      w.handle_ticket(owned, draft_label="AI Draft") == "drafted" and w.client.notes and not w.client.sent)
os.environ.pop("TRENGO_REPLIES_ENABLED", None)
w, made, _ = drafting_worker([msg(1, "Hello")], assist_routing)
check("label on an undeployed inbox: left alone", w.handle_ticket(ticket, draft_label="AI Draft") == "skipped" and not made)
w, made, _ = drafting_worker([msg(1, "Hello")], assist_routing, dry_run=True)
check("label dry run: no note, no label removal",
      w.handle_ticket(unowned, draft_label="AI Draft") == "drafted" and not w.client.notes and not w.client.removed)

# -- the webhook queue ---------------------------------------------------------------------------
class RecordingWorker:
    def __init__(self):
        self.calls = []

    def handle_ticket_id(self, ticket_id, draft_label=""):
        self.calls.append((ticket_id, draft_label))
        return "ok"


q = trengo_webhook.TicketQueue.__new__(trengo_webhook.TicketQueue)
q.worker, q._pending, q._active, q._lock, q._queue = RecordingWorker(), {}, 0, threading.Lock(), queue.Queue()
q.submit("88")
q.submit("88", draft_label="AI Draft")
check("queue: a label upgrades a pending routine ring", q._pending == {"88": "AI Draft"} and q._queue.qsize() == 1)
q.submit("88")
check("queue: a later routine ring never downgrades it", q._pending == {"88": "AI Draft"})

# -- switches ----------------------------------------------------------------------------------
os.environ["TRENGO_NOTES_ENABLED"] = ""
offline = trengo.TrengoClient(api_key="x", base_url="http://trengo.invalid")
check("add_note and remove_label refuse without TRENGO_NOTES_ENABLED",
      "error" in offline.add_note(1, "x") and "error" in offline.remove_label(1, 2))
os.environ.pop("TRENGO_REPLIES_ENABLED", None)
check("send_message and assign_ticket refuse without TRENGO_REPLIES_ENABLED",
      "error" in offline.send_message(1, "x") and "error" in offline.assign_ticket(1, 2))

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
