"""The standalone ARAS smoke probe (scripts/aras_smoke.py).

The script must stay stdlib-only and import nothing from ``app`` — it is
meant to be copied next to any .env and run where the tunnel terminates.
Everything here is offline: parsing, verdicts, the aggregate diagnosis and
the port check against a real local socket.
"""

from __future__ import annotations

import socket
import unittest

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
