"""Provider-agnostic LLM access. Both OpenAI and Gemini are reached through the OpenAI SDK
(Gemini via its OpenAI-compatible endpoint), so swapping LLM_API_KEY is the only change needed."""
import hashlib
import json
import logging

import openai
from openai import OpenAI

from app import kvcache
from app.config import BACKEND_DIR, settings as s

log = logging.getLogger(__name__)

_client = OpenAI(
    api_key=s.llm_api_key,
    base_url=s.base_url,
    timeout=s.llm_timeout_s,
    max_retries=s.llm_max_retries,  # SDK retries 429/5xx with exponential backoff
)

# --- Embeddings, cached in Postgres (shared by all workers; saves quota on repeated questions) ---

_EMB_NS = f"emb:{s.provider}:{s.embed_model}:{s.embed_dim}"
_LLM_NS = f"llm:{s.provider}"
_migrated = False


def _migrate_file_caches() -> None:
    """Earlier versions cached in JSON files per process; move those entries into the shared cache once."""
    global _migrated
    if not _migrated:
        _migrated = True
        cache_dir = BACKEND_DIR / ".cache"
        kvcache.import_json_file(_EMB_NS, cache_dir / f"emb_{s.provider}_{s.embed_model}_{s.embed_dim}.json")
        kvcache.import_json_file(_LLM_NS, cache_dir / f"llm_{s.provider}.json")


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def embed(texts: list[str]) -> list[list[float]]:
    _migrate_file_caches()
    keys = {t: _key(t) for t in texts}
    cached = kvcache.get_many(_EMB_NS, list(set(keys.values())))
    missing = [t for t in dict.fromkeys(texts) if keys[t] not in cached]
    if missing:
        resp = _client.embeddings.create(model=s.embed_model, input=missing, dimensions=s.embed_dim)
        fresh = {keys[text]: item.embedding for text, item in zip(missing, resp.data)}
        kvcache.put_many(_EMB_NS, fresh)
        cached.update(fresh)
    return [cached[keys[t]] for t in texts]


# --- Chat with structured (JSON) output, model fallback and optional response cache ---

def _token_limit(n: int) -> dict:
    # OpenAI deprecated max_tokens in favour of max_completion_tokens; Gemini's compat layer uses max_tokens.
    return {"max_completion_tokens": n} if s.provider == "openai" else {"max_tokens": n}


# Errors that mean "this model can't serve us right now, try the next one".
_FALLBACK_ERRORS = (openai.RateLimitError, openai.NotFoundError, openai.InternalServerError,
                    openai.APITimeoutError, openai.APIConnectionError)
_no_json_schema: set[str] = set()  # models that rejected strict json_schema mode

last_model_used: str = ""


def _create(model: str, schema: dict, kwargs: dict, retries: int):
    client = _client.with_options(max_retries=retries)
    if model not in _no_json_schema:
        fmt = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema, "strict": True}}
        try:
            return client.chat.completions.create(model=model, response_format=fmt, **kwargs)
        except openai.BadRequestError as e:
            log.warning("%s rejected json_schema mode (%s); using json_object", model, str(e)[:120])
            _no_json_schema.add(model)
    return client.chat.completions.create(model=model, response_format={"type": "json_object"}, **kwargs)


def chat_json(system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict:
    """Return the model's reply parsed as JSON. Strict JSON-schema mode where supported, plain JSON
    mode otherwise (the backend validates the shape either way). Falls through CHAT_FALLBACKS when a
    model is rate-limited or unavailable."""
    global last_model_used
    cache_key = _key(json.dumps([s.chat_model, system, user, schema], sort_keys=True))
    if s.llm_cache:
        _migrate_file_caches()
        if (hit := kvcache.get(_LLM_NS, cache_key)) is not None:
            last_model_used = "cache"
            return hit

    kwargs = dict(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=s.temperature,
        **_token_limit(max_tokens or s.max_output_tokens),
    )
    models = [s.chat_model, *s.chat_fallbacks]
    for i, model in enumerate(models):
        # Fail over immediately on non-final models; only the last one uses the configured retries.
        retries = 0 if i < len(models) - 1 else s.llm_max_retries
        try:
            resp = _create(model, schema, kwargs, retries)
            break
        except _FALLBACK_ERRORS as e:
            log.warning("model %s unavailable (%s)%s", model, type(e).__name__,
                        "; trying next" if i < len(models) - 1 else "")
            if i == len(models) - 1:
                raise
    last_model_used = model

    content = (resp.choices[0].message.content or "").strip()
    # Some providers wrap JSON in a markdown fence even in JSON mode.
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    data = json.loads(content)

    if s.llm_cache:
        kvcache.put(_LLM_NS, cache_key, data)
    return data


def chat_text(system: str, user: str, max_tokens: int = 60) -> str:
    for i, model in enumerate(models := [s.chat_model, *s.chat_fallbacks]):
        try:
            resp = _client.with_options(max_retries=0).chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
                **_token_limit(max_tokens),
            )
            return (resp.choices[0].message.content or "").strip()
        except _FALLBACK_ERRORS:
            if i == len(models) - 1:
                raise
    return ""
