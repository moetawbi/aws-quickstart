# Automotive Call Center Agent (Claude)

A virtual service advisor ("Alex") for an automotive dealership call
center, built on the [Claude API](https://docs.claude.com) with the
Anthropic Python SDK's tool runner driving the agentic loop.

The agent can:

- **Identify callers** by phone number against the CRM
- **Look up vehicles** — year/make/model, mileage, warranty status
- **Check open safety recalls** (proactively, on every vehicle lookup)
- **Quote service pricing** from the dealership's service menu
- **Book, list, and cancel service appointments** against live availability
- **Check repair status** for vehicles currently in the shop
- **Escalate to a human advisor** for disputes, complaints, safety issues,
  or anything outside its tools

## Quick start

```bash
cd automotive-call-center-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # or `ant auth login`

python agent.py          # interactive call
python agent.py --demo   # scripted demo call
```

Example demo exchange (mock data ships in `data_store.py`, so it runs with
no other setup):

```
Caller: Hi, I'd like to get an oil change for my truck. My number is 555-123-0003.
Alex:   Thanks — I have Elena Vasquez with a 2022 Ford F-150, is that right?
        One thing before we book: your truck has an open safety recall on the
        rearview camera, and the fix is free. Want me to add that to the visit?
```

## Architecture

```
agent.py        The call loop. One CallCenterAgent per phone call; mirrors
                conversation history (the API is stateless) and restarts the
                tool runner across turns, handling pause_turn and refusals.
tools.py        Eleven @beta_tool functions. The SDK generates JSON schemas
                from the type hints + docstrings and executes calls for the
                tool runner.
prompts.py      The system prompt: call-handling procedure, identity
                verification, recall policy, escalation rules, phone style.
data_store.py   Mock CRM/DMS/scheduling/recall data. The integration seam:
                replace these dicts and helpers with calls to your real
                dealership management system.
```

The agentic loop is `client.beta.messages.tool_runner(...)`: Claude decides
which tools to call, the SDK executes them and feeds results back, and
iteration ends when Claude has a final spoken reply for the caller.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API credential (or use an `ant auth login` profile) |
| `CALL_CENTER_MODEL` | `claude-opus-5` | Claude model to use |
| `CALL_CENTER_EFFORT` | `medium` | Thinking effort: `low`–`max`. `low` for fastest responses, `high`+ for harder reasoning |

Cost/latency notes baked in:

- **Prompt caching** — the system prompt sits under a `cache_control`
  breakpoint, so every turn after the first reads it from cache (~90%
  cheaper on that prefix). Per-call context (today's date, channel) is
  injected as the first *user* message so the cached prefix stays
  byte-stable.
- **Adaptive thinking** at `medium` effort keeps phone-style replies fast;
  tune per route with `CALL_CENTER_EFFORT`.

## Guardrails

- Account details are only shared after the caller is identified via
  `lookup_customer` — the model is instructed to never invent prices,
  slots, or recall data and to confirm before booking/cancelling.
- Open recalls are surfaced proactively and offered (never pushed).
- Safety issues (brakes, smoke, fuel smell) get a "don't drive it"
  warning and an urgent escalation; emergencies are directed to 911.
- `stop_reason == "refusal"` is handled explicitly with a graceful
  fallback line.
- Tool failures return structured `{"error": ...}` payloads (never raise),
  so the model recovers conversationally instead of crashing the call.

## Integrating with real systems

`data_store.py` is the only file that knows the data is fake. To go to
production, keep the tool signatures in `tools.py` and re-point their
bodies at your dealership management system (CDK, Reynolds & Reynolds,
Tekion, ...), CRM, and scheduling APIs. For telephony, put `CallCenterAgent`
behind your IVR/voice stack (e.g. Amazon Connect + Lex, or a
speech-to-text/text-to-speech bridge): one `CallCenterAgent` instance per
live call, feeding transcribed caller utterances to `agent.send(...)`.
