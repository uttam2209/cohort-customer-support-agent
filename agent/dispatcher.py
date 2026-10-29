from tools.models import GatedAction


def classify_result(result) -> str:
    """Return 'GATED' if result requires human approval, 'AUTO' otherwise."""
    return "GATED" if isinstance(result, GatedAction) else "AUTO"
