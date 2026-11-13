"""Developer probe: one direct Gemini call (no ADK) to check a model's availability, latency and token usage.

Usage:  python tests/model_smoke.py <model> [thinking_budget] [--tools] [--system]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst import config  # noqa: E402

config.load_api_key()
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = {a for a in sys.argv[1:] if a.startswith("--")}
model = args[0]
budget = int(args[1]) if len(args) > 1 else None

tools = None
if "--tools" in flags:
    tools = [types.Tool(function_declarations=[types.FunctionDeclaration(
        name="lookup", description="Look something up", parameters_json_schema={
            "type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]})])]
cfg = types.GenerateContentConfig(
    http_options=types.HttpOptions(timeout=45_000),
    thinking_config=types.ThinkingConfig(thinking_budget=budget) if budget is not None else None,
    tools=tools,
    system_instruction="You are a terse assistant." if "--system" in flags else None,
)
t0 = time.time()
try:
    client = genai.Client()  # keep a reference: a temporary client is closed when garbage collected
    r = client.models.generate_content(model=model, contents="Reply with the single word: ready", config=cfg)
    u = r.usage_metadata
    print(f"OK {model} budget={budget} flags={sorted(flags)} {time.time() - t0:.1f}s text={r.text!r} "
          f"prompt={u.prompt_token_count} out={u.candidates_token_count} thoughts={u.thoughts_token_count}")
except Exception as exc:  # noqa: BLE001
    print(f"FAIL {model} budget={budget} flags={sorted(flags)} {time.time() - t0:.1f}s "
          f"{type(exc).__name__}: {str(exc)[:200]}")
