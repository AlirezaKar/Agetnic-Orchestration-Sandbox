"""Provider selection: `LLM_PROVIDER=auto` vs a manual choice, with explicit models.

The contract being tested:

* a provider is usable only when it has **both** a key and a model;
* `LLM_PROVIDER=auto` walks ``AUTO_PROVIDER_ORDER`` and picks the first usable one;
* a manual `LLM_PROVIDER=name` uses that provider and nothing else;
* in both modes the model is exactly the id written in ``<PROVIDER>_MODEL``.
"""

from __future__ import annotations

import argparse
import unittest

from app.config import AUTO_PROVIDER_ORDER, Settings
from app.llm.registry import build_chain, describe_providers, resolve_provider
from scripts import doctor, start


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class AutoProviderTests(unittest.TestCase):
    def test_auto_is_the_default(self):
        cfg = settings()
        self.assertTrue(cfg.is_auto_provider)
        self.assertEqual(cfg.llm_provider, "auto")
        self.assertEqual(cfg.llm_chain[: len(AUTO_PROVIDER_ORDER)], list(AUTO_PROVIDER_ORDER))

    def test_auto_picks_the_first_ready_provider(self):
        cfg = settings(
            llm_provider="auto",
            groq_api_key="k",
            groq_model="openai/gpt-oss-120b",
            gemini_api_key="k2",
            gemini_model="gemini-flash-latest",
        )
        chain = build_chain(cfg)
        self.assertEqual([p.name for p in chain.providers], ["groq", "gemini"])
        self.assertEqual(chain.primary.name, "groq")

    def test_auto_falls_through_to_the_next_ready_provider(self):
        cfg = settings(
            llm_provider="auto",
            gemini_api_key="k2",
            gemini_model="gemini-flash-latest",
        )
        chain = build_chain(cfg)
        self.assertEqual([p.name for p in chain.providers], ["gemini"])

    def test_auto_skips_a_key_without_a_model(self):
        cfg = settings(llm_provider="auto", groq_api_key="k")
        chain = build_chain(cfg)
        self.assertEqual(chain.providers, [])
        self.assertTrue(any("GROQ_MODEL" in note for note in chain.skipped))

    def test_auto_uses_the_model_written_next_to_the_key(self):
        cfg = settings(
            llm_provider="auto",
            openrouter_api_key="k",
            openrouter_model="google/gemma-4-31b-it:free",
        )
        provider = build_chain(cfg).primary
        self.assertEqual(provider.name, "openrouter")
        self.assertEqual(provider.model, "google/gemma-4-31b-it:free")
        self.assertFalse(provider.is_auto)

    def test_auto_honours_explicit_fallbacks_first(self):
        cfg = settings(
            llm_provider="auto",
            llm_fallbacks="custom",
            custom_base_url="https://gateway.test/v1",
            custom_model="my-model",
        )
        names = [p.name for p in build_chain(cfg).providers]
        self.assertEqual(names[0], "custom", "an explicit fallback should lead the chain")

    def test_auto_substitution_is_off_by_default(self):
        self.assertFalse(settings().llm_auto_substitute)

    def test_auto_ignores_llm_model_override(self):
        # LLM_MODEL is a manual-mode override; auto relies on <PROVIDER>_MODEL.
        cfg = settings(
            llm_provider="auto",
            llm_model="some-global-model",
            groq_api_key="k",
            groq_model="openai/gpt-oss-120b",
        )
        self.assertEqual(build_chain(cfg).primary.model, "openai/gpt-oss-120b")


class IgnoredOverrideTests(unittest.TestCase):
    def test_manual_mode_uses_every_override(self):
        cfg = settings(
            llm_provider="groq",
            groq_api_key="k",
            groq_model="m",
            llm_model="proxy",
            llm_base_url="https://proxy.test/v1",
        )
        self.assertEqual(cfg.unused_llm_overrides, [])

    def test_auto_mode_reports_what_it_ignores(self):
        cfg = settings(
            llm_provider="auto",
            llm_model="proxy",
            groq_api_key="k",
            groq_model="m",
        )
        self.assertEqual(cfg.unused_llm_overrides, ["LLM_MODEL"])
        # and the provider still uses the model written next to its key
        self.assertEqual(build_chain(cfg).primary.model, "m")


class ManualProviderTests(unittest.TestCase):
    def test_manual_choice_wins_over_other_ready_providers(self):
        cfg = settings(
            llm_provider="gemini",
            gemini_api_key="k",
            gemini_model="gemini-flash-latest",
            groq_api_key="k2",
            groq_model="openai/gpt-oss-120b",
        )
        self.assertEqual([p.name for p in build_chain(cfg).providers], ["gemini"])

    def test_manual_choice_uses_its_own_model(self):
        cfg = settings(
            llm_provider="mistral",
            mistral_api_key="k",
            mistral_model="mistral-small-latest",
            groq_model="openai/gpt-oss-120b",  # another provider's model is irrelevant
        )
        self.assertEqual(build_chain(cfg).primary.model, "mistral-small-latest")

    def test_manual_choice_without_model_is_not_ready(self):
        cfg = settings(llm_provider="mistral", mistral_api_key="k")
        chain = build_chain(cfg)
        self.assertEqual(chain.providers, [])
        self.assertIn("MISTRAL_MODEL", chain.skipped[0])

    def test_manual_choice_missing_key_reports_the_key(self):
        cfg = settings(llm_provider="mistral", mistral_model="mistral-small-latest")
        self.assertIn("MISTRAL_API_KEY", build_chain(cfg).skipped[0])

    def test_llm_model_still_overrides_the_primary(self):
        cfg = settings(
            llm_provider="groq",
            groq_api_key="k",
            groq_model="ignored",
            llm_model="proxy-model",
        )
        self.assertEqual(build_chain(cfg).primary.model, "proxy-model")


class DescriptionTests(unittest.TestCase):
    def test_missing_model_status(self):
        entries = {p["name"]: p for p in describe_providers(settings(groq_api_key="k"))}
        self.assertEqual(entries["groq"]["status"], "missing_model")
        self.assertEqual(entries["groq"]["missing_env"], ["GROQ_MODEL"])
        self.assertEqual(entries["groq"]["model_value"], "")

    def test_ready_status(self):
        entries = {
            p["name"]: p
            for p in describe_providers(settings(groq_api_key="k", groq_model="m"))
        }
        self.assertEqual(entries["groq"]["status"], "ready")
        self.assertTrue(entries["groq"]["model_value"] == "m")

    def test_resolved_model_is_never_auto(self):
        resolved = resolve_provider("groq", settings(groq_api_key="k", groq_model="m"))
        self.assertEqual(resolved.model, "m")
        self.assertNotIn("auto", (resolved.model or "").lower())


class DoctorSkipRecordTests(unittest.TestCase):
    def test_skipped_is_explicit_not_null(self):
        record = doctor.skipped("not requested")
        self.assertEqual(record, {"skipped": True, "reason": "not requested"})
        self.assertIsNot(record.get("skipped"), None)


class StartScriptTests(unittest.TestCase):
    def args(self, **overrides) -> argparse.Namespace:
        base = dict(agent=False, query=None, port=8000, host=None, reload=False, log_level=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_checks_run_providers_then_doctor(self):
        steps = start.build_steps(self.args())
        self.assertEqual([step.name for step in steps], ["providers", "doctor"])
        self.assertIn("scripts.check_providers", " ".join(steps[0].argv))
        self.assertIn("scripts.doctor", " ".join(steps[1].argv))
        self.assertNotIn("--agent", steps[1].argv)

    def test_agent_flag_is_forwarded(self):
        steps = start.build_steps(self.args(agent=True, query="python 3.13"))
        self.assertIn("--agent", steps[1].argv)
        self.assertIn("python 3.13", steps[1].argv)

    def test_server_command_defaults_to_the_env_port(self):
        command = start.serve_command(self.args(port=None))
        self.assertEqual(command[1], "run.py")
        self.assertNotIn("--port", command)

    def test_server_command_uses_run_py(self):
        command = start.serve_command(self.args(port=9100, host="127.0.0.1", reload=True))
        self.assertEqual(command[1], "run.py")
        self.assertEqual(command[command.index("--port") + 1], "9100")
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertIn("--reload", command)

    def test_decide(self):
        self.assertEqual(start.decide([], force=False, interactive=False, check_only=False), "serve")
        self.assertEqual(start.decide(["doctor"], force=False, interactive=False, check_only=False), "stop")
        self.assertEqual(start.decide(["doctor"], force=True, interactive=False, check_only=False), "serve")
        self.assertEqual(start.decide(["doctor"], force=False, interactive=True, check_only=False), "ask")
        self.assertEqual(start.decide([], force=False, interactive=True, check_only=True), "stop")


if __name__ == "__main__":
    unittest.main()
