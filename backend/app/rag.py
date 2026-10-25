"""answer(question) -> {"answer", "citations", "abstained"}

Pipeline: validate -> mask PII -> injection guard -> intent (+ follow-up?) -> hybrid retrieval
(relevance gate) -> LLM with strict JSON schema -> validate reply (schema, citations, evidence quotes,
output guard) -> deterministic escalation -> audit log.

Conversation context: the caller passes earlier turns (app/conversation.py loads them server-side).
A follow-up ("what about the first response?") is searched together with the previous question, the
previous turn's articles stay available, and the model sees a compact history. History is never a
source of facts: answers are still verified against the cited articles only.
Every failure path ends in a safe abstain; nothing unvalidated reaches the employee."""
import logging
import re
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app import audit, db, ingest, llm
from app.config import settings as s
from app.conversation import CONTEXT_OUTCOMES, Turn
from app.intent import Intent, classify, is_followup
from app.prompts import REWRITE_PROMPT, SYSTEM_PROMPT, build_user_message, reply_schema
from app.retrieval import Hit, retrieve
from app.security import injection, output_guard, pii
from app.security.text import normalize

log = logging.getLogger(__name__)

ABSTAIN_MSG = ("I couldn't find this in the Vectra IT knowledge base, so I won't guess. "
               "Please raise a ticket with the IT Service Desk and an agent will help you.")
REFUSAL_MSG = ("I can only help with IT support questions answered in the Vectra knowledge base. "
               "Please rephrase your question, or raise a ticket with the IT Service Desk.")
ERROR_MSG = ("I couldn't process your question right now. Please try again in a moment, "
             "or raise a ticket with the IT Service Desk.")
GREETING_MSG = ("Hi! I'm the Vectra IT help assistant. Ask me an IT question, for example about "
                "password resets, VPN, email on your phone, or ticket priorities.")
THANKS_MSG = "You're welcome! Ask me anything else about Vectra IT, or raise a ticket if you need an agent."

# Escalations required by the KB itself, added by code so they never depend on the model.
# (doc_id, fact that must still be in that article, text)
ESCALATIONS = {
    "security": ("KB-006", "7333",
                 "If this may be a security incident: disconnect from the network (turn off wifi or unplug "
                 "the cable) but do not shut the laptop down, then call SecOps immediately on ext. 7333 "
                 "and raise a Security ticket."),
    "p1": ("KB-003", "4357",
           "This sounds like a P1 (business stopped). Raise the ticket and also phone the 24x7 service "
           "desk on ext. 4357."),
}


class InvalidQuestion(ValueError):
    pass


class ModelReply(BaseModel):
    answer: str
    citations: list[str]
    evidence: list[dict]
    abstained: bool


@dataclass
class Trace:
    """Everything about how an answer was produced. Agents/admins see it; the audit log stores it."""
    question_masked: str = ""
    pii_types: list = field(default_factory=list)
    intent: str = ""
    flags: list = field(default_factory=list)
    followup: bool = False
    context_turns: int = 0
    search_query: str = ""
    retrieved: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    outcome: str = ""  # answered | abstained_no_match | abstained_model | blocked_* | smalltalk | error
    model: str = ""  # which chat model answered ("cache" if served from the response cache)
    latency_ms: int = 0


def _result(answer: str, citations: list[str], abstained: bool) -> dict:
    return {"answer": answer, "citations": citations, "abstained": abstained}


# --- helpers -----------------------------------------------------------------

_doc_cache: tuple[float, dict[str, str]] = (0.0, {})


def _indexed_docs() -> dict[str, str]:
    """doc_id -> content of every current indexed article (cached briefly)."""
    global _doc_cache
    if time.monotonic() - _doc_cache[0] > 60:
        with db.connect() as conn:
            rows = conn.execute("SELECT doc_id, content FROM kb_chunks WHERE status = 'current'").fetchall()
        _doc_cache = (time.monotonic(), dict(rows))
    return _doc_cache[1]


_index_checked = False


def _ensure_index_once() -> None:
    """Never search an index built by a different embedding model (same dimension = silent garbage)."""
    global _index_checked
    if not _index_checked:
        ingest.ensure_index()
        _index_checked = True
        refresh_doc_cache()


def refresh_doc_cache() -> None:
    global _doc_cache
    _doc_cache = (0.0, {})


def _norm(text: str) -> str:
    text = re.sub(r"[*_`|>#]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _quote_supported(quote: str, source: str) -> bool:
    """Exact (normalised) substring, or >= 85% of the quote's words present in the source."""
    q, src = _norm(quote), _norm(source)
    if not q:
        return False
    if q in src:
        return True
    words = re.findall(r"\w+", q)
    src_words = set(re.findall(r"\w+", src))
    return bool(words) and sum(w in src_words for w in words) / len(words) >= 0.85


def _escalate(answer: str, citations: list[str], intent: Intent, docs: dict[str, str], trace: Trace) -> tuple[str, list[str]]:
    for flag, active in (("security", intent.security_incident), ("p1", intent.p1)):
        if not active:
            continue
        doc_id, fact, text = ESCALATIONS[flag]
        if fact not in docs.get(doc_id, ""):  # KB changed: don't assert a stale escalation path
            log.warning("escalation %s skipped: %s no longer contains %s", flag, doc_id, fact)
            continue
        trace.flags.append(f"escalation:{flag}")
        if fact not in answer:
            answer = f"{answer}\n\n{text}".strip()
        if doc_id not in citations:
            citations = citations + [doc_id]
    return answer, citations


def _rewrite(masked: str) -> str:
    try:
        q = normalize(llm.chat_text(REWRITE_PROMPT, f"<message>{masked}</message>", max_tokens=40))
    except Exception as e:  # rewrite is an optimisation; fall back to the original text
        log.warning("query rewrite failed: %s", e)
        return masked
    return masked if not q or len(q) > 200 or injection.scan(q) else q


# --- main entry point --------------------------------------------------------

def answer(question: str, history: list[Turn] | None = None) -> dict:
    return answer_with_trace(question, history=history)[0]


def make_turn(result: dict, trace: Trace) -> Turn:
    """What gets remembered of this turn for the next one (masked question, guarded answer)."""
    return Turn(trace.question_masked, trace.search_query or trace.question_masked,
                result["answer"], tuple(result["citations"]), trace.outcome)


def answer_with_trace(question: str, account: str = "local", role: str = "staff",
                      history: list[Turn] | None = None, conversation_id: str | None = None) -> tuple[dict, Trace]:
    t0 = time.perf_counter()
    trace = Trace()
    try:
        result = _run(question, trace, history or [])
    except InvalidQuestion:
        raise
    except Exception:
        log.exception("answer pipeline failed")
        trace.outcome = "error"
        result = _result(ERROR_MSG, [], True)
    trace.latency_ms = int((time.perf_counter() - t0) * 1000)
    audit.write({
        "account": account, "role": role, "provider": s.provider, "conversation_id": conversation_id,
        **trace.__dict__, **result,  # answer is PII-scrubbed by the output guard
    })
    return result, trace


MAX_CONTEXT_QUERY_CHARS = 300


def _run(question: str, trace: Trace, history: list[Turn]) -> dict:
    # 1. Validate input
    if not isinstance(question, str):
        raise InvalidQuestion("question must be a string")
    text = normalize(question)
    if len(text) < 2:
        raise InvalidQuestion("question is empty")
    if len(text) > s.max_question_chars:
        raise InvalidQuestion(f"question is longer than {s.max_question_chars} characters")

    # 2. Mask PII: nothing below this line sees the raw text
    masked, trace.pii_types = pii.mask(text)
    trace.question_masked = masked

    # 3. Prompt-injection guard
    if hits := injection.scan(masked):
        trace.outcome, trace.violations = "blocked_injection", [f"injection:{h}" for h in hits]
        return _result(REFUSAL_MSG, [], True)

    # 4. Intent
    intent = classify(masked)
    trace.intent = intent.kind
    if intent.kind in ("greeting", "thanks"):
        trace.outcome = "smalltalk"
        return _result(GREETING_MSG if intent.kind == "greeting" else THANKS_MSG, [], False)

    _ensure_index_once()
    docs = _indexed_docs()

    # Conversation context: earlier answered/abstained turns (never refusals or small talk)
    context = [t for t in history if t.outcome in CONTEXT_OUTCOMES][-s.history_turns:]
    trace.context_turns = len(context)
    trace.followup = bool(context) and is_followup(masked)

    # 5. Retrieve, with relevance gate. A follow-up is searched together with the previous question,
    #    and the previous turn's articles stay available ("what about the first response?").
    query = _rewrite(masked) if s.query_rewrite else masked
    carry_over = None
    if trace.followup:
        prev = context[-1]
        room = max(0, MAX_CONTEXT_QUERY_CHARS - len(query))
        query = f"{prev.resolved_query[-room:] if room else ''} {query}".strip()
        carry_over = list(prev.citations)
    trace.search_query = query
    relevant, candidates = retrieve(query, carry_over=carry_over)
    trace.retrieved = [
        {"doc_id": h.doc_id, "sim": round(h.vector_sim, 3), "keyword": round(h.keyword_rank, 3),
         "code_match": h.exact_code_match, "gate": h.gate, "used": h in relevant}
        for h in candidates[:5]
    ]
    if not relevant:
        trace.outcome = "abstained_no_match"
        ans, cites = _escalate(ABSTAIN_MSG, [], intent, docs, trace)
        return _result(ans, cites, not cites)

    # 6. Generate
    allowed = [h.doc_id for h in relevant]
    raw = llm.chat_json(SYSTEM_PROMPT, build_user_message(masked, relevant, context), reply_schema(allowed))
    trace.model = llm.last_model_used

    # 7. Validate the reply
    try:
        reply = ModelReply.model_validate(raw)
    except ValidationError as e:
        trace.outcome, trace.violations = "error", [f"schema:{e.error_count()} errors"]
        return _result(ABSTAIN_MSG, [], True)

    citations = list(dict.fromkeys(c for c in reply.citations if c in allowed))
    if reply.abstained or not reply.answer.strip() or not citations:
        trace.outcome = "abstained_model"
        ans, cites = _escalate(ABSTAIN_MSG, [], intent, docs, trace)
        return _result(ans, cites, not cites)

    by_id: dict[str, Hit] = {h.doc_id: h for h in relevant}
    for ev in reply.evidence:
        doc_id, quote = str(ev.get("doc_id", "")), str(ev.get("quote", ""))
        ok = doc_id in by_id and _quote_supported(quote, by_id[doc_id].content)
        trace.evidence.append({"doc_id": doc_id, "quote": quote[:200], "verified": ok})
    # A citation without a verified quote is dropped (e.g. KB-006 refers to KB-001 and the model cites
    # both but only quotes KB-006). The answer survives only if it is grounded in what remains.
    unsupported = [c for c in citations if not any(e["verified"] and e["doc_id"] == c for e in trace.evidence)]
    citations = [c for c in citations if c not in unsupported]
    if unsupported and citations:
        trace.flags.append(f"dropped_unverified_citation:{unsupported}")
    elif unsupported:
        trace.violations.append(f"no_verified_evidence:{unsupported}")

    cited_text = "\n".join(f"{by_id[c].title}\n{by_id[c].content}" for c in citations)
    guard = output_guard.check(reply.answer, cited_text, set(docs), question=masked)
    trace.violations += guard.violations
    if guard.pii_scrubbed:
        trace.flags.append("pii_scrubbed_from_answer")
    if trace.violations:
        trace.outcome = "blocked_output_guard"
        ans, cites = _escalate(ABSTAIN_MSG, [], intent, docs, trace)
        return _result(ans, cites, not cites)

    # 8. Escalation required by the KB (P1 phone line, SecOps)
    ans, citations = _escalate(guard.answer, citations, intent, docs, trace)
    trace.outcome = "answered"
    return _result(ans, citations, False)
