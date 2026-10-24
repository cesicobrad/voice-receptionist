"""Appointment booking: business rules and a small SQLite store.

All times are local, naive datetimes (the clinic lives in one time zone).
`now` is injectable so the rules can be tested without a clock.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

CLINIC_FILE = Path(__file__).with_name("clinic.json")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def load_clinic(path: Path = CLINIC_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class BookingError(Exception):
    """A booking rule was broken. `code` is stable and safe to show to the model."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Appointment:
    reference: str
    service: str
    start: str  # ISO "YYYY-MM-DDTHH:MM"
    end: str
    name: str
    phone: str
    notes: str
    status: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", phone or "")
    digits = cleaned.lstrip("+")
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        raise BookingError("invalid_phone", "Phone number should have 6 to 15 digits.")
    return ("+" if cleaned.startswith("+") else "") + digits


def normalize_name(name: str) -> str:
    name = " ".join((name or "").split())
    if len(name) < 2 or len(name) > 80 or any(ch.isdigit() for ch in name):
        raise BookingError("invalid_name", "Please give a name without numbers.")
    return name.title() if name.islower() or name.isupper() else name


def _hm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


class BookingService:
    def __init__(self, db_path: str | Path, clinic: dict | None = None,
                 now: Callable[[], datetime] = datetime.now):
        self.clinic = clinic or load_clinic()
        self.now = now
        self.db_path = str(db_path)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                reference TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'booked',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    # ---- rules -------------------------------------------------------------

    def service(self, key: str) -> dict:
        svc = self.clinic["services"].get((key or "").lower().strip())
        if not svc:
            known = ", ".join(self.clinic["services"])
            raise BookingError("unknown_service", f"Unknown service. Choose one of: {known}.")
        return svc

    def opening_hours(self, day: date) -> tuple[time, time] | None:
        hours = self.clinic["hours"][WEEKDAYS[day.weekday()]]
        return (_hm(hours[0]), _hm(hours[1])) if hours else None

    def _check_day(self, day: date) -> tuple[time, time]:
        today = self.now().date()
        if day < today:
            raise BookingError("in_past", "That date has already passed.")
        if day > today + timedelta(days=self.clinic["booking_horizon_days"]):
            raise BookingError("too_far", f"We book at most {self.clinic['booking_horizon_days']} days ahead.")
        hours = self.opening_hours(day)
        if not hours:
            raise BookingError("closed", f"The clinic is closed on {WEEKDAYS[day.weekday()].title()}s.")
        return hours

    def _busy(self, day: date) -> list[tuple[datetime, datetime]]:
        rows = self._db.execute(
            "SELECT start, end FROM appointments WHERE status = 'booked' AND start LIKE ?",
            (day.isoformat() + "%",),
        ).fetchall()
        return [(datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])) for r in rows]

    def available_slots(self, service: str, day: date) -> list[str]:
        """Start times ("HH:MM") where `service` fits completely inside opening hours."""
        minutes = self.service(service)["minutes"]
        opens, closes = self._check_day(day)
        step = timedelta(minutes=self.clinic["slot_minutes"])
        length = timedelta(minutes=minutes)
        busy = self._busy(day)
        earliest = self.now() + timedelta(minutes=30)  # no bookings starting in the next half hour
        slots = []
        start = datetime.combine(day, opens)
        while start + length <= datetime.combine(day, closes):
            end = start + length
            if start >= earliest and all(end <= b0 or start >= b1 for b0, b1 in busy):
                slots.append(start.strftime("%H:%M"))
            start += step
        return slots

    # ---- actions -----------------------------------------------------------

    def book(self, service: str, day: date, start_time: str, name: str, phone: str,
             notes: str = "") -> Appointment:
        name = normalize_name(name)
        phone = normalize_phone(phone)
        try:
            start_t = _hm(start_time)
        except (TypeError, ValueError):
            raise BookingError("invalid_time", "Time should look like 14:30.")
        free = self.available_slots(service, day)  # validates service and day too
        hhmm = start_t.strftime("%H:%M")
        if hhmm not in free:
            opens, closes = self.opening_hours(day)
            start = datetime.combine(day, start_t)
            minutes = self.service(service)["minutes"]
            if start_t < opens or start + timedelta(minutes=minutes) > datetime.combine(day, closes):
                raise BookingError("outside_hours", f"We are open {opens:%H:%M} to {closes:%H:%M} that day.")
            if start < self.now() + timedelta(minutes=30):
                raise BookingError("in_past", "That time is too soon or has passed.")
            raise BookingError("taken", "That time is not available.")
        start = datetime.combine(day, start_t)
        end = start + timedelta(minutes=self.service(service)["minutes"])
        appt = Appointment(
            reference=self._new_reference(),
            service=service.lower().strip(),
            start=start.isoformat(timespec="minutes"),
            end=end.isoformat(timespec="minutes"),
            name=name,
            phone=phone,
            notes=(notes or "").strip()[:500],
            status="booked",
            created_at=self.now().isoformat(timespec="seconds"),
        )
        self._db.execute(
            "INSERT INTO appointments VALUES (:reference, :service, :start, :end, :name, :phone, :notes, :status, :created_at)",
            appt.to_dict(),
        )
        self._db.commit()
        return appt

    def cancel(self, reference: str, phone: str) -> Appointment:
        ref = (reference or "").upper().replace(" ", "").replace("-", "")
        row = self._db.execute("SELECT * FROM appointments WHERE reference = ?", (ref,)).fetchone()
        if not row or row["status"] != "booked":
            raise BookingError("not_found", "No active booking with that reference.")
        if normalize_phone(phone)[-6:] != row["phone"][-6:]:
            raise BookingError("phone_mismatch", "The phone number does not match that booking.")
        self._db.execute("UPDATE appointments SET status = 'cancelled' WHERE reference = ?", (ref,))
        self._db.commit()
        return Appointment(**{**dict(row), "status": "cancelled"})

    def leave_message(self, name: str, phone: str, message: str) -> dict:
        message = " ".join((message or "").split())
        if not message:
            raise BookingError("empty_message", "The message is empty.")
        entry = {
            "name": normalize_name(name),
            "phone": normalize_phone(phone),
            "message": message[:1000],
            "created_at": self.now().isoformat(timespec="seconds"),
        }
        cur = self._db.execute(
            "INSERT INTO messages (name, phone, message, created_at) VALUES (:name, :phone, :message, :created_at)",
            entry,
        )
        self._db.commit()
        return {"id": cur.lastrowid, **entry}

    def upcoming(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM appointments WHERE status = 'booked' AND start >= ? ORDER BY start LIMIT ?",
            (self.now().date().isoformat(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def messages(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _new_reference(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easier to read aloud
        while True:
            ref = "".join(secrets.choice(alphabet) for _ in range(5))
            if not self._db.execute("SELECT 1 FROM appointments WHERE reference = ?", (ref,)).fetchone():
                return ref
