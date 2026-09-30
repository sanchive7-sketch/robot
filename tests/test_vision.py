import cv2
import numpy as np

from app.vision import VisionService


class _Result:
    boxes = None


class _PersonModel:
    def __init__(self):
        self.kwargs = None

    def predict(self, _frame, **kwargs):
        self.kwargs = kwargs
        return [_Result()]


class _Boxes:
    def __init__(self, values):
        self.xyxy = self
        self.values = values

    def cpu(self):
        return self

    def numpy(self):
        return self.values

    def __len__(self):
        return len(self.values)


class _GroupResult:
    def __init__(self):
        self.boxes = _Boxes(
            np.asarray(
                [
                    [10, 10, 110, 300],
                    [180, 20, 500, 430],
                    [520, 30, 620, 260],
                ],
                dtype=np.float32,
            )
        )


class _GroupPersonModel:
    def predict(self, _frame, **_kwargs):
        return [_GroupResult()]


def test_deep_inference_uses_selected_compute_device() -> None:
    vision = VisionService(
        camera_source="0",
        camera_width=640,
        camera_height=480,
        camera_fps=10,
    )
    person_model = _PersonModel()
    vision._person_model = person_model
    vision._person_device = 0

    assert vision._process_deep(np.zeros((480, 640, 3), dtype=np.uint8)) is None
    assert person_model.kwargs is not None
    assert person_model.kwargs["device"] == 0


def test_deep_inference_reports_all_detected_people() -> None:
    vision = VisionService(
        camera_source="0",
        camera_width=640,
        camera_height=480,
        camera_fps=10,
    )
    vision._person_model = _GroupPersonModel()

    visitor = vision._process_deep(np.zeros((480, 640, 3), dtype=np.uint8))

    assert visitor is not None
    assert visitor.person_count == 3


def test_camera_uses_configured_windows_backend(monkeypatch) -> None:
    captured = {}

    class _Camera:
        def set(self, *_args):
            return True

    def fake_video_capture(source, backend):
        captured["source"] = source
        captured["backend"] = backend
        return _Camera()

    monkeypatch.setattr("app.vision.cv2.VideoCapture", fake_video_capture)
    vision = VisionService(
        camera_source="2",
        camera_backend="msmf",
        camera_width=640,
        camera_height=480,
        camera_fps=10,
    )

    vision._open_camera()

    assert captured == {"source": 2, "backend": cv2.CAP_MSMF}
