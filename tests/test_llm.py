from app.llm import HybridLlm


class FakeSarvam:
    def __init__(self):
        self.calls = 0

    def answer(self, question, system_prompt):
        self.calls += 1
        return "Sarvam fallback"


def make_llm(provider: str = "local_first") -> HybridLlm:
    return HybridLlm(
        provider=provider,
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="llama3.2:3b-instruct-q4_K_M",
        local_timeout_seconds=1,
        context_tokens=8192,
        max_output_tokens=180,
        keep_alive="30m",
        sarvam=FakeSarvam(),
    )


def test_local_failure_uses_sarvam_fallback(monkeypatch) -> None:
    llm = make_llm()

    def fail_local(question, system_prompt):
        raise RuntimeError("Ollama offline")

    monkeypatch.setattr(llm, "_answer_local", fail_local)

    assert llm.answer("Where is the exhibit?", "grounded prompt") == "Sarvam fallback"
    assert llm.sarvam.calls == 1
    assert llm.status()["last_provider"] == "sarvam"


def test_local_only_does_not_silently_use_cloud(monkeypatch) -> None:
    llm = make_llm("local_only")

    def fail_local(question, system_prompt):
        raise RuntimeError("Ollama offline")

    monkeypatch.setattr(llm, "_answer_local", fail_local)

    try:
        llm.answer("Question", "Prompt")
    except RuntimeError as exc:
        assert "Ollama offline" in str(exc)
    else:
        raise AssertionError("local_only must not call Sarvam")
    assert llm.sarvam.calls == 0
