"""Mock dealership data store for the automotive call center agent.

This module simulates the backend systems a real call center would query:
a CRM (customers), a DMS (vehicles, service history, repair orders),
a scheduling system (appointments), and a recall database.

Replace the functions here with real API/database calls to integrate with
your dealership management system (CDK, Reynolds & Reynolds, Tekion, etc.).
All lookups are read/write against in-memory dicts so the agent runs out
of the box with no external dependencies.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Customers (CRM)
# ---------------------------------------------------------------------------

CUSTOMERS = {
    "+15551230001": {
        "customer_id": "C-1001",
        "name": "Sarah Mitchell",
        "phone": "+15551230001",
        "email": "sarah.mitchell@example.com",
        "vehicles": ["1HGCM82633A004352"],
    },
    "+15551230002": {
        "customer_id": "C-1002",
        "name": "James Okafor",
        "phone": "+15551230002",
        "email": "james.okafor@example.com",
        "vehicles": ["5YJ3E1EA7KF317000", "2T1BURHE5JC978123"],
    },
    "+15551230003": {
        "customer_id": "C-1003",
        "name": "Elena Vasquez",
        "phone": "+15551230003",
        "email": "elena.vasquez@example.com",
        "vehicles": ["1FTFW1ET5DFC10312"],
    },
}

# ---------------------------------------------------------------------------
# Vehicles (DMS)
# ---------------------------------------------------------------------------

VEHICLES = {
    "1HGCM82633A004352": {
        "vin": "1HGCM82633A004352",
        "year": 2021,
        "make": "Honda",
        "model": "Accord",
        "trim": "EX-L",
        "mileage": 41250,
        "warranty": {"type": "Powertrain", "expires": "2026-03-15", "active": True},
    },
    "5YJ3E1EA7KF317000": {
        "vin": "5YJ3E1EA7KF317000",
        "year": 2019,
        "make": "Tesla",
        "model": "Model 3",
        "trim": "Standard Range Plus",
        "mileage": 68900,
        "warranty": {"type": "Battery/Drive Unit", "expires": "2027-06-01", "active": True},
    },
    "2T1BURHE5JC978123": {
        "vin": "2T1BURHE5JC978123",
        "year": 2018,
        "make": "Toyota",
        "model": "Corolla",
        "trim": "LE",
        "mileage": 92300,
        "warranty": {"type": "Powertrain", "expires": "2023-08-20", "active": False},
    },
    "1FTFW1ET5DFC10312": {
        "vin": "1FTFW1ET5DFC10312",
        "year": 2022,
        "make": "Ford",
        "model": "F-150",
        "trim": "Lariat",
        "mileage": 28750,
        "warranty": {"type": "Bumper-to-Bumper", "expires": "2025-11-30", "active": True},
    },
}

SERVICE_HISTORY = {
    "1HGCM82633A004352": [
        {"date": "2025-04-02", "mileage": 36100, "service": "Oil change + tire rotation", "cost": 89.99},
        {"date": "2024-10-18", "mileage": 30500, "service": "30k mile service", "cost": 349.00},
    ],
    "5YJ3E1EA7KF317000": [
        {"date": "2025-06-11", "mileage": 65200, "service": "Cabin air filter + brake fluid check", "cost": 120.00},
    ],
    "2T1BURHE5JC978123": [
        {"date": "2025-01-25", "mileage": 88000, "service": "Brake pads (front) + rotors resurfaced", "cost": 412.50},
        {"date": "2024-07-09", "mileage": 81200, "service": "Oil change", "cost": 64.99},
    ],
    "1FTFW1ET5DFC10312": [
        {"date": "2025-07-30", "mileage": 27900, "service": "Oil change + multi-point inspection", "cost": 99.99},
    ],
}

# Open safety recalls keyed by VIN.
RECALLS = {
    "1FTFW1ET5DFC10312": [
        {
            "recall_id": "NHTSA-24V-512",
            "component": "Rearview camera",
            "summary": "Rearview camera image may not display; increases risk of a crash while reversing.",
            "remedy": "Dealer will update camera software free of charge.",
        }
    ],
    "2T1BURHE5JC978123": [
        {
            "recall_id": "NHTSA-23V-088",
            "component": "Airbag inflator",
            "summary": "Passenger airbag inflator may rupture on deployment.",
            "remedy": "Dealer will replace the inflator free of charge.",
        }
    ],
}

# ---------------------------------------------------------------------------
# Service menu / pricing
# ---------------------------------------------------------------------------

SERVICE_MENU = {
    "oil_change": {"name": "Oil change (synthetic)", "price": 89.99, "duration_min": 45},
    "tire_rotation": {"name": "Tire rotation", "price": 39.99, "duration_min": 30},
    "brake_inspection": {"name": "Brake inspection", "price": 0.00, "duration_min": 30},
    "brake_pads_front": {"name": "Front brake pads replacement", "price": 289.00, "duration_min": 90},
    "battery_replacement": {"name": "12V battery replacement", "price": 219.00, "duration_min": 30},
    "30k_service": {"name": "30,000-mile scheduled service", "price": 349.00, "duration_min": 120},
    "60k_service": {"name": "60,000-mile scheduled service", "price": 549.00, "duration_min": 180},
    "diagnostic": {"name": "Check-engine diagnostic", "price": 129.00, "duration_min": 60},
    "recall_service": {"name": "Open recall remedy", "price": 0.00, "duration_min": 90},
    "state_inspection": {"name": "State inspection", "price": 25.00, "duration_min": 30},
}

# ---------------------------------------------------------------------------
# Appointments (scheduling system)
# ---------------------------------------------------------------------------

_appointment_counter = itertools.count(5001)

APPOINTMENTS: dict[str, dict] = {
    "A-5000": {
        "appointment_id": "A-5000",
        "customer_id": "C-1002",
        "vin": "2T1BURHE5JC978123",
        "service_code": "oil_change",
        "date": (date.today() + timedelta(days=3)).isoformat(),
        "time": "10:00",
        "status": "confirmed",
    }
}

# Active repair orders (vehicles currently in the shop).
REPAIR_ORDERS = {
    "RO-7742": {
        "repair_order_id": "RO-7742",
        "customer_id": "C-1001",
        "vin": "1HGCM82633A004352",
        "description": "Transmission fluid leak diagnosis",
        "status": "parts_ordered",
        "status_detail": "Seal kit ordered, expected to arrive in 2 business days.",
        "estimated_completion": (date.today() + timedelta(days=4)).isoformat(),
    }
}

ESCALATIONS: list[dict] = []
_escalation_counter = itertools.count(9001)

# Sales/service leads created by the call center (mock CRM backend).
LEADS: dict[str, dict] = {}
_lead_counter = itertools.count(3001)

_SLOT_TIMES = ["08:00", "09:30", "11:00", "13:00", "14:30", "16:00"]


def available_slots(day: date) -> list[str]:
    """Open times for a given day. Weekends are closed; booked slots are removed."""
    if day.weekday() >= 6:  # closed Sundays
        return []
    taken = {
        a["time"]
        for a in APPOINTMENTS.values()
        if a["date"] == day.isoformat() and a["status"] == "confirmed"
    }
    return [t for t in _SLOT_TIMES if t not in taken]


def next_appointment_id() -> str:
    return f"A-{next(_appointment_counter)}"


def next_escalation_id() -> str:
    return f"E-{next(_escalation_counter)}"


def next_lead_id() -> str:
    return f"L-{next(_lead_counter)}"
