from __future__ import annotations

import base64
import io
import logging
import re
import textwrap
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

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

    TTS_MAX_TEXT_CHARS = 2500
    TTS_CHUNK_CHARS = 220
    TTS_CACHE_ENTRIES = 32

    def __init__(
        self,
        api_key: str | None,
        microphone_device: str | None,
        on_activity: Callable[[str], None] | None = None,
    ):
        self.api_key = api_key
        self.microphone_device = microphone_device
        self.on_activity = on_activity or (lambda _: None)
        self._speak_lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self._cancel_generation = 0
        self._cache_lock = threading.Lock()
        self._audio_cache: OrderedDict[
            tuple[str, str], tuple[np.ndarray, int]
        ] = OrderedDict()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise SpeechError("SARVAM_API_KEY is missing; add it to .env")
        return {"api-subscription-key": self.api_key}

    def cancel(self) -> None:
        """Cancel current capture/playback without cancelling future requests."""
        with self._cancel_lock:
            self._cancel_generation += 1
        try:
            sd.stop()
        except (ValueError, sd.PortAudioError):
            LOG.exception("Could not stop the active audio stream")

    def _cancellation_token(self) -> int:
        with self._cancel_lock:
            return self._cancel_generation

    def _is_cancelled(
        self,
        generation: int,
        cancel_event: threading.Event | None,
    ) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        with self._cancel_lock:
            return generation != self._cancel_generation

    def _report_activity(
        self,
        message: str,
        generation: int,
        cancel_event: threading.Event | None,
    ) -> bool:
        # Synchronizing the callback with cancel() prevents a stale "Speaking"
        # update from replacing the dashboard's final STOPPED state.
        with self._cancel_lock:
            if generation != self._cancel_generation:
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            self.on_activity(message)
            return True

    def speak(
        self,
        text: str,
        language_code: str = "en-IN",
        cancel_event: threading.Event | None = None,
    ) -> bool:
        generation = self._cancellation_token()
        spoken_text = self._prepare_tts_text(text)[: self.TTS_MAX_TEXT_CHARS]
        LOG.info("Robot says: %s", spoken_text)
        if not self.configured:
            LOG.warning("Speech skipped because SARVAM_API_KEY is not configured")
            return not self._is_cancelled(generation, cancel_event)
        if not spoken_text:
            return not self._is_cancelled(generation, cancel_event)
        if self._is_cancelled(generation, cancel_event):
            return False

        chunks = self._split_tts_chunks(spoken_text)
        if not self._report_activity(
            "Generating voice…", generation, cancel_event
        ):
            return False
        audio = [self._get_cached_audio(chunk, language_code) for chunk in chunks]
        needs_client = any(item is None for item in audio)
        client_context = httpx.Client(timeout=45) if needs_client else nullcontext(None)
        with client_context as client:
            def audio_for(index: int) -> tuple[np.ndarray, int]:
                cached = audio[index]
                if cached is not None:
                    return cached
                if client is None:
                    raise SpeechError("TTS client unavailable for uncached speech")
                generated = self._synthesize_chunk(
                    client, chunks[index], language_code
                )
                audio[index] = generated
                return generated

            current = audio_for(0)
            if self._is_cancelled(generation, cancel_event):
                return False

            if len(chunks) == 1:
                with self._speak_lock:
                    if self._is_cancelled(generation, cancel_event):
                        return False
                    if not self._report_activity(
                        f"Speaking: {spoken_text}", generation, cancel_event
                    ):
                        return False
                    sd.play(*current)
                    if self._is_cancelled(generation, cancel_event):
                        sd.stop()
                        return False
                    sd.wait()
                return not self._is_cancelled(generation, cancel_event)

            # Generate the next sentence group while the current group plays.
            # A single worker preserves request order and avoids API bursts.
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tts-prefetch"
            ) as executor:
                index = 0
                announced = False
                while True:
                    if self._is_cancelled(generation, cancel_event):
                        return False
                    next_audio = (
                        executor.submit(audio_for, index + 1)
                        if index + 1 < len(chunks)
                        else None
                    )
                    with self._speak_lock:
                        if self._is_cancelled(generation, cancel_event):
                            if next_audio is not None:
                                next_audio.cancel()
                            return False
                        if not announced:
                            if not self._report_activity(
                                f"Speaking: {spoken_text}",
                                generation,
                                cancel_event,
                            ):
                                return False
                            announced = True
                        sd.play(*current)
                        if self._is_cancelled(generation, cancel_event):
                            sd.stop()
                            return False
                        sd.wait()
                    if self._is_cancelled(generation, cancel_event):
                        if next_audio is not None:
                            next_audio.cancel()
                        return False
                    if next_audio is None:
                        break
                    current = next_audio.result()
                    index += 1
            return True

    def _synthesize_chunk(
        self,
        client: httpx.Client,
        text: str,
        language_code: str,
    ) -> tuple[np.ndarray, int]:
        payload = {
            "text": text,
            "target_language_code": language_code,
            "model": "bulbul:v3",
            "pace": 1.0,
            "speech_sample_rate": 24000,
        }
        response = client.post(
            f"{SARVAM_BASE}/text-to-speech",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        encoded = response.json()["audios"][0]
        audio_bytes = base64.b64decode(encoded)
        samples, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        generated = (samples, sample_rate)
        self._cache_audio(text, language_code, generated)
        return generated

    def _get_cached_audio(
        self, text: str, language_code: str
    ) -> tuple[np.ndarray, int] | None:
        key = (language_code, text)
        with self._cache_lock:
            cached = self._audio_cache.get(key)
            if cached is not None:
                self._audio_cache.move_to_end(key)
            return cached

    def _cache_audio(
        self,
        text: str,
        language_code: str,
        audio: tuple[np.ndarray, int],
    ) -> None:
        key = (language_code, text)
        with self._cache_lock:
            self._audio_cache[key] = audio
            self._audio_cache.move_to_end(key)
            while len(self._audio_cache) > self.TTS_CACHE_ENTRIES:
                self._audio_cache.popitem(last=False)

    @classmethod
    def _split_tts_chunks(cls, text: str) -> list[str]:
        """Create short, ordered chunks without cutting normal sentences."""
        if len(text) <= cls.TTS_CHUNK_CHARS:
            return [text]

        units: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if not sentence:
                continue
            units.extend(
                textwrap.wrap(
                    sentence,
                    width=cls.TTS_CHUNK_CHARS,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
            )

        chunks: list[str] = []
        current = ""
        for unit in units:
            candidate = f"{current} {unit}".strip()
            if current and len(candidate) > cls.TTS_CHUNK_CHARS:
                chunks.append(current)
                current = unit
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text]

    @staticmethod
    def _prepare_tts_text(text: str) -> str:
        """Convert display/LLM formatting into natural text for speech."""
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = text.replace("**", "").replace("__", "")
        text = re.sub(r"(?m)^\s*(?:[-*•]+|\d+[.)])\s+", "", text)
        text = re.sub(r"_+", " ", text)
        # Mixed-case brands such as HealthAI and SkillNova are otherwise often
        # pronounced one character at a time by TTS engines.
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def listen(
        self,
        seconds: float = 6.0,
        cancel_event: threading.Event | None = None,
    ) -> str:
        generation = self._cancellation_token()
        if not self._report_activity("Listening…", generation, cancel_event):
            return ""
        sample_rate = 16000
        frames = int(seconds * sample_rate)
        device = self._input_device(sample_rate)
        try:
            recording = sd.rec(
                frames,
                samplerate=sample_rate,
                channels=1,
                dtype=np.float32,
                device=device,
            )
            if self._is_cancelled(generation, cancel_event):
                sd.stop()
                return ""
            sd.wait()
        except (ValueError, sd.PortAudioError) as exc:
            if self._is_cancelled(generation, cancel_event):
                return ""
            raise SpeechError(f"Microphone capture failed: {exc}") from exc
        if self._is_cancelled(generation, cancel_event):
            return ""
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
        if self._is_cancelled(generation, cancel_event):
            return ""
        if not self._report_activity(
            f"Heard: {transcript or '(nothing)'}", generation, cancel_event
        ):
            return ""
        return transcript

    def _input_device(self, sample_rate: int) -> int | None:
        """Resolve a name across Windows audio APIs and verify the capture rate."""
        configured = self.microphone_device
        if not configured:
            return None
        devices = sd.query_devices()
        if configured.strip().isdigit():
            candidates = [int(configured.strip())]
        else:
            name = configured.strip().casefold()
            candidates = [
                index for index, device in enumerate(devices)
                if device["max_input_channels"] > 0 and name in device["name"].casefold()
            ]
        if not candidates:
            raise SpeechError(f"Microphone {configured!r} was not found. Check MICROPHONE_DEVICE in .env.")

        default_input = sd.default.device[0]
        if default_input in candidates:
            candidates.remove(default_input)
            candidates.insert(0, default_input)
        errors = []
        for index in candidates:
            if index < 0 or index >= len(devices) or devices[index]["max_input_channels"] < 1:
                continue
            try:
                sd.check_input_settings(device=index, samplerate=sample_rate, channels=1)
            except (ValueError, sd.PortAudioError) as exc:
                errors.append(f"{index}: {exc}")
                continue
            LOG.info("Using microphone %s: %s", index, devices[index]["name"])
            return index
        detail = "; ".join(errors) or "no matching input device"
        raise SpeechError(f"Microphone {configured!r} cannot record at {sample_rate} Hz ({detail}).")

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
