"""Accounts and sessions.

- Passwords are hashed with scrypt (memory-hard, standard library - no
  extra dependency) with a per-user random salt.
- A session is a random 256-bit token sent to the browser in an HttpOnly
  cookie. Only its SHA-256 is stored server-side.
- Demo sessions need no account and expire quickly; they see the same
  real data as signed-in users - "demo" means "no account", never
  "sample data".
- Repeated failed logins for one email are throttled in-process.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.storage.models import SessionORM, UserORM

SESSION_COOKIE = "verifi_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# A fixed hash to verify against when the email doesn't exist, so a login
# attempt takes the same time whether or not the account exists.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Principal:
    kind: str  # "user" | "demo"
    user_id: str | None
    name: str
    email: str | None
    expires_at: datetime


# ---- Failed-login throttle -----------------------------------------------------

_FAILURE_WINDOW_SECONDS = 300
_MAX_FAILURES = 5
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _recent_failures(key: str) -> list[float]:
    cutoff = time.monotonic() - _FAILURE_WINDOW_SECONDS
    with _failures_lock:
        kept = [t for t in _failures.get(key, []) if t > cutoff]
        _failures[key] = kept
        return kept


def _record_failure(key: str) -> None:
    with _failures_lock:
        _failures.setdefault(key, []).append(time.monotonic())


def reset_throttle() -> None:
    with _failures_lock:
        _failures.clear()


# ---- Accounts ------------------------------------------------------------------


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(db: Session, name: str, email: str, password: str) -> UserORM:
    name = name.strip()
    email = normalize_email(email)
    if not name or len(name) > 80:
        raise AuthError("Please enter your name.")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if db.scalar(select(UserORM).where(UserORM.email == email)):
        raise AuthError("An account with this email already exists. Sign in instead.", status=409)
    user = UserORM(user_id=f"usr_{uuid.uuid4().hex[:16]}", email=email, name=name,
                   password_hash=hash_password(password), created_at=_now())
    db.add(user)
    db.commit()
    return user


def authenticate(db: Session, email: str, password: str) -> UserORM:
    email = normalize_email(email)
    if len(_recent_failures(email)) >= _MAX_FAILURES:
        raise AuthError("Too many failed attempts. Please wait a few minutes and try again.", status=429)
    user = db.scalar(select(UserORM).where(UserORM.email == email))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        _record_failure(email)
        raise AuthError("Incorrect email or password.", status=401)
    return user


# ---- Sessions -------------------------------------------------------------------


def start_session(db: Session, kind: str, user_id: str | None = None) -> tuple[str, datetime]:
    settings = get_settings()
    lifetime = timedelta(days=settings.user_session_days) if kind == "user" else timedelta(hours=settings.demo_session_hours)
    token = secrets.token_urlsafe(32)
    expires = _now() + lifetime
    db.add(SessionORM(token_hash=_hash_token(token), kind=kind, user_id=user_id, created_at=_now(), expires_at=expires))
    # Opportunistic cleanup keeps the table from growing without bound.
    db.execute(delete(SessionORM).where(SessionORM.expires_at < _now()))
    db.commit()
    return token, expires


def resolve_session(db: Session, token: str | None) -> Principal | None:
    if not token:
        return None
    row = db.get(SessionORM, _hash_token(token))
    if row is None or row.expires_at < _now():
        return None
    if row.kind == "demo":
        return Principal(kind="demo", user_id=None, name="Guest", email=None, expires_at=row.expires_at)
    user = db.get(UserORM, row.user_id) if row.user_id else None
    if user is None:
        return None
    return Principal(kind="user", user_id=user.user_id, name=user.name, email=user.email, expires_at=row.expires_at)


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(SessionORM).where(SessionORM.token_hash == _hash_token(token)))
        db.commit()
