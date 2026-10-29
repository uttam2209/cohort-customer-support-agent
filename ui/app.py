import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel

from agent.graph import AgentTimeoutError
from approval.queue import ApprovalQueue
from approval.tokens import TokenExpired, TokenTampered, _TOKEN_TTL, create_token, verify_token

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Support Agent")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ── Shared singletons (overridden in tests via dependency_overrides) ──────────

_graph = None
_queue: ApprovalQueue | None = None
# Set to a string by main.py to inject a system prompt on the first turn of each thread.
_system_prompt: str | None = None


def get_graph():
    return _graph


def get_queue() -> ApprovalQueue:
    return _queue


# ── Request / response schemas ────────────────────────────────────────────────


class ChatRequest(BaseModel):
    customer_id: str
    message: str
    thread_id: str | None = None


class DecideRequest(BaseModel):
    decision: str   # "approved" | "rejected"
    token: str      # HMAC-signed token returned by POST /chat


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html")


@app.post("/chat")
def chat(
    req: ChatRequest,
    graph=Depends(get_graph),
    queue: ApprovalQueue = Depends(get_queue),
):
    thread_id = req.thread_id or req.customer_id
    config = {"configurable": {"thread_id": thread_id}}

    # If a pending approval exists for this thread the graph is paused at an
    # interrupt; invoking it with new messages would crash with a RuntimeError.
    if any(p["thread_id"] == thread_id for p in queue.get_pending()):
        return {
            "reply": (
                "There is a pending approval for this conversation. "
                "Please resolve it in the approvals dashboard before sending another message."
            ),
            "pending_approval": None,
        }

    messages = []
    if _system_prompt:
        # Prepend the system message only on the very first turn of a thread.
        state = graph.get_state(config)
        if not state.values:
            messages.append(SystemMessage(content=_system_prompt))
    messages.append(HumanMessage(content=req.message))

    try:
        result = graph.invoke(
            {"messages": messages, "customer_id": req.customer_id},
            config=config,
        )
    except AgentTimeoutError:
        for entry in queue.get_pending():
            if entry["thread_id"] == thread_id:
                queue.resolve(entry["approval_id"], "expired")
        return {
            "reply": (
                "Your request took too long to process and was cancelled. "
                "Please try again."
            ),
            "pending_approval": None,
        }

    if "__interrupt__" in result:
        interrupt_info = result["__interrupt__"][0].value
        # Mint a signed token so the approver proves they saw the original payload.
        expires_at = time.time() + _TOKEN_TTL
        token = create_token(
            {"approval_id": interrupt_info["approval_id"], "action": interrupt_info["action"]}
        )
        queue.store_token(interrupt_info["approval_id"], token, expires_at)
        return {"reply": None, "pending_approval": {**interrupt_info, "token": token}}

    return {"reply": result["messages"][-1].content, "pending_approval": None}


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(
    request: Request,
    graph=Depends(get_graph),
    queue: ApprovalQueue = Depends(get_queue),
):
    # Resume any interrupted threads whose token has expired, then mark them cleaned up.
    for entry in queue.get_stale():
        config = {"configurable": {"thread_id": entry["thread_id"]}}
        graph.invoke(Command(resume="expired"), config=config)
        queue.resolve(entry["approval_id"], "expired")

    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "pending": queue.get_pending(),
            "expired": queue.get_expired(),
        },
    )


@app.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    req: DecideRequest,
    graph=Depends(get_graph),
    queue: ApprovalQueue = Depends(get_queue),
):
    # Verify token before touching the graph — catches expired and tampered tokens.
    try:
        verify_token(req.token)
    except TokenExpired:
        raise HTTPException(status_code=400, detail="Token expired.")
    except TokenTampered:
        raise HTTPException(status_code=400, detail="Token tampered.")

    entry = queue.get_by_id(approval_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Approval not found.")

    thread_id = entry["thread_id"]
    config = {"configurable": {"thread_id": thread_id}}

    try:
        graph.invoke(Command(resume=req.decision), config=config)
    except AgentTimeoutError:
        # The interrupt checkpoint was consumed by this invocation even though the
        # node failed, so the thread is no longer resumable. Mark the entry expired
        # now so the TTL cleanup in GET /approvals does not try to resume it again.
        queue.resolve(approval_id, "expired")
        raise HTTPException(
            status_code=504,
            detail="Approval processing timed out. The action may not have completed.",
        )

    return {"status": "ok", "decision": req.decision, "approval_id": approval_id}
