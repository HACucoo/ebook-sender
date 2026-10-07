"""Goodreads shelves via their public RSS feed, and matching books against them.

Goodreads has no API any more, but every public shelf still has a feed:
https://www.goodreads.com/review/list_rss/<user id>?shelf=<shelf>
The profile must be visible to everyone for this to work.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEED = "https://www.goodreads.com/review/list_rss/{user}?shelf={shelf}&per_page=200&page={page}"
USER_AGENT = "ebook-sender/1.0 (+https://github.com/HACucoo/ebook-sender)"
MAX_PAGES = 10


class GoodreadsError(Exception):
    pass


def user_id(value: str) -> str | None:
    """The numeric id from a profile/shelf URL, or the id itself."""
    value = (value or "").strip()
    if value.isdigit():
        return value
    m = re.search(r"/(?:user/show|review/list(?:_rss)?)/(\d+)", value)
    return m.group(1) if m else None


def shelf_from_url(value: str) -> str | None:
    query = urllib.parse.urlparse(value or "").query
    return (urllib.parse.parse_qs(query).get("shelf") or [None])[0]


def parse_feed(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    entries = []
    for item in root.iter("item"):
        def text(tag: str) -> str:
            return (item.findtext(tag) or "").strip()
        title = text("title")
        if not title:
            continue
        entries.append({
            "book_id": text("book_id") or text("guid") or title,
            "title": title,
            "author": text("author_name") or None,
            "isbn": (text("isbn") or None),
            "image": text("book_large_image_url") or text("book_image_url") or None,
            "added_at": text("user_date_added") or text("pubDate") or None,
        })
    return entries


def fetch_shelf(user: str, shelf: str = "to-read", timeout: int = 20) -> list[dict]:
    uid = user_id(user)
    if not uid:
        raise GoodreadsError(f"Keine Goodreads-Benutzer-ID in „{user}“ gefunden")
    shelf = shelf_from_url(user) or shelf or "to-read"
    entries: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = FEED.format(user=uid, shelf=urllib.parse.quote(shelf), page=page)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except OSError as err:
            raise GoodreadsError(f"Goodreads nicht erreichbar: {err}") from err
        try:
            batch = parse_feed(body)
        except ET.ParseError as err:
            # A private profile answers with an HTML sign-in page instead of RSS
            raise GoodreadsError("Antwort ist kein RSS – ist das Profil öffentlich?") from err
        entries.extend(batch)
        if len(batch) < 100:
            break
    return entries


# ── matching ────────────────────────────────────────────────────────────────

_STOP = {"the", "a", "an", "der", "die", "das", "ein", "eine", "le", "la", "les"}


def normalize(text: str | None) -> str:
    """Lower case, umlauts folded, punctuation gone, leading article dropped."""
    if not text:
        return ""
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    if words and words[0] in _STOP and len(words) > 1:
        words = words[1:]
    return " ".join(words)


def core_title(title: str | None) -> str:
    """Without subtitle and series: 'Iron Flame (Flammengeküsst 2)' → 'iron flame'."""
    if not title:
        return ""
    title = re.split(r"\s*[:(\[]|\s+[-–—]\s+", title, maxsplit=1)[0]
    return normalize(title)


def surname(author: str | None) -> str:
    if not author:
        return ""
    author = author.split(",")[0] if "," in author else author.split()[-1]
    return normalize(author)


def matches(book: dict, entry: dict) -> bool:
    """ISBN wins; otherwise the core title plus the author's surname must fit."""
    if book.get("isbn") and entry.get("isbn") and book["isbn"] == entry["isbn"]:
        return True
    a, b = core_title(book.get("title")), core_title(entry.get("title"))
    if not a or not b:
        return False
    title_fits = a == b or (len(a) >= 6 and len(b) >= 6 and (a in b or b in a))
    if not title_fits:
        return False
    sa, sb = surname(book.get("author")), surname(entry.get("author"))
    if sa and sb:
        return sa == sb
    # A book without an author in its metadata may still match on an exact title
    return a == b


def match_users(book: dict, shelves: list[dict]) -> list[str]:
    """User ids whose shelf holds this book."""
    return sorted({e["user_id"] for e in shelves if matches(book, e)})
