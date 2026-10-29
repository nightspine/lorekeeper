"""Admin sign-in: one account, server-side sessions, stdlib only.

The first start creates `admin` with an initial password (from the environment or generated and printed
once; only a scrypt hash is stored) and forces a new one at the first sign-in. A session is a random
cookie token; the server keeps only its sha256 in `auth.json` (0600, written atomically), so a leaked
data file cannot be replayed as a cookie. Sessions expire after 30 days idle or 90 days in all. Failed
sign-ins are throttled in memory per (username, client address). Whoever can read the data directory
can reset the password (`lorekeeper reset-password`), so file access is admin access.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 3
COOKIE = "lorekeeper_session"
ADMIN = "admin"
IDLE = timedelta(days=30)
ABSOLUTE = timedelta(days=90)
SEEN_REFRESH = timedelta(hours=1)
MIN_LEN, MAX_LEN = 12, 128
MAX_FREE_FAILURES = 5
MAX_LOCK = 15 * 60

RULE = "─" * 46


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                          maxmem=max(64 * 1024 * 1024, 256 * n * r), dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = SCRYPT_N, SCRYPT_R, SCRYPT_P
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(_scrypt(password, salt, n, r, p))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, TypeError):
        return False


def check_password(new: str, current_plain: str | None = None) -> str | None:
    """The policy error for a new password, or None when it is acceptable."""
    if len(new) < MIN_LEN:
        return f"Use at least {MIN_LEN} characters."
    if len(new) > MAX_LEN:
        return f"Use at most {MAX_LEN} characters."
    if current_plain is not None and new == current_plain:
        return "Choose a different password."
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def say(message: str) -> None:
    """print with a flush: the first-start password must reach a redirected log at once."""
    print(message, flush=True)


def _announce(password: str, log, reason: str = "First start.") -> None:
    for line in (RULE, f"{reason} Sign in as  admin  with the password", f"    {password}",
                 "You will be asked to choose a new password.", RULE):
        log(line)


class Throttle:
    """After 5 failures for a key, refuse for 2^(failures-5) seconds (max 15 minutes). Success clears it."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._state: dict[tuple, tuple[int, float]] = {}   # key → (failures, locked until)
        self._lock = threading.Lock()

    def wait(self, key: tuple) -> int:
        """Whole seconds to wait before another attempt (0 = go ahead)."""
        with self._lock:
            _, until = self._state.get(key, (0, 0.0))
        left = until - self.clock()
        return math.ceil(left) if left > 0 else 0

    def fail(self, key: tuple) -> None:
        with self._lock:
            n = self._state.get(key, (0, 0.0))[0] + 1
            lock = min(2 ** (n - MAX_FREE_FAILURES), MAX_LOCK) if n >= MAX_FREE_FAILURES else 0
            self._state[key] = (n, self.clock() + lock)

    def clear(self, key: tuple) -> None:
        with self._lock:
            self._state.pop(key, None)


class Auth:
    def __init__(self, path: Path, clock=time.monotonic) -> None:
        self.path = path
        self.throttle = Throttle(clock)
        self._lock = threading.RLock()
        self._dummy = hash_password("not a real password")

    # store
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("users", {})
        data.setdefault("sessions", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # users
    def has_users(self) -> bool:
        return bool(self._read()["users"])

    def ensure_admin(self, initial_password: str | None = None, log=say) -> bool:
        """Create admin on a first start (nothing printed when users exist). Returns whether it did."""
        with self._lock:
            data = self._read()
            if data["users"]:
                return False
            password = initial_password or secrets.token_urlsafe(12)
            data["users"][ADMIN] = {"hash": hash_password(password), "mustChangePassword": True,
                                    "created": _iso(_now()), "changed": None}
            self._write(data)
        _announce(password, log)
        return True

    def reset_password(self, log=say) -> bool:
        """Give admin a new generated password, force a change and sign everyone out. False if no admin."""
        with self._lock:
            data = self._read()
            user = data["users"].get(ADMIN)
            if not user:
                return False
            password = secrets.token_urlsafe(12)
            user.update(hash=hash_password(password), mustChangePassword=True)
            data["sessions"] = {}
            self._write(data)
        _announce(password, log, "Password reset.")
        return True

    def user(self, name: str) -> dict | None:
        return self._read()["users"].get(name)

    def public_user(self, name: str) -> dict:
        u = self.user(name) or {}
        return {"name": name, "mustChangePassword": bool(u.get("mustChangePassword"))}

    def setup(self) -> bool:
        """True while admin has never chosen their own password."""
        return any(u.get("changed") is None for u in self._read()["users"].values())

    def authenticate(self, username: str, password: str) -> str | None:
        """The canonical user name when the password is right. Unknown names still cost one hash."""
        name = username.strip().lower()
        u = self._read()["users"].get(name)
        ok = verify_password(password, u["hash"] if u else self._dummy)
        return name if u and ok else None

    def set_password(self, name: str, new: str) -> None:
        with self._lock:
            data = self._read()
            u = data["users"][name]
            u.update(hash=hash_password(new), mustChangePassword=False, changed=_iso(_now()))
            self._write(data)

    # sessions
    def create_session(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _iso(_now())
        with self._lock:
            data = self._read()
            data["sessions"][_token_hash(token)] = {"user": name, "created": now, "seen": now}
            self._write(data)
        return token

    def session_for(self, token: str | None) -> str | None:
        """The user name for a live session cookie, refreshing `seen` at most once an hour."""
        if not token:
            return None
        key, now = _token_hash(token), _now()
        with self._lock:
            data = self._read()
            s = data["sessions"].get(key)
            if not s:
                return None
            try:
                created, seen = datetime.fromisoformat(s["created"]), datetime.fromisoformat(s["seen"])
            except (KeyError, ValueError):
                created = seen = datetime.min.replace(tzinfo=timezone.utc)
            if now - seen > IDLE or now - created > ABSOLUTE or s.get("user") not in data["users"]:
                del data["sessions"][key]
                self._write(data)
                return None
            if now - seen > SEEN_REFRESH:
                s["seen"] = _iso(now)
                self._write(data)
            return s["user"]

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            data = self._read()
            if data["sessions"].pop(_token_hash(token), None) is not None:
                self._write(data)

    def delete_other_sessions(self, name: str, keep_token: str | None) -> None:
        keep = _token_hash(keep_token) if keep_token else None
        with self._lock:
            data = self._read()
            data["sessions"] = {k: v for k, v in data["sessions"].items()
                                if v.get("user") != name or k == keep}
            self._write(data)
