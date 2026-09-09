# Deploying on your CRM server

How to install the agent on the same Linux server as your CRM, run it as
services, and manage Knowledge, Manuals, and Memory from your CRM backend
through the Admin API.

## Architecture on the server

```
                    Internet
                       │
             ┌─────────▼──────────┐
             │  your reverse proxy │  exposes ONLY /webhooks/trengo (TLS)
             └─────────┬──────────┘
   ┌───────────────────┼─────────────────────────────────┐
   │  CRM server       ▼                                 │
   │   trengo_webhook.py  :8080   (agent replies)        │
   │   admin_api.py       :8081   (bound to 127.0.0.1)   │
   │        ▲                                            │
   │        │ localhost HTTP                             │
   │   your CRM backend ──────► manages knowledge/,      │
   │                            manuals/, memory/        │
   └──────────────────────────────────────────────────────┘
   Outbound HTTPS needed: api.anthropic.com, app.trengo.com
```

## 1. Install

Requirements: Linux, Python 3.10+, outbound HTTPS to `api.anthropic.com`
and `app.trengo.com`. Nothing else — no database, no queue.

```bash
sudo mkdir -p /opt/call-center-agent && sudo chown $USER /opt/call-center-agent
git clone https://github.com/moetawbi/aws-quickstart.git /tmp/acc-src
cd /tmp/acc-src && git checkout claude/automotive-call-center-agent-catotx
cp -r automotive-call-center-agent/* /opt/call-center-agent/
cd /opt/call-center-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env && chmod 600 .env
```

Edit `.env` — minimum for server testing:

```bash
ANTHROPIC_API_KEY=sk-ant-...
TRENGO_API_KEY=...
TRENGO_WEBHOOK_SECRET=<openssl rand -hex 24>
ADMIN_API_TOKEN=<openssl rand -hex 24>
```

Verify against your real Trengo account before starting services:

```bash
set -a; source .env; set +a
.venv/bin/python trengo_verify.py --webhook
```

## 3. Run as services (systemd)

`/etc/systemd/system/cc-agent-webhook.service`:

```ini
[Unit]
Description=Call center agent - Trengo webhook listener
After=network-online.target

[Service]
WorkingDirectory=/opt/call-center-agent
EnvironmentFile=/opt/call-center-agent/.env
ExecStart=/opt/call-center-agent/.venv/bin/python trengo_webhook.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/cc-agent-admin.service`: same file with
`Description=... admin API` and `ExecStart=... admin_api.py`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cc-agent-webhook cc-agent-admin
journalctl -u cc-agent-webhook -f     # watch it work
```

Optional polling safety net for missed webhook deliveries (shares dedupe
state, so nothing is ever answered twice):

```bash
# crontab -e
*/5 * * * * cd /opt/call-center-agent && set -a && . ./.env && set +a && .venv/bin/python trengo_worker.py --once >> /var/log/cc-agent-poll.log 2>&1
```

## 4. Expose the webhook (only the webhook)

Only `/webhooks/trengo` needs to be public. Nginx example on the CRM's
existing TLS site:

```nginx
location /webhooks/trengo {
    proxy_pass http://127.0.0.1:8080/webhooks/trengo;
    proxy_set_body $request_body;
}
```

Then in Trengo → Settings → Webhooks, point an inbound-message webhook at
`https://crm.yourdomain.com/webhooks/trengo?token=<TRENGO_WEBHOOK_SECRET>`.

Do **not** proxy `/admin/` — the admin API stays on `127.0.0.1:8081`,
reachable only by processes on the same server (i.e. your CRM backend).

## 5. Manage Knowledge, Manuals, Memory from the CRM backend

The Admin API is plain HTTP on localhost; every request carries
`Authorization: Bearer <ADMIN_API_TOKEN>`. Stores: `knowledge`
(`.md .txt .csv .json .pdf` — inlined into new conversations), `manuals`
(same types — chunked and searched), `memory` (`.md .txt .json .csv` —
the agent's own notes).

```bash
TOKEN=...  # ADMIN_API_TOKEN
A="http://127.0.0.1:8081/admin"

curl -H "Authorization: Bearer $TOKEN" $A/status            # overview
curl -H "Authorization: Bearer $TOKEN" $A/knowledge         # list files
curl -H "Authorization: Bearer $TOKEN" -T promos.md  $A/knowledge/promos.md   # upload
curl -H "Authorization: Bearer $TOKEN" -T manual.pdf $A/manuals/f150_2023.pdf # upload
curl -H "Authorization: Bearer $TOKEN" $A/memory/customers.md                 # read agent notes
curl -H "Authorization: Bearer $TOKEN" -X DELETE $A/knowledge/old_promo.md    # delete
```

Changes apply automatically — **no restart**: a knowledge upload takes
effect on the next new conversation, a manual upload is re-indexed on the
next search, memory files are live immediately. Ongoing conversations keep
their snapshot.

PHP example for a CRM admin page (list + upload):

```php
function agentAdmin(string $method, string $path, ?string $body = null) {
    $ch = curl_init("http://127.0.0.1:8081/admin/" . $path);
    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => ["Authorization: Bearer " . getenv("AGENT_ADMIN_TOKEN")],
    ]);
    if ($body !== null) curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    $out = curl_exec($ch);
    curl_close($ch);
    return $out;
}

$files = json_decode(agentAdmin("GET", "knowledge"), true)["files"];        // list
agentAdmin("PUT", "knowledge/promos.md", file_get_contents($upload));       // upload
agentAdmin("DELETE", "manuals/old_manual.pdf");                             // delete
$notes = agentAdmin("GET", "memory/customers.md");                          // agent notes
```

Wire those calls behind three CRM admin pages (Knowledge, Manuals, Memory)
with a file list, an upload button, and a delete button each — that is the
entire integration.

## 6. Operations

- **Logs:** everything logs to stderr with `[trengo]`, `[trengo-webhook]`,
  `[admin]`, `[knowledge]`, `[manuals]` prefixes → `journalctl -u <unit>`.
- **Back up** `/opt/call-center-agent/memory/` (the agent's learned notes)
  and your `knowledge/` + `manuals/` content; `.trengo_state.json` prevents
  double replies after restarts — keep it with the install.
- **Upgrades:** `git pull` the branch, `pip install -r requirements.txt`,
  `systemctl restart cc-agent-webhook cc-agent-admin`.
- **Kill switch:** `systemctl stop cc-agent-webhook` (and remove the cron
  line) stops all replies immediately; tickets stay in Trengo for humans.
