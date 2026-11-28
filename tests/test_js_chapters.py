"""The Reader's chapter numbers (static/js/chapters.js), run in Node against chapters.py: the shared vectors, plus
titles and books worked out by the Python rule on the spot, so the two ports cannot drift apart."""
import json
import shutil
import subprocess

import pytest

from lorekeeper.chapters import classify, numbering, own_number

from .conftest import ROOT

TITLES = [
    "I", "II.", "I went home", "Interlude", "Twenty-One", "Twenty One", "twenty-one", "Chapter Twenty-First",
    "Chapter 1 - The Boy Who Lived", "CHAPTER V", "Chapter the Third", "I: Nothing but Darkness", "XIV. Title", "3.",
    "3 - Title", "3-Title", "3 – Title", "1984", "One-Eyed Jack", "X-Men", "Three Men in a Boat", "MIX", "DID", "Chapter",
    "Prologue: Julie", "Dramatis Personæ", "Dramatis Personae", "Cast of Characters", "Book One. Deathseeker",
    "PART I.", "Part Two", "Book of the Dead", "Part Mic", "SCENE II. A room", "The Company", "", "  ", "Codex",
    "Codename", "Forward March", "Coda", "Coda: After", "Acknowledgments", "Acknowledgements", "Notes on a Scandal",
    "Notes", "Chapter Dix", "Chapter Mix", "Chapter Mix-up", "Chapter One-Eyed", "Chapter IV The Return", "Chapter XL",
    "Book Mix", "Chapter I. Parting Gifts", "Sunset", "“One”", "*Two*", "_III_", "Chapter 3-4", "Chapter 12a",
    "chapter iv", "ii", "IIII", "IC", "Vol. 2", "Volume Three: The End", "Act I", "Section 4", "Chapter One: Holden",
    "100", "1000", "Chapter 1000", "Chapter 10000", "0", "Chapter 0", "Ninety-Nine", "Ninety-Ten", "Twenty-First",
    "The Prologue", "A Note on the Text", "Author's Note", "Author’s Note", "Translator’s Note", "About the Author",
    "Also by Bram Stoker", "Map", "Maps", "Mapping the Stars", "Index", "Indexing", "Characters", "The Characters",
    "Principal Characters", "Cover", "Cover Story", "Chapter—The End", "IV—Return", "IV (continued)", "V)", "[VI]",
    "One, Two, Three", "One Night", "Fifty Shades", "L", "C", "LXXX", "CD", "Chapter MCMXC", "Chapter\tVII",
    "Chapter  VII", "Chapter XIX\nThe Night", "Twenty‐One", "Twenty–One",
]

BOOKS = [
    ["Contents", "Chapter I", "Chapter II", "Epilogue"],
    ["The Company", "One", "Two", "Three"],
    ["Sunset", "Book One. Deathseeker", "Chapter I. Parting Gifts", "Chapter II", "Book Two", "Chapter III"],
    ["Prologue", "The Arrival", "A Long Night", "Interlude", "The Storm"],
    [{"title": "Chapter 1", "story": False}, {"title": "The Start", "story": True}, {"title": "Then", "story": None}],
    ["II", "Untitled", "IV", "V"],
]


def cases():
    return {
        "titles": [{"title": t, "kind": classify(t), "number": own_number(t)} for t in TITLES],
        "books": [{"name": f"book {i}", "entries": b, "expected": numbering(b)} for i, b in enumerate(BOOKS)],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chapters_js(tmp_path):
    f = tmp_path / "chapter_cases.json"
    f.write_text(json.dumps(cases()), encoding="utf-8")
    r = subprocess.run(["node", str(ROOT / "tests/js/run_chapters.mjs"), str(f)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
