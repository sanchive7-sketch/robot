from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

from .models import Visitor

LOG = logging.getLogger(__name__)


class VisionService:
    """DroidCam-aware visitor detection for equal welcoming.

    The preferred backend uses YOLO26n person detection. If that model cannot
    load, the service falls back to Haar face detection and reports the fallback
    in ``last_error``. No identity or biometric matching is performed.
    """

    def __init__(
        self,
        camera_source: str,
        camera_width: int,
        camera_height: int,
        camera_fps: int,
        camera_backend: str = "dshow",
        backend: str = "deep",
    ):
        self.camera_source = camera_source
        if camera_backend not in {"dshow", "msmf"}:
            raise ValueError("CAMERA_BACKEND must be 'dshow' or 'msmf'")
        self.camera_backend = camera_backend
        self.camera_width = camera_width
        self.camera_height = camera_height
        self.camera_fps = camera_fps
        self.requested_backend = backend
        self.active_backend = "not_loaded"
        self.latest_visitor: Visitor | None = None
        self.camera_ok = False
        self.model_ok = False
        self.last_error = ""

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._person_model = None
        self._person_device: int | str = "cpu"
        self._frame_number = 0
        self._latest_jpeg: bytes | None = None

        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._haar_detector = cv2.CascadeClassifier(cascade_path)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_visitor(self) -> Visitor | None:
        with self._lock:
            return self.latest_visitor

    def get_latest_jpeg(self) -> bytes | None:
        """Return the most recently processed camera image for the dashboard."""
        with self._lock:
            return self._latest_jpeg

    def _load_models(self) -> None:
        if self.requested_backend in {"haar", "lbph"}:
            self._load_haar()
            return
        if self.requested_backend != "deep":
            raise ValueError("VISION_BACKEND must be 'deep' or 'haar'")
        try:
            # Lazy imports keep simulation/configuration usable before the larger
            # deep-learning dependencies have been installed.
            import torch
            from ultralytics import YOLO

            LOG.info("Loading YOLO26n person detector")
            self._person_model = YOLO("yolo26n.pt")
            self._person_device = 0 if torch.cuda.is_available() else "cpu"
            LOG.info("Loading YOLO on %s", self._person_device)
            self.active_backend = "yolo26n"
            self.model_ok = True
        except Exception as exc:
            LOG.exception("Deep vision failed; using Haar fallback")
            self.last_error = f"Deep vision unavailable; Haar fallback: {exc}"
            self._load_haar()

    def _load_haar(self) -> None:
        self.active_backend = "haar"
        self.model_ok = True

    def _open_camera(self) -> cv2.VideoCapture:
        source = self.camera_source.strip()
        if source.lstrip("-").isdigit():
            capture_backend = (
                cv2.CAP_MSMF if self.camera_backend == "msmf" else cv2.CAP_DSHOW
            )
            camera = cv2.VideoCapture(int(source), capture_backend)
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.camera_width)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.camera_height)
            camera.set(cv2.CAP_PROP_FPS, self.camera_fps)
        else:
            # DroidCam direct stream example:
            # http://192.168.1.20:4747/video/1280x720
            camera = cv2.VideoCapture(source)
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return camera

    def _run(self) -> None:
        try:
            self._load_models()
        except Exception as exc:
            self.last_error = f"Vision model initialization failed: {exc}"
            LOG.exception(self.last_error)
            return

        while not self._stop.is_set():
            camera = self._open_camera()
            if not camera.isOpened():
                self.camera_ok = False
                self.last_error = f"Could not open camera source {self.camera_source!r}"
                LOG.error(self.last_error)
                camera.release()
                self._stop.wait(2.0)
                continue

            self.camera_ok = True
            LOG.info(
                "DroidCam source %r opened at requested %dx%d/%d FPS",
                self.camera_source,
                self.camera_width,
                self.camera_height,
                self.camera_fps,
            )
            try:
                while not self._stop.is_set():
                    ok, frame = camera.read()
                    if not ok:
                        self.last_error = "DroidCam frame lost; reconnecting"
                        LOG.warning(self.last_error)
                        break
                    # Publish the camera preview immediately. Face/person
                    # recognition can be slow on CPU, but it must never delay
                    # the operator's ability to see the DroidCam feed.
                    self._publish_preview(frame)
                    self._frame_number += 1
                    # Processing alternate frames prevents a CPU-only laptop from
                    # accumulating seconds of stale video.
                    if self._frame_number % 2 == 0:
                        self._process_frame(frame)
            finally:
                camera.release()
                self.camera_ok = False
            self._stop.wait(1.0)

    def _process_frame(self, frame: np.ndarray) -> None:
        if self.active_backend == "yolo26n":
            visitor = self._process_deep(frame)
        else:
            visitor = self._process_haar(frame)
        with self._lock:
            self.latest_visitor = visitor

    def _publish_preview(self, frame: np.ndarray) -> None:
        """Cache a compact preview independently of the AI processing speed."""
        encoded_ok, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
        )
        if not encoded_ok:
            return
        with self._lock:
            self._latest_jpeg = encoded.tobytes()

    def _process_deep(self, frame: np.ndarray) -> Visitor | None:
        height, width = frame.shape[:2]
        result = self._person_model.predict(
            frame,
            classes=[0],
            conf=0.45,
            imgsz=640,
            device=self._person_device,
            verbose=False,
        )[0]
        person_boxes = (
            result.boxes.xyxy.cpu().numpy()
            if result.boxes is not None and len(result.boxes) > 0
            else np.empty((0, 4))
        )
        if len(person_boxes) == 0:
            return None

        x1, y1, x2, y2 = max(
            person_boxes,
            key=lambda box: (box[2] - box[0]) * (box[3] - box[1]),
        )
        return Visitor(
            center_x=float((x1 + x2) * 0.5 / width),
            center_y=float((y1 + y2) * 0.5 / height),
            proximity_fraction=float(min(1.0, (y2 - y1) / height)),
            person_count=int(len(person_boxes)),
        )

    def _process_haar(self, frame: np.ndarray) -> Visitor | None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._haar_detector.detectMultiScale(
            gray, scaleFactor=1.12, minNeighbors=6, minSize=(70, 70)
        )
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
        height, width = gray.shape
        return Visitor(
            center_x=(x + w / 2) / width,
            center_y=(y + h / 2) / height,
            proximity_fraction=min(1.0, 3.0 * h / height),
            person_count=len(faces),
        )
