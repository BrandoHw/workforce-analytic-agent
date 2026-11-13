"""Step 5b: post-model telemetry (`after_model_cost_tracker`).

Runs after EVERY model completion of EVERY agent. It reads `usage_metadata`, accumulates prompt
and completion token counts, estimates USD cost and records which agent made the call, so
the coordinator/specialist split is visible in the cost breakdown.

PRICING: USD per 1M tokens (Gemini API paid tier, standard text rates). These are list prices
that change over time. Verify them at https://ai.google.dev/gemini-api/docs/pricing and edit
the table below. The estimate is for demo observability, not billing.
"""
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass

from analyst.guardrails import session_key

logger = logging.getLogger("workforce_analyst.usage")

PRICING_USD_PER_1M = {
    # model prefix:          input, output (thinking tokens bill as output), cached input
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.01},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
}
DEFAULT_MODEL = "gemini-2.5-flash"

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
USAGE_LOG_FILE = os.path.join(LOG_DIR, "usage.jsonl")


def pricing_for(model: str | None) -> tuple[str, dict]:
    """Longest-prefix match so 'gemini-2.5-flash-lite-001' prices as flash-lite."""
    name = (model or DEFAULT_MODEL).lower().removeprefix("models/")
    for prefix in sorted(PRICING_USD_PER_1M, key=len, reverse=True):
        if name.startswith(prefix):
            return prefix, PRICING_USD_PER_1M[prefix]
    return DEFAULT_MODEL, PRICING_USD_PER_1M[DEFAULT_MODEL]


def estimate_cost_usd(model, prompt_tokens, completion_tokens, thinking_tokens=0, cached_tokens=0) -> float:
    _, p = pricing_for(model)
    fresh_input = max(prompt_tokens - cached_tokens, 0)
    cost = (
        fresh_input * p["input"]
        + cached_tokens * p["cached_input"]
        + (completion_tokens + thinking_tokens) * p["output"]
    ) / 1_000_000
    return cost


@dataclass
class UsageRecord:
    time: str
    session_id: str
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    thinking_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_usd: float


class UsageLedger:
    """Thread-safe store of per-call usage. The Streamlit sidebar reads from it."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def add(self, record: UsageRecord) -> None:
        with self._lock:
            self._records.append(record)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(USAGE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except OSError:
            pass  # read-only filesystems must never break an answer
        logger.info(
            "model_call agent=%s model=%s prompt=%d completion=%d thinking=%d cost_usd=%.6f",
            record.agent, record.model, record.prompt_tokens, record.completion_tokens,
            record.thinking_tokens, record.cost_usd,
        )

    def records(self, session_id: str | None = None) -> list[UsageRecord]:
        with self._lock:
            return [r for r in self._records if session_id is None or r.session_id == session_id]

    def reset(self, session_id: str | None = None) -> None:
        with self._lock:
            self._records = [] if session_id is None else [r for r in self._records if r.session_id != session_id]

    def summary(self, session_id: str | None = None) -> dict:
        recs = self.records(session_id)
        by_agent: dict[str, dict] = {}
        for r in recs:
            a = by_agent.setdefault(r.agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
            a["calls"] += 1
            a["prompt_tokens"] += r.prompt_tokens
            a["completion_tokens"] += r.completion_tokens + r.thinking_tokens
            a["cost_usd"] += r.cost_usd
        return {
            "calls": len(recs),
            "prompt_tokens": sum(r.prompt_tokens for r in recs),
            "completion_tokens": sum(r.completion_tokens + r.thinking_tokens for r in recs),
            "total_tokens": sum(r.total_tokens for r in recs),
            "cost_usd": sum(r.cost_usd for r in recs),
            "by_agent": by_agent,
        }


LEDGER = UsageLedger()


# ------------------------------------------------------------------------------
# Tool-call trace: which agent called which tool (delegations and MCP calls).
# Powers the "MCP calls" caption in the UI and the trajectory checks in the eval harness.
# ------------------------------------------------------------------------------
TOOL_TRACE: list[dict] = []
_TRACE_LOCK = threading.Lock()


def get_tool_trace(session_id: str | None = None) -> list[dict]:
    with _TRACE_LOCK:
        return [t for t in TOOL_TRACE if session_id is None or t["session_id"] == session_id]


def clear_tool_trace(session_id: str | None = None) -> None:
    with _TRACE_LOCK:
        TOOL_TRACE[:] = [] if session_id is None else [t for t in TOOL_TRACE if t["session_id"] != session_id]


def trace_tool_call(tool, args, tool_context):
    """ADK before_tool_callback. Records the call and never alters or blocks it."""
    with _TRACE_LOCK:
        TOOL_TRACE.append({
            "time": time.strftime("%H:%M:%S"),
            "session_id": session_key(tool_context),
            "agent": getattr(tool_context, "agent_name", "unknown"),
            "tool": getattr(tool, "name", str(tool)),
            "args": {k: (v if len(str(v)) <= 300 else str(v)[:300] + "...") for k, v in (args or {}).items()},
        })
    return None


def after_model_cost_tracker(callback_context, llm_response):
    """ADK after_model_callback. Observes only; always returns None so the response is unchanged."""
    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None  # e.g. guardrail short-circuit replies: no model call happened, no cost

    prompt = (usage.prompt_token_count or 0) + (getattr(usage, "tool_use_prompt_token_count", 0) or 0)
    completion = usage.candidates_token_count or 0
    thinking = getattr(usage, "thoughts_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    total = usage.total_token_count or (prompt + completion + thinking)
    model = getattr(llm_response, "model_version", None) or DEFAULT_MODEL

    LEDGER.add(UsageRecord(
        time=time.strftime("%H:%M:%S"),
        session_id=session_key(callback_context),
        agent=getattr(callback_context, "agent_name", "unknown"),
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        thinking_tokens=thinking,
        cached_tokens=cached,
        total_tokens=total,
        cost_usd=estimate_cost_usd(model, prompt, completion, thinking, cached),
    ))
    return None
