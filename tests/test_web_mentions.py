"""GET /api/library/{key}/mentions: counted from the book's text, gated at the position."""

import pytest

pytest.importorskip("fastapi")

from .test_editor import edit, handmade  # noqa: E402
from .test_web import client, upload  # noqa: E402, F401

# The Lighthouse: seq 0 "Chapter One" heading, 1 Alice arrives, 2 her brother Tom, 3 "The keeper, a quiet
# man named Ezra Pike", 4 "Yes.", 5 the letter, 6 "Chapter Two" heading, 7 "Pike admitted...", 8 Tom left by boat.


def setup(client, tmp_path):
    book, key = handmade(client, tmp_path)
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
    assert r["chapters"] == [{"n": 1, "title": "Chapter One", "count": 1}]      # chapter two: not even its title
    assert r["current"]["n"] == 1 and r["current"]["count"] == 1
    assert r["first"]["n"] == 1 and r["latest"]["n"] == 1 and r["busiest"] == {"n": 1, "title": "Chapter One", "count": 1}
    snip = r["current"]["snippets"][0]
    assert snip["seq"] == 1 and snip["text"][snip["start"]:snip["end"]] == "Alice Marlow"
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
    # "keeper" is the entity's name from seq 5 (hidden identity shape); its use in block 3 must not count
    # while the name is hidden, and the later "Pike" must not count in chapter two before its reveal.
    before = get(client, key, "ezra-pike", 5).json()
    assert before["chapters"] == [{"n": 1, "title": "Chapter One", "count": 1}]
    assert [s["text"][s["start"]:s["end"]] for s in before["current"]["snippets"]] == ["Ezra Pike"]
    after = get(client, key, "ezra-pike", 6).json()                    # block 5 is now finished: "keeper" is visible
    assert after["chapters"][0]["count"] == 2
    still = get(client, key, "ezra-pike", 7).json()                    # "Pike" (block 7) is not revealed yet
    assert still["chapters"][1]["count"] == 0
    assert get(client, key, "ezra-pike", 8).json()["chapters"][1]["count"] == 1


def test_not_found_cases(client, tmp_path):
    book, key = setup(client, tmp_path)
    assert get(client, "nope", "alice-marlow", 5).status_code == 404
    assert get(client, key, "no-such-entry", 5).status_code == 404
    assert get(client, key, "alice-marlow", 999).status_code == 422
    # Without the EPUB on disk there is nothing to count in.
    for p in (tmp_path / "data").rglob("*.epub"):
        p.unlink()
    assert get(client, key, "alice-marlow", 5).status_code == 404
