"""GET /api/library/{key}/mentions: counted from the book's text, gated at the position."""

import pytest

pytest.importorskip("fastapi")

from .conftest import CORPUS, EXAMPLES  # noqa: E402
from .test_editor import edit  # noqa: E402
from .test_web import client, upload  # noqa: E402, F401

# The Lighthouse: seq 0 "Chapter One" heading, 1 Alice arrives, 2 her brother Tom, 3 "The keeper, a quiet
# man named Ezra Pike", 4 "Yes.", 5 the letter, 6 "Chapter Two" heading, 7 "Pike admitted...", 8 Tom left by boat.


def setup(client, tmp_path):
    key = book_with(client, tmp_path, front=False)
    book = None
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=1, desc="A young woman who arrives at a lighthouse.")
    edit(client, key, op="add-entity", name="Tom Marlow", type="person", seq=2)
    edit(client, key, op="add-name", entity="tom-marlow", value="Tom", kind="alias", seq=2)
    edit(client, key, op="add-entity", name="Ezra Pike", type="person", seq=3, desc="The keeper of the lighthouse.")
    # Two names for one person; the second is revealed later than the text that uses it.
    edit(client, key, op="add-name", entity="ezra-pike", value="keeper", kind="descriptive", seq=5)
    edit(client, key, op="add-name", entity="ezra-pike", value="Pike", kind="alias", seq=7)
    return book, key


def get(client, key, entity, seq):
    return client.get(f"/api/library/{key}/mentions", params={"entity": entity, "seq": seq})


def test_chapters_titles_and_counts_stop_at_the_position(client, tmp_path):
    _, key = setup(client, tmp_path)
    r = get(client, key, "alice-marlow", 5).json()
    assert r["total"] == 2
    # "Alice Marlow" (block 1) and the given name "Alice" (block 5), derived from the visible name.
    assert r["chapters"] == [{"n": 1, "title": "Chapter One", "count": 2}]      # chapter two: not even its title
    assert r["current"]["n"] == 1 and r["current"]["count"] == 2
    assert r["first"]["n"] == 1 and r["latest"]["n"] == 1 and r["busiest"] == {"n": 1, "title": "Chapter One", "count": 2}
    assert [(s["seq"], s["text"][s["start"]:s["end"]]) for s in r["current"]["snippets"]] == \
        [(1, "Alice Marlow"), (5, "Alice")]
    assert "Chapter Two" not in str(r)


def test_the_current_chapter_counts_only_through_the_position(client, tmp_path):
    _, key = setup(client, tmp_path)
    # Tom: "Tom Marlow" (2) and "Tom" (3) in chapter one; "Tom" in blocks 7 and 8 of chapter two.
    r = get(client, key, "tom-marlow", 5).json()
    assert [c["count"] for c in r["chapters"]] == [2] and r["current"]["n"] == 1
    r = get(client, key, "tom-marlow", 7).json()                       # through block 7, not 8
    assert [c["count"] for c in r["chapters"]] == [2, 1] and [s["seq"] for s in r["current"]["snippets"]] == [7]
    assert r["busiest"] == {"n": 1, "title": "Chapter One", "count": 2} and r["latest"]["n"] == 2
    r = get(client, key, "tom-marlow", 8).json()
    assert [c["count"] for c in r["chapters"]] == [2, 2] and [s["seq"] for s in r["current"]["snippets"]] == [7, 8]
    assert r["busiest"]["n"] == 1                                      # a tie goes to the earlier chapter


def test_nothing_before_the_first_mention(client, tmp_path):
    _, key = setup(client, tmp_path)
    r = get(client, key, "alice-marlow", 7).json()                     # fine: she was named in chapter one
    assert r["first"]["n"] == 1
    # Before Ezra's own first block nobody can ask about him.
    assert get(client, key, "ezra-pike", 3).status_code == 404
    r = get(client, key, "ezra-pike", 4).json()
    assert r["total"] == 2 and r["first"]["n"] == 1


def test_a_name_revealed_later_is_not_counted_earlier(client, tmp_path):
    _, key = setup(client, tmp_path)
    # "keeper" is a name of the entity from seq 5 (a hidden identity): its use in block 3 must not count while hidden.
    before = get(client, key, "ezra-pike", 5).json()
    assert before["chapters"] == [{"n": 1, "title": "Chapter One", "count": 1}]
    assert [s["text"][s["start"]:s["end"]] for s in before["current"]["snippets"]] == ["Ezra Pike"]
    after = get(client, key, "ezra-pike", 6).json()                    # block 5 is now finished: "keeper" is visible
    assert after["chapters"][0]["count"] == 2
    # "Pike" is the surname of the display name "Ezra Pike", visible from the start: a derived form, so block 7 counts
    # at seq 7 (inclusive) although the alias "Pike" is only revealed after block 7.
    assert get(client, key, "ezra-pike", 7).json()["chapters"][1]["count"] == 1


def test_an_entry_whose_every_form_is_revealed_later_is_never_counted_before_the_reveal(client, tmp_path):
    _, key = setup(client, tmp_path)
    # A stranger known only as "keeper"; the name "Ezra Pike" (block 3, same block) is revealed at block 5.
    e = edit(client, key, op="add-entity", name="keeper", type="person", seq=3)
    edit(client, key, op="add-name", entity=e["id"], value="Ezra Pike", kind="alias", seq=5)
    r = get(client, key, e["id"], 5).json()
    assert r["chapters"][0]["count"] == 1
    assert [s["text"][s["start"]:s["end"]] for s in r["current"]["snippets"]] == ["keeper"]   # never "Ezra Pike", "Pike", "Ezra"
    r = get(client, key, e["id"], 6).json()                            # revealed: both names, and their forms, count
    assert r["chapters"][0]["count"] >= 2


def test_not_found_cases(client, tmp_path):
    book, key = setup(client, tmp_path)
    assert get(client, "nope", "alice-marlow", 5).status_code == 404
    assert get(client, key, "no-such-entry", 5).status_code == 404
    assert get(client, key, "alice-marlow", 999).status_code == 422
    # Without the EPUB on disk there is nothing to count in.
    for p in (tmp_path / "data").rglob("*.epub"):
        p.unlink()
    assert get(client, key, "alice-marlow", 5).status_code == 404


# ---- chapters are the story chapters -------------------------------------------------------------------------

PAD = " The wind came off the water and the gulls wheeled over the rocks until the light began to fade at last."


def book_with(client, tmp_path, front):
    """The Lighthouse with chapter two long enough to count as story, and optionally a front-matter entry first."""
    from .test_generate import CH2, make_epub
    path = make_epub(tmp_path / "front.epub", ch2=[CH2[0] + PAD * 2, CH2[1]],
                     front=("Title Page", ["The Lighthouse, a novel in two chapters by A. Writer, printed for the first time.",
                                           "All rights reserved by the author and the publisher of this edition."]) if front else None)
    r = client.post("/api/books", files={"file": ("front.epub", path.read_bytes(), "application/epub+zip")})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/catalog/books/up:{r.json()['id']}/handmade", json={"license": "CC-BY-4.0"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def test_front_matter_is_not_a_chapter(client, tmp_path):
    key = book_with(client, tmp_path, front=True)
    # Front matter takes seq 0-2 (heading and two paragraphs); chapter one's heading is 3, Alice arrives at 4.
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=4, desc="A young woman who arrives at a lighthouse.")
    r = get(client, key, "alice-marlow", 6).json()
    assert r["total"] == 2 and [(c["n"], c["title"]) for c in r["chapters"]] == [(1, "Chapter One")]
    assert r["current"]["n"] == 1 and r["first"]["n"] == 1
    # Chapter two is numbered 2, not 3.
    r = get(client, key, "alice-marlow", 10).json()
    assert [(c["n"], c["title"]) for c in r["chapters"]] == [(1, "Chapter One"), (2, "Chapter Two")]
    assert r["total"] == 2


def test_a_position_in_front_matter_has_no_chapters(client, tmp_path):
    key = book_with(client, tmp_path, front=True)
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=4)
    # Nobody is visible at seq 1, so ask about an entry added at the title page itself.
    edit(client, key, op="add-entity", name="Writer", type="person", seq=1)
    r = get(client, key, "writer", 2)
    assert r.status_code == 200
    assert r.json() == {"total": 2, "chapters": [], "current": None, "first": None, "busiest": None, "latest": None}


@pytest.mark.skipif(not CORPUS.is_dir(), reason="needs corpus/pg244")
def test_study_in_scarlet_is_numbered_from_chapter_one(tmp_path):
    from fastapi.testclient import TestClient

    from lorekeeper import epub
    from lorekeeper.web import app as web_app
    from lorekeeper.web.jobs import mark_story

    path = CORPUS / "pg244" / "pg244.epub"
    if not path.exists():
        pytest.skip("needs corpus/pg244")
    book = epub.read(path)
    ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
    secs = [{"title": c.title or "", "words": sum(len(b.words) for b in book.blocks[c.start:e])}
            for c, e in zip(book.chapters, ends)]
    mark_story(secs)
    story = [i for i, s in enumerate(secs) if s["story"]]
    assert story[0] > 0                                               # there is front matter
    app = web_app.create_app(tmp_path / "data", [("examples", EXAMPLES.parent)], [CORPUS / "pg244"],
                             ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        key = "examples--sherlock-holmes--study-in-scarlet"
        seq = book.chapters[story[1]].start - 1                       # the last block of chapter one
        r = c.get(f"/api/library/{key}/mentions", params={"entity": "holmes", "seq": seq})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == len(story)
        assert body["chapters"][0]["n"] == 1 and body["chapters"][0]["title"].startswith("CHAPTER I.")
        assert [ch["n"] for ch in body["chapters"]] == list(range(1, len(body["chapters"]) + 1))
