"""Community indexes through the web API, against a fake registry: a static tree built with `registry.build`
and served through the injected fetch function, so no test touches the network."""

from lorekeeper.web.catalog import model_label
import copy
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import epub, registry  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import community as community_mod  # noqa: E402

from .conftest import CORPUS, ROOT  # noqa: E402
from .test_generate import make_epub  # noqa: E402

NOW = "2026-11-13T20:00:00Z"
LATER = "2026-11-14T08:00:00Z"
URL = "https://registry.test/reg"
HEADERS = {"X-Lorekeeper": "1"}


def new_app(tmp_path, name, **kw):
    return web_app.create_app(tmp_path / name, [], [], ollama_url="http://127.0.0.1:9",
                              allowed_hosts=("testserver",), auth=False, **kw)


def upload(c, path):
    r = c.post("/api/books", files={"file": (path.name, path.read_bytes(), "application/epub+zip")})
    assert r.status_code == 200, r.text
    return r.json()


class Registry:
    """A registry tree + its static build, served by name. `calls` records every URL asked for."""

    def __init__(self, tmp_path):
        self.src, self.out = tmp_path / "regsrc", tmp_path / "regout"
        self.calls: list[str] = []
        self.override: dict[str, object] = {}     # url → (status, body) or an exception to raise

    def add(self, bundle, publisher="someone", now=NOW, tmp=None):
        p = (tmp or self.src.parent) / f"bundle-{publisher}-{now}.json"
        p.write_text(registry.dumps(bundle), encoding="utf-8")
        listing, issues = registry.add(str(p), publisher, self.src, now)
        assert listing is not None, [str(i) for i in issues]
        registry.build(self.src, self.out, base_path="/reg")
        return listing

    def __call__(self, url):
        self.calls.append(url)
        got = self.override.get(url)
        if isinstance(got, Exception):
            raise got
        if got is not None:
            return got
        assert url.startswith(URL + "/"), url
        f = self.out / url[len(URL) + 1:]
        if not f.is_file():
            return 404, b""
        return 200, f.read_bytes()


def maker_index(tmp_path, epub_path, complete=True, license="CC0-1.0"):
    """A valid hand-made index for the book, made in a throwaway app."""
    app = new_app(tmp_path, "maker")
    with TestClient(app, headers=HEADERS) as c:
        book = upload(c, epub_path)
        r = c.post(f"/api/catalog/books/up:{book['id']}/handmade", json={"license": license})
        assert r.status_code == 200, r.text
        key = r.json()["key"]
        if complete:
            assert c.post(f"/api/library/{key}/edits", json={"op": "set-complete", "complete": True}).status_code == 200
        doc = c.get(f"/api/library/{key}").json()
    shutil.rmtree(tmp_path / "maker")
    return doc


def bundle_of(doc, epub_path):
    bundle, rep = registry.make_bundle(doc, blocks=epub.blocks(epub_path), now=NOW)
    assert bundle is not None, [str(i) for i in rep["issues"]]
    return bundle


@pytest.fixture
def epub_path(tmp_path):
    return make_epub(tmp_path / "lighthouse.epub")


@pytest.fixture
def reg(tmp_path, epub_path):
    r = Registry(tmp_path)
    r.listing = r.add(bundle_of(maker_index(tmp_path, epub_path), epub_path))
    return r


@pytest.fixture
def env(tmp_path, epub_path, reg):
    app = new_app(tmp_path, "data")
    app.state.community.fetch = reg
    with TestClient(app, headers=HEADERS) as c:
        c.reg, c.app = reg, app
        c.book = upload(c, epub_path)
        c.key = f"up:{c.book['id']}"
        c.th = c.book["textHash"]
        yield c


def turn_on(c, url=None):
    r = c.put("/api/community", json={"enabled": True, **({"url": url} if url else {})})
    assert r.status_code == 200, r.text
    return r.json()


def books(c, key=None):
    r = c.get(f"/api/community/books/{key or c.key}")
    assert r.status_code == 200, r.text
    return r.json()


def listing_url(c):
    return f"{URL}/v0/lookup/text/{c.th}.json"


def offer(c):
    return books(c)["results"][0]


def fetch(c, res=None):
    res = res or offer(c)
    return c.post("/api/community/fetch", json={"catalogKey": c.key, "id": res["id"], "digest": res["digest"]})


def install(c, res=None):
    res = res or offer(c)
    return c.post("/api/community/install", json={"catalogKey": c.key, "id": res["id"], "digest": res["digest"]})


def serve_index(c, body: bytes):
    """Make the registry list `body` as the index of the listing: its digest everywhere, its bytes at the href."""
    digest = registry.digest_of(body)
    url = listing_url(c)
    lookup = json.loads((c.reg.out / f"v0/lookup/text/{c.th}.json").read_text())
    r = lookup["results"][0]
    r["digest"] = r["listing"]["digest"] = digest
    r["listing"]["versions"][-1]["digest"] = digest
    c.reg.override[url] = (200, json.dumps(lookup).encode())
    c.reg.override[f"{URL}{r['href'][len('/reg'):]}"] = (200, body)
    return digest


# ---- opt-in ---------------------------------------------------------------------------------

def test_never_asked_makes_no_request(env):
    assert env.get("/api/community").json() == {"enabled": None, "url": community_mod.DEFAULT_URL, "default": True}
    assert books(env) == {"state": "ask", "results": []}
    assert env.reg.calls == []


def test_off_makes_no_request(env):
    assert env.put("/api/community", json={"enabled": False}).json()["enabled"] is False
    assert books(env) == {"state": "off", "results": []}
    assert env.reg.calls == []
    assert env.post("/api/community/fetch", json={"catalogKey": env.key, "id": "x", "digest": "0" * 64}).status_code == 409


def test_book_without_a_file_has_no_edition(env, tmp_path):
    turn_on(env, URL)
    (tmp_path / "data" / "bookorbit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "bookorbit" / "books.json").write_text(json.dumps({"books": [
        {"id": "7", "title": "Elsewhere", "authors": ["X"], "libraryId": "l", "epubFileId": "f"}], "synced": 1}))
    assert books(env, "bo:7") == {"state": "no-edition", "results": []}
    assert env.reg.calls == []


def test_unknown_book_is_404(env):
    assert env.get("/api/community/books/up:nope").status_code == 404


def test_configure_validates_and_clears_the_cache(env):
    turn_on(env, URL)
    books(env)
    cached = env.app.state.community._lookup_file(env.th)
    assert cached.exists()
    for bad in ("ftp://x.test", "javascript:alert(1)", "https://u:p@x.test", "nonsense", "http://registry.example/reg"):
        assert env.put("/api/community", json={"enabled": True, "url": bad}).status_code == 400
    assert env.put("/api/community", json={"enabled": "yes"}).status_code == 400
    assert env.put("/api/community", json={"enabled": True, "url": URL + "/"}).json()["url"] == URL
    assert cached.exists()                         # the same address keeps it
    assert turn_on(env, "https://other.test/reg")["default"] is False
    assert not cached.exists()


# ---- the books route ------------------------------------------------------------------------

def test_found(env):
    turn_on(env, URL)
    got = books(env)
    assert got["state"] == "ok" and len(got["results"]) == 1
    r = got["results"][0]
    assert r["id"] == env.reg.listing["id"] and r["id"].startswith("someone/")
    assert r["publisher"] == "someone" and r["version"] == 1 and r["complete"] is True
    assert r["license"] == "CC0-1.0" and r["made"] == "Hand-written" and r["edited"] is True
    assert r["checked"] == {"kind": "book-checks", "by": "builder"}
    assert r["early"] == 0 and r["installed"] is None and r["update"] is False
    assert r["digest"] == env.reg.listing["digest"]


def test_not_listed_is_ok_with_no_results(env):
    turn_on(env, URL)
    env.reg.override[listing_url(env)] = (404, b"")
    assert books(env) == {"state": "ok", "results": []}


def test_unreachable_then_served_stale(env):
    turn_on(env, URL)
    now = [1000.0]
    env.app.state.community.now = lambda: now[0]
    env.reg.override[listing_url(env)] = community_mod.FetchError("down")
    assert books(env) == {"state": "unreachable", "results": []}
    del env.reg.override[listing_url(env)]
    assert len(books(env)["results"]) == 1
    n = len(env.reg.calls)
    now[0] += 60
    assert len(books(env)["results"]) == 1 and len(env.reg.calls) == n           # fresh: no request
    now[0] += 1000
    env.reg.override[listing_url(env)] = community_mod.FetchError("down")
    got = books(env)
    assert got["state"] == "ok" and len(got["results"]) == 1                      # stale, registry down
    env.reg.override[listing_url(env)] = (500, b"")
    assert books(env)["state"] == "ok"


def test_oversized_lookup_is_unreachable(env):
    turn_on(env, URL)
    env.reg.override[listing_url(env)] = (200, b"x" * (community_mod.MAX_BODY + 1))
    assert books(env)["state"] == "unreachable"


def test_garbage_lookup_is_unreachable(env):
    turn_on(env, URL)
    for body in (b"not json", b'{"results": "no"}', b"[" * 100 + b"]" * 100, b'{"results": [NaN]}'):
        env.reg.override[listing_url(env)] = (200, body)
        assert books(env)["state"] == "unreachable", body


def test_series_listing_is_dropped(env):
    turn_on(env, URL)
    lookup = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())
    lookup["results"][0]["series"] = {"id": "s", "book": "b"}
    env.reg.override[listing_url(env)] = (200, json.dumps(lookup).encode())
    assert books(env) == {"state": "ok", "results": []}
    lookup["results"][0].pop("series")
    lookup["results"][0]["listing"]["series"] = {"id": "s", "book": "b", "requires": []}
    env.reg.override[listing_url(env)] = (200, json.dumps(lookup).encode())
    env.app.state.community.now = lambda: 10 ** 10      # past the cached answer
    assert books(env) == {"state": "ok", "results": []}


def test_other_text_is_not_offered(env):
    turn_on(env, URL)
    lookup = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())
    lookup["results"][0]["listing"]["editions"] = [{"textHash": "0" * 64}]
    env.reg.override[listing_url(env)] = (200, json.dumps(lookup).encode())
    assert books(env)["results"] == []


def test_off_origin_href_is_not_offered(env):
    turn_on(env, URL)
    lookup = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())
    good = lookup["results"][0]["href"]
    for href in ("https://evil.test/reg/v0/indexes/someone/the-lighthouse/v1.lore.json",
                 "//evil.test/reg/v0/indexes/someone/the-lighthouse/v1.lore.json",
                 "/other/v0/indexes/someone/the-lighthouse/v1.lore.json",
                 good + "?x=1", good + "#x", "/reg/v0/indexes/../../etc/passwd",
                 "/reg/v0/indexes/someone/the-lighthouse/v1.lore.json/../v2.lore.json",
                 "http://registry.test/reg/v0/indexes/someone/the-lighthouse/v1.lore.json"):
        lookup["results"][0]["href"] = href
        env.reg.override[listing_url(env)] = (200, json.dumps(lookup).encode())
        env.app.state.community.now = lambda: 10 ** 10 + len(env.reg.calls)
        assert books(env)["results"] == [], href
    assert not [u for u in env.reg.calls if "evil" in u]


def test_resolve_refuses_outside_the_registry():
    r = community_mod.Community._resolve
    assert r(URL, "/reg/v0/indexes/a/b/v3.lore.json")[1:] == ("a", "b", 3)
    for href in ("https://evil.test/reg/v0/indexes/a/b/v1.lore.json", "/v0/indexes/a/b/v1.lore.json",
                 "/reg/v0/indexes/a/b/v1.lore.json?q", "/reg/v0/lookup/text/x.json", "/reg/v0/indexes/a/b/v0.json"):
        with pytest.raises(community_mod.CommunityError):
            r(URL, href)


def test_a_redirect_is_followed_only_under_the_registry():
    import urllib.request
    h = community_mod._Confined(URL)
    req = urllib.request.Request(URL + "/v0/lookup/text/x.json")
    for target in ("https://evil.test/reg/x", "https://registry.test/other/x", "https://registry.test/regx/x",
                   "http://registry.test/reg/x", "https://registry.test/reg/../other/x"):
        with pytest.raises(Exception, match="outside the registry"):
            h.redirect_request(req, None, 302, "Found", {}, target)
    assert h.redirect_request(req, None, 302, "Found", {}, URL + "/v0/other.json").full_url == URL + "/v0/other.json"


def serve_once(handler_body, n=1):
    """A loopback server running handler_body(conn) for each of n connections; (url, thread-stopper)."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        for _ in range(n):
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda c=conn: handler_body(c), daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}", srv.close


def test_fetch_follows_a_redirect_inside_the_registry_and_refuses_one_outside():
    def h(conn):
        data = conn.recv(4096).decode()
        path = data.split()[1]
        if path == "/reg/a":
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /reg/b\r\nContent-Length: 0\r\n\r\n")
        elif path == "/reg/b":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        else:
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 0\r\n\r\n")
        conn.close()
    url, stop = serve_once(h, 6)
    try:
        assert community_mod.default_fetch(url + "/reg/a", url + "/reg") == (200, b"ok")
        with pytest.raises(community_mod.FetchError):
            community_mod.default_fetch(url + "/reg/c", url + "/reg")      # another path on the same host
    finally:
        stop()


def test_fetch_stops_after_a_few_redirects():
    hits = []

    def h(conn):
        hits.append(1)
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /reg/loop\r\nContent-Length: 0\r\n\r\n")
        conn.close()
    url, stop = serve_once(h, 30)
    try:
        with pytest.raises(community_mod.FetchError):
            community_mod.default_fetch(url + "/reg/loop", url + "/reg")
        assert len(hits) <= community_mod.MAX_REDIRECTS + 2
    finally:
        stop()


def test_one_deadline_covers_slow_headers_and_a_slow_body(monkeypatch):
    import time as _t
    monkeypatch.setattr(community_mod, "TIMEOUT", 1)

    def drip_headers(conn):         # a byte every 0.3 s: no single read ever times out
        conn.recv(4096)
        try:
            for b in b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 100:
                conn.send(bytes([b]))
                _t.sleep(0.3)
        except OSError:
            pass

    def drip_body(conn):
        conn.recv(4096)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(100):
                conn.send(b"x")
                _t.sleep(0.3)
        except OSError:
            pass
    for body in (drip_headers, drip_body):
        url, stop = serve_once(body)
        t0 = _t.monotonic()
        try:
            with pytest.raises(community_mod.FetchError):
                community_mod.default_fetch(url + "/reg/x", url + "/reg")
        finally:
            stop()
        assert _t.monotonic() - t0 < 15, body.__name__      # ~1 s healthy; following the drip takes 30 s


def test_a_huge_lookup_is_answered_quickly_and_validates_a_bounded_number_of_results(env, monkeypatch):
    import time as _t
    turn_on(env, URL)
    n = 0
    real = community_mod._result_validator()

    class Counting:
        def iter_errors(self, r):
            nonlocal n
            n += 1
            return real.iter_errors(r)
    monkeypatch.setattr(community_mod, "_result_validator", lambda: Counting())
    env.reg.override[listing_url(env)] = (200, b'{"results": [' + b",".join([b"{}"] * 1_000_000) + b"]}")
    t0 = _t.monotonic()
    assert books(env) == {"state": "ok", "results": []}
    assert _t.monotonic() - t0 < 60 and n == community_mod.MAX_RESULTS


def test_one_malformed_result_is_dropped_and_the_valid_one_kept(env):
    turn_on(env, URL)
    lookup = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())
    lookup["results"] = [{"id": 5}, lookup["results"][0], {}, "x"]
    env.reg.override[listing_url(env)] = (200, json.dumps(lookup).encode())
    assert [r["id"] for r in books(env)["results"]] == [env.reg.listing["id"]]


def test_an_edition_that_is_not_built_from_the_text_is_refused_at_fetch_and_install(env):
    turn_on(env, URL)
    for flag in (False, None):      # false, or missing: only builtFrom true is an exact edition
        env.app.state.community.now = lambda: 10 ** 10 + (10_000 if flag is None else 0)
        doc = good_doc(env)
        if flag is None:
            doc["editions"][0].pop("builtFrom", None)
        else:
            doc["editions"][0]["builtFrom"] = flag
        d = serve_index(env, registry.dumps(doc).encode())
        res = {"id": offer(env)["id"], "digest": d}
        r = fetch(env, res)
        assert r.status_code == 422 and r.json()["code"] == "invalid" and "not made for this copy" in r.json()["detail"]
        assert not env.app.state.community._cache_file(d).exists()
        assert install(env, res).status_code in (409, 422)


def test_an_unresolved_anchor_with_a_seq_hint_is_refused_but_one_without_is_reported(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["anchors"][0]["fp"] = "0" * len(doc["anchors"][0]["fp"])      # the hint says 0 but nothing resolves
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422 and r.json()["code"] == "invalid"
    assert not env.app.state.community._cache_file(d).exists()
    env.app.state.community.now = lambda: 10 ** 10
    doc["anchors"][0].pop("seq")
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 200 and r.json()["resolved"] == 0


def settled(timeout=15):
    """Wait for every fetch slot to come back (polling, never one instant)."""
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if community_mod._slots._value == community_mod.MAX_INFLIGHT:
            return True
        _t.sleep(0.05)
    return False


def test_timed_out_fetches_are_closed_and_in_flight_ones_are_capped(monkeypatch):
    import threading
    import time as _t
    monkeypatch.setattr(community_mod, "TIMEOUT", 0.6)
    closed = []

    def drip(conn):
        conn.recv(4096)
        try:
            for b in b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 1000:
                conn.send(bytes([b]))
                _t.sleep(0.2)
        except OSError:
            closed.append(1)
    url, stop = serve_once(drip, 20)
    try:
        for _ in range(community_mod.MAX_INFLIGHT + 3):      # more than the cap, one after another
            with pytest.raises(community_mod.FetchError):
                community_mod.default_fetch(url + "/reg/x", url + "/reg")
        assert settled()      # the timed-out connections were shut and their threads ended
        assert closed
    finally:
        stop()


def test_a_connection_that_comes_up_after_the_deadline_is_closed_and_frees_its_slot(monkeypatch):
    import socket
    import time as _t
    monkeypatch.setattr(community_mod, "TIMEOUT", 0.5)
    served = []

    def drip(conn):
        served.append(1)
        try:
            conn.recv(4096)
            for b in b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 1000:
                conn.send(bytes([b]))
                _t.sleep(0.2)
        except OSError:
            pass
    url, stop = serve_once(drip, 5)
    real = socket.create_connection

    def slow(*a, **k):
        _t.sleep(1.2)         # connecting returns well after the 0.5 s deadline
        return real(*a, **k)
    monkeypatch.setattr(socket, "create_connection", slow)
    try:
        with pytest.raises(community_mod.FetchError):
            community_mod.default_fetch(url + "/reg/x", url + "/reg")
        assert settled(10)      # the worker closed the late connection instead of reading the drip
    finally:
        stop()


def test_fetches_beyond_the_cap_are_refused_at_once(monkeypatch):
    import threading
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(community_mod, "_slots", sem)
    with pytest.raises(community_mod.FetchError, match="too many"):
        community_mod.default_fetch("http://127.0.0.1:9/x")


def test_a_lying_seq_hint_is_refused_and_not_cached(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["anchors"][0]["seq"] = doc["anchors"][0]["seq"] + 1
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422 and r.json()["code"] == "invalid"
    assert not env.app.state.community._cache_file(d).exists()


def test_registry_slug_keeps_a_non_default_port_and_the_default_name():
    slug = community_mod.registry_slug
    assert slug(community_mod.DEFAULT_URL) == "nightspine-github-io-lorekeeper-registry"
    assert slug("https://nightspine.github.io:443/lorekeeper-registry") == slug(community_mod.DEFAULT_URL)
    assert slug("http://localhost:8001/reg") != slug("http://localhost:8002/reg")
    assert slug("http://localhost:8001/reg") == "localhost-8001-reg"


def test_a_name_too_long_for_the_filesystem_is_refused():
    with pytest.raises(community_mod.CommunityError) as e:
        community_mod.Community._file_name(URL, "p" * 128, "a" * 128, 1)
    assert e.value.status == 422
    name = community_mod.Community._file_name(URL, "someone", "a" * 100, 1)
    assert len(name) + 37 <= 255


def listing(i, **kw):
    base = {"id": f"p{i}/b", "publisher": f"p{i}", "work": {"title": "B"}, "version": 1, "digest": "sha256:" + "0" * 64,
            "complete": True, "updated": f"2026-11-1{i}T00:00:00Z", "license": "CC0-1.0", "attestations": []}
    return {"id": base["id"], "digest": base["digest"], "href": "", "listing": {**base, **kw}}


def audit(n, of, by="builder"):
    return {"kind": "audit", "by": by, "at": NOW, "textHash": "t", "sampled": of, "supported": n, "model": "m"}


def gate(early):
    return {"kind": "spoiler-gate", "by": "builder", "at": NOW, "textHash": "t", "names": 9, "found": 9, "early": early}


def test_ranking(env, monkeypatch):
    c = env.app.state.community
    rows = [
        listing(1, complete=False),                                    # partial
        listing(2, attestations=[gate(3)]),                            # names too early
        listing(3),                                                    # nothing known, older than 4
        listing(4, attestations=[gate(0)]),                            # clean, no audit
        listing(5, attestations=[gate(0), audit(2, 2)]),               # audited, but only two claims
        listing(6, attestations=[gate(0), audit(48, 50)]),             # audited well
    ]
    monkeypatch.setattr(c, "lookup", lambda th: rows)
    got = [r["publisher"] for r in c.results("t")]
    assert got == ["p6", "p4", "p5", "p3", "p2", "p1"]          # p5's 2 of 2 is below 0.5: after unaudited p4


def test_checked_names_the_model_by_its_label(env, monkeypatch):
    c = env.app.state.community
    att = {**audit(40, 50), "model": "claude-haiku-5-5"}
    monkeypatch.setattr(c, "lookup", lambda th: [listing(1, attestations=[att])])
    got = c.results("t")[0]["checked"]
    assert got["model"] == model_label({"model": "claude-haiku-5-5"}) and got["model"]


def test_a_newer_listing_wins_a_tie(env, monkeypatch):
    c = env.app.state.community
    monkeypatch.setattr(c, "lookup", lambda th: [listing(1), listing(2)])
    assert [r["publisher"] for r in c.results("t")] == ["p2", "p1"]


def test_checked_prefers_audit_and_newest(env, monkeypatch):
    c = env.app.state.community
    old, new = {**audit(10, 50), "at": "2026-01-01T00:00:00Z", "by": "reader"}, {**audit(48, 50), "by": "registry"}
    book = {"kind": "book-checks", "by": "builder", "at": NOW, "textHash": "t", "anchors": 1, "resolved": 1, "errors": 2}
    monkeypatch.setattr(c, "lookup", lambda th: [listing(1, attestations=[old, new, book]), listing(2, attestations=[book])])
    by = {r["publisher"]: r for r in c.results("t")}
    assert by["p1"]["checked"] == {"kind": "audit", "by": "registry", "sampled": 50, "supported": 48, "model": "m"}
    assert by["p2"]["checked"] == {"kind": None, "by": None}          # book checks with errors say nothing


# ---- fetch ----------------------------------------------------------------------------------

def test_fetch_checks_the_index_against_the_book(env):
    turn_on(env, URL)
    r = fetch(env)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["anchors"] == got["resolved"] == 1 and got["early"] == 0 and got["errors"] == []
    cached = env.app.state.community._cache_file(got["digest"])
    assert registry.digest_of(cached.read_bytes()) == got["digest"]
    hexd = got["digest"].split(":")[1]
    doc = env.get(f"/api/community/preview/{hexd}").json()
    assert doc["kind"] == "index" and doc["editions"][0]["textHash"] == env.th


def test_preview_needs_a_fetch_and_a_real_digest(env):
    turn_on(env, URL)
    assert env.get("/api/community/preview/" + "a" * 64).status_code == 404
    assert env.get("/api/community/preview/..%2f..%2fsecrets").status_code == 404
    assert env.get("/api/community/preview/short").status_code == 404


def test_preview_notices_a_changed_cache_file(env):
    turn_on(env, URL)
    d = fetch(env).json()["digest"]
    env.app.state.community._cache_file(d).write_text("{}")
    r = env.get(f"/api/community/preview/{d.split(':')[1]}")
    assert r.status_code == 409 and r.json()["code"] == "digest"


def test_fetch_refuses_a_digest_mismatch(env):
    turn_on(env, URL)
    res = offer(env)
    href = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())["results"][0]["href"]
    url = f"{URL}{href[len('/reg'):]}"
    env.reg.override[url] = (200, (env.reg.out / href[len("/reg/"):]).read_bytes().replace(b"Lighthouse", b"Lightbulb"))
    r = fetch(env, res)
    assert r.status_code == 502 and r.json()["code"] == "digest"
    assert not (env.app.state.community.dir / "cache").exists()      # nothing from a tampered download is kept


def test_fetch_only_names_a_listed_index(env):
    turn_on(env, URL)
    res = offer(env)
    assert env.post("/api/community/fetch", json={"catalogKey": env.key, "id": "x/y", "digest": res["digest"]}).status_code == 404
    assert env.post("/api/community/fetch", json={"catalogKey": env.key, "id": res["id"], "digest": "sha256:" + "1" * 64}).status_code == 404
    assert env.post("/api/community/fetch", json={"catalogKey": env.key, "id": res["id"], "digest": "nope"}).status_code == 400
    assert not [u for u in env.reg.calls if "/indexes/" in u]


def good_doc(env):
    href = json.loads((env.reg.out / f"v0/lookup/text/{env.th}.json").read_text())["results"][0]["href"]
    return json.loads((env.reg.out / href[len("/reg/"):]).read_text())


def test_fetch_refuses_an_oversized_index(env):
    turn_on(env, URL)
    serve_index(env, b"{" + b" " * (community_mod.MAX_BODY + 10) + b"}")
    r = fetch(env)
    assert r.status_code == 502 and r.json()["code"] == "unreachable"


def test_fetch_refuses_deep_nesting_and_not_json(env):
    turn_on(env, URL)
    for n, body in enumerate((b"[" * 40 + b"]" * 40, b"not json at all", b'{"a": NaN}')):
        res = serve_index(env, body)
        env.app.state.community.now = lambda n=n: 10 ** 10 + n * 10_000
        r = fetch(env, {"id": offer(env)["id"], "digest": res})
        assert r.status_code == 422 and r.json()["code"] == "invalid", body


def test_fetch_refuses_a_schema_invalid_index(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["entities"] = "none"
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422 and r.json()["code"] == "invalid"


def test_fetch_refuses_an_index_that_breaks_the_rules(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["coverage"]["through"] = "no-such-anchor"
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422 and r.json()["code"] == "invalid"


def test_fetch_refuses_an_index_for_another_text(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["editions"][0]["textHash"] = "f" * 64
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422 and r.json()["code"] == "invalid"


def test_fetch_refuses_a_series_index(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["series"] = {"id": "s", "book": "b"}
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 422


def test_fetch_reports_problems_against_the_copy_without_refusing(env):
    turn_on(env, URL)
    doc = good_doc(env)
    a = doc["anchors"][0]
    a["fp"] = "0" * len(a["fp"])
    a.pop("seq", None)      # with a seq hint this is refused (below); without one it is only reported
    d = serve_index(env, registry.dumps(doc).encode())
    r = fetch(env, {"id": offer(env)["id"], "digest": d})
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] == 0 and r.json()["anchors"] == 1


# ---- install --------------------------------------------------------------------------------

def test_install_makes_it_the_default_and_the_reader_opens_it(env):
    turn_on(env, URL)
    assert install(env).status_code == 409                       # not downloaded yet
    assert fetch(env).status_code == 200
    r = install(env)
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert r.json()["madeDefault"] is True and key.startswith("community--registry-test-reg--someone--")
    entry = env.get(f"/api/catalog/books/{env.key}").json()
    assert entry["default"] == key and entry["status"] == "complete"
    v = entry["versions"][0]
    assert v["source"] == "community" and v["sharedBy"] == "someone" and v["isDefault"]
    assert env.get(f"/api/library/{key}").json()["kind"] == "index"
    assert (env.app.state.community.indexes / (key[len("community--"):] + ".json")).exists()
    got = offer(env)
    assert got["installed"] == key and got["update"] is False


def test_install_never_replaces_a_default_the_person_chose(env):
    turn_on(env, URL)
    made = env.post(f"/api/catalog/books/{env.key}/handmade", json={}).json()["key"]
    assert env.put(f"/api/catalog/books/{env.key}/default", json={"version": made}).status_code == 200
    fetch(env)
    r = install(env)
    assert r.json()["madeDefault"] is False
    assert env.get(f"/api/catalog/books/{env.key}").json()["default"] == made


def test_a_newer_version_updates_and_keeps_the_old_file(env, tmp_path, epub_path):
    turn_on(env, URL)
    fetch(env)
    first = install(env).json()["key"]
    # the publisher shares a second version
    doc = good_doc(env)
    doc["provenance"]["updated"] = "2099-01-01T00:00:00Z"      # newer than the first, which was made today
    env.reg.add(bundle_of(doc, epub_path), now=LATER)
    env.app.state.community.now = lambda: 10 ** 10
    got = offer(env)
    assert got["version"] == 2 and got["installed"] == first and got["update"] is True
    assert fetch(env, got).status_code == 200
    second = install(env, got).json()
    assert second["madeDefault"] is True and second["key"].endswith("--v2.lore")
    entry = env.get(f"/api/catalog/books/{env.key}").json()
    assert entry["default"] == second["key"] and {v["key"] for v in entry["versions"]} == {first, second["key"]}
    again = offer(env)
    assert again["installed"] == second["key"] and again["update"] is False
    # removing the new one falls back to the old one
    assert env.delete(f"/api/community/installed/{second['key']}").status_code == 200
    entry = env.get(f"/api/catalog/books/{env.key}").json()
    assert entry["default"] == first and [v["key"] for v in entry["versions"]] == [first]


def test_uninstall_removes_the_file_and_the_record(env):
    turn_on(env, URL)
    fetch(env)
    key = install(env).json()["key"]
    path = env.app.state.community.indexes / (key[len("community--"):] + ".json")
    assert path.exists()
    assert env.delete(f"/api/community/installed/{key}").json() == {"ok": True}
    assert not path.exists()
    assert env.get(f"/api/catalog/books/{env.key}").json()["versions"] == []
    assert env.app.state.community.catalog.state()["community"]["installed"] == {}
    assert env.delete(f"/api/community/installed/{key}").status_code == 404
    assert env.delete("/api/community/installed/generated--x").status_code == 404
    assert books(env)["results"][0]["installed"] is None


def test_uninstall_only_touches_installed_community_files(env, tmp_path):
    turn_on(env, URL)
    made = env.post(f"/api/catalog/books/{env.key}/handmade", json={}).json()["key"]
    assert env.delete(f"/api/community/installed/{made}").status_code == 404
    assert env.get(f"/api/library/{made}").status_code == 200


# ---- a community index is read-only ---------------------------------------------------------

def test_a_community_index_cannot_be_edited_or_merged(env):
    turn_on(env, URL)
    fetch(env)
    key = install(env).json()["key"]
    for method, url, body in (
            ("post", f"/api/library/{key}/edits", {"op": "set-complete", "complete": False}),
            ("post", f"/api/library/{key}/merge", {"pairs": [{"keep": "a", "drop": "b"}]}),
            ("post", f"/api/library/{key}/tidy-names", {}),
            ("post", f"/api/library/{key}/duplicates/check", {}),
            ("post", f"/api/library/{key}/distinct", {"a": "a", "b": "b"}),
            ("post", f"/api/library/{key}/undo/abc", {})):
        r = getattr(env, method)(url, json=body)
        assert r.status_code == 409, (url, r.status_code, r.text)
        assert "read-only" in r.json()["detail"]


# ---- sharing --------------------------------------------------------------------------------

@pytest.fixture
def mine(env):
    """A hand-made, complete index of the book, built here."""
    key = env.post(f"/api/catalog/books/{env.key}/handmade", json={"license": "CC-BY-4.0"}).json()["key"]
    assert env.post(f"/api/library/{key}/edits", json={"op": "set-complete", "complete": True}).status_code == 200
    return key


def check(env, key):
    r = env.post(f"/api/community/share/{key}/check")
    assert r.status_code == 200, r.text
    return r.json()


def states(got):
    return {i["id"]: i["state"] for i in got["items"]}


def test_share_check_passes_a_sound_complete_index(env, mine):
    got = check(env, mine)
    assert got["ok"] is True and got["license"] == "CC-BY-4.0" and got["bundleBytes"] > 0
    assert states(got) == {"validates": "pass", "complete": "pass", "book-checks": "pass", "spoiler-gate": "pass",
                           "license": "pass", "audit": "todo"}
    assert next(i for i in got["items"] if i["id"] == "audit")["text"] == "Needs a cloud model key (Settings)"


def test_partial_is_a_warning_not_a_failure(env):
    key = env.post(f"/api/catalog/books/{env.key}/handmade", json={}).json()["key"]
    got = check(env, key)
    item = next(i for i in got["items"] if i["id"] == "complete")
    assert got["ok"] is True and item["state"] == "warn"
    assert item["text"] == "Covers part of the book. It will be listed as partial."


def test_a_licence_outside_the_two_fails_and_can_be_set(env, mine, tmp_path):
    path = next((tmp_path / "data" / "indexes").glob("*.lore.json"))
    doc = json.loads(path.read_text())
    doc["provenance"]["license"] = "All-Rights-Reserved"
    path.write_text(json.dumps(doc))
    got = check(env, mine)
    assert got["ok"] is False and states(got)["license"] == "fail" and got["bundleBytes"] is None
    assert got["licenses"] == ["CC0-1.0", "CC-BY-4.0"]
    assert env.get(f"/api/community/share/{mine}/bundle").status_code == 422
    bad = env.post(f"/api/library/{mine}/edits", json={"op": "set-license", "license": "MIT"})
    assert bad.status_code == 400
    ok = env.post(f"/api/library/{mine}/edits", json={"op": "set-license", "license": "CC0-1.0"})
    assert ok.status_code == 200, ok.text
    assert check(env, mine)["ok"] is True
    assert env.post(f"/api/library/{mine}/undo/{ok.json()['undo']}").status_code == 200
    assert states(check(env, mine))["license"] == "fail"


def test_early_names_are_counted_not_named(env, mine, tmp_path):
    ed = env.post(f"/api/library/{mine}/edits", json={"op": "add-entity", "name": "Tom Marlow", "type": "person", "seq": 1})
    assert ed.status_code == 200, ed.text
    got = check(env, mine)
    gate = next(i for i in got["items"] if i["id"] == "spoiler-gate")
    assert gate["state"] == "warn" and got["ok"] is True
    assert "Tom" not in json.dumps(got)
    assert gate["text"] == "1 name is shown before the book uses it."


def test_the_bundle_is_accepted_by_the_registry(env, mine, tmp_path):
    r = env.get(f"/api/community/share/{mine}/bundle")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="') and "lorekeeper-share.json" in r.headers["content-disposition"]
    p = tmp_path / "got.json"
    p.write_bytes(r.content)
    ctx, issues = registry.check(str(p), "me", tmp_path / "other-registry")
    assert ctx is not None, [str(i) for i in issues]
    assert "label" not in json.dumps(json.loads(r.content)["index"]["editions"])


def test_only_indexes_built_here_can_be_shared(env):
    turn_on(env, URL)
    fetch(env)
    key = install(env).json()["key"]
    for route in ("check", "audit"):
        assert env.post(f"/api/community/share/{key}/{route}").status_code == 409
    assert env.get(f"/api/community/share/{key}/bundle").status_code == 409
    assert env.post("/api/community/share/generated--nothing/check").status_code == 404


def test_a_series_book_is_refused_for_now(tmp_path):
    meta = ('<meta property="belongs-to-collection">The Beacons</meta>'
            '<meta property="group-position">1</meta>')
    path = make_epub(tmp_path / "s.epub", series_meta=meta)
    app = new_app(tmp_path, "data")
    with TestClient(app, headers=HEADERS) as c:
        book = upload(c, path)
        key = c.post(f"/api/catalog/books/up:{book['id']}/handmade", json={}).json()["key"]
        r = c.post(f"/api/community/share/{key}/check")
        assert r.status_code == 409 and r.json()["code"] == "series"
        assert c.get(f"/api/community/share/{key}/bundle").status_code == 409


def fake_audit(index, blocks, llm, n=50, seed=0):
    fake_audit.calls.append((n, seed, llm))
    return {"sampled": 10, "supported": 8, "unsupported": 1, "unsure": 1, "model": "fake-model"}


fake_audit.calls = []


def test_audit_a_share_and_stale_when_the_index_changes(env, mine):
    com = env.app.state.community
    r = env.post(f"/api/community/share/{mine}/audit")
    assert r.status_code in (409, 501)               # no cloud key configured, or no audit module yet
    if r.status_code == 409:
        assert r.json()["code"] == "needs-cloud-model"
    com._audit, com.model = fake_audit, lambda: "model"
    got = env.post(f"/api/community/share/{mine}/audit")
    assert got.status_code == 200, got.text
    assert got.json()["sampled"] == 10 and got.json()["supported"] == 8 and got.json()["unsupported"] == 1 and got.json()["unsure"] == 1 and got.json()["model"] == "fake-model"
    assert fake_audit.calls[-1][:2] == (50, 0)
    item = next(i for i in check(env, mine)["items"] if i["id"] == "audit")
    assert item == {"id": "audit", "state": "pass", "text": "8 of 9 claims held up. 1 could not be judged from the passage cited."}
    bundle = json.loads(env.get(f"/api/community/share/{mine}/bundle").content)
    att = [a for a in bundle["attestations"] if a["kind"] == "audit"]
    assert len(att) == 1 and att[0]["by"] == "builder" and att[0]["supported"] == 8 and att[0].get("unsupported", 1) == 1 and att[0]["model"] == "fake-model"
    assert registry.errors_of(registry.validate.check_schema(bundle, "share")) == []
    # editing the index makes the stored audit stale
    assert env.post(f"/api/library/{mine}/edits", json={"op": "add-entity", "name": "Tom Marlow", "type": "person",
                                                        "seq": 1}).status_code == 200
    assert next(i for i in check(env, mine)["items"] if i["id"] == "audit")["state"] == "todo"
    bundle = json.loads(env.get(f"/api/community/share/{mine}/bundle").content)
    assert not [a for a in bundle["attestations"] if a["kind"] == "audit"]


def test_audit_cost_estimate_follows_the_model(env, mine):
    com = env.app.state.community
    com.plan = lambda: {"local": False, "label": "Claude Sonnet 5.5", "price": (2.0, 10.0)}
    text = next(i for i in check(env, mine)["items"] if i["id"] == "audit")["text"]
    assert text.endswith("cents") and text.startswith("Check ")
    com.plan = lambda: {"local": False, "label": "x", "price": None}
    assert next(i for i in check(env, mine)["items"] if i["id"] == "audit")["text"].endswith("price unknown")
    com.plan = lambda: None            # only Ollama: no audit offered
    item = next(i for i in check(env, mine)["items"] if i["id"] == "audit")
    assert item == {"id": "audit", "state": "todo", "text": "Needs a cloud model key (Settings)"}


def test_the_reader_can_check_a_preview_and_it_shows_as_by_you(env):
    turn_on(env, URL)
    com = env.app.state.community
    com._audit, com.model = fake_audit, lambda: "model"
    d = fetch(env).json()["digest"]
    r = env.post("/api/community/audit", json={"digest": d})
    assert r.status_code == 200, r.text
    assert (com.dir / "cache" / f"{d.split(':')[1]}.audit.json").exists()
    assert offer(env)["checked"] == {"kind": "audit", "by": "you", "sampled": 10, "supported": 8, "unsupported": 1, "unsure": 1, "model": "fake-model"}
    assert env.post("/api/community/audit", json={"digest": "sha256:" + "2" * 64}).status_code == 404


def test_no_model_is_a_clear_refusal(env):
    turn_on(env, URL)
    com = env.app.state.community
    com._audit, com.model = fake_audit, lambda: None
    d = fetch(env).json()["digest"]
    r = env.post("/api/community/audit", json={"digest": d})
    assert r.status_code == 409 and r.json()["code"] == "needs-cloud-model"


# ---- sign-in and CSRF -----------------------------------------------------------------------

def test_routes_need_a_session_and_the_header(tmp_path):
    app = web_app.create_app(tmp_path / "d", [], [], allowed_hosts=("testserver",), auth=True,
                             initial_password="correct horse battery", log=lambda *a: None)
    with TestClient(app) as c:
        assert c.get("/api/community").status_code == 401
        assert c.put("/api/community", json={"enabled": True}, headers=HEADERS).status_code == 401
        assert c.put("/api/community", json={"enabled": True}).status_code == 403


# ---- the Dracula example --------------------------------------------------------------------

DRACULA = CORPUS / "pg345" / "pg345.epub"


@pytest.mark.skipif(not DRACULA.exists(), reason="corpus/pg345 is not downloaded")
def test_share_check_on_dracula_passes_every_item(tmp_path):
    app = new_app(tmp_path, "data")
    with TestClient(app, headers=HEADERS) as c:
        book = upload(c, DRACULA)
        out = tmp_path / "data" / "indexes"
        out.mkdir(parents=True, exist_ok=True)
        doc = json.loads((ROOT / "examples" / "dracula" / "dracula.json").read_text())
        (out / "dracula.lore.json").write_text(json.dumps(doc))
        assert doc["editions"][0]["textHash"] == book["textHash"]
        got = c.post("/api/community/share/generated--dracula.lore/check").json()
        assert got["ok"] is True, got
        assert {i["id"]: i["state"] for i in got["items"] if i["id"] != "audit"} == {
            "validates": "pass", "complete": "pass", "book-checks": "pass", "spoiler-gate": "pass", "license": "pass"}
        r = c.get("/api/community/share/generated--dracula.lore/bundle")
        p = tmp_path / "b.json"
        p.write_bytes(r.content)
        ctx, issues = registry.check(str(p), "someone", tmp_path / "reg")
        assert ctx is not None, [str(i) for i in issues]


def test_nothing_audits_without_a_cloud_model(env):
    turn_on(env, URL)
    d = fetch(env).json()["digest"]
    assert env.app.state.community.plan() is None          # no key is configured in tests
    r = env.post("/api/community/audit", json={"digest": d})
    assert r.status_code == 409 and r.json()["code"] == "needs-cloud-model"


def test_a_community_index_ranks_last_on_ties(env):
    turn_on(env, URL)
    made = env.post(f"/api/catalog/books/{env.key}/handmade", json={}).json()["key"]
    env.post(f"/api/library/{made}/edits", json={"op": "set-complete", "complete": True})
    fetch(env)
    com = install(env).json()["key"]
    entry = env.get(f"/api/catalog/books/{env.key}").json()
    assert {v["key"] for v in entry["versions"]} == {made, com}
    assert entry["default"] == made


# ---- untrusted audit counts, ranking, wording ------------------------------------------------


def test_impossible_audit_counts_are_ignored_and_never_crash(env, monkeypatch):
    c = env.app.state.community
    bad = [audit(60, 50), audit(0, 0), {**audit(10, 50), "unsupported": 30, "unsure": 30}]
    for i, att in enumerate(bad):
        monkeypatch.setattr(c, "lookup", lambda th, att=att: [listing(1, attestations=[att])])
        assert c.results("t")[0]["checked"] == {"kind": None, "by": None}, i
    assert community_mod.wilson_lower(60, 50) <= 1 and community_mod.wilson_lower(-3, 50) == 0
    assert community_mod.wilson_lower(5, 0) == 0 and community_mod.wilson_lower("x", 5) == 0


def test_a_bad_audit_does_not_hide_an_older_sound_one(env, monkeypatch):
    c = env.app.state.community
    good = {**audit(40, 50), "at": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(c, "lookup", lambda th: [listing(1, attestations=[good, audit(60, 50)])])
    assert c.results("t")[0]["checked"]["supported"] == 40


def test_the_books_route_drops_one_broken_listing_not_the_answer(env, monkeypatch):
    turn_on(env, URL)
    c = env.app.state.community
    broken = listing(1)
    del broken["listing"]["work"]
    monkeypatch.setattr(c, "lookup", lambda th: [broken, listing(2)])
    got = books(env)
    assert got["state"] == "ok" and [r["publisher"] for r in got["results"]] == ["p2"]


def test_ranking_uses_supported_over_judged_so_unsure_does_not_hurt(env, monkeypatch):
    c = env.app.state.community
    many_unsure = {**audit(24, 50), "unsupported": 0, "unsure": 26}          # 24 of 24 judged
    mixed = {**audit(40, 50), "unsupported": 10}                            # 40 of 50
    nothing = {**audit(0, 10), "unsupported": 0, "unsure": 10}              # nothing judged: as no audit
    rows = [listing(1, attestations=[nothing]), listing(2, attestations=[mixed]),
            listing(3, attestations=[many_unsure])]
    monkeypatch.setattr(c, "lookup", lambda th: rows)
    assert [r["publisher"] for r in c.results("t")] == ["p3", "p2", "p1"]


def test_missing_counts_are_the_rest_of_the_sample_and_weak_audits_rank_last(env, monkeypatch):
    c = env.app.state.community
    judged = community_mod._judged
    assert judged({"sampled": 50, "supported": 30}) == (30, 50)
    assert judged({"sampled": 50, "supported": 30, "unsure": 10}) == (30, 40)
    thirty = audit(30, 50)                                                  # no unsupported: 30 of 50, bound below 0.5
    zero = {**audit(0, 50), "unsupported": 50}
    none, good = listing(3), listing(4, attestations=[audit(48, 50)])
    rows = [listing(1, attestations=[thirty]), listing(2, attestations=[zero]), none, good]
    monkeypatch.setattr(c, "lookup", lambda th: rows)
    got = [r["publisher"] for r in c.results("t")]
    assert got[:2] == ["p4", "p3"] and set(got[2:]) == {"p1", "p2"}
    assert got.index("p1") < got.index("p2")                                # 30 of 50 above 0 of 50


def test_the_audit_line_counts_judged_claims_and_names_the_unsure():
    line = community_mod.Community._audit_line
    assert line({"sampled": 50, "supported": 24, "unsupported": 0, "unsure": 26}) == \
        "24 of 24 claims held up. 26 could not be judged from the passage cited."
    assert line({"sampled": 50, "supported": 48, "unsupported": 2}) == "48 of 50 claims held up."


def test_install_file_names_carry_the_registry(env):
    turn_on(env, URL)
    fetch(env)
    key = install(env).json()["key"]
    assert key.startswith("community--registry-test-reg--someone--")
    turn_on(env, "https://other.test/reg")
    other = env.app.state.community._file_name("https://other.test/reg", "someone", "dracula", 1)
    assert other != key[len("community--"):] + ".json" and other.startswith("other-test-reg--")


def test_problems_name_where_never_the_quoted_book_text():
    from lorekeeper.validate import Issue
    d = community_mod.Community._describe(Issue("error", "facets[3].text",
                                                "quotes more than 8 consecutive words: 'the quick brown fox jumps over the lazy dog'"))
    assert d == "facets[3].text: quotes too much of the book" and "quick" not in d


def test_default_fetch_has_an_overall_deadline(monkeypatch):
    import time
    import urllib.request

    class Slow:
        headers, status = {}, 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            time.sleep(0.05)
            return b"x" * 10            # a trickle: each read is quick, the download never ends

    class Opener:
        def open(self, req, timeout=None):
            return Slow()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    monkeypatch.setattr(community_mod, "TIMEOUT", 0.3)
    t = time.monotonic()
    with pytest.raises(community_mod.FetchError, match="too long"):
        community_mod.default_fetch("https://registry.test/reg/x")
    assert time.monotonic() - t < 2


def test_installs_from_another_registry_are_not_updates(env):
    turn_on(env, URL)
    fetch(env)
    first = install(env).json()["key"]
    assert offer(env)["installed"] == first
    turn_on(env, "https://other.test/reg")
    com = env.app.state.community
    assert com._installed() == {}                       # the install belongs to the first registry
    turn_on(env, URL)
    assert com._installed()


def test_simultaneous_cache_writes_do_not_collide(tmp_path):
    import threading
    com = community_mod.Community(tmp_path, None, None)
    errors = []

    def go(i):
        try:
            for _ in range(30):
                com._write(tmp_path / "community" / "lookup" / "x.json", str(i).encode())
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and not list((tmp_path / "community" / "lookup").glob("*.tmp"))


# ---- round 4 ----------------------------------------------------------------------------------


def test_fetch_checks_the_deadline_after_every_recv(monkeypatch):
    import time
    import urllib.request

    class Drip:
        headers, status = {}, 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            raise AssertionError("read() would wait for a full buffer: read1 must be used")

        def read1(self, n):
            time.sleep(0.05)
            return b"x"

    class Opener:
        def open(self, req, timeout=None):
            return Drip()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    monkeypatch.setattr(community_mod, "TIMEOUT", 0.3)
    t = time.monotonic()
    with pytest.raises(community_mod.FetchError, match="too long"):
        community_mod.default_fetch("https://registry.test/reg/x")
    assert time.monotonic() - t < 2


def test_a_bad_status_line_is_unreachable_and_serves_the_stale_lookup(env):
    import http.client
    turn_on(env, URL)
    now = [1000.0]
    env.app.state.community.now = lambda: now[0]
    assert len(books(env)["results"]) == 1
    now[0] += 5000
    env.reg.override[listing_url(env)] = http.client.BadStatusLine("garbage")
    got = books(env)
    assert got["state"] == "ok" and len(got["results"]) == 1
    # and with nothing cached it is unreachable, not a 500
    env.app.state.community._lookup_file(env.th).unlink()
    assert books(env) == {"state": "unreachable", "results": []}


def test_default_fetch_turns_http_errors_into_fetch_errors(monkeypatch):
    import http.client
    import urllib.request

    class Opener:
        def open(self, req, timeout=None):
            raise http.client.BadStatusLine("x")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(community_mod.FetchError):
        community_mod.default_fetch("https://registry.test/reg/x")


def test_a_lone_install_is_not_pinned_and_a_built_index_wins_later(env):
    turn_on(env, URL)
    fetch(env)
    key = install(env).json()["key"]
    assert env.app.state.community.catalog.state()["defaults"].get(env.key) is None
    assert env.get(f"/api/catalog/books/{env.key}").json()["default"] == key
    made = env.post(f"/api/catalog/books/{env.key}/handmade", json={}).json()["key"]
    assert env.post(f"/api/library/{made}/edits", json={"op": "set-complete", "complete": True}).status_code == 200
    entry = env.get(f"/api/catalog/books/{env.key}").json()
    assert entry["default"] == made


def test_saving_the_address_does_not_answer_the_opt_in_question(env):
    assert env.get("/api/community").json()["enabled"] is None
    got = env.put("/api/community", json={"url": "https://other.test/reg"})
    assert got.status_code == 200 and got.json()["enabled"] is None and got.json()["url"] == "https://other.test/reg"
    assert books(env)["state"] == "ask"
    assert env.put("/api/community", json={}).status_code == 400
    assert env.put("/api/community", json={"enabled": True}).json()["enabled"] is True
    assert env.put("/api/community", json={"url": URL}).json()["enabled"] is True      # on stays on


def test_install_runs_off_the_event_loop():
    import inspect
    src = inspect.getsource(web_app.create_app)
    assert "asyncio.to_thread(community.install" in src


def test_the_estimate_is_computed_from_the_real_prompts(env, mine):
    com = env.app.state.community
    item = com.library.scan()[mine]
    blocks = com.reader(com._edition_of(item["doc"])[1]).blocks
    com.plan = lambda: {"price": [3.0, 15.0]}
    cheap = com._estimate(item["doc"], blocks)
    com.plan = lambda: {"price": [30.0, 150.0]}
    dear = com._estimate(item["doc"], blocks)
    com.plan = lambda: {"price": None}
    unknown = com._estimate(item["doc"], blocks)
    assert unknown.endswith("price unknown") and "cents" in cheap and "cents" in dear
    assert "cents" not in unknown


def test_a_licence_change_is_not_a_hand_edit_and_keeps_the_audit(env, mine, tmp_path):
    com = env.app.state.community
    com._audit, com.model = fake_audit, lambda: "model"
    assert env.post(f"/api/community/share/{mine}/audit").status_code == 200
    item = com.library.scan()[mine]
    before = item["doc"]["provenance"]
    sidecar = com.dir.parent / "edits" / f"{env.th}.json"
    edits = sidecar.read_text() if sidecar.exists() else None
    got = env.post(f"/api/library/{mine}/edits", json={"op": "set-license", "license": "CC0-1.0"})
    assert got.status_code == 200
    prov = env.get(f"/api/library/{mine}").json()["provenance"]
    assert prov["license"] == "CC0-1.0" and prov["updated"] >= before["updated"]
    assert prov["contributors"] == before["contributors"]
    assert (sidecar.read_text() if sidecar.exists() else None) == edits          # nothing to replay
    assert next(i for i in check(env, mine)["items"] if i["id"] == "audit")["state"] == "pass"
    assert env.post(f"/api/library/{mine}/undo/{got.json()['undo']}").status_code == 200
    assert env.get(f"/api/library/{mine}").json()["provenance"]["license"] == before["license"]


def test_a_content_edit_still_stales_the_audit(env, mine):
    com = env.app.state.community
    com._audit, com.model = fake_audit, lambda: "model"
    env.post(f"/api/community/share/{mine}/audit")
    env.post(f"/api/library/{mine}/edits", json={"op": "set-complete", "complete": False})
    assert next(i for i in check(env, mine)["items"] if i["id"] == "audit")["state"] == "pass"   # coverage is not audited content
    assert env.post(f"/api/library/{mine}/edits", json={"op": "add-entity", "type": "person", "name": "Mina Murray",
                                                        "seq": 1}).status_code == 200
    assert next(i for i in check(env, mine)["items"] if i["id"] == "audit")["state"] == "todo"


def test_fetch_reuses_the_cached_download_and_still_checks(env):
    turn_on(env, URL)
    d = serve_index(env, registry.dumps(good_doc(env)).encode())
    res = {"id": offer(env)["id"], "digest": d}
    assert fetch(env, res).status_code == 200
    n = len(env.reg.calls)
    again = fetch(env, res)
    assert again.status_code == 200 and len(env.reg.calls) == n         # not downloaded twice
    cache = env.app.state.community._cache_file(d)
    cache.write_bytes(b"{}")                                            # damaged on disk: fetched again
    assert fetch(env, res).status_code == 200 and len(env.reg.calls) == n + 1


def test_install_refuses_a_cache_file_that_no_longer_hashes(env):
    turn_on(env, URL)
    fetch(env)
    res = offer(env)
    env.app.state.community._cache_file(res["digest"]).write_bytes(b"{}")
    assert install(env, res).status_code == 409


def test_the_cache_keeps_the_twenty_most_recent(tmp_path):
    com = community_mod.Community(tmp_path, None, None)
    folder = tmp_path / "community" / "cache"
    folder.mkdir(parents=True)
    import os
    for i in range(25):
        f = folder / f"{i:064x}.lore.json"
        f.write_text("{}")
        (folder / f"{i:064x}.audit.json").write_text("{}")
        os.utime(f, (1000 + i, 1000 + i))
    com._prune_cache()
    left = sorted(p.name for p in folder.glob("*.lore.json"))
    assert len(left) == 20 and left[0] == f"{5:064x}.lore.json"
    assert len(list(folder.glob("*.audit.json"))) == 20


def test_an_index_with_two_built_from_editions_is_refused(env):
    turn_on(env, URL)
    for case in (0, 1):
        env.app.state.community.now = lambda: 10 ** 10 + case * 10_000
        doc = good_doc(env)
        other = {"textHash": "e" * 64, "builtFrom": True}
        doc["editions"] = [doc["editions"][0], other] if case == 0 else [other, doc["editions"][0]]
        d = serve_index(env, registry.dumps(doc).encode())
        res = {"id": offer(env)["id"], "digest": d}
        r = fetch(env, res)
        assert r.status_code == 422 and "not made for this copy" in r.json()["detail"], case
        assert not env.app.state.community._cache_file(d).exists()
    # one built-from edition plus an unrelated one that is not built from: fine
    env.app.state.community.now = lambda: 10 ** 10 + 50_000
    doc = good_doc(env)
    doc["editions"].append({"textHash": "e" * 64, "builtFrom": False})
    d = serve_index(env, registry.dumps(doc).encode())
    assert fetch(env, {"id": offer(env)["id"], "digest": d}).status_code == 200


def test_a_refused_index_is_removed_from_the_cache(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["anchors"][0]["seq"] = doc["anchors"][0]["seq"] + 1
    body = registry.dumps(doc).encode()
    d = serve_index(env, body)
    com = env.app.state.community
    com._write(com._cache_file(d), body)                 # cached before the check existed
    com._write(com._audit_file(d), b"{}")
    res = {"id": offer(env)["id"], "digest": d}
    assert fetch(env, res).status_code == 422
    assert not com._cache_file(d).exists() and not com._audit_file(d).exists()
    r = install(env, res)
    assert r.status_code == 409 and r.json()["code"] == "not-fetched"


def test_uninstall_still_removes_an_install_made_under_the_port_less_name(env):
    url = "http://localhost:8123/reg"
    com = env.app.state.community
    old = com._file_name(url, "someone", "tale", 1, legacy=True)
    assert old != com._file_name(url, "someone", "tale", 1)
    key = f"community--{old[:-len('.json')]}"
    com.indexes.mkdir(parents=True, exist_ok=True)
    (com.indexes / old).write_text("{}")

    def apply(st):
        st.setdefault("community", {}).setdefault("installed", {})[key] = {
            "listing": "someone/tale", "version": 1, "registry": url}
    com.catalog.update_state(apply)
    assert env.delete(f"/api/community/installed/{key}").json() == {"ok": True}
    assert not (com.indexes / old).exists()


def test_resolve_uses_the_one_confinement_rule():
    assert community_mod.inside(URL, URL + "/v0/x") and not community_mod.inside(URL, URL + "/../x")
    assert not hasattr(community_mod, "_SameOrigin") and not hasattr(community_mod, "_Tracked")


def test_install_checks_the_cached_document_again(env):
    turn_on(env, URL)
    doc = good_doc(env)
    doc["anchors"][0]["seq"] = doc["anchors"][0]["seq"] + 1
    body = registry.dumps(doc).encode()
    d = serve_index(env, body)
    com = env.app.state.community
    com._write(com._cache_file(d), body)                 # cached before the check existed
    res = {"id": offer(env)["id"], "digest": d}
    r = install(env, res)
    assert r.status_code == 422 and r.json()["code"] == "invalid"
    assert not com._cache_file(d).exists() and not list(com.indexes.glob("*")) if com.indexes.exists() else True
    assert env.app.state.community.catalog.state().get("community", {}).get("installed", {}) == {}
