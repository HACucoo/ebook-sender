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
    language: str = "any"    # de | en | any — which edition to fetch
    fetch: bool = True       # fetch new shelf entries via NZBHydra2/SABnzbd


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
    # Fetching books from the shelves
    fetch_enabled: bool = False
    hydra_url: str = ""
    hydra_api_key: str = ""
    sab_url: str = ""
    sab_api_key: str = ""
    sab_category: str = "ebooks"
    fetch_retry_hours: int = 12
    fetch_max_mb: int = 50
    users: list[User] = field(default_factory=list)

    @property
    def mail_ready(self) -> bool:
        return bool(self.smtp_host and self.sender)

    @property
    def fetch_ready(self) -> bool:
        return bool(self.fetch_enabled and self.hydra_url and self.hydra_api_key and self.sab_url and self.sab_api_key)

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
-- Shelf entries and how far getting them has come:
-- baseline (was on the shelf before fetching started) | wanted | grabbed | done | skipped
CREATE TABLE IF NOT EXISTS wanted (
    user_id     TEXT NOT NULL,
    book_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    author      TEXT,
    isbn        TEXT,
    image       TEXT,
    status      TEXT NOT NULL,
    release     TEXT,                  -- name of the grabbed release
    nzo_id      TEXT,                  -- SABnzbd job
    blocked     TEXT NOT NULL DEFAULT '[]',  -- releases that failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_search REAL,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
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

    # ── wanted (fetching) ──

    def _wanted_row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["blocked"] = json.loads(item["blocked"] or "[]")
        return item

    def sync_wanted(self, user_id: str, entries: list[dict], new_status: str) -> int:
        """Add shelf entries not seen before with `new_status`; forget entries
        that left the shelf unless something already happened to them.
        Returns how many were added."""
        now = time.time()
        ids = [e["book_id"] for e in entries]
        added = 0
        with self._lock, self._db:
            known = {r["book_id"] for r in self._db.execute("SELECT book_id FROM wanted WHERE user_id = ?", (user_id,))}
            for e in entries:
                if e["book_id"] in known:
                    continue
                self._db.execute(
                    "INSERT INTO wanted (user_id, book_id, title, author, isbn, image, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, e["book_id"], e["title"], e.get("author"), e.get("isbn"), e.get("image"), new_status, now, now),
                )
                added += 1
            if ids:
                self._db.execute(
                    f"DELETE FROM wanted WHERE user_id = ? AND status IN ('baseline', 'wanted', 'skipped')"
                    f" AND book_id NOT IN ({','.join('?' * len(ids))})",
                    [user_id, *ids],
                )
            else:
                self._db.execute("DELETE FROM wanted WHERE user_id = ? AND status IN ('baseline', 'wanted', 'skipped')", (user_id,))
        return added

    def wanted(self, user_id: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM wanted WHERE 1=1", []
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            return [self._wanted_row(r) for r in self._db.execute(sql, args).fetchall()]

    def wanted_get(self, user_id: str, book_id: str) -> dict | None:
        with self._lock:
            return self._wanted_row(self._db.execute(
                "SELECT * FROM wanted WHERE user_id = ? AND book_id = ?", (user_id, book_id)).fetchone())

    def update_wanted(self, user_id: str, book_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        if "blocked" in fields:
            fields["blocked"] = json.dumps(fields["blocked"], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE wanted SET {sets} WHERE user_id = ? AND book_id = ?", [*fields.values(), user_id, book_id])

    def drop_wanted_except(self, user_ids: list[str]) -> None:
        with self._lock, self._db:
            if user_ids:
                self._db.execute(f"DELETE FROM wanted WHERE user_id NOT IN ({','.join('?' * len(user_ids))})", user_ids)
            else:
                self._db.execute("DELETE FROM wanted")

    # ── small key/value ──

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
