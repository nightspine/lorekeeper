"""Audit: check a sample of an index's claims against the book's own text.

The index is untrusted input here (it may come from a registry). Python picks the claims, finds the
passage for each from the cited anchors, and counts; the model only gives a verdict per claim.
The result is counts ("48 of 50 held up"), never a percentage, and it says nothing about claims
that were not sampled.

What is sampled: claims that cite their evidence locally, that is non-profile facets, relations, events
and the sentences of chapter recaps. Profile facets are not sampled: a profile sums up many paragraphs, so
checking it against the few it cites would score a sound index low. Story-so-far recaps are left out for
the same reason. `unsure` means the passage does not settle the claim either way and does not count
against the index.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from . import anchors as anchors_mod
from .epub import Block
from .generate import MIN_SENTENCE_WORDS, SYSTEM, _ARR, _STR, _obj
from . import text as textmod

BATCH = 10                # claims per model call
CONTEXT = 2               # blocks either side of each cited block
MAX_BLOCKS = 14           # most blocks in one passage; the cited ones and the from block are kept first
SUMMARY_CHARS = 60_000    # a chapter recap sentence is checked against the whole stretch the recap covers, up to this
SUMMARY_SHARE = 10        # recap sentences are at most one in this many of the sample
BLOCK_CHARS = 8000        # a paragraph is sent whole (the longest in Dracula is 4,700 characters); this only stops a runaway one

AUDIT = """
One question per claim: does its passage (the paragraphs given under it) state or clearly imply the claim?
Give every claim a verdict with a one-line reason:
- supported: the passage states or clearly implies it. A paraphrase in other words, a detail spread over several
  of the paragraphs, or a plain reading of what they show is supported.
- unsupported: the passage contradicts it, or the claim asserts something (a detail, person, place, time, number,
  tie or order of events) that the passage gives no ground for. Name that detail in "because".
- unsure: the claim may be right but needs more of the book than the passage shows, so the passage settles
  nothing either way. Say what is missing in "because". Use it instead of guessing.
A claim marked (summary) sums the passage up and may combine several paragraphs.
A claim marked (summary, start of the passage left out) cannot be settled against what is missing: for it
answer supported or unsure, never unsupported.
Use only the passages. Never use outside knowledge of the book.

{claims}"""

AUDIT_SCHEMA = _obj({"verdicts": _ARR(_obj({"id": _STR, "verdict": {"enum": ["supported", "unsupported", "unsure"]},
                                            "because": _STR}, ["id", "verdict", "because"]))}, ["verdicts"])


@dataclass
class Claim:
    cid: str            # "c1"...
    ordinal: int        # position of the item's `from` anchor in the index
    kind: str           # fact | relation | event | summary
    text: str
    seqs: list[int]     # block seqs that show it
    frm: int | None = None   # seq of the item's own `from` block, at or after the last cited one
    window: int = CONTEXT
    span_start: int | None = None   # a chapter-recap sentence: first block of the stretch the recap covers


def _sentences(t: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z\"'(\[])", " ".join(t.split()))
    return [p for p in parts if len(textmod.words(p)) >= MIN_SENTENCE_WORDS]


def _name_at(entity: dict, frm: str, ordinal: dict[str, int], default: str) -> str:
    """The name to use for `entity` at anchor `frm`: its primary name when the book has revealed it by then;
    else the latest name revealed by then; else the earliest name. Never a name the reader has not met yet."""
    at = ordinal.get(frm)
    named = [(ordinal[n["from"]], i, n["value"], n.get("kind") == "primary") for i, n in enumerate(entity.get("names") or [])
             if n.get("value") and n.get("from") in ordinal]
    if at is None or not named:
        return default
    seen = [t for t in named if t[0] <= at]
    for t in seen:
        if t[3]:
            return t[2]
    return max(seen)[2] if seen else min(named)[2]


def claims_of(index: dict, resolved: dict[str, int | None]) -> list[Claim]:
    """Every checkable claim of the index, in anchor order. Claims whose anchors do not resolve
    in this edition are left out: there is no passage to check them against."""
    ordinal = {a["id"]: i for i, a in enumerate(index.get("anchors") or [])}
    out: list[Claim] = []
    # the stretch a chapter recap covers starts after the previous chapter recap's `from` block
    chapter_ends = sorted({resolved[rc["from"]] for rc in index.get("recaps") or []
                           if rc.get("scope") == "chapter" and resolved.get(rc.get("from")) is not None})

    def add(kind: str, text_: str, frm: str, evidence: list[str] | None, **kw):
        if frm not in ordinal or not text_.strip():
            return
        ids = [a for a in (evidence or [frm]) if a in ordinal] or [frm]
        seqs = sorted({resolved[a] for a in ids if resolved.get(a) is not None})
        if not seqs:
            return
        out.append(Claim(f"c{len(out) + 1}", ordinal[frm], kind, text_.strip(), seqs, frm=resolved.get(frm), **kw))

    ents = {e.get("id"): e for e in index.get("entities") or []}
    names = {}
    for e in index.get("entities") or []:
        nm = next((n["value"] for n in e.get("names") or [] if n.get("kind") == "primary"), None) \
            or next((n["value"] for n in e.get("names") or []), e.get("id", ""))
        names[e.get("id")] = nm
        for f in e.get("facets") or []:
            if f.get("kind") != "profile":
                add("fact", f"{_name_at(e, f.get('from', ''), ordinal, nm)}: {f.get('text', '')}",
                    f.get("from", ""), f.get("evidence"))
    for r in index.get("relations") or []:
        def end(id_):
            e = ents.get(id_)
            return _name_at(e, r.get("from", ""), ordinal, names.get(id_, id_)) if e else id_
        a, b = end(r.get("src")), end(r.get("dst"))
        add("relation", f"{a}, {r.get('label', '')}, {b}", r.get("from", ""), r.get("evidence"))
    for ev in index.get("events") or []:
        add("event", ev.get("summary", ""), ev.get("from", ""), ev.get("evidence"))
    for rc in index.get("recaps") or []:
        if rc.get("scope") != "chapter":   # a story-so-far recap sums up the whole arc, which no window holds
            continue
        for s in _sentences(rc.get("text", "")):
            end = resolved.get(rc.get("from"))
            before = [e for e in chapter_ends if end is not None and e < end]
            add("summary", s, rc.get("from", ""), rc.get("evidence"),
                span_start=(before[-1] + 1) if before else 0)
    out.sort(key=lambda c: c.ordinal)        # stable: ties keep document order
    for i, c in enumerate(out, 1):
        c.cid = f"c{i}"
    return out


def sample(claims: list[Claim], n: int, seed: int = 0) -> list[Claim]:
    """n claims spread evenly across the book: the sorted claims are cut into n equal strata and one is
    drawn from each. Same index, n and seed give the same sample."""
    if n <= 0:
        return []
    summaries = [c for c in claims if c.kind == "summary"]
    others = [c for c in claims if c.kind != "summary"]
    if len(claims) <= n and len(summaries) <= len(claims) // SUMMARY_SHARE:
        return list(claims)
    # recap sentences are at most a tenth of what is actually sampled, with two exceptions: at least one when
    # any exist beside other claims, and up to a tenth of n when there is nothing else to check
    k_o = min(len(others), n - min(len(summaries), n // SUMMARY_SHARE))
    if others:
        k_s = min(len(summaries), max(1, k_o // (SUMMARY_SHARE - 1)), n - k_o)
    else:
        k_s = min(len(summaries), n // SUMMARY_SHARE)
    picked = _strata(summaries, k_s, seed) + _strata(others, k_o, seed + 1)
    picked.sort(key=lambda c: (c.ordinal, int(c.cid[1:])))
    return picked


def _strata(claims: list[Claim], n: int, seed: int) -> list[Claim]:
    if n <= 0:
        return []
    if len(claims) <= n:
        return list(claims)
    rng = random.Random(seed)
    L = len(claims)
    picked = []
    for k in range(n):
        lo, hi = k * L // n, (k + 1) * L // n
        picked.append(claims[rng.randrange(lo, max(hi, lo + 1))])
    return picked


def _line(blocks: list[Block], s: int) -> str:
    return f"[{s}] {' '.join(blocks[s].raw.split())[:BLOCK_CHARS]}"


def summary_span(c: Claim, blocks: list[Block]) -> tuple[list[int], bool]:
    """The blocks a chapter-recap sentence is checked against: from the block after the previous chapter
    recap's `from` (or the first block) through this recap's `from` block, whole up to SUMMARY_CHARS;
    beyond that the end is kept. The flag says the start was left out."""
    end = min(len(blocks) - 1, c.frm if c.frm is not None else c.seqs[-1])
    start = min(c.span_start or 0, end)
    want, total = [], 0
    for s in range(end, start - 1, -1):
        total += len(_line(blocks, s)) + 1
        if total > SUMMARY_CHARS and want:
            return want[::-1], True
        want.append(s)
    return want[::-1], False


def passage(c: Claim, blocks: list[Block]) -> str:
    """From CONTEXT blocks before the first cited block through the item's `from` block (the generator sets
    it at or after the last cited one), plus CONTEXT blocks after the last cited block; at most MAX_BLOCKS,
    keeping the cited blocks and the from block. A chapter-recap sentence gets the whole stretch the recap
    covers (`summary_span`)."""
    last = len(blocks) - 1
    seqs = c.seqs
    if c.kind == "summary":
        want, _ = summary_span(c, blocks)
    else:
        core = sorted({*seqs, *([c.frm] if c.frm is not None else [])})
        if len(core) > MAX_BLOCKS:       # keep the from block and the first cited ones
            core = core[:MAX_BLOCKS - 1] + [core[-1]]
        lo, hi = max(0, core[0] - c.window), min(last, max(core[-1], seqs[-1] + c.window))
        full = list(range(lo, hi + 1))
        if len(full) <= MAX_BLOCKS:
            want = full
        else:   # fill the rest by nearness to a core block
            rest = sorted((b for b in full if b not in core), key=lambda b: (min(abs(b - x) for x in core), b))
            want = sorted({*core, *rest[:MAX_BLOCKS - len(core)]})
    return "\n".join(_line(blocks, s) for s in want)


def _prompt(batch: list[Claim], blocks: list[Block]) -> str:
    parts = []
    for c in batch:
        cut = c.kind == "summary" and summary_span(c, blocks)[1]
        kind = "summary, start of the passage left out" if cut else c.kind
        parts.append(f"{c.cid}: ({kind}) {c.text}\nPassage for {c.cid}:\n{passage(c, blocks)}")
    return AUDIT.format(claims="\n\n".join(parts))


def _picked(index: dict, blocks: list[Block], n: int, seed: int) -> list[Claim]:
    resolved = anchors_mod.resolve(index.get("anchors") or [], blocks)
    return sample(claims_of(index, resolved), n, seed)


def estimate(index: dict, blocks: list[Block], n: int = 50, seed: int = 0) -> dict:
    """What `audit` would send, without calling a model: {"claims", "input_chars", "calls"}. The caller
    prices the run from the real size."""
    picked = _picked(index, blocks, n, seed)
    batches = [picked[i:i + BATCH] for i in range(0, len(picked), BATCH)]
    return {"claims": len(picked), "calls": len(batches),
            "input_chars": sum(len(SYSTEM) + len(_prompt(b, blocks)) for b in batches)}


def audit(index: dict, blocks: list[Block], llm, n: int = 50, seed: int = 0, detail: list | None = None) -> dict:
    """Check up to n sampled claims against the book. Counts satisfy sampled == supported + unsupported +
    unsure; a claim the model gave no verdict for is unsure. If `detail` is a list, one dict per claim
    (verdict, kind, claim, because, passage) is appended to it. No sampling parameters are set here."""
    picked = _picked(index, blocks, n, seed)
    n_ok = {"supported": 0, "unsupported": 0, "unsure": 0}
    for i in range(0, len(picked), BATCH):
        batch = picked[i:i + BATCH]
        out = llm.complete(SYSTEM, _prompt(batch, blocks), AUDIT_SCHEMA)
        verdicts = out.get("verdicts") if isinstance(out, dict) else None
        got: dict[str, tuple[str, str]] = {}
        for v in verdicts if isinstance(verdicts, list) else []:
            if isinstance(v, dict) and v.get("verdict") in n_ok:
                got.setdefault(str(v.get("id", "")).strip(), (v["verdict"], " ".join(str(v.get("because", "")).split())))
        for c in batch:
            verdict, because = got.get(c.cid, ("unsure", "no verdict returned"))
            if verdict == "unsupported" and c.kind == "summary" and summary_span(c, blocks)[1]:
                verdict = "unsure"       # the start of the stretch was left out: it cannot be called unsupported
            n_ok[verdict] += 1
            if detail is not None:
                detail.append({"verdict": verdict, "kind": c.kind, "claim": c.text, "because": because,
                               "passage": passage(c, blocks)})
    return {"sampled": len(picked), **n_ok, "model": getattr(llm, "name", "")}
