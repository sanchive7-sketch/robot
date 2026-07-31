from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx

from .speech import SarvamSpeech

LOG = logging.getLogger(__name__)


class HybridLlm:
    """Local Ollama Llama with an automatic Sarvam cloud fallback."""

    VALID_PROVIDERS = {"local_first", "local_only", "sarvam"}

    def __init__(
        self,
        provider: str,
        ollama_base_url: str,
        ollama_model: str,
        local_timeout_seconds: float,
        context_tokens: int,
        max_output_tokens: int,
        keep_alive: str,
        sarvam: SarvamSpeech,
        on_activity: Callable[[str], None] | None = None,
    ):
        if provider not in self.VALID_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(self.VALID_PROVIDERS)}"
            )
        self.provider = provider
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self.ollama_model = ollama_model
        self.local_timeout_seconds = local_timeout_seconds
        self.context_tokens = context_tokens
        self.max_output_tokens = max_output_tokens
        self.keep_alive = keep_alive
        self.sarvam = sarvam
        self.on_activity = on_activity or (lambda _: None)

        self.local_ready = False
        self.last_provider = "none"
        self.last_duration_seconds = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._warmup_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.provider == "sarvam":
            return
        self._warmup_thread = threading.Thread(
            target=self._warmup,
            name="ollama-warmup",
            daemon=True,
        )
        self._warmup_thread.start()

    def close(self) -> None:
        if self.provider == "sarvam" or not self.local_ready:
            return
        # Release GPU memory when the robot controller shuts down.
        try:
            with httpx.Client(timeout=3) as client:
                client.post(
                    f"{self.ollama_base_url}/api/generate",
                    json={"model": self.ollama_model, "keep_alive": 0},
                )
        except Exception:
            LOG.debug("Could not unload Ollama model", exc_info=True)

    def answer(self, question: str, system_prompt: str) -> str:
        if self.provider == "sarvam":
            return self._answer_sarvam(question, system_prompt)
        try:
            return self._answer_local(question, system_prompt)
        except Exception as exc:
            with self._lock:
                self.local_ready = False
                self.last_error = f"Local Llama failed: {exc}"
            LOG.warning("%s", self.last_error)
            if self.provider == "local_only":
                raise
            self.on_activity("Local Llama unavailable; using Sarvam…")
            return self._answer_sarvam(question, system_prompt)

    def status(self) -> dict:
        with self._lock:
            return {
                "configured_provider": self.provider,
                "local_model": self.ollama_model,
                "local_ready": self.local_ready,
                "last_provider": self.last_provider,
                "last_duration_seconds": round(self.last_duration_seconds, 2),
                "last_error": self.last_error,
            }

    def _warmup(self) -> None:
        self.on_activity(f"Loading local {self.ollama_model}…")
        try:
            # Empty generation loads the model and keeps it resident without
            # creating visitor-facing content.
            with httpx.Client(timeout=90) as client:
                response = client.post(
                    f"{self.ollama_base_url}/api/generate",
                    json={
                        "model": self.ollama_model,
                        "prompt": "",
                        "stream": False,
                        "keep_alive": self.keep_alive,
                    },
                )
                response.raise_for_status()
            with self._lock:
                self.local_ready = True
                self.last_error = ""
            self.on_activity(f"Local {self.ollama_model} ready.")
            LOG.info("Ollama model %s is warm", self.ollama_model)
        except Exception as exc:
            message = (
                f"Local model is not ready: {exc}. Install Ollama and run "
                f"'ollama pull {self.ollama_model}'."
            )
            with self._lock:
                self.local_ready = False
                self.last_error = message
            LOG.warning(message)
            self.on_activity("Local Llama unavailable; Sarvam fallback remains active.")

    def _answer_local(self, question: str, system_prompt: str) -> str:
        self.on_activity("Thinking locally…")
        started = time.monotonic()
        with httpx.Client(timeout=self.local_timeout_seconds) as client:
            response = client.post(
                f"{self.ollama_base_url}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": question},
                    ],
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": self.context_tokens,
                        "num_predict": self.max_output_tokens,
                    },
                },
            )
            response.raise_for_status()
            answer = str(response.json()["message"]["content"]).strip()
        if not answer:
            raise RuntimeError("Ollama returned an empty answer")
        duration = time.monotonic() - started
        with self._lock:
            self.local_ready = True
            self.last_provider = "local_llama"
            self.last_duration_seconds = duration
            self.last_error = ""
        LOG.info("Local Llama answered in %.2fs", duration)
        return answer
    def _answer_sarvam(self, question: str, system_prompt: str) -> str:
        self.on_activity("Thinking with Sarvam…")
        started = time.monotonic()
        answer = self.sarvam.answer(question, system_prompt)
        duration = time.monotonic() - started
        with self._lock:
            self.last_provider = "sarvam"
            self.last_duration_seconds = duration
        return answer
