"""Hybrid retrieval: pgvector cosine similarity + Postgres full-text, fused with Reciprocal Rank Fusion.

Dense vectors handle paraphrase ("can't log in" ~ "account lockout"); full-text catches exact
tokens that embeddings blur, like error codes "ZT-401" or "0x80070005". Only status='current'
rows are ever searched, even though ingestion already excludes everything else.

Relevance gate, per candidate article:
  sim >= threshold                          -> relevant
  question has a code (ZT-401) found in it  -> relevant
  threshold - band <= sim < threshold,
    it is the top vector hit AND has keyword support (ts_rank >= KW_MIN, i.e. 2+ term hits)
                                            -> relevant ("grey": borderline, the model decides and can
                                               still abstain)
Cosine alone can't separate in-scope from off-topic questions (on 36 labelled questions the score
ranges overlap); keyword support is the second signal. Threshold and band are auto-calibrated when
the index is built (app/calibration.py) unless set in .env."""
import re
from dataclasses import dataclass

from app import db
from app.config import settings as s
from app.llm import embed

RRF_K = 60
CANDIDATES = 8
KW_MIN = 0.2  # ts_rank_cd of two matching terms
# Identifier-like tokens (contain a digit): ZT-401, 0x80070005, P1, VX12345
_CODE_TOKEN = re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*\b", re.I)


@dataclass
class Hit:
    doc_id: str
    title: str
    content: str
    effective_date: str
    vector_sim: float = 0.0
    keyword_rank: float = 0.0
    exact_code_match: bool = False
    rrf: float = 0.0
    gate: str = ""  # why it passed: "sim" | "code" | "grey" | "context"; "" = gated out

    @property
    def relevant(self) -> bool:
        return bool(self.gate)


_gate_cache: tuple[float, float] | None = None


def gate_params() -> tuple[float, float]:
    """(threshold, grey band): .env values if set, else the values calibrated at index build, else fallbacks."""
    global _gate_cache
    if _gate_cache is None:
        try:
            meta = db.get_meta()
        except Exception:
            meta = {}
        _gate_cache = (float(meta.get("sim_threshold", s.sim_threshold_fallback)),
                       float(meta.get("grey_band", 0.04)))
    t, band = _gate_cache
    return (s.sim_threshold if s.sim_threshold is not None else t,
            s.grey_band if s.grey_band is not None else band)


def threshold() -> float:
    return gate_params()[0]


def reset_threshold_cache() -> None:
    global _gate_cache
    _gate_cache = None


def apply_gate(hits: list[Hit], t: float, band: float) -> None:
    top_vec = max(hits, key=lambda h: h.vector_sim, default=None)
    for h in hits:
        h.gate = ""
        if h.vector_sim >= t:
            h.gate = "sim"
        elif h.exact_code_match:
            h.gate = "code"
        elif h is top_vec and h.keyword_rank >= KW_MIN and h.vector_sim >= t - band:
            h.gate = "grey"


def _or_tsquery(conn, text: str) -> str | None:
    """Turn free text into an OR tsquery ('vpn' | 'gateway' | ...) so one matching term is enough."""
    row = conn.execute("SELECT tsvector_to_array(to_tsvector('english', %s))", (text,)).fetchone()
    lexemes = [lx for lx in (row[0] or []) if "'" not in lx and "\\" not in lx]
    return " | ".join(f"'{lx}'" for lx in lexemes) or None


def search(query: str) -> list[Hit]:
    """All candidates with scores, best first (no relevance cut applied)."""
    qvec = embed([query])[0]
    hits: dict[str, Hit] = {}
    with db.connect() as conn:
        vec_rows = conn.execute(
            """SELECT doc_id, title, content, effective_date::text, 1 - (embedding <=> %s::vector) AS sim
               FROM kb_chunks WHERE status = 'current'
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (qvec, qvec, CANDIDATES),
        ).fetchall()
        for rank, (doc_id, title, content, eff, sim) in enumerate(vec_rows, 1):
            hits[doc_id] = Hit(doc_id, title, content, eff, vector_sim=float(sim), rrf=1 / (RRF_K + rank))

        if tsq := _or_tsquery(conn, query):
            kw_rows = conn.execute(
                """SELECT doc_id, title, content, effective_date::text, ts_rank_cd(tsv, q) AS r
                   FROM kb_chunks, to_tsquery('english', %s) q
                   WHERE status = 'current' AND tsv @@ q
                   ORDER BY r DESC LIMIT %s""",
                (tsq, CANDIDATES),
            ).fetchall()
            for rank, (doc_id, title, content, eff, r) in enumerate(kw_rows, 1):
                h = hits.setdefault(doc_id, Hit(doc_id, title, content, eff))
                h.keyword_rank = float(r)
                h.rrf += 1 / (RRF_K + rank)

    codes = {c.lower() for c in _CODE_TOKEN.findall(query) if len(c) >= 4}
    for h in hits.values():
        text = (h.title + " " + h.content).lower()
        h.exact_code_match = any(c in text for c in codes)

    ranked = sorted(hits.values(), key=lambda h: h.rrf, reverse=True)
    apply_gate(ranked, *gate_params())
    return ranked


def retrieve(query: str, k: int | None = None, carry_over: list[str] | None = None) -> tuple[list[Hit], list[Hit]]:
    """Returns (relevant top-k hits to ground the answer on, all scored candidates for diagnostics).

    carry_over: articles cited in the previous turn of a follow-up question ("what about the first
    response?"). They stay available to the model even if the short follow-up alone scores low; the
    model still has to find the answer in them, and abstains otherwise."""
    candidates = search(query)
    for h in candidates:
        if not h.gate and carry_over and h.doc_id in carry_over:
            h.gate = "context"
    relevant = [h for h in candidates if h.relevant][: k or s.top_k]
    return relevant, candidates
