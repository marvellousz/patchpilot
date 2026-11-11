"""Accounts, sessions and connected repositories in SQLite. GitHub tokens are encrypted at rest."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SESSION_SECONDS = 7 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL COLLATE NOCASE,
    token_enc BLOB NOT NULL,
    token_hint TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (user_id, full_name)
);
CREATE TABLE IF NOT EXISTS run_requests (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    at REAL NOT NULL
);
"""


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_key(data_dir: Path) -> bytes:
    """PATCHPILOT_SECRET_KEY if set, else a key generated once into data/secret.key."""
    if key := os.getenv("PATCHPILOT_SECRET_KEY", "").strip():
        return key.encode()
    path = data_dir / "secret.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return path.read_bytes().strip()


class Store:
    def __init__(self, data_dir: Path = DATA_DIR):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "patchpilot.db"
        self.fernet = Fernet(load_key(data_dir))
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- accounts and sessions ---

    def create_user(self, email: str, password: str) -> int:
        try:
            with self._db() as db:
                cur = db.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                                 (email, hash_password(password), time.time()))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("An account with this email already exists") from None

    def user_id(self, email: str) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        return row["id"] if row else None

    def set_password(self, user_id: int, password: str) -> None:
        with self._db() as db:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def authenticate(self, email: str, password: str) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password(password, row["password_hash"]):
            return row["id"]
        return None

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (_sha256(token), user_id, time.time() + SESSION_SECONDS))
        return token

    def session_user(self, token: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT users.id, users.email FROM sessions JOIN users ON users.id = sessions.user_id "
                             "WHERE token_hash = ? AND expires_at > ?", (_sha256(token), time.time())).fetchone()
        return dict(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))

    # --- run quota ---

    def record_run_request(self, user_id: int) -> None:
        with self._db() as db:
            db.execute("INSERT INTO run_requests VALUES (?, ?)", (user_id, time.time()))

    def run_requests_since(self, user_id: int, since: float) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM run_requests WHERE user_id = ? AND at > ?",
                              (user_id, since)).fetchone()[0]

    # --- connected repositories ---

    def add_repo(self, user_id: int, full_name: str, token: str) -> dict:
        """Connect a repo, or replace the token if this user already connected it."""
        with self._db() as db:
            db.execute("INSERT INTO repos (user_id, full_name, token_enc, token_hint, created_at) "
                       "VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, full_name) DO UPDATE SET "
                       "token_enc = excluded.token_enc, token_hint = excluded.token_hint",
                       (user_id, full_name, self.fernet.encrypt(token.encode()), token[-4:], time.time()))
            row = db.execute("SELECT id FROM repos WHERE user_id = ? AND full_name = ?",
                             (user_id, full_name)).fetchone()
        return next(r for r in self.list_repos(user_id) if r["id"] == row["id"])

    def list_repos(self, user_id: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, full_name, token_hint, created_at FROM repos WHERE user_id = ? "
                              "ORDER BY full_name", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_repo(self, user_id: int, repo_id: int) -> dict | None:
        """The repo with its decrypted token, for server-side use only."""
        with self._db() as db:
            row = db.execute("SELECT id, full_name, token_enc FROM repos WHERE user_id = ? AND id = ?",
                             (user_id, repo_id)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "full_name": row["full_name"], "token": self.fernet.decrypt(row["token_enc"]).decode()}

    def delete_repo(self, user_id: int, repo_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM repos WHERE user_id = ? AND id = ?", (user_id, repo_id)).rowcount > 0

    def token_for(self, full_name: str) -> str | None:
        """The most recently connected token for a repo, used for webhook-triggered runs."""
        with self._db() as db:
            row = db.execute("SELECT token_enc FROM repos WHERE full_name = ? ORDER BY id DESC LIMIT 1",
                             (full_name,)).fetchone()
        return self.fernet.decrypt(row["token_enc"]).decode() if row else None
