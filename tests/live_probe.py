"""Developer probe: run one live question and print the answer with the full agent/MCP/SQL trace.

Usage:  python tests/live_probe.py "Is anyone scheduled to work while on approved leave?"
Not part of the offline test suite (it calls the real model).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst import config  # noqa: E402

if not config.load_api_key():
    sys.exit("No GOOGLE_API_KEY configured (set it in .streamlit/secrets.toml or the environment).")

from analyst.runner import run_turn  # noqa: E402

question = " ".join(sys.argv[1:]) or "Is anyone scheduled to work while on approved leave?"
r = run_turn(question, "probe")
print("ANSWER:\n" + r.reply + "\n")
print(f"calls={r.calls} tokens={r.prompt_tokens}/{r.completion_tokens} cost=${r.cost_usd:.4f} time={r.elapsed_s:.1f}s\n")
for c in r.mcp_calls:
    print(f"[{c['agent']}] {c['tool']}")
    for k, v in c["args"].items():
        print(f"    {k}: {v}")
