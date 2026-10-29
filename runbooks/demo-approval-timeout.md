# Demo Runbook: Approval Timeout Flow

Shows that when approval execution exceeds the configured deadline, the approver
receives a graceful HTTP 504 response and the order is left unchanged.

---

## How it works

When a human clicks **Approve**, the server resumes the paused graph and runs
`_execute_approved()`. `DEMO_SLOW_APPROVAL_SECONDS` artificially delays that
step using a cancellable `threading.Event`. `AGENT_TIMEOUT_SECONDS` caps how
long any single execution step may take. When the delay exceeds the cap:

1. `AgentTimeoutError` is raised — the `POST /approvals/{id}/decide` route returns 504
2. The cancellation event is set — the sleeping background thread wakes immediately and exits **without** writing to the DB
3. `queue.resolve()` is never called — the approval card **stays visible** on the dashboard so the approver can retry

---

## Step 1 — Start the server with timeout settings

```bash
DEMO_SLOW_APPROVAL_SECONDS=4 AGENT_TIMEOUT_SECONDS=3 uvicorn main:app --reload
```

| Variable | Value | Meaning |
|---|---|---|
| `DEMO_SLOW_APPROVAL_SECONDS` | `4` | Approval execution sleeps 4 s |
| `AGENT_TIMEOUT_SECONDS` | `3` | Deadline is 3 s — fires before execution completes |

---

## Step 2 — Request a refund (customer side)

```bash
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"CUST-001","message":"I want a refund for ORD-001"}' \
  | python3 -m json.tool
```

**Expected response:**

```json
{
    "reply": null,
    "pending_approval": {
        "approval_id": "<some-id>",
        "action": "request_refund",
        "token": "<token>"
    }
}
```

Copy `approval_id` and `token`.

---

## Step 3 — Confirm the approval card appears

Open the approvals dashboard in a browser:

```
http://localhost:8000/approvals
```

The refund request should appear as a pending card with **Approve** and **Reject** buttons.

---

## Step 4 — Approve the request (human side)

Either click **Approve** in the dashboard, or use curl:

```bash
curl -s -X POST http://localhost:8000/approvals/<approval_id>/decide \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approved","token":"<token>"}' \
  -w "\nHTTP status: %{http_code}\n"
```

**Expected response (HTTP 504 after ~3 seconds):**

```json
{
    "detail": "Approval processing timed out. The action may not have completed."
}
HTTP status: 504
```

---

## Step 5 — Confirm the approval card is still on the dashboard

Refresh the approvals page:

```
http://localhost:8000/approvals
```

The refund card should **still be visible** with Approve and Reject buttons. The queue
entry was not resolved because `queue.resolve()` only runs after `_execute_approved()`
completes successfully. The approver can retry.

---

## Step 6 — Confirm the order was not updated

```bash
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"CUST-001","message":"What is the status of ORD-001?"}' \
  | python3 -m json.tool
```

The order status remains `return_initiated` — the cancellation event woke the
background thread immediately, which exited before calling `update_order_status()`.

---

## Step 7 — Restore normal behaviour

Restart without the demo variables:

```bash
uvicorn main:app --reload
```

The default timeout is 30 seconds and no artificial delay is applied.
