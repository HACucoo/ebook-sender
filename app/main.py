"""Web UI and a small JSON API (for the NAS Hub integration in Home Assistant)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import logging
from pathlib import Path
import time

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, goodreads
from .mailer import Mailer
from .store import (
    ERROR,
    IGNORED,
    SENT,
    TOO_LARGE,
    UNASSIGNED,
    BookStore,
    SettingsStore,
    User,
    new_id,
)
from .worker import Worker, cover_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).parent
settings_store = SettingsStore()
books = BookStore()
worker = Worker(settings_store, books)
templates = Jinja2Templates(directory=HERE / "templates")

PENDING = (UNASSIGNED, ERROR, TOO_LARGE)
STATUS_LABEL = {
    UNASSIGNED: "wartet auf Zuordnung",
    ERROR: "Fehler",
    TOO_LARGE: "zu groß",
    SENT: "verschickt",
    IGNORED: "ausgeblendet",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="ebook-sender", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _when(ts: float | None) -> str:
    if not ts:
        return "–"
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M")


templates.env.filters["when"] = _when
templates.env.globals["status_label"] = STATUS_LABEL
templates.env.globals["version"] = __version__


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


# ── pages ───────────────────────────────────────────────────────────────────


@app.get("/")
def index(request: Request):
    settings = settings_store.load()
    return templates.TemplateResponse(request, "index.html", {
        "settings": settings,
        "pending": books.list(PENDING),
        "sent": books.list((SENT,), limit=50),
        "ignored": books.list((IGNORED,), limit=20),
        "worker": worker,
    })


@app.get("/settings")
def settings_page(request: Request, saved: str | None = None, test: str | None = None):
    settings = settings_store.load()
    shelf_info = {}
    for user in settings.users:
        shelf_info[user.id] = {
            "count": len(books.shelf(user.id)),
            "error": books.meta(f"goodreads_error:{user.id}") or "",
            "at": books.meta(f"goodreads_at:{user.id}"),
        }
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings, "shelf_info": shelf_info, "saved": saved, "test": test,
    })


@app.get("/users/{user_id}")
def user_page(request: Request, user_id: str):
    settings = settings_store.load()
    user = settings.user(user_id)
    if user is None:
        raise HTTPException(404)
    sent_titles = {b["id"]: b for b in books.list((SENT,), limit=1000)}
    received = [b for b in sent_titles.values() if any(s["user"] == user_id for s in b["sent_to"])]
    shelf = books.shelf(user_id)
    for entry in shelf:
        entry["received"] = any(goodreads.matches(b, entry) for b in received)
    return templates.TemplateResponse(request, "user.html", {
        "user": user,
        "shelf": shelf,
        "received": received,
        "error": books.meta(f"goodreads_error:{user_id}") or "",
        "at": books.meta(f"goodreads_at:{user_id}"),
    })


@app.get("/covers/{book_id}")
def cover(book_id: str):
    path = cover_path(book_id) if book_id.isalnum() else None
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


# ── actions ─────────────────────────────────────────────────────────────────


@app.post("/books/{book_id}/send")
async def send_book(request: Request, book_id: str):
    form = await request.form()
    user_ids = [u for u in form.getlist("user") if isinstance(u, str)]
    if user_ids:
        await run_in_threadpool(worker.send, book_id, user_ids)
    return _back(request)


@app.post("/books/{book_id}/retry")
async def retry_book(request: Request, book_id: str):
    book = books.get(book_id)
    if book and book["matched"]:
        await run_in_threadpool(worker.send, book_id, book["matched"])
    return _back(request)


@app.post("/books/{book_id}/ignore")
def ignore_book(request: Request, book_id: str):
    books.update(book_id, status=IGNORED)
    return _back(request)


@app.post("/books/{book_id}/restore")
def restore_book(request: Request, book_id: str):
    books.update(book_id, status=UNASSIGNED, error=None)
    return _back(request)


@app.post("/books/{book_id}/forget")
def forget_book(request: Request, book_id: str):
    books.delete(book_id)
    return _back(request)


@app.post("/scan")
def scan_now(request: Request):
    worker.poke(goodreads_too=True)
    return _back(request)


@app.post("/settings")
def save_settings(
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_security: str = Form("starttls"),
    sender: str = Form(""),
    scan_minutes: int = Form(5),
    goodreads_minutes: int = Form(60),
    min_age_seconds: int = Form(120),
    max_mb: int = Form(23),
):
    s = settings_store.load()
    s.smtp_host, s.smtp_port, s.smtp_user = smtp_host.strip(), smtp_port, smtp_user.strip()
    if smtp_password:  # empty field = keep the stored password
        s.smtp_password = smtp_password
    s.smtp_security = smtp_security if smtp_security in ("starttls", "ssl", "none") else "starttls"
    s.sender = sender.strip()
    s.scan_minutes = max(1, scan_minutes)
    s.goodreads_minutes = max(10, goodreads_minutes)
    s.min_age_seconds = max(0, min_age_seconds)
    s.max_mb = max(1, max_mb)
    settings_store.save(s)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test")
async def test_mail():
    try:
        await run_in_threadpool(Mailer(settings_store.load()).test)
        result = "ok"
    except Exception as err:
        result = f"Fehler: {err}"
    return RedirectResponse(f"/settings?test={result}", status_code=303)


@app.post("/users")
def add_user(name: str = Form(...), email: str = Form(...), goodreads_url: str = Form(""), shelf: str = Form("to-read")):
    s = settings_store.load()
    s.users.append(User(id=new_id(), name=name.strip(), email=email.strip(), goodreads=goodreads_url.strip(), shelf=shelf.strip() or "to-read"))
    settings_store.save(s)
    worker.poke(goodreads_too=True)
    return RedirectResponse("/settings?saved=1#benutzer", status_code=303)


@app.post("/users/{user_id}")
def update_user(user_id: str, name: str = Form(...), email: str = Form(...), goodreads_url: str = Form(""), shelf: str = Form("to-read")):
    s = settings_store.load()
    user = s.user(user_id)
    if user is None:
        raise HTTPException(404)
    user.name, user.email = name.strip(), email.strip()
    user.goodreads, user.shelf = goodreads_url.strip(), shelf.strip() or "to-read"
    settings_store.save(s)
    worker.poke(goodreads_too=True)
    return RedirectResponse("/settings?saved=1#benutzer", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(user_id: str):
    s = settings_store.load()
    s.users = [u for u in s.users if u.id != user_id]
    settings_store.save(s)
    books.drop_shelves_except([u.id for u in s.users])
    return RedirectResponse("/settings?saved=1#benutzer", status_code=303)


# ── JSON API ────────────────────────────────────────────────────────────────


def _api_book(book: dict, settings) -> dict:
    names = {u.id: u.name for u in settings.users}
    return {
        "id": book["id"],
        "title": book["title"],
        "author": book["author"],
        "isbn": book["isbn"],
        "filename": book["filename"],
        "size": book["size"],
        "status": book["status"],
        "matched": [{"id": uid, "name": names.get(uid, uid)} for uid in book["matched"]],
        "sent_to": [{"id": s["user"], "name": s.get("name") or names.get(s["user"], s["user"]), "at": s["at"]} for s in book["sent_to"]],
        "error": book["error"],
        "found_at": book["found_at"],
        "updated_at": book["updated_at"],
        "cover": f"/covers/{book['id']}" if book["has_cover"] else None,
    }


@app.get("/api/books")
def api_books(status: str | None = None, limit: int = 30):
    settings = settings_store.load()
    statuses = tuple(status.split(",")) if status else None
    return JSONResponse({
        "books": [_api_book(b, settings) for b in books.list(statuses, limit=max(1, min(limit, 200)))],
    })


@app.get("/api/status")
def api_status():
    settings = settings_store.load()
    return {
        "version": __version__,
        "time": time.time(),
        "mail_ready": settings.mail_ready,
        "users": [{"id": u.id, "name": u.name, "shelf": len(books.shelf(u.id))} for u in settings.users],
        "pending": len(books.list(PENDING)),
        "last_scan": worker.last_scan or None,
        "last_error": worker.last_error,
    }
