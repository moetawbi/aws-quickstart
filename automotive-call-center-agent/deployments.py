"""Which agent works which Trengo inbox - Phase 3 deployments, from the CRM.

``GET {CRM_API_BASE_URL}/deployments`` lists every live deployment as
``{channel_id, channel_name, channel_type, agent, mode, work_when, delivery}``;
mode is ``assist`` (draft a reply for an advisor) or ``autopilot`` (answer the
customer). The CRM already downgrades AutoPilot to Assist unless its own
server allows it, and the worker downgrades again unless
TRENGO_REPLIES_ENABLED=1. ``work_when`` is ``owned`` (only once a team or a
person owns the ticket - Trengo's chatbot runs its flows on unowned ones) or
``always``; a row without it is treated as ``owned``. ``delivery`` says where
an Assist draft goes: ``note`` (an internal note on the ticket) or ``sidebar``
(sent to the CRM with deliver_draft(), which shows it in Trengo's sidebar app -
one living suggestion per ticket instead of notes piling up in it); a row
without it is treated as ``note``.

Fail closed: an inbox the CRM does not list - or any inbox while the CRM
cannot be reached - is left to the team. Cached briefly, so a change in the
panel reaches the worker within a minute.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import requests

TTL = float(os.environ.get("DEPLOYMENTS_TTL_SECONDS", "60"))
TIMEOUT = float(os.environ.get("AGENT_CONFIG_TIMEOUT", "10"))

_cache: tuple[float, dict[str, dict]] | None = None
_lock = threading.Lock()


def _log(text: str) -> None:
    print(f"[deployments] {text}", file=sys.stderr)


def _base() -> str:
    return os.environ.get("CRM_API_BASE_URL", "").strip().rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    api_key = os.environ.get("CRM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _fetch() -> dict[str, dict] | None:
    base = _base()
    if not base:
        return None
    headers = _headers()
    try:
        response = requests.get(f"{base}/deployments", headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        rows = response.json().get("data", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        _log(f"could not load deployments ({exc.__class__.__name__}) - leaving every inbox to the team")
        return None
    return {str(row.get("channel_id")): row for row in rows if isinstance(row, dict) and row.get("channel_id")}


def routing() -> dict[str, dict]:
    """Trengo channel id -> deployment; {} when nothing is deployed or the
    CRM is unreachable. A failed fetch is retried on the next call."""
    global _cache
    with _lock:
        if _cache and (time.time() - _cache[0]) < TTL:
            return _cache[1]
    rows = _fetch()
    if rows is None:
        return {}
    with _lock:
        _cache = (time.time(), rows)
    return rows


def for_channel(channel_id) -> dict | None:
    """The deployment for one inbox, or None when no agent works it."""
    return routing().get(str(channel_id or ""))


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def deliver_draft(draft: dict) -> bool:
    """Hand an Assist draft to the CRM for Trengo's sidebar app:
    ``POST {CRM_API_BASE_URL}/drafts``. ``draft_id`` makes a retry harmless.
    False when it did not arrive - the caller posts it as a note instead, so
    the advisor still gets it."""
    base = _base()
    if not base:
        return False
    try:
        response = requests.post(f"{base}/drafts", json=draft, headers=_headers(), timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        _log(f"ticket {draft.get('ticket_id')}: could not deliver the draft ({exc.__class__.__name__})")
        return False
    return True
