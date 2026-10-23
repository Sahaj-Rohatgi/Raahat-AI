"""Local speech-to-text, vocal-tone signals, and optional text-to-speech.

Voice notes are transcribed on this server with faster-whisper when it is installed, so the
audio never has to leave the machine; chatbot.transcribe_audio (Gemini) is only the fallback.

The "acoustic" signals are simple loudness/pause/tremor heuristics, not a validated clinical
measure. They are returned next to the transcript as context for the care team and never
feed the risk score — urgency stays the text safety router's job.

Every heavy dependency (faster-whisper, numpy, soundfile, chatterbox) is imported lazily and
optional: without them each function degrades to "unavailable" instead of failing import.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import wave

from .. import config

log = logging.getLogger(__name__)

EXTENSIONS = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".mp4"}

_whisper_model = None
_whisper_failed = False
_tts_model = None
_tts_failed = False
_load_lock = threading.Lock()


def _patch_av_open() -> None:
    # Newer PyAV releases dropped the `metadata_errors` keyword that some faster-whisper
    # versions still pass to av.open; strip it rather than pin an old PyAV.
    try:
        import av
        original = av.open
        if getattr(original, "_raahat_patched", False):
            return

        def safe_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return original(*args, **kwargs)

        safe_open._raahat_patched = True
        av.open = safe_open
    except Exception:
        pass


def _whisper():
    """The faster-whisper model, loaded once on first use (CPU, int8). None if unavailable."""
    global _whisper_model, _whisper_failed
    if _whisper_model is not None or _whisper_failed or not config.LOCAL_STT_ENABLED:
        return _whisper_model
    with _load_lock:
        if _whisper_model is None and not _whisper_failed:
            try:
                _patch_av_open()
                from faster_whisper import WhisperModel
                log.info("loading faster-whisper model", extra={"model": config.WHISPER_MODEL})
                _whisper_model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
            except Exception as exc:
                # Remembered so every later voice note does not retry a load that cannot work.
                _whisper_failed = True
                log.warning("local speech-to-text unavailable; Gemini fallback only", extra={"error": type(exc).__name__})
    return _whisper_model


def acoustic_signals(audio_path: str) -> dict:
    """Loudness, pause and tremor heuristics from one recording.

    Returns an empty dict when the audio cannot be decoded. `indicators` are plain-language
    observations; `estimated_stress_score` is a 0-100 sum of the heuristics that fired.
    """
    try:
        import numpy as np
        try:
            from faster_whisper.audio import decode_audio
            data, rate = decode_audio(audio_path, sampling_rate=16000), 16000
        except Exception:
            import soundfile as sf
            data, rate = sf.read(audio_path, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
        if len(data) == 0:
            return {}

        rms = float(np.sqrt(np.mean(data ** 2)))
        silence_threshold = max(0.01, float(np.max(np.abs(data))) * 0.15)
        silence_ratio = float(np.mean(np.abs(data) < silence_threshold))
        frame = int(rate * 0.03)
        if len(data) > frame:
            zero_crossings = float(np.mean(np.abs(np.diff(np.sign(data)))) / 2)
            energy_variance = float(np.var([np.sum(data[i:i + frame] ** 2) for i in range(0, len(data) - frame, frame)]))
        else:
            zero_crossings = energy_variance = 0.0

        indicators, score = [], 0.0
        if silence_ratio > 0.45:
            indicators.append("Long pauses / hesitation"); score += 25
        elif silence_ratio > 0.30:
            indicators.append("Noticeable pauses"); score += 12
        if energy_variance > 0.05:
            indicators.append("Unsteady volume (possible tremor)"); score += 25
        if rms < 0.015:
            indicators.append("Very quiet voice"); score += 20
        elif rms > 0.18:
            indicators.append("Raised voice"); score += 20
        if zero_crossings > 0.18:
            indicators.append("Tense, high-frequency voice"); score += 20

        return {
            "rms_energy": round(rms, 4),
            "silence_ratio": round(silence_ratio, 2),
            "pitch_variability": "elevated" if score >= 40 else "moderate" if score >= 20 else "normal",
            "estimated_stress_score": min(100.0, round(score, 1)),
            "indicators": indicators,
        }
    except Exception as exc:
        log.debug("acoustic analysis skipped", extra={"error": type(exc).__name__})
        return {}


def transcribe_local(audio: bytes, mime_type: str) -> dict:
    """Transcribe a voice note on this server. Always returns a dict:
    {"transcript": str | None, "language": str | None, "acoustic": dict}."""
    with tempfile.NamedTemporaryFile(suffix=EXTENSIONS.get(mime_type, ".webm"), delete=False) as handle:
        handle.write(audio)
        path = handle.name
    try:
        acoustic = acoustic_signals(path)
        model = _whisper()
        if model is None:
            return {"transcript": None, "language": None, "acoustic": acoustic}
        try:
            segments, info = model.transcribe(path, beam_size=5)
            transcript = " ".join(segment.text for segment in segments).strip()
        except Exception as exc:
            log.warning("local transcription failed", extra={"error": type(exc).__name__})
            return {"transcript": None, "language": None, "acoustic": acoustic}
        return {"transcript": transcript[:4000] or None, "language": info.language, "acoustic": acoustic}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _tts():
    """Chatterbox TTS, loaded once on first use. None if the package is not installed."""
    global _tts_model, _tts_failed
    if _tts_model is not None or _tts_failed:
        return _tts_model
    with _load_lock:
        if _tts_model is None and not _tts_failed:
            try:
                from chatterbox.tts import ChatterboxTTS
                _tts_model = ChatterboxTTS.from_pretrained(device="cpu")
            except Exception as exc:
                _tts_failed = True
                log.info("server text-to-speech unavailable; browser speech is used", extra={"error": type(exc).__name__})
    return _tts_model


def synthesize_speech(text: str) -> bytes:
    """WAV bytes for `text`, or b"" when no server TTS engine is installed — the frontend
    then speaks with the browser's own voices. (Returning a silent clip instead would look
    like success and play nothing.)"""
    clean = text.strip()[:1000]
    model = _tts() if clean else None
    if model is None:
        return b""
    try:
        import numpy as np
        samples = model.generate(clean).squeeze().cpu().numpy()
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(model.sr)
            wav.writeframes(pcm)
        return buffer.getvalue()
    except Exception as exc:
        log.warning("text-to-speech failed", extra={"error": type(exc).__name__})
        return b""
