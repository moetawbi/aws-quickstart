"""Verify a CRM's agent-facing endpoints against the integration contract.

Run this while developing the CRM-side endpoints (see CRM_INTEGRATION.md)
to prove the agent can talk to your CRM:

    export CRM_API_BASE_URL=http://anb-used-vehicles.test/api/agent
    export CRM_API_KEY=...
    python crm_verify.py --phone +96170123456              # read-only checks
    python crm_verify.py --phone +96170123456 --create-lead  # + posts ONE test lead

Checks, as a PASS/WARN/FAIL checklist (exit 0 = no FAILs):

1. Customer search by phone returns a record with the fields the agent
   uses (customer_id, name; vehicles/email/phone recommended)
2. Single-customer fetch round-trips by customer_id
3. Endpoints reject requests WITHOUT the bearer token (security)
4. --create-lead: posting the documented lead payload returns a lead_id,
   and the lead can be fetched back

The only write is --create-lead, and the test lead is clearly labeled.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

import crm

RESULTS: list[str] = []


def record(level: str, check: str, detail: str = "") -> None:
    RESULTS.append(level)
    mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "INFO": "·"}[level]
    print(f" {mark} [{level}] {check}" + (f" - {detail}" if detail else ""))


def check_lookup(client: crm.RestCRM, phone: str) -> dict | None:
    customer = client.find_customer_by_phone(phone)
    if "error" in customer:
        record("FAIL", "Customer search by phone", f"{customer['error']} "
               f"(GET /customers?phone=<digits> must return the record for {phone})")
        return None
    record("PASS", "Customer search by phone", "record returned")
    for field, level in (("customer_id", "FAIL"), ("name", "FAIL"),
                         ("phone", "WARN"), ("email", "WARN"), ("vehicles", "WARN")):
        if customer.get(field) not in (None, "", []):
            record("PASS", f"Customer field '{field}'", "present")
        else:
            hint = "the agent identifies customers and books by this" if level == "FAIL" \
                else "recommended - improves the agent's context"
            record(level, f"Customer field '{field}'", f"missing ({hint})")
    vehicles = customer.get("vehicles")
    if vehicles and not all(isinstance(v, str) for v in vehicles):
        record("WARN", "Vehicles shape", "expected a list of VIN strings")
    return customer


def check_get_customer(client: crm.RestCRM, customer_id: str) -> None:
    fetched = client.get_customer(customer_id)
    if "error" in fetched:
        record("FAIL", "Single-customer fetch", f"GET /customers/{customer_id}: {fetched['error']}")
    elif str(fetched.get("customer_id", "")) == str(customer_id):
        record("PASS", "Single-customer fetch", "round-trips by customer_id")
    else:
        record("WARN", "Single-customer fetch", "returned a record but customer_id does not match")


def check_auth_enforced(base_url: str, phone: str) -> None:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/customers",
                                params={"phone": phone}, timeout=10)
    except requests.RequestException as exc:
        record("WARN", "Auth enforcement", f"could not test ({exc.__class__.__name__})")
        return
    if response.status_code in (401, 403):
        record("PASS", "Auth enforcement", "request without token is rejected")
    else:
        record("FAIL", "Auth enforcement",
               f"request WITHOUT a token returned HTTP {response.status_code} - "
               "these endpoints expose customer data and must require the bearer token")


def check_create_lead(client: crm.RestCRM, customer: dict | None) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lead = {
        "name": f"[TEST {stamp}] Agent integration test",
        "phone": "+10000000000",
        "lead_type": "other",
        "interest": "Integration test lead - safe to delete",
        "email": "",
        "customer_id": str((customer or {}).get("customer_id", "")),
        "notes": "Created by crm_verify.py",
        "source": "call_center",
    }
    created = client.create_lead(lead)
    if "error" in created:
        record("FAIL", "Create lead", f"POST /leads: {created['error']}")
        return
    lead_id = created.get("lead_id") or created.get("id")
    if not lead_id:
        record("FAIL", "Create lead", f"response has no lead_id/id (got keys: {sorted(created)})")
        return
    record("PASS", "Create lead", f"lead {lead_id} created - delete it in the CRM when done")
    fetched = client.get_lead(str(lead_id))
    if "error" in fetched:
        record("WARN", "Fetch lead back", f"GET /leads/{lead_id}: {fetched['error']}")
    else:
        record("PASS", "Fetch lead back", "round-trips")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify CRM endpoints against the agent contract")
    parser.add_argument("--phone", required=True,
                        help="phone number of a REAL customer in the CRM to search for")
    parser.add_argument("--create-lead", action="store_true",
                        help="also post one clearly-labeled test lead and fetch it back")
    args = parser.parse_args()

    base_url = os.environ.get("CRM_API_BASE_URL", "").strip()
    if not base_url:
        print("CRM_API_BASE_URL is not set.", file=sys.stderr)
        raise SystemExit(2)
    print(f"Verifying CRM endpoints at {base_url}\n")

    client = crm.RestCRM(base_url, api_key=os.environ.get("CRM_API_KEY", ""))
    customer = check_lookup(client, args.phone)
    if customer and customer.get("customer_id"):
        check_get_customer(client, str(customer["customer_id"]))
    check_auth_enforced(base_url, args.phone)
    if args.create_lead:
        check_create_lead(client, customer)

    fails = RESULTS.count("FAIL")
    print(f"\n{len(RESULTS)} checks: {fails} failed, {RESULTS.count('WARN')} warnings.")
    if fails == 0:
        print("CRM endpoints match the agent contract. Point the agent at them:\n"
              "  CRM_API_BASE_URL + CRM_API_KEY in the agent's .env - done.")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
