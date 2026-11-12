import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://rag:ragchat_dev@localhost:5432/ragchat")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK_ID = 727_001  # arbitrary constant: serialises migrations if two processes start at once

# open=False: the pool is opened explicitly in init_db()
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
    open=False,
)


def init_db() -> None:
    """Open the pool, then apply any migrations that haven't run yet."""
    # The pool keeps retrying in the background; this raises PoolTimeout if
    # Postgres still isn't reachable after 60 seconds.
    pool.open(wait=True, timeout=60)
    _run_migrations()


def close_db() -> None:
    pool.close()


def _run_migrations() -> None:
    with pool.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with pool.connection() as conn:  # one transaction per migration file
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (path.name,)).fetchone():
                continue
            conn.execute(path.read_text(encoding="utf-8"))  # no parameters, so multiple statements are allowed
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            print(f"[db] applied migration {path.name}")