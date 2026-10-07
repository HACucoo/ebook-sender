import pytest

from app import worker as worker_mod
from app.fetcher import Hydra, Release, pick
from app.store import BookStore, Settings, SettingsStore, User

HYDRA_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
<item><title>Andy Weir - Der Astronaut (German) EPUB</title><guid>g1</guid>
  <link>http://hydra/getnzb/1</link><enclosure url="http://hydra/getnzb/1" length="2000000" type="application/x-nzb"/>
  <newznab:attr name="size" value="2000000"/><newznab:attr name="grabs" value="12"/></item>
<item><title>Andy.Weir.Project.Hail.Mary.2021.RETAIL.EPUB-eBook</title><guid>g2</guid>
  <link>http://hydra/getnzb/2</link><newznab:attr name="size" value="1500000"/></item>
<item><title>Andy Weir - Project Hail Mary (Audiobook) m4b</title><guid>g3</guid>
  <link>http://hydra/getnzb/3</link><newznab:attr name="size" value="400000000"/></item>
<item><title>Andy Weir - Project Hail Mary German Deutsch epub</title><guid>g4</guid>
  <link>http://hydra/getnzb/4</link><newznab:attr name="size" value="1800000"/></item>
</channel></rss>"""


def test_parse_and_pick():
    releases = Hydra.parse(HYDRA_XML)
    assert [r.guid for r in releases] == ["g1", "g2", "g3", "g4"]
    assert releases[0].size == 2_000_000 and releases[0].grabs == 12
    # German wanted: only the German edition that names the book
    assert pick(releases, "Project Hail Mary", "Andy Weir", "de", 50, set()).guid == "g4"
    # English: the retail epub, not the German one, never the audiobook
    assert pick(releases, "Project Hail Mary", "Andy Weir", "en", 50, set()).guid == "g2"
    # A blocked release is skipped
    assert pick(releases, "Project Hail Mary", "Andy Weir", "de", 50, {"g4"}) is None
    # Wrong author
    assert pick(releases, "Project Hail Mary", "Someone Else", "any", 50, set()) is None


def test_hydra_error_answer():
    from app.fetcher import FetchError
    with pytest.raises(FetchError):
        Hydra.parse(b'<error code="100" description="Incorrect user credentials"/>')


class FakeHydra:
    calls = []

    def search(self, title, author):
        FakeHydra.calls.append(title)
        if title == "Project Hail Mary":
            return [Release(title="Andy Weir - Project Hail Mary German epub", link="http://n/1", size=1_000_000, guid="r1")]
        return []


class FakeSab:
    added = []
    state = "queued"

    def add(self, release, name):
        FakeSab.added.append((release.guid, name))
        return f"nzo_{len(FakeSab.added)}"

    def status(self, nzo_id):
        return FakeSab.state


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeHydra.calls, FakeSab.added, FakeSab.state = [], [], "queued"
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(
        smtp_host="mail", sender="nas@example.com", fetch_enabled=True,
        hydra_url="http://hydra", hydra_api_key="k", sab_url="http://sab", sab_api_key="k",
        users=[User(id="s", name="Stephan", email="s@kindle.com", goodreads="https://www.goodreads.com/user/show/1-s", language="de")],
    ))
    books = BookStore(tmp_path / "books.sqlite")
    imp = tmp_path / "import"
    imp.mkdir()
    w = worker_mod.Worker(store, books, import_dir=imp)
    monkeypatch.setattr(w, "clients", lambda settings: (FakeHydra(), FakeSab()))
    return w, books, store, monkeypatch


def shelf_fn(entries):
    return lambda user, shelf: entries


def test_only_new_entries_are_fetched(env):
    w, books, store, monkeypatch = env
    old = {"book_id": "1", "title": "Der Marsianer", "author": "Andy Weir"}
    new = {"book_id": "2", "title": "Project Hail Mary", "author": "Andy Weir"}
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([old]))
    w.refresh_goodreads(store.load())
    assert books.wanted_get("s", "1")["status"] == "baseline"

    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([old, new]))
    w.refresh_goodreads(store.load())
    assert books.wanted_get("s", "2")["status"] == "wanted"

    w.fetch(store.load())
    assert FakeHydra.calls == ["Project Hail Mary"]
    assert FakeSab.added == [("r1", "Andy Weir - Project Hail Mary")]
    entry = books.wanted_get("s", "2")
    assert entry["status"] == "grabbed" and entry["nzo_id"] == "nzo_1"
    # The old entry was never searched
    assert books.wanted_get("s", "1")["last_search"] is None


def test_failed_download_is_blocked_and_searched_again(env):
    w, books, store, monkeypatch = env
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([]))
    w.refresh_goodreads(store.load())  # first sync: empty baseline
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([{"book_id": "2", "title": "Project Hail Mary", "author": "Andy Weir"}]))
    w.refresh_goodreads(store.load())
    w.fetch(store.load())
    FakeSab.state = "failed"
    w.fetch(store.load())
    entry = books.wanted_get("s", "2")
    # Failed → blocked, back to wanted; the only release is blocked now, so nothing found
    assert entry["blocked"] == ["Andy Weir - Project Hail Mary German epub"]
    assert entry["status"] == "wanted" and "Nichts Passendes" in entry["error"]


def test_not_found_waits_for_retry(env):
    w, books, store, monkeypatch = env
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([]))
    w.refresh_goodreads(store.load())
    monkeypatch.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([{"book_id": "9", "title": "Unfindable", "author": "Nobody"}]))
    w.refresh_goodreads(store.load())
    w.fetch(store.load())
    w.fetch(store.load())
    assert FakeHydra.calls == ["Unfindable"]  # second run is within the retry window
    assert books.wanted_get("s", "9")["attempts"] == 1


def test_sent_book_marks_entry_done(env, epub_factory, monkeypatch):
    w, books, store, mp = env

    class FakeMailer:
        def __init__(self, settings): pass
        def send(self, *a): pass
        def close(self): pass

    mp.setattr(worker_mod, "Mailer", FakeMailer)
    mp.setattr(worker_mod, "COVER_DIR", w.import_dir.parent / "covers")
    mp.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn([]))
    w.refresh_goodreads(store.load())
    entries = [{"book_id": "2", "title": "Project Hail Mary", "author": "Andy Weir"}]
    mp.setattr(worker_mod.goodreads, "fetch_shelf", shelf_fn(entries))
    w.refresh_goodreads(store.load())
    w.fetch(store.load())
    # SABnzbd finished: the book lands in a job folder with leftovers
    job = w.import_dir / "Andy Weir - Project Hail Mary"
    epub_factory(job / "phm.epub", "Project Hail Mary", "Andy Weir")
    (job / "release.nfo").write_text("nfo")
    w.scan()
    assert books.wanted_get("s", "2")["status"] == "done"
    assert not job.exists()
