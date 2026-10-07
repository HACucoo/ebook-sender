from app import goodreads
from app.epub import read_epub

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Iron Flame (The Empyrean, #2)</title><book_id>90202302</book_id><author_name>Rebecca Yarros</author_name>
<isbn>1649374178</isbn><book_large_image_url>https://img/x.jpg</book_large_image_url><user_date_added>Mon, 01 Sep 2026</user_date_added></item>
<item><title>Project Hail Mary</title><book_id>54493401</book_id><author_name>Andy Weir</author_name><isbn></isbn></item>
</channel></rss>"""


def test_user_id_from_urls():
    assert goodreads.user_id("https://www.goodreads.com/user/show/12345-stephan") == "12345"
    assert goodreads.user_id("https://www.goodreads.com/review/list/777?shelf=to-read") == "777"
    assert goodreads.user_id("4711") == "4711"
    assert goodreads.user_id("https://example.com") is None
    assert goodreads.shelf_from_url("https://www.goodreads.com/review/list/777?shelf=kindle") == "kindle"
    assert goodreads.shelf_from_url("https://www.goodreads.com/review/list/777-name?tag=to-grab") == "to-grab"
    assert goodreads.user_id("https://www.goodreads.com/review/list/777-name?tag=to-grab") == "777"


def test_parse_feed():
    entries = goodreads.parse_feed(RSS)
    assert [e["title"] for e in entries] == ["Iron Flame (The Empyrean, #2)", "Project Hail Mary"]
    assert entries[0]["author"] == "Rebecca Yarros"
    assert entries[0]["isbn"] == "1649374178"
    assert entries[1]["isbn"] is None


def test_matching():
    shelf = [{**e, "user_id": "u1"} for e in goodreads.parse_feed(RSS)]
    # German edition: other subtitle, umlaut-free core title still fits
    assert goodreads.match_users({"title": "Iron Flame - Flammengeküsst", "author": "Yarros, Rebecca"}, shelf) == ["u1"]
    assert goodreads.match_users({"title": "Der Marsianer", "author": "Andy Weir"}, shelf) == []
    assert goodreads.match_users({"title": "Project Hail Mary", "author": None}, shelf) == ["u1"]
    # Same title, other author: no match
    assert goodreads.match_users({"title": "Project Hail Mary", "author": "Someone Else"}, shelf) == []
    # ISBN beats a differing title
    assert goodreads.match_users({"title": "Ganz anders", "isbn": "1649374178"}, shelf) == ["u1"]


def test_normalize():
    assert goodreads.normalize("Die Känguru-Chroniken") == "kanguru chroniken"
    assert goodreads.core_title("Iron Flame (Flammengeküsst 2)") == "iron flame"


def test_read_epub(tmp_path, epub_factory):
    for epub3 in (True, False):
        p = epub_factory(tmp_path / f"b{epub3}.epub", "Project Hail Mary", "Andy Weir", isbn="978-3-453-27270-4", epub3=epub3)
        info = read_epub(p)
        assert (info.title, info.author, info.isbn) == ("Project Hail Mary", "Andy Weir", "9783453272704")
        assert info.cover.startswith(b"\xff\xd8")
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    assert read_epub(broken).title is None
