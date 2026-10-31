"""Pick a usable model id from a provider's ``/models`` listing.

Model ids retire constantly (``gemini-2.5-flash`` → 404 "no longer available to
new users", OpenRouter free models lose all endpoints → 404/400 "No endpoints
found"). Hard-coding a default guarantees breakage, so the backend can ask the
provider what it actually serves and rank that list.

Ranking is provider aware: Gemini prefers the ``gemini-flash-latest`` alias and
then the newest numbered Flash model; OpenRouter only ever gets ``:free`` ids;
Groq prefers the current GPT-OSS/Llama models; local runtimes take whatever they
serve.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Sequence

# Things that are not chat models (embeddings, speech, image generation, ...)
_NON_CHAT = (
    "embed",
    "whisper",
    "tts",
    "rerank",
    "moderation",
    "guard",
    "ocr",
    "transcribe",
    "stable-diffusion",
    "flux",
    "sdxl",
    "dall-e",
    "bge-",
    "clip",
    "audio",
    "-live",
    "image",
)

_GEMINI_NUMERIC = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-(flash|pro)(?:-(lite))?$")

# Ordered substring preferences per provider (earlier = better).
_PREFERRED = {
    "groq": ["gpt-oss-120b", "gpt-oss-20b", "llama-3.3-70b", "llama-3.1-8b-instant", "qwen", "llama"],
    "openrouter": ["gemma-4", "llama-3.3", "qwen", "nemotron", "mistral", "gemma", "llama", "deepseek"],
    "mistral": [
        "mistral-medium-latest",
        "mistral-small-latest",
        "mistral-large-latest",
        "ministral",
        "codestral-latest",
        "open-mistral",
    ],
    "nvidia": ["llama-3.3-70b", "llama-4", "qwen", "mistral", "deepseek"],
    "huggingface": ["Llama-3.1-8B-Instruct", "Llama-3.3-70B-Instruct", "Qwen", "Mistral", "gemma"],
    "cloudflare": [
        "@cf/openai/gpt-oss-120b",
        "@cf/meta/llama-3.3-70b",
        "@cf/meta/llama-3.1-8b-instruct",
        "@cf/qwen",
        "@cf/mistral",
    ],
    "cerebras": ["llama-3.3-70b", "gpt-oss", "qwen", "llama"],
    "ollama": ["llama3", "qwen", "mistral", "gemma"],
    "lmstudio": ["instruct", "llama", "qwen", "mistral", "gemma"],
    "gemini": [],
    "custom": [],
}

DEFAULT_ATTEMPTS = 3


def _clean(model: str) -> str:
    return model.split("/", 1)[1] if model.startswith("models/") else model


def is_chat_model(model: str) -> bool:
    lowered = model.lower()
    if any(marker in lowered for marker in _NON_CHAT):
        # "instruct" chat models can still contain e.g. "vision" — only the
        # explicit non-chat markers above disqualify a model.
        return False
    return True


def _gemini_key(model: str) -> tuple:
    name = _clean(model)
    # Google's rolling alias is the safest pick: it survives version renames.
    if name == "gemini-flash-latest":
        return (0, 0, 0, 0)
    match = _GEMINI_NUMERIC.match(name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        kind = match.group(3)
        lite = bool(match.group(4))
        if kind == "flash":
            # newest full Flash first, then Lite (weaker at tool calling), then Pro
            return ((1 if not lite else 2), -major, -minor, 0)
        return (4, -major, -minor, 0)
    if name == "gemini-flash-lite-latest":
        return (2, 0, 0, 1)
    if "flash" in name:
        return (3, 0, 0, 0)
    if "pro" in name:
        return (5, 0, 0, 0)
    return (6, 0, 0, 0)


def _pref_key(provider: str, model: str) -> tuple:
    lowered = model.lower()
    preferences = _PREFERRED.get(provider, [])
    for index, token in enumerate(preferences):
        if token.lower() in lowered:
            return (0, index, model)
    return (1, len(preferences), model)


def rank_models(
    provider: str,
    models: Iterable[str],
    *,
    require_tools: bool = False,
    tool_capable: set[str] | None = None,
) -> list[str]:
    """Return the best chat model ids first, filtered to what can actually chat."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in models:
        model = (raw or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        if not is_chat_model(model):
            continue
        if provider == "openrouter" and not model.endswith(":free"):
            continue
        if require_tools and tool_capable is not None:
            # Only drop models we positively know cannot call tools.
            if tool_capable and model not in tool_capable:
                continue
        cleaned.append(model)

    if not cleaned:
        return []

    if provider == "gemini":
        return sorted(cleaned, key=_gemini_key)
    return sorted(cleaned, key=lambda model: _pref_key(provider, model))


def choose_model(provider: str, models: Iterable[str], **kwargs: Any) -> str | None:
    ranked = rank_models(provider, models, **kwargs)
    return ranked[0] if ranked else None


def suggest_models(configured: str, available: Sequence[str], limit: int = 3) -> list[str]:
    """'Did you mean' suggestions for a model id that the provider rejected."""
    if not configured:
        return list(available[:limit])
    lowered = [model.lower() for model in available]

    # 1. same family, different version, e.g. gemini-2.5-flash -> gemini-3.8-flash
    family = re.sub(r"[\d.]+", "", configured.lower())
    same_family = [
        model
        for model, low in zip(available, lowered)
        if family and re.sub(r"[\d.]+", "", low) == family
    ]
    if same_family:
        return same_family[:limit]

    # 2. close string match
    close = difflib.get_close_matches(configured, available, n=limit, cutoff=0.5)
    if close:
        return close

    # 3. same vendor prefix (openrouter style)
    prefix = configured.split("/", 1)[0].lower()
    same_prefix = [model for model, low in zip(available, lowered) if low.startswith(prefix + "/")]
    if same_prefix:
        return same_prefix[:limit]

    return list(available[:limit])


def looks_like_model_error(status: int | None, body: str) -> bool:
    """True when the provider is complaining about the model, not the request shape."""
    if status not in (400, 404, 422):
        return False
    lowered = (body or "").lower()
    # A wrong URL path also produces 404 — that is not a model problem.
    if ("route" in lowered or "path" in lowered) and "model" not in lowered:
        return False

    strong_markers = (
        "no endpoints",
        "no longer available",
        "model_not_found",
        "decommissioned",
        "retired",
        "unknown model",
        "invalid model",
        "does not exist",
        "support tool use",
        "does not support tool",
    )
    if any(marker in lowered for marker in strong_markers):
        return True
    if "not found" in lowered and ("model" in lowered or "models/" in lowered):
        return True
    if "not available" in lowered and "model" in lowered:
        return True
    if "not supported" in lowered and ("model" in lowered or "generatecontent" in lowered):
        return True
    return False
