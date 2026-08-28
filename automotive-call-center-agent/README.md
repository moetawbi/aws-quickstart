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
- **Answer customers on Trengo** — a worker watches open Trengo tickets
  and replies on the ticket (WhatsApp, chat, email) using the same agent
  and tools
- **Answer from your own documents** — drop files into `knowledge/`
  (hours, policies, promotions, FAQs) and the agent uses them as
  authoritative reference material
- **Search full service manuals** — files in `manuals/` are chunked and
  BM25-indexed at startup; the agent searches them on demand for specs,
  maintenance schedules, warning lamps, and towing limits

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
tools.py        Fifteen @beta_tool functions. The SDK generates JSON schemas
                from the type hints + docstrings and executes calls for the
                tool runner. Customer lookup and lead creation route through
                the CRM client.
crm.py          The CRM client layer: RestCRM (real HTTP API, selected when
                CRM_API_BASE_URL is set) and MockCRM (in-memory default).
knowledge.py    Loads reference documents from knowledge/ into a cached
                system block at startup (.md/.txt/.csv/.json/.pdf).
knowledge/      Your reference documents. Ships with a sample dealership
                info sheet (hours, policies, promotions, FAQ).
retrieval.py    Chunks files in manuals/ (heading-aware for text, per-page
                for PDFs) and BM25-indexes them; the search_service_manuals
                tool queries the index on demand.
manuals/        Full service/owner manuals - any size. Ships with an
                F-150 owner manual excerpt.
trengo.py       Trengo REST client (tickets, messages, replies) with
                tolerant message-shape parsing.
trengo_worker.py Polls open Trengo tickets and answers new customer
                messages with the agent; deduplicates and persists state.
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
| `KNOWLEDGE_DIR` | `./knowledge` | Directory of reference documents to load at startup |
| `KNOWLEDGE_MAX_CHARS_PER_FILE` | `100000` | Per-file size cap (larger files are truncated with a marker) |
| `KNOWLEDGE_MAX_CHARS_TOTAL` | `400000` | Total knowledge size cap (later files are skipped) |
| `MANUALS_DIR` | `./manuals` | Directory of service manuals to chunk and index for search |
| `TRENGO_API_KEY` | — | Trengo API token (Settings → API) for the Trengo worker |
| `TRENGO_BASE_URL` | `https://app.trengo.com/api/v2` | Trengo API base URL |
| `TRENGO_POLL_SECONDS` | `20` | How often the worker polls for new customer messages |
| `TRENGO_TICKET_STATUS` | `OPEN` | Which tickets the worker watches |
| `TRENGO_STATE_FILE` | `./.trengo_state.json` | Where replied-message ids persist across restarts |

## Feeding the agent knowledge files

Put reference documents in `knowledge/` (or point `KNOWLEDGE_DIR`
elsewhere) and restart the agent — no code changes:

- **Formats:** `.md`, `.txt`, `.csv`, `.json` (read as text) and `.pdf`
  (text extracted with pypdf). Other extensions are ignored.
- **How it's used:** files are concatenated (sorted by name, wrapped in
  `<document name="...">` tags) into a system block after the agent's
  instructions. The prompt tells the agent these documents are
  authoritative for general questions — hours, policies, promotions —
  while account-specific facts still come only from the tools, and to
  escalate rather than guess when neither covers a question.
- **Cost:** the knowledge block sits under the prompt-cache breakpoint,
  so after the first turn of a call it is read from cache at ~10% of the
  normal input price. Keep it byte-stable while the process runs; edits
  apply on restart.
- **Scale:** size caps (see configuration) keep the context sane. For
  documents too large to inline — full service manuals — use `manuals/`
  and retrieval instead (next section).

A sample `knowledge/dealership_info.md` ships with hours, transportation
policies, financing options, and current promotions; the demo call's
first question ("are you open Saturdays, do you have a shuttle?") is
answered from it.

## Full service manuals (retrieval)

Manuals don't fit in a prompt, so `manuals/` works differently from
`knowledge/`: at startup `retrieval.py` splits each file into ~1600-char
chunks (heading-aware for Markdown/text, per-page for PDFs, with overlap
so boundary-straddling facts stay findable) and builds a BM25 index over
them. The agent gets a `search_service_manuals` tool and pulls in only
the top few matching passages per question — a 900-page PDF costs nothing
until a caller asks about torque specs.

- **Formats:** same as `knowledge/` — `.md`, `.txt`, `.csv`, `.json`,
  and `.pdf` (per-page via pypdf).
- **Prompting:** the agent quotes specs exactly, names the source
  section, says so when the manuals don't cover a question, and gives
  lookups rather than repair walk-throughs — safety-critical work is
  redirected to a service visit.
- **No infrastructure:** BM25 is pure Python in-process — no vector
  database, embedding API, or network calls, and lexical matching fits
  manual lookups ("lug nut torque", "coolant capacity") well. If you
  later want semantic search (paraphrased questions across huge corpora),
  swap `BM25Index` for an embedding store (e.g. Voyage AI + a vector DB);
  the chunker, tool, and prompt are unchanged.
- **Indexing cost:** in-memory and fast (tens of MB of text index in
  seconds at startup); re-index by restarting after manual changes.

A sample `manuals/f150_2022_owner_manual_excerpt.md` ships with tire and
torque specs, fluid capacities, maintenance schedules, warning lamp
meanings, and towing limits; the demo call's lug-nut-torque question is
answered from it.

## Answering customers on Trengo

`trengo_worker.py` connects the agent to your [Trengo](https://trengo.com)
inbox so it answers tickets (WhatsApp, live chat, email) with the same
tools and guardrails as the phone flow:

```bash
export ANTHROPIC_API_KEY=...
export TRENGO_API_KEY=...      # Trengo Settings → API
python trengo_worker.py        # watch tickets continuously
python trengo_worker.py --once # single cycle, e.g. from cron
```

How it works:

- The worker polls open tickets and answers a ticket only when its
  **latest** message is from the customer — it never replies to itself,
  and the id of the last answered message is persisted to a state file so
  restarts don't double-reply.
- Each ticket gets its own agent session. The first turn carries the
  channel type, the customer's name/phone from the ticket, and a replay
  of the ticket's earlier conversation, so the agent picks up mid-thread
  correctly; later turns feed just the new message.
- The agent is told it's a written channel: reply in the customer's
  language, stay brief, no re-greeting every message. Identity is still
  verified via the CRM before account details are shared, and escalations
  still open advisor tickets.
- Internal notes are ignored; agent errors are logged and retried on the
  next cycle without marking the message answered.

Polling needs no public URL. To go push-based instead, point a Trengo
webhook for inbound messages at a small HTTP endpoint that calls
`TrengoWorker.handle_ticket(ticket)` — the per-ticket logic is identical.

Endpoint paths and message-field parsing live in `trengo.py`; if your
Trengo account's API differs from the v2 shapes used there (see
developers.trengo.com), that's the one file to adjust.

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
