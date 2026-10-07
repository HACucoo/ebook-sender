"""Settings (a JSON file) and the book list (SQLite), both under the data directory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IMPORT_DIR = Path(os.environ.get("IMPORT_DIR", "/import"))

# Book states
WAITING = "waiting"        # just seen, not yet quiet long enough
UNASSIGNED = "unassigned"  # on nobody's Goodreads list — someone has to pick
SENT = "sent"
TOO_LARGE = "too_large"
ERROR = "error"
IGNORED = "ignored"        # dismissed in the UI, file stays where it is


@dataclass
class User:
    id: str
    name: str
    email: str
    goodreads: str = ""      # profile or shelf URL, or the numeric user id
    shelf: str = "to-read"


@dataclass
class Settings:
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: str = "starttls"   # starttls | ssl | none
    sender: str = ""
    scan_minutes: int = 5
    goodreads_minutes: int = 60
    min_age_seconds: int = 120
    max_mb: int = 23
    users: list[User] = field(default_factory=list)

    @property
    def mail_ready(self) -> bool:
        return bool(self.smtp_host and self.sender)

    def user(self, user_id: str) -> User | None:
        return next((u for u in self.users if u.id == user_id), None)


def new_id() -> str:
    return secrets.token_hex(4)


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_DIR / "settings.json"
        self._lock = threading.Lock()

    def load(self) -> Settings:
        with self._lock:
            if not self.path.exists():
                return Settings()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        users = [User(**u) for u in raw.pop("users", [])]
        known = Settings.__dataclass_fields__
        return Settings(users=users, **{k: v for k, v in raw.items() if k in known})

    def save(self, settings: Settings) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
            # The SMTP password is in here
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          TEXT PRIMARY KEY,      -- sha1 of the file content
    path        TEXT NOT NULL,         -- relative to the import directory
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    title       TEXT,
    author      TEXT,
    isbn        TEXT,
    has_cover   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    matched     TEXT NOT NULL DEFAULT '[]',   -- user ids from the Goodreads match
    sent_to     TEXT NOT NULL DEFAULT '[]',   -- [{"user": id, "name": ..., "at": ts}]
    error       TEXT,
    found_at    REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS books_updated ON books(updated_at);
CREATE TABLE IF NOT EXISTS shelf (
    user_id     TEXT NOT NULL,
    book_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    author      TEXT,
    isbn        TEXT,
    image       TEXT,
    added_at    TEXT,
    PRIMARY KEY (user_id, book_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_COLUMNS = ("matched", "sent_to")


class BookStore:
    """One connection, guarded by a lock: the web server and the worker share it."""

    def __init__(self, path: Path | None = None) -> None:
        path = path or DATA_DIR / "books.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        book = dict(row)
        for col in JSON_COLUMNS:
            book[col] = json.loads(book[col] or "[]")
        book["has_cover"] = bool(book["has_cover"])
        return book

    def get(self, book_id: str) -> dict | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone())

    def list(self, statuses: tuple[str, ...] | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM books"
        args: list[Any] = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, args).fetchall()]

    def insert(self, book: dict) -> None:
        now = time.time()
        book = {"found_at": now, "updated_at": now, "matched": [], "sent_to": [], "error": None, **book}
        for col in JSON_COLUMNS:
            book[col] = json.dumps(book[col], ensure_ascii=False)
        book["has_cover"] = int(bool(book.get("has_cover")))
        cols = ", ".join(book)
        with self._lock, self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO books ({cols}) VALUES ({', '.join('?' * len(book))})",
                list(book.values()),
            )

    def update(self, book_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        for col in JSON_COLUMNS:
            if col in fields:
                fields[col] = json.dumps(fields[col], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE books SET {sets} WHERE id = ?", [*fields.values(), book_id])

    def delete(self, book_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM books WHERE id = ?", (book_id,))

    # ── Goodreads shelves ──

    def set_shelf(self, user_id: str, entries: list[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM shelf WHERE user_id = ?", (user_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO shelf (user_id, book_id, title, author, isbn, image, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(user_id, e["book_id"], e["title"], e.get("author"), e.get("isbn"), e.get("image"), e.get("added_at"))
                 for e in entries],
            )

    def shelf(self, user_id: str | None = None) -> list[dict]:
        with self._lock:
            if user_id is None:
                rows = self._db.execute("SELECT * FROM shelf").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM shelf WHERE user_id = ? ORDER BY added_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def drop_shelves_except(self, user_ids: list[str]) -> None:
        with self._lock, self._db:
            if user_ids:
                self._db.execute(f"DELETE FROM shelf WHERE user_id NOT IN ({','.join('?' * len(user_ids))})", user_ids)
            else:
                self._db.execute("DELETE FROM shelf")

    # ── small key/value ──

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
