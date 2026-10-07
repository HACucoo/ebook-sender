import os
from pathlib import Path
import sys
import tempfile
import zipfile

import pytest

# The app reads its directories at import time
_TMP = Path(tempfile.mkdtemp(prefix="ebook-versand-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["IMPORT_DIR"] = str(_TMP / "import")
(_TMP / "import").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_epub(path: Path, title: str, author: str | None = None, isbn: str | None = None, epub3: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    creator = f"<dc:creator>{author}</dc:creator>" if author else ""
    ident = f'<dc:identifier opf:scheme="ISBN" xmlns:opf="http://www.idpf.org/2007/opf">{isbn}</dc:identifier>' if isbn else ""
    if epub3:
        cover_item = '<item id="c" href="img/cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
        cover_meta = ""
    else:
        cover_item = '<item id="c" href="img/cover.jpg" media-type="image/jpeg"/>'
        cover_meta = '<meta name="cover" content="c"/>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{title}</dc:title>{creator}{ident}{cover_meta}</metadata>"
                   f"<manifest>{cover_item}</manifest></package>")
        z.writestr("OEBPS/img/cover.jpg", b"\xff\xd8 fake jpeg " + title.encode())
    # Old enough to count as finished
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))
    return path


@pytest.fixture
def epub_factory():
    return make_epub
