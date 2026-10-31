"""The "one log file per version" contract, enforced instead of documented.

These tests are deliberately strict about the *shape* of the log folder:

* every version that ever shipped has ``log/v<version>.md``;
* the index in ``log/README.md`` lists each of them;
* the changelog for the current ``VERSION`` mentions that same version;
* ``app.applog`` writes one line per event into ``log/runtime/v<version>.log``.
"""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from app import applog
from app.main import VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "log"
README = LOG_DIR / "README.md"


class LogFolderTests(unittest.TestCase):
    def test_log_folder_exists(self):
        self.assertTrue(LOG_DIR.is_dir(), "log/ must exist and be committed")
        self.assertTrue(README.is_file(), "log/README.md indexes the versions")

    def test_every_release_has_a_log_file(self):
        changelogs = sorted(p.stem for p in LOG_DIR.glob("v*.md"))
        self.assertGreaterEqual(len(changelogs), 3, "expected a file per released version")
        for stem in changelogs:
            self.assertRegex(stem, r"^v\d+\.\d+\.\d+$", f"{stem} is not a version")

    def test_current_version_has_a_log_file(self):
        target = LOG_DIR / f"v{VERSION}.md"
        self.assertTrue(target.is_file(), f"missing changelog for current VERSION {VERSION}")
        body = target.read_text(encoding="utf-8")
        self.assertIn(VERSION, body.splitlines()[0] + body[:400])
        self.assertIn("## Verify", body, "every changelog says how to check the release")

    def test_index_lists_every_version(self):
        index = README.read_text(encoding="utf-8")
        for path in LOG_DIR.glob("v*.md"):
            self.assertIn(path.name, index, f"log/README.md does not link {path.name}")

    def test_versions_are_unique_and_ordered(self):
        versions = [p.stem[1:] for p in LOG_DIR.glob("v*.md")]

        def key(v: str) -> tuple[int, ...]:
            return tuple(int(part) for part in v.split("."))

        self.assertEqual(len(versions), len(set(versions)))
        self.assertEqual(versions, sorted(versions, key=key))

    def test_no_log_file_is_empty_or_placeholder(self):
        for path in sorted(LOG_DIR.glob("v*.md")):
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 400, f"{path.name} is too thin to be useful")
            self.assertIsNone(
                re.search(r"\bTODO\b|\bTBD\b", text), f"{path.name} still has placeholders"
            )

    def test_runtime_logs_are_ignored_by_git(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("log/runtime/", gitignore)


class AppLogTests(unittest.TestCase):
    def test_append_creates_header_and_one_line_per_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = applog.append("9.9.9", "first event", base_dir=Path(tmp))
            applog.append("9.9.9", "second event", base_dir=Path(tmp))

            self.assertEqual(path, Path(tmp) / "v9.9.9.log")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith("#"))
            self.assertEqual(len(lines), 4, lines)  # 2 header lines + 2 events
            self.assertRegex(lines[2], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[v9\.9\.9\] first event$")
            self.assertIn("second event", lines[3])

    def test_separate_versions_write_separate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            applog.append("1.0.0", "a", base_dir=Path(tmp))
            applog.append("1.0.1", "b", base_dir=Path(tmp))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["v1.0.0.log", "v1.0.1.log"])

    def test_version_slug_is_filesystem_safe(self):
        self.assertEqual(applog.version_slug("0.4.0"), "0.4.0")
        self.assertNotIn("/", applog.version_slug("1.0.0/../../etc"))
        self.assertEqual(applog.version_slug(""), "unknown")

    def test_logging_never_raises_on_an_unwritable_path(self):
        blocker = Path(tempfile.mkdtemp()) / "file"
        blocker.write_text("x", encoding="utf-8")
        # A file where a directory should be cannot be written into; must not raise.
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertIsNone(applog.append("0.0.1", "boom", base_dir=blocker))
        self.assertIn("could not write", captured.getvalue())

    def test_startup_record_mentions_port_and_pid(self):
        record = applog.startup_record("0.4.0", "127.0.0.1", 8123, "http://localhost:8123")
        self.assertIn("port=8123", record)
        self.assertIn("pid=", record)
        self.assertIn("v0.4.0", f"v{applog.version_slug('0.4.0')}")


if __name__ == "__main__":
    unittest.main()
