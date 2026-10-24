"""The receptionist "brains".

ClaudeReceptionist  - Claude with tool use (the real thing, needs an API key).
ScriptedReceptionist - a small rule-based fallback so the demo runs offline.
Both expose `greeting` and `respond(text) -> Turn`.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .booking import BookingService, WEEKDAYS
from .tools import run_tool, tool_definitions

log = logging.getLogger("receptionist")
KNOWLEDGE_FILE = Path(__file__).with_name("knowledge.md")
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`). Haiku 5.5 has none.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}


@dataclass
class Turn:
    reply: str
    events: list[dict] = field(default_factory=list)


LANGUAGES = ("en", "sl")
SL_WEEKDAYS = ["ponedeljek", "torek", "sreda", "četrtek", "petek", "sobota", "nedelja"]
SL_MONTHS = ["januarja", "februarja", "marca", "aprila", "maja", "junija", "julija",
             "avgusta", "septembra", "oktobra", "novembra", "decembra"]  # genitive: "24. oktobra"


def requested_language() -> str:
    lang = (os.getenv("RECEPTIONIST_LANGUAGE") or "en").lower()
    if lang not in LANGUAGES:
        raise ValueError(f"RECEPTIONIST_LANGUAGE must be one of {LANGUAGES}, got {lang!r}")
    return lang


def claude_enabled() -> bool:
    wanted = (os.getenv("RECEPTIONIST_MODE") or "auto").lower()
    has_key = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return wanted == "claude" or (wanted == "auto" and has_key)


def greeting(clinic: dict, language: str = "en") -> str:
    if language == "sl":
        return f"Pozdravljeni, dobili ste {clinic['business']}. Sem {clinic['assistant']}, virtualna receptorka. Kako vam lahko pomagam?"
    return f"Hello, you've reached {clinic['business']}. I'm {clinic['assistant']}, the virtual receptionist. How can I help?"


def sl_date(day: date) -> str:
    """'sobota, 24. oktobra 2026', the way a Slovenian receptionist would say it."""
    return f"{SL_WEEKDAYS[day.weekday()]}, {day.day}. {SL_MONTHS[day.month - 1]} {day.year}"


def say_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()].title()} {day.day} {day:%B}"


def hours_sentence(clinic: dict) -> str:
    parts = []
    for day in WEEKDAYS:
        h = clinic["hours"][day]
        parts.append(f"{day.title()} {h[0]} to {h[1]}" if h else f"{day.title()} closed")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

SPEECH_STYLE = {
    "en": """Everything you say is converted to speech, so:
- Answer in one to three short, natural sentences, under 40 words. No lists, markdown, emoji or abbreviations.
- Only mention information the caller needs right now; offer more instead of reciting it.
- Say dates like "Tuesday the 3rd of November" and times like "half past two" or "9 am".
- When you read out a booking reference, spell it letter by letter, separated by spaces.
- Ask for one missing detail at a time.""",
    "sl": """This line is in Slovenia. Speak Slovenian (slovenščina) and use the formal "vi" form, even though
these instructions and the clinic notes are in English. Switch to English only if the caller speaks English.
Everything you say is converted to speech, so:
- Answer in one to three short, natural sentences, under 40 words. No lists, markdown, emoji or abbreviations.
- Only mention information the caller needs right now; offer more instead of reciting it.
- Say dates the Slovenian way: "v torek, 27. oktobra". Say times in words: "ob devetih", "ob pol desetih"
  (9:30), "ob četrt na enajst" (10:15), "ob dveh popoldne" (14:00). Prices: "sedemdeset evrov".
- Read phone numbers back in groups of digits. Spell booking references letter by letter, separated by spaces.
- Ask for one missing detail at a time.""",
}


def build_system_prompt(clinic: dict, knowledge: str, today: date, language: str = "en") -> str:
    services = "\n".join(
        f"- {key}: {s['label']}, {s['minutes']} minutes, {s['price_eur']} euros"
        for key, s in clinic["services"].items()
    )
    return f"""You are {clinic['assistant']}, the virtual receptionist of {clinic['business']}, answering a voice call.
Today is {WEEKDAYS[today.weekday()].title()}, {today.isoformat()}{f" ({sl_date(today)})" if language == "sl" else ""}.

{SPEECH_STYLE[language]}

How you work:
- Use check_availability before offering any time; never invent free slots. Offer two or three options, not the whole list.
- Before book_appointment, read the service, day, time and name back once and get a yes.
- Speech-to-text can mishear names and numbers. If something looks garbled, ask the caller to repeat it.
- Do not diagnose or give medication advice. For anything you cannot handle, offer leave_message so staff call back.
- For danger signs (heavy bleeding, swelling that affects breathing or swallowing, facial injury) tell the caller to call 112 now.
- Stay on clinic topics and keep a calm, friendly, professional tone.

Clinic: {clinic['business']}, {clinic['address']}, phone {clinic['phone']}.
Opening hours: {hours_sentence(clinic)}.
Services (key: label, length, price):
{services}

Front desk notes:
{knowledge}"""


class ClaudeReceptionist:
    mode = "claude"

    def __init__(self, bookings: BookingService, client=None, model: str | None = None,
                 effort: str | None = None, language: str = "en"):
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model or (os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5")
        self.effort = effort or (os.getenv("CLAUDE_EFFORT") or "low")  # low keeps voice replies snappy
        self.bookings = bookings
        self.clinic = bookings.clinic
        self.tools = tool_definitions(list(self.clinic["services"]))
        self.knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")
        self.language = language
        self.greeting = greeting(self.clinic, language)
        self.calls: list[dict] = []  # per-request usage and latency, for logging and model comparison
        # Append-only history; assistant content is passed back unchanged (thinking blocks included).
        self.history: list[dict] = [{"role": "assistant", "content": self.greeting}]

    def respond(self, text: str) -> Turn:
        checkpoint = len(self.history)
        self.history.append({"role": "user", "content": text})
        events: list[dict] = []
        system = build_system_prompt(self.clinic, self.knowledge, self.bookings.now().date(), self.language)
        try:
            for _ in range(8):
                extra = {}
                if self.model in SERVER_FALLBACK_MODELS:
                    # If a safety classifier declines, the API retries on a suitable model.
                    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                started = time.perf_counter()
                response = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    tools=self.tools,
                    messages=self.history,
                    output_config={"effort": self.effort},
                    **extra,
                )
                self._record(response, time.perf_counter() - started)
                if response.stop_reason == "refusal":
                    del self.history[checkpoint:]
                    return Turn("Sorry, I can't help with that. I can book, move or cancel appointments, or take a message.")
                self.history.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    reply = " ".join(b.text for b in response.content if b.type == "text").strip()
                    return Turn(reply or "Sorry, could you say that again?", events)
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result, event = run_tool(self.bookings, block.name, dict(block.input))
                    log.info("tool %s(%s) -> %s", block.name, block.input, result)
                    if event:
                        events.append(event)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": _json(result), "is_error": not result.get("ok", True)})
                self.history.append({"role": "user", "content": results})
            return Turn("Sorry, that took too long on my side. Could you repeat the request?", events)
        except self.anthropic.AuthenticationError:
            log.exception("Claude authentication failed")
            reply = "My connection to the assistant service is not set up. Please check the API key."
        except self.anthropic.RateLimitError:
            reply = "I'm a little busy right now. Please try again in a moment."
        except (self.anthropic.APIConnectionError, self.anthropic.APIStatusError):
            log.exception("Claude request failed")
            reply = "Sorry, I lost my connection for a moment. Could you say that again?"
        del self.history[checkpoint:]
        return Turn(reply, events)


    def _record(self, response, seconds: float):
        usage = getattr(response, "usage", None)
        self.calls.append({
            "seconds": round(seconds, 3),
            "stop_reason": response.stop_reason,
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        })


def _json(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------
# Offline fallback: keyword intents + slot filling
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
NUMBER_WORDS = {w: i for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
     "ten", "eleven", "twelve"])}
AMPM = r"(a\.?\s?m\.?|p\.?\s?m\.?)"


def parse_date(text: str, today: date) -> date | None:
    t = text.lower()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if "tomorrow" in t:
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", t):
        return today
    month = r"(" + "|".join(MONTHS) + r")"
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? {month}\b", t) or \
        re.search(rf"\b{month} (\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        day_s, month_s = (m[1], m[2]) if m[1].isdigit() else (m[2], m[1])
        try:
            candidate = date(today.year, MONTHS[month_s], int(day_s))
        except ValueError:
            return None
        return candidate if candidate >= today else candidate.replace(year=today.year + 1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            ahead = (i - today.weekday()) % 7 or 7  # "Monday" said on a Monday means next week
            if "next week" in t and ahead <= 6 - today.weekday():
                ahead += 7  # still this week, so push to next week
            return today + timedelta(days=ahead)
    if m := re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", t):
        d = int(m[1])
        try:
            if d >= today.day:
                return today.replace(day=d)
            return (today.replace(day=1) + timedelta(days=32)).replace(day=d)
        except ValueError:
            return None
    return None


def parse_time(text: str, expecting: bool = False) -> str | None:
    t = text.lower().replace("o'clock", "").strip()
    for word, n in NUMBER_WORDS.items():
        t = re.sub(rf"\b{word}\b", str(n), t)
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return "12:00"
    hour = minute = None
    ampm = ""
    if m := re.search(rf"\bhalf past (\d{{1,2}})\b", t):
        hour, minute = int(m[1]), 30
    elif m := re.search(rf"\b(\d{{1,2}})[:.](\d{{2}})\s*{AMPM}?", t):
        hour, minute, ampm = int(m[1]), int(m[2]), m[3] or ""
    elif m := re.search(rf"\b(\d{{1,2}})\s*{AMPM}", t):
        hour, minute, ampm = int(m[1]), 0, m[2]
    elif m := re.search(r"\b(?:at|around|about)\s+(?:around\s+|about\s+)?(\d{1,2})\b(?!\s*(?:st|nd|rd|th|of|\d))", t):
        hour, minute = int(m[1]), 0
    elif expecting and (m := re.fullmatch(r"\D*?(\d{1,2})\W*", t)):
        hour, minute = int(m[1]), 0
    if hour is None or hour > 23 or minute > 59:
        return None
    ampm = ampm.replace(".", "").replace(" ", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and 1 <= hour <= 7:
        hour += 12  # "at 3" at a clinic means 15:00
    return f"{hour:02d}:{minute:02d}"


def parse_phone(text: str) -> str | None:
    for m in re.finditer(r"\+?\d[\d\s\-()]{4,}\d", text):
        digits = re.sub(r"\D", "", m[0])
        if 6 <= len(digits) <= 15:
            return ("+" if m[0].startswith("+") else "") + digits
    return None


def parse_name(text: str, expecting: bool = False) -> str | None:
    t = text.strip().rstrip(".!")
    prefixes = r"my name is|my name's|name is|this is|you can put it under|under the name"
    if expecting:
        prefixes += r"|i'm|i am|it's|it is"
    if m := re.search(rf"\b(?:{prefixes})\s+([A-Za-zÀ-ž'\-]+(?:\s+[A-Za-zÀ-ž'\-]+){{0,2}})", t, re.I):
        name = re.split(r"\s+(?:and|my|phone|number)\b", m[1], flags=re.I)[0]
        return name.strip()
    if expecting and re.fullmatch(r"[A-Za-zÀ-ž'\-]+(?:\s+[A-Za-zÀ-ž'\-]+){0,2}", t):
        return t
    # "Luka Horvat, and my number is 040 123 456"
    if expecting and (m := re.match(r"([A-Za-zÀ-ž'\-]+(?:\s+[A-Za-zÀ-ž'\-]+){0,2})\s*(?:,|\band\b)", t)):
        return m[1]
    return None


def parse_reference(text: str) -> str | None:
    # Letters read out one by one: "K 7 M P Q" (but not the "s" of "it's")
    if m := re.search(r"(?<![\w'])(?:[A-Za-z0-9][\s\-.]+){4}[A-Za-z0-9](?!\w)", text):
        return re.sub(r"[\s\-.]", "", m[0]).upper()
    for token in re.findall(r"\b[A-Za-z0-9]{5}\b", text):
        if token.isupper() or any(c.isdigit() for c in token):
            return token.upper()
    return None


def has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", text) for w in words)


class ScriptedReceptionist:
    """Deterministic and offline. Good enough to show the voice loop and the booking store."""

    mode = "offline"

    def __init__(self, bookings: BookingService):
        self.bookings = bookings
        self.clinic = bookings.clinic
        self.greeting = greeting(self.clinic)
        self.task: str | None = None  # "book" | "cancel" | "message"
        self.form: dict = {}
        self.awaiting: str | None = None

    def find_service(self, text: str) -> str | None:
        for key, svc in self.clinic["services"].items():
            if any(re.search(rf"\b{re.escape(k)}", text) for k in svc["keywords"]):
                return key
        return None

    def respond(self, text: str) -> Turn:
        t = text.lower().strip()
        today = self.bookings.now().date()
        if has(t, "112") or (has(t, "bleeding") and has(t, "heav", "a lot", "won't stop")) or has(t, "can't breathe", "cannot breathe", "hard to breathe", "trouble breathing",
                     "difficulty breathing", "hard to swallow", "can't swallow", "cannot swallow",
                     "trouble swallowing", "difficulty swallowing"):
            self.reset()
            return Turn("That sounds serious. Please hang up and call 112 right now.")
        if self.task is None:
            if has(t, "cancel"):
                self.task = "cancel"
            elif has(t, "book", "appointment", "schedule", "reserve", "come in", "slot", "available", "see a dentist", "see the dentist") \
                    or (self.find_service(t) and (parse_date(t, today) or has(t, "want", "need", "like", "can i", "could i"))):
                self.task = "book"
            elif has(t, "call me back", "callback", "call back", "leave a message", "message", "speak to", "talk to"):
                self.task = "message"
            elif self.find_service(t) == "emergency":
                self.task = "book"
            else:
                return Turn(self.answer_question(t))
        if self.task == "book":
            return self.book_step(text, t, today)
        if self.task == "cancel":
            return self.cancel_step(text)
        return self.message_step(text)

    def reset(self):
        self.task, self.form, self.awaiting = None, {}, None

    def answer_question(self, t: str) -> str:
        c = self.clinic
        if has(t, "thank", "bye", "goodbye", "that's all", "that is all"):
            return "You're welcome. Have a good day, and goodbye."
        # Answer every topic in the question ("Are you open Friday? And is there parking?")
        answers = []
        if has(t, "hour", "open", "close", "when are you"):
            answers.append(f"We're open {hours_sentence(c)}.")
        if has(t, "price", "cost", "how much", "expensive", "fee"):
            svc = self.find_service(t)
            if svc:
                s = c["services"][svc]
                answers.append(f"A {s['label'].lower()} is {s['price_eur']} euros and takes about {s['minutes']} minutes.")
            else:
                answers.append("A check-up is 45 euros, a cleaning 70, a filling 90 and whitening 220 euros.")
        if has(t, "where", "address", "located", "find you", "location"):
            answers.append(f"We're at {c['address']}.")
        if has(t, "park", "bus", r"car\b"):
            answers.append("There's free parking behind the building, entrance from Birch Lane. "
                           "Buses 3 and 7 stop at Linden Square, two minutes away.")
        if has(t, "insurance", "insured", "covered"):
            answers.append("We accept the national health insurance for check-ups and fillings. "
                           "Whitening is not covered. Please bring your insurance card.")
        if has(t, "pay", "card", "cash"):
            answers.append("You can pay by card, cash or bank transfer after the visit.")
        if has(t, "child", "kid", "son", "daughter"):
            answers.append("We see children from age three, with a parent present.")
        if has(t, "new patient", "first time", "first visit"):
            answers.append("New patients are welcome. Please arrive ten minutes early to fill in a short health form.")
        if answers:
            return " ".join(answers)
        if has(t, "hello", "hi", "good morning", "good afternoon", "hey"):
            return "Hello. I can book or cancel an appointment, answer questions about the clinic, or take a message."
        return "I can book or cancel appointments, tell you about our hours, prices and location, or take a message for the team. What would you like?"

    # ---- booking ---------------------------------------------------------

    def book_step(self, raw: str, t: str, today: date) -> Turn:
        f = self.form
        if svc := self.find_service(t):
            f["service"] = svc
        if day := parse_date(t, today):
            f["date"] = day
        if tm := parse_time(t, expecting=self.awaiting == "time"):
            f["time"] = tm
        if (phone := parse_phone(raw)) and (self.awaiting == "phone" or len(re.sub(r"\D", "", phone)) >= 7):
            f["phone"] = phone
        if name := parse_name(raw, expecting=self.awaiting == "name"):
            f["name"] = name

        if "service" not in f:
            self.awaiting = "service"
            return Turn("Sure. What is the visit for: a check-up, a cleaning, a filling, whitening, or an emergency?")
        label = self.clinic["services"][f["service"]]["label"].lower()
        if "date" not in f:
            self.awaiting = "date"
            if f["service"] == "emergency":
                return Turn("I'm sorry you're in pain. We keep emergency slots every day. Would today or tomorrow work?")
            return Turn(f"A {label}. Which day would suit you?")
        if "time" not in f or not self._slot_ok(f):
            return self._offer_times(f, label)
        if "name" not in f:
            self.awaiting = "name"
            return Turn(f"{say_date(f['date'])} at {f['time']} is free. May I have your full name?")
        if "phone" not in f:
            self.awaiting = "phone"
            return Turn(f"Thank you, {f['name'].split()[0]}. And a phone number we can reach you on?")
        result, event = run_tool(self.bookings, "book_appointment", {
            "service": f["service"], "date": f["date"].isoformat(), "time": f["time"],
            "name": f["name"], "phone": f["phone"], "notes": ""})
        if not result["ok"]:
            field_ = {"invalid_name": "name", "invalid_phone": "phone"}.get(result["error"], "time")
            f.pop(field_, None)
            self.awaiting = field_
            return Turn(f"{result['detail']} Could you give me the {field_} again?")
        self.reset()
        ref = " ".join(result["reference"])
        return Turn(f"You're booked for a {label} on {say_date(date.fromisoformat(result['start'][:10]))} "
                    f"at {result['start'][11:]}. Your reference is {ref}. Anything else?", [event])

    def _slot_ok(self, f: dict) -> bool:
        try:
            return f["time"] in self.bookings.available_slots(f["service"], f["date"])
        except Exception:
            return False

    def _offer_times(self, f: dict, label: str) -> Turn:
        result, _ = run_tool(self.bookings, "check_availability",
                             {"service": f["service"], "date": f["date"].isoformat()})
        asked = f.pop("time", None)
        if not result.get("free_start_times"):
            f.pop("date")
            self.awaiting = "date"
            why = result.get("detail") or f"{say_date(date.fromisoformat(result['date']))} is fully booked."
            return Turn(f"{why} Which other day would work?")
        slots = result["free_start_times"]
        self.awaiting = "time"
        if asked:
            later = [s for s in slots if s > asked] or slots
            return Turn(f"{asked} isn't available on {say_date(f['date'])}. The nearest free times are "
                        f"{_join(later[:3])}. Would one of those work?")
        picks = slots[:2] + ([slots[len(slots) // 2]] if len(slots) > 4 else [])
        return Turn(f"On {say_date(f['date'])} I have {_join(picks)}, among others. What time would you like?")

    # ---- cancel / message ------------------------------------------------

    def cancel_step(self, raw: str) -> Turn:
        f = self.form
        if ref := parse_reference(raw):
            f["reference"] = ref
        if phone := parse_phone(raw):
            f["phone"] = phone
        if "reference" not in f:
            return Turn("I can cancel that. What is your five-character booking reference?")
        if "phone" not in f:
            return Turn("Thanks. And the phone number on the booking?")
        result, event = run_tool(self.bookings, "cancel_appointment", f)
        self.reset()
        if not result["ok"]:
            return Turn(f"{result['detail']} Would you like me to take a message for the team instead?")
        when = datetime.fromisoformat(result["start"])
        return Turn(f"Done. Your appointment on {say_date(when.date())} at {when:%H:%M} is cancelled. Anything else?", [event])

    def message_step(self, raw: str) -> Turn:
        f = self.form
        if self.awaiting == "message":
            f["message"] = raw.strip()
        if name := parse_name(raw, expecting=self.awaiting == "name"):
            f["name"] = name
        if (phone := parse_phone(raw)) and self.awaiting == "phone":
            f["phone"] = phone
        if "message" not in f:
            self.awaiting = "message"
            return Turn("Of course. What should I tell the team?")
        if "name" not in f:
            self.awaiting = "name"
            return Turn("Got it. Your name, please?")
        if "phone" not in f:
            self.awaiting = "phone"
            return Turn("And the best number to call you back on?")
        result, event = run_tool(self.bookings, "leave_message", f)
        self.reset()
        if not result["ok"]:
            return Turn(f"{result['detail']} Let's try that again: what should I tell the team?")
        return Turn("Thanks, I've passed that on. Someone will call you back within one working day.", [event])


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def make_receptionist(bookings: BookingService, language: str = "en"):
    """Claude when credentials are configured, otherwise the offline script (English only)."""
    if claude_enabled():
        return ClaudeReceptionist(bookings, language=language)
    return ScriptedReceptionist(bookings)
