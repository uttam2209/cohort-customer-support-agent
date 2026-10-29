import time


class ApprovalQueue:
    """In-memory store for pending approval requests."""

    def __init__(self):
        self._store: dict = {}

    def add(self, approval_id: str, action: str, payload: dict, thread_id: str = "") -> None:
        self._store[approval_id] = {
            "approval_id": approval_id,
            "action": action,
            "payload": payload,
            "thread_id": thread_id,
            "status": "pending",
            "token": None,
            "expires_at": None,
            "created_at": time.time(),
        }

    def store_token(self, approval_id: str, token: str, expires_at: float) -> None:
        if approval_id in self._store:
            self._store[approval_id]["token"] = token
            self._store[approval_id]["expires_at"] = expires_at

    def get_pending(self) -> list:
        return [r for r in self._store.values() if r["status"] == "pending"]

    def get_stale(self) -> list:
        """Pending entries whose token has expired — need cleanup and thread resume."""
        now = time.time()
        return [
            r for r in self._store.values()
            if r["status"] == "pending"
            and r["expires_at"] is not None
            and r["expires_at"] < now
        ]

    def get_expired(self) -> list:
        """Entries already marked expired after cleanup."""
        return [r for r in self._store.values() if r["status"] == "expired"]

    def get_by_id(self, approval_id: str) -> dict | None:
        return self._store.get(approval_id)

    def resolve(self, approval_id: str, decision: str) -> None:
        if approval_id in self._store:
            self._store[approval_id]["status"] = decision
            self._store[approval_id]["resolved_at"] = time.time()
