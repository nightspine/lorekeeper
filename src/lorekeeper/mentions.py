"""Mentions of entities per chapter, computed from the book's text (SPEC §11 question 3).

Nothing here is stored in an index. The caller passes the names *visible at the reader's
position* (`visible_names` of a gated view): a name revealed later must not be counted
earlier, or a hidden identity would leak through the counts. Chapters whose first block
lies after `upto_seq` are never looked at, and the chapter holding `upto_seq` is counted
only through that block.

Rules: whole words, case-sensitive as the index spells the name, over each block's raw
text; overlapping matches are merged and the longest name wins, so "Potter" inside
"Harry Potter" counts only for the entity that owns "Harry Potter"; a pronoun is never a
name. A name shared by two entities counts for both.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from . import text

SNIPPET_MAX = 240
_NOTE = re.compile(r"\([^)]*\)")  # "Chevalier (Gabriel)": a parenthetical note is not part of the name
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "mt", "messrs", "mme", "mlle", "capt", "col", "gen", "lt", "sgt",
           "prof", "rev", "hon", "no", "vs", "etc", "jr", "sr"}


def visible_names(view: dict) -> dict[str, list[str]]:
    """Entity id → every name the gated view shows for it (display name first)."""
    return {e["id"]: [e["name"], *e.get("aliases", [])] for e in view["entities"]}


class Matcher:
    """One compiled alternation over all visible names, longest first."""

    def __init__(self, names: dict[str, list[str]]):
        self.owners: dict[str, list[str]] = {}
        for eid, ns in names.items():
            for n in ns:
                n = " ".join(_NOTE.sub(" ", n).split())
                if n and text.words(n):
                    owned = self.owners.setdefault(n, [])
                    if eid not in owned:
                        owned.append(eid)
        forms = sorted(self.owners, key=lambda n: (-len(n), n))
        self.pattern = (re.compile(r"(?<![\w'’])(?:" + "|".join(re.escape(f) for f in forms) + r")(?!\w)")
                        if forms else None)

    def find(self, raw: str) -> list[tuple[int, int, list[str]]]:
        """Non-overlapping (start, end, owner ids) matches in one block, left to right."""
        if self.pattern is None:
            return []
        return [(m.start(), m.end(), self.owners[m.group()]) for m in self.pattern.finditer(raw)]


@dataclass
class Mentions:
    current: int | None = None                 # index into `chapters` of the chapter holding upto_seq
    counts: dict[str, list[int]] = field(default_factory=dict)       # entity → count per chapter 0..current
    spans: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)  # entity → (seq, start, end), current chapter


def count(blocks, chapters, names: dict[str, list[str]] | Matcher, upto_seq: int) -> Mentions:
    """Mentions per entity and chapter through block `upto_seq` (inclusive).

    `blocks` need `.seq` and `.raw`; `chapters` need `.start`, in reading order. Blocks before
    the first chapter belong to no chapter and count for nothing.
    """
    matcher = names if isinstance(names, Matcher) else Matcher(names)
    ids = {e for o in matcher.owners.values() for e in o}
    if isinstance(names, dict):
        ids |= set(names)
    starts = [c.start for c in chapters]
    cur = bisect_right(starts, upto_seq) - 1
    out = Mentions(current=cur if cur >= 0 else None)
    if cur < 0:
        out.counts = {e: [] for e in ids}
        return out
    out.counts = {e: [0] * (cur + 1) for e in ids}
    out.spans = {e: [] for e in ids}
    for b in blocks:
        if b.seq > upto_seq:
            break
        ch = bisect_right(starts, b.seq) - 1
        if ch < 0:
            continue
        for s, e, owners in matcher.find(b.raw):
            for eid in owners:
                out.counts[eid][ch] += 1
                if ch == cur:
                    out.spans[eid].append((b.seq, s, e))
    return out


def snippet(raw: str, start: int, end: int, limit: int = SNIPPET_MAX) -> dict:
    """The sentence around raw[start:end] as {"text", "start", "end"} (offsets of the name in text)."""
    a = 0
    for m in re.finditer(r"[.!?…][”\"’']*\s", raw[:start]):
        if _ends_sentence(raw, m.start()):
            a = m.end()
    b = end
    while b < len(raw):
        if raw[b] in ".!?…" and _ends_sentence(raw, b):
            b += 1
            while b < len(raw) and raw[b] in "”\"’'":
                b += 1
            break
        b += 1
    s0 = raw[a:b]
    lead = len(s0) - len(s0.lstrip())
    a += lead
    sent = raw[a:b].rstrip()
    st, en = start - a, end - a
    if len(sent) > limit:
        room = max(0, limit - (en - st) - 2)
        lo = max(0, st - room // 2)
        hi = min(len(sent), lo + limit - 2)
        lo = max(0, min(lo, hi - (limit - 2)))
        if lo > 0:
            nxt = sent.find(" ", lo)
            lo = nxt + 1 if 0 <= nxt < st else lo
        if hi < len(sent):
            prv = sent.rfind(" ", en, hi)
            hi = prv if prv > 0 else hi
        piece = sent[lo:hi]
        pre = "…" if lo > 0 else ""
        post = "…" if hi < len(sent) else ""
        return {"text": pre + piece + post, "start": st - lo + len(pre), "end": en - lo + len(pre)}
    return {"text": sent, "start": st, "end": en}


def _ends_sentence(raw: str, i: int) -> bool:
    """A "." at raw[i] is a sentence end unless it closes an abbreviation or an initial ("Mr.", "J.")."""
    if raw[i] != ".":
        return True
    m = re.search(r"(\w+)$", raw[:i])
    if not m:
        return True
    w = m.group(1)
    return not (w.lower() in _ABBREV or (len(w) == 1 and w.isupper()))


def snippets(blocks_by_seq, spans: list[tuple[int, int, int]], limit: int = 60) -> list[dict]:
    """Snippets for one entity's spans in the current chapter, one per sentence, oldest first, the last `limit`."""
    out, seen = [], set()
    for seq, s, e in spans:
        sn = snippet(blocks_by_seq[seq].raw, s, e)
        key = (seq, sn["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"seq": seq, **sn})
    return out[-limit:]
