"""Program logs: files, rotation, redaction, request lines, tracebacks, process data.

These tests write into a temporary directory, so they never touch the real
``logs/`` and never need a network.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import logs
from scripts import logs as log_cli


class LogTestCase(unittest.TestCase):
    """Fresh logging configuration pointed at a throwaway directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.env_backup = {k: os.environ.get(k) for k in ("LOG_DIR", "LOG_LEVEL")}
        os.environ["LOG_DIR"] = str(self.dir)
        self.summary = logs.setup_logging(self.dir, level="INFO", force=True, console=False)

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for name in ("agetnic.requests",):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        logs._CONFIGURED = False
        logs._LOGGERS_WITH_FILE_HANDLERS.clear()
        for name, value in self.env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def read(self, name: str) -> str:
        path = self.dir / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


class FileLayoutTests(LogTestCase):
    def test_creates_the_expected_files(self):
        self.assertTrue(self.summary["writable"])
        for name in ("agetnic.log", "error.log", "requests.log"):
            self.assertTrue((self.dir / name).exists(), name)
        self.assertTrue((self.dir / "archive").is_dir())

    def test_paths_are_reported_for_the_banner(self):
        self.assertEqual(self.summary["dir"], self.dir)
        self.assertTrue(self.summary["files"]["app"].endswith("agetnic.log"))
        self.assertEqual(self.summary["level"], "INFO")

    def test_setup_is_idempotent(self):
        before = len(logging.getLogger().handlers)
        logs.setup_logging(self.dir, console=False)
        self.assertEqual(len(logging.getLogger().handlers), before, "handlers were added twice")

    def test_unwritable_directory_degrades_to_console(self):
        blocker = Path(tempfile.mkdtemp()) / "file"
        blocker.write_text("x", encoding="utf-8")
        summary = logs.setup_logging(blocker, force=True, console=False)
        self.assertFalse(summary["writable"])

    def test_log_dir_from_environment(self):
        self.assertEqual(logs.log_dir(), self.dir)
        os.environ["LOG_DIR"] = str(self.dir / "custom")
        self.assertEqual(logs.log_dir(), self.dir / "custom")


class AppLogContentTests(LogTestCase):
    def test_info_goes_to_app_log_only(self):
        logs.get_logger("api").info("hello from the api")
        logging.getLogger().handlers[0].flush()
        self.assertIn("hello from the api", self.read("agetnic.log"))
        self.assertNotIn("hello from the api", self.read("error.log"))

    def test_warnings_and_errors_reach_error_log(self):
        log = logs.get_logger("api")
        log.warning("model not configured")
        log.error("provider exploded")
        self.assertIn("model not configured", self.read("error.log"))
        self.assertIn("provider exploded", self.read("error.log"))

    def test_traceback_is_written(self):
        try:
            raise ValueError("boom with context")
        except ValueError:
            logs.get_logger("crash").exception("unhandled error on POST /chat")

        body = self.read("error.log")
        self.assertIn("unhandled error on POST /chat", body)
        self.assertIn("ValueError: boom with context", body)
        self.assertIn("Traceback (most recent call last)", body)

    def test_lines_carry_timestamp_level_pid_and_logger(self):
        logs.get_logger("agent").info("step 1")
        line = [ln for ln in self.read("agetnic.log").splitlines() if "step 1" in ln][0]
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO\s+pid=\d+ agetnic\.agent: step 1$")

    def test_file_entries_are_flushed_immediately(self):
        logs.get_logger("agent").info("visible right away")
        self.assertIn("visible right away", self.read("agetnic.log"))


class RedactionTests(LogTestCase):
    def test_secrets_never_reach_the_file(self):
        os.environ["GROQ_API_KEY"] = "gsk_thisIsARealLookingKey1234567890"
        try:
            logs.get_logger("api").info(
                "calling provider with %s", os.environ["GROQ_API_KEY"]
            )
        finally:
            os.environ.pop("GROQ_API_KEY", None)

        body = self.read("agetnic.log")
        self.assertNotIn("thisIsARealLookingKey", body)
        self.assertIn(logs.REDACTED, body)

    def test_common_key_shapes_are_scrubbed(self):
        logs.get_logger("api").info("headers: Authorization: Bearer abcdefghijklmnop123456")
        logs.get_logger("api").info("key=sk-proj-abcdefghijklmnopqrstuvwxyz")
        body = self.read("agetnic.log")
        self.assertNotIn("abcdefghijklmnop123456", body)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", body)

    def test_redact_handles_plain_text(self):
        self.assertEqual(logs.redact("nothing to hide"), "nothing to hide")
        self.assertIn(logs.REDACTED, logs.redact("api_key=verysecretvalue"))
        self.assertEqual(logs.redact(""), "")


class RequestLoggingTests(LogTestCase):
    def make_client(self, handler_raises: bool = False) -> TestClient:
        app = FastAPI()

        @app.get("/ok")
        async def ok():
            return {"ok": True}

        @app.get("/bad")
        async def bad():
            return {"error": "nope"}

        if handler_raises:

            @app.get("/crash")
            async def crash():
                raise RuntimeError("handler exploded")

        app.add_middleware(logs.RequestLoggingMiddleware)
        return TestClient(app, raise_server_exceptions=False)

    def test_each_request_writes_one_line(self):
        client = self.make_client()
        client.get("/ok")
        client.get("/bad")
        lines = [ln for ln in self.read("requests.log").splitlines() if " -> " in ln]
        self.assertEqual(len(lines), 2, lines)
        self.assertRegex(lines[0], r"GET /ok -> 200 \d+\.\dms req=[0-9a-f]+ client=testclient")
        self.assertIn("GET /bad -> 200", lines[1])

    def test_request_id_is_returned_to_the_caller(self):
        client = self.make_client()
        response = client.get("/ok", headers={"x-request-id": "abc123"})
        self.assertEqual(response.headers["x-request-id"], "abc123")
        self.assertIn("req=abc123", self.read("requests.log"))

    def test_four_xx_is_a_warning(self):
        client = self.make_client()
        client.get("/missing")
        self.assertIn("GET /missing -> 404", self.read("requests.log"))
        self.assertIn("GET /missing -> 404", self.read("error.log"))

    def test_unhandled_exception_writes_a_traceback(self):
        client = self.make_client(handler_raises=True)
        response = client.get("/crash")
        self.assertEqual(response.status_code, 500)
        body = self.read("error.log")
        self.assertIn("RuntimeError: handler exploded", body)
        self.assertIn("Traceback (most recent call last)", body)
        self.assertIn("/crash", self.read("requests.log"))

    def test_duration_and_size_are_recorded(self):
        client = self.make_client()
        client.get("/ok")
        line = [ln for ln in self.read("requests.log").splitlines() if "GET /ok" in ln][0]
        self.assertRegex(line, r"bytes=[1-9]\d*")


class RotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        logs._CONFIGURED = False
        logs._LOGGERS_WITH_FILE_HANDLERS.clear()
        self._tmp.cleanup()

    def test_small_limit_rotates_into_backups(self):
        logs.setup_logging(self.dir, level="INFO", rotation_mb=0.001, backups=3, force=True, console=False)
        log = logs.get_logger("bulk")
        for index in range(400):
            log.info("line %s %s", index, "x" * 60)
        for handler in logging.getLogger().handlers:
            handler.flush()
        backups = sorted(self.dir.glob("agetnic.log.*"))
        self.assertTrue(backups, "no rotation happened")
        self.assertLessEqual(len(backups), 3)

    def test_terminal_rotation_keeps_recent_files(self):
        terminal = self.dir / "terminal.log"
        terminal.write_text("x" * 2048, encoding="utf-8")
        for index in range(4):
            logs._rotate_raw(terminal, self.dir / "archive", limit_mb=0.001, keep=2)
            terminal.write_text("x" * 2048, encoding="utf-8")
        archived = sorted((self.dir / "archive").glob("terminal-*.log"))
        self.assertTrue(archived)
        self.assertLessEqual(len(archived), 2)


class TerminalCaptureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._stdout, self._stderr = sys.stdout, sys.stderr

    def tearDown(self):
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self._tmp.cleanup()

    def test_stdout_and_stderr_are_mirrored(self):
        path = logs.capture_terminal(self.dir)
        self.assertIsNotNone(path)
        try:
            print("a line from print()")
            print("an error line", file=sys.stderr)
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = self._stdout, self._stderr
        body = (self.dir / "terminal.log").read_text(encoding="utf-8")
        self.assertIn("a line from print()", body)
        self.assertIn("an error line", body)

    def test_capture_is_not_applied_twice(self):
        sys.stdout = logs.Tee(sys.stdout, io.StringIO())
        # the second call must return the same path without nesting another tee
        path = logs.capture_terminal(self.dir)
        self.assertTrue(isinstance(sys.stdout, logs.Tee))
        self.assertEqual(path, self.dir / "terminal.log")

    def test_tee_keeps_stream_attributes(self):
        class Stream(io.StringIO):
            encoding = "utf-8"

        stream = Stream()
        tee = logs.Tee(stream, io.StringIO())
        tee.write("hi")
        self.assertEqual(stream.getvalue(), "hi")
        tee.flush()
        self.assertEqual(tee.encoding, "utf-8")


class ProcessDataTests(LogTestCase):
    def test_snapshot_has_the_fields_you_need_when_something_dies(self):
        os.environ["SOME_API_KEY"] = "super-secret-value-123456"
        try:
            snapshot = logs.process_snapshot("9.9.9")
        finally:
            os.environ.pop("SOME_API_KEY", None)
        for key in ("timestamp", "pid", "ppid", "cwd", "python", "platform", "git", "argv"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["version"], "9.9.9")
        self.assertEqual(snapshot["pid"], os.getpid())
        self.assertNotIn("super-secret-value", json.dumps(snapshot))

    def test_startup_is_appended_as_json_lines(self):
        logs.record_startup("9.9.9", port=8123, host="127.0.0.1")
        logs.record_startup("9.9.9", port=8124, host="127.0.0.1")
        lines = self.read("process.log").strip().splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[-1])
        self.assertEqual(record["port"], 8124)
        self.assertEqual(record["version"], "9.9.9")

    def test_startup_is_also_in_the_app_log(self):
        logs.record_startup("9.9.9", port=8123)
        self.assertIn("start version=9.9.9", self.read("agetnic.log"))

    def test_startup_creates_a_nested_log_directory(self):
        nested = Path(tempfile.mkdtemp()) / "nested" / "logs"
        os.environ["LOG_DIR"] = str(nested)
        try:
            logs.setup_logging(nested, force=True, console=False)
            snapshot = logs.record_startup("9.9.9")
            self.assertEqual(snapshot["version"], "9.9.9")
            self.assertTrue((nested / "process.log").exists())
        finally:
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()
            os.environ["LOG_DIR"] = str(self.dir)
            logs._CONFIGURED = False
            logs.setup_logging(self.dir, level="INFO", force=True, console=False)


class CrashCaptureTests(LogTestCase):
    def test_faulthandler_file_is_created(self):
        path = logs.enable_faulthandler(self.dir)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists() or path.parent.exists())

    def test_exception_hook_logs_and_delegates(self):
        original = sys.excepthook
        seen = []
        try:
            logs.install_exception_hooks()
            sys.excepthook(TypeError, TypeError("hook test"), None)
            sys.excepthook = original
        finally:
            sys.excepthook = original
            seen = self.read("error.log").splitlines()
        # the logger writes CRITICAL "uncaught exception"; the default hook then prints
        self.assertTrue(any("hook test" in line for line in seen))


class LogReaderTests(LogTestCase):
    def test_tail_returns_the_last_lines_of_a_big_file(self):
        path = self.dir / "agetnic.log"
        path.write_text("\n".join(f"line {i}" for i in range(5000)) + "\n", encoding="utf-8")
        lines = logs.tail(path, 5)
        self.assertEqual(lines, ["line 4995", "line 4996", "line 4997", "line 4998", "line 4999"])

    def test_tail_of_a_missing_file_is_empty(self):
        self.assertEqual(logs.tail(self.dir / "nope.log"), [])

    def test_resolve_log_file_accepts_short_names(self):
        self.assertEqual(logs.resolve_log_file("error", self.dir), self.dir / "error.log")
        self.assertEqual(logs.resolve_log_file("requests.log", self.dir), self.dir / "requests.log")

    def test_resolve_log_file_refuses_to_escape(self):
        with self.assertRaises(ValueError):
            logs.resolve_log_file("../secrets.log", self.dir)

    def test_read_errors_regroups_records(self):
        logs.get_logger("api").warning("first problem")
        try:
            raise RuntimeError("second problem")
        except RuntimeError:
            logs.get_logger("crash").exception("context")
        records = logs.read_errors(self.dir / "error.log", limit=5)
        self.assertEqual(len(records), 2, records)
        self.assertIn("Traceback", records[-1])

    def test_list_logs_reports_sizes(self):
        logs.get_logger("api").info("something")
        entries = {entry["name"]: entry for entry in logs.list_logs(self.dir)}
        self.assertIn("agetnic.log", entries)
        self.assertGreater(entries["agetnic.log"]["bytes"], 0)


class LogCliTests(LogTestCase):
    """The CLI is exercised for real; its output is captured so the suite stays quiet."""

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = log_cli.main(["--log-dir", str(self.dir), *argv])
        return code, buffer.getvalue()

    def test_path_and_list(self):
        code, out = self.run_cli("path")
        self.assertEqual(code, 0)
        self.assertIn(str(self.dir), out)

        logs.get_logger("api").warning("visible")
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("agetnic.log", out)

    def test_tail_error_and_requests(self):
        logs.get_logger("api").warning("cli warning")
        self.assertEqual(self.run_cli("tail", "error", "-n", "5")[0], 0)
        code, out = self.run_cli("errors", "-l", "5")
        self.assertEqual(code, 0)
        self.assertIn("cli warning", out)

        (self.dir / "requests.log").write_text(
            "2026-10-31 12:00:00,000 INFO     pid=1 agetnic.requests: GET /health -> 200 1.5ms req=abc client=1.2.3.4:5 bytes=42\n"
            "2026-10-31 12:00:01,000 WARNING  pid=1 agetnic.requests: POST /chat -> 400 9.0ms req=def client=1.2.3.4:6 bytes=99\n",
            encoding="utf-8",
        )
        code, out = self.run_cli("requests")
        self.assertEqual(code, 0, out)
        self.assertIn("2 requests", out)
        self.assertIn("/chat", out)

    def test_process_command_needs_a_record(self):
        self.assertEqual(self.run_cli("process")[0], 1)
        logs.record_startup("9.9.9", port=8000)
        code, out = self.run_cli("process")
        self.assertEqual(code, 0)
        self.assertIn("port=8000", out)

    def test_no_command_prints_help(self):
        self.assertEqual(self.run_cli()[0], 1)


class ChangelogContractTests(unittest.TestCase):
    """``logs/changelog`` still holds one markdown file per version."""

    def setUp(self):
        from app.main import VERSION

        self.version = VERSION
        self.root = Path(__file__).resolve().parents[1] / "logs" / "changelog"

    def test_one_file_per_version(self):
        versions = sorted(path.stem for path in self.root.glob("v*.md"))
        self.assertGreaterEqual(len(versions), 4)
        for stem in versions:
            self.assertRegex(stem, r"^v\d+\.\d+\.\d+$")

    def test_current_version_is_documented(self):
        path = self.root / f"v{self.version}.md"
        self.assertTrue(path.is_file(), f"missing logs/changelog/v{self.version}.md")
        self.assertIn("## Verify", path.read_text(encoding="utf-8"))

    def test_index_links_every_version(self):
        index = (self.root / "README.md").read_text(encoding="utf-8")
        for path in self.root.glob("v*.md"):
            self.assertIn(path.name, index, f"index does not link {path.name}")

    def test_runtime_logs_are_git_ignored(self):
        gitignore = (self.root.parents[1] / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("logs/*.log", gitignore)


if __name__ == "__main__":
    unittest.main()
