import concurrent.futures
import os
import threading
import time
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agent.dispatcher import classify_result
from approval.queue import ApprovalQueue
from db.database import create_escalation, update_order_status
from tools.escalation import escalate_case
from tools.lookup import lookup_order
from tools.refund import request_refund

_TOOL_FUNCTIONS = {
    "lookup_order": lookup_order,
    "request_refund": request_refund,
    "escalate_case": escalate_case,
}

_AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT_SECONDS", "30"))
# Set to a positive integer to simulate slow approval execution (demo only).
_DEMO_APPROVAL_DELAY = int(os.getenv("DEMO_SLOW_APPROVAL_SECONDS", "0"))


class AgentTimeoutError(RuntimeError):
    """Raised when an LLM call or tool execution exceeds the configured deadline."""


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    customer_id: str


def build_graph(llm, queue: ApprovalQueue, db_path: str | None = None, timeout: int | None = _AGENT_TIMEOUT):
    """
    Compile and return the LangGraph agent.

    Args:
        llm:      A chat model (or mock). Should already be bound with tool
                  schemas when used in production; mocks can pass as-is.
        queue:    ApprovalQueue instance shared with the web layer.
        db_path:  Optional path to the SQLite DB (used in tests).
        timeout:  Seconds before an LLM call or tool execution is aborted with
                  AgentTimeoutError. Pass None to disable. Defaults to
                  AGENT_TIMEOUT_SECONDS env var (30 s).
    """

    # Shared cancellation flag: set by _run_with_timeout on deadline, checked by
    # _execute_approved before the DB write so the background thread exits cleanly.
    _demo_cancel = threading.Event()

    def _run_with_timeout(fn, *args, **kwargs):
        """Run fn(*args, **kwargs) in a thread; raise AgentTimeoutError if it exceeds timeout."""
        if timeout is None:
            return fn(*args, **kwargs)
        _demo_cancel.clear()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            _demo_cancel.set()
            executor.shutdown(wait=False)
            raise AgentTimeoutError(
                f"{fn.__name__} exceeded the {timeout}s deadline."
            )

    def _execute_approved(tool_name: str, payload: dict) -> str:
        """Carry out the approved action and return a confirmation string."""
        if _DEMO_APPROVAL_DELAY:
            # wait() returns True if the event was set (i.e. cancelled), False on timeout.
            if _demo_cancel.wait(timeout=_DEMO_APPROVAL_DELAY):
                return ""  # timeout fired; skip DB write and exit cleanly
        if tool_name == "request_refund":
            update_order_status(
                payload["order_id"], payload["customer_id"], "refunded", db_path=db_path
            )
            return (
                f"Refund of ${payload['amount']:.2f} for order {payload['order_id']} "
                f"has been processed."
            )
        if tool_name == "escalate_case":
            create_escalation(
                payload["order_id"], payload["customer_id"], payload["reason"], db_path=db_path
            )
            return (
                f"Order {payload['order_id']} has been escalated to a human agent."
            )
        return f"Action '{tool_name}' was approved and executed."

    def agent_node(state: AgentState):
        response = _run_with_timeout(llm.invoke, state["messages"])
        return {"messages": [response]}

    def tool_executor_node(state: AgentState, config: RunnableConfig):
        last = state["messages"][-1]
        thread_id = config.get("configurable", {}).get("thread_id", "")
        tool_messages = []

        for tc in last.tool_calls:
            fn = _TOOL_FUNCTIONS[tc["name"]]
            args = dict(tc["args"])
            args["customer_id"] = state["customer_id"]

            if db_path:
                args["db_path"] = db_path

            result = _run_with_timeout(fn, **args)

            if classify_result(result) == "GATED":
                # Use the LLM tool-call ID as approval_id — it's deterministic
                # so the idempotent guard below works correctly when LangGraph
                # replays this node on resume.
                approval_id = tc["id"]
                if not queue.get_by_id(approval_id):
                    queue.add(approval_id, result.tool_name, result.payload, thread_id=thread_id)
                # Pause here; resume value is the human decision string.
                decision = interrupt(
                    {"approval_id": approval_id, "action": result.tool_name}
                )
                if decision == "approved":
                    # Resolve only after execution succeeds; if _execute_approved raises
                    # AgentTimeoutError the entry stays pending so the dashboard still shows it.
                    content = _run_with_timeout(_execute_approved, result.tool_name, result.payload)
                    queue.resolve(approval_id, decision)
                elif decision == "expired":
                    queue.resolve(approval_id, decision)
                    content = (
                        f"Your {result.tool_name.replace('_', ' ')} request timed out "
                        "and was not processed. Please try again."
                    )
                else:
                    queue.resolve(approval_id, decision)
                    content = (
                        f"Your {result.tool_name.replace('_', ' ')} request was not approved."
                    )
            else:
                content = str(result)

            tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"]))

        return {"messages": tool_messages}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.set_entry_point("agent")
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_executor_node)
    graph.add_conditional_edges("agent", should_continue)
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=MemorySaver())
