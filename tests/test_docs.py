"""Keeps the documentation — and the strip-down — honest.

Three promises are checked here:

1. ``.env.example`` documents every ARAS setting, contains nothing the app
   does not read, and never carries a real secret.
2. The README explains the ARAS services and how to verify them.
3. Nothing of the removed providers is left: no module, no script, no setting,
   no variable in the template, no mention in the code, the page or the README.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app import aras
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"

#: Read by ``app/logs.py`` from the process environment — not ``Settings`` fields,
#: and not parsed out of ``.env``; the template lists them with a note saying so.
LOG_ENV = {"LOG_LEVEL", "LOG_DIR", "LOG_ROTATE_MB", "LOG_BACKUPS"}

#: Identifiers that only existed for the removed model / search providers.
#: Old changelogs and the redesign notes are history and are not scanned.
REMOVED = (
    # settings and env vars
    "LLM_PROVIDER", "LLM_FALLBACKS", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_TIMEOUT",
    "GROQ_", "GEMINI_", "OPENROUTER_", "MISTRAL_", "CEREBRAS_", "NVIDIA_", "HUGGINGFACE_",
    "CLOUDFLARE_", "OLLAMA_", "LMSTUDIO_", "CUSTOM_BASE_URL", "CUSTOM_API_KEY", "CUSTOM_MODEL",
    "SEARCH_PROVIDER", "SEARCH_FALLBACKS", "SEARCH_TIMEOUT", "TAVILY_", "SERPER_", "EXA_API_KEY",
    "GOOGLE_CSE", "SEARXNG_", "JINA_", "DUCKDUCKGO_", "WIKIPEDIA_LANG", "FETCH_MAX_CHARS",
    "LLM_AUTO_SUBSTITUTE", "LLM_MODEL_ATTEMPTS", "SEARCH_DEDUPE", "SEARCH_MAX_RESULTS",
    "SEARCH_USER_AGENT", "CUSTOM_AUTH_", "CUSTOM_EXTRA_", "CUSTOM_LABEL",
    # code
    "app.llm", "app/llm", "app.search", "app/search", "build_chain", "build_search_chain",
    "ProviderError", "SearchError", "ConfigurationError", "model_picker", "fetch_page", "fetch_url",
    # scripts and routes
    "check_providers", "check_search", "scripts.doctor", "scripts/doctor", "scripts.start",
    "scripts/start", "/search/diagnose", "/providers/{name}/models",
)

#: Vendor names the page and the README must not advertise any more.
VENDORS = (
    "groq", "gemini", "ollama", "lm studio", "lmstudio", "mistral", "cerebras", "huggingface",
    "cloudflare", "tavily", "serper", "duckduckgo", "wikipedia", "searxng", "jina", "google cse",
    "openrouter.ai",
)


def env_lines() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.append((key.strip(), value.strip()))
    return out


def env_keys() -> set[str]:
    return {key for key, _ in env_lines()}


def shipped_files() -> list[Path]:
    """Everything a user runs or reads: code, page, scripts, template, README."""
    files = [ROOT / "run.py", ENV_EXAMPLE, README, ROOT / "requirements.txt", ROOT / "logs" / "README.md"]
    for folder, patterns in (
        ("app", ("*.py", "*.md", "*.html", "*.js", "*.css")),
        ("scripts", ("*.py",)),
    ):
        for pattern in patterns:
            files.extend(sorted((ROOT / folder).rglob(pattern)))
    return [path for path in files if path.is_file() and "__pycache__" not in path.parts]


class _DocCase(unittest.TestCase):
    """assertIn prints the whole document on failure; these print the needle."""

    def assertHas(self, needle: str, text: str, where: str) -> None:
        self.assertTrue(needle in text, f"{where}: expected to find {needle!r}")

    def assertHasNot(self, needle: str, text: str, where: str) -> None:
        self.assertFalse(needle in text, f"{where}: still mentions {needle!r}")


class EnvExampleTests(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(ENV_EXAMPLE.exists(), ".env.example is required")

    def test_every_aras_setting_is_documented(self):
        keys = env_keys()
        for name in Settings.model_fields:
            if not name.startswith("aras_"):
                continue
            if name.endswith("_url") and name != "aras_base_url":
                continue  # per-service URL overrides: README §2, not the template
            self.assertIn(name.upper(), keys, f"{name} is a setting but not in .env.example")

    def test_every_service_has_its_token_line(self):
        keys = env_keys()
        self.assertIn("ARAS_TOKEN", keys)
        for service in aras.CLIENTS:
            self.assertIn(f"ARAS_{service.upper()}_TOKEN", keys, service)

    def test_the_template_holds_nothing_the_app_does_not_read(self):
        """A variable with no reader is a lie about what can be configured."""
        known = {name.upper() for name in Settings.model_fields} | LOG_ENV
        for key in env_keys():
            self.assertIn(key, known, f"{key} is in .env.example but nothing reads it")

    def test_the_required_variables_come_first(self):
        keys = [key for key, _ in env_lines()]
        self.assertEqual(keys[0], "ARAS_BASE_URL")
        for required in ("ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL", "ARAS_WEB_TOKEN"):
            self.assertLess(keys.index(required), keys.index("ORCHESTRATOR_ENABLED"), required)

    def test_models_are_never_prefilled(self):
        # The model is chosen by the owner of the token, never guessed here.
        for key, value in env_lines():
            if key.endswith("_MODEL"):
                self.assertEqual(value, "", f"{key} must be left blank")

    def test_template_stays_lean(self):
        lines = [
            line.strip()
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        comments = [line for line in lines if line.startswith("#")]
        self.assertLess(
            len(comments) / len(lines),
            0.4,
            "comments should stay minimal: one short line per section or knob",
        )
        # the provider blocks are gone: the template is roughly half of what it was
        self.assertLess(len(lines), 100, "the template must stay readable")

    def test_no_secret_is_committed(self):
        secret = re.compile(r"(?:_TOKEN|_KEY|_SECRET|_PASSWORD)$|^ARAS_(?:TENANT|OWNER|DATA_SOURCE_ID)$")
        checked = 0
        for key, value in env_lines():
            if not secret.search(key):
                continue
            checked += 1
            self.assertEqual(value, "", f"{key} must be left empty in .env.example (found a value)")
        self.assertGreaterEqual(checked, 8)  # the shared token + seven service tokens

    def test_links_are_https(self):
        for path in (ENV_EXAMPLE, README):
            for url in re.findall(r"https?://[^\s)]+", path.read_text(encoding="utf-8")):
                if "localhost" in url or "127.0.0.1" in url or "example.com" in url:
                    continue
                self.assertTrue(url.startswith("https://"), f"insecure link: {url}")


class ReadmeTests(_DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_readme_names_the_three_parts(self):
        for part in ("orchestrator", "aras", "app/static"):
            self.assertHas(part, self.lower, "README.md")
        self.assertHas("app/orchestrator/", self.text, "README.md")
        self.assertHas("app/aras.py", self.text, "README.md")

    def test_readme_covers_every_aras_service(self):
        for service, (port, _) in Settings.ARAS_PORTS.items():
            self.assertHas(service, self.lower, "README.md")
            self.assertHas(str(port), self.text, "README.md")
            self.assertHas(f"ARAS_{service.upper()}_TOKEN", self.text, "README.md")

    def test_readme_names_the_variables_a_first_run_needs(self):
        for env in ("ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL", "ARAS_WEB_TOKEN"):
            self.assertHas(env, self.text, "README.md")

    def test_readme_explains_verification(self):
        self.assertHas("verify", self.lower, "README.md")
        for marker in ("/diagnose", "/aras", "/aras/models", "scripts/aras_smoke.py", "python run.py"):
            self.assertHas(marker, self.text, "README.md")

    def test_readme_documents_every_route_the_app_lists(self):
        from app.main import API_ROUTES

        for line in API_ROUTES:
            path = line.split()[1]
            if path == "/" or "…" in path:
                continue
            base = re.sub(r"/\{[^}]*\}.*$", "", path)
            self.assertHas(base, self.text, f"README.md (route {path})")

    def test_every_script_the_readme_runs_exists(self):
        modules = set(re.findall(r"python3? -m (scripts\.[a-z_]+)", self.text))
        files = set(re.findall(r"python3? (scripts/[a-z_]+\.py)", self.text))
        self.assertTrue(modules or files)
        for module in modules:
            self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file(), module)
        for path in files:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_every_file_the_readme_points_at_exists(self):
        """A path in backticks or a relative link must lead somewhere."""
        linked = set(re.findall(r"\]\((?!https?://|#)([^)#\s]+)", self.text))
        for target in linked:
            self.assertTrue((ROOT / target).exists(), f"README.md links to a missing file: {target}")
        quoted = set(re.findall(r"`((?:app|scripts|tests|logs|docs)/[A-Za-z0-9_./-]+\.(?:py|md|js|html|css))`", self.text))
        for target in quoted:
            self.assertTrue((ROOT / target).exists(), f"README.md names a missing file: {target}")


class StripDownTests(_DocCase):
    """Only the orchestrator, the UI and ARAS are left — and nothing points elsewhere."""

    def test_the_removed_packages_and_scripts_are_gone(self):
        for gone in (
            "app/llm", "app/search",
            "scripts/start.py", "scripts/doctor.py", "scripts/check_providers.py", "scripts/check_search.py",
            "tests/test_llm.py", "tests/test_model_picker.py", "tests/test_search.py", "tests/test_selection.py",
        ):
            self.assertFalse((ROOT / gone).exists(), f"{gone} should have been removed")

    def test_the_scripts_folder_holds_only_what_still_has_a_job(self):
        scripts = {path.name for path in (ROOT / "scripts").glob("*.py")}
        self.assertEqual(scripts, {"__init__.py", "aras_smoke.py", "audit.py", "logs.py", "memory.py"})

    def test_nothing_shipped_names_a_removed_identifier(self):
        for path in shipped_files():
            text = path.read_text(encoding="utf-8")
            for name in REMOVED:
                self.assertHasNot(name, text, str(path.relative_to(ROOT)))

    def test_the_page_and_the_readme_advertise_no_other_vendor(self):
        pages = [README, ENV_EXAMPLE] + sorted((ROOT / "app" / "static").rglob("*.html"))
        pages += sorted((ROOT / "app" / "static").rglob("*.js"))
        for path in pages:
            text = path.read_text(encoding="utf-8").lower()
            for vendor in VENDORS:
                self.assertHasNot(vendor, text, str(path.relative_to(ROOT)))

    def test_the_sample_tasks_are_about_this_app(self):
        """Same cards in the static page and in the script; none shops for a provider."""
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "app" / "static" / "assets" / "js" / "app.js").read_text(encoding="utf-8")
        prompts = re.findall(r'class="suggestion" data-prompt="([^"]+)"', html)
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertHas(prompt, script, "app.js (sample task from index.html)")
        for stale in ("free tier", "free llm", "compare providers"):
            self.assertHasNot(stale, html.lower(), "index.html")
            self.assertHasNot(stale, script.lower(), "app.js")

    def test_the_only_outbound_http_client_is_the_aras_module(self):
        """Every network call the app makes is in app/aras.py — one place to debug."""
        users = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(import httpx|from httpx import)", source, flags=re.M):
                users.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(users, ["app/aras.py"])

    def test_requirements_carry_no_provider_sdk(self):
        names = set()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.add(re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0].lower())
        for sdk in ("openai", "groq", "google-generativeai", "anthropic", "mistralai", "ollama",
                    "duckduckgo-search", "ddgs", "tavily-python", "wikipedia", "beautifulsoup4"):
            self.assertNotIn(sdk, names, sdk)


if __name__ == "__main__":
    unittest.main()
