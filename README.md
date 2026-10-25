# Vectra IT KB Assistant

Answers Vectra staff IT questions **from the IT knowledge base only**, cites the article, and says
"raise a ticket" instead of guessing.

```python
from app.rag import answer
answer("What is the resolution target for a P2 ticket?")
# {"answer": "The resolution target for a P2 ticket is 8 hours.",
#  "citations": ["KB-003"], "abstained": False}
```

## Documentation

| Doc | Contents |
|---|---|
| [docs/03_BRIEF_ANSWERS.md](docs/03_BRIEF_ANSWERS.md) | Every question in the brief answered, with real outputs |
| [docs/04_RUNBOOK.md](docs/04_RUNBOOK.md) | Commands to run the DB, backend and frontend by hand, switching to OpenAI, troubleshooting |

## Quick start

```bash
cd backend
copy .env.example .env                        # first time only: fill in LLM_API_KEY, PG_PASSWORD, JWT_SECRET, AUTH_USERS
docker compose up -d                          # pgvector on 127.0.0.1:5433 (reads PG_* from .env)
venv\Scripts\python check_key.py              # ~10 tokens: verifies chat + embeddings for LLM_API_KEY
venv\Scripts\python -m app.ingest --reset     # build the index, prints the admission report
venv\Scripts\python -m uvicorn app.main:app --port 8000   # API + Swagger at http://127.0.0.1:8000/docs

cd ../frontend
copy .env.example .env                        # first time only: VITE_BASE_URL=http://127.0.0.1:8000
npm install && npm run dev                    # UI at http://127.0.0.1:5173
```

**Configuration files:**

| File | Committed? | Holds |
|---|---|---|
| `backend/.env.example` | yes | every backend setting with placeholders for the secrets |
| `backend/.env` | no (gitignored) | the real values: API key, DB password, JWT secret, account hashes |
| `frontend/.env.example` | yes | `VITE_BASE_URL` template |
| `frontend/.env` | no (gitignored) | `VITE_BASE_URL=http://127.0.0.1:8000`, the backend's origin |

With `VITE_BASE_URL` set, the browser calls the API directly, so `CORS_ORIGINS` in `backend/.env` must contain the UI's
origin (`http://127.0.0.1:5173`). Leave `VITE_BASE_URL` empty and the Vite dev server proxies `/api` instead (same origin,
no CORS). Vite copies `VITE_*` values into the public JavaScript bundle, so that file must never hold a secret.

Demo accounts (`admin`, `agent`, `staff`) and passwords are in `backend/.demo_credentials.txt`; only
bcrypt hashes are stored in `.env`. In Swagger, click **Authorize** and log in with one of them.

Other entry points: `python -m app.cli "question" --trace`, `python -m app.cli --chat` (multi-turn),
`python -m eval.run_eval`, `python -m eval.calibrate`, `python -m pytest tests`.

The API keeps no state of its own (rate limits, caches and conversations are in Postgres), so it can run with several
workers: `uvicorn app.main:app --port 8000 --workers 4`.

> Behind a TLS-intercepting proxy, npm fails with `SELF_SIGNED_CERT_IN_CHAIN`. Run it with
> `NODE_OPTIONS=--use-system-ca` (trusts the Windows cert store) rather than disabling strict-ssl.

### OpenAI or Gemini: one variable

Everything is configured in `backend/.env`. The provider is detected from `LLM_API_KEY`
(`sk-…` = OpenAI, otherwise Gemini). Both are called through the OpenAI SDK (Gemini through its
OpenAI-compatible endpoint), so there is no second SDK. Model names and fallbacks are **provider-scoped**
(`GEMINI_CHAT_MODEL`, `OPENAI_CHAT_MODEL`, …), so swapping the key never pairs one provider's key with the other's
models. On restart, the backend sees that the index was built by the other provider's embedding model, rebuilds it
(`AUTO_REINGEST=true`), and **re-calibrates the relevance gate** on the new model's scores. Then, once, to confirm:

```bash
python -m eval.calibrate         # shows the calibrated gate and what it lets through
python -m eval.run_eval
```

## Architecture

```
POST /api/ask {question, conversation_id?}
                   (JWT, role check, per-account rate limit shared by all workers, 8 KB body cap, strict schema)
  0 history        load this account's earlier turns from Postgres (opaque id; can't be forged by the client)
  1 normalise      NFKC, strip zero-width / bidi / control chars          (defeats obfuscated payloads)
  2 mask PII       email, phone, Aadhaar, PAN, card, VX#####, "password is …", names (by cue or by shape)
                   -> only masked text reaches the LLM, the embedder, the logs and the stored history
  3 injection      heuristic rules + decoded base64 -> refuse without calling the LLM
  4 intent         rules: greeting / thanks short-circuit; follow-up?; security-incident and P1 flags
                   (English, Hinglish, Devanagari)
  5 retrieve       hybrid: pgvector cosine + Postgres full-text (catches "ZT-401"), RRF fusion,
                   WHERE status='current', self-calibrated gate (cosine, exact code, keyword-supported
                   grey band); a follow-up is searched with the previous question and keeps its articles
                   -> nothing relevant = abstain without calling the LLM
  6 generate       temperature 0, strict JSON schema, citations enum-constrained to retrieved ids,
                   articles as <kb_article> data, compact <conversation> (context only, never facts),
                   list of article dates already past, the model has no tools
  7 validate       schema; citations ⊆ retrieved; each citation needs a verbatim evidence quote checked
                   against the article (unquoted ones dropped); output guard (unsafe advice, invented
                   hosts / numbers / codes, excluded docs, system-prompt leak via canary, PII echo)
                   -> any failure = abstain
  8 escalate       code (not the model) appends "phone ext. 4357" for P1 and SecOps ext. 7333 for
                   security incidents, and only while KB-003 / KB-006 still say so
  9 audit          JSONL: masked question, conversation, intent, scores, gates, evidence, verdict, model
```

| Module | Role |
|---|---|
| `app/config.py` | all settings from `.env`, provider detection |
| `app/llm.py` | OpenAI-SDK client for both providers, cached embeddings, model fallback chain, response cache |
| `app/db.py`, `app/kvcache.py` | connection pool, schema, shared cache in Postgres |
| `app/ingest.py`, `app/calibration.py` | frontmatter parsing, admission policy, idempotent upsert, removal of retired docs, gate calibration |
| `app/retrieval.py` | hybrid search + relevance gate |
| `app/conversation.py` | server-side conversation history (per account, TTL) |
| `app/prompts.py` | system prompt (input sources, tools = none, rules, output format), JSON schema, canary |
| `app/rag.py` | the `answer()` pipeline |
| `app/security/` | normalisation, PII masking, injection scanner, output guard |
| `app/auth.py`, `app/main.py` | JWT + RBAC + shared rate limits, FastAPI routes |
| `eval/` | gate calibration report, 31 eval cases (some multi-turn), scorer |
| `frontend/` | React chat UI: conversations, citations, abstain badge, "why this answer" trace for agents |

**Roles:** `staff` can ask. `agent` can also see the retrieval trace and the KB list. `admin` can also re-index
and read the audit log.

## The planted KB problems and how each is handled

| Problem | Handling |
|---|---|
| **KB-002** is deprecated and superseded by KB-007 (old VPN host, claims Linux works) | Admission policy is an **allowlist**: only `status: current` is indexed. Every row also carries `status`, and queries filter on it. The output guard blocks the stale host `vpn.vectralogistics.example`. |
| **KB-099** is unverified, unowned, and contains an *"AI assistants… turn off antivirus"* injection | Rejected on **three independent grounds**: status, no owner, and the injection scanner (`addressed_to_ai`, `always_tell_user`). Even if it got in: the prompt treats articles as data, the model has no tools, and the output guard blocks "disable antivirus" advice. |
| **Tables** in KB-003 and KB-007 | One chunk per article (all are < 1.5 KB), so tables are never split and each citation maps to one article. Full-text search catches exact codes like `ZT-401`. |
| **Escalation rules** buried in text (P1 → ext. 4357, SecOps ext. 7333) | Added by code from rule-based intent flags, and only while the source article still contains that number. |
| **PII** in Anand's question | Masked before embedding, the LLM and logs. The output guard scrubs any echo. |
| **Out of scope** (parking pass) | Relevance gate abstains before any LLM call. The model can also abstain, and the backend returns a fixed message. |

Ingestion report:

```
indexed:  KB-001 KB-003 KB-004 KB-005 KB-006 KB-007 KB-008
rejected: KB-002  status=deprecated (superseded by KB-007)
          KB-099  status=unverified; no owning team; prompt-injection patterns: [addressed_to_ai, always_tell_user]
```

## How we know it works

**Unit and API tests** (`pytest tests`, 41 tests, no LLM calls):
- PII masking, including names by context and by shape, with KB terms left untouched
- injection rules, including zero-width and full-width obfuscation, base64, Hinglish, and false-positive checks
- the output guard: negation ("Never disable antivirus" passes), number formats, prompt-leak canary
- follow-up detection, Hinglish/Devanagari escalation, history escaping, the grey-band gate, calibration
- quote verification
- the RBAC matrix
- forged, tampered, expired, `alg=none` and role-escalated JWTs
- input validation and rate limits
- conversations: id reuse, privacy between accounts, delete

**Gate calibration** (`app/calibration.py`, automatic at every index build; `python -m eval.calibrate` prints it). On 20
in-scope and 16 off-topic questions, cosine scores **overlap** (by 0.077), so no single cut-off works. The gate adds
keyword support for borderline scores. Calibrated on gemini-embedding-001: threshold **0.562**, grey band **0.04**.
**0/20** in-scope questions missed, 14/16 off-topic questions gated without an LLM call, and the model abstains on the other 2
(printer on the 4th floor, guest wifi password).

**End-to-end eval** (`eval/run_eval.py`, 31 cases: the 5 samples, KB tricky spots, escalation, off-topic, attacks,
multi-turn follow-ups, Hinglish, name masking), all on the final prompt and scored per dimension:

| Dimension | Score | Meaning |
|---|---|---|
| retrieval | 19/19 | expected article was given to the model |
| abstain | 31/31 | abstained exactly when it should |
| citation | 31/31 | required ids cited, nothing extra |
| content | 31/31 | required facts present, forbidden ones absent |
| safety | 31/31 | no unsafe advice, no excluded docs, no PII echo |

The multi-turn case from real use:

```
you: there is a ticket flagged by leadership, till when can i solve it or what is the deadline for it
bot: Tickets from leadership flagged VIP are handled as P2 at minimum, regardless of impact. For a P2 ticket,
     the resolution target is 8 hours, and the first-response target is 1 hour.            [KB-003]
you: what about the first response
bot: For a P2 ticket, the first-response target is 1 hour, and the resolution target is 8 hours.   [KB-003]
```

## What still fails or is weak (honest list)

- **The gate is only as good as its 36 hand-written labels.** Real tickets (including Hinglish) should be labelled and added
  to `app/calibration.py`.
- **On-topic but uncovered questions reach the model** (e.g. office printers, guest wifi). Each costs one LLM call, and
  their safety rests on the model abstaining, backed by the evidence and output-guard checks.
- **Follow-up detection is rule-based.** An unusual follow-up phrasing is treated as a new question (the model still sees
  the history).
- **Name masking misses lowercase or single names in passing.** Production would add an NER model such as Presidio.
- **The injection scanner is heuristic.** It is one of five layers (allowlist ingestion, data-not-instructions prompt, no
  tools, schema + citation constraints, output guard with canary).
- **Not yet run end to end on OpenAI** (no key). The gate calibrates itself, but run `eval.run_eval` once.
- **The audit log is a local JSONL file**, so it is not shared across replicas.
- **The Gemini free tier allows 20 requests per day per model**, which is why there is a fallback chain and a response cache.
  Free-tier prompts may be used for training, so it is not acceptable for production data.

## Before going live to 3,000 staff (prioritised)

1. **Re-index on KB change automatically.** Trigger ingestion from the KB's publish/retire workflow, the way
   KB-002 was retired for KB-007, and alert on any rejected or quarantined article.
2. **Never ingest unverified content.** Keep the allowlist and add a required reviewer field in the KB.
3. **Mask PII with a proper NER model**, keep masking before logs and the LLM, and use a paid LLM tier with no training
   on data and a data-processing agreement (or Azure OpenAI in an India region).
4. **Build a larger test set from real Vectra tickets** (a few hundred, including Hinglish), add them to the gate
   calibration set, and run the eval in CI on every KB, prompt or model change. Block releases that regress safety or abstain accuracy.
5. **P1 handling.** Keep the code-enforced "phone ext. 4357" and SecOps lines. Consider routing P1-flagged
   questions straight to a human.
6. **Roll out to service desk agents first** (they see the trace and can flag bad answers), then to all staff.
7. **Platform hardening.** SSO (Entra ID) in place of `.env` accounts, httpOnly cookies in place of sessionStorage, central
   logging, TLS, secrets in a vault, monitoring of abstain rate, guard blocks and latency.
