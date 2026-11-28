"""chapters.py: chapter numbers as the book states them. The vectors in tests/fixtures/chapter_numbering.json are
shared with web/static/js/chapters.js, so a change here is a change to both."""
import json
from pathlib import Path

import pytest

from lorekeeper.chapters import classify, label, numbering, own_number, roman

CASES = json.loads((Path(__file__).parent / "fixtures" / "chapter_numbering.json").read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_vectors(case):
    assert numbering(case["entries"]) == case["expected"]


@pytest.mark.parametrize("title, kind, number", [
    ("I", "chapter", 1), ("II.", "chapter", 2), ("I went home", "other", None), ("Interlude", "named", None),
    ("Twenty-One", "chapter", 21), ("Twenty One", "chapter", 21), ("Chapter Twenty-First", "chapter", 21),
    ("Chapter 1 - The Boy Who Lived", "chapter", 1), ("CHAPTER V", "chapter", 5), ("Chapter the Third", "chapter", 3),
    ("I: Nothing but Darkness", "chapter", 1), ("XIV. Title", "chapter", 14), ("3.", "chapter", 3),
    ("3 - Title", "chapter", 3), ("1984", "other", None), ("One-Eyed Jack", "other", None), ("X-Men", "other", None),
    ("Three Men in a Boat", "other", None), ("MIX", "other", None), ("Chapter", "chapter", None),
    ("Prologue: Julie", "named", None), ("Dramatis Personæ", "named", None), ("Cast of Characters", "named", None),
    ("Book One. Deathseeker", "divider", None), ("PART I.", "divider", None), ("Part Two", "divider", None),
    ("Book of the Dead", "other", None), ("Part Mic", "other", None), ("SCENE II. A room", "named", None),
    ("The Company", "other", None), ("", "other", None),
    ("Codex", "other", None), ("Codename", "other", None), ("Forward March", "other", None), ("Coda", "named", None),
    ("Acknowledgments", "named", None), ("Notes on a Scandal", "other", None),
    ("Chapter Dix", "chapter", None), ("Chapter Mix", "chapter", None), ("Chapter Mix-up", "chapter", None),
    ("Chapter One-Eyed", "chapter", None), ("Chapter IV The Return", "chapter", 4), ("Chapter XL", "chapter", 40),
    ("Book Mix", "other", None),
])
def test_classify(title, kind, number):
    assert classify(title) == kind
    assert own_number(title) == number


def test_restart_after_divider_keeps_the_books_numbers():
    got = numbering(["Part I", "Chapter I", "Chapter II", "Part II", "Chapter I"])
    assert [g["number"] for g in got] == [None, 1, 2, None, 1]


def test_numbers_never_depend_on_which_rows_a_view_lists():
    # Stated numbers stay put whatever is left out.
    toc = ["Prologue", "One", "Two", "Three"]
    assert numbering(toc)[3]["number"] == 3 == numbering(toc[:1] + toc[2:])[2]["number"]
    # A title-only book is a count, so the rule is over the whole table of contents: dropping a row renumbers what
    # follows, which is why callers pass every entry, never only the rows a view lists.
    titled = ["Prologue", "The Arrival", "A Long Night", "The Storm"]
    assert [x["number"] for x in numbering(titled)] == [None, 1, 2, 3]
    assert [x["number"] for x in numbering(titled[:1] + titled[2:])] == [None, 1, 2]


def test_strings_and_dicts_and_roman_and_label():
    assert numbering([{"title": "One"}, "Two"]) == [{"kind": "chapter", "number": 1}, {"kind": "chapter", "number": 2}]
    assert roman("XIV") == 14 and roman("IIII") is None and roman("IC") is None
    assert label(3) == "Chapter 3" and label(None) == ""


def test_a_cast_list_is_front_matter():
    from lorekeeper.web.jobs import is_story
    for t in ("Dramatis Personae", "Dramatis Personæ", "Cast of Characters", "List of Characters", "Characters"):
        assert not is_story(t, 5000), t
    assert is_story("The Characters of Mr. Fox", 5000) and is_story("One", 5000)


def test_toc_command_shows_the_label_beside_the_toc_position(tmp_path, capsys):
    from lorekeeper import cli

    from .test_generate import make_epub
    path = make_epub(tmp_path / "b.epub", front=("Title Page", ["A title page with a few words on it, nothing more."]))
    assert cli.main(["toc", str(path)]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if "seq" in line]
    assert rows[0].split()[0] == "1" and "Chapter" not in rows[0].split("seq")[0]
    assert rows[1].split()[:3] == ["2", "Chapter", "1"] and rows[2].split()[0] == "3"   # the position stays --chapters


RECAP = json.loads((Path(__file__).parent / "fixtures" / "recap_chapter.json").read_text("utf-8"))
TIDIERS = {"lower": str.lower}


@pytest.mark.parametrize("case", RECAP["cases"], ids=[c["name"] for c in RECAP["cases"]])
def test_recap_chapter_vectors(case):
    """recap_chapter and the Reader's recapChapter run the same vectors (tests/js/run_chapters.mjs)."""
    from lorekeeper.chapters import recap_chapter
    chapters = RECAP["maps"][case["map"]]
    words = None if case["words"] is None else RECAP["blockWords"][case["words"]]
    kw = {"tidy": TIDIERS[case["tidy"]]} if case.get("tidy") else {}
    i = recap_chapter(chapters, case["seq"], case["title"], words, **kw)
    assert (None if i is None else chapters[i]["start"]) == case["expected"]


def test_recap_chapter_takes_chapter_objects():
    from lorekeeper.chapters import recap_chapter
    from lorekeeper.epub import Chapter
    chs = [Chapter("CHAPTER I", 0), Chapter("CHAPTER II", 3)]
    assert recap_chapter(chs, 4, "CHAPTER I", [2, 40, 40, 2, 40]) == 0
