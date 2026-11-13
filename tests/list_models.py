"""Developer probe: list Gemini models that support generateContent for the configured key.

Usage:  python tests/list_models.py
Prints model names only (never the key).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst import config  # noqa: E402

config.load_api_key()
from google import genai  # noqa: E402

client = genai.Client()
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions and "flash" in (m.name or ""):
        print(m.name)
