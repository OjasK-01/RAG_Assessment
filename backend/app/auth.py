"""Account-level auth: bcrypt-hashed accounts from .env, short-lived HS256 JWTs, role checks,
and rate limits (per account for /ask, per client+username for login).

Rate limits are counted in Postgres (RATE_LIMIT_BACKEND=postgres, the default) so every API worker
and replica shares one budget per account; "memory" is a per-process sliding window for tests."""
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.config import settings as s

log = logging.getLogger(__name__)

# OAuth2 password flow: standard Bearer tokens, and Swagger's "Authorize" button can log in directly.
_bearer = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)
# Compared against when the username doesn't exist, so response time doesn't reveal valid usernames.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(12))


@dataclass(frozen=True)
class User:
    username: str
    role: str


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, slow down.",
                         headers={"Retry-After": str(max(1, retry_after))})


class RateLimiter:
    """Per-process sliding window."""

    def __init__(self, name: str, limit: int, window_s: float = 60):
        self.name, self.limit, self.window = name, limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise _too_many(int(self.window - (now - q[0])) + 1)
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class PgRateLimiter:
    """Sliding-window estimate counted in Postgres, shared by all workers: per-minute counters, and
    the previous minute's count weighted by how much of it still overlaps the last 60 s (so a burst
    can't get 2x the limit across a minute boundary, as a plain fixed window allows).
    If the database is unreachable it falls back to the per-process limiter (login doesn't need the
    database, so failing open would leave password guessing unthrottled)."""

    def __init__(self, name: str, limit: int, window_s: int = 60):
        self.name, self.limit, self.window = name, limit, window_s
        self._fallback = RateLimiter(name, limit, window_s)

    def check(self, key: str) -> None:
        from app import db
        now = time.time()
        window_start = int(now // self.window * self.window)
        k = f"{self.name}:{key}"
        try:
            with db.connect() as conn:
                hits = conn.execute(
                    """INSERT INTO rate_limits (key, window_start, hits) VALUES (%s, to_timestamp(%s), 1)
                       ON CONFLICT (key, window_start) DO UPDATE SET hits = rate_limits.hits + 1
                       RETURNING hits""",
                    (k, window_start)).fetchone()[0]
                prev = conn.execute("SELECT hits FROM rate_limits WHERE key = %s AND window_start = to_timestamp(%s)",
                                    (k, window_start - self.window)).fetchone()
                if hits == 1:  # first hit of a window: tidy up old windows
                    conn.execute("DELETE FROM rate_limits WHERE window_start < now() - interval '10 minutes'")
        except Exception as e:
            log.warning("rate limiter DB unavailable (%s); using per-process limit", type(e).__name__)
            return self._fallback.check(key)
        overlap = 1 - (now - window_start) / self.window
        if hits + (prev[0] if prev else 0) * overlap > self.limit:
            raise _too_many(int(window_start + self.window - now) + 1)

    def reset(self) -> None:
        from app import db
        self._fallback.reset()
        with db.connect() as conn:
            conn.execute("DELETE FROM rate_limits WHERE key LIKE %s", (f"{self.name}:%",))


_Limiter = PgRateLimiter if s.rate_limit_backend == "postgres" else RateLimiter
ask_limiter = _Limiter("ask", s.rate_limit_per_min)
login_limiter = _Limiter("login", s.login_rate_limit_per_min)


def authenticate(username: str, password: str) -> User | None:
    entry = s.users.get(username)
    pw_hash = entry[1].encode() if entry else _DUMMY_HASH
    ok = bcrypt.checkpw(password.encode(), pw_hash)
    return User(username, entry[0]) if ok and entry else None


def issue_token(user: User) -> tuple[str, int]:
    now = int(time.time())
    ttl = s.jwt_ttl_min * 60
    claims = {"sub": user.username, "role": user.role, "iat": now, "exp": now + ttl,
              "jti": secrets.token_hex(8), "iss": "vectra-kb-assistant"}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256"), ttl


def current_user(token: str | None = Depends(_bearer)) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})
    if not token:
        raise unauthorized
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=["HS256"],
                            issuer="vectra-kb-assistant", options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError:
        raise unauthorized
    # The account must still exist with the same role: removing it from .env revokes its tokens.
    entry = s.users.get(claims["sub"])
    if not entry or entry[0] != claims["role"]:
        raise unauthorized
    return User(claims["sub"], claims["role"])


def require_roles(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot access this resource")
        return user
    return dep


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
