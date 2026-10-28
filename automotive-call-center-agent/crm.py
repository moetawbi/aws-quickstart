"""CRM client layer for the automotive call center agent.

Two interchangeable implementations of the same interface:

``RestCRM``
    A generic REST client for a real CRM API, selected automatically when
    ``CRM_API_BASE_URL`` is set. Endpoint contract (adapt ``RestCRM`` if your
    CRM differs - Salesforce, HubSpot, DealerSocket, VinSolutions, etc.):

    - ``GET  {base}/customers?phone={phone}``  -> customer records for a phone
    - ``GET  {base}/customers/{customer_id}``  -> one customer record
    - ``POST {base}/leads``                    -> create a sales/service lead
    - ``GET  {base}/leads/{lead_id}``          -> one lead record

    Auth is a bearer token from ``CRM_API_KEY``
    (``Authorization: Bearer <key>``).

``MockCRM``
    The default when ``CRM_API_BASE_URL`` is unset - backed by the in-memory
    data in ``data_store.py`` so the agent runs with no external services.

Both return plain dicts. Failures come back as ``{"error": ...}`` dicts
(never exceptions) so the tools layer can hand them to the model, which
recovers conversationally.
"""

from __future__ import annotations

import os

import requests

import agent_context
import data_store as db

DEFAULT_TIMEOUT = float(os.environ.get("CRM_TIMEOUT_SECONDS", "10"))

LEAD_TYPES = ("new_vehicle", "used_vehicle", "trade_in", "test_drive", "service_contract", "other")


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())[-10:]


class MockCRM:
    """In-memory CRM backed by data_store.py (local development default)."""

    def find_customer_by_phone(self, phone: str) -> dict:
        digits = _normalize_phone(phone)
        if len(digits) < 7:
            return {"error": "Phone number is too short to search on."}
        for key, customer in db.CUSTOMERS.items():
            if _normalize_phone(key) == digits:
                return dict(customer)
        return {"error": f"No customer found for phone {phone}."}

    def get_customer(self, customer_id: str) -> dict:
        for customer in db.CUSTOMERS.values():
            if customer["customer_id"] == customer_id:
                return dict(customer)
        return {"error": f"No customer found with ID {customer_id}."}

    def create_lead(self, lead: dict) -> dict:
        record = {"lead_id": db.next_lead_id(), "status": "new", **lead}
        db.LEADS[record["lead_id"]] = record
        return record

    def get_lead(self, lead_id: str) -> dict:
        lead = db.LEADS.get(lead_id)
        if lead is None:
            return {"error": f"No lead found with ID {lead_id}."}
        return dict(lead)

    # -- insurance -------------------------------------------------------------
    # There is no mock insurance book; these need the real CRM (the portfolio,
    # tariffs and rule engine all live there), so say so rather than inventing
    # cover or prices.

    _NO_MOCK = {"error": "Insurance information is only available against the live CRM."}

    def get_insurance_policies(self, customer_id: str) -> dict:
        return dict(self._NO_MOCK)

    def get_insurance_policy(self, policy_number: str) -> dict:
        return dict(self._NO_MOCK)

    def get_insurance_catalogue(self, insurance_type: str = "") -> dict:
        return dict(self._NO_MOCK)

    def quote_insurance(self, payload: dict) -> dict:
        return dict(self._NO_MOCK)


class RestCRM:
    """Generic REST CRM client. Point it at your CRM with CRM_API_BASE_URL
    and CRM_API_KEY, or subclass/edit it to match your CRM's API shape."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Accept"] = "application/json"

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        url = f"{self.base_url}{path}"
        # The actor grant (internal chats only) rides on every request; the
        # CRM uses it to apply that staff member's own permissions.
        headers = {**agent_context.headers(), **kwargs.pop("headers", {})}
        try:
            response = self.session.request(
                method, url, timeout=self.timeout, headers=headers or None, **kwargs
            )
        except requests.RequestException as exc:
            return {"error": f"CRM is unreachable ({exc.__class__.__name__})."}
        if response.status_code == 404:
            return {"error": "Not found in CRM."}
        if not response.ok:
            # The CRM explains refusals in its own words (a permission denial
            # names the user and the missing permission); pass that through
            # rather than flattening everything to a status code.
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("error") or body.get("message") or "")
            except ValueError:
                detail = ""
            if detail:
                return {"error": detail}
            return {"error": f"CRM returned HTTP {response.status_code}."}
        try:
            return response.json()
        except ValueError:
            return {"error": "CRM returned a non-JSON response."}

    @staticmethod
    def _first_record(payload) -> dict | None:
        """Tolerate the common list-wrapping shapes: bare list, or an object
        with the records under 'customers' / 'data' / 'results'."""
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            if "error" in payload:
                return payload
            records = None
            for key in ("customers", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    records = payload[key]
                    break
            if records is None:
                return payload  # already a single record
        else:
            return None
        return records[0] if records else None

    def find_customer_by_phone(self, phone: str) -> dict:
        digits = _normalize_phone(phone)
        if len(digits) < 7:
            return {"error": "Phone number is too short to search on."}
        payload = self._request("GET", "/customers", params={"phone": digits})
        record = self._first_record(payload)
        if not record:
            return {"error": f"No customer found for phone {phone}."}
        return record

    def get_customer(self, customer_id: str) -> dict:
        payload = self._request("GET", f"/customers/{customer_id}")
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    def create_lead(self, lead: dict) -> dict:
        payload = self._request("POST", "/leads", json=lead)
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    def get_lead(self, lead_id: str) -> dict:
        payload = self._request("GET", f"/leads/{lead_id}")
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    # -- insurance (read-only) -------------------------------------------------

    def get_insurance_policies(self, customer_id: str) -> dict:
        payload = self._request("GET", "/insurance/policies", params={"customer_id": customer_id})
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    def get_insurance_policy(self, policy_number: str) -> dict:
        payload = self._request("GET", f"/insurance/policies/{policy_number}")
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    def get_insurance_catalogue(self, insurance_type: str = "") -> dict:
        params = {"insurance_type": insurance_type} if insurance_type else None
        payload = self._request("GET", "/insurance/catalogue", params=params)
        return payload if isinstance(payload, dict) else {"error": "Unexpected CRM response."}

    def quote_insurance(self, payload: dict) -> dict:
        result = self._request("POST", "/insurance/quote", json=payload)
        return result if isinstance(result, dict) else {"error": "Unexpected CRM response."}


def get_crm() -> MockCRM | RestCRM:
    """Select the CRM backend from the environment (RestCRM when
    CRM_API_BASE_URL is set, MockCRM otherwise)."""
    base_url = os.environ.get("CRM_API_BASE_URL", "").strip()
    if base_url:
        return RestCRM(base_url, api_key=os.environ.get("CRM_API_KEY", ""))
    return MockCRM()
