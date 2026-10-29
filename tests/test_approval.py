import time
import pytest
from approval.tokens import create_token, verify_token, TokenTampered, TokenExpired
from approval.queue import ApprovalQueue


# ── Token tests ────────────────────────────────────────────────────────────

def test_valid_token_round_trip():
    payload = {"order_id": "ORD-001", "amount": 79.99, "customer_id": "CUST-001"}
    token = create_token(payload, ttl_seconds=60)
    result = verify_token(token)
    assert result["order_id"] == "ORD-001"
    assert result["amount"] == 79.99
    assert result["customer_id"] == "CUST-001"


def test_tampered_token_raises():
    payload = {"order_id": "ORD-001", "amount": 79.99, "customer_id": "CUST-001"}
    token = create_token(payload, ttl_seconds=60)
    # flip one character in the middle of the token
    mid = len(token) // 2
    tampered = token[:mid] + ("X" if token[mid] != "X" else "Y") + token[mid + 1:]
    with pytest.raises(TokenTampered):
        verify_token(tampered)


def test_expired_token_raises():
    payload = {"order_id": "ORD-001", "amount": 79.99, "customer_id": "CUST-001"}
    token = create_token(payload, ttl_seconds=1)
    time.sleep(2)
    with pytest.raises(TokenExpired):
        verify_token(token)


def test_tampered_amount_raises():
    """Changing the amount in the payload must be detected."""
    payload = {"order_id": "ORD-001", "amount": 25.00, "customer_id": "CUST-001"}
    token = create_token(payload, ttl_seconds=60)
    # replace "25.0" with "999" in the raw token string
    tampered = token.replace("25.0", "999.0")
    if tampered == token:
        pytest.skip("Amount not directly visible in token — tamper via character flip instead")
    with pytest.raises(TokenTampered):
        verify_token(tampered)


# ── Queue tests ────────────────────────────────────────────────────────────

def test_queue_add_and_get_pending():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001", "amount": 79.99})
    pending = q.get_pending()
    assert len(pending) == 1
    assert pending[0]["approval_id"] == "APR-001"
    assert pending[0]["action"] == "request_refund"
    assert pending[0]["status"] == "pending"


def test_queue_resolve_approved():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.resolve("APR-001", "approved")
    assert q.get_pending() == []
    assert q.get_by_id("APR-001")["status"] == "approved"


def test_queue_resolve_rejected():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.resolve("APR-001", "rejected")
    assert q.get_by_id("APR-001")["status"] == "rejected"


def test_queue_get_pending_excludes_resolved():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.add("APR-002", "escalate_case", {"order_id": "ORD-002"})
    q.resolve("APR-001", "approved")
    pending = q.get_pending()
    assert len(pending) == 1
    assert pending[0]["approval_id"] == "APR-002"


def test_store_token_sets_expires_at():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    expires = time.time() + 300
    q.store_token("APR-001", "tok", expires)
    entry = q.get_by_id("APR-001")
    assert entry["token"] == "tok"
    assert entry["expires_at"] == expires


def test_queue_get_stale_returns_past_expiry_pending():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.store_token("APR-001", "tok", time.time() - 1)  # already expired
    assert len(q.get_stale()) == 1
    assert q.get_stale()[0]["approval_id"] == "APR-001"


def test_queue_get_stale_excludes_valid_pending():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.store_token("APR-001", "tok", time.time() + 300)  # not yet expired
    assert q.get_stale() == []


def test_queue_get_stale_excludes_no_token_entries():
    """Entries without a token (expires_at is None) are not considered stale."""
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    assert q.get_stale() == []


def test_queue_get_expired_returns_marked_entries():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.resolve("APR-001", "expired")
    expired = q.get_expired()
    assert len(expired) == 1
    assert expired[0]["approval_id"] == "APR-001"


def test_queue_get_expired_excludes_pending_and_resolved():
    q = ApprovalQueue()
    q.add("APR-001", "request_refund", {"order_id": "ORD-001"})
    q.add("APR-002", "request_refund", {"order_id": "ORD-002"})
    q.resolve("APR-002", "approved")
    assert q.get_expired() == []
