"""Account tools for whoever runs the server. Inside the backend container:

    docker compose exec backend python -m admin create-admin
    docker compose exec backend python -m admin set-password USERNAME     (recovery if an admin is locked out)
    docker compose exec backend python -m admin list-users
"""
import getpass
import sys

import psycopg

from auth import MAX_PASSWORD, MIN_PASSWORD, USERNAME_RE, hasher
from database import close_db, init_db, pool


def ask_password() -> str:
    while True:
        first = getpass.getpass(f"Password ({MIN_PASSWORD}+ characters): ")  # typed invisibly, never stored in shell history
        if not MIN_PASSWORD <= len(first) <= MAX_PASSWORD:
            print(f"The password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters.")
            continue
        if first != getpass.getpass("Repeat password: "):
            print("The two passwords differ. Try again.")
            continue
        return first


def create_admin() -> None:
    username = input("Username: ").strip()
    if not USERNAME_RE.match(username):
        sys.exit("Username must be 3 to 32 characters: letters, numbers, dots, dashes or underscores.")
    display_name = input(f"Display name [{username}]: ").strip() or username
    password_hash = hasher.hash(ask_password())
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role) VALUES (%s, %s, %s, 'admin')",
                (username, display_name, password_hash),
            )
    except psycopg.errors.UniqueViolation:
        sys.exit(f'A user named "{username}" already exists.')
    print(f'Created admin "{username}". You can sign in now.')


def set_password(username: str) -> None:
    password_hash = hasher.hash(ask_password())
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE users SET password_hash = %s WHERE lower(username) = lower(%s) RETURNING id",
            (password_hash, username),
        ).fetchone()
        if row is None:
            sys.exit(f'No user named "{username}".')
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (row["id"],))  # sign them out everywhere
    print(f'Password changed for "{username}".')


def list_users() -> None:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT username, display_name, role, status, last_login_at FROM users ORDER BY lower(username)"
        ).fetchall()
    if not rows:
        print("No users yet. Create one with: python -m admin create-admin")
    for r in rows:
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "never"
        print(f'{r["username"]:<20} {r["role"]:<6} {r["status"]:<9} last sign-in: {last}   ({r["display_name"]})')


def main() -> None:
    args = sys.argv[1:]
    commands = {"create-admin": 0, "set-password": 1, "list-users": 0}
    if not args or args[0] not in commands or len(args) - 1 != commands[args[0]]:
        sys.exit(__doc__)
    init_db()
    try:
        if args[0] == "create-admin":
            create_admin()
        elif args[0] == "set-password":
            set_password(args[1])
        else:
            list_users()
    finally:
        close_db()


if __name__ == "__main__":
    main()