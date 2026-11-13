"""Run the golden evaluation from the command line.

    python eval/run_eval.py                     # all cases
    python eval/run_eval.py --only q1_coverage_gaps,g1_prompt_injection

Needs GOOGLE_API_KEY (environment or .streamlit/secrets.toml). Writes eval/results.json.
Exit code 0 only if every case passes.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst import config, evaluation  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden-dataset evaluation with an LLM judge.")
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()

    if not config.load_api_key():
        print("GOOGLE_API_KEY not found. Set the env var or create .streamlit/secrets.toml.")
        return 2

    def progress(i, n, r):
        status = "PASS" if r["passed"] else "FAIL"
        failed = [c["name"] for c in r["checks"] if not c["ok"]]
        print(f"[{i}/{n}] {status}  score={r['judge_score']}  {r['id']:<30} {r['elapsed_s']:>5}s  "
              f"${r['agent_cost_usd'] + r['judge_cost_usd']:.4f}" + (f"   failed checks: {failed}" if failed else ""))
        print(f"        judge: {r['judge_rationale']}")

    ids = [s.strip() for s in args.only.split(",")] if args.only else None
    report = evaluation.run_evaluation(ids, progress=progress)
    s = report["summary"]
    print(f"\nPassed {s['passed']}/{s['cases']}   average score {s['average_score']:.2f}/5   "
          f"agent cost ${s['agent_cost_usd']:.4f}   judge cost ${s['judge_cost_usd']:.4f}")
    print("Results saved to eval/results.json")
    return 0 if s["passed"] == s["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
