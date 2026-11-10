"""The standalone ARAS smoke probe (scripts/aras_smoke.py).

The script must stay stdlib-only and import nothing from ``app`` — it is
meant to be copied next to any .env and run where the tunnel terminates.
Everything here is offline: parsing, verdicts, the aggregate diagnosis and
the port check against a real local socket.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts import aras_smoke


class EnvParsingTests(unittest.TestCase):
    def test_parse_env_reads_the_shapes_people_actually_write(self) -> None:
        env = aras_smoke.parse_env(
            "# comment\n"
            "ARAS_BASE_URL=http://127.0.0.1\n"
            "ARAS_TOKEN='shared-secret'\n"
            'ARAS_ROUTER_TOKEN="router-secret"\n'
            "export ARAS_TENANT=alireza-kalaie\n"
            "ARAS_TIMEOUT=60 # trailing comment\n"
            "BROKEN LINE WITHOUT EQUALS\n"
        )
        self.assertEqual(env["ARAS_BASE_URL"], "http://127.0.0.1")
        self.assertEqual(env["ARAS_TOKEN"], "shared-secret")
        self.assertEqual(env["ARAS_ROUTER_TOKEN"], "router-secret")
        self.assertEqual(env["ARAS_TENANT"], "alireza-kalaie")
        self.assertEqual(env["ARAS_TIMEOUT"], "60")

    def test_token_falls_back_to_the_shared_one(self) -> None:
        env = {"ARAS_TOKEN": "shared", "ARAS_ROUTER_TOKEN": "mine"}
        self.assertEqual(aras_smoke.token_for(env, "router"), "mine")
        self.assertEqual(aras_smoke.token_for(env, "web"), "shared")
        self.assertEqual(aras_smoke.token_for({}, "web"), "")

    def test_base_host_strips_scheme_port_and_path(self) -> None:
        self.assertEqual(aras_smoke.base_host({}), "127.0.0.1")
        self.assertEqual(aras_smoke.base_host({"ARAS_BASE_URL": "http://10.0.0.5:9999/x"}),
                         "10.0.0.5")
        self.assertEqual(aras_smoke.base_host({"ARAS_BASE_URL": "http://localhost"}),
                         "localhost")


class VerdictTests(unittest.TestCase):
    def test_auth_classification_tells_token_problems_apart_from_body_problems(self) -> None:
        ok, note = aras_smoke.classify_auth(401)
        self.assertFalse(ok)
        self.assertIn("TOKEN REJECTED", note)
        ok, _ = aras_smoke.classify_auth(403)
        self.assertFalse(ok)
        # the empty probe body SHOULD be rejected — that still proves the token
        for status in (400, 404, 405, 422):
            ok, note = aras_smoke.classify_auth(status)
            self.assertTrue(ok, status)
        ok, note = aras_smoke.classify_auth(200)
        self.assertTrue(ok)
        ok, note = aras_smoke.classify_auth(503)
        self.assertFalse(ok)
        self.assertIn("token not the problem", note)

    def test_the_forbidden_orchestrator_port_is_never_probed(self) -> None:
        self.assertNotIn(8106, [service["port"] for service in aras_smoke.SERVICES])
        self.assertEqual(len(aras_smoke.SERVICES), 7)

    def test_the_script_imports_nothing_from_the_app(self) -> None:
        import ast
        from pathlib import Path

        source = Path(aras_smoke.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                self.assertFalse(name == "app" or name.startswith("app."),
                                 f"standalone script imports {name}")


class DiagnosisTests(unittest.TestCase):
    @staticmethod
    def result(**overrides):
        base = {"service": "router", "port": 8101, "port_open": True,
                "health": 200, "auth": 200, "auth_ok": True, "notes": []}
        base.update(overrides)
        return base

    def test_all_ports_closed_names_the_tunnel(self) -> None:
        results = [self.result(port_open=False, health=None, auth=None)
                   for _ in range(3)]
        lines = aras_smoke.diagnose(results)
        self.assertTrue(any("tunnel is not open" in line for line in lines), lines)

    def test_everything_answering_sick_names_the_proxy(self) -> None:
        results = [self.result(health=503, auth_ok=False) for _ in range(3)]
        lines = aras_smoke.diagnose(results)
        self.assertTrue(any("proxy" in line for line in lines), lines)

    def test_rejected_tokens_are_listed_by_service(self) -> None:
        results = [self.result(), self.result(service="web", auth=401, auth_ok=False)]
        lines = aras_smoke.diagnose(results)
        self.assertTrue(any("Rejected tokens: web" in line for line in lines), lines)

    def test_a_clean_bill_says_so(self) -> None:
        lines = aras_smoke.diagnose([self.result()])
        self.assertEqual(lines, ["All good: tunnel up, services healthy, tokens accepted."])


class SkipTests(unittest.TestCase):
    """A service with no token is skipped: it is shown, and it decides nothing."""

    @staticmethod
    def result(**overrides):
        base = {"service": "router", "port": 8101, "configured": True, "port_open": True,
                "health": 200, "auth": 200, "auth_ok": True, "notes": []}
        base.update(overrides)
        return base

    def skipped(self, **overrides):
        base = self.result(service="ocr", port=8102, configured=False, auth=None)
        del base["auth_ok"]  # the authenticated probe never ran
        base.update(overrides)
        return base

    def test_a_service_without_a_token_is_skipped_not_failed(self) -> None:
        results = [self.result(), self.skipped()]
        self.assertEqual([aras_smoke.verdict(r) for r in results], ["OK", "SKIP"])
        self.assertTrue(aras_smoke.all_good(results))
        lines = aras_smoke.diagnose(results)
        self.assertEqual(lines[0], "All good: tunnel up, services healthy, tokens accepted.")
        self.assertTrue(any("Skipped, no token in .env: ocr" in line for line in lines), lines)

    def test_a_closed_port_of_a_skipped_service_is_not_a_failure(self) -> None:
        results = [self.result(), self.skipped(port_open=False, health=None)]
        self.assertTrue(aras_smoke.all_good(results))
        lines = aras_smoke.diagnose(results)
        self.assertFalse(any("Closed ports" in line for line in lines), lines)

    def test_a_closed_port_of_a_configured_service_is_a_failure(self) -> None:
        results = [self.result(), self.result(service="web", port=8108, port_open=False,
                                              health=None, auth=None, auth_ok=False)]
        self.assertEqual(aras_smoke.verdict(results[1]), "FAIL")
        self.assertFalse(aras_smoke.all_good(results))
        lines = aras_smoke.diagnose(results)
        self.assertTrue(any("Closed ports: web:8108" in line for line in lines), lines)

    def test_nothing_configured_is_not_a_pass(self) -> None:
        results = [self.skipped(), self.skipped(service="web", port=8108)]
        self.assertFalse(aras_smoke.all_good(results))
        lines = aras_smoke.diagnose(results)
        self.assertTrue(any("No ARAS token in .env" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("All good") for line in lines), lines)

    def test_a_failure_is_never_summarised_as_all_good(self) -> None:
        # neither a closed port nor a rejected token: the summary must still name them
        results = [
            self.result(),
            self.result(service="web", port=8108, health=503, auth_ok=False),
            self.result(service="data", port=8107, auth=500, auth_ok=False),
        ]
        self.assertFalse(aras_smoke.all_good(results))
        lines = aras_smoke.diagnose(results)
        self.assertFalse(any(line.startswith("All good") for line in lines), lines)
        self.assertTrue(
            any("Failing: web (health 503), data (authenticated call HTTP 500)" in line
                for line in lines),
            lines,
        )


class _OneService(BaseHTTPRequestHandler):
    """Health without auth, one route that wants ``Bearer good-token``."""

    def log_message(self, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health/":
            status = 200
        elif self.headers.get("Authorization") == "Bearer good-token":
            status = 200
        else:
            status = 401
        body = b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RunTests(unittest.TestCase):
    """main() end to end against a real loopback listener: rows, summary, exit code."""

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OneService)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.closed_port = closed.getsockname()[1]
        closed.close()
        self.tmp = tempfile.TemporaryDirectory()
        services = [
            {"name": "router", "port": self.port, "health": "/health/",
             "method": "GET", "path": "/v1/things/", "body": None},
            {"name": "web", "port": self.closed_port, "health": "/health/",
             "method": "GET", "path": "/v1/things/", "body": None},
        ]
        patcher = mock.patch.object(aras_smoke, "SERVICES", services)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_with(self, env_text: str, *flags: str) -> tuple[int, str]:
        env = Path(self.tmp.name) / ".env"
        env.write_text(env_text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = aras_smoke.main(["--env", str(env), *flags])
        return code, out.getvalue()

    def test_the_configured_service_passes_and_the_tokenless_one_is_skipped(self) -> None:
        code, text = self.run_with("ARAS_ROUTER_TOKEN=good-token\n")
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"(?m)^OK +router ")
        self.assertRegex(text, r"(?m)^SKIP +web ")
        self.assertIn("All good", text)
        self.assertNotIn("FAIL", text)

    def test_a_rejected_token_fails_the_run(self) -> None:
        code, text = self.run_with("ARAS_ROUTER_TOKEN=wrong-token\n")
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"(?m)^FAIL +router .*auth=401")
        self.assertIn("Rejected tokens: router", text)
        self.assertNotIn("All good", text)

    def test_a_configured_service_behind_a_closed_port_fails_the_run(self) -> None:
        code, text = self.run_with("ARAS_TOKEN=good-token\n")  # the shared token configures both
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"(?m)^OK +router ")
        self.assertRegex(text, r"(?m)^FAIL +web .*port=CLOSED")
        self.assertNotIn("All good", text)

    def test_no_token_at_all_is_exit_one_with_the_reason(self) -> None:
        code, text = self.run_with("ARAS_BASE_URL=http://127.0.0.1\n")
        self.assertEqual(code, 1, text)
        self.assertIn("No ARAS token in .env", text)

    def test_json_output_is_one_document_and_never_holds_the_token(self) -> None:
        code, text = self.run_with("ARAS_ROUTER_TOKEN=good-token\n", "--json")
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertIs(report["ok"], True)
        self.assertEqual([r["verdict"] for r in report["results"]], ["OK", "SKIP"])
        self.assertNotIn("good-token", text)


class PortCheckTests(unittest.TestCase):
    def test_port_open_against_a_real_listener_and_a_closed_port(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        _, port = listener.getsockname()
        try:
            self.assertTrue(aras_smoke.port_open("127.0.0.1", port))
        finally:
            listener.close()
        self.assertFalse(aras_smoke.port_open("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
