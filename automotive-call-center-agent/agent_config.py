"""Agent configuration, loaded from the CRM.

An agent used to be this package: two prompt constants and one tool list. It
is now a CRM record, and this module fetches the resolved form of it from
``GET {CRM_API_BASE_URL}/config/{slug}`` using the same key the CRM tools use.

Two rules shape this:

- **Snapshot per conversation.** A config is fetched when a conversation
  starts and then held, so an edit in the CRM applies to the next NEW
  conversation and never rewrites a prompt mid-call. That is what keeps the
  cached system prefix byte-stable, the same way knowledge already behaves.
- **Never go silent.** If the CRM is unreachable or has no such agent, the
  caller falls back to the built-in prompts rather than failing the call — a
  customer on the phone must still be answered.
"""

from __future__ import annotations

import os
import threading
import time

import requests

CONFIG_TTL = float(os.environ.get("AGENT_CONFIG_TTL_SECONDS", "300"))
TIMEOUT = float(os.environ.get("AGENT_CONFIG_TIMEOUT", "10"))


class AgentConfigError(RuntimeError):
    """The CRM could not give us a usable config."""


_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _base_url() -> str:
    return os.environ.get("CRM_API_BASE_URL", "").strip().rstrip("/")


def available() -> bool:
    """Whether agent configs can be fetched at all (the CRM is configured)."""
    return bool(_base_url())


def fetch(slug: str | None = None, *, refresh: bool = False) -> dict:
    """The resolved config for one agent, or the CRM's default agent.

    Cached briefly so a burst of new conversations does not hammer the CRM;
    pass refresh=True after editing an agent.
    """
    base = _base_url()
    if not base:
        raise AgentConfigError("CRM_API_BASE_URL is not set; no agent configs available.")

    key = slug or "__default__"

    if not refresh:
        with _lock:
            cached = _cache.get(key)
            if cached and (time.time() - cached[0]) < CONFIG_TTL:
                return cached[1]

    url = f"{base}/config" + (f"/{slug}" if slug else "")
    headers = {"Accept": "application/json"}
    api_key = os.environ.get("CRM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AgentConfigError(f"CRM unreachable for agent config ({exc.__class__.__name__}).") from exc

    if response.status_code == 404:
        raise AgentConfigError(f"No agent '{slug}' in the CRM." if slug else "No active agent in the CRM.")
    if not response.ok:
        raise AgentConfigError(f"CRM returned HTTP {response.status_code} for the agent config.")

    try:
        config = response.json()
    except ValueError as exc:
        raise AgentConfigError("CRM returned a non-JSON agent config.") from exc

    if not isinstance(config, dict) or not config.get("system_prompt"):
        raise AgentConfigError("Agent config has no system prompt.")

    with _lock:
        _cache[key] = (time.time(), config)

    return config


def fetch_or_none(slug: str | None = None) -> dict | None:
    """fetch(), but None instead of raising — for callers that fall back to
    the built-in prompts."""
    try:
        return fetch(slug)
    except AgentConfigError:
        return None


def clear_cache() -> None:
    with _lock:
        _cache.clear()
