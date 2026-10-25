"""Key-value cache in Postgres (shared by all API workers) with a small in-process front.

Used for embeddings (a query embedded once is never paid for again) and, when LLM_CACHE=true, for
LLM responses. Values are JSON. Postgres is already a dependency, so this needs no Redis; swapping
in Redis later only touches this module."""
import json
import logging
import threading
from pathlib import Path

from psycopg.types.json import Jsonb

from app import db

log = logging.getLogger(__name__)

_MEMORY_MAX = 5000  # per process; the database is the source of truth
_memory: dict[tuple[str, str], object] = {}
_lock = threading.Lock()


def get_many(namespace: str, keys: list[str]) -> dict[str, object]:
    found, missing = {}, []
    for k in keys:
        if (namespace, k) in _memory:
            found[k] = _memory[(namespace, k)]
        else:
            missing.append(k)
    if missing:
        try:
            with db.connect() as conn:
                rows = conn.execute("SELECT key, value FROM kv_cache WHERE namespace = %s AND key = ANY(%s)",
                                    (namespace, missing)).fetchall()
        except Exception as e:  # the cache is an optimisation: a DB hiccup must not fail the request
            log.warning("cache read failed: %s", e)
            rows = []
        for k, v in rows:
            found[k] = v
            _remember(namespace, k, v)
    return found


def get(namespace: str, key: str):
    return get_many(namespace, [key]).get(key)


def _write(namespace: str, items: dict[str, object]) -> None:
    with db.connect() as conn:
        for k, v in items.items():
            conn.execute(
                """INSERT INTO kv_cache (namespace, key, value) VALUES (%s, %s, %s)
                   ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, created_at = now()""",
                (namespace, k, Jsonb(v)))


def put_many(namespace: str, items: dict[str, object]) -> None:
    for k, v in items.items():
        _remember(namespace, k, v)
    try:
        _write(namespace, items)
    except Exception as e:
        log.warning("cache write failed: %s", e)


def put(namespace: str, key: str, value) -> None:
    put_many(namespace, {key: value})


def _remember(namespace: str, key: str, value) -> None:
    with _lock:
        if len(_memory) >= _MEMORY_MAX:
            _memory.clear()
        _memory[(namespace, key)] = value


def import_json_file(namespace: str, path: Path) -> int:
    """One-time migration of the old per-process JSON cache files. The file is renamed afterwards."""
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
        if data:
            _write(namespace, data)  # raises on failure, so the file is only renamed once it is stored
        path.rename(path.with_suffix(".json.imported"))
        log.info("imported %d cache entries from %s", len(data), path.name)
        return len(data)
    except Exception as e:
        log.warning("could not import %s: %s", path.name, e)
        return 0
