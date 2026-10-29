from db.database import query_order


def lookup_order(order_id: str, customer_id: str, db_path: str = None) -> dict | None:
    """Return order details scoped to customer_id. Returns None if not found or wrong customer."""
    return query_order(order_id, customer_id, db_path=db_path)
