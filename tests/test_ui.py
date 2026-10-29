import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage

import ui.app as _web
from agent.graph import build_graph
from approval.queue import ApprovalQueue
from approval.tokens import create_token
from ui.app import app, get_graph, get_queue


# ── Helpers ───────────────────────────────────────────────────────────────────


class MockLLM:
    def __init__(self, responses: list):
        self._responses = iter(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return next(self._responses)


class SlowMockLLM:
    """Sleeps for `sleep_seconds` before returning; simulates a slow or hung LLM."""

    def __init__(self, sleep_seconds: float, responses: list):
        self._sleep = sleep_seconds
        self._responses = iter(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        time.sleep(self._sleep)
        return next(self._responses)


def _make_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, email TEXT)"
    )
    conn.execute(
        "CREATE TABLE orders "
        "(order_id TEXT PRIMARY KEY, customer_id TEXT, product TEXT, amount REAL, status TEXT)"
    )
    conn.execute("INSERT INTO customers VALUES ('CUST-001', 'Alice', 'alice@example.com')")
    conn.execute(
        "INSERT INTO orders VALUES ('ORD-001', 'CUST-001', 'Widget', 25.00, 'return_initiated')"
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()
    _web._system_prompt = None  # reset between tests


def _inject(graph, queue):
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_queue] = lambda: queue


# ── Page rendering ────────────────────────────────────────────────────────────


def test_chat_page_returns_html():
    queue = ApprovalQueue()
    _inject(build_graph(MockLLM([]), queue), queue)
    r = TestClient(app).get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_approvals_page_returns_html():
    queue = ApprovalQueue()
    _inject(build_graph(MockLLM([]), queue), queue)
    r = TestClient(app).get("/approvals")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_approvals_page_cleans_up_stale_entries(tmp_path):
    """GET /approvals resumes expired threads and moves entries to the expired list."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c-stale",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Your request timed out."),
        ]
    )
    queue = ApprovalQueue()
    _inject(build_graph(llm, queue, db_path=_make_db(tmp_path)), queue)
    client = TestClient(app)

    # Trigger interrupt
    r1 = client.post("/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001"})
    assert r1.json()["pending_approval"] is not None
    approval_id = r1.json()["pending_approval"]["approval_id"]

    # Force-expire the queue entry
    queue._store[approval_id]["expires_at"] = time.time() - 1

    # Load the approvals page — cleanup should fire
    r2 = client.get("/approvals")
    assert r2.status_code == 200

    # Entry must be expired and thread unblocked
    assert queue.get_by_id(approval_id)["status"] == "expired"
    assert queue.get_pending() == []
    assert len(queue.get_expired()) == 1


# ── POST /chat ────────────────────────────────────────────────────────────────


def test_post_chat_lookup_returns_reply(tmp_path):
    """lookup_order → AUTO: endpoint returns a non-null reply, no pending_approval."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup_order",
                        "args": {"order_id": "ORD-001"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Your order is shipped."),
        ]
    )
    queue = ApprovalQueue()
    _inject(build_graph(llm, queue, db_path=_make_db(tmp_path)), queue)
    r = TestClient(app).post(
        "/chat", json={"customer_id": "CUST-001", "message": "Where is ORD-001?"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] is not None
    assert data["pending_approval"] is None


def test_post_chat_refund_returns_pending_approval(tmp_path):
    """request_refund → GATED: endpoint returns pending_approval, reply is None."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c2",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    queue = ApprovalQueue()
    _inject(build_graph(llm, queue, db_path=_make_db(tmp_path)), queue)
    r = TestClient(app).post(
        "/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] is None
    assert data["pending_approval"]["approval_id"] == "c2"
    assert len(queue.get_pending()) == 1


def test_second_message_while_interrupted_returns_friendly_error(tmp_path):
    """Sending a message on an interrupted thread returns a reply, not a 500."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c-dup",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    queue = ApprovalQueue()
    # _make_db creates CUST-001 / ORD-001 — must match the customer_id below.
    _inject(build_graph(llm, queue, db_path=_make_db(tmp_path)), queue)
    client = TestClient(app)

    # First request — thread is now interrupted.
    r1 = client.post("/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001"})
    assert r1.status_code == 200
    assert r1.json()["pending_approval"] is not None

    # Second request on same thread — must not 500.
    r2 = client.post("/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001 again"})
    assert r2.status_code == 200
    data = r2.json()
    assert data["reply"] is not None
    assert "pending approval" in data["reply"].lower()


# ── POST /approvals/{id}/decide ───────────────────────────────────────────────


def test_decide_approved_resolves_and_resumes(tmp_path):
    """Approving a pending refund resolves the queue entry and resumes the graph."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c3",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Your refund has been approved."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))
    _inject(graph, queue)
    client = TestClient(app)

    # Trigger interrupt — capture token from response
    r1 = client.post("/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001"})
    token = r1.json()["pending_approval"]["token"]

    # Approve
    r = client.post("/approvals/c3/decide", json={"decision": "approved", "token": token})
    assert r.status_code == 200
    assert queue.get_by_id("c3")["status"] == "approved"


def test_decide_rejected_resolves_as_rejected(tmp_path):
    """Rejecting a pending refund marks the queue entry as rejected."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c4",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Your refund has been rejected."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))
    _inject(graph, queue)
    client = TestClient(app)

    r1 = client.post("/chat", json={"customer_id": "CUST-001", "message": "Refund ORD-001"})
    token = r1.json()["pending_approval"]["token"]

    r = client.post("/approvals/c4/decide", json={"decision": "rejected", "token": token})
    assert r.status_code == 200
    assert queue.get_by_id("c4")["status"] == "rejected"


def test_decide_unknown_id_returns_404():
    """Deciding on a non-existent approval_id returns 404 (token valid, id unknown)."""
    queue = ApprovalQueue()
    _inject(build_graph(MockLLM([]), queue), queue)
    valid_token = create_token({"approval_id": "ghost-id"}, ttl_seconds=300)
    r = TestClient(app).post(
        "/approvals/ghost-id/decide",
        json={"decision": "approved", "token": valid_token},
    )
    assert r.status_code == 404


# ── System prompt tests ───────────────────────────────────────────────────────


def test_system_prompt_prepended_on_new_thread():
    """When _system_prompt is set, the first turn includes a SystemMessage in graph state."""
    _web._system_prompt = "You are a support agent."

    llm = MockLLM([AIMessage(content="Hello, how can I help?")])
    queue = ApprovalQueue()
    graph = build_graph(llm, queue)
    _inject(graph, queue)

    TestClient(app).post("/chat", json={"customer_id": "CUST-sys-1", "message": "Hi"})

    state = graph.get_state({"configurable": {"thread_id": "CUST-sys-1"}})
    messages = state.values["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "support agent" in messages[0].content


def test_system_prompt_not_repeated_on_second_turn():
    """The SystemMessage is only added once even across multiple turns."""
    _web._system_prompt = "You are a support agent."

    llm = MockLLM([AIMessage(content="Hello!"), AIMessage(content="Sure!")])
    queue = ApprovalQueue()
    graph = build_graph(llm, queue)
    _inject(graph, queue)
    client = TestClient(app)

    client.post("/chat", json={"customer_id": "CUST-sys-2", "message": "Hi"})
    client.post("/chat", json={"customer_id": "CUST-sys-2", "message": "Help me"})

    state = graph.get_state({"configurable": {"thread_id": "CUST-sys-2"}})
    system_msgs = [m for m in state.values["messages"] if isinstance(m, SystemMessage)]
    assert len(system_msgs) == 1


# ── Timeout tests ─────────────────────────────────────────────────────────────


def test_chat_llm_timeout_returns_graceful_reply():
    """When the LLM call exceeds the deadline, POST /chat returns a graceful message (not 500)."""
    llm = SlowMockLLM(sleep_seconds=0.5, responses=[AIMessage(content="Too slow.")])
    queue = ApprovalQueue()
    _inject(build_graph(llm, queue, timeout=0.1), queue)

    r = TestClient(app).post("/chat", json={"customer_id": "CUST-001", "message": "Hello"})

    assert r.status_code == 200
    data = r.json()
    assert data["reply"] is not None
    assert "too long" in data["reply"].lower() or "cancelled" in data["reply"].lower()
    assert data["pending_approval"] is None
    assert queue.get_pending() == []


def test_chat_tool_timeout_returns_graceful_reply(monkeypatch):
    """When a tool call exceeds the deadline, POST /chat returns a graceful message (not 500)."""
    import agent.graph as _graph

    def slow_lookup(**kwargs):
        time.sleep(0.5)
        return {"order_id": "ORD-001", "status": "shipped"}

    monkeypatch.setitem(_graph._TOOL_FUNCTIONS, "lookup_order", slow_lookup)

    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup_order",
                        "args": {"order_id": "ORD-001"},
                        "id": "t-slow",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    queue = ApprovalQueue()
    _inject(build_graph(llm, queue, timeout=0.1), queue)

    r = TestClient(app).post(
        "/chat", json={"customer_id": "CUST-001", "message": "Where is my order?"}
    )

    assert r.status_code == 200
    data = r.json()
    assert data["reply"] is not None
    assert "too long" in data["reply"].lower() or "cancelled" in data["reply"].lower()
    assert data["pending_approval"] is None
