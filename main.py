"""
Entry point for the Support Agent.

Wires the real LLM, database, and approval queue into the FastAPI app.

Usage:
    uvicorn main:app --reload
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Fail early rather than getting a confusing AuthenticationError mid-request.
if not os.getenv("OPENAI_API_KEY"):
    sys.exit(
        "ERROR: OPENAI_API_KEY is not set.\n"
        "Copy .env.example → .env, add your key, then try again."
    )

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import ui.app as web
from agent.graph import build_graph
from approval.queue import ApprovalQueue
from db.database import get_connection

# ── Database ──────────────────────────────────────────────────────────────────

_SCHEMA = Path(__file__).parent / "db" / "schema.sql"


def _init_db() -> None:
    with get_connection() as conn:
        conn.executescript(_SCHEMA.read_text())


_init_db()

try:
    from db.seed import seed
    seed()
except Exception as exc:  # noqa: BLE001
    print(f"[warn] seed() skipped: {exc}")

# ── Tool schemas ──────────────────────────────────────────────────────────────
# customer_id is intentionally absent from every schema — it is injected from
# graph state so the LLM can never fabricate or leak another customer's ID.


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by order ID and return its product, amount, and status."""


@tool
def request_refund(order_id: str, amount: float, reason: str) -> str:
    """Request a refund for an order. Requires manager approval before it executes."""


@tool
def escalate_case(order_id: str, reason: str) -> str:
    """Escalate a support case to a human agent. Requires manager approval."""


# ── Agent + queue ─────────────────────────────────────────────────────────────

_llm = ChatOpenAI(model="gpt-4o", temperature=0).bind_tools(
    [lookup_order, request_refund, escalate_case]
)
_queue = ApprovalQueue()
_graph = build_graph(_llm, _queue)

# ── Wire singletons into the FastAPI layer ────────────────────────────────────

web._graph = _graph
web._queue = _queue
web._system_prompt = (
    "You are a helpful customer support agent for an e-commerce platform. "
    "You can look up orders, request refunds, and escalate cases to a human agent. "
    "Always look up an order before proposing a refund or escalation. "
    "Be concise and professional."
)

# ── Export for uvicorn ────────────────────────────────────────────────────────

app = web.app
