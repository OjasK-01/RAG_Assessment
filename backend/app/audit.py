"""Append-only JSONL audit log. Only masked text is ever written here."""
import json
import threading
from datetime import datetime, timezone

from app.config import settings as s

_lock = threading.Lock()


def write(event: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        s.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with s.audit_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
