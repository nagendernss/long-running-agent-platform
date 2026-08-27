"""Speech to text, running locally.

`faster-whisper` with the `small.en` model already in the Hugging Face cache. Two
things matter for a live call and shape the code:

* **The model is loaded once, lazily.** It costs ~15s and about a gigabyte, so
  importing the app must not pay for it, and neither should a test that never speaks.
* **Transcription never blocks the event loop.** It is CPU-bound for a second or two,
  which is long enough to stall every other call and every HTTP request if run inline,
  so it goes through `asyncio.to_thread`.

Browsers record WebM/Opus and whisper wants PCM, so anything that is not already a
wav goes through ffmpeg first.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

DEFAULT_MODEL = "small.en"


class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, mime: str = "audio/webm") -> str:
        """Return what was said, or "" for silence or noise."""
        ...


class ScriptedSTT:
    """Test double: returns prepared lines, then silence. Keeps what it was handed so
    a test can still assert that audio actually arrived."""

    def __init__(self, utterances: list[str] | None = None):
        self._utterances = list(utterances or [])
        self.received: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, mime: str = "audio/webm") -> str:
        self.received.append((audio, mime))
        return self._utterances.pop(0) if self._utterances else ""


class WhisperSTT:
    def __init__(self, model_size: str = DEFAULT_MODEL, device: str = "cpu", compute_type: str = "int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # imported here: heavy, and optional

            log.info("loading whisper %s (%s/%s)", self.model_size, self.device, self.compute_type)
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    async def transcribe(self, audio: bytes, mime: str = "audio/webm") -> str:
        if not audio:
            return ""
        return await asyncio.to_thread(self._transcribe_sync, audio, mime)

    # -- runs on a worker thread ----------------------------------------------------
    def _transcribe_sync(self, audio: bytes, mime: str) -> str:
        with tempfile.TemporaryDirectory(prefix="hc_stt_") as tmp:
            source = Path(tmp) / ("input.wav" if "wav" in mime else "input.webm")
            source.write_bytes(audio)
            wav = source if source.suffix == ".wav" else self._to_wav(source)
            if wav is None:
                return ""
            # beam_size=1 trades a little accuracy for latency, which is the right way
            # round when someone is waiting on the line for a reply.
            segments, _info = self._load().transcribe(str(wav), beam_size=1)
            return " ".join(segment.text for segment in segments).strip()

    def _to_wav(self, source: Path) -> Path | None:
        target = source.with_suffix(".wav")
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-ar", "16000", "-ac", "1", str(target)],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0 or not target.exists():
            log.warning("ffmpeg could not decode the utterance: %s", result.stderr.decode()[:200])
            return None
        return target
