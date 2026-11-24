"""Accounts: Argon2 password hashing, cookie sessions stored in Postgres, one-time
setup links, and the /api/auth and /api/admin routes."""
import hashlib
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Literal

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from database import pool

# ───────────────────────── Settings ─────────────────────────
SESSION_COOKIE = "ragchat_session"  # a distinctive name: cookies are shared across ports on localhost
SESSION_IDLE_DAYS = 14   # signed out after this long without activity
SESSION_MAX_DAYS = 60    # ...and never stays signed in longer than this
SETUP_LINK_DAYS = 7
MIN_PASSWORD = 10
MAX_PASSWORD = 128
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"  # set to true once served over HTTPS
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")  # used to build the links you send people

ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]
if PUBLIC_URL not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append(PUBLIC_URL)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINK_INVALID = "This link is invalid, has expired, or has already been used."

hasher = PasswordHasher()  # Argon2id with the library's recommended defaults
# Checked against when a username doesn't exist, so response time can't reveal which usernames exist
_DUMMY_HASH = hasher.hash("not-a-real-password")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(user: dict) -> dict:
    return {k: user[k] for k in ("id", "username", "display_name", "email", "role")}


# ───────────────────────── Validation (friendly messages) ─────────────────────────

def _check_username(value: str | None) -> str:
    value = (value or "").strip()
    if not USERNAME_RE.match(value):
        raise HTTPException(422, "Username must be 3 to 32 characters: letters, numbers, dots, dashes or underscores.")
    return value


def _check_display_name(value: str | None) -> str:
    value = (value or "").strip()
    if not 1 <= len(value) <= 60:
        raise HTTPException(422, "Display name must be 1 to 60 characters.")
    return value


def _check_email(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if len(value) > 200 or not EMAIL_RE.match(value):
        raise HTTPException(422, "That email address doesn't look right.")
    return value


def _check_password(password: str, username: str | None = None) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise HTTPException(422, f"Password must be at most {MAX_PASSWORD} characters.")
    if username and password.lower() == username.lower():
        raise HTTPException(422, "Password can't be the same as the username.")


# ───────────────────────── Login rate limiting ─────────────────────────

class _Limiter:
    """Sliding window of failed attempts, held in memory (so it resets when the server restarts)."""

    def __init__(self, max_failures: int, window_seconds: int):
        self.max = max_failures
        self.window = window_seconds
        self.failures: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        with self.lock:
            q = self.failures.get(key)
            if not q:
                return 0
            now = time.monotonic()
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max:
                return int(self.window - (now - q[0])) + 1
            return 0

    def fail(self, key: str) -> None:
        with self.lock:
            now = time.monotonic()
            if len(self.failures) > 5000:  # someone is spraying usernames: drop entries that have expired
                for k in [k for k, q in self.failures.items() if not q or now - q[-1] > self.window]:
                    del self.failures[k]
            self.failures[key].append(now)

    def clear(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


_login_by_user = _Limiter(max_failures=8, window_seconds=15 * 60)  # per username
_login_global = _Limiter(max_failures=60, window_seconds=60)       # across everyone: slows down password spraying


# ───────────────────────── Sessions and dependencies ─────────────────────────

def check_origin(request: Request) -> None:
    """Defense in depth next to SameSite cookies: a browser request that changes data
    must come from one of our own origins."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
            raise HTTPException(403, "Origin not allowed. If you use a new address, add it to ALLOWED_ORIGINS.")


def _start_session(conn, user_id: int, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at < %s", (now,))  # tidy up old rows
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, user_agent) VALUES (%s, %s, %s, %s)",
        (_hash_token(token), user_id, now + timedelta(days=SESSION_IDLE_DAYS), (request.headers.get("user-agent") or "")[:200]),
    )
    conn.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user_id))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_DAYS * 86400,  # the server enforces the idle and absolute limits; the cookie just carries the token
        httponly=True,                     # JavaScript can never read it
        samesite="lax",                    # not sent on cross-site POSTs
        secure=COOKIE_SECURE,
        path="/",
    )


def current_user(request: Request) -> dict:
    """Dependency for every protected route: who is signed in? Raises 401 if nobody."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in.")
    token_hash = _hash_token(token)
    now = _now()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.email, u.role, s.last_seen_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > %s AND s.created_at > %s AND u.status = 'active'
            """,
            (token_hash, now, now - timedelta(days=SESSION_MAX_DAYS)),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "Your session has ended. Please sign in again.")
        if now - row["last_seen_at"] > timedelta(minutes=5):  # slide the idle timeout, but not on every request
            conn.execute(
                "UPDATE sessions SET last_seen_at = %s, expires_at = %s WHERE token_hash = %s",
                (now, now + timedelta(days=SESSION_IDLE_DAYS), token_hash),
            )
    return _public(row)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrators only.")
    return user


# ───────────────────────── /api/auth ─────────────────────────
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)


class SetupCheckIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class SetupCompleteIn(SetupCheckIn):
    password: str = Field(max_length=1000)
    username: str | None = Field(None, max_length=100)
    display_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=300)


@auth_router.get("/bootstrap")
def bootstrap():
    """Lets the login page tell a first-time installer what to do. Reveals nothing else."""
    with pool.connection() as conn:
        row = conn.execute("SELECT NOT EXISTS (SELECT 1 FROM users) AS needs_admin").fetchone()
    return {"needs_admin": row["needs_admin"]}


@auth_router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    username = body.username.strip().lower()
    wait = max(_login_by_user.retry_after(username), _login_global.retry_after("all"))
    if wait:
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
            headers={"Retry-After": str(wait)},
        )

    with pool.connection() as conn:
        user = conn.execute(
            "SELECT id, username, display_name, email, role, status, password_hash FROM users WHERE lower(username) = %s",
            (username,),
        ).fetchone()

    has_password = bool(user and user["password_hash"])
    try:
        hasher.verify(user["password_hash"] if has_password else _DUMMY_HASH, body.password)
        password_ok = has_password
    except (VerificationError, InvalidHashError):
        password_ok = False

    if not password_ok:
        _login_by_user.fail(username)
        _login_global.fail("all")
        raise HTTPException(401, "Incorrect username or password.")  # same message whether or not the user exists
    if user["status"] != "active":
        raise HTTPException(403, "This account is disabled. Please contact the administrator.")

    _login_by_user.clear(username)
    with pool.connection() as conn:
        if hasher.check_needs_rehash(user["password_hash"]):  # keeps hashes current if the library's defaults get stronger
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hasher.hash(body.password), user["id"]))
        _start_session(conn, user["id"], request, response)
    return _public(user)


@auth_router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with pool.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    return {"ok": True}


@auth_router.get("/me")
def me(user: dict = Depends(current_user)):
    return user


@auth_router.post("/setup/check")
def setup_check(body: SetupCheckIn):
    """Is this link still usable? Used to show the right form."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT l.purpose, u.username, u.display_name
            FROM setup_links l LEFT JOIN users u ON u.id = l.user_id
            WHERE l.token_hash = %s AND l.used_at IS NULL AND l.revoked_at IS NULL AND l.expires_at > now()
            """,
            (_hash_token(body.token),),
        ).fetchone()
    if row is None:
        raise HTTPException(404, LINK_INVALID)
    return {"purpose": row["purpose"], "username": row["username"], "display_name": row["display_name"]}


@auth_router.post("/setup/complete")
def setup_complete(body: SetupCompleteIn, request: Request, response: Response):
    """Spend a setup link: create the account (invite) or set a new password, then sign the person in."""
    username = display_name = email = None
    # A cheap look first, so we validate the right fields and don't hash for nothing
    with pool.connection() as conn:
        peek = conn.execute(
            "SELECT purpose FROM setup_links WHERE token_hash = %s AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()",
            (_hash_token(body.token),),
        ).fetchone()
    if peek is None:
        raise HTTPException(404, LINK_INVALID)
    if peek["purpose"] == "invite":
        username = _check_username(body.username)
        display_name = _check_display_name(body.display_name)
        email = _check_email(body.email)
    _check_password(body.password, username)
    password_hash = hasher.hash(body.password)

    with pool.connection() as conn:  # one transaction: if anything below fails, the link is NOT used up
        # Atomic claim: of two simultaneous attempts with the same link, only one gets a row back
        link = conn.execute(
            """
            UPDATE setup_links SET used_at = now()
            WHERE token_hash = %s AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
            RETURNING purpose, user_id
            """,
            (_hash_token(body.token),),
        ).fetchone()
        if link is None:
            raise HTTPException(404, LINK_INVALID)

        if link["purpose"] == "invite":
            if conn.execute("SELECT 1 FROM users WHERE lower(username) = lower(%s)", (username,)).fetchone():
                raise HTTPException(409, "That username is already taken. Please choose another.")
            try:
                user = conn.execute(
                    """
                    INSERT INTO users (username, display_name, email, password_hash)
                    VALUES (%s, %s, %s, %s) RETURNING id, username, display_name, email, role
                    """,
                    (username, display_name, email, password_hash),
                ).fetchone()
            except psycopg.errors.UniqueViolation:
                raise HTTPException(409, "That username is already taken. Please choose another.")
        else:
            user = conn.execute(
                """
                UPDATE users SET password_hash = %s WHERE id = %s AND status = 'active'
                RETURNING id, username, display_name, email, role
                """,
                (password_hash, link["user_id"]),
            ).fetchone()
            if user is None:
                raise HTTPException(403, "This account is disabled. Please contact the administrator.")
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))  # a new password signs out every old session

        _start_session(conn, user["id"], request, response)
    return _public(user)


# ───────────────────────── /api/admin ─────────────────────────
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class InviteIn(BaseModel):
    note: str | None = Field(None, max_length=100)


class NewUserIn(BaseModel):
    username: str = Field(max_length=100)
    display_name: str = Field(max_length=200)
    role: Literal["admin", "user"] = "user"


class UserPatch(BaseModel):
    status: Literal["active", "disabled"] | None = None
    role: Literal["admin", "user"] | None = None


def _issue_link(conn, purpose: str, created_by: int, user_id: int | None = None, note: str | None = None) -> dict:
    """Create a one-time link. The raw token exists only in the return value: it is never stored."""
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SETUP_LINK_DAYS)
    conn.execute(
        "INSERT INTO setup_links (token_hash, purpose, user_id, created_by, expires_at, note) VALUES (%s, %s, %s, %s, %s, %s)",
        (_hash_token(token), purpose, user_id, created_by, expires, note),
    )
    # The token goes after "#": browsers never send that part to a server, so it can't end up in any log
    return {"url": f"{PUBLIC_URL}/#setup={token}", "expires_at": _ms(expires)}


@admin_router.get("/users")
def list_users():
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, username, display_name, email, role, status,
                   (password_hash IS NOT NULL) AS has_password, created_at, last_login_at
            FROM users ORDER BY lower(username)
            """
        ).fetchall()
    return {
        "users": [
            {**{k: r[k] for k in ("id", "username", "display_name", "email", "role", "status", "has_password")},
             "created_at": _ms(r["created_at"]), "last_login_at": _ms(r["last_login_at"])}
            for r in rows
        ]
    }


@admin_router.post("/invites")
def create_invite(body: InviteIn, admin: dict = Depends(current_user)):
    note = (body.note or "").strip() or None
    with pool.connection() as conn:
        return _issue_link(conn, "invite", admin["id"], note=note)


@admin_router.post("/users")
def create_user(body: NewUserIn, admin: dict = Depends(current_user)):
    """Create an account without a password, plus a link for the person to choose one."""
    username = _check_username(body.username)
    display_name = _check_display_name(body.display_name)
    with pool.connection() as conn:
        try:
            user = conn.execute(
                "INSERT INTO users (username, display_name, role) VALUES (%s, %s, %s) RETURNING id, username",
                (username, display_name, body.role),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "That username is already taken.")
        link = _issue_link(conn, "password", admin["id"], user_id=user["id"])
    return {"user": {"id": user["id"], "username": user["username"]}, **link}


@admin_router.post("/users/{user_id}/reset-link")
def reset_link(user_id: int, admin: dict = Depends(current_user)):
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone():
            raise HTTPException(404, "User not found.")
        conn.execute(  # an older unused link for this person must stop working
            "UPDATE setup_links SET revoked_at = now() WHERE user_id = %s AND purpose = 'password' AND used_at IS NULL AND revoked_at IS NULL",
            (user_id,),
        )
        return _issue_link(conn, "password", admin["id"], user_id=user_id)


@admin_router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: dict = Depends(current_user)):
    # Because you can never change yourself, at least one active admin (you) always remains
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't change your own role or status. Ask another admin.")
    sets, params = [], []
    if body.status:
        sets.append("status = %s")
        params.append(body.status)
    if body.role:
        sets.append("role = %s")
        params.append(body.role)
    if not sets:
        raise HTTPException(400, "Nothing to change.")
    params.append(user_id)
    with pool.connection() as conn:
        row = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s RETURNING id", params).fetchone()  # column names are fixed above, never user input
        if row is None:
            raise HTTPException(404, "User not found.")
        if body.status == "disabled":
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))  # signed out immediately
    return {"id": user_id}


@admin_router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(current_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account.")
    with pool.connection() as conn:
        row = conn.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found.")
    return {"deleted": user_id}


@admin_router.get("/links")
def list_links():
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.purpose, l.note, l.created_at, l.expires_at, u.username AS for_username
            FROM setup_links l LEFT JOIN users u ON u.id = l.user_id
            WHERE l.used_at IS NULL AND l.revoked_at IS NULL AND l.expires_at > now()
            ORDER BY l.created_at DESC
            """
        ).fetchall()
    return {
        "links": [
            {"id": r["id"], "purpose": r["purpose"], "note": r["note"], "for_username": r["for_username"],
             "created_at": _ms(r["created_at"]), "expires_at": _ms(r["expires_at"])}
            for r in rows
        ]
    }


@admin_router.delete("/links/{link_id}")
def revoke_link(link_id: int):
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE setup_links SET revoked_at = now() WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL RETURNING id",
            (link_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Link not found or already used.")
    return {"revoked": link_id}