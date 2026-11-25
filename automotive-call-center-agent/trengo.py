"""Trengo API client for the automotive call center agent.

Talks to the Trengo REST API (https://app.trengo.com/api/v2) to read
tickets and their messages and to post replies. Auth is a Bearer token
from ``TRENGO_API_KEY`` (create one under Trengo Settings > API).

Endpoints used (verify paths against https://developers.trengo.com if
your account is on a different API version - they are isolated in the
methods below so adjustments are one-line changes):

- ``GET  /tickets?status=OPEN&page=N``   list tickets
- ``GET  /tickets/{ticket_id}/messages`` messages on a ticket
- ``POST /tickets/{ticket_id}/messages`` post a reply, body ``{"message": ...}``

Response field names vary a little across Trengo channel types, so the
message parsing helpers (`message_text`, `media_kind`, `is_inbound`,
`is_internal_note`) probe the common shapes instead of assuming one. Failures are returned as
``{"error": ...}`` dicts, never raised, matching the CRM client.

Nothing is ever posted to a customer unless ``TRENGO_REPLIES_ENABLED=1``:
``send_message`` and ``assign_ticket`` refuse without it, so reading tickets
for testing can never turn into answering them by accident. Internal notes -
Assist mode's drafts, which customers never see - have their own switch,
``TRENGO_NOTES_ENABLED=1``.
"""

from __future__ import annotations

import os

import requests

DEFAULT_BASE_URL = "https://app.trengo.com/api/v2"
DEFAULT_TIMEOUT = float(os.environ.get("TRENGO_TIMEOUT_SECONDS", "15"))
REPLIES_DISABLED = "Trengo replies are disabled (set TRENGO_REPLIES_ENABLED=1 to let the agent answer customers)."
NOTES_DISABLED = "Trengo internal notes are disabled (set TRENGO_NOTES_ENABLED=1 for Assist drafts)."


def replies_enabled() -> bool:
    """The kill switch for every customer-visible Trengo message. Read per
    call, so flipping the env var and restarting is the whole change."""
    return os.environ.get("TRENGO_REPLIES_ENABLED", "").strip() == "1"


def notes_enabled() -> bool:
    """The switch for internal notes - drafts only the team sees."""
    return os.environ.get("TRENGO_NOTES_ENABLED", "").strip() == "1"


class TrengoClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or os.environ.get("TRENGO_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout
        key = api_key if api_key is not None else os.environ.get("TRENGO_API_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            return {"error": f"Trengo is unreachable ({exc.__class__.__name__})."}
        if response.status_code == 429:
            return {"error": "Trengo rate limit hit.", "retry": True}
        if not response.ok:
            return {"error": f"Trengo returned HTTP {response.status_code} for {path}."}
        if not response.content.strip():
            return {}  # a 2xx with no body (an assignment, for one) is a success
        try:
            return response.json()
        except ValueError:
            return {"error": "Trengo returned a non-JSON response."}

    @staticmethod
    def _data(payload) -> list | None:
        """Unwrap Trengo's paginated {"data": [...]} envelope (or a bare list)."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return None

    def list_tickets(self, status: str = "OPEN", max_pages: int = 3) -> list[dict] | dict:
        """Tickets with the given status, newest pages first as Trengo returns
        them; paginates up to max_pages. Returns {"error": ...} on failure."""
        tickets: list[dict] = []
        for page in range(1, max_pages + 1):
            payload = self._request("GET", "/tickets", params={"status": status, "page": page})
            data = self._data(payload)
            if data is None:
                return payload if isinstance(payload, dict) else {"error": "Unexpected Trengo response."}
            tickets.extend(t for t in data if isinstance(t, dict))
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            if not data or page >= int(meta.get("last_page", page) or page):
                break
        return tickets

    def get_ticket(self, ticket_id) -> dict:
        payload = self._request("GET", f"/tickets/{ticket_id}")
        if isinstance(payload, dict):
            # Some deployments wrap the record: {"data": {...}}
            data = payload.get("data")
            return data if isinstance(data, dict) else payload
        return {"error": "Unexpected Trengo response."}

    def get_messages(self, ticket_id) -> list[dict] | dict:
        """The ticket's latest messages (Trengo's first page), OLDEST FIRST.
        Trengo returns them newest first; every caller here reads the end of
        the list as the latest message, so they are put in time order."""
        payload = self._request("GET", f"/tickets/{ticket_id}/messages")
        data = self._data(payload)
        if data is None:
            return payload if isinstance(payload, dict) else {"error": "Unexpected Trengo response."}
        return sorted((m for m in data if isinstance(m, dict)),
                      key=lambda m: (str(m.get("created_at") or ""), int(m.get("id") or 0)))

    def assign_ticket(self, ticket_id, team_id: int, note: str = "") -> dict:
        """Re-assign a ticket to a team - the agent's handover. The customer
        does not see it, but it is the agent acting on Trengo, so it sits
        behind the same switch as replies. The note tells the team why."""
        if not replies_enabled():
            return {"error": REPLIES_DISABLED}
        body = {"type": "team", "team_id": int(team_id)}
        if note.strip():
            body["note"] = note.strip()
        payload = self._request("POST", f"/tickets/{ticket_id}/assign", json=body)
        return payload if isinstance(payload, dict) else {"assigned": True}

    def send_message(self, ticket_id, text: str) -> dict:
        if not replies_enabled():
            return {"error": REPLIES_DISABLED}
        payload = self._request("POST", f"/tickets/{ticket_id}/messages", json={"message": text})
        return payload if isinstance(payload, dict) else {"sent": True}

    def remove_label(self, ticket_id, label_id) -> dict:
        """Take a label off a ticket - the agent clears the "AI Draft" label
        it was asked with, so an advisor can ask again. Internal, like a note."""
        if not notes_enabled():
            return {"error": NOTES_DISABLED}
        payload = self._request("DELETE", f"/tickets/{ticket_id}/labels/{int(label_id)}")
        return payload if isinstance(payload, dict) else {"removed": True}

    def add_note(self, ticket_id, text: str) -> dict:
        """An internal note on the ticket: visible to the team, never sent to
        the customer. Assist mode's drafts go here."""
        if not notes_enabled():
            return {"error": NOTES_DISABLED}
        payload = self._request("POST", f"/tickets/{ticket_id}/messages",
                                json={"message": text, "internal_note": True})
        return payload if isinstance(payload, dict) else {"noted": True}


# ---------------------------------------------------------------------------
# Message shape helpers - Trengo field names differ across channel types,
# so probe the common variants rather than assume one schema.
# ---------------------------------------------------------------------------

def message_text(message: dict) -> str:
    for key in ("message", "body_text", "text", "body"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# WhatsApp media reaches the API as a placeholder word in `message` ("Audio",
# "Image", ...), with the kind in `body_type` and the file under `file_url`.
# Stickers arrive as IMAGE too; they are a reaction, not a request.
MEDIA_KINDS = {
    "AUDIO": "voice note",
    "IMAGE": "image",
    "VIDEO": "video",
    "DOCUMENT": "document",
    "FILE": "file",
    "LOCATION": "location",
}
_PLACEHOLDERS = {"audio": "voice note", "image": "image", "video": "video", "document": "document"}
# WhatsApp error 131051: Trengo received the message but not its content.
UNREADABLE_MARKER = "Message type unknown"


def media_kind(message: dict) -> str:
    """What the customer sent that the agent cannot open - "voice note",
    "image", ... - or "" for text (and for a sticker)."""
    text = message_text(message)
    if text.lower() == "sticker":
        return ""
    if UNREADABLE_MARKER in text:
        return "unreadable message"
    body_type = str(message.get("body_type") or "").upper()
    if body_type and body_type != "TEXT":
        return MEDIA_KINDS.get(body_type, body_type.lower())
    if not body_type:
        # Payloads without body_type: fall back to the file and the placeholder.
        if message.get("file_url"):
            return _PLACEHOLDERS.get(text.lower(), "attachment")
        return _PLACEHOLDERS.get(text.lower(), "")
    return ""


def is_internal_note(message: dict) -> bool:
    # The API marks notes with type "NOTE" - the agent's own Assist drafts
    # among them, which must never read back as replies the team sent.
    return bool(message.get("is_note") or message.get("internal") or message.get("note")
                or str(message.get("type", "")).upper() == "NOTE")


def is_inbound(message: dict) -> bool:
    """True when the message came from the customer (contact), False when it
    was sent by an agent/bot or is an internal note."""
    if is_internal_note(message):
        return False
    direction = str(message.get("direction", "")).upper()
    if direction in ("INBOUND", "IN", "INCOMING"):
        return True
    if direction in ("OUTBOUND", "OUT", "OUTGOING"):
        return False
    mtype = str(message.get("type", "")).upper()
    if mtype == "INBOUND":
        return True
    if mtype == "OUTBOUND":
        return False
    # Fall back to authorship: a contact wrote it and no agent/user did.
    has_contact = bool(message.get("contact") or message.get("contact_id"))
    has_agent = bool(message.get("agent") or message.get("agent_id") or
                     message.get("user") or message.get("user_id"))
    return has_contact and not has_agent


def is_automated(message: dict) -> bool:
    """An outbound message nobody typed - Trengo's chatbot and auto-replies.
    (Messages sent through the API, like the CRM's templates, carry the
    token's user and count as ours.)"""
    if is_inbound(message) or is_internal_note(message):
        return False
    if message.get("is_auto_reply"):
        return True
    return not (message.get("agent") or message.get("agent_id") or message.get("user") or message.get("user_id"))


def ticket_summary(ticket: dict) -> dict:
    """The bits of a Trengo ticket the worker cares about."""
    contact = ticket.get("contact") or {}
    channel = ticket.get("channel") or {}
    return {
        "ticket_id": ticket.get("id"),
        # A team or a person owns it. While Trengo's chatbot runs a flow the
        # ticket is ASSIGNED to nobody - no team, no user.
        "owned": bool(ticket.get("team_id") or ticket.get("team") or ticket.get("user_id") or ticket.get("assignee")),
        "labels": {str(label.get("name") or "").strip().lower(): label.get("id")
                   for label in ticket.get("labels") or [] if isinstance(label, dict)},
        "channel_id": str(channel.get("id") or ticket.get("channel_id") or ""),
        "status": ticket.get("status", ""),
        "subject": ticket.get("subject") or "",
        "contact_name": contact.get("full_name") or contact.get("name") or "",
        "contact_phone": contact.get("phone") or "",
        "channel_name": channel.get("title") or channel.get("name") or "",
        "channel_type": channel.get("type") or "",
    }
