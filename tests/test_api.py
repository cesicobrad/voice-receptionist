from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import NOW


class NoSTT:
    available = False


def test_offline_session_books_through_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEPTIONIST_MODE", "offline")
    client = TestClient(create_app(db_path=str(tmp_path / "api.db"), transcriber=NoSTT(), now=lambda: NOW))

    session = client.post("/api/session").json()
    assert session["mode"] == "offline" and session["stt"] is False
    sid = session["session_id"]

    say = lambda text: client.post("/api/chat", json={"session_id": sid, "text": text}).json()
    assert "08:00" in say("I'd like a checkup on Tuesday")["reply"]
    say("8 am")
    say("Ana Novak")
    done = say("041 555 123")
    assert done["events"][0]["type"] == "booked"

    [appt] = client.get("/api/appointments").json()
    assert appt["name"] == "Ana Novak" and appt["label"] == "Check-up and X-ray"


def test_unknown_session_and_missing_stt(tmp_path):
    client = TestClient(create_app(db_path=str(tmp_path / "api.db"), transcriber=NoSTT()))
    assert client.post("/api/chat", json={"session_id": "nope", "text": "hi"}).status_code == 404
    assert client.post("/api/stt", files={"audio": ("a.webm", b"x")}).status_code == 503
    assert client.get("/").status_code == 200
