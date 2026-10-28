"""Tools the automotive call center agent can call.

Each tool is a plain typed function decorated with ``@beta_tool``; the
Anthropic SDK generates the JSON schema from the signature and docstring
and drives the execute-and-loop cycle via the tool runner.

Every tool returns a JSON string so results are unambiguous for the model.
Failures are returned as ``{"error": ...}`` payloads rather than raised,
so the model can recover conversationally (ask the caller to re-spell a
VIN, hand over to a person, etc.).

Every vehicle, recall, service-request and handover call goes through the
CRM client (crm.py): the live CRM in production, the demo data in
data_store.py only when no CRM is configured.
"""

from __future__ import annotations

import json

from anthropic import beta_tool

import agent_context
import catalogue
import retrieval
from crm import LEAD_TYPES, get_crm

# CRM backend: RestCRM when CRM_API_BASE_URL is set, MockCRM otherwise.
CRM = get_crm()


def _json(payload) -> str:
    return json.dumps(payload, indent=2)


@beta_tool
def lookup_customer(phone: str) -> str:
    """Look up a customer record in the CRM by phone number.

    Args:
        phone: The caller's phone number, e.g. "+15551230001" or "555-123-0001".
    """
    return _json(CRM.find_customer_by_phone(phone))


@beta_tool
def get_customer_details(customer_id: str) -> str:
    """Fetch a customer's full CRM record (contact info, owned vehicles) by ID.

    Args:
        customer_id: The customer's CRM ID, e.g. "C-1001".
    """
    return _json(CRM.get_customer(customer_id))


@beta_tool
def get_vehicle(vin: str, customer_id: str = "") -> str:
    """Get a vehicle's details from the CRM: make/model/year, plate, warranty
    and maintenance-plan end dates, Ford Protect cover, its last workshop visit
    and the mileage its next service is due at.

    With a customer, pass their customer_id from lookup_customer - the CRM
    only shares vehicles on the identified customer's own account. A field
    that is null is simply not on record: say so, never guess.

    Args:
        vin: The vehicle's chassis number (VIN), usually 17 characters.
        customer_id: The identified customer's ID, from lookup_customer.
    """
    return _json(CRM.get_vehicle(vin.strip(), str(customer_id or "").strip()))


@beta_tool
def get_service_history(vin: str, customer_id: str = "") -> str:
    """List a vehicle's completed workshop visits at ANB, newest first: job
    card, type of work (mechanical, body shop, service) and dates. It does not
    list the individual jobs done, and never contains prices.

    With a customer, pass their customer_id - only vehicles on their own
    account are shared.

    Args:
        vin: The vehicle's chassis number (VIN).
        customer_id: The identified customer's ID, from lookup_customer.
    """
    return _json(CRM.get_service_history(vin.strip(), str(customer_id or "").strip()))


@beta_tool
def check_recalls(vin: str) -> str:
    """Check which recall campaigns a vehicle is part of, from ANB's records.

    "on_record" means the dealer system flagged the vehicle for the campaign;
    whether the repair has already been done is NOT recorded, so never tell
    the customer it is outstanding or fixed - the service team confirms.
    Recall repairs are free. If do_not_drive is true, tell the customer not
    to drive the vehicle and hand over as urgent.

    Args:
        vin: The vehicle's chassis number (VIN).
    """
    return _json(CRM.check_recalls(vin.strip()))


@beta_tool
def request_service_appointment(
    service_needed: str,
    customer_id: str = "",
    vin: str = "",
    preferred_date: str = "",
    preferred_time: str = "",
    mileage_km: int = 0,
    phone: str = "",
    name: str = "",
    notes: str = "",
) -> str:
    """Ask ANB's service team to book a workshop visit. Nothing is booked:
    the team calls the customer within one working day to confirm the slot.

    Read the details back and get the customer's agreement first. Tell them
    it is a request, give them the reference, and never promise a time.

    Args:
        service_needed: What they want done, e.g. "10,000 km service" or "brake noise".
        customer_id: The identified customer's ID, from lookup_customer.
        vin: The vehicle's VIN, if known.
        preferred_date: Preferred day as YYYY-MM-DD, if they gave one.
        preferred_time: Preferred time of day, e.g. "morning" or "10:00".
        mileage_km: The current mileage, if they know it. 0 means unknown.
        phone: A callback number - required when the customer is not identified.
        name: The customer's name, when they are not identified.
        notes: Anything else the service team should know.
    """
    if not service_needed.strip():
        return _json({"error": "Say what service the customer needs."})
    request = {
        "service_needed": service_needed.strip(),
        "customer_id": str(customer_id or "").strip(),
        "vin": vin.strip(),
        "preferred_date": preferred_date.strip(),
        "preferred_time": preferred_time.strip(),
        "mileage_km": int(mileage_km) if mileage_km else None,
        "phone": phone.strip(),
        "name": name.strip(),
        "notes": notes.strip(),
    }
    return _json(CRM.create_service_request(request))


@beta_tool
def escalate_to_human(
    customer_id: str,
    reason: str,
    priority: str = "normal",
    phone: str = "",
    name: str = "",
    vin: str = "",
) -> str:
    """Hand the conversation to a person: opens a callback case for ANB's
    customer service team, who call the customer back.

    Use for: warranty or billing disputes, complaints, anything involving an
    accident or injury, a vehicle that may be unsafe, requests to speak to a
    manager, changing or cancelling an appointment, or anything the other
    tools cannot handle.

    Args:
        customer_id: The customer's ID, or "unknown" if they could not be identified.
        reason: A one-to-three sentence summary of the issue for the team.
        priority: "normal", "high", or "urgent" (urgent = safety-related).
        phone: A callback number - required when the customer is not identified.
        name: The customer's name, when they are not identified.
        vin: The vehicle's VIN, if the issue is about a vehicle.
    """
    if priority not in ("normal", "high", "urgent"):
        priority = "normal"
    handover = {
        "customer_id": str(customer_id or "").strip(),
        "reason": reason.strip(),
        "priority": priority,
        "phone": phone.strip(),
        "name": name.strip(),
        "vin": vin.strip(),
    }
    return _json(CRM.create_handover(handover))


@beta_tool
def create_lead(
    name: str,
    phone: str,
    lead_type: str,
    interest: str,
    email: str = "",
    customer_id: str = "",
    notes: str = "",
) -> str:
    """Create a sales/service lead in the CRM so the right team follows up.

    Use when a caller expresses buying interest (new/used vehicle, test
    drive, trade-in, service contract) - whether or not they are an
    existing customer. Always confirm the details with the caller and get
    their consent to be contacted before creating the lead.

    Args:
        name: The caller's full name.
        phone: The caller's callback phone number.
        lead_type: One of "new_vehicle", "used_vehicle", "trade_in",
            "test_drive", "service_contract", "accessories", "parts", or "other".
        interest: What they are interested in, e.g. "2026 F-150 Lariat, financing".
        email: The caller's email address, if they offered one.
        customer_id: The existing CRM customer ID, if the caller was identified.
        notes: Anything else useful for the sales team (timeline, budget, trade-in vehicle).
    """
    if lead_type not in LEAD_TYPES:
        return _json({"error": f"Invalid lead_type '{lead_type}'. Use one of: {', '.join(LEAD_TYPES)}."})
    if not name.strip() or not phone.strip() or not interest.strip():
        return _json({"error": "name, phone, and interest are all required to create a lead."})
    lead = {
        "name": name.strip(),
        "phone": phone.strip(),
        "lead_type": lead_type,
        "interest": interest.strip(),
        "email": email.strip(),
        "customer_id": customer_id.strip(),
        "notes": notes.strip(),
        "source": "call_center",
    }
    result = CRM.create_lead(lead)
    if "error" in result:
        return _json(result)
    return _json({"created": True, "lead": result})


@beta_tool
def get_lead(lead_id: str) -> str:
    """Fetch a lead from the CRM to check its status.

    Args:
        lead_id: The lead's CRM ID, e.g. "L-3001".
    """
    return _json(CRM.get_lead(lead_id))


@beta_tool
def search_service_manuals(query: str, max_results: int = 4) -> str:
    """Search the dealership's service and owner manuals for technical
    information: fluid capacities and specs, torque values, maintenance
    schedules, warning lamp meanings, towing limits, feature operation.

    Returns the most relevant manual passages with their source file and
    section. Quote specs exactly as written; if nothing relevant comes
    back, say the manuals don't cover it rather than guessing.

    Args:
        query: What to look for, with specifics - vehicle, system, and
            measurement, e.g. "F-150 lug nut torque" or "EcoBoost coolant capacity".
        max_results: How many passages to return (1-8, default 4).
    """
    # The answering agent's manuals: shared ones plus its own folder.
    index = retrieval.get_index(agent_context.get_agent_slug())
    if not index.chunks:
        return _json({"error": "No service manuals are loaded.", "results": []})
    max_results = max(1, min(max_results, 8))
    results = [
        {
            "source": chunk.source,
            "section": chunk.section,
            "relevance": round(score, 2),
            "text": chunk.text,
        }
        for score, chunk in index.search(query, k=max_results)
    ]
    if not results:
        return _json({"results": [], "note": "No manual passages matched this query."})
    return _json({"results": results})


# -- insurance ------------------------------------------------------------------
# Read-only. What comes back is decided by the CRM from the conversation's actor
# grant: a staff member sees what their own CRM permissions allow (including
# commission figures only with "view policy commissions"), while a customer
# conversation gets their own cover in customer-safe terms.


@beta_tool
def get_customer_insurance(customer_id: str) -> str:
    """Look up the insurance policies the dealership holds for a customer.

    Use after identifying the caller with lookup_customer. Answer whether a
    vehicle is covered from the returned "cover_state" field, NOT from
    "status": the CRM's status column is frequently stale, and cover_state
    reconciles it against the policy dates. When cover_state is "expired",
    the customer is not covered no matter what status says.

    Args:
        customer_id: The CRM customer ID, as returned by lookup_customer.
    """
    result = CRM.get_insurance_policies(str(customer_id).strip())
    if "error" in result:
        return _json(result)
    policies = result.get("data") or []
    if not policies:
        return _json({"policies": [], "note": f"No insurance policies on file for customer {customer_id}."})
    return _json({"policies": policies})


@beta_tool
def get_insurance_policy(policy_number: str) -> str:
    """Look up one insurance policy by its policy number.

    Only for an actual policy number, which contains letters and usually a
    slash - "MIF/895342", "MLF/1430198". A bare run of digits is NOT a policy
    number: 7-8 digits is a Lebanese phone number (use lookup_customer) and a
    short number on its own is usually a customer ID (use
    get_customer_details). Do not guess with this tool.

    Read cover from "cover_state", not "status" (see get_customer_insurance).

    Args:
        policy_number: The policy number as printed on the policy, e.g. "MIF/895342".
    """
    return _json(CRM.get_insurance_policy(str(policy_number).strip()))


@beta_tool
def list_insurance_products(insurance_type: str = "") -> str:
    """List the motor insurance products and plans the dealership can sell,
    with what each one covers. Use for "what insurance do you offer?" and
    "what does comprehensive include?". Contains no prices - premiums depend
    on the vehicle, so use quote_insurance for those.

    Args:
        insurance_type: Optional filter, e.g. "comprehensive", "obligatory",
            "third_party". Leave empty for everything.
    """
    return _json(CRM.get_insurance_catalogue(str(insurance_type or "").strip()))


@beta_tool
def quote_insurance(
    make: str,
    model: str,
    year: int,
    value: float,
    vehicle_type: str = "private",
    deductible: float | None = None,
    policy_period: int = 12,
    driver_age: int | None = None,
) -> str:
    """Price motor insurance for a vehicle across every eligible plan,
    cheapest first.

    The figures are INDICATIVE: nothing is saved and there is no quote
    reference. Always say the price is indicative and subject to confirmation,
    and never promise a premium as final. If the caller wants a firm quote,
    offer to have the insurance team follow up.

    Args:
        make: Vehicle make, e.g. "Ford".
        model: Vehicle model, e.g. "F-150".
        year: Model year, e.g. 2022.
        value: The vehicle's insured value in USD. Ask the caller if unknown -
            do not guess, it drives the whole premium.
        vehicle_type: "private", "commercial", "motorcycle" etc. Defaults to private.
        deductible: Optional deductible the caller wants, in USD.
        policy_period: Months of cover, normally 12.
        driver_age: Optional age of the main driver.
    """
    payload = {
        "make": make.strip(),
        "model": model.strip(),
        "year": int(year),
        "value": float(value),
        "vehicle_type": (vehicle_type or "private").strip(),
        "policy_period": int(policy_period or 12),
    }
    if deductible is not None:
        payload["deductible"] = float(deductible)
    if driver_age is not None:
        payload["driver_age"] = int(driver_age)
    return _json(CRM.quote_insurance(payload))


# -- catalogue -------------------------------------------------------------------


@beta_tool
def search_catalogue(query: str = "", brand: str = "", model: str = "", min_year: int = 0) -> str:
    """Search the products and vehicles this agent has been given a catalogue
    of (configured on the agent in the CRM - for example a stock XML feed).

    Use it for "do you have a ...?", "what Ford pickups do you have?",
    "anything from 2020 or newer?". Recommend from what it returns and share
    the item's link when there is one.

    It never contains prices, by design. Do not state, estimate or hint at a
    price for anything you find - say the sales team will confirm the price and
    availability, and offer to have them follow up. Stock can sell at any time,
    so never promise a unit is still available.

    Args:
        query: Free-text description of what the customer wants, e.g. "pickup 4x4".
        brand: Optional brand/make filter, e.g. "Ford".
        model: Optional model filter, e.g. "Ranger".
        min_year: Optional earliest model year, e.g. 2020. 0 means no limit.
    """
    config = agent_context.get_agent_config() or {}
    catalogues = [c for c in (config.get("catalogues") or []) if c.get("feed_url")]
    if not catalogues:
        return _json({"results": [], "note": "No catalogue is configured for this agent. "
                                             "Offer to have the sales team help instead."})
    result = catalogue.search(catalogues, query=query, brand=brand, model=model,
                              min_year=int(min_year) or None)
    if not result["results"] and result["unavailable_catalogues"]:
        result["note"] = ("The catalogue could not be loaded right now. Do not guess what is in stock "
                          "- offer to have the sales team confirm.")
    return _json(result)


# The full tool surface handed to the tool runner.
TOOLS = [
    lookup_customer,
    get_customer_details,
    create_lead,
    get_lead,
    search_service_manuals,
    search_catalogue,
    get_customer_insurance,
    get_insurance_policy,
    list_insurance_products,
    quote_insurance,
    get_vehicle,
    get_service_history,
    check_recalls,
    request_service_appointment,
    escalate_to_human,
]

# The read-only subset, for internal (staff) conversations in the CRM chat.
# Service requests, lead creation and handovers are withheld so that a staff
# member exploring the agent can never write to the CRM by accident. Keep in
# sync with TOOLS when adding a read tool.
READ_ONLY_TOOLS = [
    lookup_customer,
    get_customer_details,
    get_lead,
    search_service_manuals,
    search_catalogue,
    get_customer_insurance,
    get_insurance_policy,
    list_insurance_products,
    quote_insurance,
    get_vehicle,
    get_service_history,
    check_recalls,
]
