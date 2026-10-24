"""
tests.test_trace — persistence of run traces (input/thinking/process/output).
"""

import json

from backend.orchestration.trace import TraceRecorder, list_trace_summaries, load_trace
from backend.schemas import RunTrace, TaskPlan, TaskSpec, TaskStatus, TraceStep, AgentName, new_id


def test_trace_round_trip(tmp_path, monkeypatch):
    import backend.orchestration.trace as trace_mod
    from dataclasses import replace

    monkeypatch.setattr(trace_mod, "settings", replace(trace_mod.settings, trace_dir=tmp_path))

    trace = RunTrace(
        run_id=new_id("run"),
        input="test goal",
        thinking=["analysis one", "analysis two"],
    )
    trace.plan = TaskPlan(
        analysis="pa",
        tasks=[TaskSpec(id="t1", agent=AgentName.GENERAL, description="do it")],
    )
    recorder = TraceRecorder(trace)
    recorder.add_step(
        TraceStep(
            id="task_t1",
            type="agent_task",
            status=TaskStatus.COMPLETED,
            input="do it",
            output="did it",
            thinking="thought about it",
            data={"agent": "general"},
        )
    )
    trace.finish("final answer", success=True)
    path = recorder.save()

    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # The four evaluation pillars must all be present:
    assert raw["input"] == "test goal"
    assert raw["thinking"] == ["analysis one", "analysis two"]
    assert raw["steps"][0]["output"] == "did it"
    assert raw["output"] == "final answer"

    # Loadable through the public loader.
    loaded = load_trace(trace.run_id)
    assert loaded is not None
    assert loaded.output == "final answer"

    # Indexed for the run-history listing.
    summaries = list_trace_summaries()
    assert any(s["run_id"] == trace.run_id for s in summaries)
    entry = next(s for s in summaries if s["run_id"] == trace.run_id)
    assert entry["success"] is True
    assert entry["agents_used"] == ["general"]


def test_load_unknown_trace(tmp_path, monkeypatch):
    import backend.orchestration.trace as trace_mod
    from dataclasses import replace

    monkeypatch.setattr(trace_mod, "settings", replace(trace_mod.settings, trace_dir=tmp_path))
    assert load_trace("run_doesnotexist") is None
    assert list_trace_summaries() == []
