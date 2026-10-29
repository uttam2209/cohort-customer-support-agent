import pytest
import sqlite3
import tempfile
import os
from db.database import query_order, get_connection


@pytest.fixture
def db_path(tmp_path):
    """Create a fresh seeded database for each test."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "../db/schema.sql")).read())
    conn.execute(
        "INSERT INTO customers VALUES ('CUST-001', 'Jane Doe', 'jane@example.com')"
    )
    conn.execute(
        "INSERT INTO customers VALUES ('CUST-002', 'Bob Smith', 'bob@example.com')"
    )
    conn.execute(
        "INSERT INTO orders VALUES ('ORD-001', 'CUST-001', 'Wireless Headphones', 79.99, 'delivered')"
    )
    conn.execute(
        "INSERT INTO orders VALUES ('ORD-002', 'CUST-002', 'Phone Case', 12.99, 'shipped')"
    )
    conn.commit()
    conn.close()
    return path


def test_query_order_returns_order_for_correct_customer(db_path):
    result = query_order("ORD-001", "CUST-001", db_path=db_path)
    assert result["order_id"] == "ORD-001"
    assert result["customer_id"] == "CUST-001"
    assert result["product"] == "Wireless Headphones"
    assert result["amount"] == 79.99
    assert result["status"] == "delivered"


def test_query_order_returns_none_for_unknown_order(db_path):
    result = query_order("ORD-999", "CUST-001", db_path=db_path)
    assert result is None


def test_query_order_returns_none_for_wrong_customer(db_path):
    # ORD-002 belongs to CUST-002, not CUST-001
    result = query_order("ORD-002", "CUST-001", db_path=db_path)
    assert result is None
