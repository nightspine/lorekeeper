"""Map a Book Orbit reading-progress entry onto Lorekeeper's text model (SPEC §3.1).

Book Orbit stores where the user is in several dialects (EPUB CFI from its web reader and iOS app,
a spine file plus percent from Kobo, a KOReader XPointer, and a whole-book percentage). `resolve`
turns the most precise usable one into a block `seq` of the reader's own edition (the parsing lives in
`lorekeeper.locators`; this module keeps Book Orbit's entry handling), plus `p`, the
fraction of the book's words *before* that block, so the block being read never counts as read
(the under-reveal rule of SPEC §3.6). Nothing here raises on odd input: a candidate that can't be
read is dropped and the next one is tried, ending at the percentage.
"""

from __future__ import annotations

import bisect
import re
from urllib.parse import unquote

from .. import epub, locators

STALE = 0.05      # a candidate this far from the whole-book percentage is older than another device's progress


class _Layout:
    """Word counts of a book, so fractions of the text can be turned into blocks."""

    def __init__(self, book: epub.Book) -> None:
        self.book = book
        self.cum = [0]
        for b in book.blocks:
            self.cum.append(self.cum[-1] + len(b.words))
        self.total = self.cum[-1]

    def end(self, doc: epub.Doc) -> int:
        later = [d.first for d in self.book.docs if d.first > doc.first]
        return min(later) if later else len(self.book.blocks)

    def block_at(self, fraction: float, lo: int = 0, hi: int | None = None) -> int | None:
        """The last block in [lo, hi) whose words-before share of that range is <= fraction."""
        hi = len(self.book.blocks) if hi is None else hi
        if hi <= lo:
            return None
        span = self.cum[hi] - self.cum[lo]
        target = self.cum[lo] + max(0.0, min(1.0, fraction)) * span
        i = bisect.bisect_right(self.cum, target, lo, hi) - 1
        return max(lo, min(hi - 1, i))

    def p(self, seq: int) -> float:
        return self.cum[seq] / self.total if self.total and seq >= 0 else 0.0


def _kobo_doc(book: epub.Book, source: str) -> epub.Doc | None:
    src = unquote(source).split("#", 1)[0].lstrip("/")
    for d in book.docs:
        if d.path == src:
            return d
    for d in book.docs:
        if d.path.endswith("/" + src) or src.endswith("/" + d.path):
            return d
    return None


_KOREADER = re.compile(r"/body/DocFragment\[(\d+)\](/.*)?")


def resolve(book: epub.Book, entry: dict) -> dict:
    """One progress entry → {"seq": int|None, "p": float, "basis": "cfi"|"kobo"|"koreader"|"percentage"}.

    Documents are reopened from `book.path`; a Book without one can only use Kobo and the percentage.
    A position that lies before the first block reads as block 0 here (`p` is the words before it).
    """
    path = book.path
    lay = _Layout(book)
    pct = entry.get("percentage")
    pct = max(0.0, min(100.0, float(pct))) / 100 if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
    by_child = {d.spine_child_index: d for d in book.docs}

    def percentage_seq() -> int | None:
        if pct is None:
            return None
        return lay.block_at(pct)

    def located(locator: dict) -> locators.Located | None:
        try:
            return locators.locate(book, locator)
        except locators.LocatorError:
            return None

    candidates: list[tuple[str, object]] = []
    text_, kobo = entry.get("text") or {}, entry.get("kobo") or {}
    if path and text_.get("cfi"):
        def cfi_seq():
            r = located({"cfi": text_["cfi"]})
            return r.seq if r else None
        candidates.append(("cfi", cfi_seq))
    if kobo.get("source") and isinstance(kobo.get("contentPercent"), (int, float)):
        def kobo_seq():
            d = _kobo_doc(book, kobo["source"])
            if d is None:
                return None
            r = located({"spine": d.spine, "progress": max(0.0, min(1.0, float(kobo["contentPercent"]) / 100))})
            return r.seq if r else None
        candidates.append(("kobo", kobo_seq))
    if path and entry.get("koreader"):
        def ko_seq():
            r = located({"xpointer": entry["koreader"]})
            if r is not None and r.exact:
                return r.seq
            m = _KOREADER.fullmatch(str(entry["koreader"]).strip())
            d = by_child.get(int(m.group(1)) - 1) if m else None
            if d is None or pct is None:
                return r.seq if r else None
            lo, hi = d.first, lay.end(d)                       # fall back to the percentage inside this document
            if hi <= lo:
                return None
            word = max(lay.cum[lo], min(lay.cum[hi], pct * lay.total))
            return max(lo, min(hi - 1, bisect.bisect_right(lay.cum, word, lo, hi) - 1))
        candidates.append(("koreader", ko_seq))

    for basis, fn in candidates:
        try:
            seq = fn()
        except Exception:
            seq = None
        if seq is None:
            continue
        seq = max(0, seq)
        p = lay.p(seq)
        if pct is not None and abs(p - pct) > STALE:
            continue                                          # another device has moved on since
        return {"seq": seq, "p": p, "basis": basis}
    seq = percentage_seq()
    return {"seq": seq, "p": lay.p(seq) if seq is not None else 0.0, "basis": "percentage"}
