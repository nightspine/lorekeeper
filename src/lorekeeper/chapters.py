"""Chapter numbers as a book states them: which table-of-contents entries are chapters and what number each shows.

One pure rule over a book's whole ordered list of chapter titles, never over the rows a view happens to list, so a
prologue, a "Part I" divider or a chapter nobody reached never shifts the numbers after it. Stdlib only; the Reader's
port is `web/static/js/chapters.js`, and both run the vectors in `tests/fixtures/chapter_numbering.json`.

- `classify(title)`: `chapter` (the title carries a chapter number: "Chapter III", "Two", "XIV. Title", "3 - Title"),
  `divider` (a Part / Book / Volume / Section / Act heading: "PART I", "Book One. Deathseeker"), `named` (a prologue,
  interlude, epilogue, front or back matter: "Prologue: Julie", "Dramatis Personae") or `other` (a plain title).
- `own_number(title)`: the number a chapter title states (arabic, roman or a number word one to ninety-nine), else None.
- `numbering(entries)`: `{kind, number}` per entry; `number` None means no "Chapter N" label (the title alone).
"""
from __future__ import annotations

import re

_UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_UNITS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
_ORD_TEENS = ["tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
              "eighteenth", "nineteenth"]
_ORD_TENS = ["twentieth", "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth"]

WORDS: dict[str, int] = {}
for _i, _w in enumerate(_UNITS):
    WORDS[_w] = _i + 1
for _i, _w in enumerate(_TEENS):
    WORDS[_w] = _i + 10
for _i, _w in enumerate(_TENS):
    WORDS[_w] = (_i + 2) * 10
ORDINALS: dict[str, int] = {}
for _i, _w in enumerate(_ORD_UNITS):
    ORDINALS[_w] = _i + 1
for _i, _w in enumerate(_ORD_TEENS):
    ORDINALS[_w] = _i + 10
for _i, _w in enumerate(_ORD_TENS):
    ORDINALS[_w] = (_i + 2) * 10

# A heading's number is its whole leading token: digits, a roman numeral, or a number word ("Twenty-One" and
# "Twenty One" are one token). Standing alone (no "Chapter" before it) it must end the title or be followed by a
# separator, so "I went home", "Three Men in a Boat" and "12 Angry Men" are not numbers but "I", "II." and
# "3 - Title" are.
_TENS_RE = "|".join(_TENS)
_WORD_RE = rf"(?:(?:{_TENS_RE})[\s\-‐–]+(?:{'|'.join(_UNITS)})|{'|'.join(sorted(WORDS, key=len, reverse=True))})"
_ORD_RE = rf"(?:(?:{_TENS_RE})[\s\-‐–]+(?:{'|'.join(_ORD_UNITS)})|{'|'.join(sorted(ORDINALS, key=len, reverse=True))})"
_ROMAN_RE = r"[ivxlc]+"           # I V X L C only, as standing alone: "Chapter Mix" and "Chapter Dix" are no numbers
# end of the title, punctuation, or a dash: an en or em dash anywhere, a hyphen only after a space ("3 - Title",
# never "One-Eyed" or "X-Men")
_SEP = r"(?=\s*(?:$|[.:,;)\]–—])|\s+[-‐])"

# After "Chapter" (or "Part", "Scene"...) the token ends at a space, punctuation, a dash or the end of the title, never
# inside a word or before "-letter" ("Chapter Mix-up", "Chapter One-Eyed" have no number). ASCII classes only, so the
# JS port reads them the same.
_END = r"(?=$|[\s.:,;)\]–—]|[-‐](?![A-Za-z]))"
_ANY = rf"(?P<t>\d{{1,4}}|{_ROMAN_RE}|{_ORD_RE}|{_WORD_RE}){_END}"    # ordinals first: "Twenty-First" is not "Twenty"
_CHAPTER = re.compile(rf"^chapter\s+(?:the\s+)?{_ANY}", re.I)
_CHAPTER_BARE = re.compile(r"^chapter\b", re.I)
_DIVIDER = re.compile(rf"^(?:part|book|volume|vol\.|section|act)\s+(?:the\s+)?{_ANY}", re.I)
# A play's scene keeps its own heading ("SCENE II. A room in the Castle."): never a "Chapter N" label.
_SCENE = re.compile(rf"^scene\s+{_ANY}", re.I)
_ALONE_DIGITS = re.compile(rf"^(?P<t>\d{{1,3}}){_SEP}")
_ALONE_ROMAN = re.compile(rf"^(?P<t>[IVXLC]+){_SEP}")       # upper case only, and no D or M: "MIX" or "DID" are words
_ALONE_WORD = re.compile(rf"^(?P<t>{_WORD_RE}){_SEP}", re.I)
_NAMED = re.compile(
    r"^(?:the\s+|an?\s+)?(?:prologue|epilogue|interlude|intermission|introduction|preface|foreword|afterword|"
    r"prelude|postlude|postscript|coda|dramatis\s+person(?:ae|æ)|cast\s+of\s+characters|list\s+of\s+characters|"
    r"characters$|principal\s+characters|appendix|appendices|glossary|author[’']?s\s+note|note\s+on|"
    r"translator[’']?s\s+note|acknowledge?ments?|dedication|epigraph|contents|table\s+of\s+contents|copyright|"
    r"colophon|title\s+page|half\s+title|cover$|about\s+the\s+(?:author|publisher)|also\s+by|maps?$|index$|notes$|"
    r"endnotes|bibliography|further\s+reading|reading\s+group|discussion\s+questions)"
    r"(?![A-Za-z0-9])", re.I)     # whole words only: "Coda" but never "Codex" or "Codename"

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _tidy(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    return t.lstrip("\"'“‘«*_ ").strip()


def roman(token: str) -> int | None:
    """The value of a well-formed roman numeral ("XIV" 14), else None ("IIII", "VX", "IC")."""
    t = token.lower()
    if not t or any(ch not in _ROMAN_VALUES for ch in t):
        return None
    total = 0
    for i, ch in enumerate(t):
        v = _ROMAN_VALUES[ch]
        total += -v if i + 1 < len(t) and _ROMAN_VALUES[t[i + 1]] > v else v
    return total if 0 < total < 4000 and _to_roman(total) == t else None


def _to_roman(n: int) -> str:
    out = ""
    for v, s in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                 (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += s
            n -= v
    return out


def _value(token: str) -> int | None:
    t = token.strip().lower()
    if t.isdigit():
        n = int(t)
        return n if n > 0 else None
    parts = [p for p in re.split(r"[\s\-‐–]+", t) if p]
    if len(parts) == 2 and parts[0] in _TENS:
        unit = WORDS.get(parts[1]) or ORDINALS.get(parts[1])
        return WORDS[parts[0]] + unit if unit and unit < 10 else None
    if len(parts) == 1:
        if parts[0] in WORDS:
            return WORDS[parts[0]]
        if parts[0] in ORDINALS:
            return ORDINALS[parts[0]]
        return roman(parts[0])
    return None


def _divider(t: str) -> bool:
    m = _DIVIDER.match(t)
    return bool(m) and _value(m.group("t")) is not None      # "Part Mic" is no divider


def own_number(title: str) -> int | None:
    """The chapter number a title states: "Chapter III" 3, "Chapter One: Holden" 1, "Twenty-One" 21, "II" 2,
    "I: Nothing but Darkness" 1, "3 - Title" 3; None for "Prologue", "I went home", "Part Two" or "The Company"."""
    t = _tidy(title)
    if _divider(t) or _NAMED.match(t) or _SCENE.match(t):
        return None
    m = _CHAPTER.match(t)
    if m:
        return _value(m.group("t"))
    for rx in (_ALONE_DIGITS, _ALONE_ROMAN, _ALONE_WORD):
        m = rx.match(t)
        if m:
            return _value(m.group("t"))
    return None


def classify(title: str) -> str:
    """`chapter`, `divider`, `named` or `other` (see the module docstring)."""
    t = _tidy(title)
    if _divider(t):
        return "divider"
    if _NAMED.match(t) or _SCENE.match(t):
        return "named"
    if _CHAPTER_BARE.match(t) or own_number(t) is not None:
        return "chapter"
    return "other"


def numbering(entries) -> list[dict]:
    """`{kind, number}` for each entry of a book's table of contents, in order. An entry is a title or a dict with
    `title` and optionally `story` (False for front and back matter; absent or None counts as story).

    Only chapters get a number. When more than half of the story entries that are chapters or plain titles state
    their own number, those numbers are shown as they are (they may restart after a divider) and an unnumbered plain
    title gets none; otherwise every story chapter or plain title is counted in order, dividers, named sections and
    front and back matter skipped, and a stated number still wins."""
    rows = [e if isinstance(e, dict) else {"title": e} for e in entries]
    kinds = [classify(r.get("title") or "") for r in rows]
    own = [own_number(r.get("title") or "") for r in rows]
    counted = [i for i, (r, k) in enumerate(zip(rows, kinds)) if r.get("story") is not False and k in ("chapter", "other")]
    stated = sum(1 for i in counted if own[i] is not None)
    by_own = stated * 2 > len(counted)
    out = [{"kind": k, "number": None} for k in kinds]
    for place, i in enumerate(counted, 1):
        out[i]["number"] = own[i] if own[i] is not None else (None if by_own else place)
    return out


def label(number: int | None) -> str:
    """"Chapter 3", or "" for no number."""
    return f"Chapter {number}" if number is not None else ""
