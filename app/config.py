"""Environment driven configuration.

The app has exactly one set of backends: the ARAS microservices. Everything
that can be configured at runtime — where they live, which tokens open them,
which model id the router is asked for, how the orchestrator behaves — is read
here from the environment / ``.env`` file.

There is intentionally NO mock/offline model and NO fallback provider: if the
ARAS router is not configured the app refuses to start a model call and says
exactly which env vars are missing, instead of silently answering with canned
text or quietly switching to something else.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, ClassVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def trust_env_for(url: str) -> bool:
    """Should an HTTP client honour ``HTTP(S)_PROXY`` for this URL? Not loopback.

    A VPN or system proxy that answers for ``127.0.0.1`` turns every tunnelled
    ARAS call into the proxy's own error page: the user sees HTTP 503 from
    seven services at once while the tunnel is perfectly fine. Loopback
    traffic therefore never goes through a proxy; any other host keeps
    ``trust_env=True`` so a proxy still works where it is genuinely needed.
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

    # ------------------------------------------------- ARAS microservices
    # The only backends this app talks to (see README §"ARAS microservices").
    # One host root — service ports are appended — or a full per-service URL
    # override. Tokens are per service; ARAS_TOKEN is the shared fallback for
    # any that are not set. A service is off until a URL + token pair exists.
    aras_base_url: str = ""  # e.g. http://127.0.0.1 (through the SSH tunnel)
    aras_token: str | None = None  # fallback for every ARAS_*_TOKEN below
    aras_tenant: str | None = None  # X-Tenant for the data service
    aras_owner: str | None = None  # X-Owner-External-Id for the data service
    aras_timeout: float = 60.0
    # The model: the id the router is asked for, e.g. sharif-rayen-gemma4-31b
    # (the router strips the sharif- prefix and sends the request to Rayen).
    # Required — the model you write is the model that is called.
    aras_model: str | None = None
    # RAG: the collection the document agent retrieves from (and ingestion
    # writes to), e.g. alireza_notes. Empty = retrieval is not offered.
    aras_collection: str = ""
    aras_retrieval_mode: str = "hybrid"  # semantic | keyword | hybrid
    # Also index every accepted upload into ARAS_COLLECTION (off by default).
    aras_auto_ingest: bool = False
    # The analytics source `POST /v1/query/` should answer from (optional —
    # the data agent only uses the service when this is set).
    aras_data_source_id: str | None = None
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

    # ------------------------------------------------------- model calls
    # Sent with every router call. The names are the orchestrator's contract
    # (``contracts.OrchestratorSettings``), so they are not ARAS-prefixed.
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024

    # ----------------------------------------------------------- agent
    # The tool loop the search agent (and POST /chat) runs: model <-> web_search.
    agent_max_steps: int = 4
    agent_max_search_results: int = 5

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
    @model_validator(mode="after")
    def _aras_token_fallback(self) -> "Settings":
        """Every ARAS service token falls back to the shared ARAS_TOKEN."""
        for name in self.ARAS_PORTS:
            field = f"aras_{name}_token"
            if not getattr(self, field):
                setattr(self, field, self.aras_token)
        return self

    # ------------------------------------------------------- ARAS helpers
    #: service → (default port, path prefix the client expects). Port 8106
    #: (the remote orchestrator) is deliberately absent: it is never called.
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
        """A service is reachable when it has both an endpoint and a token."""
        return bool(self.aras_endpoint(service) and getattr(self, f"aras_{service}_token", None))

    @property
    def aras_services_configured(self) -> list[str]:
        return [name for name in self.ARAS_PORTS if self.aras_configured(name)]

    @property
    def aras_model_id(self) -> str:
        """The router model id, or '' when it was not written.

        ``auto`` counts as not written: nothing here guesses a model. The id
        in ``ARAS_MODEL`` is the one that is called, and a missing one is
        reported instead of silently picking something from the router's list.
        """
        value = (self.aras_model or "").strip()
        return "" if value.lower() == "auto" else value

    def aras_missing(self, service: str) -> list[str]:
        """Env vars ``service`` still needs before it can do its job ([] = ready).

        "Its job" is the role the service has in this app, so the list goes
        past URL + token: the router also needs the model id, retrieval and
        ingestion need the collection, the data service needs its identity
        headers and a source to query.
        """
        missing: list[str] = []
        if not self.aras_endpoint(service):
            missing.append("ARAS_BASE_URL")
        if not getattr(self, f"aras_{service}_token", None):
            missing.append(f"ARAS_{service.upper()}_TOKEN")
        if service == "router" and not self.aras_model_id:
            missing.append("ARAS_MODEL")
        if service in ("retrieval", "ingestion") and not self.aras_collection.strip():
            missing.append("ARAS_COLLECTION")
        if service == "data":
            for env, value in (
                ("ARAS_TENANT", self.aras_tenant),
                ("ARAS_OWNER", self.aras_owner),
                ("ARAS_DATA_SOURCE_ID", self.aras_data_source_id),
            ):
                if not value:
                    missing.append(env)
        return missing

    @property
    def aras_model_ready(self) -> bool:
        """The model can be called: router URL + token + a model id."""
        return not self.aras_missing("router")

    @property
    def aras_search_ready(self) -> bool:
        """Web search can be called: web URL + token."""
        return not self.aras_missing("web")

    @property
    def aras_rag_ready(self) -> bool:
        """The document agent may retrieve: retrieval service + a collection."""
        return not self.aras_missing("retrieval")

    @property
    def aras_data_ready(self) -> bool:
        """The data agent may query: data service + tenant/owner + a source id."""
        return not self.aras_missing("data")

    # ----------------------------------------------------------- helpers
    @property
    def orchestrator_roster(self) -> list[str]:
        """Agents allowed to run, in display order."""
        allowed = split_csv(self.orchestrator_agents)
        known = ["document", "search", "data", "general"]
        if not allowed:
            return known
        return [name for name in known if name in allowed]

    def public_summary(self) -> dict[str, Any]:
        """Config summary safe to expose over HTTP (no secrets)."""
        return {
            "model_provider": "aras",
            "model": self.aras_model_id,
            "model_ready": self.aras_model_ready,
            "search_provider": "aras",
            "search_ready": self.aras_search_ready,
            "temperature": self.llm_temperature,
            "max_tokens": self.llm_max_tokens,
            "agent_max_steps": self.agent_max_steps,
            "agent_max_search_results": self.agent_max_search_results,
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
            "aras_auto_ingest": self.aras_auto_ingest,
            "aras_timeout": self.aras_timeout,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests after monkeypatching the environment."""
    get_settings.cache_clear()
