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
    index = retrieval.get_index()
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


# The full tool surface handed to the tool runner.
TOOLS = [
    lookup_customer,
    get_customer_details,
    create_lead,
    get_lead,
    search_service_manuals,
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
