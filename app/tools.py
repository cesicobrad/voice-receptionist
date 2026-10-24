"""Tool definitions shared by the Claude brain, and the code that runs them."""
from __future__ import annotations

from datetime import date

from .booking import BookingError, BookingService, WEEKDAYS


def tool_definitions(services: list[str]) -> list[dict]:
    service = {"type": "string", "enum": services, "description": "Service key."}
    day = {"type": "string", "description": "Date as YYYY-MM-DD."}
    phone = {"type": "string", "description": "Caller's phone number, digits only or with +."}
    name = {"type": "string", "description": "Caller's full name."}

    def tool(name_: str, description: str, props: dict) -> dict:
        return {
            "name": name_,
            "description": description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        }

    return [
        tool("check_availability",
             "List free start times for a service on one date. Call this before offering or booking any time.",
             {"service": service, "date": day}),
        tool("book_appointment",
             "Book an appointment. Only call after the caller confirmed service, date, time, name and phone.",
             {"service": service, "date": day,
              "time": {"type": "string", "description": "Start time as HH:MM, 24-hour."},
              "name": name, "phone": phone,
              "notes": {"type": "string", "description": "Short note for the dentist, or empty string."}}),
        tool("cancel_appointment",
             "Cancel a booking by its 5-character reference. The phone number must match the booking.",
             {"reference": {"type": "string"}, "phone": phone}),
        tool("leave_message",
             "Log a callback request for the clinic staff, for anything you cannot handle yourself.",
             {"name": name, "phone": phone,
              "message": {"type": "string", "description": "What the caller needs, in one or two sentences."}}),
    ]


def run_tool(bookings: BookingService, name: str, args: dict) -> tuple[dict, dict | None]:
    """Run one tool. Returns (result for the model, UI event or None). Never raises on bad input."""
    try:
        if name == "check_availability":
            day = _parse_date(args.get("date"))
            slots = bookings.available_slots(args.get("service"), day)
            opens, closes = bookings.opening_hours(day)
            return {
                "date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()],
                "open": f"{opens:%H:%M}-{closes:%H:%M}",
                "free_start_times": slots[:16],
                "more_available": len(slots) > 16,
            }, None
        if name == "book_appointment":
            appt = bookings.book(args.get("service"), _parse_date(args.get("date")), args.get("time"),
                                 args.get("name"), args.get("phone"), args.get("notes", ""))
            return {"ok": True, **appt.to_dict()}, {"type": "booked", "appointment": appt.to_dict()}
        if name == "cancel_appointment":
            appt = bookings.cancel(args.get("reference"), args.get("phone"))
            return {"ok": True, **appt.to_dict()}, {"type": "cancelled", "appointment": appt.to_dict()}
        if name == "leave_message":
            entry = bookings.leave_message(args.get("name"), args.get("phone"), args.get("message"))
            return {"ok": True, **entry}, {"type": "message", "message": entry}
        return {"ok": False, "error": "unknown_tool"}, None
    except BookingError as err:
        return {"ok": False, "error": err.code, "detail": err.message}, None


def _parse_date(value) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BookingError("invalid_date", "Date should look like 2026-11-03.")
