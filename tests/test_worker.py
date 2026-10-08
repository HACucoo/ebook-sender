import pytest

from app import worker as worker_mod
from app.store import ERROR, IGNORED, SENT, TOO_LARGE, UNASSIGNED, BookStore, Settings, SettingsStore, User


class FakeMailer:
    sent: list = []
    fail = False

    def __init__(self, settings):
        pass

    def send(self, path, to, subject):
        if FakeMailer.fail:
            raise OSError("smtp down")
        FakeMailer.sent.append((path.name, to, subject))

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeMailer.sent = []
    FakeMailer.fail = False
    monkeypatch.setattr(worker_mod, "Mailer", FakeMailer)
    monkeypatch.setattr(worker_mod, "COVER_DIR", tmp_path / "covers")
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(
        smtp_host="mail", sender="nas@example.com", max_mb=1,
        users=[User(id="s", name="Stephan", email="s@kindle.com"), User(id="d", name="Daniela", email="d@kindle.com")],
    ))
    books = BookStore(tmp_path / "books.sqlite")
    books.set_shelf("s", [{"book_id": "1", "title": "Project Hail Mary", "author": "Andy Weir"}])
    books.set_shelf("d", [{"book_id": "1", "title": "Project Hail Mary", "author": "Andy Weir"},
                          {"book_id": "2", "title": "Iron Flame", "author": "Rebecca Yarros"}])
    imp = tmp_path / "import"
    imp.mkdir()
    return worker_mod.Worker(store, books, import_dir=imp), books, imp


def test_routes_by_shelf_and_moves(env, epub_factory):
    w, books, imp = env
    epub_factory(imp / "job1" / "phm.epub", "Project Hail Mary", "Andy Weir")
    epub_factory(imp / "iron.epub", "Iron Flame (Flammengeküsst 2)", "Rebecca Yarros")
    w.scan()
    assert sorted(FakeMailer.sent) == sorted([
        ("phm.epub", "s@kindle.com", "Project Hail Mary"),
        ("phm.epub", "d@kindle.com", "Project Hail Mary"),
        ("iron.epub", "d@kindle.com", "Iron Flame (Flammengeküsst 2)"),
    ])
    assert (imp / "versandt" / "phm.epub").exists()
    assert not (imp / "job1").exists()  # emptied job folder removed
    sent = books.list((SENT,))
    assert {b["title"] for b in sent} == {"Project Hail Mary", "Iron Flame (Flammengeküsst 2)"}
    # A second scan sends nothing again
    w.scan()
    assert len(FakeMailer.sent) == 3


def test_sent_to_records_since_when_on_shelf(env, epub_factory):
    w, books, imp = env
    books.set_shelf("s", [{"book_id": "1", "title": "Project Hail Mary", "author": "Andy Weir",
                           "added_at": "Mon, 21 Sep 2026 10:00:00 -0700", "image": "https://gr/img.jpg"}])
    epub_factory(imp / "phm.epub", "Project Hail Mary", "Andy Weir")
    w.scan()
    [book] = books.list((SENT,))
    by_user = {s["user"]: s for s in book["sent_to"]}
    assert by_user["s"]["listed_at"] == 1790010000.0  # 2026-09-21 17:00 UTC
    assert by_user["s"]["image"] == "https://gr/img.jpg"
    # Daniela's shelf entry has no date and nothing in "wanted": unknown, not "now"
    assert by_user["d"]["listed_at"] is None


def test_unassigned_waits_then_manual_send(env, epub_factory):
    w, books, imp = env
    epub_factory(imp / "x.epub", "Unbekanntes Buch", "Niemand")
    w.scan()
    [book] = books.list((UNASSIGNED,))
    assert FakeMailer.sent == [] and (imp / "x.epub").exists()
    w.send(book["id"], ["s"])
    assert FakeMailer.sent == [("x.epub", "s@kindle.com", "Unbekanntes Buch")]
    assert books.get(book["id"])["status"] == SENT
    assert books.get(book["id"])["sent_to"][0]["name"] == "Stephan"


def test_unassigned_sent_when_it_lands_on_a_shelf(env, epub_factory, monkeypatch):
    w, books, imp = env
    epub_factory(imp / "y.epub", "Der Marsianer", "Andy Weir")
    w.scan()
    assert books.list((UNASSIGNED,))
    settings = w.settings_store.load()
    settings.users[0].goodreads = "https://www.goodreads.com/user/show/123-s"
    w.settings_store.save(settings)
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf",
                        lambda user, shelf: [{"book_id": "3", "title": "Der Marsianer", "author": "Andy Weir"}])
    w.refresh_goodreads(settings)
    assert FakeMailer.sent == [("y.epub", "s@kindle.com", "Der Marsianer")]
    # Daniela has no Goodreads link, so her shelf is empty now
    assert books.shelf("d") == []


def test_too_large_and_error_and_vanished(env, epub_factory):
    w, books, imp = env
    big = epub_factory(imp / "big.epub", "Project Hail Mary", "Andy Weir")
    with big.open("ab") as fh:
        fh.write(b"\0" * (1024 * 1024))
    import os
    os.utime(big, (0, 0))
    w.scan()
    assert books.list((TOO_LARGE,))[0]["filename"] == "big.epub"
    assert FakeMailer.sent == []

    FakeMailer.fail = True
    epub_factory(imp / "iron.epub", "Iron Flame", "Rebecca Yarros")
    w.scan()
    [err] = books.list((ERROR,))
    assert "smtp down" in err["error"] and (imp / "iron.epub").exists()

    (imp / "big.epub").unlink()
    w.scan()
    assert books.list((IGNORED,))[0]["filename"] == "big.epub"
