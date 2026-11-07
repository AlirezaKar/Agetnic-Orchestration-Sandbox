"""The self-audit: the engine, its scorecard, and the routes that serve it.

Three things are worth testing here, and only three: that the audit passes on a
healthy tree (otherwise it is noise), that it *fails loudly* when a part is
broken (otherwise it is decoration), and that it never leaves anything behind
(no run records, no state, no files in the user's log directory).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.orchestrator import audit
from app.orchestrator.audit import (
    CHECKS,
    LEVELS,
    Result,
    Spec,
    catalogue,
    load_latest,
    run_audit,
    scorecard_markdown,
    write_scorecard,
)

from tests.test_orchestrator import FakeModel, FakeSearchChain


def audit_sync(*args, **kwargs):
    """The audit is async; these tests are not."""
    return asyncio.run(run_audit(*args, **kwargs))


def settings_for(tmp: str, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        files_dir=str(Path(tmp) / "files"),
        memory_dir=str(Path(tmp) / "memory"),
        orchestrator_enabled=True,
        orchestrator_timeout=2.0,
        orchestrator_planner="heuristic",
        orchestrator_evaluate=True,
        orchestrator_optimize=True,
        orchestrator_review=True,
        orchestrator_review_rounds=1,
        orchestrator_log_runs=False,
    )
    base.update(overrides)
    return Settings(**base)


class AuditEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_for(self.tmp.name)
        self.work = Path(self.tmp.name) / "audit"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------ catalogue
    def test_catalogue_is_ordered_by_level(self) -> None:
        checks = catalogue()
        self.assertEqual([item["id"] for item in checks], [spec.id for spec in CHECKS])
        order = [level for level, _ in LEVELS]
        ranks = [order.index(item["level"]) for item in checks]
        self.assertEqual(ranks, sorted(ranks), "checks should run level by level")

    def test_every_check_is_described(self) -> None:
        ids = [item["id"] for item in catalogue()]
        self.assertEqual(len(ids), len(set(ids)), "check ids must be unique")
        for item in catalogue():
            self.assertIn(item["level"], [level for level, _ in LEVELS])
            self.assertTrue(item["title"], item["id"])
            self.assertTrue(item["proves"], item["id"])
            self.assertTrue(item["needs"], item["id"])

    def test_checks_cover_every_level(self) -> None:
        levels = {item["level"] for item in catalogue()}
        for level, _ in LEVELS:
            self.assertIn(level, levels, f"level '{level}' has no checks")

    # ------------------------------------------------------------- a run
    def test_offline_audit_passes(self) -> None:
        card = audit_sync(self.settings, log_dir=self.work)
        failed = [row["id"] for row in card["checks"] if row["status"] == "fail"]
        self.assertEqual(failed, [], f"the audit failed: {failed}")
        self.assertIn(card["status"], ("pass", "warn"))
        self.assertEqual(card["summary"]["total"], len(catalogue()))
        self.assertEqual(
            card["summary"]["pass"] + card["summary"]["warn"]
            + card["summary"]["fail"] + card["summary"]["skip"],
            card["summary"]["total"],
        )

    def test_by_level_totals_match_the_checks(self) -> None:
        card = audit_sync(self.settings, log_dir=self.work)
        total = sum(row["total"] for row in card["by_level"])
        self.assertEqual(total, len(card["checks"]))
        for row in card["by_level"]:
            self.assertEqual(row["pass"] + row["warn"] + row["fail"] + row["skip"], row["total"])

    def test_every_check_reports_metrics_or_findings(self) -> None:
        card = audit_sync(self.settings, log_dir=self.work)
        for row in card["checks"]:
            self.assertIsInstance(row["metrics"], dict)
            self.assertIsInstance(row["findings"], list)
            self.assertTrue(row["detail"], row["id"])
            self.assertGreaterEqual(row["ms"], 0)

    def test_live_checks_skip_without_a_provider(self) -> None:
        card = audit_sync(self.settings, live=True, models=None, log_dir=self.work)
        live_rows = [row for row in card["checks"] if row["live"]]
        self.assertTrue(live_rows)
        for row in live_rows:
            self.assertEqual(row["status"], "skip")
            self.assertIn("key", row["detail"].lower())
        self.assertTrue(card["live_requested"])

    def test_the_caller_supplies_the_version(self) -> None:
        card = audit_sync(self.settings, only=["plan.waves"], version="9.9.9", log_dir=self.work)
        self.assertEqual(card["version"], "9.9.9")
        written = write_scorecard(card, self.work)
        self.assertIn("9.9.9", Path(written["markdown"]).read_text(encoding="utf-8"))

    def test_only_runs_the_named_checks(self) -> None:
        card = audit_sync(self.settings, only=["plan.waves", "gate.rules"], log_dir=self.work)
        self.assertEqual([row["id"] for row in card["checks"]], ["plan.waves", "gate.rules"])
        self.assertEqual(card["summary"]["total"], 2)

    def test_a_broken_check_is_reported_not_raised(self) -> None:
        async def boom(_: audit.AuditContext) -> Result:
            raise RuntimeError("deliberate")

        spec = Spec(
            id="zz.boom",
            level="plan",
            title="Deliberately broken",
            proves="the audit survives its own bugs",
            needs="nothing",
            live=False,
            fn=boom,
        )
        CHECKS.append(spec)
        try:
            card = audit_sync(self.settings, only=["zz.boom"], log_dir=self.work)
        finally:
            CHECKS.remove(spec)
        row = card["checks"][0]
        self.assertEqual(row["status"], "fail")
        self.assertIn("RuntimeError", row["detail"])
        self.assertTrue(any("deliberate" in line for line in row["findings"]))
        self.assertEqual(card["status"], "fail")

    def test_a_warn_makes_the_carda_warn_not_a_fail(self) -> None:
        async def meh(_: audit.AuditContext) -> Result:
            return audit.warn("almost", ["one finding"], score=0.9)

        spec = Spec(id="zz.warn", level="plan", title="Meh", proves="warnings are not failures",
                    needs="nothing", live=False, fn=meh)
        CHECKS.append(spec)
        try:
            card = audit_sync(self.settings, only=["zz.warn"], log_dir=self.work)
        finally:
            CHECKS.remove(spec)
        self.assertEqual(card["status"], "warn")
        self.assertEqual(card["summary"]["warn"], 1)

    def test_the_audit_writes_no_run_records(self) -> None:
        """An audit is a read: the user's run log must gain nothing."""
        from app import logs as app_logs

        target = Path(app_logs.log_dir()) / "orchestrator"
        before = sorted(target.glob("*.json")) if target.exists() else []
        card = audit_sync(self.settings)  # no log_dir → a temporary scratch dir
        after = sorted(target.glob("*.json")) if target.exists() else []
        self.assertTrue(card["sandboxed"])
        self.assertEqual(before, after, "an audit must not log runs")

    # --------------------------------------------------------- the scorecard
    def test_scorecard_is_written_as_json_and_markdown(self) -> None:
        card = audit_sync(self.settings, log_dir=self.work)
        paths = write_scorecard(card, self.work)
        written = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
        self.assertEqual(written["audit_id"], card["audit_id"])
        self.assertEqual(written["checks"], card["checks"])
        self.assertEqual(load_latest(self.work)["audit_id"], card["audit_id"])
        markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertIn(card["audit_id"], markdown)
        self.assertIn("| Check | Level | Status | ms | Detail |", markdown)
        for row in card["checks"]:
            self.assertIn(row["id"], markdown)

    def test_load_latest_is_none_before_the_first_run(self) -> None:
        self.assertIsNone(load_latest(self.work / "nowhere"))

    def test_markdown_escapes_pipes_in_details(self) -> None:
        card = {
            "audit_id": "audit-x",
            "version": "0.0.0",
            "status": "pass",
            "summary": {"pass": 1},
            "checks": [{"id": "a", "level": "plan", "status": "pass", "ms": 1, "detail": "a|b"}],
        }
        self.assertIn("a\\|b", scorecard_markdown(card))

    def test_scorecard_survives_a_json_roundtrip(self) -> None:
        card = audit_sync(self.settings, log_dir=self.work)
        again = json.loads(json.dumps(card, ensure_ascii=False))
        self.assertEqual(again, card)


class AuditApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_for(self.tmp.name)
        get_settings.cache_clear()
        main_module.reset_store()
        main_module.reset_memory()
        self.patches = [
            mock.patch.object(main_module, "get_settings", lambda: self.settings),
            mock.patch.object(main_module, "build_chain", lambda settings=None: FakeModel()),
            mock.patch.object(
                main_module, "build_search_chain", lambda settings=None: FakeSearchChain()
            ),
            mock.patch.object(main_module, "_audit_dir", lambda: Path(self.tmp.name) / "audit"),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(main_module.app)

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        main_module.reset_store()
        main_module.reset_memory()
        get_settings.cache_clear()
        self.tmp.cleanup()

    def test_catalogue_route_explains_every_check(self) -> None:
        body = self.client.get("/audit").json()
        self.assertEqual(body["total"], len(catalogue()))
        self.assertTrue(body["live_available"])
        self.assertIsNone(body["last"])
        for item in body["checks"]:
            self.assertIn(item["level"], [level for level, _ in LEVELS])
            self.assertTrue(item["proves"])
            self.assertTrue(item["needs"])
            self.assertIn("live", item)

    def test_run_returns_a_scorecard_and_writes_it(self) -> None:
        response = self.client.post("/audit/run", json={})
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertIn(card["status"], ("pass", "warn", "fail"))
        self.assertEqual(card["summary"]["total"], len(catalogue()))
        for key in ("json", "markdown", "latest"):
            self.assertTrue(Path(card["files"][key]).exists(), key)
        self.assertTrue(card["live_available"])
        self.assertEqual(card["version"], main_module.VERSION)

    def test_last_is_404_until_the_first_run(self) -> None:
        missing = self.client.get("/audit/last")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"], "no_audit")
        card = self.client.post("/audit/run", json={}).json()
        found = self.client.get("/audit/last")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["audit_id"], card["audit_id"])

    def test_run_accepts_only(self) -> None:
        card = self.client.post("/audit/run", json={"only": ["plan.waves"]}).json()
        self.assertEqual([row["id"] for row in card["checks"]], ["plan.waves"])

    def test_stream_emits_start_check_and_result(self) -> None:
        events: list[str] = []
        data: list[dict] = []
        with self.client.stream("GET", "/audit/stream") as response:
            for line in response.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                elif line.startswith("data:"):
                    data.append(json.loads(line.split(":", 1)[1]))
        self.assertEqual(events[0], "open")
        self.assertEqual(events[-1], "result")
        self.assertEqual(events.count("start"), len(catalogue()))
        self.assertEqual(events.count("check"), len(catalogue()))
        card = data[-1]
        self.assertEqual(card["summary"]["total"], len(catalogue()))
        first_check = next(item for item in data if "status" in item and "id" in item)
        for key in ("id", "level", "title", "status", "ms", "detail", "metrics", "findings"):
            self.assertIn(key, first_check)

    def test_live_run_degrades_when_the_provider_is_missing(self) -> None:
        def broken(settings=None):
            raise RuntimeError("no key")

        with mock.patch.object(main_module, "build_chain", broken):
            self.assertEqual(main_module._audit_ports(self.settings), (None, None))

    def test_live_run_skips_its_live_checks_when_unavailable(self) -> None:
        def broken(settings=None):
            raise RuntimeError("no key")

        with mock.patch.object(main_module, "build_chain", broken):
            card = self.client.post("/audit/run", json={"live": True}).json()
        live_rows = [row for row in card["checks"] if row["live"]]
        self.assertTrue(live_rows)
        self.assertTrue(all(row["status"] == "skip" for row in live_rows))
        self.assertFalse(card["environment"]["live_models"])


if __name__ == "__main__":
    unittest.main()
