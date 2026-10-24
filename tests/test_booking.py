from datetime import date

import pytest

from app.booking import BookingError, BookingService
from app.tools import run_tool
from tests.conftest import FRI, MON, NOW, SAT, TUE, WED


def code(fn, *args):
    with pytest.raises(BookingError) as err:
        fn(*args)
    return err.value.code


def test_slots_fit_inside_opening_hours(svc):
    slots = svc.available_slots("whitening", FRI)  # 90 min, Friday closes 14:00
    assert slots[0] == "08:00"
    assert slots[-1] == "12:30"
    assert svc.available_slots("checkup", WED)[0] == "10:00"  # Wednesday opens late


def test_today_skips_times_that_already_passed(svc):
    assert svc.available_slots("checkup", MON)[0] == "09:30"  # now is 09:00, 30 min lead time


def test_closed_past_and_too_far_days_are_rejected(svc):
    assert code(svc.available_slots, "checkup", SAT) == "closed"
    assert code(svc.available_slots, "checkup", date(2026, 10, 30)) == "in_past"
    assert code(svc.available_slots, "checkup", date(2027, 3, 1)) == "too_far"
    assert code(svc.available_slots, "root canal", TUE) == "unknown_service"


def test_booking_blocks_overlapping_times(svc):
    appt = svc.book("cleaning", TUE, "10:00", "ana novak", "041 555 123")
    assert (appt.start, appt.end) == ("2026-11-03T10:00", "2026-11-03T11:00")
    assert appt.name == "Ana Novak" and appt.phone == "041555123"

    assert code(svc.book, "checkup", TUE, "10:30", "Bo Lee", "041555124") == "taken"
    assert code(svc.book, "cleaning", TUE, "09:30", "Bo Lee", "041555124") == "taken"  # would run into 10:00
    assert svc.book("checkup", TUE, "09:30", "Bo Lee", "041555124").end == "2026-11-03T10:00"  # touching is fine
    assert svc.book("checkup", TUE, "11:00", "Cy Ray", "041555125")
    free = svc.available_slots("checkup", TUE)
    assert {"09:30", "10:00", "10:30", "11:00"}.isdisjoint(free)


def test_booking_outside_hours_or_too_soon(svc):
    assert code(svc.book, "checkup", TUE, "07:30", "Ana Novak", "041555123") == "outside_hours"
    assert code(svc.book, "cleaning", TUE, "17:30", "Ana Novak", "041555123") == "outside_hours"  # ends 18:30
    assert code(svc.book, "checkup", MON, "09:00", "Ana Novak", "041555123") == "in_past"
    assert code(svc.book, "checkup", TUE, "9am", "Ana Novak", "041555123") == "invalid_time"


@pytest.mark.parametrize("name,phone,expected", [
    ("A", "041555123", "invalid_name"),
    ("R2D2", "041555123", "invalid_name"),
    ("Ana Novak", "12345", "invalid_phone"),
    ("Ana Novak", "call me", "invalid_phone"),
])
def test_booking_validates_contact_details(svc, name, phone, expected):
    assert code(svc.book, "checkup", TUE, "10:00", name, phone) == expected


def test_cancel_needs_matching_phone_and_frees_the_slot(svc):
    appt = svc.book("filling", TUE, "14:00", "Ana Novak", "+386 41 555 123")
    assert code(svc.cancel, appt.reference, "040999888") == "phone_mismatch"
    spoken_ref = " ".join(appt.reference.lower())  # "k 7 m p q" as speech-to-text writes it
    assert svc.cancel(spoken_ref, "041 555 123").status == "cancelled"
    assert "14:00" in svc.available_slots("filling", TUE)
    assert code(svc.cancel, appt.reference, "041555123") == "not_found"


def test_references_are_unique_and_easy_to_read_aloud(svc):
    refs = {svc.book("checkup", TUE, t, "Ana Novak", "041555123").reference for t in ("08:00", "08:30", "09:00")}
    assert len(refs) == 3
    assert all(len(r) == 5 and not set(r) & set("01IO") for r in refs)


def test_bookings_persist_in_sqlite(tmp_path):
    db = tmp_path / "clinic.db"
    BookingService(db, now=lambda: NOW).book("checkup", TUE, "10:00", "Ana Novak", "041555123")
    reopened = BookingService(db, now=lambda: NOW)
    assert [a["name"] for a in reopened.upcoming()] == ["Ana Novak"]
    assert "10:00" not in reopened.available_slots("checkup", TUE)


def test_leave_message(svc):
    entry = svc.leave_message("ana novak", "041555123", "  Question about   my insurance ")
    assert entry["message"] == "Question about my insurance"
    assert svc.messages()[0]["name"] == "Ana Novak"
    assert code(svc.leave_message, "Ana Novak", "041555123", " ") == "empty_message"


def test_tools_return_errors_instead_of_raising(svc):
    result, event = run_tool(svc, "book_appointment", {
        "service": "checkup", "date": "next tuesday", "time": "10:00", "name": "Ana", "phone": "041555123", "notes": ""})
    assert result == {"ok": False, "error": "invalid_date", "detail": "Date should look like 2026-11-03."}
    assert event is None

    result, event = run_tool(svc, "check_availability", {"service": "checkup", "date": "2026-11-07"})
    assert result["error"] == "closed"

    result, event = run_tool(svc, "book_appointment", {
        "service": "checkup", "date": "2026-11-03", "time": "10:00", "name": "Ana Novak", "phone": "041555123", "notes": ""})
    assert result["ok"] and event["type"] == "booked"
