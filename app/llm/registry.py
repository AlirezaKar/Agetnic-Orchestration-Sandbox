"""Turn settings into concrete providers, with an ordered failover chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.llm.base import ModelResponse, ProviderError
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.presets import PRESETS, LLMPreset, get_preset


class ConfigurationError(RuntimeError):
    """The selected provider is not usable yet (usually a missing env var)."""

    def __init__(self, message: str, *, missing: list[str] | None = None):
        self.missing = missing or []
        super().__init__(message)


@dataclass
class ResolvedProvider:
    preset: LLMPreset
    base_url: str | None
    api_key: str | None
    model: str | None
    missing: list[str] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    auth_scheme: str = "bearer"
    auth_header: str = "Authorization"

    @property
    def ready(self) -> bool:
        return not self.missing

    @property
    def name(self) -> str:
        return self.preset.name


def _value(settings: Settings, field_name: str | None) -> str | None:
    if not field_name:
        return None
    value = getattr(settings, field_name, None)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value  # type: ignore[return-value]


def resolve_provider(name: str, settings: Settings, *, primary: bool = False) -> ResolvedProvider:
    """Resolve one preset name into endpoint + key + model + what's missing."""
    preset = get_preset(name)
    if preset is None:
        raise ConfigurationError(
            f"Unknown provider '{name}'. Known providers: {', '.join(sorted(PRESETS))}",
            missing=[],
        )

    base_url = preset.base_url
    api_key = _value(settings, preset.key_field)
    model = _value(settings, preset.model_field)
    extra_body: dict[str, Any] = {}
    extra_headers = dict(preset.extra_headers)
    auth_scheme = preset.auth_scheme
    auth_header = preset.auth_header

    if preset.name == "custom":
        base_url = _value(settings, "custom_base_url") or ""
        extra_headers.update(settings.custom_extra_headers or {})
        extra_body = dict(settings.custom_extra_body or {})
        auth_scheme = (settings.custom_auth_scheme or "bearer").strip().lower()
        auth_header = (settings.custom_auth_header or "Authorization").strip() or "Authorization"
    else:
        # ---- local runtimes / templated endpoints
        base_url = base_url.format(
            ollama_base_url=(settings.ollama_base_url or "").rstrip("/"),
            lmstudio_base_url=(settings.lmstudio_base_url or "").rstrip("/"),
            custom_base_url=(settings.custom_base_url or "").rstrip("/"),
            account_id=(settings.cloudflare_account_id or "").strip(),
        )

    # No model configured anywhere → let the provider tell us what it serves.
    if not model:
        model = "auto"

    # ---- optional overrides, primary provider only
    if primary:
        base_url = _value(settings, "llm_base_url") or base_url
        api_key = _value(settings, "llm_api_key") or api_key
        model = _value(settings, "llm_model") or model

    # ---- what is still missing?
    missing: list[str] = []
    for required in preset.extra_required:
        if not _value(settings, required):
            missing.append(required.upper())
    if preset.name == "custom" and not base_url:
        missing.append("CUSTOM_BASE_URL")
    if preset.key_field and preset.name != "custom" and not api_key:
        # A custom gateway may be keyless; every hosted provider needs a key.
        missing.append(preset.key_field.upper())

    return ResolvedProvider(
        preset=preset,
        base_url=base_url or None,
        api_key=api_key,
        model=model,
        missing=missing,
        extra_headers=extra_headers,
        extra_body=extra_body,
        auth_scheme=auth_scheme,
        auth_header=auth_header,
    )


def resolve_named(name: str, settings: Settings) -> ResolvedProvider:
    """Resolve a provider by name, applying LLM_* overrides if it is the primary."""
    primary = (name or "").strip().lower() == (settings.llm_provider or "").strip().lower()
    return resolve_provider(name, settings, primary=primary)


def build_provider(resolved: ResolvedProvider, settings: Settings) -> OpenAICompatProvider:
    if not resolved.ready:
        raise ConfigurationError(
            f"Provider '{resolved.name}' is not configured. Missing env: "
            f"{', '.join(resolved.missing)}. Get a key at {resolved.preset.key_url}",
            missing=resolved.missing,
        )
    return OpenAICompatProvider(
        name=resolved.name,
        label=resolved.preset.label,
        base_url=resolved.base_url or "",
        model=resolved.model or "",
        api_key=resolved.api_key,
        auth_scheme=resolved.auth_scheme,
        auth_header=resolved.auth_header,
        extra_headers=resolved.extra_headers,
        extra_body=resolved.extra_body,
        timeout=settings.llm_timeout,
        key_url=resolved.preset.key_url,
        models_url=resolved.preset.models_url,
        auto_substitute=settings.llm_auto_substitute,
        max_model_attempts=settings.llm_model_attempts,
    )


class ModelChain:
    """Ordered providers: the first one that answers wins."""

    def __init__(self, providers: list[OpenAICompatProvider], skipped: list[str] | None = None):
        self.providers = providers
        self.skipped = skipped or []

    def __bool__(self) -> bool:
        return bool(self.providers)

    @property
    def primary(self) -> OpenAICompatProvider:
        return self.providers[0]

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        if not self.providers:
            raise ConfigurationError(
                "No model provider is configured. Set LLM_PROVIDER plus the matching "
                "API key in .env (see .env.example) — e.g. GROQ_API_KEY, or "
                "LLM_PROVIDER=custom with CUSTOM_BASE_URL/CUSTOM_MODEL."
            )
        errors: list[dict[str, Any]] = []
        for provider in self.providers:
            try:
                return await provider.complete(messages, tools, **kwargs)
            except ProviderError as exc:
                errors.append(exc.to_dict())
                if len(self.providers) == 1:
                    raise
        detail = "; ".join(f"{e['provider']}: {e['message']}" for e in errors)
        raise ProviderError("chain", f"every provider failed ({detail})", body=str(errors))


def build_chain(settings: Settings) -> ModelChain:
    providers: list[OpenAICompatProvider] = []
    skipped: list[str] = []
    for index, name in enumerate(settings.llm_chain):
        try:
            resolved = resolve_provider(name, settings, primary=index == 0)
        except ConfigurationError as exc:
            skipped.append(str(exc))
            continue
        if not resolved.ready:
            skipped.append(f"{name}: missing {', '.join(resolved.missing)}")
            continue
        providers.append(build_provider(resolved, settings))
    return ModelChain(providers, skipped)


def describe_providers(settings: Settings) -> list[dict[str, Any]]:
    """Everything the UI/CLI needs to explain how to switch providers."""
    out: list[dict[str, Any]] = []
    for name, preset in PRESETS.items():
        try:
            resolved = resolve_provider(name, settings)
            configured = resolved.ready
            missing = resolved.missing
        except ConfigurationError:  # pragma: no cover - presets are static
            configured, missing = False, []
        if configured:
            status = "ready"
        elif missing and all(env.endswith("MODEL") for env in missing):
            status = "missing_model"
        elif any(env in ("CUSTOM_BASE_URL", "CLOUDFLARE_ACCOUNT_ID") for env in missing):
            status = "incomplete"
        elif missing:
            status = "missing_key"
        else:
            status = "incomplete"
        out.append(
            {
                "name": name,
                "label": preset.label,
                "configured": configured,
                "status": status,
                "missing_env": missing,
                "model_env": preset.model_field.upper(),
                "example_model": preset.example_model,
                "model_value": (
                    getattr(settings, preset.model_field, None) or "auto"
                ),
                "api_key_env": (preset.key_field or "").upper() or None,
                "base_url": preset.base_url,
                "key_url": preset.key_url,
                "models_url": preset.models_url,
                "docs_url": preset.docs_url,
                "free_tier": preset.free_tier,
                "needs_local_server": preset.needs_local_server,
                "card_required": preset.card_required,
                "notes": preset.notes,
                "is_active": name in settings.llm_chain,
            }
        )
    return out
