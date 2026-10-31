"""Keeps documentation honest: every supported provider must be documented in
.env.example with its env vars, and no real secret may ever be committed."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.llm.presets import PRESETS
from app.search.presets import SEARCH_PRESETS

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"


def env_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


class EnvExampleTests(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(ENV_EXAMPLE.exists(), ".env.example is required")

    def test_every_llm_preset_is_documented(self):
        keys = env_keys()
        for preset in PRESETS.values():
            if preset.key_field:
                self.assertIn(preset.key_field.upper(), keys, f"{preset.name} key")
            self.assertIn(preset.model_field.upper(), keys, f"{preset.name} model")
            for extra in preset.extra_required:
                self.assertIn(extra.upper(), keys, f"{preset.name} {extra}")

    def test_every_search_preset_is_documented(self):
        keys = env_keys()
        for preset in SEARCH_PRESETS.values():
            for env in preset.key_env:
                self.assertIn(env, keys, f"{preset.name} {env}")

    def test_selector_variables_exist(self):
        keys = env_keys()
        for required in (
            "LLM_PROVIDER",
            "LLM_MODEL",
            "LLM_FALLBACKS",
            "CUSTOM_BASE_URL",
            "CUSTOM_API_KEY",
            "CUSTOM_MODEL",
            "SEARCH_PROVIDER",
            "SEARCH_PROVIDERS",
        ):
            self.assertIn(required, keys)

    def test_no_secret_is_committed(self):
        pattern = re.compile(
            r"^([A-Z0-9_]*(?:API_KEY|API_TOKEN|ACCESS_TOKEN|API_SECRET|_ACCOUNT_ID|_CSE_ID)[A-Z0-9_]*)=(.*)$"
        )
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            value = match.group(2).strip()
            self.assertTrue(
                value == "" or value.startswith("{"),
                f"{match.group(1)} must be left empty in .env.example (found a value)",
            )

    def test_links_are_https(self):
        for path in (ENV_EXAMPLE, README):
            for url in re.findall(r"https?://[^\s)]+", path.read_text(encoding="utf-8")):
                if "localhost" in url or "127.0.0.1" in url or "example.com" in url:
                    continue
                self.assertTrue(url.startswith("https://"), f"insecure link: {url}")


class ReadmeTests(unittest.TestCase):
    def test_readme_covers_every_provider(self):
        text = README.read_text(encoding="utf-8").lower()
        for preset in PRESETS.values():
            self.assertIn(preset.name, text, f"README should mention {preset.name}")
        for preset in SEARCH_PRESETS.values():
            self.assertIn(preset.name, text, f"README should mention search provider {preset.name}")

    def test_readme_explains_verification(self):
        text = README.read_text(encoding="utf-8").lower()
        self.assertIn("verify", text)
        self.assertIn("/search/diagnose", text)


if __name__ == "__main__":
    unittest.main()
