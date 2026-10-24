from types import SimpleNamespace as NS

import pytest

from app.assistant import ClaudeReceptionist, ScriptedReceptionist, parse_date, parse_time
from tests.conftest import NOW, THU, TUE


@pytest.mark.parametrize("text,expected", [
    ("tomorrow please", TUE),
    ("Thursday works", THU),
    ("on the 5th of November", THU),
    ("November 5th", THU),
    ("2026-11-05", THU),
    ("monday", NOW.date().replace(day=9)),  # said on a Monday: next Monday
    ("whenever", None),
])
def test_parse_date(text, expected):
    assert parse_date(text, NOW.date()) == expected


@pytest.mark.parametrize("text,expecting,expected", [
    ("at 3", False, "15:00"),
    ("10:30 a.m.", False, "10:30"),
    ("2 pm please", False, "14:00"),
    ("half past nine", False, "09:30"),
    ("Nine.", True, "09:00"),
    ("noon", False, "12:00"),
    ("on the 5th of November", False, None),
    ("my number is 041 555 123", False, None),
])
def test_parse_time(text, expecting, expected):
    assert parse_time(text, expecting) == expected


def test_scripted_booking_conversation_writes_to_store(svc):
    bot = ScriptedReceptionist(svc)
    assert "which day" in bot.respond("Hi, I'd like to book a cleaning.").reply.lower()
    offer = bot.respond("Tuesday would be good.").reply
    assert "Tuesday 3 November" in offer and "08:00" in offer
    assert "full name" in bot.respond("At 10 am.").reply
    assert "phone number" in bot.respond("Ana Novak").reply
    final = bot.respond("It's 041 555 123.")
    assert "You're booked" in final.reply
    [event] = final.events
    assert event["type"] == "booked"
    assert svc.upcoming()[0]["start"] == "2026-11-03T10:00"


def test_scripted_offers_alternatives_when_slot_taken(svc):
    svc.book("checkup", TUE, "10:00", "Bo Lee", "041555124")
    bot = ScriptedReceptionist(svc)
    reply = bot.respond("Can I book a checkup on Tuesday at 10?").reply
    assert "10:00 isn't available" in reply and "10:30" in reply


def test_scripted_answers_questions_and_escalates_danger(svc):
    bot = ScriptedReceptionist(svc)
    assert "Friday 08:00 to 14:00" in bot.respond("What are your opening hours?").reply
    assert "70 euros" in bot.respond("How much is a cleaning?").reply
    assert "112" in bot.respond("My face is swollen and I can't breathe").reply


def test_scripted_cancel_and_message(svc):
    appt = svc.book("checkup", TUE, "10:00", "Ana Novak", "041555123")
    bot = ScriptedReceptionist(svc)
    assert "reference" in bot.respond("I need to cancel my appointment").reply
    spoken = " ".join(appt.reference)
    reply = bot.respond(f"It's {spoken}, phone 041 555 123").reply
    assert "cancelled" in reply

    bot.respond("Can someone call me back?")
    bot.respond("I have a question about my insurance.")
    bot.respond("Ana Novak")
    assert bot.respond("041 555 123").events[0]["type"] == "message"
    assert svc.messages()[0]["message"] == "I have a question about my insurance."


# ---- Claude loop with a fake client: checks tool plumbing without network ----

class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def test_claude_loop_runs_tools_and_keeps_history(svc):
    tool_call = NS(type="tool_use", id="tu_1", name="book_appointment", input={
        "service": "checkup", "date": "2026-11-03", "time": "10:00",
        "name": "Ana Novak", "phone": "041555123", "notes": ""})
    client = FakeClient([
        NS(stop_reason="tool_use", content=[NS(type="thinking", thinking=""), tool_call]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="You're booked for Tuesday at 10 am.")]),
    ])
    bot = ClaudeReceptionist(svc, client=client, model="claude-opus-5-5", effort="low")
    turn = bot.respond("Book me a checkup Tuesday at ten, Ana Novak, 041 555 123.")

    assert turn.reply == "You're booked for Tuesday at 10 am."
    assert turn.events[0]["type"] == "booked"
    assert svc.upcoming()[0]["start"] == "2026-11-03T10:00"
    first, second = client.calls
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default"
    assert "Monday, 2026-11-02" in first["system"]
    result = second["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "tu_1" and result["is_error"] is False
    assert second["messages"][-2]["content"][0].type == "thinking"  # passed back unchanged


def test_claude_refusal_rolls_back_the_turn(svc):
    client = FakeClient([NS(stop_reason="refusal", content=[])])
    bot = ClaudeReceptionist(svc, client=client)
    before = list(bot.history)
    assert "can't help" in bot.respond("something off-topic").reply
    assert bot.history == before
