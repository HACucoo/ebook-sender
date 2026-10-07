"""Title, author, ISBN and cover straight from the EPUB's package document."""
from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
MAX_COVER = 2 * 1024 * 1024


@dataclass
class EpubInfo:
    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    cover: bytes | None = None
    cover_type: str | None = None


def _isbn(text: str) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", text)
    return digits.upper() if len(digits) in (10, 13) else None


def read_epub(path) -> EpubInfo:
    """Whatever cannot be read stays None; a broken file is not an error here."""
    info = EpubInfo()
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(".//c:rootfile", NS).get("full-path")
            opf = ET.fromstring(z.read(opf_path))
            info.title = (opf.findtext(".//dc:title", default="", namespaces=NS).strip() or None)
            info.author = (opf.findtext(".//dc:creator", default="", namespaces=NS).strip() or None)
            for ident in opf.findall(".//dc:identifier", NS):
                text = ident.text or ""
                scheme = " ".join(ident.attrib.values()).lower()
                isbn = _isbn(text)
                # A bare 13-digit number only counts when it looks like an ISBN
                if isbn and ("isbn" in scheme or "isbn" in text.lower() or isbn.startswith(("978", "979"))):
                    info.isbn = isbn
                    break

            # EPUB 3: properties="cover-image"; EPUB 2: <meta name="cover" content="id">
            items = opf.findall(".//opf:manifest/opf:item", NS)
            cover = next((i for i in items if "cover-image" in (i.get("properties") or "").split()), None)
            if cover is None:
                meta = opf.find(".//opf:metadata/opf:meta[@name='cover']", NS)
                if meta is not None:
                    cover = next((i for i in items if i.get("id") == meta.get("content")), None)
            if cover is not None and (cover.get("media-type") or "").startswith("image/"):
                href = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), cover.get("href")))
                data = z.read(href)
                if len(data) <= MAX_COVER:
                    info.cover = data
                    info.cover_type = cover.get("media-type")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, AttributeError, OSError):
        pass
    return info
