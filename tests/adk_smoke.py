"""Developer probe: minimal ADK agent turn to isolate model/endpoint problems from the app's own wiring.

Usage:  python tests/adk_smoke.py <model> [thinking_budget]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst import config  # noqa: E402

config.load_api_key()
from google.adk.agents import Agent  # noqa: E402
from google.adk.planners import BuiltInPlanner  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

model = sys.argv[1]
budget = int(sys.argv[2]) if len(sys.argv) > 2 else None


async def main() -> None:
    planner = BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_budget=budget)) if budget is not None else None
    agent = Agent(name="smoke", model=model, instruction="Be terse.", planner=planner)
    svc = InMemorySessionService()
    await svc.create_session(app_name="smoke", user_id="u", session_id="s")
    runner = Runner(agent=agent, app_name="smoke", session_service=svc)
    msg = types.Content(role="user", parts=[types.Part(text="Reply with the single word: ready")])
    t0 = time.time()
    try:
        async for ev in runner.run_async(user_id="u", session_id="s", new_message=msg):
            if ev.is_final_response() and ev.content and ev.content.parts:
                print(f"OK {model} budget={budget} {time.time() - t0:.1f}s text={ev.content.parts[0].text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {model} budget={budget} {time.time() - t0:.1f}s {type(exc).__name__}: {str(exc)[:200]}")


asyncio.run(main())
