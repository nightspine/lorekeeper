"""Anchor resolution against a reader's own copy of a book (SPEC.md §3.6–3.7).

This is the client-side half of the format: given the blocks of the EPUB the
reader actually has, find where each anchor lives and which anchors the reader
has passed. Unresolvable anchors are always pushed *later*, never earlier, so
a mismatched edition can only under-reveal.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

from .epub import Block

INFINITY = float("inf")


def resolve(anchors: list[dict], blocks: list[Block]) -> dict[str, int | None]:
    """Map anchor id → block seq in this edition, or None if it cannot be found.

    Anchors are matched in document order; each must land strictly after the
    previous resolved anchor, which disambiguates repeated fingerprints.
    """
    by_fp: dict[str, list[int]] = defaultdict(list)
    for b in blocks:
        by_fp[b.fingerprint].append(b.seq)

    out: dict[str, int | None] = {}
    prev = -1
    for a in anchors:
        seqs = by_fp.get(a["fp"], [])
        i = bisect_right(seqs, prev)
        if i < len(seqs):
            prev = seqs[i]
            out[a["id"]] = prev
        else:
            out[a["id"]] = None
    return out


def effective_locations(anchors: list[dict], resolved: dict[str, int | None]) -> list[float]:
    """Location used for gating: an unresolved anchor takes the next resolved anchor's location."""
    eff: list[float] = [INFINITY] * len(anchors)
    nxt = INFINITY
    for i in range(len(anchors) - 1, -1, -1):
        loc = resolved.get(anchors[i]["id"])
        if loc is not None:
            nxt = loc
        eff[i] = nxt
    return eff


def frontier(anchors: list[dict], resolved: dict[str, int | None], completed_seq: int) -> str | None:
    """Last anchor revealed once the reader has finished block `completed_seq`.

    Returns None when the reader has not yet passed the first anchor.
    """
    eff = effective_locations(anchors, resolved)
    i = bisect_right(eff, completed_seq)
    return anchors[i - 1]["id"] if i else None
