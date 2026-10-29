import base64
import hashlib
import hmac
import json
import os
import time

_SECRET = os.getenv("APPROVAL_SECRET_KEY", "dev-secret-change-in-production")
_TOKEN_TTL = int(os.getenv("APPROVAL_TOKEN_TTL", "300"))


class TokenTampered(Exception):
    """Raised when a token's HMAC signature does not match its payload."""


class TokenExpired(Exception):
    """Raised when a token's TTL has passed."""


def _sign(payload_b64: str) -> str:
    return hmac.new(
        _SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()


def create_token(payload: dict, ttl_seconds: int = _TOKEN_TTL) -> str:
    """Return a signed, time-limited token encoding the given payload."""
    data = {**payload, "expires_at": time.time() + ttl_seconds}
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(data, sort_keys=True).encode()
    ).decode()
    signature = _sign(payload_b64)
    return f"{payload_b64}.{signature}"


def verify_token(token: str) -> dict:
    """Verify signature and TTL. Returns payload on success.

    Raises:
        TokenTampered: if the signature does not match.
        TokenExpired:  if the token is past its TTL.
    """
    try:
        payload_b64, signature = token.rsplit(".", 1)
    except ValueError:
        raise TokenTampered("Token format invalid.")

    expected = _sign(payload_b64)
    if not hmac.compare_digest(expected, signature):
        raise TokenTampered("Token signature mismatch.")

    data = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())

    if time.time() > data["expires_at"]:
        raise TokenExpired("Token has expired.")

    return {k: v for k, v in data.items() if k != "expires_at"}
