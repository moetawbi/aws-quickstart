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
from datetime import date

import anthropic

from prompts import SYSTEM_PROMPT
from tools import TOOLS

MODEL = os.environ.get("CALL_CENTER_MODEL", "claude-opus-5")
# Call centers are latency-sensitive; "medium" balances quality and speed.
# Raise to "high" if answers need more depth, lower to "low" for speed.
EFFORT = os.environ.get("CALL_CENTER_EFFORT", "medium")
MAX_TOKENS = 16000
MAX_PAUSE_RESTARTS = 5


class CallCenterAgent:
    """One phone call: owns the conversation history across turns.

    The Messages API is stateless, so the full history is mirrored locally
    and re-sent each turn. The system prompt sits under a cache breakpoint,
    so on every turn after the first it is served from the prompt cache.
    """

    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.messages: list[dict] = []

    def send(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})

        # The tool runner exits when no client tool ran; a long server turn
        # can end with stop_reason "pause_turn", so restart it (bounded)
        # with the paused assistant turn already appended to the history.
        last = None
        for _ in range(MAX_PAUSE_RESTARTS + 1):
            runner = self.client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
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
        return "\n".join(b.text for b in last.content if b.type == "text").strip()


def _call_context() -> str:
    """Per-call context, injected as the first user turn (not the system
    prompt) so the cached system prefix stays byte-stable across calls."""
    return (
        f"[Call metadata - not spoken by the caller] Today's date: {date.today().isoformat()}. "
        "Channel: phone. Greet the caller now."
    )


def run_interactive() -> None:
    agent = CallCenterAgent()
    print("Summit Auto Group call center - type 'quit' to hang up.\n")
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
        "Hi, I'd like to get an oil change for my truck. My number is 555-123-0003.",
        "Yes, this is Elena Vasquez.",
        "What do you have early next week, mornings if possible?",
        "The first morning slot works. And yes, please take care of that recall at the same time.",
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
