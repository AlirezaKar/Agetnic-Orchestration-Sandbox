"""
tests.test_api — HTTP layer of the test harness (FastAPI TestClient).
"""

import io

import pytest
from fastapi.testclient import TestClient

from backend.api import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["llm_provider"] in ("mock", "groq")


def test_modes_catalog(client):
    res = client.get("/api/modes")
    body = res.json()
    assert [m["id"] for m in body["modes"]] == [
        "auto", "document", "web_search", "general", "number",
    ]
    assert {a["id"] for a in body["agents"]} == {
        "document", "web_search", "general", "number",
    }


def test_frontend_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Agetnic Orchestration Sandbox" in res.text
    res = client.get("/assets/js/app.js")
    assert res.status_code == 200


def test_chat_auto_mode_end_to_end(client):
    res = client.post("/api/chat", json={
        "message": "Search the web for AI news and analyze the numbers in the results",
        "mode": "auto",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["plan"] is not None
    assert len(body["plan"]["tasks"]) >= 2
    assert any(s["type"] == "synthesis" for s in body["steps"])

    # The full trace must be retrievable for debugging/evaluation.
    trace_res = client.get(f"/api/traces/{body['run_id']}")
    assert trace_res.status_code == 200
    trace = trace_res.json()
    assert trace["input"] == "Search the web for AI news and analyze the numbers in the results"
    assert trace["thinking"]
    assert trace["steps"]
    assert trace["output"] == body["output"]


def test_chat_manual_modes(client):
    for mode, marker in [("general", ""), ("number", "9*(3+1)")]:
        res = client.post("/api/chat", json={"message": f"help with {marker} please", "mode": mode})
        assert res.status_code == 200
        assert res.json()["mode_used"] == mode


def test_document_upload_and_rag_query(client):
    # 1. Upload a text document.
    upload = client.post(
        "/api/documents",
        files={"file": ("notes.txt", io.BytesIO(b"The launch date is March 15. The budget is 50000 dollars."), "text/plain")},
    )
    assert upload.status_code == 200
    assert upload.json()["status"] == "indexed"

    listing = client.get("/api/documents").json()
    assert listing["documents"]

    # 2. Ask about it in document mode.
    res = client.post("/api/chat", json={
        "message": "According to the uploaded documents, what is the launch date?",
        "mode": "document",
    })
    body = res.json()
    assert body["success"]
    task_steps = [s for s in body["steps"] if s["type"] == "agent_task"]
    assert task_steps and task_steps[0]["data"].get("retrieved_chunks")


def test_document_upload_rejects_bad_files(client):
    res = client.post(
        "/api/documents",
        files={"file": ("evil.exe", io.BytesIO(b"binary"), "application/octet-stream")},
    )
    assert res.status_code == 400


def test_traces_listing_and_404(client):
    res = client.get("/api/traces")
    assert res.status_code == 200
    assert isinstance(res.json()["traces"], list)

    res = client.get("/api/traces/run_missing")
    assert res.status_code == 404


def test_session_clearing(client):
    res = client.request("DELETE", "/api/sessions/sess_test123")
    assert res.status_code == 200
    assert res.json()["status"] == "cleared"
