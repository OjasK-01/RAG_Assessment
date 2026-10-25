"""Show how the relevance gate is calibrated for the current embedding model.

The same calibration runs automatically on every index build (app/calibration.py); this script only
prints the details: per-question scores, the chosen threshold and band, and what each setting misses.

  python -m eval.calibrate
"""
from app import calibration
from app.retrieval import KW_MIN

data = calibration.measure()
t, band = calibration.choose(data)
missed, leaked = calibration.errors(data, t, band)

print("IN-SCOPE   expected-doc cosine | keyword | top-1?")
for q, exp, hits in data:
    if exp:
        h = next(h for h in hits if h.doc_id == exp)
        top = max(hits, key=lambda x: x.vector_sim).doc_id == exp
        print(f"  {h.vector_sim:.3f} | {h.keyword_rank:.2f} | {'yes' if top else 'NO '}  {exp}  {q[:60]}")
print("OFF-TOPIC  best cosine | keyword of that doc")
for q, exp, hits in data:
    if not exp:
        h = max(hits, key=lambda x: x.vector_sim)
        print(f"  {h.vector_sim:.3f} | {h.keyword_rank:.2f}  {h.doc_id}  {q[:60]}")

print(f"\nchosen gate: threshold {t:.3f}, grey band {band:.2f} (needs keyword >= {KW_MIN})")
print(f"in-scope gated out ({len(missed)}): {missed}")
print(f"off-topic reaching the model ({len(leaked)}): {leaked}  <- the model must abstain on these")
