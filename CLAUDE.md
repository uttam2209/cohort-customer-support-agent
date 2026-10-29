# Support Agent — Project Context

A customer support chatbot with human-in-the-loop approval gates, built with LangGraph, FastAPI, and SQLite.

## Stack

- **LLM**: OpenAI GPT-4o via `langchain-openai`
- **Agent**: LangGraph `StateGraph` with `MemorySaver` checkpointing
- **API**: FastAPI + Jinja2 templates
- **DB**: SQLite3 (customer-scoped queries)
- **Security**: HMAC-SHA256 tokens with 5-minute TTL
- **Tests**: pytest (50 passing, 1 skipped)

## Common commands

```bash
# Run server
uvicorn main:app --reload

# Run tests
.venv/bin/pytest tests/ -v

# Re-seed database (delete stale DB first)
rm -f db/support_agent.db && uvicorn main:app --reload
```

## Project layout

```
main.py                  # Entry point — wires LLM, DB, approval queue into FastAPI
agent/
  graph.py               # LangGraph state machine with interrupt/resume + AgentTimeoutError
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
  test_ui.py             # FastAPI endpoint tests (includes SlowMockLLM timeout tests)
  test_e2e.py            # Full approval workflow (all 4 states)
runbooks/
  demo-tampered-token.md    # Step-by-step demo of the tampered/expired token 400 flow
  demo-approval-timeout.md  # Step-by-step demo of approval execution timeout 504 flow
```

## Architecture: how approval gates work

```
POST /chat
  → graph.invoke()  [wrapped in _run_with_timeout]
    → agent_node: LLM decides to call request_refund or escalate_case
    → tool_executor_node: calls tool function
        → tool returns GatedAction (GATED) or plain value (AUTO)
        → GATED: interrupt() pauses graph, stores approval in queue
  → AgentTimeoutError → graceful reply, pending entry resolved as "expired"
  → response: { pending_approval: { approval_id, action, token } }

POST /approvals/{id}/decide
  → verify HMAC token (400 if expired or tampered)
  → graph.invoke(Command(resume=decision))  [_execute_approved wrapped in _run_with_timeout]
    → tool_executor_node resumes: executes or rejects
      → approved refund  → update_order_status(..., "refunded") → queue.resolve("approved")
      → approved escalate → create_escalation(...) → queue.resolve("approved")
      → rejected         → queue.resolve("rejected"), returns rejection message to LLM
  → AgentTimeoutError → queue.resolve("expired"), HTTP 504
```

## Key design decisions

### Customer ID is never LLM-controlled
`customer_id` is absent from every tool schema exposed to the LLM. It is injected by `tool_executor_node` from `AgentState`, so the LLM cannot fabricate or leak another customer's ID.

### Approval ID is the LLM tool-call ID
`approval_id = tc["id"]` — deterministic across LangGraph node replays. The idempotent guard `if not queue.get_by_id(approval_id)` prevents duplicate queue entries when the node replays on resume.

### Refund eligibility check
`request_refund` queries the DB and returns an error string (not a `GatedAction`) if the order status is not `return_initiated`. Only orders explicitly in that state can be refunded.

### System prompt injection
Injected only on the first turn of a thread by checking `graph.get_state(config).values` — empty dict means new thread.

### Timeout and cancellation (`_run_with_timeout` + `_demo_cancel`)
Every LLM call and tool execution runs inside `_run_with_timeout`, which uses a `ThreadPoolExecutor` with `future.result(timeout=N)`. On timeout it raises `AgentTimeoutError`.

The demo slow-path (`DEMO_SLOW_APPROVAL_SECONDS`) uses `threading.Event.wait()` instead of `time.sleep()` so that when `_run_with_timeout` fires, it immediately sets `_demo_cancel`, waking the background thread without the thread performing the DB write.

`queue.resolve()` for an approved action is called **after** `_execute_approved()` returns, not before. This keeps the dashboard card visible if execution fails.

LangGraph consumes the interrupt checkpoint the moment `graph.invoke(Command(resume=...))` is called, even if the node subsequently raises. If `AgentTimeoutError` propagates from `decide()`, the thread can never be resumed again — so `queue.resolve(approval_id, "expired")` is called immediately in the except block to prevent the TTL cleanup path from attempting a dead resume.

## Database schema

```sql
customers  (customer_id PK, name, email)
orders     (order_id PK, customer_id FK, product, amount, status)
escalations (id PK AUTOINCREMENT, order_id, customer_id, reason, created_at)
```

**Order statuses**: `processing`, `shipped`, `delivered`, `return_initiated`, `refunded`

**Seed data** — orders eligible for refund (status = `return_initiated`):
- ORD-001 (CUST-001, Wireless Headphones, $79.99)
- ORD-004 (CUST-002, USB-C Hub, $49.99)
- ORD-007 (CUST-004, Webcam HD, $89.99)

## Environment variables

```
OPENAI_API_KEY=...            # Required — app exits on startup if missing
APPROVAL_SECRET_KEY=...       # Optional — defaults to "dev-secret-change-in-production"
APPROVAL_TOKEN_TTL=...        # Optional — approval token lifetime in seconds, defaults to 300 (5 min)
AGENT_TIMEOUT_SECONDS=...          # Optional — per-call deadline for LLM/tool calls, defaults to 30
DEMO_SLOW_APPROVAL_SECONDS=...     # Optional — sleep inside _execute_approved() for timeout demo, defaults to 0
LANGCHAIN_TRACING_V2=false    # Optional — set true only if using LangSmith
LANGCHAIN_API_KEY=...         # Optional — only needed if tracing is enabled
```

## Testing patterns

**MockLLM** — used in all agent/UI tests instead of `FakeMessagesListChatModel` (which doesn't support `bind_tools`):
```python
class MockLLM:
    def __init__(self, responses: list): self._responses = iter(responses)
    def bind_tools(self, tools): return self
    def invoke(self, messages, **kwargs): return next(self._responses)
```

**`SlowMockLLM`** — used in timeout tests. Sleeps in `invoke()` so `_run_with_timeout` fires:
```python
class SlowMockLLM:
    def __init__(self, sleep_seconds, responses=None): ...
    def bind_tools(self, tools): return self
    def invoke(self, messages, **kwargs):
        time.sleep(self._sleep)
        return next(self._responses)
```
Pass `timeout=0.1` to `build_graph` to trigger `AgentTimeoutError` without real wall-clock waits.

**`_make_db(tmp_path)`** — all tests that exercise `request_refund` need a real SQLite DB with an order in `return_initiated` status. Pass the returned path to `build_graph(llm, queue, db_path=...)`.

**Dependency injection** — UI tests override graph/queue via `app.dependency_overrides`. The `clear_overrides` autouse fixture resets them between tests.

## Known limitations

- Approval queue is in-memory — lost on server restart
- SQLite is not safe for multi-process deployments
- `APPROVAL_SECRET_KEY` must be set to a real secret in production
