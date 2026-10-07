"""Getting books from a Goodreads shelf: search NZBHydra2, hand the NZB to SABnzbd.

SABnzbd's e-book category saves into the drop folder, so a finished download
is picked up by the normal scan and mailed like any other book.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import goodreads

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
EBOOK_CATEGORY = "7020"  # newznab: Books > EBook
USER_AGENT = "ebook-sender/1.1"
TIMEOUT = 30

GERMAN = re.compile(r"\b(german|deutsch|ger|de|dt)\b", re.I)
ENGLISH = re.compile(r"\b(english|eng|en)\b", re.I)
# Never an e-book for a reader
REJECT = re.compile(r"\b(m4b|mp3|aac|flac|audio ?book|h[oö]rbuch|cbr|cbz|comic)\b", re.I)
GOOD_FORMAT = re.compile(r"\bepub\b", re.I)
OTHER_FORMAT = re.compile(r"\b(pdf|mobi|azw3?|djvu|lit)\b", re.I)


class FetchError(Exception):
    pass


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except OSError as err:
        # Never let an API key from the URL end up in the UI or the log
        raise FetchError(re.sub(r"apikey=[^&\s]+", "apikey=…", str(err))) from err


@dataclass
class Release:
    title: str
    link: str
    size: int
    guid: str
    pubdate: str = ""
    grabs: int = 0
    score: int = 0


class Hydra:
    """NZBHydra2 speaks the newznab API."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _api(self, **params: str) -> bytes:
        query = urllib.parse.urlencode({**params, "apikey": self.api_key})
        return _get(f"{self.url}/api?{query}")

    def test(self) -> None:
        body = self._api(t="caps")
        if b"<caps" not in body:
            raise FetchError("Antwort ist keine newznab-caps – stimmt die Adresse?")

    @staticmethod
    def parse(body: bytes) -> list[Release]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as err:
            raise FetchError("Antwort ist kein newznab-XML") from err
        error = root if root.tag == "error" else root.find("error")
        if error is not None:
            raise FetchError(f"NZBHydra2: {error.get('description') or error.get('code')}")
        releases = []
        for item in root.iter("item"):
            attrs = {a.get("name"): a.get("value") for a in item.iter(f"{NEWZNAB_NS}attr")}
            enclosure = item.find("enclosure")
            link = (enclosure.get("url") if enclosure is not None else None) or item.findtext("link") or ""
            size = attrs.get("size") or (enclosure.get("length") if enclosure is not None else 0) or 0
            releases.append(Release(
                title=(item.findtext("title") or "").strip(),
                link=link,
                size=int(size or 0),
                guid=item.findtext("guid") or link,
                pubdate=item.findtext("pubDate") or "",
                grabs=int(attrs.get("grabs") or 0),
            ))
        return releases

    def search(self, title: str, author: str | None) -> list[Release]:
        """Book search first, then a free-text search; results merged by guid."""
        found: dict[str, Release] = {}
        queries = []
        if author:
            queries.append({"t": "book", "title": goodreads.core_title_raw(title), "author": author})
        queries.append({"t": "search", "q": " ".join(x for x in (author_surname_raw(author), goodreads.core_title_raw(title)) if x)})
        for params in queries:
            try:
                for rel in self.parse(self._api(cat=EBOOK_CATEGORY, limit="100", **params)):
                    found.setdefault(rel.guid, rel)
            except FetchError:
                if params["t"] == "book":
                    continue  # not every indexer knows t=book; the free search follows
                raise
        return list(found.values())


def author_surname_raw(author: str | None) -> str:
    if not author:
        return ""
    return author.split(",")[0].strip() if "," in author else author.split()[-1]


def pick(releases: list[Release], title: str, author: str | None, language: str,
         max_mb: int, blocked: set[str]) -> Release | None:
    """The best fitting release, or None.

    A release must name the book (core title words) and, if known, the author's
    surname; audiobooks and comics are out; the language rule is strict for
    "de"/"en" — better to wait for the right edition than to send the wrong one.
    """
    want_title = goodreads.core_title(title).split()
    want_author = goodreads.surname(author)
    best: Release | None = None
    for rel in releases:
        if rel.guid in blocked or rel.title in blocked:
            continue
        name = goodreads.normalize(rel.title)
        words = set(name.split())
        if not want_title or sum(w in words for w in want_title) < max(1, round(len(want_title) * 0.75)):
            continue
        if want_author and want_author not in words:
            continue
        if REJECT.search(rel.title):
            continue
        if rel.size and (rel.size > max_mb * 1024 * 1024 or rel.size < 20 * 1024):
            continue
        german, english = bool(GERMAN.search(rel.title)), bool(ENGLISH.search(rel.title))
        if language == "de" and not german:
            continue
        if language == "en" and german and not english:
            continue
        score = 0
        if GOOD_FORMAT.search(rel.title):
            score += 4
        elif OTHER_FORMAT.search(rel.title):
            score -= 3
        if language == "en" and english:
            score += 1
        score += min(rel.grabs, 50) // 10
        rel.score = score
        if best is None or score > best.score:
            best = rel
    return best


class Sabnzbd:
    def __init__(self, url: str, api_key: str, category: str) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.category = category

    def _api(self, **params: str) -> dict:
        query = urllib.parse.urlencode({**params, "apikey": self.api_key, "output": "json"})
        body = _get(f"{self.url}/api?{query}")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise FetchError("SABnzbd antwortet nicht mit JSON – stimmt die Adresse?") from err
        if isinstance(data, dict) and data.get("status") is False:
            raise FetchError(f"SABnzbd: {data.get('error') or 'abgelehnt'}")
        return data

    def test(self) -> list[str]:
        """The categories SABnzbd knows — the configured one should be among them."""
        return list(self._api(mode="get_cats").get("categories", []))

    def add(self, release: Release, name: str) -> str | None:
        data = self._api(mode="addurl", name=release.link, nzbname=name, cat=self.category)
        ids = data.get("nzo_ids") or []
        return ids[0] if ids else None

    def status(self, nzo_id: str) -> str | None:
        """queued | completed | failed | None (unknown, e.g. history cleared)."""
        queue = self._api(mode="queue", nzo_ids=nzo_id).get("queue", {})
        if any(slot.get("nzo_id") == nzo_id for slot in queue.get("slots", [])):
            return "queued"
        history = self._api(mode="history", nzo_ids=nzo_id).get("history", {})
        for slot in history.get("slots", []):
            if slot.get("nzo_id") == nzo_id:
                status = (slot.get("status") or "").lower()
                if status == "completed":
                    return "completed"
                if status == "failed":
                    return "failed"
                return "queued"  # extracting, verifying, moving …
        return None
