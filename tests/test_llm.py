"""Provider layer tests. All HTTP is faked: no keys and no network needed."""

from __future__ import annotations

import json
import unittest

import httpx

from app.config import Settings
from app.llm.base import ProviderError
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.registry import (
    ConfigurationError,
    build_chain,
    build_provider,
    describe_providers,
    resolve_named,
    resolve_provider,
)


def settings(**overrides) -> Settings:
    """Settings that ignore the developer's real .env."""
    return Settings(_env_file=None, **overrides)


def chat_response(content="hello", tool_calls=None, model="test-model") -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def make_provider(handler, **kwargs) -> OpenAICompatProvider:
    transport = httpx.MockTransport(handler)
    defaults = dict(
        name="test",
        label="Test",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="secret",
    )
    defaults.update(kwargs)
    return OpenAICompatProvider(**defaults, transport=transport)


class MessageParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_reply(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=chat_response("pong"))

        provider = make_provider(handler)
        result = await provider.complete([{"role": "user", "content": "ping"}])
        self.assertEqual(result.content, "pong")
        self.assertEqual(result.provider, "test")
        self.assertEqual(result.usage["total_tokens"], 15)
        self.assertEqual(seen["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertFalse(seen["body"]["stream"])

    async def test_tool_call_arguments_are_parsed(self):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "python release notes"}),
                },
            }
        ]

        def handler(request):
            return httpx.Response(200, json=chat_response(None, calls))

        provider = make_provider(handler)
        result = await provider.complete([{"role": "user", "content": "?"}])
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "web_search")
        self.assertEqual(result.tool_calls[0].arguments["query"], "python release notes")
        assistant = result.to_assistant_message()
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "web_search")

    async def test_malformed_tool_arguments_do_not_crash(self):
        calls = [{"id": "c", "function": {"name": "web_search", "arguments": "{not json"}}]

        provider = make_provider(lambda request: httpx.Response(200, json=chat_response(None, calls)))
        result = await provider.complete([{"role": "user", "content": "?"}])
        self.assertIn("_raw", result.tool_calls[0].arguments)

    async def test_content_parts_are_flattened(self):
        payload = chat_response()
        payload["choices"][0]["message"]["content"] = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        provider = make_provider(lambda request: httpx.Response(200, json=payload))
        result = await provider.complete([{"role": "user", "content": "?"}])
        self.assertEqual(result.content, "ab")


class ErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_401_points_at_key_page(self):
        provider = make_provider(
            lambda request: httpx.Response(401, json={"error": {"message": "invalid api key"}}),
            key_url="https://console.groq.com/keys",
        )
        with self.assertRaises(ProviderError) as ctx:
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("console.groq.com/keys", ctx.exception.hint)

    async def test_429_mentions_rate_limit(self):
        provider = make_provider(
            lambda request: httpx.Response(429, headers={"retry-after": "7"}, json={})
        )
        with self.assertRaises(ProviderError) as ctx:
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("7s", ctx.exception.hint)

    async def test_404_suggests_model_list(self):
        provider = make_provider(
            lambda request: httpx.Response(404, json={"error": "model_not_found"}),
            models_url="https://console.groq.com/docs/models",
        )
        with self.assertRaises(ProviderError) as ctx:
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertIn("console.groq.com/docs/models", ctx.exception.hint)

    async def test_network_failure_is_wrapped(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        provider = make_provider(boom, name="ollama")
        with self.assertRaises(ProviderError) as ctx:
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.provider, "ollama")
        self.assertIn("network error", str(ctx.exception))


class ModelListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_shaped_list(self):
        payload = {"object": "list", "data": [{"id": "b"}, {"id": "a"}]}
        provider = make_provider(lambda request: httpx.Response(200, json=payload))
        self.assertEqual(await provider.list_models(), ["a", "b"])

    async def test_openai_shaped_list_is_sorted_and_deduped(self):
        payload = {"data": [{"id": "z"}, {"id": "z"}, {"id": "a"}]}
        provider = make_provider(lambda request: httpx.Response(200, json=payload))
        self.assertEqual(await provider.list_models(), ["a", "z"])

    async def test_cloudflare_result_shape(self):
        payload = {"result": [{"name": "@cf/meta/llama-3.1-8b-instruct"}]}
        provider = make_provider(lambda request: httpx.Response(200, json=payload))
        self.assertEqual(await provider.list_models(), ["@cf/meta/llama-3.1-8b-instruct"])


class ResolutionTests(unittest.TestCase):
    def test_missing_key_is_reported_not_guessed(self):
        cfg = settings(llm_provider="groq")
        chain = build_chain(cfg)
        self.assertEqual(chain.providers, [])
        self.assertIn("GROQ_API_KEY", chain.skipped[0])

    def test_key_without_model_is_not_ready_and_not_guessed(self):
        cfg = settings(llm_provider="groq", groq_api_key="gsk_test")
        chain = build_chain(cfg)
        # Nothing is invented: the model must be written in .env.
        self.assertEqual(chain.providers, [])
        self.assertIn("GROQ_MODEL", chain.skipped[0])

    def test_model_can_be_entered_explicitly(self):
        cfg = settings(llm_provider="groq", groq_api_key="k", groq_model="openai/gpt-oss-120b")
        provider = build_chain(cfg).primary
        self.assertEqual(provider.model, "openai/gpt-oss-120b")
        self.assertFalse(provider.is_auto)
        # an explicit id is used as-is, no substitution unless asked for
        self.assertFalse(cfg.llm_auto_substitute)

    def test_model_env_overrides_default(self):
        cfg = settings(llm_provider="groq", groq_api_key="k", groq_model="llama-3.1-8b-instant")
        self.assertEqual(build_chain(cfg).primary.model, "llama-3.1-8b-instant")

    def test_primary_overrides_apply_only_to_primary(self):
        cfg = settings(
            llm_provider="groq",
            groq_api_key="k",
            llm_model="my-proxy-model",
            llm_base_url="https://proxy.test/v1",
            llm_fallbacks="gemini",
            gemini_api_key="g",
            gemini_model="gemini-flash-latest",
        )
        chain = build_chain(cfg)
        self.assertEqual(chain.providers[0].base_url, "https://proxy.test/v1")
        self.assertEqual(chain.providers[0].model, "my-proxy-model")
        self.assertEqual(chain.providers[1].name, "gemini")
        self.assertEqual(chain.providers[1].model, "gemini-flash-latest")

    def test_custom_provider_requires_base_and_model(self):
        cfg = settings(llm_provider="custom")
        chain = build_chain(cfg)
        self.assertIn("CUSTOM_BASE_URL", chain.skipped[0])

    def test_custom_provider_is_wired_up(self):
        cfg = settings(
            llm_provider="custom",
            custom_base_url="https://my-gateway.test/v1",
            custom_api_key="abc",
            custom_model="my-model",
            custom_extra_body='{"reasoning_effort": "low"}',
            custom_extra_headers='{"X-Org": "sandbox"}',
        )
        provider = build_chain(cfg).primary
        self.assertEqual(provider.base_url, "https://my-gateway.test/v1")
        self.assertEqual(provider.model, "my-model")
        self.assertEqual(provider.extra_body["reasoning_effort"], "low")
        self.assertEqual(provider.extra_headers["X-Org"], "sandbox")
        self.assertEqual(provider._headers()["Authorization"], "Bearer abc")

    def test_custom_provider_keyless_and_raw_auth(self):
        cfg = settings(
            llm_provider="custom",
            custom_base_url="https://gateway.test/v1",
            custom_model="m",
            custom_auth_scheme="raw",
            custom_api_key="plain-key",
        )
        headers = build_chain(cfg).primary._headers()
        self.assertEqual(headers["Authorization"], "plain-key")

        cfg2 = settings(
            llm_provider="custom",
            custom_base_url="https://gateway.test/v1",
            custom_model="m",
            custom_auth_scheme="none",
        )
        self.assertNotIn("Authorization", build_chain(cfg2).primary._headers())

    def test_cloudflare_requires_account_id(self):
        cfg = settings(llm_provider="cloudflare", cloudflare_api_token="t")
        chain = build_chain(cfg)
        self.assertEqual(chain.providers, [])
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", chain.skipped[0])

    def test_cloudflare_base_url_is_templated(self):
        cfg = settings(
            llm_provider="cloudflare",
            cloudflare_api_token="t",
            cloudflare_account_id="acc123",
            cloudflare_model="@cf/openai/gpt-oss-120b",
        )
        provider = build_chain(cfg).primary
        self.assertEqual(
            provider.base_url,
            "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1",
        )

    def test_ollama_needs_no_key(self):
        cfg = settings(llm_provider="ollama", ollama_model="llama3.2")
        provider = build_chain(cfg).primary
        self.assertEqual(provider.base_url, "http://localhost:11434/v1")
        self.assertNotIn("Authorization", provider._headers())

    def test_gemini_uses_openai_compatible_endpoint(self):
        cfg = settings(llm_provider="gemini", gemini_api_key="k", gemini_model="gemini-flash-latest")
        self.assertEqual(
            build_chain(cfg).primary.base_url,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    def test_resolve_named_applies_overrides_only_for_the_primary(self):
        cfg = settings(
            llm_provider="gemini",
            gemini_api_key="k",
            llm_base_url="http://localhost:9102/v1beta/openai",
            llm_model="gemini-3.8-flash",
            groq_api_key="k2",
            groq_model="openai/gpt-oss-120b",
        )
        primary = resolve_named("gemini", cfg)
        self.assertEqual(primary.base_url, "http://localhost:9102/v1beta/openai")
        self.assertEqual(primary.model, "gemini-3.8-flash")
        other = resolve_named("groq", cfg)
        self.assertEqual(other.base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(other.model, "openai/gpt-oss-120b")

    def test_unknown_provider_is_a_clear_error(self):
        with self.assertRaises(ConfigurationError):
            resolve_provider("definitely-not-a-provider", settings())

    def test_description_covers_every_preset_with_links(self):
        for entry in describe_providers(settings()):
            self.assertTrue(entry["label"])
            if entry["name"] != "custom":
                self.assertTrue(entry["models_url"], entry["name"])
            self.assertTrue(entry["free_tier"], entry["name"])


class ChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_failover_to_second_provider(self):
        calls = []

        def primary(request):
            calls.append("primary")
            return httpx.Response(429, json={"error": "rate limited"})

        def secondary(request):
            calls.append("secondary")
            if request.method == "GET" and request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "gemini-flash-latest"}]})
            return httpx.Response(200, json=chat_response("from backup"))

        cfg = settings(
            llm_provider="groq",
            groq_api_key="k",
            groq_model="llama-3.1-8b-instant",
            llm_fallbacks="gemini",
            gemini_api_key="k2",
            gemini_model="gemini-flash-latest",
        )
        chain = build_chain(cfg)
        chain.providers[0]._transport = httpx.MockTransport(primary)
        chain.providers[1]._transport = httpx.MockTransport(secondary)

        result = await chain.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "from backup")
        self.assertEqual(result.provider, "gemini")
        # primary errors, then gemini resolves its model list + answers
        self.assertEqual(calls[0], "primary")
        self.assertIn("secondary", calls)

    async def test_all_providers_failing_raises_with_detail(self):
        cfg = settings(llm_provider="groq", groq_api_key="k", groq_model="llama-3.1-8b-instant")
        chain = build_chain(cfg)
        chain.providers[0]._transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
        with self.assertRaises(ProviderError) as ctx:
            await chain.complete([{"role": "user", "content": "hi"}])
        self.assertIn("groq", str(ctx.exception))

    async def test_empty_chain_explains_what_to_set(self):
        with self.assertRaises(ConfigurationError) as ctx:
            await build_chain(settings()).complete([{"role": "user", "content": "hi"}])
        message = str(ctx.exception)
        self.assertIn("GROQ_API_KEY", message)
        self.assertIn("GROQ_MODEL", message)

    async def test_missing_model_error_points_at_the_model_list(self):
        cfg = settings(llm_provider="groq", groq_api_key="k")
        with self.assertRaises(ConfigurationError) as ctx:
            build_provider(resolve_provider("groq", cfg, primary=True), cfg)
        message = str(ctx.exception)
        self.assertIn("GROQ_MODEL", message)
        self.assertIn("console.groq.com/docs/models", message)

    async def test_ping_reports_failure_without_raising(self):
        provider = make_provider(lambda request: httpx.Response(429, json={}))
        report = await provider.ping()
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["status"], 429)


class BuildProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_provider_from_resolved(self):
        cfg = settings(llm_provider="groq", groq_api_key="k", groq_model="openai/gpt-oss-120b")
        provider = build_provider(resolve_provider("groq", cfg, primary=True), cfg)
        self.assertEqual(provider.name, "groq")
        self.assertEqual(provider.timeout, 60.0)


if __name__ == "__main__":
    unittest.main()
