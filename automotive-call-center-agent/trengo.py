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
message parsing helpers (`message_text`, `is_inbound`, `is_internal_note`)
probe the common shapes instead of assuming one. Failures are returned as
``{"error": ...}`` dicts, never raised, matching the CRM client.
"""

from __future__ import annotations

import os

import requests

DEFAULT_BASE_URL = "https://app.trengo.com/api/v2"
DEFAULT_TIMEOUT = float(os.environ.get("TRENGO_TIMEOUT_SECONDS", "15"))


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

    def get_messages(self, ticket_id) -> list[dict] | dict:
        payload = self._request("GET", f"/tickets/{ticket_id}/messages")
        data = self._data(payload)
        if data is None:
            return payload if isinstance(payload, dict) else {"error": "Unexpected Trengo response."}
        return [m for m in data if isinstance(m, dict)]

    def send_message(self, ticket_id, text: str) -> dict:
        payload = self._request("POST", f"/tickets/{ticket_id}/messages", json={"message": text})
        return payload if isinstance(payload, dict) else {"sent": True}


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


def is_internal_note(message: dict) -> bool:
    return bool(message.get("is_note") or message.get("internal") or message.get("note"))


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


def ticket_summary(ticket: dict) -> dict:
    """The bits of a Trengo ticket the worker cares about."""
    contact = ticket.get("contact") or {}
    channel = ticket.get("channel") or {}
    return {
        "ticket_id": ticket.get("id"),
        "status": ticket.get("status", ""),
        "subject": ticket.get("subject") or "",
        "contact_name": contact.get("full_name") or contact.get("name") or "",
        "contact_phone": contact.get("phone") or "",
        "channel_name": channel.get("title") or channel.get("name") or "",
        "channel_type": channel.get("type") or "",
    }
