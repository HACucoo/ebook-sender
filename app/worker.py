"""The background loop: scan the import folder, refresh Goodreads, send.

A book goes to every user whose Goodreads shelf holds it. A book on nobody's
shelf waits as "unassigned" until someone picks a recipient in the UI — or
until it turns up on a shelf at the next Goodreads refresh.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import shutil
import threading
import time

from . import goodreads
from .epub import read_epub
from .mailer import Mailer
from .store import (
    DATA_DIR,
    ERROR,
    IGNORED,
    IMPORT_DIR,
    SENT,
    TOO_LARGE,
    UNASSIGNED,
    BookStore,
    Settings,
    SettingsStore,
)

_LOGGER = logging.getLogger("ebook-sender")

SENT_DIR = "versandt"
ERROR_RETRY_SECONDS = 30 * 60
COVER_DIR = DATA_DIR / "covers"
COVER_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cover_path(book_id: str) -> Path | None:
    for ext in COVER_EXT.values():
        p = COVER_DIR / f"{book_id}{ext}"
        if p.exists():
            return p
    return None


class Worker:
    def __init__(self, settings: SettingsStore, books: BookStore, import_dir: Path = IMPORT_DIR) -> None:
        self.settings_store = settings
        self.books = books
        self.import_dir = import_dir
        # Only one sender at a time: the loop and a click in the UI
        self.send_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_scan = 0.0
        self.last_goodreads = 0.0
        self.last_error: str | None = None

    # ── loop ──

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self, goodreads_too: bool = False) -> None:
        """Run a scan right away (button in the UI)."""
        self.last_scan = 0.0
        if goodreads_too:
            self.last_goodreads = 0.0
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            settings = self.settings_store.load()
            now = time.time()
            try:
                if now - self.last_goodreads >= settings.goodreads_minutes * 60:
                    self.last_goodreads = now
                    self.refresh_goodreads(settings)
                if now - self.last_scan >= settings.scan_minutes * 60:
                    self.last_scan = now
                    self.scan(settings)
                self.last_error = None
            except Exception as err:  # keep the loop alive whatever happens
                _LOGGER.exception("Worker run failed")
                self.last_error = str(err)
            self._wake.wait(15)
            self._wake.clear()

    # ── Goodreads ──

    def refresh_goodreads(self, settings: Settings) -> None:
        self.books.drop_shelves_except([u.id for u in settings.users])
        for user in settings.users:
            if not user.goodreads:
                self.books.set_shelf(user.id, [])
                continue
            try:
                entries = goodreads.fetch_shelf(user.goodreads, user.shelf)
            except goodreads.GoodreadsError as err:
                self.books.set_meta(f"goodreads_error:{user.id}", str(err))
                _LOGGER.warning("Goodreads for %s: %s", user.name, err)
                continue
            self.books.set_shelf(user.id, entries)
            self.books.set_meta(f"goodreads_error:{user.id}", "")
            self.books.set_meta(f"goodreads_at:{user.id}", str(time.time()))
        # Something waiting may have landed on a shelf in the meantime
        shelves = self.books.shelf()
        for book in self.books.list((UNASSIGNED,)):
            users = goodreads.match_users(book, shelves)
            if users:
                self.books.update(book["id"], matched=users)
                self.send(book["id"], users, settings)

    # ── scanning ──

    def _candidates(self, settings: Settings) -> list[Path]:
        sent_dir = self.import_dir / SENT_DIR
        now = time.time()
        files = []
        for p in self.import_dir.rglob("*"):
            if not p.is_file() or p.suffix.lower() != ".epub" or sent_dir in p.parents:
                continue
            # Still being copied? Wait until it has been quiet for a while
            if now - p.stat().st_mtime < settings.min_age_seconds:
                continue
            files.append(p)
        return sorted(files)

    def scan(self, settings: Settings | None = None) -> None:
        settings = settings or self.settings_store.load()
        if not self.import_dir.is_dir():
            raise RuntimeError(f"Ablageordner {self.import_dir} fehlt")
        seen: set[str] = set()
        shelves = self.books.shelf()
        for path in self._candidates(settings):
            book_id = sha1_of(path)
            seen.add(book_id)
            rel = str(path.relative_to(self.import_dir))
            known = self.books.get(book_id)
            if known and known["status"] != SENT:
                if known["path"] != rel:
                    self.books.update(book_id, path=rel, filename=path.name)
                if known["status"] == ERROR and time.time() - known["updated_at"] > ERROR_RETRY_SECONDS:
                    self.send(book_id, known["matched"], settings)
                continue
            # New — or sent before and dropped in again, which means "once more"
            self._add(path, rel, book_id, settings, shelves)

        # Waiting books whose file was taken away
        for book in self.books.list((UNASSIGNED, TOO_LARGE, ERROR)):
            if book["id"] not in seen and not (self.import_dir / book["path"]).exists():
                self.books.update(book["id"], status=IGNORED, error="Datei aus dem Ablageordner entfernt")

    def _add(self, path: Path, rel: str, book_id: str, settings: Settings, shelves: list[dict]) -> None:
        info = read_epub(path)
        if info.cover:
            COVER_DIR.mkdir(parents=True, exist_ok=True)
            (COVER_DIR / f"{book_id}{COVER_EXT.get(info.cover_type, '.jpg')}").write_bytes(info.cover)
        size = path.stat().st_size
        book = {
            "id": book_id,
            "path": rel,
            "filename": path.name,
            "size": size,
            "title": info.title or path.stem,
            "author": info.author,
            "isbn": info.isbn,
            "has_cover": bool(info.cover),
        }
        if size * 4 // 3 > settings.max_mb * 1024 * 1024:
            self.books.insert({**book, "status": TOO_LARGE, "error": f"{size // 1024**2} MB – zu groß für eine Mail"})
            return
        users = goodreads.match_users(book, shelves)
        self.books.insert({**book, "status": UNASSIGNED, "matched": users})
        _LOGGER.info("Found %s (%s): %s", book["title"], book["author"], users or "on no shelf")
        if users:
            self.send(book_id, users, settings)

    # ── sending ──

    def send(self, book_id: str, user_ids: list[str], settings: Settings | None = None) -> None:
        settings = settings or self.settings_store.load()
        with self.send_lock:
            book = self.books.get(book_id)
            if book is None:
                return
            path = self.import_dir / book["path"]
            if not path.exists():
                self.books.update(book_id, status=IGNORED, error="Datei nicht mehr im Ablageordner")
                return
            if not settings.mail_ready:
                self.books.update(book_id, status=ERROR, matched=user_ids, error="Mailversand ist noch nicht eingerichtet")
                return
            already = {s["user"] for s in book["sent_to"]}
            sent_to = list(book["sent_to"])
            mailer = Mailer(settings)
            try:
                for uid in user_ids:
                    user = settings.user(uid)
                    if user is None or uid in already:
                        continue
                    mailer.send(path, user.email, book["title"] or path.stem)
                    sent_to.append({"user": uid, "name": user.name, "at": time.time()})
                    _LOGGER.info("Sent %s to %s", book["title"], user.name)
            except Exception as err:
                self.books.update(book_id, status=ERROR, matched=user_ids, sent_to=sent_to, error=f"Versand fehlgeschlagen: {err}")
                _LOGGER.warning("Sending %s failed: %s", book["title"], err)
                return
            finally:
                mailer.close()
            target = self._move_to_sent(path)
            self.books.update(
                book_id,
                status=SENT,
                matched=user_ids,
                sent_to=sent_to,
                error=None,
                path=str(target.relative_to(self.import_dir)),
            )

    def _move_to_sent(self, path: Path) -> Path:
        sent_dir = self.import_dir / SENT_DIR
        sent_dir.mkdir(parents=True, exist_ok=True)
        target = sent_dir / path.name
        if target.exists():
            target = sent_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(target))
        # An emptied job folder from the PC is removed, like the old script did
        parent = path.parent
        if parent != self.import_dir and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return target
