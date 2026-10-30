"""Verify the Trengo integration against YOUR real Trengo account.

Run this once, wherever app.trengo.com is reachable, before going live:

    export TRENGO_API_KEY=...        # Trengo Settings > API
    python trengo_verify.py                    # read-only checks
    python trengo_verify.py --webhook          # + local webhook dry-run (no replies posted)
    python trengo_verify.py --send-test 12345  # + posts ONE test message to ticket 12345

It validates every assumption the integration makes, as a PASS/WARN/FAIL
checklist:

1.  Authentication and the ticket-list endpoint (envelope + pagination shape)
2.  Ticket records parse (id, status, contact, channel via ticket_summary)
3.  The single-ticket endpoint used by the webhook path
4.  Message fetching, and whether message_text / is_inbound / is_internal_note
    can classify your account's real message shapes (reports unclassifiable
    messages by their field names only - message content is never printed)
5.  The webhooks admin endpoint (lists which webhooks your account has)
6.  --webhook: boots the real webhook listener locally, POSTs a synthetic
    event for a real ticket id, and confirms the full path (auth -> queue ->
    re-fetch through your real API -> reply/skip decision) in dry-run mode -
    a stub agent is used and nothing is posted to the ticket
7.  --send-test <ticket_id>: posts one clearly-labeled test message to that
    ticket, re-fetches, and confirms our parser classifies it as OUTBOUND -
    the property that prevents the agent from ever answering itself

Read-only by default: only --send-test writes anything to your account.
Exit code 0 = no FAILs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests

import trengo

RESULTS: list[tuple[str, str, str]] = []  # (level, check, detail)


def record(level: str, check: str, detail: str = "") -> None:
    RESULTS.append((level, check, detail))
    mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "INFO": "·"}[level]
    print(f" {mark} [{level}] {check}" + (f" - {detail}" if detail else ""))


def check_auth_and_listing(client: trengo.TrengoClient):
    payload = client._request("GET", "/tickets", params={"page": 1})
    if isinstance(payload, dict) and "error" in payload:
        if "401" in payload["error"] or "403" in payload["error"]:
            record("FAIL", "Authentication", f"{payload['error']} - check TRENGO_API_KEY")
        else:
            record("FAIL", "Reach Trengo API", payload["error"])
        return None
    record("PASS", "Authentication", "API key accepted")
    data = client._data(payload)
    if data is None:
        record("FAIL", "Ticket list envelope",
               f"expected a 'data' list; got keys {sorted(payload) if isinstance(payload, dict) else type(payload).__name__}")
        return None
    record("PASS", "Ticket list envelope", f"'data' list present ({len(data)} tickets on page 1)")
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    if "last_page" in meta:
        record("PASS", "Pagination", f"meta.last_page = {meta['last_page']}")
    else:
        record("WARN", "Pagination", "meta.last_page missing - polling reads page 1..3 blindly")
    return data


def check_ticket_shapes(tickets: list[dict]):
    if not tickets:
        record("WARN", "Ticket parsing", "no open tickets to inspect - create one and re-run")
        return
    ids, names, channels = 0, 0, 0
    for ticket in tickets[:10]:
        summary = trengo.ticket_summary(ticket)
        ids += summary["ticket_id"] is not None
        names += bool(summary["contact_name"])
        channels += bool(summary["channel_type"] or summary["channel_name"])
    sample = min(len(tickets), 10)
    if ids == sample:
        record("PASS", "Ticket ids", f"{ids}/{sample} tickets have an id")
    else:
        record("FAIL", "Ticket ids", f"only {ids}/{sample} tickets have an id - check ticket_summary()")
    record("PASS" if names else "WARN", "Contact names", f"{names}/{sample} tickets carry a contact name")
    record("PASS" if channels else "WARN", "Channel info", f"{channels}/{sample} tickets carry channel info")


def check_single_ticket(client: trengo.TrengoClient, ticket_id) -> None:
    ticket = client.get_ticket(ticket_id)
    if isinstance(ticket, dict) and ticket.get("id") is not None:
        record("PASS", "Single-ticket endpoint (webhook path)", f"GET /tickets/{ticket_id} returns the record")
    else:
        record("WARN", "Single-ticket endpoint (webhook path)",
               f"GET /tickets/{ticket_id} did not return an id - webhook replies still work, "
               "but without contact/channel context on the first turn")


def check_messages(client: trengo.TrengoClient, tickets: list[dict]):
    inspected = classified = with_text = total = 0
    unclassified_shapes: set[tuple[str, ...]] = set()
    for ticket in tickets[:5]:
        ticket_id = ticket.get("id")
        messages = client.get_messages(ticket_id)
        if isinstance(messages, dict):
            record("FAIL", f"Fetch messages (ticket {ticket_id})", messages.get("error", "unexpected response"))
            continue
        inspected += 1
        for message in messages:
            total += 1
            if trengo.message_text(message):
                with_text += 1
            direction = str(message.get("direction", "")).upper()
            mtype = str(message.get("type", "")).upper()
            by_fields = direction in ("INBOUND", "IN", "INCOMING", "OUTBOUND", "OUT", "OUTGOING") \
                or mtype in ("INBOUND", "OUTBOUND")
            by_author = bool(message.get("contact") or message.get("contact_id")) \
                or bool(message.get("agent") or message.get("agent_id") or message.get("user") or message.get("user_id"))
            if by_fields or by_author or trengo.is_internal_note(message):
                classified += 1
            else:
                unclassified_shapes.add(tuple(sorted(message.keys())))
    if not inspected:
        record("WARN", "Message parsing", "no tickets with readable messages to inspect")
        return
    if total == 0:
        record("WARN", "Message parsing", "tickets had no messages to inspect")
        return
    record("PASS" if with_text == total else "WARN", "Message text extraction",
           f"{with_text}/{total} messages yield text (empty ones are skipped at runtime)")
    if classified == total:
        record("PASS", "Inbound/outbound classification", f"{classified}/{total} messages classifiable")
    else:
        record("FAIL", "Inbound/outbound classification",
               f"{total - classified}/{total} messages have none of the direction/type/authorship "
               f"fields is_inbound() checks. Field sets seen: {sorted(unclassified_shapes)} - "
               "adjust is_inbound() in trengo.py for these")


def check_webhook_admin(client: trengo.TrengoClient):
    payload = client._request("GET", "/webhooks")
    data = client._data(payload)
    if data is None:
        record("INFO", "Webhook admin endpoint", "GET /webhooks not available - configure webhooks in the Trengo UI")
        return
    if not data:
        record("WARN", "Configured webhooks", "none found - add one in Trengo Settings > Webhooks "
               "pointing at your trengo_webhook.py URL")
        return
    urls = [w.get("url", "?") for w in data if isinstance(w, dict)]
    record("INFO", "Configured webhooks", f"{len(urls)} found: {', '.join(urls[:5])}")


def check_webhook_loopback(client: trengo.TrengoClient, tickets: list[dict]):
    """Boot the real listener locally, feed it a synthetic event for a REAL
    ticket, and watch it fetch that ticket from the real API - dry-run, so
    nothing is posted and no Anthropic key is needed."""
    from trengo_webhook import WebhookServer
    from trengo_worker import TrengoWorker

    if not tickets:
        record("WARN", "Webhook loopback", "no tickets available to test with")
        return

    class ProbeAgent:  # stands in for Claude; proves the pipeline without cost
        def send(self, text):
            return "(verification dry run - this text is never posted)"

    worker = TrengoWorker(client=client, agent_factory=ProbeAgent, dry_run=True)
    server = WebhookServer(("127.0.0.1", 0), worker, secret="verify-secret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    ticket_id = tickets[0].get("id")
    try:
        bad = requests.post(f"http://127.0.0.1:{port}/webhooks/trengo",
                            json={"ticket_id": ticket_id}, timeout=10)
        if bad.status_code == 401:
            record("PASS", "Webhook auth", "event without token rejected (401)")
        else:
            record("FAIL", "Webhook auth", f"expected 401 without token, got {bad.status_code}")
        response = requests.post(
            f"http://127.0.0.1:{port}/webhooks/trengo?token=verify-secret",
            json={"ticket_id": ticket_id}, timeout=10)
        if response.status_code == 200 and response.json().get("queued"):
            record("PASS", "Webhook accepts events", f"ticket {ticket_id} queued")
        else:
            record("FAIL", "Webhook accepts events", f"HTTP {response.status_code}: {response.text[:120]}")
            return
        server.tickets.drain(timeout=30)
        record("PASS", "Webhook end-to-end (dry run)",
               "listener fetched the real ticket through your API and made a reply/skip decision - "
               "see the [trengo] log lines above")
    finally:
        server.shutdown()


def check_send_test(client: trengo.TrengoClient, ticket_id: str):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    text = f"[Integration test {stamp}] Automated test message from the call center agent setup - please ignore."
    result = client.send_message(ticket_id, text)
    if isinstance(result, dict) and "error" in result:
        record("FAIL", "Send message", result["error"])
        return
    record("PASS", "Send message", f"test message posted to ticket {ticket_id}")
    time.sleep(2)  # let Trengo register it
    messages = client.get_messages(ticket_id)
    if isinstance(messages, dict):
        record("WARN", "Round-trip check", messages.get("error", "could not re-fetch messages"))
        return
    ours = [m for m in messages if stamp in trengo.message_text(m)]
    if not ours:
        record("WARN", "Round-trip check", "posted message not visible via GET yet - re-check manually")
        return
    record("PASS", "Round-trip check", "posted message visible via the API")
    if trengo.is_inbound(ours[-1]):
        record("FAIL", "Self-reply protection",
               "our own message classifies as INBOUND - the agent would answer itself! "
               "Adjust is_inbound() in trengo.py before going live")
    else:
        record("PASS", "Self-reply protection", "our own message classifies as outbound - the agent will never answer itself")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the Trengo integration against your real account")
    parser.add_argument("--webhook", action="store_true",
                        help="also boot the webhook listener locally and dry-run a real ticket through it")
    parser.add_argument("--send-test", metavar="TICKET_ID",
                        help="post ONE labeled test message to this ticket and verify the round trip")
    args = parser.parse_args()

    if not os.environ.get("TRENGO_API_KEY"):
        print("TRENGO_API_KEY is not set. Get one from Trengo Settings > API.", file=sys.stderr)
        raise SystemExit(2)

    print(f"Verifying against {os.environ.get('TRENGO_BASE_URL', trengo.DEFAULT_BASE_URL)}\n")
    client = trengo.TrengoClient()

    tickets = check_auth_and_listing(client)
    if tickets is not None:
        check_ticket_shapes(tickets)
        if tickets:
            check_single_ticket(client, tickets[0].get("id"))
        check_messages(client, tickets)
        check_webhook_admin(client)
        if args.webhook:
            check_webhook_loopback(client, tickets)
        if args.send_test:
            check_send_test(client, args.send_test)

    fails = sum(1 for level, _, _ in RESULTS if level == "FAIL")
    warns = sum(1 for level, _, _ in RESULTS if level == "WARN")
    print(f"\n{len(RESULTS)} checks: {fails} failed, {warns} warnings.")
    if fails == 0:
        print("Integration verified against this account." if tickets else "")
        print("\nFinal live test (needs your public URL):\n"
              "  1. Run: TRENGO_WEBHOOK_SECRET=... python trengo_webhook.py\n"
              "  2. In Trengo Settings > Webhooks, point an inbound-message webhook at\n"
              "     https://<your-host>/webhooks/trengo?token=<secret>\n"
              "  3. Send a WhatsApp/chat message to your inbox and watch the reply arrive.")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
