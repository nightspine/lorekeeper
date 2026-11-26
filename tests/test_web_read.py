"""GET /api/library/{key}/read and /read/toc: the book's own text by chapter, for the reading view."""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import epub  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402

from .conftest import ROOT, corpus_epub  # noqa: E402
from .test_web import client  # noqa: E402, F401
from .test_web_mentions import book_with  # noqa: E402

# Index content that must never travel in a read reply (the browser joins text and index itself).
INDEX_KEYS = {"anchors", "entities", "names", "from", "facets", "events", "fp"}


def keys_in(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in keys_in(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys_in(v)}
    return set()


def the_epub(tmp_path) -> epub.Book:
    return epub.read(next((tmp_path / "data").rglob("*.epub")))


def test_toc_is_the_readers_chapter_map(client, tmp_path):
    key = book_with(client, tmp_path, front=True)
    book = the_epub(tmp_path)
    toc = client.get(f"/api/library/{key}/read/toc").json()
    meta = client.get(f"/api/editions/{json.loads(next((tmp_path / 'data').rglob('*.lore.json')).read_text())['editions'][0]['textHash']}").json()
    # Same numbering and titles as the chapter map the Reader's chapter select is built from.
    assert [(c["n"], c["title"], c["firstSeq"]) for c in toc["chapters"]] == [(c["n"], c["title"], c["start"]) for c in meta["chapters"]]
    assert [c["words"] for c in toc["chapters"]] == [c["words"] for c in meta["chapters"]]
    assert [c["story"] for c in toc["chapters"]] == [c["story"] for c in meta["chapters"]]
    assert toc["blocks"] == len(book.blocks) and toc["words"] == sum(len(b.words) for b in book.blocks)
    # The chapters cover every block once, in order.
    spans = [(c["firstSeq"], c["lastSeq"]) for c in toc["chapters"]]
    assert spans[0][0] == 0 and spans[-1][1] == len(book.blocks) - 1
    assert all(a[1] + 1 == b[0] for a, b in zip(spans, spans[1:]))


def test_a_chapter_is_exactly_the_epubs_blocks_in_its_range(client, tmp_path):
    key = book_with(client, tmp_path, front=True)
    book = the_epub(tmp_path)
    toc = client.get(f"/api/library/{key}/read/toc").json()["chapters"]
    for c in toc:
        r = client.get(f"/api/library/{key}/read", params={"chapter": c["n"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["chapter"] == {k: c[k] for k in ("n", "title", "firstSeq", "lastSeq")}
        want = book.blocks[c["firstSeq"]:c["lastSeq"] + 1]
        assert [b["seq"] for b in body["blocks"]] == [b.seq for b in want]
        assert [b["text"] for b in body["blocks"]] == [b.raw for b in want]
        i = c["n"] - 1
        assert body["prev"] == ({"n": toc[i - 1]["n"], "title": toc[i - 1]["title"]} if i else None)
        assert body["next"] == ({"n": toc[i + 1]["n"], "title": toc[i + 1]["title"]} if i + 1 < len(toc) else None)
        assert not keys_in(body) & INDEX_KEYS


def test_headings_are_marked_and_short_blocks_kept(client, tmp_path):
    key = book_with(client, tmp_path, front=False)
    body = client.get(f"/api/library/{key}/read", params={"chapter": 1}).json()
    # The Lighthouse: seq 0 is the "Chapter One" <h2>, seq 4 is "Yes." (too short to carry an anchor, still text).
    assert body["blocks"][0] == {"seq": 0, "text": "Chapter One", "heading": True}
    assert not any(b["heading"] for b in body["blocks"][1:])
    assert any(b["text"] == "Yes." for b in body["blocks"])


def test_seq_finds_the_chapter_holding_the_block(client, tmp_path):
    key = book_with(client, tmp_path, front=True)
    toc = client.get(f"/api/library/{key}/read/toc").json()["chapters"]
    for c in toc:
        for seq in {c["firstSeq"], c["lastSeq"], (c["firstSeq"] + c["lastSeq"]) // 2}:
            got = client.get(f"/api/library/{key}/read", params={"seq": seq}).json()
            assert got["chapter"]["n"] == c["n"]
            assert seq in [b["seq"] for b in got["blocks"]]


def test_refusals(client, tmp_path):
    key = book_with(client, tmp_path, front=False)
    last = client.get(f"/api/library/{key}/read/toc").json()["chapters"][-1]
    read = lambda **q: client.get(f"/api/library/{key}/read", params=q)  # noqa: E731
    assert read(chapter=0).status_code == 404 and read(chapter=0).json()["code"] == "no-chapter"
    assert read(chapter=last["n"] + 1).status_code == 404
    assert read(seq=-1).status_code == 422 and read(seq=last["lastSeq"] + 1).status_code == 422
    assert read().status_code == 422
    assert read(chapter="two").status_code == 422
    assert client.get("/api/library/nope/read", params={"chapter": 1}).json()["code"] == "not-found"
    assert client.get("/api/library/nope/read/toc").status_code == 404
    # Without the EPUB on disk there is no text to read.
    for p in (tmp_path / "data").rglob("*.epub"):
        p.unlink()
    for r in (read(chapter=1), client.get(f"/api/library/{key}/read/toc")):
        assert r.status_code == 404 and r.json()["code"] == "no-book-file"


def test_text_before_the_first_contents_entry_belongs_to_chapter_one(tmp_path):
    from lorekeeper.web import read as readmod
    book = epub.Book(blocks=[epub.Block(seq=i, spine=0, words=["w"] * (i + 1), raw=f"b{i}") for i in range(5)],
                     chapters=[epub.Chapter("One", 2), epub.Chapter("Two", 4)])
    spans = readmod.chapter_spans(book)
    assert [(c["n"], c["firstSeq"], c["lastSeq"], c["words"]) for c in spans] == [(1, 0, 3, 10), (2, 4, 4, 5)]
    # No table of contents: one chapter, named after the book.
    book.chapters, book.title = [], "Untitled"
    assert [(c["title"], c["firstSeq"], c["lastSeq"]) for c in readmod.chapter_spans(book)] == [("Untitled", 0, 4)]


def test_sign_in_is_required(tmp_path):
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9",
                             allowed_hosts=("testserver",), log=lambda *_: None)
    with TestClient(app) as c:
        for path in ("/api/library/x/read?chapter=1", "/api/library/x/read/toc"):
            r = c.get(path)
            assert r.status_code == 401 and r.json() == {"detail": "Sign in required"}


def test_dracula(tmp_path):
    path = corpus_epub("pg345/pg345.epub")
    app = web_app.create_app(tmp_path / "data", [("examples", ROOT / "examples" / "dracula")], [path.parent],
                             ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app) as c:
        key = next(i["key"] for i in c.get("/api/library").json()["indexes"] if i["source"] == "examples")
        book = epub.read(path)
        toc = c.get(f"/api/library/{key}/read/toc").json()["chapters"]
        assert len(toc) == len(book.chapters)
        ch = next(t for t in toc if t["title"].startswith("CHAPTER I "))
        assert ch["n"] == 4 and ch["firstSeq"] == 41   # the chapter select's numbering: Chapter I is toc entry 4
        assert toc[0]["firstSeq"] == 0                  # seq 0-6 come before the first entry and read as part of it
        body = c.get(f"/api/library/{key}/read", params={"chapter": ch["n"]}).json()
        assert [b["text"] for b in body["blocks"]] == [b.raw for b in book.blocks[ch["firstSeq"]:ch["lastSeq"] + 1]]
        assert c.get(f"/api/library/{key}/read", params={"seq": ch["firstSeq"] + 3}).json()["chapter"]["n"] == ch["n"]
