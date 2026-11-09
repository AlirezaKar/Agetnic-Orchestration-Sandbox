"""Environment driven configuration.

Everything that can be swapped at runtime (core model provider, custom provider,
web-search backend) is configured here from the environment / ``.env`` file.

There is intentionally NO mock/offline model: if no provider is configured the
app refuses to start a model call and tells you exactly which env vars are
missing, instead of silently answering with canned text.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, ClassVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# When LLM_PROVIDER=auto, the first provider in this order that has both a key
# and an explicitly configured model is used. ``aras`` is first on purpose:
# when the ARAS router is configured it IS the model, and the public providers
# below become fallbacks.
AUTO_PROVIDER_ORDER: tuple[str, ...] = (
    "aras",
    "groq",
    "gemini",
    "openrouter",
    "mistral",
    "cerebras",
    "nvidia",
    "cloudflare",
    "huggingface",
    "ollama",
    "lmstudio",
    "custom",
)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def trust_env_for(url: str) -> bool:
    """Should an HTTP client honour ``HTTP(S)_PROXY`` for this URL? Not loopback.

    A VPN or system proxy that answers for ``127.0.0.1`` turns every tunnelled
    ARAS call — and local Ollama / LM Studio — into the proxy's own error page:
    the user sees HTTP 503 from seven services at once while the tunnel is
    perfectly fine. Loopback traffic therefore never goes through a proxy;
    public hosts keep ``trust_env=True`` so a proxy still works where it is
    genuinely needed.
    """
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
    except ValueError:
        return True
    return host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def split_csv(value: str | None) -> list[str]:
    """Parse ``"a, b;c"`` into ``["a", "b", "c"]`` (order preserved, deduped)."""
    if not value:
        return []
    parts = [p.strip() for p in value.replace(";", ",").split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    app_name: str = "Agetnic Orchestration Sandbox"
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    # ------------------------------------------------------- core model
    # Which provider is the brain of the backend: "auto" or a provider name.
    # See app/llm/presets.py; "auto" walks AUTO_PROVIDER_ORDER.
    llm_provider: str = "auto"
    # Optional override applied to the *primary* provider only.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    # Ordered failover list, e.g. "gemini,openrouter". Each entry uses its own
    # <PROVIDER>_API_KEY / <PROVIDER>_MODEL variables.
    llm_fallbacks: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0
    # Off by default: the model you wrote in <PROVIDER>_MODEL is the one used.
    # Turn on to substitute a live model when that id has been retired.
    llm_auto_substitute: bool = False
    llm_model_attempts: int = 3

    # ------------------------------------------------- provider API keys
    groq_api_key: str | None = None
    groq_model: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str | None = None

    openrouter_api_key: str | None = None
    openrouter_model: str | None = None

    mistral_api_key: str | None = None
    mistral_model: str | None = None

    cloudflare_api_token: str | None = None
    cloudflare_account_id: str | None = None
    cloudflare_model: str | None = None

    nvidia_api_key: str | None = None
    nvidia_model: str | None = None

    cerebras_api_key: str | None = None
    cerebras_model: str | None = None

    huggingface_api_key: str | None = None
    huggingface_model: str | None = None

    # Local, no key, no cloud account
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str | None = None
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str | None = None

    # Any other OpenAI-compatible endpoint (self hosted vLLM, LiteLLM, a
    # provider I have not presettled, an internal gateway, ...)
    custom_label: str = "custom"
    custom_base_url: str | None = None
    custom_api_key: str | None = None
    custom_model: str | None = None
    # "bearer" (default) | "raw" (key sent as-is) | "none" (no auth header)
    custom_auth_scheme: str = "bearer"
    custom_auth_header: str = "Authorization"
    # JSON objects, merged into headers / request body
    custom_extra_headers: dict[str, Any] = {}
    custom_extra_body: dict[str, Any] = {}

    # ---------------------------------------------------------- search
    # Primary search backend: ddg | wikipedia | tavily | serper | exa |
    # google_cse | searxng
    search_provider: str = "ddg"
    # Optional ordered chain, overrides SEARCH_PROVIDER + SEARCH_FALLBACKS
    search_providers: str = ""
    search_fallbacks: str = "wikipedia"
    search_max_results: int = 5
    search_timeout: float = 20.0
    search_user_agent: str = DEFAULT_UA
    # Drop results whose URL repeats an already-seen host+path
    search_dedupe: bool = True

    duckduckgo_region: str = "wt-wt"
    duckduckgo_safe_search: str = "moderate"
    wikipedia_lang: str = "en"

    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    exa_api_key: str | None = None
    google_cse_api_key: str | None = None
    google_cse_id: str | None = None
    searxng_base_urls: str = ""  # comma separated, e.g. http://localhost:8080
    searxng_api_key: str | None = None
    jina_api_key: str | None = None

    # ------------------------------------------------- ARAS microservices
    # The other app's services (see README §"ARAS microservices"). One host
    # root — service ports are appended — or a full per-service URL override.
    # Tokens are per service; ARAS_TOKEN is the shared fallback for any that
    # are not set. Everything stays off until a URL + token pair exists.
    aras_base_url: str = ""  # e.g. http://127.0.0.1 (through the SSH tunnel)
    aras_token: str | None = None  # fallback for every ARAS_*_TOKEN below
    aras_tenant: str | None = None  # X-Tenant for the data service
    aras_owner: str | None = None  # X-Owner-External-Id for the data service
    aras_timeout: float = 60.0
    # RAG: the collection the document agent retrieves from (and ingestion
    # writes to), e.g. alireza_notes. Empty = retrieval is not offered.
    aras_collection: str = ""
    aras_retrieval_mode: str = "hybrid"  # semantic | keyword | hybrid
    # Also index every accepted upload into ARAS_COLLECTION (off by default).
    aras_auto_ingest: bool = False
    # The analytics source `POST /v1/query/` should answer from (optional —
    # the data agent only uses the service when this is set).
    aras_data_source_id: str | None = None
    # Router model id, e.g. sharif-rayen-gemma4-31b (the router strips the
    # sharif- prefix and sends the request to Rayen).
    aras_model: str | None = None
    # Per-service URL overrides (default: ARAS_BASE_URL + the known port)
    aras_router_url: str | None = None  # :8101 (OpenAI-compatible, /api/v1)
    aras_ocr_url: str | None = None  # :8102
    aras_vector_url: str | None = None  # :8103
    aras_ingestion_url: str | None = None  # :8104
    aras_retrieval_url: str | None = None  # :8105
    aras_data_url: str | None = None  # :8107
    aras_web_url: str | None = None  # :8108
    # Per-service tokens (each falls back to ARAS_TOKEN)
    aras_router_token: str | None = None
    aras_ocr_token: str | None = None
    aras_vector_token: str | None = None
    aras_ingestion_token: str | None = None
    aras_retrieval_token: str | None = None
    aras_data_token: str | None = None
    aras_web_token: str | None = None

    # ----------------------------------------------------------- agent
    agent_max_steps: int = 4
    agent_max_search_results: int = 5
    fetch_max_chars: int = 4000

    # ---------------------------------------------------- orchestration
    # "auto" mode in the UI: a manager plans, specialist agents run in
    # parallel, the manager writes the final answer.
    orchestrator_enabled: bool = True
    # auto      = rules pick the agents, the model writes each agent's brief (+1 call)
    # heuristic = rules only, no planner call at all
    # llm       = the model picks the agents and writes their briefs
    orchestrator_planner: str = "auto"
    # The gate between the agents and the manager: one small call scores every
    # report, lists what is missing and rewrites the weak ones before the manager
    # (and its review) read anything. Rules-only when there is no model.
    orchestrator_evaluate: bool = True
    # Accept the evaluator's rewritten reports. Off = score and flag only, the
    # agents' own wording reaches the manager.
    orchestrator_optimize: bool = True
    # After the evaluated reports arrive, let the manager ask for more work before
    # it writes the final answer. A follow-up wave goes back through the evaluator.
    orchestrator_review: bool = True
    # How many times the manager may ask for another wave in one run.
    orchestrator_review_rounds: int = 1
    # Longest string the browser receives per trace entry; the log file keeps it all.
    orchestrator_trace_chars: int = 4000
    # Keep a JSON record of every run in logs/orchestrator/.
    orchestrator_log_runs: bool = True
    # Empty = every agent may be selected. "search,data" restricts the roster.
    orchestrator_agents: str = ""
    # The manager's closing summary. Off = deterministic merge of agent answers
    # (cheaper/faster, less fluent).
    orchestrator_synthesize: bool = True
    orchestrator_timeout: float = 120.0
    orchestrator_max_parallel: int = 4
    orchestrator_context_chars: int = 6000

    # ---------------------------------------------------- harness memory
    # The conversation itself (not the run log): every user turn and every
    # answer is written to memory/ so a session can be reopened and continued.
    harness_memory: bool = True
    memory_dir: str = "memory"
    # How many earlier turns are handed back to the model as context.
    memory_history_turns: int = 6
    memory_history_chars: int = 3000
    # Oldest turns are dropped from a session file past this many.
    memory_max_turns: int = 200

    # ------------------------------------------------------ attachments
    files_dir: str = ".data/files"
    files_max_mb: float = 10.0
    files_max_chars: int = 200_000
    # How much of a document is handed to an agent.
    files_context_chars: int = 12_000
    # Ask the user (allow / don't allow) before an agent may read a file.
    # Off by default: this is a trusted workspace, so files are allowed at once.
    files_permission_prompt: bool = False

    # ------------------------------------------------------- validators
    @field_validator("custom_extra_headers", "custom_extra_body", mode="before")
    @classmethod
    def _parse_json_object(cls, value: Any) -> Any:
        if value in (None, "", b""):
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:  # pragma: no cover - guard
                raise ValueError(f"expected a JSON object, got {value!r}: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
            return parsed
        return value

    @model_validator(mode="after")
    def _aras_token_fallback(self) -> "Settings":
        """Every ARAS service token falls back to the shared ARAS_TOKEN."""
        for name in ("router", "ocr", "vector", "ingestion", "retrieval", "data", "web"):
            field = f"aras_{name}_token"
            if not getattr(self, field):
                setattr(self, field, self.aras_token)
        return self

    # ------------------------------------------------------- ARAS helpers
    #: service → (default port, path prefix the client expects)
    ARAS_PORTS: ClassVar[dict[str, tuple[int, str]]] = {
        "router": (8101, "/api/v1"),
        "ocr": (8102, ""),
        "vector": (8103, ""),
        "ingestion": (8104, ""),
        "retrieval": (8105, ""),
        "data": (8107, ""),
        "web": (8108, ""),
    }

    def aras_endpoint(self, service: str) -> str:
        """The base URL for one ARAS service ('' when nothing is configured)."""
        override = getattr(self, f"aras_{service}_url", None)
        if override:
            return str(override).rstrip("/")
        root = (self.aras_base_url or "").rstrip("/")
        if not root:
            return ""
        port, prefix = self.ARAS_PORTS[service]
        return f"{root}:{port}{prefix}"

    def aras_configured(self, service: str) -> bool:
        """A service is usable when it has both an endpoint and a token."""
        return bool(self.aras_endpoint(service) and getattr(self, f"aras_{service}_token", None))

    @property
    def aras_services_configured(self) -> list[str]:
        return [name for name in self.ARAS_PORTS if self.aras_configured(name)]

    @property
    def aras_rag_ready(self) -> bool:
        """The document agent may retrieve: retrieval service + a collection."""
        return bool(self.aras_configured("retrieval") and self.aras_collection.strip())

    @property
    def aras_data_ready(self) -> bool:
        """The data agent may query: data service + tenant/owner + a source id."""
        return bool(
            self.aras_configured("data")
            and self.aras_tenant
            and self.aras_owner
            and self.aras_data_source_id
        )

    # ----------------------------------------------------------- helpers
    @property
    def orchestrator_roster(self) -> list[str]:
        """Agents allowed to run, in display order."""
        allowed = split_csv(self.orchestrator_agents)
        known = ["document", "search", "data", "general"]
        if not allowed:
            return known
        return [name for name in known if name in allowed]

    @property
    def is_auto_provider(self) -> bool:
        return (self.llm_provider or "").strip().lower() in ("", "auto")

    @property
    def unused_llm_overrides(self) -> list[str]:
        """LLM_* knobs that only apply to a manually chosen provider."""
        if not self.is_auto_provider:
            return []
        pairs = (
            ("LLM_MODEL", self.llm_model),
            ("LLM_BASE_URL", self.llm_base_url),
            ("LLM_API_KEY", self.llm_api_key),
        )
        return [name for name, value in pairs if value]

    @property
    def llm_chain(self) -> list[str]:
        if self.is_auto_provider:
            chain = [*AUTO_PROVIDER_ORDER, *split_csv(self.llm_fallbacks)]
        else:
            chain = [self.llm_provider, *split_csv(self.llm_fallbacks)]
        return split_csv(",".join(chain))

    @property
    def search_chain(self) -> list[str]:
        if self.search_providers.strip():
            return split_csv(self.search_providers)
        chain = split_csv(",".join([self.search_provider, self.search_fallbacks]))
        # The ARAS web service, when configured, leads the chain: the point of
        # wiring it is that it is used, with the public backends as fallbacks.
        if self.aras_configured("web") and "aras" not in [c.lower() for c in chain]:
            chain = ["aras", *chain]
        return chain

    def public_summary(self) -> dict[str, Any]:
        """Config summary safe to expose over HTTP (no secrets)."""
        return {
            "llm_provider": self.llm_provider,
            "llm_chain": self.llm_chain,
            "search_chain": self.search_chain,
            "llm_model": self.llm_model,
            "temperature": self.llm_temperature,
            "max_tokens": self.llm_max_tokens,
            "search_max_results": self.search_max_results,
            "agent_max_steps": self.agent_max_steps,
            "orchestrator_planner": self.orchestrator_planner,
            "orchestrator_agents": self.orchestrator_roster,
            "orchestrator_evaluate": self.orchestrator_evaluate,
            "orchestrator_optimize": self.orchestrator_optimize,
            "orchestrator_review": self.orchestrator_review,
            "orchestrator_review_rounds": self.orchestrator_review_rounds,
            "harness_memory": self.harness_memory,
            "memory_dir": self.memory_dir,
            "memory_history_turns": self.memory_history_turns,
            "files_permission_prompt": self.files_permission_prompt,
            "files_max_mb": self.files_max_mb,
            "aras_services": self.aras_services_configured,
            "aras_rag_ready": self.aras_rag_ready,
            "aras_data_ready": self.aras_data_ready,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests after monkeypatching the environment."""
    get_settings.cache_clear()
