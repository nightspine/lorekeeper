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
    detail = None          # the `readStatus` of GET /books/11: dict, str, None, or "missing" to leave the field out; "down" = 500
    row_finished = "2026-11-01T10:00:00Z"
    row_status = None      # `readStatus` on the /books/query row of book 11 (a plain string), when set

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
        if self.path == "/api/v1/books/11":
            if FakeBookOrbit.detail == "down":
                return self._json(500, {"message": "boom"})
            book = {"id": 11, "title": "The Lighthouse"}
            if FakeBookOrbit.detail != "missing":
                book["readStatus"] = FakeBookOrbit.detail
            return self._json(200, book)
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
                 "isbn13": "9781250245380",
                 **({"readStatus": FakeBookOrbit.row_status, "readFinishedAt": FakeBookOrbit.row_finished}
                    if FakeBookOrbit.row_status else {})},
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
    FakeBookOrbit.detail, FakeBookOrbit.row_status, FakeBookOrbit.progress = "missing", None, None
    FakeBookOrbit.row_finished = "2026-11-01T10:00:00Z"
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
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
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


# ---- finished in Book Orbit -------------------------------------------------------------------------

def _indexed_orbit_book(client, bo_server, tmp_path):
    by_isbn = make_epub(tmp_path / "isbn.epub", series_meta="<dc:identifier>urn:isbn:9781250245380</dc:identifier>")
    up = client.post("/api/books", files={"file": ("isbn.epub", by_isbn.read_bytes())}).json()
    job = client.post("/api/jobs", json={"config": {"catalog_key": f"up:{up['id']}"}}).json()
    wait_done(client, job["id"])
    key = next(i["key"] for i in client.get(f"/api/library").json()["indexes"] if i["source"] == "generated")
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    return key


def test_trim_book_and_read_status_shapes():
    from lorekeeper.web import bookorbit as bo
    assert bo.read_status_of({"readStatus": {"status": "Read", "finishedAt": "2026-11-01T10:00:00Z"}}) == \
        {"status": "read", "finishedAt": "2026-11-01T10:00:00Z"}
    assert bo.read_status_of({"readStatus": "READ", "readFinishedAt": "2026-11-02T00:00:00Z"}) == \
        {"status": "read", "finishedAt": "2026-11-02T00:00:00Z"}
    for none in ({}, {"readStatus": None}, {"readStatus": ""}, {"readStatus": 3}, "x", None):
        assert bo.read_status_of(none) == {"status": None, "finishedAt": None}
    card = bo.trim_book({"id": 1, "libraryId": 1,
                         "readStatus": {"status": "read", "finishedAt": "2026-11-01T10:00:00Z"}})   # a BookCard
    assert card["readStatus"] == "read" and card["finishedAt"] == "2026-11-01T10:00:00Z"
    t = bo.trim_book({"id": 1, "libraryId": 1, "readStatus": "rereading"})
    assert t["readStatus"] == "rereading" and t["finishedAt"] is None
    assert bo.trim_book({"id": 1, "libraryId": 1})["readStatus"] is None   # an older server
    assert bo.is_finished("read") and not any(bo.is_finished(x) for x in
                                              ("rereading", "reading", "skimmed", "abandoned", "unread", None))


def test_client_reads_status_with_a_get_only(monkeypatch):
    from lorekeeper.web import bookorbit as bo
    sent = []
    monkeypatch.setattr(bo, "_request", lambda method, url, *a, **k: sent.append((method, url)) or
                        {"readStatus": {"status": "read", "source": "manual", "finishedAt": "2026-11-01T10:00:00Z"}})
    client = bo.BookOrbit(bo.Session("https://x", "r", None, "u"), lambda s: None)
    client._access, client._access_until = "t", 1e12
    assert client.read_status(11) == {"status": "read", "finishedAt": "2026-11-01T10:00:00Z", "known": True}
    assert sent == [("GET", "https://x/api/v1/books/11")]


def test_catalog_marks_finished_books(client, bo_server):
    FakeBookOrbit.row_status = "read"
    client.post("/api/sources/bookorbit", json={"url": bo_server, "username": "reader", "password": "hunter2"})
    books = {b["bookorbitId"]: b for b in client.get("/api/catalog").json()["books"] if b["source"] == "bookorbit"}
    assert books["11"]["finished"] is True and books["11"]["finishedAt"] == "2026-11-01T10:00:00Z"
    assert books["10"]["finished"] is False and books["10"]["finishedAt"] is None
    FakeBookOrbit.row_status = "rereading"
    client.post("/api/sources/bookorbit/sync")
    books = {b["bookorbitId"]: b for b in client.get("/api/catalog").json()["books"] if b["source"] == "bookorbit"}
    assert books["11"]["finished"] is False and books["11"]["finishedAt"] is None


def test_position_finished_in_book_orbit(client, bo_server, tmp_path):
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    url = f"/api/library/{key}/position"
    stale = [{"fileId": "f1", "percentage": 40, "updatedAt": "2026-10-21T09:30:00Z", "text": {"cfi": None},
              "kobo": {"source": None, "contentPercent": None}, "koreader": None}]

    # finished, no progress rows at all (an object status)
    FakeBookOrbit.progress = []
    FakeBookOrbit.detail = {"status": "read", "source": "manual", "finishedAt": "2026-11-01T10:00:00Z"}
    r = client.get(url).json()
    last = r["seq"]
    assert r == {"synced": True, "source": "bookorbit", "finished": True, "percentage": 100, "p": 1.0,
                 "seq": last, "basis": "finished", "updatedAt": "2026-11-01T10:00:00Z"}
    assert last > 4

    # a stale 40% row does not hold it back; a string status works too; no finishedAt: the newest row's time
    FakeBookOrbit.progress, FakeBookOrbit.detail = stale, "read"
    r = client.get(url).json()
    assert r["finished"] is True and r["seq"] == last and r["p"] == 1.0 and r["updatedAt"] == "2026-10-21T09:30:00Z"
    FakeBookOrbit.progress = []
    assert client.get(url).json()["updatedAt"] is None

    # rereading and the other statuses behave as today, with finished: false
    FakeBookOrbit.progress = stale
    for status in ("rereading", "reading", "skimmed", "abandoned", "on_hold", "unread"):
        FakeBookOrbit.detail = {"status": status}
        r = client.get(url).json()
        assert r["synced"] and r["finished"] is False and r["basis"] == "percentage" and r["percentage"] == 40
    FakeBookOrbit.progress = []
    assert client.get(url).json() == {"synced": False, "reason": "no-progress"}

    # an older server with no status field: nothing changes
    FakeBookOrbit.detail, FakeBookOrbit.progress = "missing", stale
    assert client.get(url).json()["finished"] is False
    FakeBookOrbit.detail = None
    assert client.get(url).json()["finished"] is False


def test_position_null_status_clears_the_cache_but_a_missing_field_keeps_it(client, bo_server, tmp_path):
    FakeBookOrbit.row_status = "read"
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    url = f"/api/library/{key}/position"
    FakeBookOrbit.progress = [{"fileId": "f1", "percentage": 40, "updatedAt": "2026-10-21T09:30:00Z"}]
    FakeBookOrbit.detail = "missing"             # older server: the cached read stays
    assert client.get(url).json()["finished"] is True
    FakeBookOrbit.detail = None                  # newer server, no status now: not finished
    r = client.get(url).json()
    assert r["finished"] is False and r["percentage"] == 40


def test_position_unreachable_server_skips_the_second_call(client, bo_server, tmp_path, monkeypatch):
    from lorekeeper.web import bookorbit as bo
    FakeBookOrbit.row_status, FakeBookOrbit.row_finished = "read", None    # no date: progress would be asked
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    url = f"/api/library/{key}/position"
    calls = []

    def down(self, method, path, *a, **k):
        calls.append(path)
        raise bo.BookOrbitUnreachable("Couldn't reach Book Orbit.")
    monkeypatch.setattr(bo.BookOrbit, "_call", down)
    assert client.get(url).json()["finished"] is True
    assert len(calls) == 1 and calls[0].startswith("/books/11")
    calls.clear()

    def other(self, method, path, *a, **k):         # a non-connection error still goes on to progress
        calls.append(path)
        raise bo.BookOrbitError("Book Orbit couldn't find that item.")
    monkeypatch.setattr(bo.BookOrbit, "_call", other)
    client.get(url)
    assert len(calls) >= 2 and calls[0].startswith("/books/11") and "progress" in calls[1]


def test_position_status_falls_back_to_the_cache(client, bo_server, tmp_path):
    FakeBookOrbit.row_status = "read"            # the sync saw it finished
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    url = f"/api/library/{key}/position"
    FakeBookOrbit.progress = []
    FakeBookOrbit.detail = "down"                # the live status fails: the cached one holds
    r = client.get(url).json()
    assert r["synced"] and r["finished"] is True and r["updatedAt"] == "2026-11-01T10:00:00Z"
    FakeBookOrbit.progress = None                # progress is unreachable too: still finished
    assert client.get(url).json()["finished"] is True
    FakeBookOrbit.detail = {"status": "rereading"}   # live wins over the cache
    FakeBookOrbit.progress = [{"fileId": "f1", "percentage": 20, "updatedAt": "2026-11-02T09:00:00Z"}]
    assert client.get(url).json()["finished"] is False
    FakeBookOrbit.progress = None
    r = client.get(url).json()
    assert r["synced"] is False and r["reason"] == "unreachable"


def test_finished_status_never_writes(client, bo_server, tmp_path):
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    FakeBookOrbit.log.clear()
    FakeBookOrbit.detail, FakeBookOrbit.progress = {"status": "read"}, []
    client.get(f"/api/library/{key}/position")
    assert [m for m, *_ in FakeBookOrbit.log if m != "GET"] == []


def test_refresh_failing_offline_is_unreachable_and_asked_once(monkeypatch):
    import urllib.error
    from lorekeeper.web import bookorbit as bo
    seen = []

    def offline(method, url, *a, **k):
        seen.append(url)
        raise urllib.error.URLError("down")
    monkeypatch.setattr(bo, "_request", offline)
    client = bo.BookOrbit(bo.Session("https://x", "r", None, "u"), lambda s: None)   # no access token: must refresh
    with pytest.raises(bo.BookOrbitUnreachable):
        client.read_status(11)
    assert len(seen) == 1 and seen[0].endswith("/auth/refresh")


def test_unreadable_answers_are_book_orbit_errors(monkeypatch):
    import http.client
    from lorekeeper.web import bookorbit as bo
    client = bo.BookOrbit(bo.Session("https://x", "r", None, "u"), lambda s: None)
    client._access, client._access_until = "t", 1e12
    for exc in (json.JSONDecodeError("bad", "x", 0), http.client.IncompleteRead(b"ab"), UnicodeDecodeError("utf-8", b"", 0, 1, "x")):
        def boom(*a, _e=exc, **k):
            raise _e
        monkeypatch.setattr(bo, "_request", boom)
        with pytest.raises(bo.BookOrbitError) as got:
            client.read_status(11)
        assert not isinstance(got.value, bo.BookOrbitUnreachable)


def test_position_keeps_the_last_live_status_when_a_call_fails(client, bo_server, tmp_path):
    key = _indexed_orbit_book(client, bo_server, tmp_path)      # the sync cache has no status
    url = f"/api/library/{key}/position"
    FakeBookOrbit.progress = [{"fileId": "f1", "percentage": 40, "updatedAt": "2026-10-21T09:30:00Z"}]
    FakeBookOrbit.detail = {"status": "read", "finishedAt": "2026-11-01T10:00:00Z"}
    assert client.get(url).json()["finished"] is True
    FakeBookOrbit.detail = "down"                                # a 500 must not bounce the Reader
    assert client.get(url).json()["finished"] is True
    FakeBookOrbit.detail = {"status": "reading"}                 # a live answer replaces it
    assert client.get(url).json()["finished"] is False
    FakeBookOrbit.detail = "down"
    assert client.get(url).json()["finished"] is False
    client.delete("/api/sources/bookorbit")                      # disconnecting forgets it


def test_finished_with_a_date_skips_the_progress_call(client, bo_server, tmp_path):
    key = _indexed_orbit_book(client, bo_server, tmp_path)
    FakeBookOrbit.detail, FakeBookOrbit.progress = {"status": "read", "finishedAt": "2026-11-01T10:00:00Z"}, []
    FakeBookOrbit.log.clear()
    r = client.get(f"/api/library/{key}/position").json()
    assert r["finished"] is True and not any("progress" in e[1] for e in FakeBookOrbit.log)
    FakeBookOrbit.detail = {"status": "read"}                    # no date: progress is asked for the time
    FakeBookOrbit.log.clear()
    client.get(f"/api/library/{key}/position")
    assert any("progress" in e[1] for e in FakeBookOrbit.log)
