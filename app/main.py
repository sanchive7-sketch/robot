from __future__ import annotations

import logging
import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .controller import RobotController
from .event_store import EventStore
from .llm import HybridLlm
from .models import RobotMode
from .serial_link import SerialLink
from .speech import SarvamSpeech
from .vision import VisionService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

event_store = EventStore(settings.event_file, settings.project_catalog_file)
vision = VisionService(
    camera_source=settings.camera_source,
    camera_width=settings.camera_width,
    camera_height=settings.camera_height,
    camera_fps=settings.camera_fps,
    vip_file=settings.vip_file,
    vip_dir=settings.project_root / "data" / "vips",
    backend=settings.vision_backend,
    insightface_model=settings.insightface_model,
    vip_similarity_threshold=settings.vip_similarity_threshold,
)
controller: RobotController


def _telemetry_callback(telemetry) -> None:
    controller.update_telemetry(telemetry)


link = SerialLink(
    settings.serial_port,
    settings.serial_baud,
    settings.simulation,
    _telemetry_callback,
)
speech = SarvamSpeech(
    settings.sarvam_api_key,
    settings.microphone_device,
    lambda message: controller.set_activity(message),
)
llm = HybridLlm(
    provider=settings.llm_provider,
    ollama_base_url=settings.ollama_base_url,
    ollama_model=settings.ollama_model,
    local_timeout_seconds=settings.ollama_local_timeout_seconds,
    context_tokens=settings.ollama_context_tokens,
    max_output_tokens=settings.llm_max_output_tokens,
    keep_alive=settings.ollama_keep_alive,
    sarvam=speech,
    on_activity=lambda message: controller.set_activity(message),
)
controller = RobotController(link, vision, speech, llm, event_store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    llm.start()
    controller.start()
    yield
    controller.close()
    llm.close()


app = FastAPI(title="College Welcome Robot", version="0.1.0", lifespan=lifespan)


class ModeRequest(BaseModel):
    mode: str
    pin: str = Field(min_length=1, max_length=32)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    pin: str = Field(min_length=1, max_length=32)


class PinRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=32)


def _verify_pin(pin: str) -> None:
    if not hmac.compare_digest(pin, settings.remote_control_pin):
        raise HTTPException(403, "Incorrect control PIN")


@app.get("/")
def remote_page() -> FileResponse:
    # The remote panel changes while the robot is being configured. Never let a
    # phone or browser keep an old copy that sends commands using stale logic.
    return FileResponse(
        settings.project_root / "app" / "static" / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/status")
def status() -> dict:
    return controller.snapshot()


@app.get("/api/camera/stream")
def camera_stream() -> StreamingResponse:
    """Serve the single DroidCam capture to all dashboard viewers as MJPEG."""

    def frames():
        while True:
            frame = vision.get_latest_jpeg()
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
            time.sleep(1 / 12)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/mode")
def set_mode(request: ModeRequest) -> dict:
    _verify_pin(request.pin)
    allowed = {
        "welcome": RobotMode.WELCOME,
        "patrol": RobotMode.PATROL,
        "stop": RobotMode.STOPPED,
    }
    selected = allowed.get(request.mode.lower())
    if selected is None:
        raise HTTPException(400, "mode must be welcome, patrol, or stop")
    try:
        controller.set_mode(selected)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return controller.snapshot()


@app.post("/api/reload-event")
def reload_event(request: PinRequest) -> dict:
    _verify_pin(request.pin)
    event_store.reload()
    return {
        "ok": True,
        "event": event_store.event_name(),
        "projects": event_store.project_count(),
        "validation_warnings": event_store.validation_warnings,
    }


@app.post("/api/question")
def ask_question(request: QuestionRequest) -> dict:
    _verify_pin(request.pin)
    try:
        return {"answer": controller.ask_text(request.question)}
    except Exception as exc:
        logging.exception("Question failed")
        raise HTTPException(502, f"Sarvam request failed: {exc}") from exc
