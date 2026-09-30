import base64
import threading

import numpy as np

from app.speech import SarvamSpeech


def test_tts_text_removes_internal_and_markdown_formatting() -> None:
    raw = (
        "**HealthAI Pro** uses `agriculture_domain_expert_llm`.\n"
        "* Ask it a farming question."
    )

    spoken = SarvamSpeech._prepare_tts_text(raw)

    assert spoken == (
        "Health AI Pro uses agriculture domain expert llm. "
        "Ask it a farming question."
    )
    assert "_" not in spoken


def test_speak_sends_cleaned_text_to_tts(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **kwargs):
            captured.update(kwargs["json"])
            return _Response()

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr(
        "app.speech.sf.read", lambda *_args, **_kwargs: (np.zeros(10), 24000)
    )
    monkeypatch.setattr("app.speech.sd.play", lambda *_args: None)
    monkeypatch.setattr("app.speech.sd.wait", lambda: None)
    speech = SarvamSpeech("test-key", None)

    speech.speak("HealthAI_pro")

    assert captured["text"] == "Health AI pro"


def test_speak_reports_generation_before_speaking(monkeypatch) -> None:
    events = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            events.append("response")
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            events.append("request")
            return _Response()

    def read_audio(*_args, **_kwargs):
        events.append("decoded")
        return np.zeros(10), 24000

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr("app.speech.sf.read", read_audio)
    monkeypatch.setattr(
        "app.speech.sd.play", lambda *_args: events.append("play")
    )
    monkeypatch.setattr("app.speech.sd.wait", lambda: events.append("wait"))
    speech = SarvamSpeech(
        "test-key", None, lambda message: events.append(f"activity:{message}")
    )

    speech.speak("The answer is ready.")

    assert events == [
        "activity:Generating voice…",
        "request",
        "response",
        "decoded",
        "activity:Speaking: The answer is ready.",
        "play",
        "wait",
    ]


def test_long_answer_is_chunked_with_one_client_and_cached(monkeypatch) -> None:
    client_count = 0
    requested_texts = []
    played = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            nonlocal client_count
            client_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **kwargs):
            requested_texts.append(kwargs["json"]["text"])
            return _Response()

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr(
        "app.speech.sf.read", lambda *_args, **_kwargs: (np.zeros(10), 24000)
    )
    monkeypatch.setattr(
        "app.speech.sd.play", lambda *_args: played.append("play")
    )
    monkeypatch.setattr("app.speech.sd.wait", lambda: None)
    speech = SarvamSpeech("test-key", None)
    answer = (
        "This is the first explanation for the visitor. " * 5
        + "This is the second explanation for the visitor. " * 5
    ).strip()

    speech.speak(answer)
    first_request_count = len(requested_texts)
    speech.speak(answer)

    assert first_request_count > 1
    assert len(requested_texts) == first_request_count
    assert client_count == 1
    assert len(played) == first_request_count * 2
    assert all(len(chunk) <= SarvamSpeech.TTS_CHUNK_CHARS for chunk in requested_texts)


def test_next_chunk_is_generated_while_current_chunk_plays(monkeypatch) -> None:
    second_request_started = threading.Event()
    request_count = 0
    wait_count = 0

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            nonlocal request_count
            request_count += 1
            if request_count == 2:
                second_request_started.set()
            return _Response()

    def wait():
        nonlocal wait_count
        wait_count += 1
        if wait_count == 1:
            assert second_request_started.wait(timeout=2)

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr(
        "app.speech.sf.read", lambda *_args, **_kwargs: (np.zeros(10), 24000)
    )
    monkeypatch.setattr("app.speech.sd.play", lambda *_args: None)
    monkeypatch.setattr("app.speech.sd.wait", wait)
    speech = SarvamSpeech("test-key", None)
    answer = "First sentence for the visitor. " * 12

    speech.speak(answer)

    assert request_count > 1
    assert wait_count == request_count


def test_concurrent_speech_does_not_overlap_playback(monkeypatch) -> None:
    first_playing = threading.Event()
    release_first = threading.Event()
    play_calls = []
    wait_calls = 0

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            return _Response()

    def play(*_args):
        play_calls.append(threading.current_thread().name)
        if len(play_calls) == 1:
            first_playing.set()

    def wait():
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert release_first.wait(timeout=2)

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr(
        "app.speech.sf.read", lambda *_args, **_kwargs: (np.zeros(10), 24000)
    )
    monkeypatch.setattr("app.speech.sd.play", play)
    monkeypatch.setattr("app.speech.sd.wait", wait)
    speech = SarvamSpeech("test-key", None)
    first = threading.Thread(target=speech.speak, args=("First answer.",), name="first")
    second = threading.Thread(target=speech.speak, args=("Second answer.",), name="second")

    first.start()
    assert first_playing.wait(timeout=2)
    second.start()
    assert play_calls == ["first"]
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert play_calls == ["first", "second"]


def test_cancel_stops_playback_and_prevents_remaining_chunks(monkeypatch) -> None:
    playback_started = threading.Event()
    playback_released = threading.Event()
    stop_called = threading.Event()
    play_calls = []
    result = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"audios": [base64.b64encode(b"audio").decode("ascii")]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            return _Response()

    def play(*_args):
        play_calls.append("play")
        playback_started.set()

    def wait():
        assert playback_released.wait(timeout=2)

    def stop():
        stop_called.set()
        playback_released.set()

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr(
        "app.speech.sf.read", lambda *_args, **_kwargs: (np.zeros(10), 24000)
    )
    monkeypatch.setattr("app.speech.sd.play", play)
    monkeypatch.setattr("app.speech.sd.wait", wait)
    monkeypatch.setattr("app.speech.sd.stop", stop)
    speech = SarvamSpeech("test-key", None)
    answer = "A sufficiently long visitor answer. " * 20
    worker = threading.Thread(target=lambda: result.append(speech.speak(answer)))

    worker.start()
    assert playback_started.wait(timeout=2)
    speech.cancel()
    worker.join(timeout=2)

    assert stop_called.is_set()
    assert not worker.is_alive()
    assert result == [False]
    assert play_calls == ["play"]


def test_cancel_stops_microphone_before_transcription_request(monkeypatch) -> None:
    recording_started = threading.Event()
    recording_released = threading.Event()
    client_count = 0
    result = []

    class _Client:
        def __init__(self, **_kwargs):
            nonlocal client_count
            client_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def record(frames, **_kwargs):
        recording_started.set()
        return np.zeros((frames, 1), dtype=np.float32)

    def wait():
        assert recording_released.wait(timeout=2)

    monkeypatch.setattr("app.speech.httpx.Client", _Client)
    monkeypatch.setattr("app.speech.sd.rec", record)
    monkeypatch.setattr("app.speech.sd.wait", wait)
    monkeypatch.setattr("app.speech.sd.stop", recording_released.set)
    speech = SarvamSpeech("test-key", None)
    worker = threading.Thread(target=lambda: result.append(speech.listen()))

    worker.start()
    assert recording_started.wait(timeout=2)
    speech.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [""]
    assert client_count == 0


def test_named_microphone_chooses_compatible_default_among_windows_duplicates(monkeypatch) -> None:
    devices = [
        {"name": "Microphone (DroidCam Audio)", "max_input_channels": 2},
        {"name": "Microphone (DroidCam Audio)", "max_input_channels": 2},
        {"name": "Microphone (DroidCam Audio)", "max_input_channels": 1},
    ]
    checked = []

    def check_input_settings(*, device, samplerate, channels):
        checked.append(device)
        assert samplerate == 16000
        assert channels == 1
        if device == 2:
            raise ValueError("Invalid sample rate")

    monkeypatch.setattr("app.speech.sd.query_devices", lambda: devices)
    monkeypatch.setattr("app.speech.sd.check_input_settings", check_input_settings)
    monkeypatch.setattr("app.speech.sd.default.device", [2, 3])

    assert SarvamSpeech("test-key", "DroidCam Audio")._input_device(16000) == 0
    assert checked == [2, 0]
