"""
tests.test_config — settings loading & secret redaction.
"""

from backend.config import Settings, load_settings, settings


def test_settings_defaults():
    assert settings.max_parallel_tasks >= 1
    assert settings.max_plan_tasks >= 1
    assert settings.task_timeout_seconds > 0
    # With LLM_PROVIDER=mock forced by conftest:
    assert settings.effective_llm_provider == "mock"


def test_effective_provider_auto_uses_key(monkeypatch):
    s = Settings(llm_provider="auto", groq_api_key="secret")
    assert s.effective_llm_provider == "groq"
    s2 = Settings(llm_provider="auto", groq_api_key="")
    assert s2.effective_llm_provider == "mock"


def test_redacted_never_leaks_secrets():
    s = Settings(groq_api_key="sk-secret", tavily_api_key="t-secret", serper_api_key="s-secret")
    red = s.redacted()
    dumped = str(red)
    assert "sk-secret" not in dumped
    assert "t-secret" not in dumped
    assert "s-secret" not in dumped
    assert red["groq_api_key_set"] is True


def test_load_settings_creates_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("DOCUMENT_DIR", str(tmp_path / "docs"))
    s = load_settings()
    assert s.trace_dir.exists()
    assert s.document_dir.exists()
