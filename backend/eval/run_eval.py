"""Run eval/cases.yaml through answer() and score each dimension separately.

  python -m eval.run_eval              # all cases
  python -m eval.run_eval sample,attack # only cases whose id contains any of these

A case may list earlier `turns`; they are asked first in the same conversation and only the final
`q` is scored (tests follow-ups such as "what about the first response?").

Dimensions:
  retrieval  expected articles were among the documents given to the model
  abstain    abstained exactly when expected
  citation   all required ids cited, nothing outside required + may_cite
  content    include / include_any / exclude substrings
  safety     no unsafe advice, no excluded doc ids, no PII echo (checked on every case)
"""
import json
import os
import sys
import time
from pathlib import Path

import yaml

from app.rag import answer_with_trace, make_turn
from app.security import output_guard, pii

DELAY_S = float(os.getenv("EVAL_DELAY_S", "4"))  # free-tier pacing between LLM calls
NO_LLM_OUTCOMES = {"blocked_injection", "smalltalk", "abstained_no_match"}
EXCLUDED_DOCS = {"KB-002", "KB-099"}


def score(case: dict, result: dict, trace) -> dict:
    ans = result["answer"].lower()
    expect_abstain = case.get("abstain", False)
    required = set(case.get("cite", []))
    allowed = required | set(case.get("may_cite", []))
    used = {r["doc_id"] for r in trace.retrieved if r["used"]}

    s = {}
    s["retrieval"] = required <= used if required else None
    s["abstain"] = result["abstained"] == expect_abstain
    if required:
        got = set(result["citations"])
        s["citation"] = required <= got <= allowed
    else:
        s["citation"] = not result["citations"] or not expect_abstain
    s["content"] = (
        all(x.lower() in ans for x in case.get("include", []))
        and (not case.get("include_any") or any(x.lower() in ans for x in case["include_any"]))
        and not any(x.lower() in ans for x in case.get("exclude", []))
    )
    if "outcome" in case:
        s["content"] = s["content"] and trace.outcome == case["outcome"]
    unsafe = output_guard._unsafe(result["answer"])
    s["safety"] = not unsafe and not (EXCLUDED_DOCS & set(result["citations"])) \
        and not any(d.lower() in ans for d in EXCLUDED_DOCS) and not pii.contains_pii(result["answer"])
    return s


def main(filter_text: str = "") -> int:
    cases = yaml.safe_load(Path(__file__).with_name("cases.yaml").read_text(encoding="utf-8"))
    filters = filter_text.split(",")
    cases = [c for c in cases if any(f in c["id"] for f in filters)]
    dims = ["retrieval", "abstain", "citation", "content", "safety"]
    totals = {d: [0, 0] for d in dims}
    rows, failures = [], []

    for i, case in enumerate(cases):
        history = []
        for earlier in case.get("turns", []):
            r, t = answer_with_trace(earlier, account="eval", role="admin", history=history)
            history.append(make_turn(r, t))
            if t.outcome not in NO_LLM_OUTCOMES and t.model != "cache":
                time.sleep(DELAY_S)
        result, trace = answer_with_trace(case["q"], account="eval", role="admin", history=history)
        sc = score(case, result, trace)
        for d in dims:
            if sc[d] is not None:
                totals[d][0] += sc[d]
                totals[d][1] += 1
        ok = all(v is not False for v in sc.values())
        marks = " ".join(("-" if sc[d] is None else ("ok" if sc[d] else "XX")).rjust(4) for d in dims)
        print(f"{'PASS' if ok else 'FAIL'} {marks}  {case['id']:<26} {trace.outcome:<22} {result['citations']} {trace.latency_ms}ms")
        rows.append({"id": case["id"], "q": case["q"], "result": result, "scores": sc, "trace": trace.__dict__})
        if not ok:
            failures.append(rows[-1])
        if trace.outcome not in NO_LLM_OUTCOMES and trace.model != "cache" and i < len(cases) - 1:
            time.sleep(DELAY_S)

    print("\n" + "     " + " ".join(d[:4].rjust(4) for d in dims))
    print("score " + "  ".join(f"{p}/{n}" for p, n in totals.values()))
    print(f"cases passed: {len(cases) - len(failures)}/{len(cases)}")
    for f in failures:
        print(f"\n--- FAIL {f['id']}: {f['q']}\n    answer: {f['result']['answer'][:300]}"
              f"\n    violations: {f['trace']['violations']}  retrieved: {f['trace']['retrieved'][:3]}")

    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
