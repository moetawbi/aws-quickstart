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


def _json(payload) -> str:
    return json.dumps(payload, indent=2)


@beta_tool
def lookup_customer(phone: str) -> str:
    """Look up a customer record by phone number.

    Args:
        phone: The caller's phone number, e.g. "+15551230001" or "555-123-0001".
    """
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 7:
        return _json({"error": "Phone number is too short to search on."})
    for key, customer in db.CUSTOMERS.items():
        if "".join(c for c in key if c.isdigit()).endswith(digits[-10:]):
            return _json(customer)
    return _json({"error": f"No customer found for phone {phone}."})


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


# The full tool surface handed to the tool runner.
TOOLS = [
    lookup_customer,
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
