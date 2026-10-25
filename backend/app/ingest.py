"""Load KB markdown files, apply the admission policy, embed and upsert into pgvector.

Admission policy (an allowlist, so anything unexpected stays out):
  * frontmatter has doc_id, title, status, effective_date, owner and doc_id matches the filename
  * status == "current"           -> drops KB-002 (deprecated) and KB-099 (unverified)
  * effective_date <= today       -> future-dated articles don't go live early
  * owner is a real team           -> unowned / user-contributed content stays out
  * injection scan is clean        -> anything addressed to "AI assistants" is quarantined

Chunking: one chunk per article. All articles are < 1.5 KB, so this keeps the KB-003 / KB-007
tables intact and makes every citation map to exactly one article.

After every build the relevance threshold is re-calibrated for the current embedding model
(app/calibration.py) and stored in index_meta next to the model name.

Run:  python -m app.ingest [--reset]
"""
import argparse
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from app import calibration, db, retrieval
from app.config import settings as s
from app.llm import embed
from app.security.injection import scan
from app.security.text import normalize

log = logging.getLogger(__name__)

REQUIRED = ("doc_id", "title", "status", "effective_date", "owner")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)


@dataclass
class Article:
    doc_id: str
    title: str
    status: str
    effective_date: date
    owner: str
    body: str
    path: str
    superseded_by: str | None = None

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n\n{self.body}"

    @property
    def content_hash(self) -> str:
        key = f"{self.title}|{self.status}|{self.effective_date}|{self.body}"
        return hashlib.sha256(key.encode()).hexdigest()


def parse(path: Path) -> Article:
    text = normalize(path.read_text(encoding="utf-8"))
    m = _FRONTMATTER.match(text.replace("\r\n", "\n"))
    if not m:
        raise ValueError("missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        raise ValueError(f"frontmatter missing {missing}")
    if not str(meta["doc_id"]).startswith(path.stem.split("_")[0]):
        raise ValueError(f"doc_id {meta['doc_id']} does not match filename")
    eff = meta["effective_date"]
    return Article(
        doc_id=str(meta["doc_id"]),
        title=str(meta["title"]),
        status=str(meta["status"]).lower(),
        effective_date=eff if isinstance(eff, date) else date.fromisoformat(str(eff)),
        owner=str(meta["owner"]),
        body=m.group(2).strip(),
        path=path.name,
        superseded_by=meta.get("superseded_by"),
    )


def admission_reasons(a: Article, today: date) -> list[str]:
    """Empty list = admitted. Otherwise, every reason the article was refused."""
    reasons = []
    if a.status != "current":
        reasons.append(f"status={a.status}" + (f" (superseded by {a.superseded_by})" if a.superseded_by else ""))
    if a.effective_date > today:
        reasons.append(f"effective_date {a.effective_date} is in the future")
    if a.owner.strip().lower() in ("none", "null", "unknown", ""):
        reasons.append("no owning team")
    if hits := scan(a.embed_text):
        reasons.append(f"prompt-injection patterns: {hits}")
    return reasons


def run(reset: bool = False) -> dict:
    if reset:
        db.drop_all()
    db.init_schema()
    today = date.today()

    admitted: list[Article] = []
    rejected: list[dict] = []
    for path in sorted(s.kb_dir.glob("*.md")):
        try:
            art = parse(path)
        except Exception as e:  # malformed file: report it, never index it
            rejected.append({"file": path.name, "reasons": [f"parse error: {e}"]})
            continue
        if reasons := admission_reasons(art, today):
            rejected.append({"file": path.name, "doc_id": art.doc_id, "reasons": reasons})
        else:
            admitted.append(art)

    with db.connect() as conn:
        meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
        if meta and (meta.get("embed_model"), meta.get("embed_dim")) != (s.embed_model, str(s.embed_dim)):
            raise RuntimeError("Index was built with a different embedding model; rerun with --reset")

        existing = dict(conn.execute("SELECT chunk_id, content_hash FROM kb_chunks").fetchall())
        changed = [a for a in admitted if existing.get(a.doc_id) != a.content_hash]
        vectors = embed([a.embed_text for a in changed]) if changed else []

        for a, vec in zip(changed, vectors):
            conn.execute(
                """INSERT INTO kb_chunks (chunk_id, doc_id, title, status, effective_date, owner,
                                          section, content, content_hash, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chunk_id) DO UPDATE SET
                     title = EXCLUDED.title, status = EXCLUDED.status,
                     effective_date = EXCLUDED.effective_date, owner = EXCLUDED.owner,
                     content = EXCLUDED.content, content_hash = EXCLUDED.content_hash,
                     embedding = EXCLUDED.embedding""",
                (a.doc_id, a.doc_id, a.title, a.status, a.effective_date, a.owner,
                 "full", a.body, a.content_hash, vec),
            )

        # Anything no longer admitted (retired, superseded, quarantined, deleted) leaves the index.
        keep = [a.doc_id for a in admitted]
        removed = [r[0] for r in conn.execute(
            "DELETE FROM kb_chunks WHERE NOT (doc_id = ANY(%s)) RETURNING doc_id", (keep,)).fetchall()]

        for k, v in {
            "provider": s.provider, "embed_model": s.embed_model, "embed_dim": str(s.embed_dim),
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }.items():
            conn.execute("INSERT INTO index_meta VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (k, v))
        conn.commit()

    calib = recalibrate()
    report = {
        "indexed": [{"doc_id": a.doc_id, "title": a.title, "effective_date": str(a.effective_date)} for a in admitted],
        "embedded_this_run": [a.doc_id for a in changed],
        "removed_from_index": removed,
        "rejected": rejected,
        "embed_model": f"{s.provider}/{s.embed_model}@{s.embed_dim}",
        "calibration": calib,
        "gate_in_use": dict(zip(("sim_threshold", "grey_band"), retrieval.gate_params())),
    }
    s.audit_log.parent.mkdir(parents=True, exist_ok=True)
    (s.audit_log.parent / "ingest_report.json").write_text(json.dumps(report, indent=2))
    return report


def recalibrate() -> dict:
    """Measure the relevance threshold for the current index and store it with the index metadata."""
    try:
        report = calibration.calibrate()
    except Exception as e:  # e.g. embedding quota: keep the previous / fallback threshold
        log.warning("threshold calibration failed (%s); using %s", e,
                    "the .env value" if s.sim_threshold is not None else "the previous or fallback value")
        return {"error": type(e).__name__}
    db.set_meta(report)
    retrieval.reset_threshold_cache()
    log.info("calibrated gate: threshold %.3f, grey band %.2f (%s labelled; %d in-scope missed, %d off-topic "
             "reach the model; top-1 %s)", report["sim_threshold"], report["grey_band"], report["calib_n"],
             report["calib_missed"], report["calib_leaked"], report["calib_top1"])
    return report


def ensure_index() -> None:
    """Make sure the index was built with the configured embedding model. When LLM_API_KEY is
    switched to another provider, rebuild it automatically (AUTO_REINGEST=true): 7 articles, one call.
    Also re-calibrates the threshold when the labelled calibration set has changed."""
    db.init_schema()
    try:
        db.check_index_compatible()
        if db.get_meta().get("calib_fingerprint") != calibration.fingerprint():
            recalibrate()
    except db.IndexNotReady as e:
        if not s.auto_reingest:
            raise
        log.warning("%s -> rebuilding the index automatically", e)
        report = run(reset=True)
        log.warning("index rebuilt with %s: %d articles indexed, %d rejected",
                    report["embed_model"], len(report["indexed"]), len(report["rejected"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="drop and rebuild the index (needed after changing embedding model)")
    print(json.dumps(run(reset=ap.parse_args().reset), indent=2))
