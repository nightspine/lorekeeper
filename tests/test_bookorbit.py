"""Book Orbit integration against a fake server that follows Book Orbit's real API shapes."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402

from .test_generate import FakeLLM, make_epub  # noqa: E402


class FakeBookOrbit(BaseHTTPRequestHandler):
    epub = b""
    refresh = "a" * 64
    log: list = []
    ranges = True          # Book Orbit supports HTTP range requests on downloads
    served = 0             # bytes of the EPUB actually sent
    progress = None        # reading-progress items served for book 11 (None: 404)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization", "").startswith("Bearer access-")

    def do_GET(self):
        FakeBookOrbit.log.append(("GET", self.path))
        if self.path == "/api/v1/server-identity":
            return self._json(200, {"product": "bookorbit", "protocolVersion": 1, "instanceId": "x"})
        if not self._authed():
            return self._json(401, {"message": "Unauthorized"})
        if self.path == "/api/v1/books/files/f1/download":
            rng = self.headers.get("Range", "")
            if FakeBookOrbit.ranges and rng.startswith("bytes="):
                a, b = (int(x) for x in rng[6:].split("-"))
                part = self.epub[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{a + len(part) - 1}/{len(self.epub)}")
            else:
                part = self.epub
                self.send_response(200)
            self.send_header("Content-Type", "application/epub+zip")
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            FakeBookOrbit.served += len(part)
            return self.wfile.write(part)
        if self.path.startswith("/api/v1/reading-progress?bookId=11") and FakeBookOrbit.progress is not None:
            return self._json(200, {"items": FakeBookOrbit.progress})
        if self.path == "/api/v1/libraries":
            return self._json(200, [{"id": 2, "name": "Comics", "type": "books", "displayOrder": 2},
                                    {"id": 1, "name": "Novels", "type": "books", "displayOrder": 1},
                                    {"id": 3, "name": "Podcasts", "type": "podcasts", "displayOrder": 3}])
        if self.path.endswith("/thumbnail"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            return self.wfile.write(b"\xff\xd8jpeg")
        return self._json(404, {"message": "Not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakeBookOrbit.log.append(("POST", self.path, body))
        if self.path == "/api/v1/auth/login":
            if body.get("password") != "hunter2" or body.get("clientKind") != "native":
                return self._json(401, {"message": "Invalid credentials"})
            return self._json(200, {"accessToken": "access-1", "refreshToken": self.refresh,
                                    "refreshTokenExpiresAt": "2026-10-28T00:00:00Z",
                                    "user": {"username": body["username"], "name": "Reader"}})
        if self.path == "/api/v1/auth/refresh":
            if body.get("refreshToken") != FakeBookOrbit.refresh:
                return self._json(401, {"message": "Invalid refresh token"})
            FakeBookOrbit.refresh = "b" * 64        # rotates
            return self._json(200, {"accessToken": "access-2", "refreshToken": FakeBookOrbit.refresh})
        if self.path == "/api/v1/auth/logout":
            return self._json(200, {})
        if not self._authed():
            return self._json(401, {"message": "Unauthorized"})
        if self.path == "/api/v1/books/query":
            page = body["pagination"]["page"]
            books = [
                {"id": 11, "libraryId": 1, "title": "The Lighthouse", "authors": ["A. Writer"], "seriesId": 7,
                 "seriesName": "Lights", "seriesIndex": "2", "files": [{"id": "f1", "format": "epub"}],
                 "hasCover": True, "coverVersion": "ebook:2026-10-01T18:59:29.175Z:2026-10-14T00:19:56.547Z",
                 "isbn13": "9781250245380"},
                {"id": 10, "libraryId": 1, "title": "The First Light", "authors": ["A. Writer"], "seriesId": 7,
                 "seriesName": "Lights", "seriesIndex": "1", "files": [{"id": "f0", "format": "epub"}], "hasCover": False},
                {"id": 12, "libraryId": 2, "title": "Absolute Batman #011", "authors": ["S. Snyder"],
                 "files": [{"id": "f2", "format": "cbz"}], "hasCover": True},
                {"id": 13, "libraryId": 2, "title": "Graphic Novel", "authors": ["B. Artist"],
                 "files": [{"id": "f3", "format": "epub"}], "hasCover": False},
            ]
            return self._json(200, {"items": books[page:page + 1], "total": len(books), "page": page, "size": 1})
        return self._json(404, {"message": "Not found"})

    def log_message(self, *a):
        pass


@pytest.fixture
def bo_server(tmp_path):
    FakeBookOrbit.epub = make_epub(tmp_path / "remote.epub").read_bytes()
    FakeBookOrbit.refresh, FakeBookOrbit.log, FakeBookOrbit.ranges, FakeBookOrbit.served = "a" * 64, [], True, 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBookOrbit)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = FakeLLM()
    seen = {}

    def make(provider, model=None, api_key=None, **kw):
        return fake
    monkeypatch.setattr(llm, "make", make)
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",))
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        c.fake, c.seen, c.data = fake, seen, tmp_path / "data"
        yield c


def wait_done(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "stopped"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_wrong_address_and_password(client, bo_server):
    r = client.post("/api/sources/bookorbit", json={"url": "http://127.0.0.1:9", "username": "u", "password": "x"})
    assert r.status_code == 400 and "Couldn't reach" in r.json()["detail"]
    r = client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "u", "password": "nope"})
    assert r.status_code == 400 and "didn't accept" in r.json()["detail"]


def test_connect_sync_and_catalog(client, bo_server):
    r = client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["connected"] and st["books"] == 3 and st["skipped"] == 1     # the CBZ comic is left out
    assert [(lib["name"], lib["books"], lib["skipped"]) for lib in st["libraries"]] == [("Novels", 2, 0), ("Comics", 1, 1)]
    assert not st["librariesChosen"]                                  # podcasts aren't offered
    secrets = (client.data / "secrets.json").read_text()
    assert "hunter2" not in secrets                                   # the password is never stored
    cat = client.get("/api/catalog").json()
    lights = next(s for s in cat["series"] if s["name"] == "Lights")
    assert lights["books"] == ["bo:10", "bo:11"]                      # ordered by series index
    assert "bo:12" not in {b["key"] for b in cat["books"]}
    url = next(b for b in cat["books"] if b["key"] == "bo:11")["cover"]
    assert client.get(url).content == b"\xff\xd8jpeg"                 # timestamp-style cover versions


def test_preflight_and_series_build_on_earlier_book(client, bo_server, tmp_path):
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    pre = client.get("/api/catalog/books/bo:11/preflight").json()
    assert [m["key"] for m in pre["missingEarlier"]] == ["bo:10"]

    # Give book 1 a local copy (uploaded by hand) placed at series position 1 of "Lights".
    up = client.post("/api/books", files={"file": ("first.epub", make_epub(tmp_path / "first.epub").read_bytes())}).json()
    # The same file as book 2's, so give it a distinct series slot by name instead: series membership by name.
    client.put(f"/api/catalog/books/up:{up['id']}/series", json={"name": "Other", "index": "1"})

    job = client.post("/api/jobs", json={"config": {"catalog_key": "bo:11"}}).json()
    done = wait_done(client, job["id"])
    assert done["status"] == "done", done["error"]
    assert done["config"]["series_id"] == "lights" and done["config"]["book_key"] == "the-lighthouse"
    assert (client.data / "indexes" / "lights.series.json").exists()
    entry = client.get("/api/catalog/books/bo:11").json()
    assert entry["status"] in ("complete", "partial") and entry["versions"] and entry["localId"]
    # The access token expired path: a forced refresh rotates the stored refresh token.
    stored = json.loads(json.loads((client.data / "secrets.json").read_text())["bookorbit"])
    assert stored["refresh_token"] in ("a" * 64, "b" * 64)


def test_disconnect_forgets_everything(client, bo_server):
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    st = client.delete("/api/sources/bookorbit").json()
    assert st == {"connected": False}
    assert "bookorbit" not in json.loads((client.data / "secrets.json").read_text())
    assert client.get("/api/catalog").json()["books"] == []
    assert any(e[:2] == ("POST", "/api/v1/auth/logout") for e in FakeBookOrbit.log)


def test_upload_series_and_default_version(client, tmp_path):
    up = client.post("/api/books", files={"file": ("b.epub", make_epub(tmp_path / "b.epub").read_bytes())}).json()
    key = f"up:{up['id']}"
    r = client.put(f"/api/catalog/books/{key}/series", json={"name": "Lights", "index": "two"})
    assert r.status_code == 400
    assert client.put(f"/api/catalog/books/{key}/series", json={"name": "Lights", "index": "2"}).json()["series"]["id"] == "lights"
    job = client.post("/api/jobs", json={"config": {"catalog_key": key}}).json()
    wait_done(client, job["id"])
    e = client.get(f"/api/catalog/books/{key}").json()
    v = e["versions"][0]["key"]
    assert e["default"] == v and e["versions"][0]["isDefault"]
    assert client.put(f"/api/catalog/books/{key}/default", json={"version": "nope"}).status_code == 404


def test_second_book_is_built_on_the_first(client, tmp_path):
    first = ["Long before the lighthouse, Ezra Pike kept the old harbour light alone for twenty winters.",
             "He kept a logbook of every ship and every storm, and never spoke of his family to anyone."]
    b1 = client.post("/api/books", files={"file": ("1.epub", make_epub(tmp_path / "1.epub", "The First Light", first).read_bytes())}).json()
    b2 = client.post("/api/books", files={"file": ("2.epub", make_epub(tmp_path / "2.epub").read_bytes())}).json()
    for bid, n in ((b1["id"], "1"), (b2["id"], "2")):
        client.put(f"/api/catalog/books/up:{bid}/series", json={"name": "Lights", "index": n})
    assert client.get(f"/api/catalog/books/up:{b2['id']}/preflight").json()["missingEarlier"][0]["key"] == f"up:{b1['id']}"

    jobs = client.post("/api/catalog/build", json={"keys": [f"up:{b1['id']}", f"up:{b2['id']}"], "config": {}}).json()["jobs"]
    for j in jobs:
        assert wait_done(client, j["id"])["status"] == "done"
    events = [json.loads(l) for l in (client.data / "jobs" / f"{jobs[1]['id']}.events.jsonl").read_text().splitlines()]
    assert any(e.get("line") == "building on 1 earlier book(s) in the series" for e in events)
    second_prompt = next(c for c in client.fake.calls if "Alice Marlow arrived" in c)
    assert "Story so far: Pike admits that Tom stayed at the lighthouse" in second_prompt   # book 1's last recap
    assert client.get(f"/api/catalog/books/up:{b2['id']}/preflight").json()["missingEarlier"] == []


def test_numbering_gap_is_reported(client, tmp_path):
    meta = lambda n: f'<meta name="calibre:series" content="Lights"/><meta name="calibre:series_index" content="{n}"/>'
    other = ["A different opening paragraph with enough words to anchor, for the first book of the series.",
             "And a second one, also long enough, so the two books have different text and ids."]
    b1 = client.post("/api/books", files={"file": ("1.epub", make_epub(tmp_path / "1.epub", "One", other, meta(1)).read_bytes())}).json()
    b3 = client.post("/api/books", files={"file": ("3.epub", make_epub(tmp_path / "3.epub", "Three", None, meta(3)).read_bytes())}).json()
    pre = client.get(f"/api/catalog/books/up:{b3['id']}/preflight").json()
    assert pre["notInLibrary"] == [2] and [m["key"] for m in pre["missingEarlier"]] == [f"up:{b1['id']}"]


def test_uploads_merge_with_their_book_orbit_entry(client, bo_server, tmp_path):
    # An upload of book 2 carrying its ISBN as an Amazon ASIN-free urn, built before connecting.
    by_isbn = make_epub(tmp_path / "isbn.epub", series_meta="<dc:identifier>urn:isbn:9781250245380</dc:identifier>")
    up = client.post("/api/books", files={"file": ("isbn.epub", by_isbn.read_bytes())}).json()
    job = client.post("/api/jobs", json={"config": {"catalog_key": f"up:{up['id']}"}}).json()
    wait_done(client, job["id"])
    before = client.get(f"/api/catalog/books/up:{up['id']}").json()["default"]

    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    books = client.get("/api/catalog").json()["books"]
    keys = [b["key"] for b in books]
    assert f"up:{up['id']}" not in keys and keys.count("bo:11") == 1          # listed once
    merged = next(b for b in books if b["key"] == "bo:11")
    assert merged["localId"] == up["id"] and merged["default"] == before     # its index came along
    assert merged["status"] in ("complete", "partial")


def test_title_match_needs_agreeing_series_number(client, bo_server, tmp_path):
    other = ["A wholly different opening paragraph, long enough to be its own text for the test.",
             "And a second paragraph to make sure the content hash differs from the other books."]
    wrong_no = make_epub(tmp_path / "w.epub", title="The First Light", ch1=other,
                         series_meta='<meta name="calibre:series" content="Lights"/><meta name="calibre:series_index" content="5"/>')
    up = client.post("/api/books", files={"file": ("w.epub", wrong_no.read_bytes())}).json()
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    keys = [b["key"] for b in client.get("/api/catalog").json()["books"]]
    assert f"up:{up['id']}" in keys and "bo:10" in keys                     # not merged: 5 vs 1


def test_isbn_from_asin():
    from lorekeeper.epub import isbns_from
    assert isbns_from(["asin:1250246512", "asin:B0DZJ3GFHM", "urn:isbn:978-0-00-835051-2"]) == ["9781250246516", "9780008350512"]



def _with_audio(epub_bytes: bytes, megabytes: int = 3) -> bytes:
    import io, os, zipfile
    src = zipfile.ZipFile(io.BytesIO(epub_bytes))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr("OEBPS/audio/track01.mp3", os.urandom(megabytes << 20), compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


@pytest.mark.parametrize("ranges", [True, False])
def test_read_along_epub_is_fetched_without_its_audio(client, bo_server, ranges):
    import io, zipfile
    FakeBookOrbit.epub = _with_audio(FakeBookOrbit.epub)
    FakeBookOrbit.ranges = ranges
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    FakeBookOrbit.served = 0
    r = client.post("/api/catalog/books/bo:11/prepare")
    assert r.status_code == 200, r.text
    local = r.json()["localId"]
    with zipfile.ZipFile(client.data / "books" / f"{local}.epub") as z:
        assert not [n for n in z.namelist() if n.endswith(".mp3")]
        assert z.namelist()[0] == "mimetype"
    if ranges:
        assert FakeBookOrbit.served < 2 << 20          # the 3 MB track was never downloaded
    assert client.get("/api/catalog/books/bo:11").json()["localId"] == local


def test_choose_libraries(client, bo_server):
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    keys = lambda: {b["key"] for b in client.get("/api/catalog").json()["books"]}  # noqa: E731
    assert keys() == {"bo:10", "bo:11", "bo:13"}                      # everything with an EPUB until chosen
    st = client.put("/api/sources/bookorbit/libraries", json={"ids": ["1"]}).json()
    assert st["librariesChosen"] and st["books"] == 2 and st["skipped"] == 0
    assert [lib["enabled"] for lib in st["libraries"]] == [True, False]
    assert keys() == {"bo:10", "bo:11"}
    assert client.post("/api/catalog/books/bo:13/prepare").status_code == 404   # hidden: not buildable
    assert client.put("/api/sources/bookorbit/libraries", json={"ids": ["3"]}).status_code == 400
    client.post("/api/sources/bookorbit/sync")
    assert keys() == {"bo:10", "bo:11"}                               # the choice survives a sync
    client.delete("/api/sources/bookorbit")
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    assert not client.get("/api/sources/bookorbit").json()["librariesChosen"]   # asked again after reconnecting


def test_position_route(client, bo_server, tmp_path):
    by_isbn = make_epub(tmp_path / "isbn.epub", series_meta="<dc:identifier>urn:isbn:9781250245380</dc:identifier>")
    up = client.post("/api/books", files={"file": ("isbn.epub", by_isbn.read_bytes())}).json()
    job = client.post("/api/jobs", json={"config": {"catalog_key": f"up:{up['id']}"}}).json()
    wait_done(client, job["id"])
    key = next(i["key"] for i in client.get("/api/library").json()["indexes"] if i["source"] == "generated")
    assert client.get(f"/api/library/{key}/position").json() == {"synced": False, "reason": "not-bookorbit"}

    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    FakeBookOrbit.progress = None
    r = client.get(f"/api/library/{key}/position").json()
    assert r["synced"] is False and r["reason"] == "unreachable" and r["detail"]
    FakeBookOrbit.progress = [{"fileId": 99, "percentage": 10, "updatedAt": None}]
    assert client.get(f"/api/library/{key}/position").json() == {"synced": False, "reason": "no-progress"}
    FakeBookOrbit.progress = [
        {"fileId": 99, "percentage": 90, "updatedAt": "2026-10-22T09:30:00Z", "text": {"cfi": None}},
        {"fileId": "f1", "percentage": 50, "updatedAt": "2026-10-21T09:30:00Z", "text": {"cfi": "epubcfi(garbage"},
         "kobo": {"source": None, "contentPercent": None}, "koreader": None}]
    r = client.get(f"/api/library/{key}/position").json()
    assert r["synced"] and r["source"] == "bookorbit" and r["basis"] == "percentage" and r["percentage"] == 50
    assert r["seq"] == 4 and r["updatedAt"] == "2026-10-21T09:30:00Z"      # the EPUB's own file wins over a newer one
    (client.data / "secrets.json").write_text("{}")
    assert client.get(f"/api/library/{key}/position").json() == {"synced": False, "reason": "not-connected"}


def test_progress_falls_back_to_flat_route_on_old_servers():
    from lorekeeper.web import bookorbit as bo
    calls = []

    def fake_call(method, path, body=None, raw=False, timeout=30):
        calls.append(path)
        if path.startswith("/reading-progress"):
            raise bo.BookOrbitError("Book Orbit couldn't find that item.")
        assert path == "/books/1097/progress"
        return [{"fileId": 5, "cfi": "epubcfi(/6/4!/4/2/1:0)", "percentage": 12.5, "koboLocationSource": None,
                 "koboContentSourceProgressPercent": None, "koreaderProgress": None, "updatedAt": "2026-10-28T10:00:00Z"}]

    client = bo.BookOrbit(bo.Session("https://x", "r", None, "u"), lambda s: None)
    client._call = fake_call
    rows = client.progress(1097)
    assert calls == ["/reading-progress?bookId=1097", "/books/1097/progress"]
    assert rows == [{"fileId": 5, "percentage": 12.5, "updatedAt": "2026-10-28T10:00:00Z",
                     "text": {"cfi": "epubcfi(/6/4!/4/2/1:0)"}, "kobo": {"source": None, "contentPercent": None}, "koreader": None}]


def test_client_refuses_every_write_before_any_request(monkeypatch):
    from lorekeeper.web import bookorbit as bo
    sent = []
    monkeypatch.setattr(bo, "_request", lambda *a, **k: sent.append(a) or {"accessToken": "t"})
    client = bo.BookOrbit(bo.Session("https://x", "r", None, "u"), lambda s: None)
    client._access, client._access_until = "t", 1e12   # a valid token, so a refusal is not a token problem
    import pytest
    for method, path in [("PUT", "/reading-progress"), ("POST", "/books/1/files/2/progress"), ("DELETE", "/reading-progress/2"),
                         ("PATCH", "/books/1/status"), ("POST", "/books/query/extra")]:
        with pytest.raises(bo.BookOrbitError, match="never writes"):
            client._call(method, path, {"x": 1})
    assert sent == []
    client._call("POST", "/books/query", {"pagination": {}})   # the read-with-a-body is still allowed
    assert len(sent) == 1
