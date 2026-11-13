"""Step 5a: input-level guardrail (`before_model_guardrail`).

Runs before EVERY model call of EVERY agent, so nothing reaches Gemini unchecked:

  1. Prompt-injection and destructive-intent rejection. The user's latest message is checked
     and a canned reply is returned directly. The model is never invoked.
  2. In-place PII redaction of everything the model is about to read: user text, earlier
     turns and tool results (so emails and phone numbers stored in the database never
     reach the model either).
       - credit card numbers (Luhn-validated), national IDs (NRIC / SSN)
       - email addresses, phone numbers
       - staff names are pseudonymised to staff IDs (Daniel Lim -> EMP_002)

`sanitize_output` applies the same redaction to the final answer before it is shown.
Every intervention is appended to GUARDRAIL_LOG for the UI. Logged text is itself redacted.
"""
import os
import re
import sqlite3
import threading
import time
import unicodedata

from google.genai import types
from google.adk.models import LlmResponse

import bootstrap_data

# ------------------------------------------------------------------------------
# Intervention log (shown in the Streamlit "Guardrail log")
# ------------------------------------------------------------------------------
GUARDRAIL_LOG: list[dict] = []
_LOG_LOCK = threading.Lock()


def log_event(action: str, agent: str, session_id: str, detail: str) -> None:
    with _LOG_LOCK:
        GUARDRAIL_LOG.append({
            "time": time.strftime("%H:%M:%S"),
            "session_id": session_id,
            "agent": agent,
            "action": action,
            "detail": detail,
        })


def get_events(session_id: str | None = None) -> list[dict]:
    with _LOG_LOCK:
        return [e for e in GUARDRAIL_LOG if session_id is None or e["session_id"] == session_id]


def clear_events(session_id: str | None = None) -> None:
    with _LOG_LOCK:
        GUARDRAIL_LOG[:] = [e for e in GUARDRAIL_LOG if session_id is not None and e["session_id"] != session_id]


# ------------------------------------------------------------------------------
# PII redaction
# ------------------------------------------------------------------------------
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
NATIONAL_ID = re.compile(r"(?<![\w-])(?:\d{6}-\d{2}-\d{4}|\d{3}-\d{2}-\d{4})(?![\w-])")  # NRIC, US SSN
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
PHONE = re.compile(
    r"(?<![\w.-])(?:"
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]?\d{3,4}"  # 012-345 1001, (555) 123-4567
    r"|(?:\+?60|0)1\d{8,9}"                                                           # unseparated Malaysian mobile
    r")(?![\w-])"
)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _card_sub(counts: dict):
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            counts["credit_card"] = counts.get("credit_card", 0) + 1
            return "[REDACTED_CARD]"
        return m.group(0)
    return repl


def _simple_sub(label: str, token: str, counts: dict):
    def repl(_m: re.Match) -> str:
        counts[label] = counts.get(label, 0) + 1
        return token
    return repl


def redact_pii(text: str) -> tuple[str, dict]:
    """Mask cards, national IDs, emails and phones. Returns (clean_text, {kind: count})."""
    counts: dict = {}
    text = CARD.sub(_card_sub(counts), text)
    text = NATIONAL_ID.sub(_simple_sub("national_id", "[REDACTED_ID]", counts), text)
    text = EMAIL.sub(_simple_sub("email", "[REDACTED_EMAIL]", counts), text)
    text = PHONE.sub(_simple_sub("phone", "[REDACTED_PHONE]", counts), text)
    return text, counts


# Staff-name pseudonymisation: names come from the database, replaced by the staff ID.
_NAME_CACHE: dict = {"mtime": None, "pattern": None, "map": {}}
_NAME_LOCK = threading.Lock()


def _staff_name_pattern():
    path = bootstrap_data.DB_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, {}
    with _NAME_LOCK:
        if _NAME_CACHE["mtime"] != mtime:
            mapping = {}
            try:
                conn = sqlite3.connect(f"file:{os.path.abspath(path).replace(chr(92), '/')}?mode=ro", uri=True)
                try:
                    mapping = {n.lower(): i for i, n in conn.execute("SELECT employee_id, full_name FROM employees")}
                finally:
                    conn.close()
            except sqlite3.Error:
                mapping = {}
            pattern = None
            if mapping:
                names = sorted(mapping, key=len, reverse=True)
                pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)
            _NAME_CACHE.update(mtime=mtime, pattern=pattern, map=mapping)
        return _NAME_CACHE["pattern"], _NAME_CACHE["map"]


def pseudonymize_staff_names(text: str) -> tuple[str, int]:
    pattern, mapping = _staff_name_pattern()
    if not pattern:
        return text, 0
    return pattern.subn(lambda m: mapping[m.group(0).lower()], text)


def sanitize_text(text: str) -> tuple[str, dict]:
    """Full masking pass: PII patterns plus staff-name pseudonymisation."""
    text, counts = redact_pii(text)
    text, n = pseudonymize_staff_names(text)
    if n:
        counts["staff_name"] = n
    return text, counts


def sanitize_output(text: str) -> str:
    """Final-answer filter used by the app so nothing sensitive is ever rendered."""
    return sanitize_text(text)[0]


def _sanitize_value(value, counts: dict):
    """Recursively sanitise strings inside tool-result payloads (dicts / lists / strings)."""
    if isinstance(value, str):
        clean, c = sanitize_text(value)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return clean
    if isinstance(value, dict):
        return {k: _sanitize_value(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v, counts) for v in value]
    return value


# ------------------------------------------------------------------------------
# Prompt-injection and destructive-intent detection
# ------------------------------------------------------------------------------
_INJECTION = [re.compile(p) for p in [
    r"ignore (?:all |any |the |your |my )*(?:previous |prior |above |earlier |preceding |former )?"
    r"(?:instructions|prompts?|rules|directives|guidelines)",
    r"disregard (?:all |any |the |your |previous |prior |above )*(?:instructions|rules|guidelines|polic(?:y|ies)|prompts?)",
    r"forget (?:all |everything |your |the )*(?:previous |prior )?(?:instructions|rules|training)",
    r"(?:reveal|show|print|display|repeat|output|leak|tell me)\W+(?:me\W+)?(?:your|the)\W+"
    r"(?:system|hidden|initial|original|secret)\W+(?:prompt|instructions?)",
    r"what (?:is|are) your (?:system|hidden|initial) (?:prompt|instructions)",
    r"\byou are now\b.{0,40}\b(?:dan|unrestricted|unfiltered|jailbroken|developer mode|evil)",
    r"\bact as (?:dan\b|an? (?:unrestricted|unfiltered|jailbroken))",
    r"\bdan mode\b|\bdeveloper mode\b|\bjailbreak",
    r"(?:override|bypass|disable|turn off) (?:your |the |all )*(?:safety|security|guardrails?|filters?|restrictions|rules|policy)",
    r"pretend (?:that )?you (?:have no|are not bound|don't have|do not have)",
    r"<\s*/?\s*system\s*>|\[\s*system\s*\]",
]]

_DESTRUCTIVE_NOUNS = (
    r"(?:tables?|database|db|records?|rows?|data|everything|assignments?|shifts?|employees?|schedules?|policies)"
)
_MUTATION = [re.compile(p) for p in [
    r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
    r"\bdelete\s+from\b",
    r"\binsert\s+into\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\balter\s+table\b",
    r"\b(?:delete|drop|wipe|erase|destroy|truncate)\b.{0,30}\b" + _DESTRUCTIVE_NOUNS + r"\b",
]]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)  # zero-width characters
    return re.sub(r"\s+", " ", text).lower().strip()


def detect_injection(text: str) -> bool:
    t = _normalize(text)
    return any(p.search(t) for p in _INJECTION)


def detect_mutation_intent(text: str) -> bool:
    t = _normalize(text)
    return any(p.search(t) for p in _MUTATION)


INJECTION_REPLY = (
    "[GUARDRAIL] Request blocked by the prompt-injection policy. I can't change my instructions, "
    "reveal internal prompts or bypass safeguards. Ask me about shifts, coverage, assignments or policies."
)
MUTATION_REPLY = (
    "[GUARDRAIL] Request blocked by the read-only data policy. This assistant can analyse and "
    "propose, but it cannot delete, drop, update or insert data. I can show you the affected "
    "records or propose a schedule change for a human to apply."
)


def _blocked(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _latest_user_text(llm_request) -> str:
    """The most recent user-authored text (skips tool results, which also carry role=user)."""
    for content in reversed(llm_request.contents or []):
        if content.role != "user":
            continue
        texts = [p.text for p in (content.parts or []) if getattr(p, "text", None)]
        if texts:
            return " ".join(texts)
    return ""


TRACKING_KEY = "tracking_id"


def session_key(callback_context) -> str:
    """Stable id of the user's chat session.

    A specialist agent invoked as a tool runs in a nested ADK session with a fresh id, but the
    parent's state is copied into it. The runner stores `tracking_id` in state so guardrail,
    cost and trace records from every agent roll up to the same user session.
    """
    try:
        tracked = callback_context.state.get(TRACKING_KEY)
        if tracked:
            return str(tracked)
    except Exception:
        pass
    try:
        return callback_context.session.id
    except Exception:
        return "unknown"


def _ids(callback_context) -> tuple[str, str]:
    return getattr(callback_context, "agent_name", "unknown"), session_key(callback_context)


# ------------------------------------------------------------------------------
# The ADK callback
# ------------------------------------------------------------------------------
def before_model_guardrail(callback_context, llm_request):
    """ADK before_model_callback. Returns an LlmResponse to short-circuit the model, else None."""
    agent, session_id = _ids(callback_context)

    # 1. Block before spending a single token.
    user_text = _latest_user_text(llm_request)
    if user_text:
        if detect_injection(user_text):
            log_event("INJECTION_BLOCKED", agent, session_id, sanitize_text(user_text[:160])[0])
            return _blocked(INJECTION_REPLY)
        if detect_mutation_intent(user_text):
            log_event("MUTATION_BLOCKED", agent, session_id, sanitize_text(user_text[:160])[0])
            return _blocked(MUTATION_REPLY)

    # 2. Redact in place everything the model is about to read.
    totals: dict = {}
    for content in llm_request.contents or []:
        for part in content.parts or []:
            if getattr(part, "text", None):
                part.text, counts = sanitize_text(part.text)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
            fr = getattr(part, "function_response", None)
            if fr is not None and fr.response is not None:
                fr.response = _sanitize_value(fr.response, totals)
    if totals:
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(totals.items()))
        log_event("PII_REDACTED", agent, session_id, summary)
    return None
