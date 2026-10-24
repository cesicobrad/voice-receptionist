"""Speech-to-text (faster-whisper, local) and text-to-speech (edge-tts)."""
from __future__ import annotations

import io
import logging
import os
import threading

log = logging.getLogger("speech")

# Per-language defaults. WHISPER_MODEL / TTS_VOICE in .env override them.
WHISPER_MODELS = {"en": "small.en", "sl": "large-v3-turbo"}  # *.en models are English-only
TTS_VOICES = {"en": "en-GB-SoniaNeural", "sl": "sl-SI-PetraNeural"}
STT_PROMPTS = {
    "en": "A phone call to a dental clinic receptionist about appointments.",
    "sl": "Telefonski klic v zobozdravstveno ordinacijo zaradi termina.",
}


class Transcriber:
    """Loads the Whisper model on first use. GPU if available, else CPU int8."""

    def __init__(self, model_size: str | None = None, device: str | None = None, language: str = "en"):
        self.language = language
        self.model_size = model_size or os.getenv("WHISPER_MODEL") or WHISPER_MODELS[language]
        self.device = device or (os.getenv("WHISPER_DEVICE") or "auto")
        self._model = None
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        from faster_whisper import WhisperModel

        attempts = [("cuda", "float16"), ("cpu", "int8")] if self.device == "auto" else \
            [(self.device, "float16" if self.device == "cuda" else "int8")]
        for device, compute in attempts:
            try:
                model = WhisperModel(self.model_size, device=device, compute_type=compute)
                # Touch the backend once so missing CUDA libraries fail here, not mid-request.
                list(model.transcribe(io.BytesIO(_silence_wav()), language=self.language)[0])
                log.info("whisper %s on %s/%s", self.model_size, device, compute)
                self.backend = f"{self.model_size} on {device}"
                return model
            except Exception as err:  # CUDA/cuDNN missing, out of memory, ...
                log.warning("whisper on %s failed: %s", device, err)
        raise RuntimeError("Could not load faster-whisper on any device")

    def transcribe(self, audio: bytes) -> str:
        with self._lock:
            if self._model is None:
                self._model = self._load()
            try:
                from faster_whisper.audio import decode_audio
                samples = decode_audio(io.BytesIO(audio))
            except Exception as err:  # PyAV raises its own error types for corrupt input
                raise ValueError(f"Could not decode audio: {err}") from err
            segments, _ = self._model.transcribe(
                samples, language=self.language, beam_size=1, vad_filter=True,
                initial_prompt=STT_PROMPTS[self.language],
            )
            return " ".join(s.text.strip() for s in segments).strip()


def _silence_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def tts_voice(language: str) -> str:
    return os.getenv("TTS_VOICE") or TTS_VOICES[language]


async def synthesize(text: str, language: str = "en", voice: str | None = None) -> bytes:
    """MP3 bytes for `text` using Microsoft Edge's online neural voices."""
    import edge_tts

    voice = voice or tts_voice(language)
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate="+5%").stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)
