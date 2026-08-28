# Automotive Call Center Agent (Claude)

A virtual service advisor ("Alex") for an automotive dealership call
center, built on the [Claude API](https://docs.claude.com) with the
Anthropic Python SDK's tool runner driving the agentic loop.

The agent can:

- **Identify callers and fetch customer details** from the CRM API
- **Create sales leads in the CRM** (new/used vehicle, test drive,
  trade-in, service contract) with the caller's consent, and check lead status
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
tools.py        Fourteen @beta_tool functions. The SDK generates JSON schemas
                from the type hints + docstrings and executes calls for the
                tool runner. Customer lookup and lead creation route through
                the CRM client.
crm.py          The CRM client layer: RestCRM (real HTTP API, selected when
                CRM_API_BASE_URL is set) and MockCRM (in-memory default).
prompts.py      The system prompt: call-handling procedure, identity
                verification, recall policy, lead capture with consent,
                escalation rules, phone style.
data_store.py   Mock DMS/scheduling/recall data, plus the MockCRM's backing
                store. The integration seam for non-CRM systems: replace
                these dicts and helpers with your real DMS/scheduler.
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
| `CRM_API_BASE_URL` | — (mock CRM) | Base URL of your CRM's REST API; setting it switches the agent from the mock to the live CRM |
| `CRM_API_KEY` | — | Bearer token sent as `Authorization: Bearer <key>` on CRM requests |
| `CRM_TIMEOUT_SECONDS` | `10` | CRM HTTP request timeout |

## CRM integration

`crm.py` defines one small interface with two backends. With no
configuration you get `MockCRM` (in-memory, for local dev). Set
`CRM_API_BASE_URL` (+ `CRM_API_KEY`) and the same tools hit your real CRM
over REST. The expected endpoint contract:

| Method & path | Purpose |
|---|---|
| `GET  /customers?phone={digits}` | Search customers by phone (a bare list, or a wrapper object keyed `customers`, `data`, `results`, or `items`, is accepted) |
| `GET  /customers/{customer_id}` | Fetch one customer record |
| `POST /leads` | Create a lead (JSON body below) |
| `GET  /leads/{lead_id}` | Fetch one lead |

Lead payload the agent POSTs:

```json
{
  "name": "Elena Vasquez",
  "phone": "+15551230003",
  "lead_type": "trade_in",
  "interest": "Trade 2022 F-150 Lariat for F-150 Lightning",
  "email": "",
  "customer_id": "C-1003",
  "notes": "Wants trade-in value discussed on the call",
  "source": "call_center"
}
```

If your CRM's API differs (Salesforce, HubSpot, DealerSocket,
VinSolutions, ...), adapt `RestCRM`'s four methods — the tools and prompt
don't change. CRM failures surface as `{"error": ...}` results, which the
agent explains gracefully and, when appropriate, escalates instead of
losing the caller.

The agent only creates a lead after confirming the details and getting
the caller's explicit consent to be contacted, and it reads back the
lead ID as a reference.

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
