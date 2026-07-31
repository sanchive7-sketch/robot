from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from .models import Visitor

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class VipRecord:
    id: str
    name: str
    greeting: str
    photos: tuple[str, ...]


class VisionService:
    """DroidCam-aware visitor detection and VIP recognition.

    The preferred backend combines YOLO26n person detection with InsightFace
    SCRFD/ArcFace. If those models cannot load, the service falls back to the
    original Haar/LBPH prototype and reports the fallback in ``last_error``.
    """

    def __init__(
        self,
        camera_source: str,
        camera_width: int,
        camera_height: int,
        camera_fps: int,
        vip_file: Path,
        vip_dir: Path,
        backend: str = "deep",
        insightface_model: str = "buffalo_s",
        vip_similarity_threshold: float = 0.50,
    ):
        self.camera_source = camera_source
        self.camera_width = camera_width
        self.camera_height = camera_height
        self.camera_fps = camera_fps
        self.vip_file = vip_file
        self.vip_dir = vip_dir
        self.requested_backend = backend
        self.active_backend = "not_loaded"
        self.insightface_model = insightface_model
        self.vip_similarity_threshold = vip_similarity_threshold
        self.latest_visitor: Visitor | None = None
        self.camera_ok = False
        self.model_ok = False
        self.last_error = ""

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._person_model = None
        self._face_app = None
        self._vip_embeddings: dict[str, tuple[VipRecord, np.ndarray]] = {}
        self._lbph_recognizer = None
        self._lbph_vips: dict[int, VipRecord] = {}
        self._frame_number = 0
        self._latest_jpeg: bytes | None = None
        self._vip_candidate_id: str | None = None
        self._vip_candidate_frames = 0

        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._haar_detector = cv2.CascadeClassifier(cascade_path)
        self._vip_records = self._read_vip_records()

    def _read_vip_records(self) -> list[VipRecord]:
        loaded = yaml.safe_load(self.vip_file.read_text(encoding="utf-8")) or {}
        return [
            VipRecord(
                id=str(raw["id"]),
                name=str(raw["name"]),
                greeting=str(raw["greeting"]),
                photos=tuple(str(item) for item in raw.get("photos", [])),
            )
            for raw in loaded.get("vips", [])
        ]

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
        if self.requested_backend == "lbph":
            self._load_lbph()
            return
        if self.requested_backend != "deep":
            raise ValueError("VISION_BACKEND must be 'deep' or 'lbph'")
        try:
            # Lazy imports keep simulation/configuration usable before the larger
            # deep-learning dependencies have been installed.
            import onnxruntime as ort
            from insightface.app import FaceAnalysis
            from ultralytics import YOLO

            LOG.info("Loading YOLO26n person detector")
            self._person_model = YOLO("yolo26n.pt")
            available = ort.get_available_providers()
            providers = (
                ["CUDAExecutionProvider", "CPUExecutionProvider"]
                if "CUDAExecutionProvider" in available
                else ["CPUExecutionProvider"]
            )
            ctx_id = 0 if "CUDAExecutionProvider" in providers else -1
            LOG.info("Loading InsightFace %s with %s", self.insightface_model, providers)
            self._face_app = FaceAnalysis(
                name=self.insightface_model,
                providers=providers,
            )
            self._face_app.prepare(ctx_id=ctx_id, det_size=(640, 640))
            self._load_vip_embeddings()
            self.active_backend = "yolo26n+insightface"
            self.model_ok = True
        except Exception as exc:
            LOG.exception("Deep vision failed; using LBPH fallback")
            self.last_error = f"Deep vision unavailable; LBPH fallback: {exc}"
            self._load_lbph()

    def _load_vip_embeddings(self) -> None:
        if self._face_app is None:
            return
        for record in self._vip_records:
            embeddings: list[np.ndarray] = []
            for filename in record.photos:
                image_path = self.vip_dir / filename
                image = cv2.imread(str(image_path))
                if image is None:
                    LOG.warning("VIP photo not found: %s", image_path)
                    continue
                faces = self._face_app.get(image)
                if len(faces) != 1:
                    LOG.warning("VIP photo %s must contain exactly one clear face", filename)
                    continue
                embedding = np.asarray(faces[0].normed_embedding, dtype=np.float32)
                embeddings.append(embedding)
            if embeddings:
                mean = np.mean(embeddings, axis=0)
                norm = np.linalg.norm(mean)
                if norm > 0:
                    self._vip_embeddings[record.id] = (
                        record,
                        (mean / norm).astype(np.float32),
                    )
        LOG.info("Loaded deep VIP galleries for %d people", len(self._vip_embeddings))

    def _load_lbph(self) -> None:
        images: list[np.ndarray] = []
        labels: list[int] = []
        for label, record in enumerate(self._vip_records):
            for filename in record.photos:
                image_path = self.vip_dir / filename
                image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    LOG.warning("VIP photo not found: %s", image_path)
                    continue
                faces = self._haar_detector.detectMultiScale(
                    image, 1.1, 5, minSize=(60, 60)
                )
                if len(faces) != 1:
                    continue
                x, y, w, h = faces[0]
                images.append(cv2.resize(image[y : y + h, x : x + w], (160, 160)))
                labels.append(label)
                self._lbph_vips[label] = record
        if images:
            self._lbph_recognizer = cv2.face.LBPHFaceRecognizer_create()
            self._lbph_recognizer.train(
                images, np.asarray(labels, dtype=np.int32)
            )
        self.active_backend = "lbph"
        self.model_ok = True

    def _open_camera(self) -> cv2.VideoCapture:
        source = self.camera_source.strip()
        if source.lstrip("-").isdigit():
            camera = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
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
        if self.active_backend == "yolo26n+insightface":
            visitor = self._process_deep(frame)
        else:
            visitor = self._process_lbph(frame)
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
            verbose=False,
        )[0]
        person_boxes = (
            result.boxes.xyxy.cpu().numpy()
            if result.boxes is not None and len(result.boxes) > 0
            else np.empty((0, 4))
        )
        faces = self._face_app.get(frame)

        if len(person_boxes) > 0:
            x1, y1, x2, y2 = max(
                person_boxes,
                key=lambda box: (box[2] - box[0]) * (box[3] - box[1]),
            )
            center_x = float((x1 + x2) * 0.5 / width)
            center_y = float((y1 + y2) * 0.5 / height)
            proximity = float(min(1.0, (y2 - y1) / height))
            matching_faces = [
                face
                for face in faces
                if x1 <= (face.bbox[0] + face.bbox[2]) * 0.5 <= x2
                and y1 <= (face.bbox[1] + face.bbox[3]) * 0.5 <= y2
            ]
        elif faces:
            # A very close visitor may fill the frame so YOLO cannot see a full
            # person. A face remains sufficient for a stationary greeting.
            face = max(
                faces,
                key=lambda item: (item.bbox[2] - item.bbox[0])
                * (item.bbox[3] - item.bbox[1]),
            )
            x1, y1, x2, y2 = face.bbox
            center_x = float((x1 + x2) * 0.5 / width)
            center_y = float((y1 + y2) * 0.5 / height)
            proximity = float(min(1.0, 3.0 * (y2 - y1) / height))
            matching_faces = [face]
        else:
            return None

        vip, identity_pending = self._match_vip(matching_faces)
        return Visitor(
            center_x=center_x,
            center_y=center_y,
            proximity_fraction=proximity,
            vip_id=vip.id if vip else None,
            vip_name=vip.name if vip else None,
            vip_greeting=vip.greeting if vip else None,
            identity_pending=identity_pending,
        )

    def _match_vip(self, faces) -> tuple[VipRecord | None, bool]:
        best_score = -1.0
        best_record: VipRecord | None = None
        for face in faces:
            probe = np.asarray(face.normed_embedding, dtype=np.float32)
            for record, reference in self._vip_embeddings.values():
                score = float(np.dot(probe, reference))
                if score > best_score:
                    best_score = score
                    best_record = record
        if best_score < self.vip_similarity_threshold or best_record is None:
            self._vip_candidate_id = None
            self._vip_candidate_frames = 0
            return None, False
        if best_record.id == self._vip_candidate_id:
            self._vip_candidate_frames += 1
        else:
            self._vip_candidate_id = best_record.id
            self._vip_candidate_frames = 1
        LOG.info(
            "VIP candidate %s similarity %.3f (%d/3)",
            best_record.name,
            best_score,
            self._vip_candidate_frames,
        )
        if self._vip_candidate_frames >= 3:
            return best_record, False
        return None, True

    def _process_lbph(self, frame: np.ndarray) -> Visitor | None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._haar_detector.detectMultiScale(
            gray, scaleFactor=1.12, minNeighbors=6, minSize=(70, 70)
        )
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
        vip = None
        if self._lbph_recognizer is not None:
            crop = cv2.resize(gray[y : y + h, x : x + w], (160, 160))
            label, distance = self._lbph_recognizer.predict(crop)
            if distance < 52:
                vip = self._lbph_vips.get(label)
        height, width = gray.shape
        return Visitor(
            center_x=(x + w / 2) / width,
            center_y=(y + h / 2) / height,
            proximity_fraction=min(1.0, 3.0 * h / height),
            vip_id=vip.id if vip else None,
            vip_name=vip.name if vip else None,
            vip_greeting=vip.greeting if vip else None,
        )
