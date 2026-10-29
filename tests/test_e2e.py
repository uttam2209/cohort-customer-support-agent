"""
End-to-end tests covering all four approval states:
  approved  — valid token, decision accepted
  rejected  — valid token, decision accepted
  timed-out — expired token → 400
  tampered  — modified token → 400
"""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent.graph import build_graph
from approval.queue import ApprovalQueue
from approval.tokens import create_token
from ui.app import app, get_graph, get_queue


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_db(tmp_path) -> str:
    """Seed a minimal DB with a return_initiated order so refund checks pass."""
    import os
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    schema = open(os.path.join(os.path.dirname(__file__), "../db/schema.sql")).read()
    conn.executescript(schema)
    conn.execute("INSERT INTO customers VALUES ('CUST-001', 'Alice', 'alice@example.com')")
    conn.execute("INSERT INTO orders VALUES ('ORD-001', 'CUST-001', 'Widget', 25.00, 'return_initiated')")
    conn.commit()
    conn.close()
    return db


class MockLLM:
    def __init__(self, responses: list):
        self._responses = iter(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return next(self._responses)


def _refund_llm(tool_call_id: str) -> MockLLM:
    """MockLLM that triggers a refund interrupt then returns a final reply."""
    return MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": tool_call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Decision processed."),
        ]
    )


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def _inject(graph, queue):
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_queue] = lambda: queue


def _trigger_refund(client: TestClient) -> dict:
    """POST /chat with a refund request; asserts interrupt and returns pending_approval."""
    r = client.post(
        "/chat", json={"customer_id": "CUST-001", "message": "Please refund ORD-001."}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] is None
    assert data["pending_approval"] is not None
    return data["pending_approval"]


# ── 1. Approved ───────────────────────────────────────────────────────────────


def test_e2e_approved(tmp_path):
    """Full flow: chat → interrupt → valid token → approved."""
    queue = ApprovalQueue()
    _inject(build_graph(_refund_llm("e2e-1"), queue, db_path=_make_db(tmp_path)), queue)
    client = TestClient(app)

    pending = _trigger_refund(client)
    r = client.post(
        f"/approvals/{pending['approval_id']}/decide",
        json={"decision": "approved", "token": pending["token"]},
    )

    assert r.status_code == 200
    assert queue.get_by_id(pending["approval_id"])["status"] == "approved"


# ── 2. Rejected ───────────────────────────────────────────────────────────────


def test_e2e_rejected(tmp_path):
    """Full flow: chat → interrupt → valid token → rejected."""
    queue = ApprovalQueue()
    _inject(build_graph(_refund_llm("e2e-2"), queue, db_path=_make_db(tmp_path)), queue)
    client = TestClient(app)

    pending = _trigger_refund(client)
    r = client.post(
        f"/approvals/{pending['approval_id']}/decide",
        json={"decision": "rejected", "token": pending["token"]},
    )

    assert r.status_code == 200
    assert queue.get_by_id(pending["approval_id"])["status"] == "rejected"


# ── 3. Timed-out ──────────────────────────────────────────────────────────────


def test_e2e_token_expired():
    """Decide with an expired token → 400."""
    queue = ApprovalQueue()
    queue.add("exp-id", "request_refund", {"order_id": "ORD-001"}, thread_id="CUST-001")
    _inject(build_graph(MockLLM([]), queue), queue)
    client = TestClient(app)

    expired_token = create_token({"approval_id": "exp-id"}, ttl_seconds=1)
    time.sleep(2)

    r = client.post(
        "/approvals/exp-id/decide",
        json={"decision": "approved", "token": expired_token},
    )

    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


# ── 4. Tampered ───────────────────────────────────────────────────────────────


def test_e2e_token_tampered():
    """Decide with a tampered token → 400."""
    queue = ApprovalQueue()
    queue.add("tamp-id", "request_refund", {"order_id": "ORD-001"}, thread_id="CUST-001")
    _inject(build_graph(MockLLM([]), queue), queue)
    client = TestClient(app)

    valid_token = create_token({"approval_id": "tamp-id"}, ttl_seconds=300)
    mid = len(valid_token) // 2
    tampered = valid_token[:mid] + ("X" if valid_token[mid] != "X" else "Y") + valid_token[mid + 1:]

    r = client.post(
        "/approvals/tamp-id/decide",
        json={"decision": "approved", "token": tampered},
    )

    assert r.status_code == 400
    assert "tampered" in r.json()["detail"].lower()
