"""Speech to text, locally.

The default suite never loads a model or touches a microphone: `ScriptedSTT` stands
in wherever a session needs to hear something. The live test proves the real thing
works against the model already in the HF cache.
"""
from __future__ import annotations

import subprocess

import pytest

from app.voice.stt import ScriptedSTT, WhisperSTT


async def test_scripted_stt_returns_utterances_in_order():
    stt = ScriptedSTT(["hello", "we need an authorization"])
    assert await stt.transcribe(b"", "audio/webm") == "hello"
    assert await stt.transcribe(b"", "audio/webm") == "we need an authorization"
    assert await stt.transcribe(b"", "audio/webm") == "", "nothing left reads as silence"


async def test_scripted_stt_records_what_it_was_given():
    """Sessions pass real bytes through even when the transcript is scripted, so a
    test can still assert audio actually arrived."""
    stt = ScriptedSTT(["hi"])
    await stt.transcribe(b"\x00\x01", "audio/webm")
    assert stt.received == [(b"\x00\x01", "audio/webm")]


def test_whisper_is_not_loaded_until_it_is_needed():
    """Importing the app must not cost 15 seconds and a gigabyte of RAM."""
    stt = WhisperSTT()
    assert stt._model is None


@pytest.mark.live
async def test_whisper_transcribes_real_speech(tmp_path):
    """SAPI speaks a sentence, whisper reads it back - the same round trip the voice
    stack does, minus the browser."""
    wav = tmp_path / "spoken.wav"
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f'$s.SetOutputToWaveFile("{wav}"); '
        '$s.Speak("The records were faxed this morning."); $s.Dispose()'
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True, timeout=180)

    text = await WhisperSTT().transcribe(wav.read_bytes(), "audio/wav")
    assert "faxed" in text.lower(), f"got {text!r}"
