# CRM Integration Spec — anb-used-vehicles

This document is the handoff spec for integrating the **automotive call
center agent** (this repo, branch `claude/automotive-call-center-agent-catotx`)
with the in-house **anb-used-vehicles** CRM. It is written to be executed
by a developer or a Claude Code session running inside the CRM codebase.

The integration has two independent halves, and **neither requires any
change to the agent's code**:

| Half | Direction | CRM builds | Agent side |
|---|---|---|---|
| A. Agent-facing API | agent → CRM | 4 small endpoints | already built (`crm.py` RestCRM) |
| B. Admin module | CRM → agent | 3 admin pages | already built (`admin_api.py`) |

---

## Part A — Agent-facing endpoints (customers + leads)

The agent identifies callers, fetches their vehicles, and creates sales
leads through a tiny REST surface. Implement these 4 endpoints in the CRM
(any base path is fine; it goes in the agent's `CRM_API_BASE_URL`, e.g.
`https://crm.example.com/api/agent`):

### Auth (all endpoints)

`Authorization: Bearer <token>` — one long random API token, stored in CRM
config and in the agent's `CRM_API_KEY`. Requests without a valid token
must get **401/403** (these endpoints expose customer PII). HTTPS in
production.

### A1. `GET {base}/customers?phone={digits}`

Search customers by phone. The agent sends the **last 10 digits only**
(no `+`, spaces, or dashes) — match loosely against stored formats.

Response `200` — either a bare JSON array or wrapped under `customers`,
`data`, `results`, or `items` (the agent unwraps all of these; empty array
= no match):

```json
{"data": [{
  "customer_id": "C-1003",
  "name": "Elena Vasquez",
  "phone": "+96170123456",
  "email": "elena@example.com",
  "vehicles": ["1FTFW1ET5DFC10312"]
}]}
```

Required fields: `customer_id` (string or int), `name`.
Recommended: `phone`, `email`, `vehicles` (**list of VIN strings** — the
agent uses them with its vehicle/service tools). Extra fields are fine and
visible to the agent, so include only what a service advisor may see.

### A2. `GET {base}/customers/{customer_id}`

One customer, same record shape. `404` if unknown.

### A3. `POST {base}/leads`

The agent posts this exact JSON body (all fields strings; `email`,
`customer_id`, `notes` may be empty):

```json
{
  "name": "Elena Vasquez",
  "phone": "+96170123456",
  "lead_type": "trade_in",
  "interest": "Trade 2022 F-150 Lariat for F-150 Lightning",
  "email": "",
  "customer_id": "C-1003",
  "notes": "Wants trade-in value discussed on the call",
  "source": "call_center"
}
```

`lead_type` is one of: `new_vehicle`, `used_vehicle`, `trade_in`,
`test_drive`, `service_contract`, `other`. Map these onto the CRM's lead
pipeline however fits (type column, tags, pipeline stage). `source` is
always `call_center` — route these to the right sales queue.

Response `200/201`: the created record **including a `lead_id`** (or `id`)
field. The agent reads the lead reference back to the customer.

### A4. `GET {base}/leads/{lead_id}`

One lead with at least `lead_id` and `status`. `404` if unknown.

### A5–A7. Vehicles (2026-10-29)

These replaced the demo data in `data_store.py` for `get_vehicle`,
`get_service_history` and `check_recalls`. `MockCRM` still answers them from
`data_store.py` when `CRM_API_BASE_URL` is unset. The CRM answers from its
own records only - never from IMAD's data.

| Endpoint | Tool | Who |
|---|---|---|
| `GET {base}/vehicles/{vin}?customer_id=` | `get_vehicle` | customer (own vehicles only) and staff - make/model/year, Ford Protect cover |
| `GET {base}/vehicles/{vin}/service-history` | `get_service_history` | staff only (403 for a customer) |
| `GET {base}/vehicles/{vin}/recalls` | `check_recalls` | staff only (403 for a customer) - the CRM's Recalls module |

"Staff" is a conversation carrying an actor grant whose CRM user has
`view service cases`.

### A8. `GET {base}/deployments` (Phase 3)

Which agent works which Trengo inbox, and how:
`{"data": [{"channel_id": "1333697", "channel_name": "...", "channel_type": "WA_BUSINESS", "agent": "call-center", "mode": "assist"}]}`.
`mode` is `assist` (draft the reply as an internal note for an advisor) or
`autopilot` (answer the customer); the CRM already reports `assist` unless its
server allows AutoPilot. `deployments.py` caches it for a minute;
`trengo_worker.py` leaves any inbox not listed - and every inbox while the CRM
is unreachable - to the team, and runs AutoPilot as Assist unless
`TRENGO_REPLIES_ENABLED=1`. The CRM rings `trengo_webhook.py` (bound to
127.0.0.1) with `{"ticket_id"}` for deployed inboxes; see DEPLOYMENT.md.

### Handover (not a CRM endpoint)

`escalate_to_human` re-assigns the Trengo ticket being answered to the
agent's handover team (`handover.trengo_team_id` in the agent config the CRM
serves) with a note: Trengo `POST /tickets/{id}/assign`
`{"type": "team", "team_id": ..., "note": ...}`. The worker then records the
ticket as handed over and never answers it again. There is no booking tool:
a workshop visit is handed over the same way, with the preferred day and
time in the note.

The customer gets one closing message with the handover: anything the note
says they were told (e.g. "our car workshops are closed on Saturdays, so
Monday at 8:00") plus "a colleague will continue here". `CallCenterAgent.send()`
returns every text block of a turn - text written beside a tool call
reaches the customer too - except that after a handover only the first
text is kept (later text is the model narrating the handover).

The worker also hands over by itself, without a model call, when the
customer's unanswered messages include a voice note, photo, video or file
(`trengo.media_kind`: Trengo's `body_type`, or WhatsApp's "Message type
unknown" notice) - the agent can only read text. Nothing is posted; the
note says what was sent. Stickers are answered normally.

The chat API can rehearse a handover for the CRM's eval: `"handover":
"dry_run"` on a read-only customer conversation gives the agent
escalate_to_human back, run against no real ticket, and the response
carries `{"team", "note"}`. There is no live handover through the chat API.

### Acceptance (Part A)

From the agent install (or any machine with this repo + Python):

```bash
export CRM_API_BASE_URL=http://anb-used-vehicles.test/api/agent
export CRM_API_KEY=<the token>
python crm_verify.py --phone <a real customer's phone>          # read-only
python crm_verify.py --phone <same> --create-lead               # + 1 test lead
```

All checks PASS (including "Auth enforcement") → set the same two values
in the agent's `.env` and the agent is live against the CRM. Done.

---

## Part B — Admin module (Knowledge, Manuals, Memory)

The agent ships an Admin API (`admin_api.py`, default `127.0.0.1:8081`,
same server as the CRM). Build three pages in the CRM's admin panel that
call it server-side (backend → localhost; don't call it from the browser).

Auth: `Authorization: Bearer <ADMIN_API_TOKEN>` (from the agent's `.env`;
store it in CRM config).

| Endpoint | Use |
|---|---|
| `GET /admin/status` | dashboard widget: model, store file counts, manual index stats |
| `GET /admin/{store}` | list files — `{"files": [{"name","size","modified"}, ...]}` |
| `GET /admin/{store}/{name}` | download / view file content (raw bytes) |
| `PUT /admin/{store}/{name}` | upload (raw file bytes as request body, **not** multipart) |
| `DELETE /admin/{store}/{name}` | delete |

Stores and what each page should say:

- **`knowledge`** (`.md .txt .csv .json .pdf`) — "Dealership facts the
  agent always knows: hours, policies, promotions, FAQs. Changes apply to
  the next new conversation automatically."
- **`manuals`** (`.md .txt .csv .json .pdf`) — "Full vehicle/service
  manuals. Indexed for search automatically after upload."
- **`memory`** (`.md .txt .json .csv`) — "Notes the agent has learned from
  conversations. View to audit; edit or delete with care." Show file
  content read-only by default with an explicit edit action.

No reload/restart buttons are needed anywhere — the agent picks up all
changes automatically. Upload errors to surface: `401` bad token, `413`
too large (default cap 25 MB), `415` wrong file type for the store.

### Acceptance (Part B)

Upload a file from the CRM page, then `GET /admin/status` shows it counted
(for manuals, listed under `manual_index.sources`); delete it and the
count drops. View a memory file and see the agent's notes.

---

## Kickoff prompt for a Claude Code session in the CRM repo

Paste this into `claude` running in `/Users/moetawbi/Sites/anb-used-vehicles`:

> Read the integration spec at
> https://raw.githubusercontent.com/moetawbi/aws-quickstart/claude/automotive-call-center-agent-catotx/automotive-call-center-agent/CRM_INTEGRATION.md
> and implement it in this CRM codebase, following this repo's existing
> framework, routing, auth, and UI conventions:
> **Part A**: the 4 agent-facing REST endpoints (customer search by phone,
> customer by id, create lead, lead by id) with bearer-token auth, mapped
> onto our real customers/vehicles/leads tables.
> **Part B**: three admin pages (Knowledge, Manuals, Memory) that manage
> the agent's stores through its localhost Admin API exactly as specified.
> Store both tokens in this app's configuration, not hardcoded. Then tell
> me the CRM_API_BASE_URL to give the agent, and how to run the spec's
> acceptance checks against this local environment.

When Part A passes `crm_verify.py`, report the base URL + token setup back
to the agent's `.env` (`CRM_API_BASE_URL`, `CRM_API_KEY`) — the agent
switches from mock to live CRM with no code change.
