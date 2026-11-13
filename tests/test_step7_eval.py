"""Step 7 verification: the evaluation harness plumbing, with scripted candidate and judge models.

No API key needed. This proves the harness itself is correct (golden file valid, deterministic
checks catch failures, judge output parsed and scored, pass/fail logic). The real quality
measurement is `python eval/run_eval.py`, which uses Gemini for both candidate and judge.

Run:  .\\.venv\\Scripts\\python.exe tests\\test_step7_eval.py
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from google.genai import types  # noqa: E402
from google.adk.models import LlmResponse  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models._capabilities import LlmCapabilities  # noqa: E402

import bootstrap_data  # noqa: E402
from analyst import evaluation  # noqa: E402
from analyst.runner import TurnResult  # noqa: E402
from test_step6_agents import fake_factory  # noqa: E402  (scripted candidate models)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class ScriptedJudge(BaseLlm):
    """Scores 5 when the candidate answer contains a known key fact or a guardrail block, else 2."""
    model: str = "fake-judge"
    out_of_range: bool = False

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(self, llm_request, stream=False):
        prompt = " ".join(p.text for c in llm_request.contents for p in (c.parts or []) if p.text)
        candidate = prompt.split("CANDIDATE ANSWER:")[1].split("TRAJECTORY:")[0]
        good = any(k in candidate for k in ("SHF_003", "[GUARDRAIL]", "11 hours of rest"))
        score = 9 if self.out_of_range else (5 if good else 2)
        verdict = {"score": score, "rationale": "scripted verdict", "missing_or_wrong": "" if good else "key facts"}
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="```json\n" + json.dumps(verdict) + "\n```")]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=400, candidates_token_count=40, total_token_count=440),
            model_version="gemini-2.5-flash",
        )


def test_golden_file() -> None:
    print("\nA. Golden dataset")
    cases = evaluation.load_golden()
    ids = [c["id"] for c in cases]
    check("at least 3 golden test cases", len(cases) >= 3, str(len(cases)))
    check("case ids are unique", len(ids) == len(set(ids)))
    check("every case has id, category, question and reference",
          all(all(c.get(k) for k in ("id", "category", "question", "reference")) for c in cases))
    cats = {c["category"] for c in cases}
    check("covers data, policy, planning and guardrail cases", {"data", "policy", "planning", "guardrail"} <= cats, str(cats))


def test_deterministic_checks() -> None:
    print("\nB. Deterministic checks")
    case = {"expected_agents": ["coverage_analyst"], "expected_tools": ["mcp_read_query"],
            "must_contain": ["SHF_003"], "must_not_contain": ["@"], "expect_blocked": False}
    good = TurnResult(reply="SHF_003 is short", delegations=["coverage_analyst"], mcp_calls=[{"tool": "mcp_read_query"}])
    check("a correct result passes every check", all(c["ok"] for c in evaluation.deterministic_checks(case, good)))

    bad = TurnResult(reply="Contact bob@x.example", delegations=[], mcp_calls=[], blocked=True, error="boom")
    failed = {c["name"] for c in evaluation.deterministic_checks(case, bad) if not c["ok"]}
    for expected in ("no runtime error", "request was not blocked", "delegated to coverage_analyst",
                     "used mcp_read_query", "answer contains 'SHF_003'", "answer does not contain '@'"):
        check(f"catches: {expected}", expected in failed, str(failed))


def test_judge() -> None:
    print("\nC. Judge agent")
    case = {"question": "q", "reference": "SHF_003 is understaffed"}
    good = TurnResult(reply="SHF_003 is understaffed")
    bad = TurnResult(reply="I do not know")
    v, cost, err = asyncio.run(evaluation.judge_async(case, good, lambda: ScriptedJudge()))
    check("judge returns a parsed verdict (JSON inside a code fence)", v is not None and v.score == 5, str(err))
    check("judge spend is tracked", cost > 0, str(cost))
    v, _, _ = asyncio.run(evaluation.judge_async(case, bad, lambda: ScriptedJudge()))
    check("judge scores a wrong answer low", v is not None and v.score == 2)
    v, _, _ = asyncio.run(evaluation.judge_async(case, good, lambda: ScriptedJudge(out_of_range=True)))
    check("an out-of-range score is clamped to 5", v is not None and v.score == 5, str(v))


def test_end_to_end() -> None:
    print("\nD. Full harness run (scripted candidate + scripted judge)")
    out_path = evaluation.RESULTS_PATH
    backup = open(out_path, encoding="utf-8").read() if os.path.exists(out_path) else None
    try:
        report = evaluation.run_evaluation(
            ["q1_coverage_gaps", "q4_policy_rest_and_overtime", "g1_prompt_injection", "g2_destructive_request"],
            model_factory=fake_factory, judge_model_factory=lambda: ScriptedJudge(), save=True)
        s, res = report["summary"], {r["id"]: r for r in report["results"]}
        check("all four selected cases ran", s["cases"] == 4, str(s))
        check("q1 passes (real gaps found via MCP, judged 5)", res["q1_coverage_gaps"]["passed"], str(res["q1_coverage_gaps"]["checks"]))
        check("q4 passes (policy grounded)", res["q4_policy_rest_and_overtime"]["passed"], str(res["q4_policy_rest_and_overtime"]["checks"]))
        check("injection case passes via guardrail block", res["g1_prompt_injection"]["passed"] and res["g1_prompt_injection"]["blocked"])
        check("destructive case passes via guardrail block", res["g2_destructive_request"]["passed"])
        check("summary reports average score and pass rate", s["average_score"] == 5 and s["pass_rate"] == 1.0, str(s))
        check("results file written and reloadable", evaluation.load_last_report()["summary"]["cases"] == 4)
        check("cost is reported for agent and judge", s["agent_cost_usd"] > 0 and s["judge_cost_usd"] > 0, str(s))
    finally:
        if backup is not None:
            open(out_path, "w", encoding="utf-8").write(backup)
        elif os.path.exists(out_path):
            os.remove(out_path)


def main() -> int:
    bootstrap_data.bootstrap()
    test_golden_file()
    test_deterministic_checks()
    test_judge()
    test_end_to_end()
    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILED: ' + str(len(FAILURES)) + ' check(s)'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
