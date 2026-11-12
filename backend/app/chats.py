"""Chat storage: request schemas, SQL, and the /api/chats routes."""
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi import Path as ApiPath
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from database import pool

router = APIRouter(prefix="/api/chats", tags=["chats"])

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
ChatId = Annotated[str, ApiPath(pattern=ID_PATTERN)]


# ───────────────────────── Schemas ─────────────────────────

class MessageIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())  # we deliberately have a field called model_name

    role: Literal["user", "assistant"]
    content: str = Field("", max_length=500_000)
    timestamp: int | None = None  # epoch milliseconds
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    search_query: str | None = Field(None, max_length=2_000)
    rerank_note: str | None = Field(None, max_length=50)
    model_name: str | None = Field(None, max_length=200)
    elapsed: str | None = Field(None, max_length=20)
    stopped: bool = False
    is_error: bool = False


class ChatIn(BaseModel):
    title: str = Field("New Chat", max_length=200)
    created_at: int | None = None  # epoch milliseconds
    messages: list[MessageIn] = Field(default_factory=list, max_length=5_000)


class ChatImportItem(ChatIn):
    id: str = Field(pattern=ID_PATTERN)


class ChatImport(BaseModel):
    chats: list[ChatImportItem] = Field(max_length=1_000)


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ───────────────────────── Helpers ─────────────────────────

def _to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def _clean(value):
    """Postgres text and jsonb cannot contain NUL (\\x00), and PDF extraction
    occasionally produces it. One such character would make a chat unsaveable forever."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {(_clean(k) if isinstance(k, str) else k): _clean(v) for k, v in value.items()}
    return value


_INSERT_MESSAGE = """
    INSERT INTO messages (chat_id, position, role, content, sources, search_query,
                          rerank_note, model_name, elapsed, sent_at, stopped, is_error)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _insert_messages(conn, chat_id: str, messages: list[MessageIn]) -> None:
    if not messages:
        return
    rows = [
        (
            chat_id,
            position,
            m.role,
            _clean(m.content),
            Jsonb(_clean(m.sources)),
            _clean(m.search_query),
            m.rerank_note,
            _clean(m.model_name),
            m.elapsed,
            _to_dt(m.timestamp),
            m.stopped,
            m.is_error,
        )
        for position, m in enumerate(messages)
    ]
    with conn.cursor() as cur:
        cur.executemany(_INSERT_MESSAGE, rows)


# ───────────────────────── Routes ─────────────────────────
# Order matters: /search and /import must be declared BEFORE /{chat_id},
# otherwise "search" would be captured as a chat id.

@router.get("")
def list_chats():
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at, count(m.position) AS message_count
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return {
        "chats": [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": _ms(r["created_at"]),
                "updated_at": _ms(r["updated_at"]),
                "message_count": r["message_count"],
            }
            for r in rows
        ]
    }


@router.get("/search")
def search_chats(q: str = ""):
    term = q.strip()
    if not term:
        return {"ids": []}
    # Escape LIKE wildcards so "100%" or "my_file" are matched literally
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id FROM chats c
            WHERE c.title ILIKE %s
               OR EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.id AND m.content ILIKE %s)
            ORDER BY c.updated_at DESC
            LIMIT 200
            """,
            (pattern, pattern),
        ).fetchall()
    return {"ids": [r["id"] for r in rows]}


@router.post("/import")
def import_chats(payload: ChatImport):
    """One-time migration of chats from the browser's localStorage. Safe to repeat:
    chats whose id already exists are skipped, never overwritten."""
    imported = skipped = 0
    with pool.connection() as conn:  # one transaction: all or nothing
        for item in payload.chats:
            created = _to_dt(item.created_at) or datetime.now(timezone.utc)
            updated = _to_dt(max((m.timestamp for m in item.messages if m.timestamp), default=None)) or created
            row = conn.execute(
                """
                INSERT INTO chats (id, title, created_at, updated_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (item.id, _clean(item.title).strip() or "New Chat", created, updated),
            ).fetchone()
            if row is None:
                skipped += 1
                continue
            _insert_messages(conn, item.id, item.messages)
            imported += 1
    return {"imported": imported, "skipped": skipped}


@router.get("/{chat_id}")
def get_chat(chat_id: ChatId):
    with pool.connection() as conn:
        chat = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chats WHERE id = %s", (chat_id,)
        ).fetchone()
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        rows = conn.execute(
            """
            SELECT role, content, sources, search_query, rerank_note, model_name,
                   elapsed, sent_at, stopped, is_error
            FROM messages WHERE chat_id = %s ORDER BY position
            """,
            (chat_id,),
        ).fetchall()
    return {
        "id": chat["id"],
        "title": chat["title"],
        "created_at": _ms(chat["created_at"]),
        "updated_at": _ms(chat["updated_at"]),
        "messages": [
            {
                "role": r["role"],
                "content": r["content"],
                "timestamp": _ms(r["sent_at"]),
                "sources": r["sources"],
                "search_query": r["search_query"],
                "rerank_note": r["rerank_note"],
                "model_name": r["model_name"],
                "elapsed": r["elapsed"],
                "stopped": r["stopped"],
                "is_error": r["is_error"],
            }
            for r in rows
        ],
    }


@router.put("/{chat_id}")
def save_chat(chat_id: ChatId, payload: ChatIn):
    """Create or fully replace a chat. One transaction, so a reader never sees a half-saved chat."""
    title = _clean(payload.title).strip() or "New Chat"
    created = _to_dt(payload.created_at) or datetime.now(timezone.utc)
    with pool.connection() as conn:
        # Upserting the chat row first also takes a row lock, so two saves of the
        # same chat queue up here instead of colliding on the messages primary key.
        conn.execute(
            """
            INSERT INTO chats (id, title, created_at, updated_at) VALUES (%s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, updated_at = now()
            """,
            (chat_id, title, created),
        )
        conn.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        _insert_messages(conn, chat_id, payload.messages)
    return {"saved": chat_id, "messages": len(payload.messages)}


@router.patch("/{chat_id}")
def rename_chat(chat_id: ChatId, payload: TitleIn):
    title = _clean(payload.title).strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    with pool.connection() as conn:
        cur = conn.execute("UPDATE chats SET title = %s WHERE id = %s", (title, chat_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Chat not found")
    return {"id": chat_id, "title": title}


@router.delete("/{chat_id}")
def delete_chat(chat_id: ChatId):
    """Idempotent: deleting a chat that doesn't exist is not an error (messages cascade)."""
    with pool.connection() as conn:
        conn.execute("DELETE FROM chats WHERE id = %s", (chat_id,))
    return {"deleted": chat_id}