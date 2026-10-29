from db.database import query_order
from tools.models import GatedAction


def request_refund(
    order_id: str, amount: float, reason: str, customer_id: str, db_path: str = None
) -> GatedAction | str:
    """Signal that a refund is requested. Does NOT execute — returns a GatedAction for the dispatcher.

    Returns an error string if the order is not in 'return_initiated' state.
    """
    order = query_order(order_id, customer_id, db_path=db_path)
    if not order or order["status"] != "return_initiated":
        return (
            f"Order {order_id} is not eligible for a refund. "
            "Only orders with 'return_initiated' status qualify."
        )
    return GatedAction(
        tool_name="request_refund",
        payload={
            "order_id": order_id,
            "amount": amount,
            "reason": reason,
            "customer_id": customer_id,
        },
    )
