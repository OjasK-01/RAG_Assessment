"""Postgres + pgvector storage. One row per chunk; every row carries the article's metadata.

Postgres also holds the state that must be shared by every API worker: conversation turns,
the embedding / LLM response cache, and rate-limit counters. Connections come from a pool."""
import threading
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from app.config import settings as s

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _ensure_extension() -> None:
    # The extension must exist before register_vector can find the type.
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _pool_get() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _ensure_extension()
                _pool = ConnectionPool(
                    s.database_url, min_size=1, max_size=s.pg_pool_max, timeout=10,
                    configure=register_vector, kwargs={"autocommit": False}, open=True,
                )
    return _pool


@contextmanager
def connect():
    """A pooled connection. Commits on normal exit, rolls back on exception."""
    with _pool_get().connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema() -> None:
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS kb_chunks (
                chunk_id       TEXT PRIMARY KEY,
                doc_id         TEXT NOT NULL,
                title          TEXT NOT NULL,
                status         TEXT NOT NULL,
                effective_date DATE NOT NULL,
                owner          TEXT NOT NULL,
                section        TEXT NOT NULL,
                content        TEXT NOT NULL,
                content_hash   TEXT NOT NULL,
                embedding      vector({s.embed_dim}) NOT NULL,
                tsv tsvector GENERATED ALWAYS AS
                    (to_tsvector('english', title || ' ' || content)) STORED
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING GIN (tsv)")
        conn.execute("CREATE INDEX IF NOT EXISTS kb_chunks_status_idx ON kb_chunks (status)")
        # Conversation memory: masked questions and guarded answers only, purged after CONVERSATION_TTL_MIN.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_turns (
                conversation_id UUID        NOT NULL,
                turn_no         INT         NOT NULL,
                account         TEXT        NOT NULL,
                question_masked TEXT        NOT NULL,
                resolved_query  TEXT        NOT NULL,
                answer          TEXT        NOT NULL,
                citations       TEXT[]      NOT NULL,
                outcome         TEXT        NOT NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (conversation_id, turn_no)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS chat_turns_created_idx ON chat_turns (created_at)")
        # Embedding and LLM response cache, shared by all workers.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_cache (
                namespace  TEXT        NOT NULL,
                key        TEXT        NOT NULL,
                value      JSONB       NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (namespace, key)
            )""")
        # Fixed-window rate-limit counters, shared by all workers.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                key          TEXT        NOT NULL,
                window_start TIMESTAMPTZ NOT NULL,
                hits         INT         NOT NULL,
                PRIMARY KEY (key, window_start)
            )""")


def drop_all() -> None:
    """Drop the KB index only. Conversations, cache and rate limits are not tied to the embedding model."""
    _ensure_extension()
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS kb_chunks")
        conn.execute("DROP TABLE IF EXISTS index_meta")


def get_meta() -> dict[str, str]:
    with connect() as conn:
        return dict(conn.execute("SELECT key, value FROM index_meta").fetchall())


def set_meta(values: dict[str, str]) -> None:
    with connect() as conn:
        for k, v in values.items():
            conn.execute("INSERT INTO index_meta VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (k, str(v)))


class IndexNotReady(RuntimeError):
    pass


def check_index_compatible() -> None:
    """Refuse to query an index built with a different embedding model: vectors from different
    models live in different spaces and similarity scores between them are meaningless."""
    meta = get_meta()
    if not meta:
        raise IndexNotReady("KB index is empty. Run ingestion first: python -m app.ingest")
    built = (meta.get("provider"), meta.get("embed_model"), meta.get("embed_dim"))
    current = (s.provider, s.embed_model, str(s.embed_dim))
    if built != current:
        raise IndexNotReady(
            f"KB index was built with {built} but config uses {current}. "
            "Re-ingest: python -m app.ingest --reset"
        )
