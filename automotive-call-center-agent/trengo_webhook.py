"""Trengo webhook listener: answer tickets the moment a customer writes.

Runs a small HTTP server (stdlib only) that receives Trengo webhook calls
for inbound messages and hands the ticket to the same ``TrengoWorker``
logic the polling mode uses.

    export ANTHROPIC_API_KEY=...
    export TRENGO_API_KEY=...
    export TRENGO_WEBHOOK_SECRET=some-long-random-string
    python trengo_webhook.py           # listens on 0.0.0.0:8080

Then in Trengo (Settings > Webhooks) create a webhook for inbound message
events pointing at:

    https://<your-public-host>/webhooks/trengo?token=<TRENGO_WEBHOOK_SECRET>

Design notes:

- **The webhook is only a doorbell.** The handler never trusts the
  payload's message content; it extracts the ticket id, returns 200
  immediately, and a background worker re-fetches the ticket through the
  Trengo API and replies only if the latest message is from the customer.
  Duplicate, replayed, or out-of-order webhook deliveries are therefore
  harmless, and the payload cannot inject text into the agent.
- **Fast 200s.** Trengo expects a quick response; agent runs take seconds,
  so tickets go on an in-process queue consumed by one worker thread.
  Multiple events for a ticket already queued collapse into one run.
- **Auth.** Requests must carry the shared secret (query ``?token=`` or
  ``X-Webhook-Token`` header, compared constant-time). Unset secret =
  server refuses to start unless TRENGO_WEBHOOK_ALLOW_INSECURE=1 (dev only).
- **Payload tolerance.** Trengo webhook bodies differ by event type and
  can be JSON or form-encoded; ``extract_ticket_id`` probes the common
  shapes (ticket_id, ticket.id, data.*, message.ticket_id).

The polling worker (``trengo_worker.py``) shares the same state file, so
you can run webhooks as primary and an occasional ``--once`` poll from
cron as a safety net without double replies.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from trengo_worker import TrengoWorker

WEBHOOK_PATH = os.environ.get("TRENGO_WEBHOOK_PATH", "/webhooks/trengo")
PORT = int(os.environ.get("TRENGO_WEBHOOK_PORT", "8080"))
MAX_BODY_BYTES = 1_000_000


def _log(text: str) -> None:
    print(f"[trengo-webhook] {text}", file=sys.stderr)


def extract_ticket_id(payload) -> str | None:
    """Pull a ticket id out of the common Trengo webhook payload shapes."""
    if not isinstance(payload, dict):
        return None
    for key in ("ticket_id", "ticketId"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    ticket = payload.get("ticket")
    if isinstance(ticket, dict) and ticket.get("id") not in (None, ""):
        return str(ticket["id"])
    for key in ("message", "data", "payload"):
        nested = payload.get(key)
        found = extract_ticket_id(nested) if isinstance(nested, dict) else None
        if found:
            return found
    return None


def parse_body(raw: bytes, content_type: str):
    """Webhook bodies arrive as JSON or form-encoded depending on the event."""
    text = raw.decode("utf-8", errors="replace")
    if "json" in (content_type or "").lower():
        try:
            return json.loads(text)
        except ValueError:
            return None
    try:
        return json.loads(text)  # some events send JSON without the header
    except ValueError:
        pass
    form = parse_qs(text)
    return {k: v[0] for k, v in form.items() if v} or None


class TicketQueue:
    """Single background consumer; collapses repeat events per ticket."""

    def __init__(self, worker: TrengoWorker):
        self.worker = worker
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._active = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._consume, daemon=True).start()

    def submit(self, ticket_id: str) -> bool:
        with self._lock:
            if ticket_id in self._pending:
                return False  # a queued (not yet started) run will see this message
            self._pending.add(ticket_id)
        self._queue.put(ticket_id)
        return True

    def _consume(self) -> None:
        while True:
            ticket_id = self._queue.get()
            # Leave _pending BEFORE handling: an event that arrives while we
            # process this ticket re-queues it, so its message isn't missed.
            with self._lock:
                self._pending.discard(ticket_id)
                self._active += 1
            try:
                outcome = self.worker.handle_ticket_id(ticket_id)
                _log(f"ticket {ticket_id}: {outcome}")
            except Exception as exc:  # one bad ticket must not kill the consumer
                _log(f"ticket {ticket_id}: crashed ({exc.__class__.__name__}: {exc})")
            finally:
                with self._lock:
                    self._active -= 1

    def drain(self, timeout: float = 5.0) -> None:
        """Block until everything queued so far is processed (for tests)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                busy = bool(self._pending) or self._active > 0
            if not busy and self._queue.empty():
                return
            time.sleep(0.02)


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, worker: TrengoWorker, secret: str):
        super().__init__(address, WebhookHandler)
        self.tickets = TicketQueue(worker)
        self.secret = secret


class WebhookHandler(BaseHTTPRequestHandler):
    server: WebhookServer

    def log_message(self, *args) -> None:  # route through our logger
        pass

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, url) -> bool:
        secret = self.server.secret
        if not secret:
            return True  # explicitly allowed insecure dev mode
        supplied = (parse_qs(url.query).get("token", [""])[0]
                    or self.headers.get("X-Webhook-Token", ""))
        return hmac.compare_digest(supplied, secret)

    def do_GET(self):
        if urlparse(self.path).path == "/healthz":
            self._respond(200, {"ok": True})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != WEBHOOK_PATH:
            self._respond(404, {"error": "not found"})
            return
        if not self._authorized(url):
            self._respond(401, {"error": "bad or missing token"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, {"error": "missing or oversized body"})
            return
        payload = parse_body(self.rfile.read(length), self.headers.get("Content-Type", ""))
        ticket_id = extract_ticket_id(payload)
        if not ticket_id:
            # 200 so Trengo doesn't retry-storm an event type we don't use.
            _log(f"ignored event without a ticket id (keys: {sorted(payload) if isinstance(payload, dict) else 'unparseable'})")
            self._respond(200, {"ignored": True})
            return
        queued = self.server.tickets.submit(str(ticket_id))
        self._respond(200, {"queued": queued, "ticket_id": str(ticket_id)})


def main() -> None:
    if not os.environ.get("TRENGO_API_KEY"):
        print("TRENGO_API_KEY is not set.", file=sys.stderr)
        raise SystemExit(1)
    secret = os.environ.get("TRENGO_WEBHOOK_SECRET", "").strip()
    if not secret and os.environ.get("TRENGO_WEBHOOK_ALLOW_INSECURE") != "1":
        print("TRENGO_WEBHOOK_SECRET is not set. Set it (recommended) or set "
              "TRENGO_WEBHOOK_ALLOW_INSECURE=1 for local development.", file=sys.stderr)
        raise SystemExit(1)
    server = WebhookServer(("0.0.0.0", PORT), TrengoWorker(), secret)
    _log(f"listening on :{PORT}{WEBHOOK_PATH} " + ("(token required)" if secret else "(INSECURE: no token)"))
    server.serve_forever()


if __name__ == "__main__":
    main()
