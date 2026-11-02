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
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-used")
os.environ.pop("CRM_API_BASE_URL", None)

import agent  # noqa: E402
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


def msg(i, text, inbound=True, body_type="TEXT", bot=False, ago=None):
    """A Trengo message as GET /tickets/{id}/messages returns it. An advisor's
    reply carries its author; the chatbot's is an auto-reply with none.
    `ago`: seconds since Trengo received it (its created_at, Amsterdam time)."""
    m = {"id": i, "message": text, "type": "INBOUND" if inbound else "OUTBOUND", "body_type": body_type}
    if ago is not None:
        m["created_at"] = (datetime.now(trengo_worker.TRENGO_TZ) - timedelta(seconds=ago)).strftime("%Y-%m-%d %H:%M:%S")
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


def drafting_worker(messages, routing, dry_run=False, deliver=None):
    os.environ["TRENGO_NOTES_ENABLED"] = "1"
    os.environ.pop("TRENGO_REPLIES_ENABLED", None)
    made = []

    def factory(slug=None, **kwargs):
        made.append((slug, kwargs, DraftingAgent({"slug": slug, "handover": TEAM})))
        return made[-1][2]

    state = state_file()
    return trengo_worker.TrengoWorker(client=FakeClient(messages), agent_factory=factory, state_path=state,
                                      dry_run=dry_run, routing=routing, deliver=deliver), made, state


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
check("assist context: the reply is copied to the customer as it is - no notes for the advisor in it",
      "sends it to the customer as it is" in turn and "never add a note, summary or lead details for the advisor" in turn)
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

# -- waiting for the customer to finish ----------------------------------------------------------
w, made, _ = drafting_worker([msg(1, "I need a service", ago=20), msg(2, "for my Vespa", ago=2)], assist_routing)
check("still typing (newest line 2 s old): no draft yet, try again in ~6 s",
      w.handle_ticket(owned) == "deferred" and not made and 4 <= w.defer_seconds["77"] <= 7)
w, made, _ = drafting_worker([msg(1, "I need a service", ago=20), msg(2, "for my Vespa", ago=9)], assist_routing)
check("paused 9 s: drafted, both lines in one draft",
      w.handle_ticket(owned) == "drafted" and "I need a service\nfor my Vespa" in made[-1][2].turns[0])
w, made, _ = drafting_worker([msg(1, "Still typing", ago=1)], assist_routing)
w._waiting_since["77"] = time.monotonic() - 31
check("still typing past the 30 s cap: drafted anyway", w.handle_ticket(owned) == "drafted")
w, made, _ = drafting_worker([msg(1, "Still typing", ago=1)], assist_routing)
check("a label request never waits", w.handle_ticket(owned, draft_label="AI Draft") == "drafted")
w, made, _ = drafting_worker([msg(1, "Old message")], assist_routing)
check("no timestamp on the message: drafted (never stuck waiting)", w.handle_ticket(owned) == "drafted")

# -- a follow-up adds to the draft the advisor already has --------------------------------------
draft_note = {"id": 2, "type": "NOTE", "message": "🤖 AI draft (Assist) - review before sending\n\nAhlan! How can we help?"
              "\n\n↪ Would hand over to ANB Call Center:\nnote"}
w, made, _ = drafting_worker([msg(1, "Salam"), draft_note, msg(3, "I need parts for my Vespa")], assist_routing)
w.replied["77"] = 1
w.handle_ticket(owned)
turn = made[-1][2].turns[0]
check("follow-up: told about the earlier unsent draft (body only), asked for no greeting and no repeats",
      "Your earlier draft(s)" in turn and "Ahlan! How can we help?" in turn and "Would hand over" not in turn
      and "no greeting" in turn and "New message from the customer:\nI need parts for my Vespa" in turn)
w, made, _ = drafting_worker([msg(1, "Salam"), draft_note, msg(3, "Welcome!", inbound=False), msg(4, "Parts please")],
                             assist_routing)
w.handle_ticket(owned)
check("once a person replied, earlier drafts are history, not pending", "Your earlier draft(s)" not in made[-1][2].turns[0])
w, made, _ = drafting_worker([msg(1, "Salam"), draft_note, msg(3, "Parts please")], assist_routing)
w.handle_ticket(owned, draft_label="AI Draft")
check("a label request gets a complete draft, not an add-on", "Your earlier draft(s)" not in made[-1][2].turns[0])

# -- sidebar delivery: one living suggestion, sent to the CRM ------------------------------------
def sidebar_routing(channel_id):
    return {"agent": "call-center", "mode": "assist", "delivery": "sidebar"}


delivered: list[dict] = []


def crm_takes(draft):
    delivered.append(draft)
    return True


w, made, _ = drafting_worker([msg(1, "Hello"), msg(2, "Hi, how can I help?", inbound=False), msg(3, "Open Saturday?")],
                             sidebar_routing, deliver=crm_takes)
check("sidebar: drafted to the CRM, no note on the ticket",
      w.handle_ticket(owned) == "drafted" and not w.client.notes and len(delivered) == 1)
sent = delivered[-1]
check("sidebar: the draft carries the ticket, inbox, agent, reply, the lines it answers and an id",
      sent["ticket_id"] == "77" and sent["channel_id"] == "1333697" and sent["agent"] == "call-center"
      and sent["draft"] == "Our Jal El Dib showroom is open Saturday 8:00-1:00." and sent["customer_message"] == "Open Saturday?"
      and len(sent["draft_id"]) == 24 and sent["on_request"] is False and sent["handover_team"] is None)
check("sidebar: the same message is never drafted twice", w.handle_ticket(owned) == "skipped" and len(delivered) == 1)

delivered.clear()
w, made, _ = drafting_worker([msg(1, "Salam"), msg(2, "I need parts for my Vespa")], sidebar_routing, deliver=crm_takes)
w.replied["77"] = 1  # "Salam" was drafted already, never sent
w.handle_ticket(owned)
turn = made[-1][2].turns[0]
check("sidebar follow-up: a complete draft over everything since the team wrote, no add-on instructions",
      "New message from the customer:\nSalam\nI need parts for my Vespa" in turn and "Your earlier draft(s)" not in turn
      and delivered[-1]["customer_message"] == "Salam\nI need parts for my Vespa")

delivered.clear()
w, made, _ = drafting_worker([msg(1, "Can someone check my recall?")], sidebar_routing, deliver=crm_takes)
w.handle_ticket(owned)
check("sidebar: a would-be handover travels as team + reason",
      delivered[-1]["handover_team"] == "ANB Call Center"
      and delivered[-1]["handover_reason"] == "High priority. Customer wants a recall check.")

w, made, _ = drafting_worker([msg(1, "Open Saturday?")], sidebar_routing, deliver=lambda draft: False)
check("sidebar: the CRM not taking it -> posted as a note, so the advisor still gets it",
      w.handle_ticket(owned) == "drafted" and len(w.client.notes) == 1
      and w.client.notes[0][1].startswith("🤖 AI draft (Assist) - review"))

delivered.clear()
w, made, _ = drafting_worker([msg(1, "Open Saturday?"), msg(2, "Yes, 8 to 1.", inbound=False)],
                             sidebar_routing, deliver=crm_takes)
check("sidebar: a label request goes to the sidebar, marked on request",
      w.handle_ticket(unowned, draft_label="AI Draft") == "drafted" and delivered[-1]["on_request"] is True
      and not w.client.notes and w.client.removed == [("77", 9001)])

delivered.clear()
w, made, _ = drafting_worker([msg(1, "Open Saturday?")], sidebar_routing, dry_run=True, deliver=crm_takes)
check("sidebar dry run: nothing delivered", w.handle_ticket(owned) == "drafted" and not delivered)

# -- one reply per turn: an answer restated after a tool call appears once ------------------------
def turn(*messages):
    """Assistant messages of one turn; each is (text blocks..., optional tool name)."""
    out = []
    for blocks in messages:
        content = [SimpleNamespace(type="text", text=b) for b in blocks if not b.startswith("tool:")]
        content += [SimpleNamespace(type="tool_use", name=b[5:]) for b in blocks if b.startswith("tool:")]
        out.append({"role": "assistant", "content": content})
    return agent._turn_text(out)


first = ("We don't have a used CX-9 in stock right now, and your request is registered so the used-vehicle team "
         "can call you when a matching one comes in.\n\nTo help them match it: what model year are you looking for, "
         "what budget range do you have in mind, and is +961 76 594 538 the best number to reach you?")
again = ("We don't have a used CX-9 in stock at the moment, and your request is already registered so the used-vehicle "
         "team can call you as soon as a matching one arrives.\n\nTo help them find the right car: which model year are "
         "you looking for, what budget range do you have in mind, and is +961 76 594 538 the best number to reach you?")
check("an answer reworded after two tool rounds appears once - the later version (ticket 990673035)",
      turn([first, "tool:memory"], ["tool:search_knowledge"], [again]) == again)
noted = ("Noted, we'll keep your request on file for a pre-owned CX-9 and contact you on this number as soon as one "
         "comes in. One thing that helps us match you faster: what budget range (and model year, if you have one in "
         "mind) are you looking at?")
noted_again = ("Noted, we'll keep your request on file for a pre-owned CX-9 and contact you on this number as soon as "
               "one comes in. To help us match you faster: what budget range are you considering, and do you have a "
               "particular model year in mind?")
check("a shorter rewording still replaces the first", turn([noted, "tool:memory"], [noted_again]) == noted_again)
check("an Arabic answer reworded appears once",
      turn(["أكيد، منعمل صيانة للـ Beverly S 300 وفي عنا قطع غيار أصلية، وفيك تحجز موعد بالورشة بأي يوم من الاثنين للجمعة.",
            "tool:search_knowledge"],
           ["أكيد، منعمل صيانة للـ Beverly S 300 وعنا قطع غيار أصلية، وفيك تحجز موعد بالورشة أي يوم من الاثنين للجمعة."]).count("Beverly") == 1)
check("a fact before the tool call and a different answer after it are both kept",
      turn(["Nahr El Mot is closed on Saturdays.", "tool:search_knowledge"],
           ["As mentioned, you can come to Jal El Dib on Monday instead. Which time suits you?"])
      == "Nahr El Mot is closed on Saturdays.\n\nAs mentioned, you can come to Jal El Dib on Monday instead. Which time suits you?")
check("two short lines that share most words are both kept",
      turn(["Jal El Dib is open Monday to Friday 8 to 5.", "tool:search_knowledge"],
           ["Chtaura is open Monday to Friday 8 to 5."]).count("open") == 2)
check("two different paragraphs on the same car are both kept",
      turn(["The CX-5 2024 comes with a 2.5 litre engine, all-wheel drive and a six-speed automatic gearbox as standard.",
            "tool:search_knowledge"],
           ["Its next service is due at 20,000 km or after one year, whichever comes first, at any of our workshops."])
      .count("\n\n") == 1)

# -- the webhook queue ---------------------------------------------------------------------------
class RecordingWorker:
    def __init__(self):
        self.calls = []

    def handle_ticket_id(self, ticket_id, draft_label=""):
        self.calls.append((ticket_id, draft_label))
        return "ok"


q = trengo_webhook.TicketQueue.__new__(trengo_webhook.TicketQueue)
q.worker, q._pending, q._submitted_at, q._active = RecordingWorker(), {}, {}, 0
q._lock, q._queue = threading.Lock(), queue.Queue()
q.submit("88")
q.submit("88", draft_label="AI Draft")
check("queue: a label upgrades a pending routine ring", q._pending == {"88": "AI Draft"} and q._queue.qsize() == 1)
q.submit("88")
check("queue: a later routine ring never downgrades it", q._pending == {"88": "AI Draft"})


class DeferringWorker:
    def __init__(self):
        self.calls, self.defer_seconds = 0, {}

    def handle_ticket_id(self, ticket_id, draft_label=""):
        self.calls += 1
        if self.calls == 1:
            self.defer_seconds[ticket_id] = 0.05
            return "deferred"
        return "drafted"


dq = trengo_webhook.TicketQueue(DeferringWorker())
dq.submit("99")
time.sleep(0.3)
dq.drain()
check("queue: a deferred ticket is tried again after the wait", dq.worker.calls == 2)

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
