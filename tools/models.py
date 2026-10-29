from dataclasses import dataclass, field


@dataclass
class GatedAction:
    tool_name: str
    payload: dict
    requires_approval: bool = True
