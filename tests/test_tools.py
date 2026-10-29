import sqlite3
import pytest
import os
from tools.refund import request_refund
from tools.escalation import escalate_case
from tools.models import GatedAction


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "../db/schema.sql")).read())
    conn.execute("INSERT INTO customers VALUES ('CUST-001', 'Jane Doe', 'jane@example.com')")
    conn.execute("INSERT INTO orders VALUES ('ORD-001', 'CUST-001', 'Wireless Headphones', 79.99, 'return_initiated')")
    conn.commit()
    conn.close()
    return path


def test_request_refund_returns_gated_action(db_path):
    result = request_refund("ORD-001", 79.99, "item damaged", "CUST-001", db_path=db_path)
    assert isinstance(result, GatedAction)
    assert result.tool_name == "request_refund"
    assert result.payload["order_id"] == "ORD-001"
    assert result.payload["amount"] == 79.99
    assert result.payload["reason"] == "item damaged"
    assert result.payload["customer_id"] == "CUST-001"
    assert result.requires_approval is True


def test_request_refund_does_not_write_to_db(db_path):
    request_refund("ORD-001", 79.99, "item damaged", "CUST-001", db_path=db_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM orders WHERE order_id = 'ORD-001'").fetchone()
    conn.close()
    # status must still be 'return_initiated' — refund did not execute
    assert row[0] == "return_initiated"


def test_request_refund_rejects_non_return_initiated_order(tmp_path):
    """Orders not in return_initiated state are rejected without creating a GatedAction."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "../db/schema.sql")).read())
    conn.execute("INSERT INTO customers VALUES ('CUST-001', 'Jane Doe', 'jane@example.com')")
    conn.execute("INSERT INTO orders VALUES ('ORD-002', 'CUST-001', 'Laptop Stand', 35.00, 'delivered')")
    conn.commit()
    conn.close()

    result = request_refund("ORD-002", 35.00, "changed mind", "CUST-001", db_path=path)
    assert isinstance(result, str)
    assert "not eligible" in result


def test_escalate_case_returns_gated_action(db_path):
    result = escalate_case("ORD-001", "customer very unhappy", "CUST-001", db_path=db_path)
    assert isinstance(result, GatedAction)
    assert result.tool_name == "escalate_case"
    assert result.payload["order_id"] == "ORD-001"
    assert result.payload["reason"] == "customer very unhappy"
    assert result.payload["customer_id"] == "CUST-001"
    assert result.requires_approval is True


def test_escalate_case_does_not_write_to_db(db_path):
    escalate_case("ORD-001", "customer very unhappy", "CUST-001", db_path=db_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM orders WHERE order_id = 'ORD-001'").fetchone()
    conn.close()
    assert row[0] == "return_initiated"


def test_escalate_case_rejects_duplicate_escalation(db_path):
    """A second escalation for the same order returns an error string, not a GatedAction."""
    import time
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO escalations (order_id, customer_id, reason, created_at) VALUES (?, ?, ?, ?)",
        ("ORD-001", "CUST-001", "first escalation", time.time()),
    )
    conn.commit()
    conn.close()

    result = escalate_case("ORD-001", "another issue", "CUST-001", db_path=db_path)
    assert isinstance(result, str)
    assert "already been escalated" in result


def test_escalate_case_allows_different_order(db_path):
    """An existing escalation on one order does not block escalation on a different order."""
    import time
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO orders VALUES ('ORD-002', 'CUST-001', 'Laptop Stand', 35.00, 'delivered')")
    conn.execute(
        "INSERT INTO escalations (order_id, customer_id, reason, created_at) VALUES (?, ?, ?, ?)",
        ("ORD-001", "CUST-001", "first escalation", time.time()),
    )
    conn.commit()
    conn.close()

    result = escalate_case("ORD-002", "different order issue", "CUST-001", db_path=db_path)
    assert isinstance(result, GatedAction)
