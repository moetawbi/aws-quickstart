"""Chat API: talk to the agent from the CRM (or any HTTP client).

    export CHAT_API_TOKEN=$(openssl rand -hex 24)
    python chat_api.py         # listens on 127.0.0.1:8082 by default

Endpoints (JSON in, JSON out; every request must carry the token as
``Authorization: Bearer <CHAT_API_TOKEN>`` or ``X-Chat-Token``):

    GET    /healthz                     liveness, no token required
    GET    /chat/sessions               live sessions (id, persona, turns, age)
    POST   /chat                        send one message, get the reply
    DELETE /chat/{session_id}           end one session

POST /chat body::

    {"session_id": "crm-42",        # optional; generated when omitted
     "persona": "internal",         # customer | internal   (default customer)
     "read_only": true,             # optional; defaults per persona
     "handover": "dry_run",         # optional; customer persona only - see below
     "message": "what do we know about 70200605?",
     "context": {"user_name": "Moe Tawbi", "user_role": "sales_manager",
                 "channel": "WhatsApp Call Center (+961 79 111 423)"}}  # channel: customer persona

Response::

    {"session_id": "crm-42", "persona": "internal", "read_only": true,
     "reply": "...", "tools_used": ["lookup_customer"], "turns": 3, "new_session": false,
     "handover": null}              # or {"team": ..., "note": ...} when rehearsed

``"handover": "dry_run"`` is the eval's handover rehearsal: a read-only
customer conversation gets escalate_to_human back, run against no real
ticket, and the response reports the team and note it would have handed
the conversation to. Nothing reaches Trengo - there is no live mode here.

Design notes:

- **Two personas, one agent.** ``customer`` is the same "Alex" that answers
  the phone; ``internal`` swaps in the staff-facing prompt. Both share the
  agent's knowledge, manuals, memory and CRM tools, so there is only ever
  one brain to maintain.
- **Read-only by default off the phone.** Internal chats never write, and a
  staff member previewing the customer experience gets the customer brain
  with the write tools withheld unless the caller explicitly asks for them -
  create_lead reaches the real CRM, so previewing must not be able to
  manufacture leads by accident.
- **Bind to localhost.** Like admin_api.py, the default bind is 127.0.0.1,
  so the CRM backend calls it directly and nothing is publicly exposed. Set
  CHAT_API_BIND=0.0.0.0 only behind a firewall or reverse proxy.
- **Sessions live in memory**, keyed by whatever id the CRM sends (its own
  conversation id). Idle sessions are evicted after CHAT_SESSION_TTL_SECONDS
  and the total is capped by CHAT_MAX_SESSIONS, so a long-running server
  cannot grow without bound. The CRM keeps the durable transcript.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

import agent_config
import agent_context
from agent import PERSONA_CUSTOMER, PERSONA_INTERNAL, PERSONAS, CallCenterAgent

BIND = os.environ.get("CHAT_API_BIND", "127.0.0.1")
PORT = int(os.environ.get("CHAT_API_PORT", "8082"))
SESSION_TTL = int(os.environ.get("CHAT_SESSION_TTL_SECONDS", str(2 * 60 * 60)))
MAX_SESSIONS = int(os.environ.get("CHAT_MAX_SESSIONS", "200"))
MAX_MESSAGE_CHARS = int(os.environ.get("CHAT_MAX_MESSAGE_CHARS", "8000"))


def _log(text: str) -> None:
    print(f"[chat] {text}", file=sys.stderr)


class Session:
    """One conversation: the agent plus its bookkeeping."""

    def __init__(self, session_id: str, persona: str, read_only: bool,
                 actor_grant: str | None = None, agent_slug: str | None = None,
                 handover_rehearsal: bool = False):
        self.id = session_id
        self.persona = persona
        self.read_only = read_only
        self.handover_rehearsal = handover_rehearsal
        # Opaque to us: the CRM issued it, the CRM reads it. Held for the
        # session so every turn is attributed to the same staff member.
        self.actor_grant = actor_grant
        # Config is snapshotted here, for the life of the session.
        self.agent_slug = agent_slug
        self.config = agent_config.fetch_or_none(agent_slug) if agent_config.available() else None
        self.agent = CallCenterAgent(persona=persona, read_only=read_only, config=self.config,
                                     handover_rehearsal=handover_rehearsal)
        self.created = time.time()
        self.last_used = self.created
        self.turns = 0
        self.lock = threading.Lock()


class SessionStore:
    """TTL + size capped session registry, safe across handler threads."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _evict_locked(self) -> None:
        cutoff = time.time() - SESSION_TTL
        for sid in [s.id for s in self._sessions.values() if s.last_used < cutoff]:
            self._sessions.pop(sid, None)
            _log(f"session {sid} expired")
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.last_used)
            self._sessions.pop(oldest.id, None)
            _log(f"session {oldest.id} evicted (cap {MAX_SESSIONS})")

    def get_or_create(self, session_id: str, persona: str, read_only: bool,
                      actor_grant: str | None = None,
                      agent_slug: str | None = None,
                      handover_rehearsal: bool = False) -> tuple[Session, bool]:
        with self._lock:
            self._evict_locked()
            existing = self._sessions.get(session_id)
            # A persona or permission change starts a fresh conversation
            # rather than continuing one under different rules.
            # A different agent is a different conversation, like a persona change.
            if existing and (existing.persona != persona
                             or existing.read_only != read_only
                             or existing.agent_slug != agent_slug
                             or existing.handover_rehearsal != handover_rehearsal):
                self._sessions.pop(session_id, None)
                existing = None
            if existing:
                # Refresh the grant: the CRM reissues it as it nears expiry.
                if actor_grant:
                    existing.actor_grant = actor_grant
                return existing, False
            session = Session(session_id, persona, read_only, actor_grant, agent_slug, handover_rehearsal)
            self._sessions[session_id] = session
            return session, True

    def drop(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def listing(self) -> list[dict]:
        with self._lock:
            self._evict_locked()
            return [
                {"session_id": s.id, "persona": s.persona, "read_only": s.read_only,
                 "agent": s.agent_slug, "has_actor": bool(s.actor_grant),
                 "turns": s.turns, "idle_seconds": int(time.time() - s.last_used)}
                for s in sorted(self._sessions.values(), key=lambda s: s.last_used, reverse=True)
            ]


STORE = SessionStore()


def opening_context(persona: str, read_only: bool, context: dict,
                    handover_rehearsal: bool = False) -> str:
    """First user turn of a session: channel metadata, injected here rather
    than in the system prompt so the cached prefix stays byte-stable."""
    today = date.today().isoformat()

    if persona == PERSONA_INTERNAL:
        who = str(context.get("user_name") or "a staff member")
        role = str(context.get("user_role") or "")
        lines = [
            f"[Chat metadata - not written by the person] Today's date: {today}. "
            f"Channel: CRM internal chat (written, staff-facing). You are talking to {who}"
            + (f", role: {role}" if role else "")
            + ", already authenticated by the CRM.",
            "This conversation is read-only: you have no booking, cancelling, lead-creation "
            "or escalation tools, and memory is view-only. Say so plainly if asked to do one "
            "of those, and hand over the details they would need instead.",
            "Do not greet - wait for their question and answer it directly.",
        ]
        return "\n".join(lines)

    # The eval replays real conversations and names their channel, so the
    # agent knows which line the customer is already writing on.
    channel = " ".join(str(context.get("channel") or "").split())[:120] or "web chat"
    lines = [
        f"[Chat metadata - not written by the caller] Today's date: {today}. "
        f"Channel: {channel} (written conversation - reply in the customer's language, "
        "no greeting on every message, keep it brief).",
    ]
    if read_only:
        withheld = "lead-creation tool" if handover_rehearsal else "lead-creation and handover tools"
        lines.append(
            f"Note: this is a preview session with the {withheld} withheld. Handle the "
            "conversation normally, but if it reaches the point of actually creating a lead"
            + ("" if handover_rehearsal else " or handing over")
            + ", explain that this preview cannot complete the action rather than claiming it is done."
        )
    lines.append("Greet the customer now.")
    return "\n".join(lines)


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token: str):
        super().__init__(address, ChatHandler)
        self.token = token


class ChatHandler(BaseHTTPRequestHandler):
    server: ChatServer

    def log_message(self, *args) -> None:
        pass

    # -- plumbing ---------------------------------------------------------------

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Chat-Token", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):]
        return bool(self.server.token) and hmac.compare_digest(supplied, self.server.token)

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return None
        if length > MAX_MESSAGE_CHARS * 4:
            self._send_json(413, {"error": "body too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._send_json(400, {"error": "body must be JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    # -- HTTP methods ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/healthz":
            self._send_json(200, {"ok": True, "service": "chat"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing chat token"})
            return
        if path == "/chat/sessions":
            self._send_json(200, {"sessions": STORE.listing()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing chat token"})
            return
        if self.path.split("?")[0].rstrip("/") != "/chat":
            self._send_json(404, {"error": "not found; POST /chat"})
            return
        payload = self._body()
        if payload is None:
            return

        message = str(payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"error": "message is required"})
            return
        if len(message) > MAX_MESSAGE_CHARS:
            self._send_json(413, {"error": f"message exceeds {MAX_MESSAGE_CHARS} characters"})
            return

        persona = str(payload.get("persona") or PERSONA_CUSTOMER)
        if persona not in PERSONAS:
            self._send_json(400, {"error": f"persona must be one of {list(PERSONAS)}"})
            return

        read_only = payload.get("read_only")
        # Internal chats are read-only, full stop; a caller cannot opt out.
        read_only = True if persona == PERSONA_INTERNAL else bool(True if read_only is None else read_only)

        session_id = str(payload.get("session_id") or "").strip() or f"chat-{uuid.uuid4().hex[:12]}"
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}

        # Only an internal conversation carries an actor: a customer-preview
        # chat must stay customer-scoped in the CRM even for an admin.
        actor_grant = None
        if persona == PERSONA_INTERNAL:
            actor_grant = str(payload.get("actor_grant") or context.get("actor_grant") or "").strip() or None

        agent_slug = str(payload.get("agent") or "").strip() or None

        handover = payload.get("handover")
        if handover not in (None, "", "dry_run"):
            self._send_json(400, {"error": 'handover must be "dry_run" - there is no live handover here'})
            return
        # Rehearsal only makes sense where handover is what a real customer
        # channel would do; it never touches a ticket either way.
        rehearsal = handover == "dry_run" and persona == PERSONA_CUSTOMER and read_only

        session, is_new = STORE.get_or_create(session_id, persona, read_only, actor_grant, agent_slug,
                                              rehearsal)

        try:
            # One turn at a time per session: the agent mirrors its own
            # history, so concurrent turns would interleave it.
            # The agent's slug rides on the turn so tools that read a store
            # mid-conversation (manual search) use this agent's files.
            with session.lock, agent_context.conversation(
                actor_grant=session.actor_grant,
                agent_slug=(session.config or {}).get("slug"),
                agent_config=session.config,
                trengo_ticket_id=f"rehearsal-{session_id}" if rehearsal else None,
                trengo_dry_run=True,
            ):
                if is_new:
                    session.agent.send(opening_context(persona, read_only, context, rehearsal))
                before = len(session.agent.messages)
                reply = session.agent.send(message)
                tools_used = self._tools_used(session.agent.messages[before:])
                handed_to = agent_context.handed_over()
                handed_over = {"team": handed_to, "note": agent_context.handover_note()} if handed_to else None
                session.turns += 1
                session.last_used = time.time()
        except anthropic.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after", "60")
            self._send_json(429, {"error": "the model is rate limited", "retry_after": retry})
            return
        except anthropic.APIStatusError as exc:
            self._send_json(502, {"error": f"model API error {exc.status_code}: {exc.message}"})
            return
        except anthropic.APIConnectionError:
            self._send_json(504, {"error": "could not reach the model API"})
            return
        except Exception as exc:  # never leak a traceback to the CRM
            _log(f"session {session_id} failed: {exc.__class__.__name__}: {exc}")
            self._send_json(500, {"error": f"agent error ({exc.__class__.__name__})"})
            return

        _log(f"[{(session.config or {}).get('slug') or 'built-in'}] {persona} session {session_id} turn {session.turns} "
             f"({len(message)} chars in, {len(reply)} out, tools: {','.join(tools_used) or '-'})")
        self._send_json(200, {
            "session_id": session_id,
            "agent": (session.config or {}).get("slug") or agent_slug,
            "agent_name": (session.config or {}).get("name"),
            "persona": persona,
            "read_only": read_only,
            "reply": reply,
            "tools_used": tools_used,
            "turns": session.turns,
            "new_session": is_new,
            "handover": handed_over,
        })

    def do_DELETE(self):
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing chat token"})
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "chat":
            self._send_json(405, {"error": "DELETE /chat/{session_id}"})
            return
        dropped = STORE.drop(parts[1])
        self._send_json(200 if dropped else 404,
                        {"ended": dropped, "session_id": parts[1]}
                        if dropped else {"error": f"no live session {parts[1]}"})

    @staticmethod
    def _tools_used(new_messages: list[dict]) -> list[str]:
        """Tool names the model called on this turn, in order, deduplicated -
        the CRM shows them so staff can see what the answer rests on."""
        names: list[str] = []
        for message in new_messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                name = getattr(block, "name", None)
                if getattr(block, "type", None) in {"tool_use", "server_tool_use"} and name:
                    if name not in names:
                        names.append(name)
        return names


def main() -> None:
    token = os.environ.get("CHAT_API_TOKEN", "").strip()
    if not token:
        print("CHAT_API_TOKEN is not set. Generate one: openssl rand -hex 24", file=sys.stderr)
        raise SystemExit(1)
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set - the agent cannot answer.", file=sys.stderr)
        raise SystemExit(1)
    server = ChatServer((BIND, PORT), token)
    _log(f"chat API listening on {BIND}:{PORT} (token required)")
    server.serve_forever()


if __name__ == "__main__":
    main()
