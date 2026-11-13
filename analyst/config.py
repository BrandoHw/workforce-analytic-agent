"""Configuration helpers: Gemini API key resolution that works locally and on Streamlit Cloud."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("WORKFORCE_MODEL", "gemini-2.5-flash")
JUDGE_MODEL = os.environ.get("WORKFORCE_JUDGE_MODEL", "gemini-2.5-flash")
MAX_LLM_CALLS_PER_TURN = 25  # hard ceiling so a looping agent cannot run up the bill

# Gemini 2.5 "thinking" tokens bill as output and dominate latency. Cap them per agent role.
# 0 disables thinking, -1 lets the model decide. Override with env vars for experiments.
THINKING_BUDGET_COORDINATOR = int(os.environ.get("WORKFORCE_THINK_COORDINATOR", "0"))
THINKING_BUDGET_SPECIALIST = int(os.environ.get("WORKFORCE_THINK_SPECIALIST", "4096"))


def load_api_key() -> str:
    """Resolve GOOGLE_API_KEY and export it for the Google GenAI SDK.

    Order: environment variable, then .streamlit/secrets.toml (local runs and the CLI).
    The Streamlit app additionally checks `st.secrets` first (see app.py).
    """
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        path = os.path.join(ROOT, ".streamlit", "secrets.toml")
        if os.path.exists(path):
            import tomllib
            with open(path, "rb") as f:
                key = tomllib.load(f).get("GOOGLE_API_KEY", "")
    if key and not key.startswith("your_"):
        os.environ["GOOGLE_API_KEY"] = key
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
        return key
    return ""
