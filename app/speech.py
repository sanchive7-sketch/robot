from __future__ import annotations

import base64
import io
import logging
from collections.abc import Callable

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf

LOG = logging.getLogger(__name__)
SARVAM_BASE = "https://api.sarvam.ai"


class SpeechError(RuntimeError):
    pass


class SarvamSpeech:
    """Sarvam STT + LLM + TTS adapter.

    Calls stay on the laptop so the API key is never exposed to the phone UI or
    ESP32. Audio interactions run from a worker thread, not the control loop.
    """

    def __init__(
        self,
        api_key: str | None,
        microphone_device: str | None,
        on_activity: Callable[[str], None] | None = None,
    ):
        self.api_key = api_key
        self.microphone_device = microphone_device
        self.on_activity = on_activity or (lambda _: None)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise SpeechError("SARVAM_API_KEY is missing; add it to .env")
        return {"api-subscription-key": self.api_key}

    def speak(self, text: str, language_code: str = "en-IN") -> None:
        LOG.info("Robot says: %s", text)
        self.on_activity(f"Speaking: {text}")
        if not self.configured:
            LOG.warning("Speech skipped because SARVAM_API_KEY is not configured")
            return
        payload = {
            "text": text[:2500],
            "target_language_code": language_code,
            "model": "bulbul:v3",
            "pace": 1.0,
            "speech_sample_rate": 24000,
        }
        with httpx.Client(timeout=45) as client:
            response = client.post(
                f"{SARVAM_BASE}/text-to-speech",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            encoded = response.json()["audios"][0]
        audio_bytes = base64.b64decode(encoded)
        samples, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        sd.play(samples, sample_rate)
        sd.wait()

    def listen(self, seconds: float = 6.0) -> str:
        self.on_activity("Listening…")
        sample_rate = 16000
        frames = int(seconds * sample_rate)
        recording = sd.rec(
            frames,
            samplerate=sample_rate,
            channels=1,
            dtype=np.float32,
            device=self.microphone_device,
        )
        sd.wait()
        memory_file = io.BytesIO()
        sf.write(memory_file, recording, sample_rate, format="WAV", subtype="PCM_16")
        memory_file.seek(0)
        with httpx.Client(timeout=45) as client:
            response = client.post(
                f"{SARVAM_BASE}/speech-to-text",
                headers=self._headers(),
                files={"file": ("visitor.wav", memory_file.getvalue(), "audio/wav")},
                data={"model": "saaras:v3", "mode": "transcribe"},
            )
            response.raise_for_status()
            transcript = str(response.json().get("transcript", "")).strip()
        self.on_activity(f"Heard: {transcript or '(nothing)'}")
        return transcript

    def answer(self, question: str, system_prompt: str) -> str:
        self.on_activity("Thinking…")
        payload = {
            "model": "sarvam-30b",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "temperature": 0.15,
            "max_tokens": 180,
        }
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{SARVAM_BASE}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"]).strip()
