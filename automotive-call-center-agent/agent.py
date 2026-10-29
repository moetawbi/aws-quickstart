"""Automotive call center agent built on the Claude API.

The Anthropic SDK's tool runner drives the agentic loop: Claude decides
which dealership tools to call (customer lookup, scheduling, recalls, ...),
the SDK executes them, and iteration continues until Claude has a final
answer for the caller.

Run interactively:
    export ANTHROPIC_API_KEY=...
    python agent.py

Or run the scripted demo call:
    python agent.py --demo
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from datetime import date

import anthropic

import agent_config
import agent_stores
from knowledge import get_knowledge_text
from memory_store import FileMemoryTool, ReadOnlyFileMemoryTool, memory_dir
from prompts import INTERNAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from tools import READ_ONLY_TOOLS, TOOLS, escalate_to_human

# One shared memory store: what the agent learns in any conversation
# (phone or Trengo) is available to every later conversation.
MEMORY = FileMemoryTool()

# The same directory, viewable but not writable - for internal staff chats.
READ_ONLY_MEMORY = ReadOnlyFileMemoryTool()

# Who the agent is talking to. "customer" is every real channel (phone,
# Trengo); "internal" is a staff member in the CRM chat, which swaps in the
# internal prompt and withholds every write tool.
PERSONA_CUSTOMER = "customer"
PERSONA_INTERNAL = "internal"
PERSONAS = (PERSONA_CUSTOMER, PERSONA_INTERNAL)

MODEL = os.environ.get("CALL_CENTER_MODEL", "claude-opus-5")
# Call centers are latency-sensitive; "medium" balances quality and speed.
# Raise to "high" if answers need more depth, lower to "low" for speed.
EFFORT = os.environ.get("CALL_CENTER_EFFORT", "medium")
MAX_TOKENS = 16000
MAX_PAUSE_RESTARTS = 5


def build_system_blocks(persona: str = PERSONA_CUSTOMER, config: dict | None = None) -> list[dict]:
    """System prompt plus the optional knowledge base, with the prompt-cache
    breakpoint on the last block so the whole prefix is cached. Each new
    conversation snapshots the current knowledge (get_knowledge_text reloads
    only when the directory changes), so knowledge edits - e.g. uploads via
    the admin API - apply to new conversations without a restart while
    ongoing conversations keep a byte-stable, cacheable prefix.

    Each persona has its own byte-stable prefix, so both cache independently.

    When a CRM agent config is given its system prompt replaces the built-in
    one — that is the whole point of agents being CRM records. Without one the
    module constants still apply, so the phone CLI and an unreachable CRM both
    keep working."""
    prompt = INTERNAL_SYSTEM_PROMPT if persona == PERSONA_INTERNAL else SYSTEM_PROMPT
    blocks = [{"type": "text", "text": config["system_prompt"] if config else prompt}]
    knowledge = get_knowledge_text((config or {}).get("slug"))
    if knowledge:
        blocks.append({"type": "text", "text": knowledge})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _is_memory_tool(tool) -> bool:
    """The memory tool is part of the runtime, not a CRM-granted capability."""
    return isinstance(tool, FileMemoryTool)  # ReadOnlyFileMemoryTool included


# One memory tool per (agent, read-only) pair, so every conversation with the
# same agent shares one lock on that agent's notebook.
_memory_tools: dict[tuple[str | None, bool], FileMemoryTool] = {}
_memory_tools_lock = threading.Lock()


def memory_tool_for(config: dict | None, read_only: bool) -> FileMemoryTool:
    """The notebook an agent reads and writes.

    ``memory_scope: shared`` (the seeded agents, and anything with no config)
    is the original single notebook every channel has always shared.
    ``memory_scope: own`` gives the agent a private notebook under
    ``memory/agents/<slug>/``, so two agents never edit each other's notes
    about the same customer.
    """
    slug = agent_stores.safe_slug((config or {}).get("slug"))
    own = (config or {}).get("memory_scope") == "own" and slug is not None

    if not own:
        return READ_ONLY_MEMORY if read_only else MEMORY

    key = (slug, read_only)
    with _memory_tools_lock:
        tool = _memory_tools.get(key)
        if tool is None:
            root = agent_stores.agent_dir(memory_dir(), slug)
            tool = ReadOnlyFileMemoryTool(root) if read_only else FileMemoryTool(root)
            _memory_tools[key] = tool
        return tool


def agent_for(slug: str | None = None, **kwargs) -> "CallCenterAgent":
    """A CallCenterAgent configured from the CRM, falling back to the built-in
    prompts when the CRM has nothing to say."""
    return CallCenterAgent(config=agent_config.fetch_or_none(slug), **kwargs)


class CallCenterAgent:
    """One phone call: owns the conversation history across turns.

    The Messages API is stateless, so the full history is mirrored locally
    and re-sent each turn. The system prompt sits under a cache breakpoint,
    so on every turn after the first it is served from the prompt cache.
    """

    def __init__(self, client: anthropic.Anthropic | None = None,
                 persona: str = PERSONA_CUSTOMER, read_only: bool | None = None,
                 config: dict | None = None, handover_rehearsal: bool = False):
        """persona picks the brain (customer-facing "Alex" vs the internal
        staff assistant); read_only picks the tool surface. They are separate
        because a staff member previewing the customer experience wants the
        customer brain without the power to book a real appointment or create
        a real CRM lead. read_only defaults to True for internal chats and
        False for real customer channels (phone, Trengo).

        handover_rehearsal gives a read-only customer conversation
        escalate_to_human back, for the eval: the caller must run each turn
        with a dry-run Trengo context, so the handover is recorded but no
        ticket is touched."""
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona!r}; use one of {PERSONAS}")
        if read_only is None:
            read_only = persona == PERSONA_INTERNAL
        self.client = client or anthropic.Anthropic()
        self.persona = persona
        self.read_only = read_only
        # Snapshotted for the life of the conversation, prompt included: an
        # edit in the CRM lands on the next new conversation, never mid-call.
        self.config = config
        self.agent_slug = (config or {}).get("slug")
        self.messages: list[dict] = []
        self.system_blocks = build_system_blocks(persona, config)  # per-conversation snapshot
        self.model = (config or {}).get("model") or MODEL
        self.effort = (config or {}).get("effort") or EFFORT
        # Read-only surface: no booking, cancelling, lead creation or
        # escalation, and memory can be viewed but never edited.
        memory = memory_tool_for(config, read_only)
        tools = list(READ_ONLY_TOOLS if read_only else TOOLS)
        if read_only and handover_rehearsal and persona == PERSONA_CUSTOMER:
            tools.append(escalate_to_human)
        self.tools = [*tools, memory]

        # An agent config may narrow the surface further. `tools` absent/null
        # means "not configured" and keeps the full surface; a list - even an
        # empty one - is exact, so an agent with every skill switched off
        # answers from knowledge alone instead of silently getting everything.
        # Unknown names are ignored rather than fatal, so adding a tool in the
        # CRM before it exists here degrades to "not available", not a crash.
        allowed = (config or {}).get("tools")
        if allowed is not None:
            allowed = set(allowed)
            self.tools = [
                tool for tool in self.tools
                if getattr(tool, "name", None) in allowed or _is_memory_tool(tool)
            ]

    def send(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        turn_start = len(self.messages)

        # The tool runner exits when no client tool ran; a long server turn
        # can end with stop_reason "pause_turn", so restart it (bounded)
        # with the paused assistant turn already appended to the history.
        last = None
        for _ in range(MAX_PAUSE_RESTARTS + 1):
            runner = self.client.beta.messages.tool_runner(
                model=self.model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=self.system_blocks,
                tools=self.tools,
                messages=self.messages,
            )
            for message in runner:
                last = message
                # Mirror the history - the runner keeps its own copy and
                # does not expose it. Content blocks (thinking included)
                # are echoed back unchanged on the same model.
                self.messages.append({"role": "assistant", "content": message.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    self.messages.append(tool_response)
            if last is None or last.stop_reason != "pause_turn":
                break
        else:
            raise RuntimeError("Turn still paused after maximum restarts.")

        if last is None:
            return ""
        if last.stop_reason == "refusal":
            details = getattr(last, "stop_details", None)
            explanation = getattr(details, "explanation", None) if details else None
            return explanation or (
                "I'm sorry, I can't help with that request. Is there anything "
                "else I can do for your vehicle today?"
            )
        # Every text block of the turn, not just the last message's: the model
        # often says something next to a tool call ("Nahr El Mot is closed on
        # Saturdays") and then refers back to it ("as mentioned") afterwards.
        texts = [
            block.text.strip()
            for message in self.messages[turn_start:] if message["role"] == "assistant"
            for block in message["content"]
            if getattr(block, "type", None) == "text" and block.text.strip()
        ]
        return "\n\n".join(texts)


def _call_context() -> str:
    """Per-call context, injected as the first user turn (not the system
    prompt) so the cached system prefix stays byte-stable across calls."""
    return (
        f"[Call metadata - not spoken by the caller] Today's date: {date.today().isoformat()}. "
        "Channel: phone. Greet the caller now."
    )


def run_interactive() -> None:
    agent = CallCenterAgent()
    print("ANB call center - type 'quit' to hang up.\n")
    print(f"Alex: {agent.send(_call_context())}\n")
    while True:
        try:
            user_input = input("Caller: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit", "bye"}:
            break
        try:
            print(f"\nAlex: {agent.send(user_input)}\n")
        except anthropic.RateLimitError as e:
            retry_after = e.response.headers.get("retry-after", "60")
            print(f"[rate limited - retry in {retry_after}s]\n", file=sys.stderr)
        except anthropic.APIStatusError as e:
            print(f"[API error {e.status_code}: {e.message}]\n", file=sys.stderr)
        except anthropic.APIConnectionError:
            print("[network error - check connectivity and try again]\n", file=sys.stderr)


def run_demo() -> None:
    """Scripted call exercising lookup, recalls, scheduling, and booking."""
    agent = CallCenterAgent()
    turns = [
        _call_context(),
        "Hi - quick question first: are you open Saturdays, and do you have a shuttle?",
        "Great. I'd like to get an oil change for my truck. My number is 555-123-0003.",
        "Yes, this is Elena Vasquez.",
        "What do you have early next week, mornings if possible?",
        "The first morning slot works. And yes, please take care of that recall at the same time.",
        "While I have you - what's the lug nut torque on this truck? I rotated the tires myself last week.",
        "One more thing - I'm thinking about trading this truck in for the new Lightning. "
        "Can someone from sales call me about that?",
        "Yes, same number is fine, and you can note I'd want to keep my trade-in value in mind. Go ahead.",
    ]
    for turn in turns:
        print(f"Caller: {turn}\n")
        print(f"Alex: {agent.send(turn)}\n{'-' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Automotive call center agent (Claude)")
    parser.add_argument("--demo", action="store_true", help="run a scripted demo call")
    args = parser.parse_args()
    if args.demo:
        run_demo()
    else:
        run_interactive()


if __name__ == "__main__":
    main()
