import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.dispatcher import classify_result
from agent.graph import build_graph
from approval.queue import ApprovalQueue
from tools.models import GatedAction


# ── Fixture ──────────────────────────────────────────────────────────────────


class MockLLM:
    """Pre-baked responses; bind_tools returns self so graph wiring works."""

    def __init__(self, responses: list):
        self._responses = iter(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return next(self._responses)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _make_db(tmp_path) -> str:
    """Seed a minimal SQLite DB for lookup and execution tests."""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, email TEXT)"
    )
    conn.execute(
        "CREATE TABLE orders "
        "(order_id TEXT PRIMARY KEY, customer_id TEXT, product TEXT, amount REAL, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE escalations "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, "
        "customer_id TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO customers VALUES ('CUST-001', 'Alice', 'alice@example.com')")
    conn.execute(
        "INSERT INTO orders VALUES ('ORD-001', 'CUST-001', 'Widget', 25.00, 'return_initiated')"
    )
    conn.commit()
    conn.close()
    return db


# ── Dispatcher tests ──────────────────────────────────────────────────────────


def test_classify_auto():
    """A plain dict (lookup result) is classified AUTO."""
    result = {"order_id": "ORD-001", "status": "shipped"}
    assert classify_result(result) == "AUTO"


def test_classify_gated():
    """A GatedAction is classified GATED."""
    action = GatedAction(tool_name="request_refund", payload={})
    assert classify_result(action) == "GATED"


# ── Graph tests ───────────────────────────────────────────────────────────────


def test_graph_lookup_no_interrupt(tmp_path):
    """lookup_order returns AUTO — graph completes with no interrupt."""
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
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))

    result = graph.invoke(
        {"messages": [HumanMessage(content="Where is ORD-001?")], "customer_id": "CUST-001"},
        config=_config("lookup-test"),
    )

    assert "__interrupt__" not in result
    assert queue.get_pending() == []


def test_graph_refund_interrupts(tmp_path):
    """request_refund is GATED — graph pauses and surfaces an interrupt."""
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
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))

    result = graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=_config("refund-interrupt-test"),
    )

    assert "__interrupt__" in result
    assert len(queue.get_pending()) == 1


def test_graph_refund_approved(tmp_path):
    """Resume with 'approved' → approval record marked approved."""
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
    config = _config("refund-approved-test")

    # First pass → interrupt
    graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )

    # Resume with approved
    result = graph.invoke(Command(resume="approved"), config=config)

    assert "__interrupt__" not in result
    statuses = [r["status"] for r in queue._store.values()]
    assert statuses == ["approved"]


def test_graph_refund_expired(tmp_path):
    """Resume with 'expired' → approval record marked expired, distinct message from rejected."""
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "c-exp",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Your request timed out."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))
    config = _config("refund-expired-test")

    graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )
    result = graph.invoke(Command(resume="expired"), config=config)

    assert "__interrupt__" not in result
    assert queue.get_by_id("c-exp")["status"] == "expired"
    # ToolMessage should use the timeout-specific wording, not the rejection wording
    messages = result["messages"]
    tool_msg = next(m for m in messages if hasattr(m, "tool_call_id"))
    assert "timed out" in tool_msg.content
    assert "not approved" not in tool_msg.content


def test_graph_refund_rejected(tmp_path):
    """Resume with 'rejected' → approval record marked rejected."""
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
            AIMessage(content="Your refund request was rejected."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=_make_db(tmp_path))
    config = _config("refund-rejected-test")

    graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )

    result = graph.invoke(Command(resume="rejected"), config=config)

    assert "__interrupt__" not in result
    statuses = [r["status"] for r in queue._store.values()]
    assert statuses == ["rejected"]


# ── Post-approval execution tests ─────────────────────────────────────────────


def test_approved_refund_updates_order_status(tmp_path):
    """Approving a refund marks the order as 'refunded' in the database."""
    db = _make_db(tmp_path)
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "exec-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Refund processed."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=db)
    config = _config("exec-approved-refund")

    graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )
    graph.invoke(Command(resume="approved"), config=config)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status FROM orders WHERE order_id = 'ORD-001'").fetchone()
    conn.close()
    assert row[0] == "refunded"


def test_rejected_refund_does_not_update_db(tmp_path):
    """Rejecting a refund leaves the order status unchanged."""
    db = _make_db(tmp_path)
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_refund",
                        "args": {"order_id": "ORD-001", "amount": 25.0, "reason": "damaged"},
                        "id": "exec-2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Refund rejected."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=db)
    config = _config("exec-rejected-refund")

    graph.invoke(
        {"messages": [HumanMessage(content="Refund ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )
    graph.invoke(Command(resume="rejected"), config=config)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status FROM orders WHERE order_id = 'ORD-001'").fetchone()
    conn.close()
    assert row[0] == "return_initiated"  # unchanged


def test_approved_escalation_creates_record(tmp_path):
    """Approving an escalation inserts a row into the escalations table."""
    db = _make_db(tmp_path)
    llm = MockLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "escalate_case",
                        "args": {"order_id": "ORD-001", "reason": "Complex billing issue"},
                        "id": "exec-3",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Case escalated."),
        ]
    )
    queue = ApprovalQueue()
    graph = build_graph(llm, queue, db_path=db)
    config = _config("exec-escalation")

    graph.invoke(
        {"messages": [HumanMessage(content="Escalate ORD-001")], "customer_id": "CUST-001"},
        config=config,
    )
    graph.invoke(Command(resume="approved"), config=config)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT order_id, customer_id, reason FROM escalations").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0] == ("ORD-001", "CUST-001", "Complex billing issue")
