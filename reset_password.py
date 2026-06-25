"""Reset the admin panel password from the command line.

Usage:
    python reset_password.py              # interactive prompt
    python reset_password.py newpass123   # non-interactive
"""

import sqlite3
import sys
from pathlib import Path

import bcrypt

DB_PATH = Path(__file__).resolve().parent / "data" / "app.db"


def reset_password(new_password: str, username: str = "admin") -> None:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    password_hash = bcrypt.hashpw(
        new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.execute(
            "UPDATE admin_users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
        if cursor.rowcount == 0:
            print(f"ERROR: No admin user found with username '{username}'")
            sys.exit(1)
        conn.commit()
        print(f"Password updated for '{username}' — restart the server if it's running.")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        new_password = sys.argv[1]
    else:
        import getpass

        new_password = getpass.getpass("New admin password: ")
        confirm = getpass.getpass("Confirm: ")
        if new_password != confirm:
            print("ERROR: Passwords do not match.")
            sys.exit(1)

    if len(new_password) < 4:
        print("ERROR: Password must be at least 4 characters.")
        sys.exit(1)

    reset_password(new_password)
