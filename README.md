# ebook-sender

Drop EPUBs into a folder, they arrive on the right e-reader.

Every book that lands in the drop folder is matched against the Goodreads "want to read" shelf of each user and mailed to everyone whose shelf holds it (Kindle, Tolino, PocketBook — anything that takes books by e-mail). A book on nobody's shelf waits in the web UI until someone ticks a recipient; if it shows up on a shelf later, it goes out at the next Goodreads refresh. Sent books move to `versandt/` inside the drop folder.

- Web UI for users, mail settings and the list of found, waiting and sent books
- One Goodreads shelf per user, read from its public RSS feed — no Goodreads API key
- Matching by ISBN, or by title (subtitle and series stripped, umlauts folded) plus the author's surname
- Cover, title, author and ISBN read straight from the EPUB
- Optional fetching: new books on a shelf are searched via NZBHydra2 and handed to SABnzbd, per user in German, English or any language
- JSON API for dashboards, e.g. the [NAS Hub](https://github.com/HACucoo/nas-hub-ha) integration for Home Assistant

## Run

```yaml
services:
  ebook-sender:
    image: ghcr.io/hacucoo/ebook-sender:latest
    restart: unless-stopped
    user: "1000:1000"          # owner of the files in the drop folder
    ports:
      - 8095:8080
    volumes:
      - ./data:/data            # settings.json, books.sqlite, covers
      - /path/to/drop-folder:/import
```

Open `http://<host>:8095`, enter the mail server under **Einstellungen**, add the users. The sender address must be on each reader's list of approved senders (Kindle: "Approved Personal Document E-mail List").

The UI has no login — keep it inside the home network.

### Goodreads

Paste a profile link (`https://www.goodreads.com/user/show/12345-name`) or a shelf link (`…/review/list/12345?shelf=kindle`). The default shelf is `to-read`. The profile must be public, otherwise Goodreads answers with a sign-in page and the user's row shows that.

### Fetching books

Under **Einstellungen → Beschaffung**: NZBHydra2 address and API key, SABnzbd address, API key and category. That category must save into the drop folder — a finished download is then picked up like any dropped book and mailed to the users whose shelf holds it.

- Only entries added to a shelf after fetching was switched on are fetched; what was already there can be requested one by one on the user's page.
- Each user picks a language. `Deutsch`/`Englisch` are strict: a release must say so in its name (`German`, `Deutsch`, …), otherwise the search waits and tries again after the retry interval (default 12 h). Search uses the title as it is on Goodreads, so for a German edition put the German edition on the shelf.
- A release must name the book and the author; audiobooks and comics are skipped, EPUB is preferred.
- A failed download is blocked and the next search takes another release. At most five searches run per scan.

## API

| Endpoint | |
|---|---|
| `GET /api/status` | version, whether mail is set up, users with shelf sizes, number of waiting books, last scan |
| `GET /api/books?status=sent&limit=30` | books, newest change first; `status` takes a comma list of `unassigned`, `sent`, `error`, `too_large`, `ignored` |
| `GET /api/wanted?status=wanted,grabbed` | shelf entries being fetched, with the grabbed release and the last search result |
| `GET /covers/<id>` | the cover from the EPUB |

## Develop

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest httpx
DATA_DIR=./data IMPORT_DIR=./import .venv/Scripts/uvicorn app.main:app --reload --port 8097
.venv/Scripts/pytest -q
```

Pushing to `main` runs the tests and publishes `ghcr.io/hacucoo/ebook-sender:latest`; a tag `v1.2.3` also publishes `1.2.3`.
