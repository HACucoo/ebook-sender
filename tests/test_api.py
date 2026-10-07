from fastapi.testclient import TestClient

from app import main


def test_pages_and_api(epub_factory):
    client = TestClient(main.app)  # no context manager: the worker thread stays off
    r = client.post("/users", data={"name": "Stephan", "email": "s@kindle.com", "goodreads_url": "", "shelf": "to-read"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/settings", data={"smtp_host": "mail", "smtp_port": "587", "sender": "nas@example.com",
                                       "smtp_password": "geheim", "scan_minutes": "5", "goodreads_minutes": "60",
                                       "min_age_seconds": "120", "max_mb": "23", "smtp_security": "starttls"},
                    follow_redirects=False)
    assert r.status_code == 303
    # An empty password field keeps the stored one
    client.post("/settings", data={"smtp_host": "mail", "sender": "nas@example.com"}, follow_redirects=False)
    assert main.settings_store.load().smtp_password == "geheim"

    epub_factory(main.worker.import_dir / "z.epub", "Unbekannt", "Jemand")
    main.worker.scan()
    assert client.get("/").status_code == 200
    page = client.get("/settings")
    assert page.status_code == 200 and "geheim" not in page.text

    data = client.get("/api/books").json()["books"]
    assert data[0]["title"] == "Unbekannt" and data[0]["status"] == "unassigned"
    assert data[0]["cover"].startswith("/covers/")
    assert client.get(data[0]["cover"]).status_code == 200
    status = client.get("/api/status").json()
    assert status["pending"] == 1 and status["users"][0]["name"] == "Stephan"
    user_id = status["users"][0]["id"]
    assert client.get(f"/users/{user_id}").status_code == 200


def test_fetch_test_saves_first_and_never_errors():
    client = TestClient(main.app)
    r = client.post("/settings", data={"smtp_host": "mail", "sender": "nas@example.com", "hydra_url": "",
                                       "hydra_api_key": "SECRETKEY123", "sab_url": "not a url",
                                       "action": "test_fetch"}, follow_redirects=False)
    assert r.status_code == 303
    assert "fetchtest=" in r.headers["location"] and "SECRETKEY123" not in r.headers["location"]
    assert main.settings_store.load().hydra_api_key == "SECRETKEY123"  # saved before testing
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "NZBHydra2: Adresse fehlt" in page.text
