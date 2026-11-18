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
