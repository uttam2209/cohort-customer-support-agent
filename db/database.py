import sqlite3
import os
import time

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "support_agent.db")


def get_connection(db_path: str = None) -> sqlite3.Connection:
    path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def query_order(order_id: str, customer_id: str, db_path: str = None) -> dict | None:
    """Return the order if it exists and belongs to customer_id, otherwise None."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND customer_id = ?",
            (order_id, customer_id),
        ).fetchone()
    return dict(row) if row else None


def update_order_status(
    order_id: str, customer_id: str, status: str, db_path: str = None
) -> None:
    """Update an order's status. Scoped to customer_id to prevent cross-customer writes."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE orders SET status = ? WHERE order_id = ? AND customer_id = ?",
            (status, order_id, customer_id),
        )


def escalation_exists(order_id: str, customer_id: str, db_path: str = None) -> bool:
    """Return True if an escalation already exists for this order and customer."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM escalations WHERE order_id = ? AND customer_id = ?",
            (order_id, customer_id),
        ).fetchone()
    return row is not None


def create_escalation(
    order_id: str, customer_id: str, reason: str, db_path: str = None
) -> None:
    """Insert a new escalation record for the given order."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO escalations (order_id, customer_id, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (order_id, customer_id, reason, time.time()),
        )
