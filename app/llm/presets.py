"""Catalogue of free, OpenAI-compatible model providers.

Every preset below is a *free* way to get a real model behind the backend:
free permanent tier, free monthly credits, or a local runtime that needs no
account at all. Paid-only vendors are deliberately not listed.

The links are the two things you actually need:

* ``key_url``    – where to create the API key
* ``models_url`` – where to see the model IDs that are currently available

Note that model IDs change often. Always copy the exact ID from ``models_url``
(or call ``GET /providers/<name>/models`` / ``python -m scripts.check_providers
--models``) instead of trusting a hard-coded default.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMPreset:
    name: str
    label: str
    # ``{account_id}`` style placeholders are filled from settings
    base_url: str
    key_field: str | None
    model_field: str
    default_model: str | None
    key_url: str
    models_url: str
    free_tier: str
    docs_url: str
    # extra required env vars, e.g. Cloudflare's account id
    extra_required: tuple[str, ...] = ()
    needs_local_server: bool = False
    card_required: bool = False
    auth_scheme: str = "bearer"
    auth_header: str = "Authorization"
    extra_headers: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def is_keyless(self) -> bool:
        return self.key_field is None


PRESETS: dict[str, LLMPreset] = {
    "groq": LLMPreset(
        name="groq",
        label="Groq Cloud",
        base_url="https://api.groq.com/openai/v1",
        key_field="groq_api_key",
        model_field="groq_model",
        default_model="llama-3.3-70b-versatile",
        key_url="https://console.groq.com/keys",
        models_url="https://console.groq.com/docs/models",
        free_tier="Free tier, no card: ~30 req/min + daily token caps (up to 14.4k req/day on 8B).",
        docs_url="https://console.groq.com/docs/openai",
        notes="Very fast, OpenAI-compatible, supports tool calling (needed for web search).",
    ),
    "gemini": LLMPreset(
        name="gemini",
        label="Google AI Studio (Gemini)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_field="gemini_api_key",
        model_field="gemini_model",
        default_model="gemini-2.5-flash",
        key_url="https://aistudio.google.com/apikey",
        models_url="https://ai.google.dev/gemini-api/docs/models",
        free_tier="Free tier, no card: per-model RPM/RPD limits (Flash ~10-15 RPM).",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
        notes="Best quality on this list. Uses Google's OpenAI-compatibility endpoint, "
        "so tool calling works through the same code path. Free-tier prompts may be used for training.",
    ),
    "openrouter": LLMPreset(
        name="openrouter",
        label="OpenRouter (free models)",
        base_url="https://openrouter.ai/api/v1",
        key_field="openrouter_api_key",
        model_field="openrouter_model",
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        key_url="https://openrouter.ai/keys",
        models_url="https://openrouter.ai/models?max_price=0",
        free_tier="Free models (ID ends in :free): 20 req/min, 50 req/day, "
        "1,000 req/day once $10 of credit has ever been bought.",
        docs_url="https://openrouter.ai/docs/quickstart",
        extra_headers={
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "Agetnic Orchestration Sandbox",
        },
        notes="One key, ~20 free models from many vendors. Model IDs must end in :free.",
    ),
    "mistral": LLMPreset(
        name="mistral",
        label="Mistral La Plateforme",
        base_url="https://api.mistral.ai/v1",
        key_field="mistral_api_key",
        model_field="mistral_model",
        default_model="mistral-small-latest",
        key_url="https://console.mistral.ai/api-keys",
        models_url="https://docs.mistral.ai/getting-started/models/models_overview/",
        free_tier="Free 'Experiment' plan: monthly token allowance, heavily rate limited (~2 req/min).",
        docs_url="https://docs.mistral.ai/api/",
        notes="Phone verification required for the free plan. Usually needs a small retry delay "
        "because of ~2 req/min limits.",
    ),
    "cloudflare": LLMPreset(
        name="cloudflare",
        label="Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        key_field="cloudflare_api_token",
        model_field="cloudflare_model",
        default_model="@cf/meta/llama-3.1-8b-instruct",
        key_url="https://dash.cloudflare.com/profile/api-tokens",
        models_url="https://developers.cloudflare.com/workers-ai/models/",
        free_tier="10,000 neurons/day free on every plan (no card).",
        docs_url="https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/",
        extra_required=("cloudflare_account_id",),
        notes="Needs both the API token and the account ID (dashboard -> Workers & Pages -> "
        "Account details). Model IDs look like @cf/meta/llama-3.1-8b-instruct.",
    ),
    "nvidia": LLMPreset(
        name="nvidia",
        label="NVIDIA NIM (build.nvidia.com)",
        base_url="https://integrate.api.nvidia.com/v1",
        key_field="nvidia_api_key",
        model_field="nvidia_model",
        default_model="meta/llama-3.3-70b-instruct",
        key_url="https://build.nvidia.com/",
        models_url="https://build.nvidia.com/models",
        free_tier="Free developer credits on signup (rate limited), no card needed.",
        docs_url="https://docs.api.nvidia.com/",
        notes="Open the model page, click 'Get API Key', copy the model id shown in the code block.",
    ),
    "cerebras": LLMPreset(
        name="cerebras",
        label="Cerebras Inference",
        base_url="https://api.cerebras.ai/v1",
        key_field="cerebras_api_key",
        model_field="cerebras_model",
        default_model="llama-3.3-70b",
        key_url="https://cloud.cerebras.ai/",
        models_url="https://inference-docs.cerebras.ai/models/overview",
        free_tier="Free trial credits (as of 2026 a card is required after the trial; "
        "it is listed here as the fastest option, not as a guaranteed-free one).",
        docs_url="https://inference-docs.cerebras.ai/",
        card_required=True,
        notes="Extremely fast. Context is capped on the free/trial tier.",
    ),
    "huggingface": LLMPreset(
        name="huggingface",
        label="Hugging Face Inference Providers",
        base_url="https://router.huggingface.co/v1",
        key_field="huggingface_api_key",
        model_field="huggingface_model",
        default_model="meta-llama/Llama-3.1-8B-Instruct",
        key_url="https://huggingface.co/settings/tokens",
        models_url="https://huggingface.co/models?inference=warm",
        free_tier="Small free monthly inference credits for signed-in accounts.",
        docs_url="https://huggingface.co/docs/inference-providers/index",
        notes="Model IDs are full repo names, e.g. meta-llama/Llama-3.1-8B-Instruct.",
    ),
    "ollama": LLMPreset(
        name="ollama",
        label="Ollama (local)",
        base_url="{ollama_base_url}",
        key_field=None,
        model_field="ollama_model",
        default_model="llama3.2",
        key_url="https://ollama.com/download",
        models_url="https://ollama.com/library",
        free_tier="100% free and local, no account, no rate limits.",
        docs_url="https://docs.ollama.com/api/openai-compatibility",
        needs_local_server=True,
        auth_scheme="none",
        notes="Run `ollama serve` and `ollama pull llama3.2`. Uses the OpenAI-compatible "
        "/v1 endpoint, so the same provider code works.",
    ),
    "lmstudio": LLMPreset(
        name="lmstudio",
        label="LM Studio (local)",
        base_url="{lmstudio_base_url}",
        key_field=None,
        model_field="lmstudio_model",
        default_model=None,
        key_url="https://lmstudio.ai/",
        models_url="https://lmstudio.ai/models",
        free_tier="100% free and local, no account.",
        docs_url="https://lmstudio.ai/docs/app/api/endpoints/openai",
        needs_local_server=True,
        auth_scheme="none",
        notes="Start the local server tab in LM Studio (default port 1234). The model id is "
        "whatever the server reports at /v1/models.",
    ),
    "custom": LLMPreset(
        name="custom",
        label="Custom OpenAI-compatible endpoint",
        base_url="{custom_base_url}",
        key_field="custom_api_key",
        model_field="custom_model",
        default_model=None,
        key_url="(your own provider's dashboard)",
        models_url="(your provider's model list, or GET /v1/models)",
        free_tier="Whatever your own provider offers.",
        docs_url="",
        notes="Point CUSTOM_BASE_URL at any /v1 endpoint: vLLM, LiteLLM, llama.cpp server, "
        "OpenRouter clone, a company gateway, a provider I have not preset. "
        "API key is optional (leave blank for keyless gateways).",
    ),
}


def get_preset(name: str) -> LLMPreset | None:
    return PRESETS.get((name or "").strip().lower())
