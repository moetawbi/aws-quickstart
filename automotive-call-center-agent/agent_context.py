"""Per-conversation context for tool calls.

Tools are plain module-level functions handed to the tool runner, so they
cannot see which conversation they are serving. This holds the bits they need
for the current turn in a ContextVar, set by whichever channel is driving:

- ``actor_grant`` - an opaque, CRM-issued grant naming the staff member behind
  an internal chat. The CRM encrypts it with its own key; the agent never
  reads it, only echoes it back so the CRM can apply that user's permissions.
  Unset on a phone call or a customer chat, which is what makes those
  conversations customer-scoped.
- ``customer_id`` - the caller the agent has already identified, so
  account-scoped tools do not have to ask the model to repeat it.
- ``agent_slug`` - which configured agent is answering, so stores resolve to
  that agent's own knowledge, manuals and memory rather than the shared set.

The chat API sets these per turn; agent.py and trengo_worker.py leave them
unset, so every real customer channel stays customer-scoped by default.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

_actor_grant: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "actor_grant", default=None
)
_customer_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "customer_id", default=None
)
_agent_slug: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_slug", default=None
)
_agent_config: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_config", default=None
)


def get_actor_grant() -> str | None:
    return _actor_grant.get()


def get_customer_id() -> str | None:
    return _customer_id.get()


def get_agent_slug() -> str | None:
    """The agent answering this turn, or None for the shared/built-in stores."""
    return _agent_slug.get()


def get_agent_config() -> dict | None:
    """The config snapshotted when this conversation began - what tools read
    so a mid-call CRM edit never changes the conversation under them."""
    return _agent_config.get()


def set_customer_id(customer_id: str | None) -> None:
    """Remembered when a lookup identifies the caller, so later tools can
    default to them."""
    _customer_id.set(customer_id or None)


@contextmanager
def conversation(actor_grant: str | None = None, customer_id: str | None = None,
                 agent_slug: str | None = None, agent_config: dict | None = None):
    """Scope one turn's context; always resets, so a failed turn cannot leak
    an actor - or the wrong agent's stores - into the next conversation
    handled by this thread."""
    grant_token = _actor_grant.set(actor_grant or None)
    customer_token = _customer_id.set(customer_id or None)
    agent_token = _agent_slug.set(agent_slug or None)
    config_token = _agent_config.set(agent_config or None)
    try:
        yield
    finally:
        _actor_grant.reset(grant_token)
        _customer_id.reset(customer_token)
        _agent_slug.reset(agent_token)
        _agent_config.reset(config_token)


def headers() -> dict[str, str]:
    """Headers the CRM client adds to every request for this turn."""
    grant = get_actor_grant()
    return {"X-Agent-Actor": grant} if grant else {}
