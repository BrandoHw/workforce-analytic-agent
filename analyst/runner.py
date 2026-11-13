"""Streamlit-free turn runner shared by the web app, the eval harness and the CLI.

    from analyst.runner import run_turn
    result = run_turn("Which shifts are understaffed next week?", session_id="demo")

CLI (needs GOOGLE_API_KEY in the environment or .streamlit/secrets.toml):
    python -m analyst.runner "Which shifts are understaffed next week?"
"""
import sys
import time
from dataclasses import dataclass, field

from google.genai import types
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

import bootstrap_data
from analyst import config, guardrails, observability
from analyst.agents import build_enterprise_agent
from analyst.aio import run_sync

APP_NAME = "workforce_analyst"
USER_ID = "planner"
ROOT_AGENT = "enterprise_analyst_agent"

# Conversation history lives here; it survives Streamlit reruns because modules are cached.
SESSION_SERVICE = InMemorySessionService()


@dataclass
class TurnResult:
    reply: str
    delegations: list[str] = field(default_factory=list)   # specialists the coordinator called
    mcp_calls: list[dict] = field(default_factory=list)    # [{"agent":..., "tool":..., "args":...}]
    blocked: bool = False                                  # stopped by the input guardrail
    error: str | None = None
    elapsed_s: float = 0.0
    calls: int = 0                                         # model calls this turn
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def tools_called(self) -> list[str]:
        """Every tool name in call order: specialists first-seen as delegations, then MCP tools."""
        return self.delegations + [c["tool"] for c in self.mcp_calls]


def _is_guardrail_reply(text: str) -> bool:
    return text.startswith("[GUARDRAIL]")


async def run_turn_async(prompt: str, session_id: str, model_factory=None) -> TurnResult:
    bootstrap_data.bootstrap()                     # no-op when enterprise.db and policies/ exist
    started = time.perf_counter()
    trace_before = len(observability.get_tool_trace(session_id))
    usage_before = observability.LEDGER.records(session_id)

    if await SESSION_SERVICE.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id) is None:
        await SESSION_SERVICE.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id,
            state={guardrails.TRACKING_KEY: session_id},   # copied into specialists' nested sessions
        )

    agent = build_enterprise_agent(model_factory)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=SESSION_SERVICE)
    reply_parts: list[str] = []
    error = None
    try:
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            run_config=RunConfig(max_llm_calls=config.MAX_LLM_CALLS_PER_TURN),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                reply_parts.extend(p.text for p in event.content.parts if p.text and not p.thought)
            if event.error_message:
                error = event.error_message
    except Exception as exc:  # quota, network, model errors: surface them in the UI, do not crash it
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await runner.close()

    raw = "".join(reply_parts).strip()
    reply = guardrails.sanitize_output(raw) if raw else ""
    trace = observability.get_tool_trace(session_id)[trace_before:]
    usage = observability.LEDGER.records(session_id)[len(usage_before):]
    return TurnResult(
        reply=reply or (f"The agent could not complete this request. ({error})" if error else "No response generated."),
        delegations=[t["tool"] for t in trace if t["agent"] == ROOT_AGENT],
        mcp_calls=[t for t in trace if t["agent"] != ROOT_AGENT],
        blocked=_is_guardrail_reply(reply),
        error=error,
        elapsed_s=time.perf_counter() - started,
        calls=len(usage),
        prompt_tokens=sum(u.prompt_tokens for u in usage),
        completion_tokens=sum(u.completion_tokens + u.thinking_tokens for u in usage),
        cost_usd=sum(u.cost_usd for u in usage),
    )


def run_turn(prompt: str, session_id: str, model_factory=None) -> TurnResult:
    """Synchronous wrapper: runs the turn on the shared background event loop (see analyst.aio)."""
    return run_sync(run_turn_async(prompt, session_id, model_factory))


def _cli() -> int:
    if not config.load_api_key():
        print("GOOGLE_API_KEY not found. Set the env var or create .streamlit/secrets.toml.")
        return 2
    prompt = " ".join(sys.argv[1:]).strip() or "Which shifts are understaffed next week?"
    r = run_turn(prompt, session_id="cli")
    print(f"\n{r.reply}\n")
    print(f"delegated to : {r.delegations or '-'}")
    print(f"MCP calls    : {[c['tool'] for c in r.mcp_calls] or '-'}")
    print(f"model calls  : {r.calls}   tokens: {r.prompt_tokens} in / {r.completion_tokens} out   "
          f"cost: ${r.cost_usd:.5f}   time: {r.elapsed_s:.1f}s")
    for e in guardrails.get_events("cli"):
        print(f"guardrail    : {e['action']} {e['detail']}")
    return 1 if r.error else 0


if __name__ == "__main__":
    sys.exit(_cli())
