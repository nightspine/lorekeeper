"""Locators: from the position a reader app actually holds to a block of its own edition (SPEC §3.7).

An index locates everything by paragraph fingerprints, but a reader app does not hold a paragraph.
It holds an EPUB CFI, a KOReader XPointer, or a spine index plus an offset. `locate` turns each of
those into the `seq` of the block the position points into; `position` then turns a `seq` into the
reader's current anchor. Locator forms (JSON objects):

    {"block": 123}                                the block seq in the reader's own edition (§3.1)
    {"cfi": "epubcfi(/6/22!/4/108/1:0)"}          EPUB CFI; the spine step counts every <itemref>,
                                                  linear="no" ones included
    {"xpointer": "/body/DocFragment[11]/body/div[3]/p[7]/text().0"}   CREngine / KOReader
    {"spine": 10, "word": 240}                    0-based linear spine document, 0-based word in it
    {"spine": 10, "progress": 0.42}               the same document and a fraction 0..1 of its words

Every rule errs towards the *preceding* block, so a wrong guess can only under-reveal (§3.6):

* A target that lies between blocks, or before the first block of its document, maps to the nearest
  preceding block; before the first block of the whole edition that is -1.
* An element outside any block (a wrapper `div`) maps to the first block that starts inside it.
* A malformed locator, or one whose document is not among the linear spine documents, raises
  `LocatorError`. A document that is found but whose inner path does not resolve maps to its first
  block minus one, with `exact=False`.
* `progress` is always `exact=False`: a fraction only guesses the block.
* A word offset past the end of a document clamps to its last block, with `exact=False`.

Character offsets inside a text node never change the block: a text node lies inside one block.
"""

from __future__ import annotations

import bisect
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from . import anchors as _anchors
from . import epub, text


class LocatorError(ValueError):
    """The locator is malformed, or names a document the edition does not have."""


@dataclass(frozen=True)
class Located:
    seq: int            # block seq, or -1 for "before the first block"
    basis: str          # "block" | "cfi" | "xpointer" | "word" | "progress"
    exact: bool         # False when a fallback decided the block


# ---- the reader's current anchor ---------------------------------------------------------------

def position(anchors: list[dict], resolved: dict[str, int | None], seq: int, *, inclusive: bool = False) -> str | None:
    """The id of the current anchor for a reader at block `seq`, or None before the first anchor.

    `inclusive=False` is the §3.7 "finished" mode: the block being read does not count (views).
    `inclusive=True` counts it ("I tapped a name on this page, who is this?").
    """
    return _anchors.frontier(anchors, resolved, seq if inclusive else seq - 1)


def resolve_edition(doc: dict, book: epub.Book) -> dict[str, int | None]:
    """Where each of the index's anchors lives in this edition (fingerprint resolution, §3.6)."""
    return _anchors.resolve(doc["anchors"], book.blocks)


# ---- documents of an edition -------------------------------------------------------------------

def _doc_end(book: epub.Book, doc: epub.Doc) -> int:
    """seq just past the document's last block."""
    i = book.docs.index(doc)
    return book.docs[i + 1].first if i + 1 < len(book.docs) else len(book.blocks)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


class _Dom:
    """One XHTML document with the block structure `epub.read` gave it (SPEC §3.1), by element.

    The walk repeats the block rules of `epub._BlockParser`: a block tag opens a block, text goes to
    the innermost open block, `br` adds a space, head/title/script/style text is ignored. Blocks are
    numbered in start order (`order`); the non-empty ones are the document's blocks, in seq order.
    """

    def __init__(self, root, block_tags: frozenset[str]) -> None:
        self.root = root
        self.parent: dict[int, object] = {}
        self.block_order: dict[int, int] = {}       # id(element) → order, for block elements
        self.ord_start: dict[int, int] = {}         # blocks started before the element's start tag
        self.ord_end: dict[int, int] = {}           # blocks started up to the element's end tag
        self.by_id: dict[str, object] = {}
        self.nonempty: list[int] = []               # orders of blocks that hold words, ascending
        self.nwords = 0
        self._scan(block_tags)

    def _scan(self, block_tags: frozenset[str]) -> None:
        order = skip = 0
        open_blocks: list[tuple[int, list[str]]] = []
        raws: dict[int, str] = {}

        def add(s: str | None) -> None:
            if s and not skip and open_blocks:
                open_blocks[-1][1].append(s)

        def enter(el, parent) -> None:
            nonlocal order, skip
            name = _local(el.tag)
            self.parent[id(el)] = parent
            self.ord_start[id(el)] = order
            el_id = el.get("id")
            if el_id and el_id not in self.by_id:
                self.by_id[el_id] = el
            if name in epub.SKIP_TAGS:
                skip += 1
            elif name in block_tags:
                self.block_order[id(el)] = order
                open_blocks.append((order, []))
                order += 1
            elif name == "br" and open_blocks and not skip:
                open_blocks[-1][1].append(" ")
            add(el.text)

        def leave(el) -> None:
            nonlocal skip
            name = _local(el.tag)
            if name in epub.SKIP_TAGS:
                skip = max(0, skip - 1)
            elif name in block_tags and open_blocks:
                o, parts = open_blocks.pop()
                raws[o] = "".join(parts)
            self.ord_end[id(el)] = order

        enter(self.root, None)
        stack = [(self.root, iter(self.root))]
        while stack:
            el, it = stack[-1]
            child = next(it, None)
            if child is None:
                leave(el)
                stack.pop()
                if stack:
                    add(el.tail)
                continue
            enter(child, el)
            stack.append((child, iter(child)))
        for o in sorted(raws):
            n = len(text.words(raws[o]))
            if n:
                self.nonempty.append(o)
                self.nwords += n

    # -- mapping points to blocks (as a doc-relative index into `nonempty`; seq = doc.first + index)

    def _owner(self, el) -> int | None:
        """Index of the innermost non-empty block that is `el` or contains it."""
        while el is not None:
            o = self.block_order.get(id(el))
            if o is not None:
                i = bisect.bisect_left(self.nonempty, o)
                if i < len(self.nonempty) and self.nonempty[i] == o:
                    return i
            el = self.parent.get(id(el))
        return None

    def _preceding(self, count: int) -> int:
        """Index of the last non-empty block that started before `count` block starts; -1 if none."""
        return bisect.bisect_left(self.nonempty, count) - 1

    def element(self, el) -> int:
        """Index of the block an element points into (-1: before the document's first block)."""
        inner = self._owner(el)
        if inner is not None:
            return inner
        lo, hi = self.ord_start[id(el)], self.ord_end[id(el)]
        i = bisect.bisect_left(self.nonempty, lo)
        if i < len(self.nonempty) and self.nonempty[i] < hi:
            return i
        return self._preceding(lo)

    def gap(self, el, k: int) -> int:
        """Index of the block holding text gap `k` of `el` (0 = el.text, k = tail of child k-1)."""
        inner = self._owner(el)
        if inner is not None:
            return inner
        kids = list(el)
        count = self.ord_start[id(el)] if k == 0 else self.ord_end[id(kids[k - 1])]
        return self._preceding(count)

    def gaps(self, el):
        """(k, text) for each text gap of `el` that holds text, in document order."""
        kids = list(el)
        if el.text:
            yield 0, el.text
        for j, c in enumerate(kids, 1):
            if c.tail:
                yield j, c.tail


def _load(book: epub.Book, doc: epub.Doc) -> _Dom | None:
    """The document as the block model saw it, or None when it can't be read or doesn't agree."""
    if not book.path:
        return None
    try:
        with zipfile.ZipFile(book.path) as z:
            root = ET.fromstring(z.read(doc.path))
        expected = _doc_end(book, doc) - doc.first
        dom = _Dom(root, epub.BLOCK_TAGS)
        if not dom.nonempty:                              # epub._parse_blocks' fallback rule (SPEC §3.1)
            probe = _Dom(root, epub.FALLBACK_BLOCK_TAGS)
            if probe.nwords >= epub.FALLBACK_MIN_WORDS:
                dom = probe
        return dom if len(dom.nonempty) == expected else None
    except Exception:                                     # bad XML, odd entities, missing file
        return None


def _in_doc(book: epub.Book, doc: epub.Doc, resolve) -> tuple[int, bool]:
    """Run `resolve(dom)` → block index (or None: path unresolved) → (seq, exact)."""
    dom = _load(book, doc)
    idx = resolve(dom) if dom is not None else None
    if idx is None:
        return doc.first - 1, False
    return doc.first + idx, True


# ---- CFI ---------------------------------------------------------------------------------------

_STEP = re.compile(r"/(\d+)(?:\[((?:[^\]^]|\^.)*)\])?")
_STEPS = re.compile(rf"(?:{_STEP.pattern})*")
_SPLIT = re.compile(r"(?<!\^),")


def _unescape(s: str) -> str:
    return re.sub(r"\^(.)", r"\1", s)


def _parse_cfi(cfi: str) -> tuple[int, list[tuple[int, str | None]]]:
    """→ (0-based itemref index, [(step, id | None)] inside the document). Offsets are dropped."""
    m = re.fullmatch(r"\s*epubcfi\((.*)\)\s*", cfi, re.S) if isinstance(cfi, str) else None
    if not m:
        raise LocatorError("not an epubcfi(...) string")
    parts = _SPLIT.split(m.group(1))
    if len(parts) > 3:
        raise LocatorError("too many commas in CFI range")
    body = parts[0] + parts[1] if len(parts) >= 2 else parts[0]     # a range: where it starts
    pkg, _, inner = body.partition("!")
    pm = re.fullmatch(r"/6(?:\[[^\]]*\])?/(\d+)(?:\[[^\]]*\])?", pkg.strip())
    if not pm:
        raise LocatorError("CFI does not start with a spine step (/6/n)")
    step = int(pm.group(1))
    if step < 2 or step % 2:
        raise LocatorError(f"spine step {step} is not an itemref (must be even)")
    inner = inner.partition("!")[0]                                  # further indirection: stop at its host
    inner = re.split(r"[~@]", inner, maxsplit=1)[0]
    inner = re.sub(r":\d+(?:\[(?:[^\]^]|\^.)*\])?\s*$", "", inner)   # the character offset
    if not _STEPS.fullmatch(inner):
        raise LocatorError("malformed CFI path")
    steps = []
    for x in _STEP.finditer(inner):
        el_id = _unescape(x.group(2)).split(";", 1)[0] if x.group(2) else None
        steps.append((int(x.group(1)), el_id or None))
    return step // 2 - 1, steps


def _cfi_target(dom: _Dom, steps: list[tuple[int, str | None]]) -> int | None:
    el = dom.root
    gap = None
    for i, (n, _) in enumerate(steps):
        if n % 2:                                       # a text step is the last one
            if i != len(steps) - 1 or (n - 1) // 2 > len(el):
                return None
            gap = (n - 1) // 2
            break
        kids = list(el)
        if not 1 <= n // 2 <= len(kids):
            el = None
            break
        el = kids[n // 2 - 1]
    last_id = next((el_id for n, el_id in reversed(steps) if n % 2 == 0), None)
    named = dom.by_id.get(last_id) if last_id else None
    if named is not None and named is not el:           # the asserted id names the element
        return dom.element(named)
    if el is None:
        return None
    return dom.gap(el, gap) if gap is not None else dom.element(el)


def _by_child(book: epub.Book, child: int) -> epub.Doc:
    doc = next((d for d in book.docs if d.spine_child_index == child), None)
    if doc is None:
        raise LocatorError(f"spine item {child} is not a linear document of this edition")
    return doc


def _locate_cfi(book: epub.Book, cfi: str) -> Located:
    child, steps = _parse_cfi(cfi)
    doc = _by_child(book, child)
    if not book.path:
        raise LocatorError("a CFI needs the EPUB file (book.path)")
    seq, exact = _in_doc(book, doc, lambda dom: _cfi_target(dom, steps))
    if not exact and (last := next((i for n, i in reversed(steps) if n % 2 == 0 and i), None)) and last in doc.ids:
        seq = doc.ids[last]                              # document unreadable: the id shortcut (§3.1 ids)
    return Located(seq, "cfi", exact)


# ---- XPointer ----------------------------------------------------------------------------------

_XP = re.compile(r"/body/DocFragment\[(\d+)\](/.*)?")
_XP_SEG = re.compile(r"([A-Za-z][A-Za-z0-9_:.-]*)(?:\[(\d+)\])?")
_XP_TEXT = re.compile(r"text\(\)(?:\[(\d+)\])?(?:\.\d+)?")


def _parse_xpointer(xp: str) -> tuple[int, list[tuple[str, int]], int | None]:
    """→ (0-based itemref index, [(element name, 1-based index)], k-th text node | None)."""
    m = _XP.fullmatch(xp.strip()) if isinstance(xp, str) else None
    if not m:
        raise LocatorError("not a /body/DocFragment[n]/... XPointer")
    n = int(m.group(1))
    if n < 1:
        raise LocatorError("DocFragment index is 1-based")
    segs = [s for s in (m.group(2) or "").split("/") if s]
    text_k = None
    if segs and segs[-1].startswith("text()"):
        tm = _XP_TEXT.fullmatch(segs.pop())
        if not tm:
            raise LocatorError("malformed text() step")
        text_k = int(tm.group(1) or 1)
    elif segs:
        segs[-1] = re.sub(r"\.\d+$", "", segs[-1])        # a bare character offset
    path = []
    for s in segs:
        sm = _XP_SEG.fullmatch(s)
        if not sm:
            raise LocatorError(f"malformed XPointer step {s!r}")
        path.append((sm.group(1).lower(), int(sm.group(2) or 1)))
    return n - 1, path, text_k


def _xp_target(dom: _Dom, path: list[tuple[str, int]], text_k: int | None) -> int | None:
    el = dom.root
    for name, idx in path:
        same = [c for c in el if _local(c.tag) == name]
        if not 1 <= idx <= len(same):
            return None
        el = same[idx - 1]
    if text_k is not None:
        gaps = list(dom.gaps(el))
        if 1 <= text_k <= len(gaps):
            return dom.gap(el, gaps[text_k - 1][0])
    return dom.element(el)


def _locate_xpointer(book: epub.Book, xp: str) -> Located:
    child, path, text_k = _parse_xpointer(xp)
    doc = _by_child(book, child)
    if not book.path:
        raise LocatorError("an XPointer needs the EPUB file (book.path)")
    seq, exact = _in_doc(book, doc, lambda dom: _xp_target(dom, path, text_k))
    return Located(seq, "xpointer", exact)


# ---- words and progress ------------------------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _doc_cum(book: epub.Book, doc: epub.Doc) -> list[int]:
    """Cumulative word counts over the document's blocks: cum[i] words precede its i-th block."""
    cum = [0]
    for b in book.blocks[doc.first:_doc_end(book, doc)]:
        cum.append(cum[-1] + len(b.words))
    return cum


def _by_spine(book: epub.Book, spine) -> epub.Doc:
    if not _is_int(spine) or spine < 0:
        raise LocatorError("spine must be a non-negative integer")
    doc = next((d for d in book.docs if d.spine == spine), None)
    if doc is None:
        raise LocatorError(f"spine {spine} is not a linear document of this edition")
    return doc


def _locate_word(book: epub.Book, spine, word) -> Located:
    if not _is_int(word) or word < 0:
        raise LocatorError("word must be a non-negative integer")
    doc = _by_spine(book, spine)
    cum = _doc_cum(book, doc)
    if len(cum) == 1:
        return Located(doc.first - 1, "word", False)
    i = bisect.bisect_right(cum, word) - 1
    if i >= len(cum) - 1:                                # past the end: the last block
        return Located(doc.first + len(cum) - 2, "word", False)
    return Located(doc.first + i, "word", True)


def _locate_progress(book: epub.Book, spine, progress) -> Located:
    if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 1:
        raise LocatorError("progress must be a number from 0 to 1")
    doc = _by_spine(book, spine)
    cum = _doc_cum(book, doc)
    total = cum[-1]
    if not total:
        return Located(doc.first - 1, "progress", False)
    i = bisect.bisect_right(cum, min(int(progress * total), total - 1)) - 1   # rounds down to a block
    return Located(doc.first + i, "progress", False)   # a fraction: a rounded-down guess


# ---- entry point -------------------------------------------------------------------------------

def locate(book: epub.Book, locator: dict) -> Located:
    """The block of `book` the locator points into. Raises `LocatorError` for locators that can't be read."""
    if not isinstance(locator, dict):
        raise LocatorError("a locator is a JSON object")
    keys = set(locator)
    if keys == {"block"}:
        n = locator["block"]
        if not _is_int(n) or not -1 <= n < len(book.blocks):
            raise LocatorError(f"block must be an integer from -1 to {len(book.blocks) - 1}")
        return Located(n, "block", True)
    if keys == {"cfi"}:
        return _locate_cfi(book, locator["cfi"])
    if keys == {"xpointer"}:
        return _locate_xpointer(book, locator["xpointer"])
    if keys == {"spine", "word"}:
        return _locate_word(book, locator["spine"], locator["word"])
    if keys == {"spine", "progress"}:
        return _locate_progress(book, locator["spine"], locator["progress"])
    raise LocatorError("a locator has exactly one form: block, cfi, xpointer, spine+word or spine+progress")
