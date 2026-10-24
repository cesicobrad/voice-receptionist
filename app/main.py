"""FastAPI app: serves the page and the /api endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from .assistant import claude_enabled, make_receptionist, requested_language  # noqa: E402  (reads env set by .env)
from .booking import BookingService  # noqa: E402
from .speech import Transcriber, synthesize  # noqa: E402

logging.basicConfig(level=(os.getenv("LOG_LEVEL") or "INFO"), format="%(levelname)s %(name)s: %(message)s")
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_SESSIONS = 100


class ChatIn(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=2000)


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


def create_app(db_path: str | None = None, transcriber: Transcriber | None = None,
               now: Callable[[], datetime] = datetime.now) -> FastAPI:
    db_path = db_path or os.getenv("DB_PATH") or str(ROOT.parent / "data" / "receptionist.db")
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    bookings = BookingService(db_path, now=now)
    language = requested_language()
    if language == "sl" and not claude_enabled():
        logging.getLogger("receptionist").warning("Slovenian needs Claude; the offline brain is English-only. Using English.")
        language = "en"
    stt = transcriber or Transcriber(language=language)
    sessions: OrderedDict[str, tuple] = OrderedDict()
    app = FastAPI(title="Voice receptionist demo")
    app.state.bookings = bookings

    @app.post("/api/session")
    def new_session():
        receptionist = make_receptionist(bookings, language)
        sid = secrets.token_urlsafe(12)
        sessions[sid] = (receptionist, threading.Lock())
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
        return {
            "session_id": sid,
            "mode": receptionist.mode,
            "model": getattr(receptionist, "model", None),
            "greeting": receptionist.greeting,
            "business": bookings.clinic["business"],
            "assistant": bookings.clinic["assistant"],
            "stt": stt.available,
            "language": language,
        }

    @app.post("/api/chat")
    def chat(body: ChatIn):
        entry = sessions.get(body.session_id)
        if entry is None:
            raise HTTPException(404, "Session expired. Reload the page.")
        sessions.move_to_end(body.session_id)
        receptionist, lock = entry
        with lock:  # one turn at a time per call, or the history interleaves
            turn = receptionist.respond(body.text.strip())
        return {"reply": turn.reply, "events": turn.events}

    @app.post("/api/stt")
    async def speech_to_text(audio: UploadFile = File(...)):
        if not stt.available:
            raise HTTPException(503, "faster-whisper is not installed")
        data = await audio.read()
        if not data:
            raise HTTPException(400, "Empty audio")
        if len(data) > 10_000_000:
            raise HTTPException(413, "Recording too long")
        try:
            text = await asyncio.to_thread(stt.transcribe, data)
        except ValueError as err:
            raise HTTPException(400, str(err))
        return {"text": text}

    @app.post("/api/tts")
    async def text_to_speech(body: SpeakIn):
        try:
            audio = await synthesize(body.text, language)
        except Exception as err:  # offline, service hiccup: the page still shows the text
            logging.getLogger("speech").warning("TTS failed: %s", err)
            raise HTTPException(502, "Text-to-speech unavailable")
        return Response(audio, media_type="audio/mpeg")

    @app.get("/api/appointments")
    def appointments():
        services = bookings.clinic["services"]
        return [{**a, "label": services[a["service"]]["label"]} for a in bookings.upcoming()]

    @app.get("/api/messages")
    def messages():
        return bookings.messages()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
