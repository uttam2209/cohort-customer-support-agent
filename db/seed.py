import sqlite3
import os
from db.database import _DEFAULT_DB_PATH

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

CUSTOMERS = [
    ("CUST-001", "Jane Doe",      "jane@example.com"),
    ("CUST-002", "Bob Smith",     "bob@example.com"),
    ("CUST-003", "Alice Johnson", "alice@example.com"),
    ("CUST-004", "Carlos Rivera", "carlos@example.com"),
    ("CUST-005", "Emily Chen",    "emily@example.com"),
]

ORDERS = [
    ("ORD-001", "CUST-001", "Wireless Headphones", 79.99,  "return_initiated"),
    ("ORD-002", "CUST-001", "Laptop Stand",         35.00,  "delivered"),
    ("ORD-003", "CUST-002", "Phone Case",            12.99,  "shipped"),
    ("ORD-004", "CUST-002", "USB-C Hub",             49.99,  "return_initiated"),
    ("ORD-005", "CUST-003", "Mechanical Keyboard",  129.99, "processing"),
    ("ORD-006", "CUST-003", "Mouse Pad XL",          24.99,  "delivered"),
    ("ORD-007", "CUST-004", "Webcam HD",             89.99,  "return_initiated"),
    ("ORD-008", "CUST-004", "Ring Light",            44.99,  "shipped"),
    ("ORD-009", "CUST-005", "Monitor Arm",           69.99,  "delivered"),
    ("ORD-010", "CUST-005", "Cable Management Kit",  18.99,  "processing"),
]


def seed(db_path: str = None):
    path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.executescript(open(SCHEMA_PATH).read())
    conn.execute("DELETE FROM escalations")
    conn.execute("DELETE FROM orders")
    conn.execute("DELETE FROM customers")
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ORDERS)
    conn.commit()
    conn.close()
    print(f"Seeded {len(CUSTOMERS)} customers and {len(ORDERS)} orders → {path}")


if __name__ == "__main__":
    seed()
