"""One persistent background event loop for all agent work.

Streamlit runs each script rerun on a short-lived thread, and `asyncio.run()` per turn would create
and close an event loop every time. The Gemini HTTP client and MCP sessions are tied to the loop that
created them, so closing the loop while they are still being cleaned up produces
"Event loop is closed" errors. A single long-lived loop on a daemon thread avoids that, and lets
concurrent chat sessions share it safely (everything on it is async).
"""
import asyncio
import threading

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="analyst-event-loop").start()
        return _loop


def run_sync(coro):
    """Run a coroutine on the shared loop and block until it finishes."""
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()
