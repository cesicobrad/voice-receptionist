from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.assistant import (ClaudeReceptionist, build_system_prompt, greeting, requested_language,
                           sl_date)
from app.booking import load_clinic
from app.main import create_app
from app.speech import Transcriber, tts_voice
from tests.conftest import NOW
from tests.test_assistant import FakeClient, NS


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("RECEPTIONIST_LANGUAGE", "WHISPER_MODEL", "TTS_VOICE"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("day,expected", [
    (date(2026, 10, 24), "sobota, 24. oktobra 2026"),
    (date(2026, 11, 5), "četrtek, 5. novembra 2026"),
    (date(2027, 2, 1), "ponedeljek, 1. februarja 2027"),
])
def test_slovenian_dates(day, expected):
    assert sl_date(day) == expected


def test_language_setting(monkeypatch):
    assert requested_language() == "en"
    monkeypatch.setenv("RECEPTIONIST_LANGUAGE", "SL")
    assert requested_language() == "sl"
    monkeypatch.setenv("RECEPTIONIST_LANGUAGE", "de")
    with pytest.raises(ValueError):
        requested_language()


def test_slovenian_prompt_and_greeting():
    clinic = load_clinic()
    sl = build_system_prompt(clinic, "notes", NOW.date(), "sl")
    en = build_system_prompt(clinic, "notes", NOW.date(), "en")
    assert "Speak Slovenian" in sl and "ponedeljek, 2. novembra 2026" in sl and "ob pol desetih" in sl
    assert "Slovenian" not in en and "half past two" in en
    assert greeting(clinic, "sl").startswith("Pozdravljeni, dobili ste Lindenvale Dental.")


def test_speech_defaults_follow_language(monkeypatch):
    assert Transcriber(language="en").model_size == "small.en"
    assert Transcriber(language="sl").model_size == "large-v3-turbo"  # multilingual model
    assert tts_voice("sl") == "sl-SI-PetraNeural"
    assert tts_voice("en") == "en-GB-SoniaNeural"
    monkeypatch.setenv("WHISPER_MODEL", "medium")
    monkeypatch.setenv("TTS_VOICE", "sl-SI-RokNeural")
    assert Transcriber(language="sl").model_size == "medium"
    assert tts_voice("sl") == "sl-SI-RokNeural"


class NoSTT:
    available = False


def test_slovenian_session_needs_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEPTIONIST_LANGUAGE", "sl")
    monkeypatch.setenv("RECEPTIONIST_MODE", "offline")
    client = TestClient(create_app(db_path=str(tmp_path / "a.db"), transcriber=NoSTT(), now=lambda: NOW))
    assert client.post("/api/session").json()["language"] == "en"  # offline brain is English-only

    monkeypatch.setenv("RECEPTIONIST_MODE", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")  # no request is made
    client = TestClient(create_app(db_path=str(tmp_path / "b.db"), transcriber=NoSTT(), now=lambda: NOW))
    session = client.post("/api/session").json()
    assert session["language"] == "sl" and session["mode"] == "claude"
    assert session["greeting"].startswith("Pozdravljeni")


def test_fallbacks_only_sent_to_models_that_support_them(svc):
    done = NS(stop_reason="end_turn", content=[NS(type="text", text="Dober dan.")], usage=None)
    for model, expect in [("claude-opus-5-5", True), ("claude-sonnet-5-5", True), ("claude-haiku-5-5", False)]:
        client = FakeClient([done])
        ClaudeReceptionist(svc, client=client, model=model, language="sl").respond("Dober dan")
        call = client.calls[0]
        assert ("fallbacks" in call) is expect, model
        assert "Speak Slovenian" in call["system"]
