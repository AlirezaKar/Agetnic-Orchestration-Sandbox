"""Auto model discovery, retired-model healing and ranking tests."""

from __future__ import annotations

import json
import unittest

import httpx

from app.llm.base import ProviderError
from app.llm.model_picker import (
    choose_model,
    is_chat_model,
    looks_like_model_error,
    rank_models,
    suggest_models,
)
from app.llm.openai_compat import OpenAICompatProvider


def chat_ok(model="m", content="ok"):
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


def provider(handler, *, model="auto", name="gemini", **kwargs):
    return OpenAICompatProvider(
        name=name,
        label=name,
        base_url="https://example.test/v1",
        model=model,
        api_key="k",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class RankingTests(unittest.TestCase):
    def test_gemini_prefers_stable_alias_then_newest_flash(self):
        models = [
            "models/gemini-2.0-flash",
            "models/gemini-3.8-flash",
            "models/gemini-2.5-flash",
            "models/gemini-flash-latest",
            "models/gemini-3.1-flash-lite",
            "models/gemini-2.5-flash-image",
            "models/gemini-3.8-flash-tts",
            "models/text-embedding-004",
        ]
        ranked = rank_models("gemini", models)
        self.assertEqual(
            ranked[:3],
            [
                "models/gemini-flash-latest",
                "models/gemini-3.8-flash",
                "models/gemini-2.5-flash",
            ],
        )
        self.assertNotIn("models/gemini-3.8-flash-tts", ranked)
        self.assertNotIn("models/gemini-2.5-flash-image", ranked)
        self.assertNotIn("models/text-embedding-004", ranked)

    def test_openrouter_only_free_ids(self):
        models = [
            "openai/gpt-4o",
            "google/gemma-4-31b-it:free",
            "meta-llama/llama-3.3-70b-instruct",
            "nvidia/nemotron-3-ultra:free",
        ]
        ranked = rank_models("openrouter", models)
        self.assertTrue(all(model.endswith(":free") for model in ranked))
        self.assertEqual(len(ranked), 2)

    def test_groq_prefers_current_models(self):
        models = ["llama-3.1-8b-instant", "openai/gpt-oss-120b", "whisper-large-v3", "llama-3.3-70b-versatile"]
        self.assertEqual(choose_model("groq", models), "openai/gpt-oss-120b")

    def test_tool_capability_filter(self):
        models = ["a/model:free", "b/model:free"]
        ranked = rank_models(
            "openrouter", models, require_tools=True, tool_capable={"b/model:free"}
        )
        self.assertEqual(ranked, ["b/model:free"])

    def test_non_chat_filter(self):
        for model in ("text-embedding-3-small", "whisper-large-v3", "sdxl", "gemini-2.5-flash-tts"):
            self.assertFalse(is_chat_model(model), model)
        self.assertTrue(is_chat_model("meta-llama/Llama-3.1-8B-Instruct"))

    def test_empty_list(self):
        self.assertEqual(rank_models("gemini", []), [])


class SuggestionTests(unittest.TestCase):
    def test_same_family_suggestion(self):
        live = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.8-pro"]
        self.assertIn("gemini-3.8-flash", suggest_models("gemini-2.5-flash", live))

    def test_vendor_prefix_suggestion(self):
        live = ["meta-llama/llama-4:free", "google/gemma-4:free"]
        self.assertEqual(suggest_models("meta-llama/llama-3.3-70b:free", live)[0], "meta-llama/llama-4:free")

    def test_model_error_detection(self):
        bodies = [
            '{"error":{"message":"This model models/gemini-2.5-flash is no longer available to new users"}}',
            '{"error":{"message":"No endpoints found for x:free","code":404}}',
            '{"error":"model_not_found"}',
            '{"error":{"message":"model does not support tool use"}}',
        ]
        for body in bodies:
            self.assertTrue(looks_like_model_error(404, body), body)
        self.assertFalse(looks_like_model_error(401, "invalid api key"))
        self.assertFalse(looks_like_model_error(404, '{"error":"route not found"}'))


class AutoModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_lists_models_then_calls_the_best(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "gemini-2.5-flash-lite"},
                            {"id": "gemini-3.8-flash"},
                            {"id": "gemini-2.5-flash-image"},
                        ]
                    },
                )
            body = json.loads(request.content)
            assert body["model"] == "gemini-3.8-flash", body["model"]
            return httpx.Response(200, json=chat_ok("gemini-3.8-flash"))

        result = await provider(handler).complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "ok")
        self.assertEqual(result.model, "gemini-3.8-flash")
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])

    async def test_auto_prefers_tool_capable_models_when_tools_are_sent(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "vendor/no-tools:free", "supported_parameters": ["temperature"]},
                            {"id": "vendor/with-tools:free", "supported_parameters": ["tools", "temperature"]},
                        ]
                    },
                )
            body = json.loads(request.content)
            assert body["model"] == "vendor/with-tools:free", body["model"]
            return httpx.Response(200, json=chat_ok("vendor/with-tools:free"))

        tools = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]
        await provider(handler, name="openrouter").complete(
            [{"role": "user", "content": "hi"}], tools
        )

    async def test_auto_with_no_usable_models_errors_clearly(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "text-embedding-004"}]})

        with self.assertRaises(ProviderError) as ctx:
            await provider(handler).complete([{"role": "user", "content": "hi"}])
        self.assertIn("no usable chat models", str(ctx.exception))


class RetiredModelTests(unittest.IsolatedAsyncioTestCase):
    GEMINI_404 = {
        "error": {
            "code": 404,
            "message": "This model models/gemini-2.5-flash is no longer available to new users. "
            "Please update your code to use models/gemini-3.8-flash",
        }
    }

    async def test_retired_model_is_substituted_and_reported(self):
        calls = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}, {"id": "gemini-3.5-flash"}]})
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "gemini-2.5-flash":
                return httpx.Response(404, json=self.GEMINI_404)
            return httpx.Response(200, json=chat_ok(body["model"]))

        result = await provider(handler, model="gemini-2.5-flash").complete(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(result.model, "gemini-3.8-flash")
        self.assertEqual(calls, ["gemini-2.5-flash", "gemini-3.8-flash"])

    async def test_substitution_can_be_disabled(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}]})
            return httpx.Response(404, json=self.GEMINI_404)

        with self.assertRaises(ProviderError) as ctx:
            await provider(handler, model="gemini-2.5-flash", auto_substitute=False).complete(
                [{"role": "user", "content": "hi"}]
            )
        error = ctx.exception
        self.assertEqual(error.status, 404)
        self.assertIn("no longer available", str(error))
        self.assertIn("gemini-3.8-flash", error.hint or "")

    async def test_openrouter_no_endpoints_rotates_to_another_free_model(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "dead/model:free", "supported_parameters": ["tools"]},
                            {"id": "live/model:free", "supported_parameters": ["tools"]},
                        ]
                    },
                )
            body = json.loads(request.content)
            if body["model"] == "dead/model:free":
                return httpx.Response(
                    404, json={"error": {"message": "No endpoints found for dead/model:free.", "code": 404}}
                )
            return httpx.Response(200, json=chat_ok(body["model"]))

        result = await provider(
            handler, name="openrouter", model="dead/model:free"
        ).complete([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "t"}}])
        self.assertEqual(result.model, "live/model:free")

    async def test_failure_after_all_attempts_mentions_alternatives(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "a/flash:free"}, {"id": "b/flash:free"}]})
            return httpx.Response(404, json={"error": {"message": "No endpoints found for this model."}})

        with self.assertRaises(ProviderError) as ctx:
            await provider(handler, name="openrouter", model="x/flash:free").complete(
                [{"role": "user", "content": "hi"}]
            )
        hint = ctx.exception.hint or ""
        self.assertIn("live options", hint)
        self.assertIn("also tried", hint)

    async def test_non_model_errors_are_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        with self.assertRaises(ProviderError) as ctx:
            await provider(handler, model="gemini-3.8-flash").complete(
                [{"role": "user", "content": "hi"}]
            )
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(calls, ["POST"])  # no /models probe, no retry

    async def test_model_list_failure_is_reported(self):
        def handler(request):
            return httpx.Response(500, text="upstream boom")

        with self.assertRaises(ProviderError) as ctx:
            await provider(handler, model="auto").complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.status, 500)


if __name__ == "__main__":
    unittest.main()
