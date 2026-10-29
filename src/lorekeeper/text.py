"""Text normalization and fingerprints (SPEC.md §3.2–3.4).

Every Lorekeeper implementation must produce byte-identical word lists and
fingerprints from the same block text, so this module is deliberately small
and has no dependencies beyond the standard library.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable

FINGERPRINT_WORDS = 12
FINGERPRINT_HEX = 16
MIN_ANCHOR_WORDS = 6

_WORD_CATEGORIES = ("L", "M", "N")
# Deleted rather than treated as separators, so "wine-glass" == "wineglass",
# "Good-bye" == "Goodbye" and "don't" == "don’t" across editions.
JOINERS = frozenset("-‐‑'’­‍⁠﻿")


def words(text: str) -> list[str]:
    """Split text into normalized words: NFKC, casefold, runs of letters/marks/digits."""
    text = unicodedata.normalize("NFKC", text).casefold()
    out: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch in JOINERS:
            continue
        if unicodedata.category(ch)[0] in _WORD_CATEGORIES:
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _digest(joined: str, hex_len: int | None) -> str:
    h = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return h if hex_len is None else h[:hex_len]


def fingerprint(block_words: list[str]) -> str:
    """Fingerprint of a block: SHA-256 of its first 12 words, first 16 hex chars."""
    return _digest(" ".join(block_words[:FINGERPRINT_WORDS]), FINGERPRINT_HEX)


def text_hash(all_blocks: Iterable[list[str]]) -> str:
    """Edition identity: SHA-256 of every word of every block, space-joined."""
    h = hashlib.sha256()
    first = True
    for block in all_blocks:
        for w in block:
            if not first:
                h.update(b" ")
            h.update(w.encode("utf-8"))
            first = False
    return h.hexdigest()


def shingles(source_words: list[str], n: int) -> set[tuple[str, ...]]:
    """Every run of n consecutive words in the source, for repeated verbatim checks."""
    return {tuple(source_words[i : i + n]) for i in range(len(source_words) - n + 1)}


def copied_runs(claim: str, source: set[tuple[str, ...]], n: int) -> list[str]:
    """Runs of n words in claim that also occur in a precomputed shingle set."""
    w = words(claim)
    return [" ".join(w[i : i + n]) for i in range(len(w) - n + 1) if tuple(w[i : i + n]) in source]


def verbatim_overlap(claim: str, source_words: list[str], max_run: int) -> list[str]:
    """Return word runs longer than max_run that appear in claim and source (SPEC.md §6)."""
    n = max_run + 1
    if len(words(claim)) < n:
        return []
    return copied_runs(claim, shingles(source_words, n), n)
