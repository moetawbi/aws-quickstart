"""Tools the automotive call center agent can call.

Each tool is a plain typed function decorated with ``@beta_tool``; the
Anthropic SDK generates the JSON schema from the signature and docstring
and drives the execute-and-loop cycle via the tool runner.

Every tool returns a JSON string so results are unambiguous for the model.
Failures are returned as ``{"error": ...}`` payloads rather than raised,
so the model can recover conversationally (ask the caller to re-spell a
VIN, offer another time slot, etc.).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from anthropic import beta_tool

import agent_context
import catalogue
import data_store as db
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
def get_vehicle(vin: str) -> str:
    """Get vehicle details (year/make/model, mileage, warranty status) by VIN.

    Args:
        vin: The 17-character Vehicle Identification Number.
    """
    vehicle = db.VEHICLES.get(vin.strip().upper())
    if vehicle is None:
        return _json({"error": f"No vehicle found for VIN {vin}."})
    return _json(vehicle)


@beta_tool
def get_service_history(vin: str) -> str:
    """Get the service history for a vehicle at this dealership.

    Args:
        vin: The 17-character Vehicle Identification Number.
    """
    vin = vin.strip().upper()
    if vin not in db.VEHICLES:
        return _json({"error": f"No vehicle found for VIN {vin}."})
    return _json({"vin": vin, "history": db.SERVICE_HISTORY.get(vin, [])})


@beta_tool
def check_recalls(vin: str) -> str:
    """Check whether a vehicle has open safety recalls.

    Args:
        vin: The 17-character Vehicle Identification Number.
    """
    vin = vin.strip().upper()
    if vin not in db.VEHICLES:
        return _json({"error": f"No vehicle found for VIN {vin}."})
    recalls = db.RECALLS.get(vin, [])
    return _json({"vin": vin, "open_recalls": recalls, "count": len(recalls)})


@beta_tool
def get_service_menu() -> str:
    """List the services this dealership offers, with prices and durations.

    Returns each service's code (used when booking), display name,
    price in USD, and expected duration in minutes.
    """
    return _json(db.SERVICE_MENU)


@beta_tool
def get_available_appointments(start_date: str, days: int = 5) -> str:
    """Find open service appointment slots.

    Args:
        start_date: First day to check, in YYYY-MM-DD format.
        days: How many days ahead to check (1-14, default 5).
    """
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return _json({"error": f"Invalid date '{start_date}'. Use YYYY-MM-DD."})
    if start < date.today():
        return _json({"error": "start_date is in the past."})
    days = max(1, min(days, 14))
    availability = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        slots = db.available_slots(day)
        if slots:
            availability[day.isoformat()] = slots
    return _json({"availability": availability, "note": "Closed Sundays."})


@beta_tool
def book_appointment(customer_id: str, vin: str, service_code: str, date_str: str, time_str: str) -> str:
    """Book a service appointment for a verified customer.

    Args:
        customer_id: The customer's ID from lookup_customer, e.g. "C-1001".
        vin: The 17-character VIN of the vehicle to be serviced.
        service_code: A service code from get_service_menu, e.g. "oil_change".
        date_str: Appointment date in YYYY-MM-DD format.
        time_str: Appointment time in 24-hour HH:MM format, e.g. "09:30".
    """
    vin = vin.strip().upper()
    if not any(c["customer_id"] == customer_id for c in db.CUSTOMERS.values()):
        return _json({"error": f"Unknown customer_id {customer_id}."})
    if vin not in db.VEHICLES:
        return _json({"error": f"No vehicle found for VIN {vin}."})
    if service_code not in db.SERVICE_MENU:
        return _json({"error": f"Unknown service_code '{service_code}'. Call get_service_menu for valid codes."})
    try:
        day = date.fromisoformat(date_str)
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return _json({"error": "Invalid date or time format. Use YYYY-MM-DD and HH:MM."})
    if time_str not in db.available_slots(day):
        return _json({
            "error": f"{date_str} {time_str} is not available.",
            "open_slots_that_day": db.available_slots(day),
        })

    appointment = {
        "appointment_id": db.next_appointment_id(),
        "customer_id": customer_id,
        "vin": vin,
        "service_code": service_code,
        "service_name": db.SERVICE_MENU[service_code]["name"],
        "date": date_str,
        "time": time_str,
        "status": "confirmed",
    }
    db.APPOINTMENTS[appointment["appointment_id"]] = appointment
    return _json({"booked": True, "appointment": appointment})


@beta_tool
def cancel_appointment(appointment_id: str) -> str:
    """Cancel an existing service appointment.

    Args:
        appointment_id: The appointment ID, e.g. "A-5000".
    """
    appointment = db.APPOINTMENTS.get(appointment_id)
    if appointment is None:
        return _json({"error": f"No appointment found with ID {appointment_id}."})
    if appointment["status"] == "cancelled":
        return _json({"error": f"Appointment {appointment_id} is already cancelled."})
    appointment["status"] = "cancelled"
    return _json({"cancelled": True, "appointment": appointment})


@beta_tool
def get_customer_appointments(customer_id: str) -> str:
    """List a customer's upcoming and past appointments.

    Args:
        customer_id: The customer's ID from lookup_customer, e.g. "C-1001".
    """
    matches = [a for a in db.APPOINTMENTS.values() if a["customer_id"] == customer_id]
    return _json({"customer_id": customer_id, "appointments": matches})


@beta_tool
def get_repair_status(customer_id: str) -> str:
    """Check the status of a customer's vehicle currently in the shop.

    Args:
        customer_id: The customer's ID from lookup_customer, e.g. "C-1001".
    """
    orders = [ro for ro in db.REPAIR_ORDERS.values() if ro["customer_id"] == customer_id]
    if not orders:
        return _json({"customer_id": customer_id, "repair_orders": [], "note": "No vehicles currently in the shop."})
    return _json({"customer_id": customer_id, "repair_orders": orders})


@beta_tool
def escalate_to_human(customer_id: str, reason: str, priority: str = "normal") -> str:
    """Create a ticket for a human service advisor to call the customer back.

    Use for: warranty or billing disputes, complaints, anything involving an
    accident or injury, requests to speak to a manager, or any request the
    other tools cannot handle.

    Args:
        customer_id: The customer's ID, or "unknown" if the caller could not be identified.
        reason: A one-to-three sentence summary of the issue for the advisor.
        priority: "normal", "high", or "urgent" (urgent = safety-related).
    """
    if priority not in ("normal", "high", "urgent"):
        priority = "normal"
    ticket = {
        "ticket_id": db.next_escalation_id(),
        "customer_id": customer_id,
        "reason": reason,
        "priority": priority,
        "callback_eta": "within 1 business hour" if priority == "urgent" else "within 1 business day",
    }
    db.ESCALATIONS.append(ticket)
    return _json({"escalated": True, "ticket": ticket})


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
            "test_drive", "service_contract", or "other".
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
    get_service_menu,
    get_available_appointments,
    book_appointment,
    cancel_appointment,
    get_customer_appointments,
    get_repair_status,
    escalate_to_human,
]

# The read-only subset, for internal (staff) conversations in the CRM chat.
# Booking, cancelling, lead creation and escalation are withheld so that a
# staff member exploring the agent can never write to the CRM or the
# scheduler by accident. Keep in sync with TOOLS when adding a read tool.
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
    get_service_menu,
    get_available_appointments,
    get_customer_appointments,
    get_repair_status,
]
