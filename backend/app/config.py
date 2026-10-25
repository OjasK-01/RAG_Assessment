"""All runtime settings come from .env. Provider is inferred from LLM_API_KEY unless set explicitly."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")

# Per-provider defaults. Override any of them in .env with a provider prefix, e.g. GEMINI_CHAT_MODEL,
# OPENAI_SIM_THRESHOLD, so switching LLM_API_KEY never pairs one provider's key with the other's models.
# sim_threshold is the minimum cosine similarity for an article to count as relevant; it depends on
# the embedding model. Leave <PROVIDER>_SIM_THRESHOLD unset and it is calibrated automatically when the
# index is built (app/calibration.py); the value below is only the fallback if calibration can't run.
PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "",
        "chat_model": "gpt-4.1-mini",
        "chat_fallbacks": "gpt-4.1-nano",
        "embed_model": "text-embedding-3-small",
        "sim_threshold": "0.30",  # fallback only; auto-calibrated at index build
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "chat_model": "gemini-2.5-flash-lite",
        "chat_fallbacks": "",
        "embed_model": "gemini-embedding-001",
        "sim_threshold": "0.57",  # fallback only; auto-calibrated at index build
    },
}

ROLES = ("admin", "agent", "staff")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "on")


def _detect_provider(key: str) -> str:
    explicit = _env("LLM_PROVIDER").lower()
    if explicit and explicit != "auto":
        if explicit not in PROVIDER_DEFAULTS:
            raise ValueError(f"LLM_PROVIDER must be auto or one of {list(PROVIDER_DEFAULTS)}")
        return explicit
    return "openai" if key.startswith("sk-") else "gemini"


def _parse_users(raw: str) -> dict[str, tuple[str, str]]:
    """AUTH_USERS='name|role|bcrypt_hash;...' -> {name: (role, hash)}"""
    users = {}
    for entry in filter(None, (e.strip() for e in raw.split(";"))):
        name, role, pw_hash = entry.split("|", 2)
        if role not in ROLES:
            raise ValueError(f"AUTH_USERS: unknown role {role!r} for {name!r}")
        users[name] = (role, pw_hash)
    return users


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_api_key: str
    provider: str
    base_url: str | None
    chat_model: str
    chat_fallbacks: list
    embed_model: str
    embed_dim: int
    temperature: float
    max_output_tokens: int
    llm_timeout_s: float
    llm_max_retries: int
    query_rewrite: bool
    llm_cache: bool
    # Retrieval
    kb_dir: Path
    top_k: int
    sim_threshold: float | None  # None = use the auto-calibrated value stored with the index
    sim_threshold_fallback: float
    grey_band: float | None  # None = calibrated
    auto_reingest: bool
    # Conversation memory
    history_turns: int
    conversation_ttl_min: int
    # Database
    database_url: str
    pg_pool_max: int
    # Auth / API
    jwt_secret: str
    jwt_ttl_min: int
    users: dict
    rate_limit_per_min: int
    login_rate_limit_per_min: int
    rate_limit_backend: str
    max_question_chars: int
    cors_origins: list
    enable_docs: bool
    audit_log: Path


def load_settings() -> Settings:
    key = _env("LLM_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY missing from .env")
    provider = _detect_provider(key)

    def p(name: str) -> str:  # provider-scoped setting: GEMINI_CHAT_MODEL, OPENAI_EMBED_MODEL, ...
        return _env(f"{provider.upper()}_{name.upper()}", PROVIDER_DEFAULTS[provider][name])

    jwt_secret = _env("JWT_SECRET")
    if len(jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be set in .env (>= 32 chars)")

    kb_dir = Path(_env("KB_DIR", "../vectra_kb_articles/kb"))
    audit_log = Path(_env("AUDIT_LOG", "logs/audit.jsonl"))

    return Settings(
        llm_api_key=key,
        provider=provider,
        base_url=p("base_url") or None,
        chat_model=p("chat_model"),
        chat_fallbacks=[m.strip() for m in p("chat_fallbacks").split(",") if m.strip()],
        embed_model=p("embed_model"),
        embed_dim=_int("EMBED_DIM", 768),
        temperature=_float("TEMPERATURE", 0.0),
        max_output_tokens=_int("MAX_OUTPUT_TOKENS", 500),
        llm_timeout_s=_float("LLM_TIMEOUT_S", 30),
        llm_max_retries=_int("LLM_MAX_RETRIES", 2),
        query_rewrite=_bool("QUERY_REWRITE", False),
        llm_cache=_bool("LLM_CACHE", False),
        kb_dir=kb_dir if kb_dir.is_absolute() else (BACKEND_DIR / kb_dir).resolve(),
        top_k=_int("TOP_K", 3),
        sim_threshold=float(v) if (v := _env(f"{provider.upper()}_SIM_THRESHOLD")) else None,
        sim_threshold_fallback=float(PROVIDER_DEFAULTS[provider]["sim_threshold"]),
        grey_band=float(v) if (v := _env("GREY_BAND")) else None,
        auto_reingest=_bool("AUTO_REINGEST", True),
        history_turns=_int("HISTORY_TURNS", 3),
        conversation_ttl_min=_int("CONVERSATION_TTL_MIN", 120),
        database_url=(
            f"postgresql://{_env('PG_USER')}:{_env('PG_PASSWORD')}@{_env('PG_HOST', '127.0.0.1')}"
            f":{_env('PG_PORT', '5432')}/{_env('PG_DB')}"
            "?connect_timeout=5"
        ),
        pg_pool_max=_int("PG_POOL_MAX", 10),
        jwt_secret=jwt_secret,
        jwt_ttl_min=_int("JWT_TTL_MIN", 60),
        users=_parse_users(_env("AUTH_USERS")),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 10),
        login_rate_limit_per_min=_int("LOGIN_RATE_LIMIT_PER_MIN", 5),
        rate_limit_backend=_env("RATE_LIMIT_BACKEND", "postgres").lower(),
        max_question_chars=_int("MAX_QUESTION_CHARS", 500),
        cors_origins=[o.strip() for o in _env("CORS_ORIGINS").split(",") if o.strip()],
        enable_docs=_bool("ENABLE_DOCS", False),
        audit_log=audit_log if audit_log.is_absolute() else BACKEND_DIR / audit_log,
    )


settings = load_settings()
