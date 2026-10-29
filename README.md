# Support Agent

A customer support chatbot with human-in-the-loop approval gates, built with LangGraph, FastAPI, and SQLite.

The agent can look up orders automatically. Refunds and case escalations require a manager to approve before any action is taken.

---

## Architecture

```
Customer (chat UI)
  │
  ▼
POST /chat
  └─► LangGraph agent
        ├─► lookup_order     → AUTO (no approval needed)
        ├─► request_refund   → GATED (pauses, waits for manager)
        └─► escalate_case    → GATED (pauses, waits for manager)

Manager (approvals dashboard)
  │
  ▼
POST /approvals/{id}/decide
  └─► resume graph with "approved" | "rejected"
        ├─► approved refund    → update_order_status("refunded")
        ├─► approved escalate  → create_escalation(...)
        └─► rejected           → sends rejection message to customer
```

**Security model**

- `customer_id` is never passed to the LLM — it is injected from server state, so the LLM cannot fabricate or leak another customer's data.
- Approval tokens are HMAC-SHA256 signed with a 5-minute TTL. The dashboard rejects expired or tampered tokens before touching the graph.

---

## Quick start

**Prerequisites**: Python 3.12+, an OpenAI API key.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your API key
export OPENAI_API_KEY=sk-...

# 4. Start the server (seeds the DB on first run)
uvicorn main:app --reload
```

Open the chat UI at `http://localhost:8000/chat` and the approvals dashboard at `http://localhost:8000/approvals`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | **Required.** App exits on startup if missing. |
| `APPROVAL_SECRET_KEY` | `dev-secret-change-in-production` | HMAC signing key for approval tokens. Set a real secret in production. |
| `APPROVAL_TOKEN_TTL` | `300` | Approval token lifetime in seconds. |
| `AGENT_TIMEOUT_SECONDS` | `30` | Per-call deadline for LLM and tool calls. Exceeding this returns a graceful error. |
| `DEMO_SLOW_APPROVAL_SECONDS` | `0` | Artificially delays approval execution for demos. Set alongside `AGENT_TIMEOUT_SECONDS`. |
| `LANGCHAIN_TRACING_V2` | `false` | Set `true` only if using LangSmith. |
| `LANGCHAIN_API_KEY` | — | Only needed if tracing is enabled. |

---

## Project layout

```
main.py                  # Entry point — wires LLM, DB, approval queue into FastAPI
agent/
  graph.py               # LangGraph state machine with interrupt/resume + timeout
  dispatcher.py          # classify_result(): "GATED" | "AUTO"
approval/
  queue.py               # In-memory approval store (lost on restart)
  tokens.py              # HMAC token create/verify, TokenExpired, TokenTampered
db/
  schema.sql             # customers, orders, escalations tables
  database.py            # query_order, update_order_status, create_escalation
  seed.py                # 5 customers, 10 orders
tools/
  models.py              # GatedAction dataclass
  lookup.py              # lookup_order — AUTO, no approval
  refund.py              # request_refund — GATED, validates return_initiated status
  escalation.py          # escalate_case — GATED, always eligible
ui/
  app.py                 # FastAPI routes: GET+POST /chat, GET /approvals, POST /approvals/{id}/decide
  templates/
    chat.html            # Customer chat UI
    approvals.html       # Manager approval dashboard
tests/
  test_db.py             # DB query scoping
  test_tools.py          # Tool unit tests
  test_approval.py       # Token and queue tests
  test_agent.py          # Graph logic with MockLLM
  test_ui.py             # FastAPI endpoint tests
  test_e2e.py            # Full approval workflow (all 4 states)
runbooks/
  demo-tampered-token.md    # How to demo the tampered token 400 flow
  demo-approval-timeout.md  # How to demo the approval execution timeout 504 flow
```

---

## Seed data

Orders eligible for refund (`status = return_initiated`):

| Order | Customer | Product | Amount |
|---|---|---|---|
| ORD-001 | CUST-001 | Wireless Headphones | $79.99 |
| ORD-004 | CUST-002 | USB-C Hub | $49.99 |
| ORD-007 | CUST-004 | Webcam HD | $89.99 |

To re-seed: `rm -f db/support_agent.db && uvicorn main:app --reload`

---

## Running tests

```bash
.venv/bin/pytest tests/ -v
```

37 passing, 1 skipped.

---

## Demo scenarios

See the runbooks for step-by-step walkthroughs of specific edge cases:

- **Tampered token** (`runbooks/demo-tampered-token.md`) — shows the 400 response when an approval token is corrupted or signed with the wrong key.
- **Approval execution timeout** (`runbooks/demo-approval-timeout.md`) — shows the 504 response when approval execution exceeds the configured deadline, with the order left unchanged.

---

## Known limitations

- Approval queue is in-memory — lost on server restart.
- SQLite is not safe for multi-process deployments.
- `APPROVAL_SECRET_KEY` must be set to a real secret in production.
