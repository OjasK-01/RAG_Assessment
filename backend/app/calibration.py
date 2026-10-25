"""Automatic calibration of the relevance gate (cosine threshold + grey band).

The cosine score that separates "this article answers the question" from "nothing relevant" depends on
the embedding model (Gemini scores sit around 0.6, OpenAI's lower). Instead of a hand-set number per
provider, the gate is tuned on labelled questions every time the index is built, so a new LLM_API_KEY
gets measured settings, not a guess. Explicit <PROVIDER>_SIM_THRESHOLD / GREY_BAND in .env still win.

The search simulates the real gate (retrieval.apply_gate) for every (threshold, band) pair and
minimises: in-scope questions whose article is gated out + LEAK_WEIGHT x off-topic questions that
reach the model. Ties go to fewer leaks, then the smallest band, then the middle of the best range.

Cost: one batched embedding call for all questions (cached afterwards)."""
import hashlib
import json
import logging

log = logging.getLogger(__name__)

# question -> article that answers it. Includes paraphrases, typos and indirect phrasing on purpose.
IN_SCOPE = {
    "My account got locked. How long until it unlocks by itself?": "KB-001",
    "can't get into my account after typing the wrong pw too many times": "KB-001",
    "MFA code not arriving on my phone": "KB-001",
    "self service password reset is not working": "KB-001",
    "What is the resolution target for a P2 ticket?": "KB-003",
    "how fast will someone respond to a P1 ticket": "KB-003",
    "ticket flagged by leadership, what is the deadline": "KB-003",
    "Company mail doesn't show up in my iPhone's built-in Mail app. How do I fix it?": "KB-004",
    "outlook on my phone stopped syncing": "KB-004",
    "my mailbox is full and I can't send emails": "KB-004",
    "my scanner battery dies after 2 hours": "KB-005",
    "All label printers at dock 3 stopped and dispatch is blocked": "KB-005",
    "label printer says paper jam but there is no paper stuck": "KB-005",
    "I clicked a link in a weird email and typed my password": "KB-006",
    "my files are being renamed with strange extensions": "KB-006",
    "What VPN server address should I use when working from home?": "KB-007",
    "Does ZenTunnel work on Linux?": "KB-007",
    "vpn stuck on negotiating": "KB-007",
    "I need AutoCAD installed": "KB-008",
    "software update fails with access denied error": "KB-008",
}
OUT_OF_SCOPE = [
    "How do I get a parking pass for the Gurugram office?",
    "My laptop is slow",
    "How do I apply for annual leave?",
    "What is on the cafeteria menu today?",
    "How do I book a meeting room?",
    "Who won yesterday's cricket match?",
    "How do I claim travel reimbursement?",
    "Can I get a second monitor for my desk?",
    "When is the next payroll date?",
    "How do I change my bank details for salary?",
    "What is the office wifi password for guests?",
    "How do I order new business cards?",
    "My desk chair is broken",
    "What are the office timings on Saturday?",
    "Where can I find the holiday calendar?",
    "How do I connect to the printer on the 4th floor?",
]

# Cost of an off-topic question reaching the model, relative to an in-scope question being gated out.
# A leak still faces three more layers (model abstain rule, verified evidence quotes, output guard); a
# miss has no recovery: the employee is sent to a ticket for something the KB answers. Ties are broken
# towards fewer leaks.
LEAK_WEIGHT = 1


def fingerprint() -> str:
    return hashlib.sha256(json.dumps([IN_SCOPE, OUT_OF_SCOPE], sort_keys=True).encode()).hexdigest()[:12]


def measure() -> list[tuple[str, str | None, list]]:
    """[(question, expected doc_id or None for off-topic, scored candidates)]"""
    from app.llm import embed
    from app.retrieval import search

    embed(list(IN_SCOPE) + OUT_OF_SCOPE)  # one batched call; search() then hits the cache
    return [(q, exp, search(q)) for q, exp in [*IN_SCOPE.items(), *((q, None) for q in OUT_OF_SCOPE)]]


def errors(data: list, t: float, band: float) -> tuple[list[str], list[str]]:
    """(in-scope questions wrongly gated out, off-topic questions wrongly let through) for one setting."""
    from app.retrieval import apply_gate

    missed, leaked = [], []
    for q, expected, hits in data:
        apply_gate(hits, t, band)
        if expected and not any(h.doc_id == expected and h.relevant for h in hits):
            missed.append(q)
        if not expected and any(h.relevant for h in hits):
            leaked.append(q)
    return missed, leaked


def choose(data: list) -> tuple[float, float]:
    sims = [h.vector_sim for _, _, hits in data for h in hits]
    grid = [round(x / 1000, 3) for x in range(int(min(sims) * 1000), int(max(sims) * 1000) + 1, 5)]
    best = None
    for band in (0.0, 0.02, 0.04, 0.06, 0.08, 0.10):
        scores = []
        for t in grid:
            missed, leaked = errors(data, t, band)
            scores.append((len(missed) + LEAK_WEIGHT * len(leaked), len(leaked)))
        low = min(scores)
        if best is None or low < best[0]:
            # middle of the longest run of thresholds that reach the best score
            runs, run = [], []
            for t, sc in zip(grid, scores):
                if sc == low:
                    run.append(t)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            longest = max(runs, key=len)
            best = (low, longest[len(longest) // 2], band)
    return best[1], best[2]


def calibrate() -> dict:
    data = measure()
    t, band = choose(data)
    missed, leaked = errors(data, t, band)
    top1 = sum(1 for _, exp, hits in data if exp and max(hits, key=lambda h: h.vector_sim).doc_id == exp)
    in_sims = [next(h.vector_sim for h in hits if h.doc_id == exp) for _, exp, hits in data if exp]
    out_sims = [max(h.vector_sim for h in hits) for _, exp, hits in data if not exp]
    report = {
        "sim_threshold": t,
        "grey_band": band,
        "calib_missed": len(missed),
        "calib_leaked": len(leaked),
        "calib_cosine_overlap": round(max(out_sims) - min(in_sims), 3),
        "calib_top1": f"{top1}/{len(IN_SCOPE)}",
        "calib_n": f"{len(IN_SCOPE)}+{len(OUT_OF_SCOPE)}",
        "calib_fingerprint": fingerprint(),
    }
    log.info("calibration: threshold %.3f band %.2f -> missed %s, reaches model %s", t, band, missed, leaked)
    return report
