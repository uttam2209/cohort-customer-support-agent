# Demo Runbook: Tampered Token Flow

Shows that a modified approval token is rejected with HTTP 400 before any action is taken.

---

## Prerequisites

- App dependencies installed (`.venv/`)
- Database seeded (run once if `db/support_agent.db` does not exist)

```bash
rm -f db/support_agent.db
uvicorn main:app --reload   # seeds on first start, then Ctrl-C
```

---

## Step 1 — Start the server

```bash
uvicorn main:app --reload
```

Leave this running in one terminal. Open a second terminal for the curl commands below.

---

## Step 2 — Trigger a refund request

```bash
curl -s -X POST http://localhost:8000/chat -H 'Content-Type: application/json' -d '{"customer_id":"CUST-001","message":"I want a refund for ORD-001"}' | python3 -m json.tool
```

**Expected response:**

```json
{
    "reply": null,
    "pending_approval": {
        "approval_id": "<some-id>",
        "action": "request_refund",
        "token": "<long-token-string>"
    }
}
```

Copy both `approval_id` and `token` from the response.

---

## Step 3 — Corrupt the token

```bash
TOKEN="<paste token here>"
TAMPERED="${TOKEN::-1}X"    # replaces the last character with X
echo "Tampered token: $TAMPERED"
```

---

## Step 4 — Submit with the corrupted token

```bash
curl -s -X POST http://localhost:8000/approvals/<approval_id>/decide -H 'Content-Type: application/json' -d '{"decision":"approved","token":"<corrupted_token>"}' | python3 -m json.tool
```

Replace `<approval_id>` with the value from Step 2.

**Expected response (HTTP 400):**

```json
{
    "detail": "Token tampered."
}
```

**What did not happen:** the refund was not processed and the order status was not changed.

---

## Step 5 — Confirm the order is unchanged (optional)

```bash
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"CUST-001","message":"What is the status of ORD-001?"}' \
  | python3 -m json.tool
```

The order status remains `return_initiated`, not `refunded`.

---

## Why this works

`verify_token()` in `approval/tokens.py` recomputes the HMAC-SHA256 signature over the
base64-encoded payload and compares it with `hmac.compare_digest`. Any single-character
change to the token — whether in the payload or the signature — causes a mismatch and
raises `TokenTampered`, which the `/approvals/{id}/decide` route converts to a 400 before
touching the graph or the database.

---

## Variant: mismatched secret key

Start the server with one secret, then verify with a different one to simulate a token
issued by a different server instance.

```bash
# Terminal 1
APPROVAL_SECRET_KEY=key-A uvicorn main:app --reload

# Terminal 2 — get a token (Step 2 above), then verify with wrong key
APPROVAL_SECRET_KEY=key-B python3 -c "
from approval.tokens import verify_token, TokenTampered
try:
    verify_token('<paste token>')
except TokenTampered as e:
    print('Caught:', e)
"
```
