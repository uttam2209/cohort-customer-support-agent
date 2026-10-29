from db.database import escalation_exists
from tools.models import GatedAction


def escalate_case(
    order_id: str, reason: str, customer_id: str, db_path: str = None
) -> GatedAction | str:
    """Signal that a case should be escalated. Does NOT execute — returns a GatedAction for the dispatcher.

    Returns an error string if an escalation already exists for this order.
    """
    if escalation_exists(order_id, customer_id, db_path=db_path):
        return (
            f"Order {order_id} has already been escalated. "
            "No further escalation is needed."
        )
    return GatedAction(
        tool_name="escalate_case",
        payload={
            "order_id": order_id,
            "reason": reason,
            "customer_id": customer_id,
        },
    )
