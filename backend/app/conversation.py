"""Server-side conversation memory, so follow-ups like "what about the first response?" work.

History lives in Postgres, keyed by conversation id AND account: the client only holds an opaque id,
so it can't forge earlier turns (e.g. a fake assistant reply with instructions in it), and one account
can't read another's conversation. Only the masked question and the guarded answer are stored, and
turns are purged after CONVERSATION_TTL_MIN."""
import uuid
from dataclasses import dataclass

from app import db
from app.config import settings as s

# Turns worth carrying forward as context. Refusals, errors and small talk are left out, so a blocked
# injection attempt can't come back as "context" in the next turn.
CONTEXT_OUTCOMES = ("answered", "abstained_no_match", "abstained_model", "blocked_output_guard")


@dataclass(frozen=True)
class Turn:
    question: str        # masked
    resolved_query: str  # what retrieval searched for (follow-ups include the earlier question)
    answer: str
    citations: tuple[str, ...]
    outcome: str


def new_id() -> str:
    return str(uuid.uuid4())


def load(conversation_id: str, account: str, limit: int | None = None) -> list[Turn]:
    """The last `limit` turns of this account's conversation, oldest first. Unknown, expired or
    someone else's conversation -> [] (no error, so ids can't be probed)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT question_masked, resolved_query, answer, citations, outcome FROM chat_turns
               WHERE conversation_id = %s AND account = %s
                 AND created_at > now() - make_interval(mins => %s)
               ORDER BY turn_no DESC LIMIT %s""",
            (conversation_id, account, s.conversation_ttl_min, limit or s.history_turns),
        ).fetchall()
    return [Turn(q, rq, a, tuple(c), o) for q, rq, a, c, o in reversed(rows)]


def append(conversation_id: str, account: str, turn: Turn) -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO chat_turns (conversation_id, turn_no, account, question_masked, resolved_query,
                                       answer, citations, outcome)
               SELECT %s, COALESCE(MAX(turn_no), 0) + 1, %s, %s, %s, %s, %s, %s
               FROM chat_turns WHERE conversation_id = %s""",
            (conversation_id, account, turn.question, turn.resolved_query, turn.answer,
             list(turn.citations), turn.outcome, conversation_id),
        )
        conn.execute("DELETE FROM chat_turns WHERE created_at < now() - make_interval(mins => %s)",
                     (s.conversation_ttl_min,))


def delete(conversation_id: str, account: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM chat_turns WHERE conversation_id = %s AND account = %s",
                     (conversation_id, account))
