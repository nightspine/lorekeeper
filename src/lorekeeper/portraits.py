"""Portraits: profiles written from every passage that mentions an entity, up to a point.

The extraction pass sees one passage at a time, so the profiles it writes are pieced together from
fragments. A portrait is written the way a reader would describe someone after reading up to the
end of a chapter: from all the places the entity has appeared so far, each with a little context,
in reading order (a keyword-in-context pass, like a term X-Ray). Only text up to that point is
sent, and the portrait is revealed at the first anchor after the last passage it read, so it can
never say more than the reader could know there.

Portraits are ordinary profile facets (SPEC.md §4.2): each replaces the entity's previous profile,
including one from an earlier series book. Extraction profiles that a portrait supersedes are
removed; entities mentioned too rarely for a portrait keep theirs.

An entity gets a first portrait once two passages mention it (a character who appears twice a
chapter should not keep a one-line fragment for chapters), and a second one as soon as four
passages do, since the first was written from thin evidence. Later rewrites follow GROWTH and
STALE as before. That roughly doubles the calls on a novel (one call per entity per rewrite,
sampled passages capped by BUDGET): with Haiku it stays at cents, about 20 cents a novel.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import epub, merge, text as text_
from .generate import HINDSIGHT, MAX_COPY, MOMENTARY, SYSTEM, unsupported_tie

MAX_CHARS = 800      # SPEC §6.3 limit for a profile facet
MIN_MENTIONS = 2     # an entity needs this many passages before its first portrait
FULL_MENTIONS = 4    # a portrait written from fewer passages is redone once there are this many
GROWTH = 1.5         # rewrite once mentions grow by half (and by at least FULL_MENTIONS),
STALE = 4            # or once this many chapters have passed with new mentions
MAX_WINDOWS = 40     # passages sent per portrait, sampled evenly, first and last always kept
BUDGET = 24000       # characters of passage text per portrait
CONTEXT = 1          # paragraphs of context either side of a mention
WORKERS = 6

PORTRAIT = """Below are passages from a novel, in reading order: the places so far where {name} ({kind}) appears, each with
a little context. {also}

Write the description of {name} a reader would want in a companion at this point in the book{sheet}. It
describes who or what {name} is, not what has happened: a description, never a plot summary.
- Identity and role in the story, look, nature and manner, abilities, as the passages show them.
- The ties that define it ("Ron's older brother", "Head of Slytherin House"), naming others as the passages do.
- Events only where they lastingly define it (orphaned as a baby, sorted into Gryffindor, revealed to serve
  someone), one short clause each. Leave out errands, trips, scenes and how they turned out.

Rules:
- Use ONLY these passages. You may recognise the book: use nothing you know from elsewhere and hint at nothing later.
- Resolve pronouns carefully; say only what the passages clearly say about {name}, not about others near it.
- What characters merely claim, suspect or rumour is told as such ("said to be", "Hagrid claims").
- Timeless present tense, like a character sheet. Never write "now", "currently", "has begun to", "so far".
- 2 or 3 sentences, at most 70 words, in your own words (quote at most 5 words).
- State a tie, rank, role, status or whereabouts ONLY if these passages say it, even if you know it from the book. If
  they show only a connection ("someone Harker keeps notes for"), state that connection plainly and stop, and do
  not upgrade it ("Harker's fiancée"). Do not narrate the passage and do not pad. Return an empty text only if they show nothing at all
  about who or what {name} is.
{previous}
Passages:
{passages}"""

SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}

KINDS = {"person": "a character", "creature": "a creature", "place": "a place", "organization": "a group",
         "object": "a thing", "concept": "a concept", "event": "an event"}

# Words that open a name without being part of it ("Detective Miller" has no first name "Detective").
TITLES = merge.HONORIFICS | {"detective", "officer", "agent", "constable", "nurse", "senator", "bishop", "cardinal",
                             "admiral", "corporal", "private", "chief", "governor", "deputy", "father", "doc"}

_PROPER = re.compile(r"(?<![.!?:;\"“(]\s)(?<!^)(?<![\w-])[A-Z][a-z]{2,}\b")
_CAPS = re.compile(r"[A-Z][a-z]{2,}")


def _patterns(d: dict) -> dict[str, list[tuple[re.Pattern, str]]]:
    """entity → [(pattern, name's from anchor)] for the names that identify it alone. A first name
    or surname of a person counts too when no other entity shares it ("Harry", not "Potter"); a
    form that is one entity's whole name belongs to that entity ("Holden", not "Jim Holden")."""
    forms: dict[str, list[tuple[str, str, bool]]] = {}
    for e in d["entities"]:
        for n in e.get("names", []):
            v = n["value"].strip()
            parts = [(v, True)]
            if e["type"] == "person" and " " in v and "(" not in v:
                words = [w for w in v.split() if w[:1].isupper() and not w.endswith(".")
                         and w.lower() not in TITLES]
                if len(words) >= 2:
                    parts += [(words[0], False), (words[-1], False)]
            for p, whole in parts:
                if len(p) >= 3:
                    forms.setdefault(p, []).append((e["id"], n["from"], whole))
    out: dict[str, list[tuple[re.Pattern, str]]] = {}
    for form, owners in forms.items():
        exact = {eid for eid, _, whole in owners if whole}
        ids = exact or {eid for eid, _, _ in owners}
        if len(ids) > 1:
            continue
        eid = next(iter(ids))
        at = next(a for i, a, _ in owners if i == eid)
        out.setdefault(eid, []).append((re.compile(r"(?<!\w)" + re.escape(form) + r"(?!\w)"), at))
    return out


def _sample(seqs: list[int], k: int) -> list[int]:
    if len(seqs) <= k:
        return seqs
    step = (len(seqs) - 1) / (k - 1)
    return sorted({seqs[round(i * step)] for i in range(k)})


def _windows(text: dict[int, str], hits: list[int], lo: int, hi: int) -> tuple[str, int]:
    """Passages around the sampled hits, merged where they touch, within the budget.
    Returns (text, last seq used)."""
    ranges: list[list[int]] = []
    for s in _sample(hits, MAX_WINDOWS):
        a, b = max(lo, s - CONTEXT), min(hi, s + CONTEXT)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    out, used, last = [], 0, lo
    for a, b in ranges:
        chunk = "\n".join(text[s] for s in range(a, b + 1) if s in text)
        if used + len(chunk) > BUDGET:
            continue  # skip rather than stop, so later passages still get in
        out.append(chunk)
        used += len(chunk)
        last = max(last, b)
    return "\n…\n".join(out), last


def _checked(text: str, known: set[str], source: str | None = None,
             book: set[tuple[str, ...]] | None = None) -> tuple[str | None, str]:
    """(text, "") if the portrait is usable, else (None, reason). `known` holds the capitalised
    words of the book up to this point; `source` is the passage text the portrait was written
    from, when given, and a tie word (wife, brother...) must occur in it; `book` is the book's
    word shingles, and a portrait may not copy more than 8 consecutive words of it (SPEC §6.3,
    the rule the validator enforces)."""
    text = " ".join(text.split())
    if not text:
        return None, "empty"
    if len(text) > MAX_CHARS:
        return None, "too long"
    if book:
        runs = text_.copied_runs(text, book, MAX_COPY)
        if runs:
            return None, f"copies the book: {runs[0]}"
    bad = HINDSIGHT.search(text) or MOMENTARY.search(text)
    if bad:
        return None, f"wording: {bad.group(0)}"
    if source is not None:
        tie = unsupported_tie(text, source)
        if tie:
            return None, f"tie not in the text: {tie}"
    # A proper name the book hasn't used yet came from the model's memory of it.
    for m in _PROPER.finditer(text):
        w = m.group(0)
        if w not in known and w + "s" not in known and w.removesuffix("s") not in known:
            return None, f"unknown name: {w}"
    return text, ""


DEFAULT_MODEL = "claude-haiku-5-5"   # about 10 cents a novel; Sonnet is a little sharper at 20 times the price


def portraits(docs: list[tuple[dict, epub.Book]], model, log=print, prior: list[dict] = (), progress=None) -> dict:
    """Write portraits into each doc (series reading order), in place. `prior` are earlier series
    books' docs, read only for the profiles a first portrait replaces. `progress(done, total)` is
    called after each chapter."""
    current: dict[str, str] = {}   # entity → id of its latest profile so far in the series
    for d in prior:
        _latest(d, current)
    stats = Counter()
    for d, book in docs:
        stats += _doc(d, book, model, current, log, progress)
    return dict(stats)


def _latest(d: dict, current: dict) -> None:
    seq_of = {a["id"]: a["seq"] for a in d["anchors"]}
    for e in d["entities"]:
        ps = sorted((f for f in e.get("facets", []) if f.get("kind") == "profile"), key=lambda f: seq_of.get(f["from"], 0))
        if ps:
            current[e["id"]] = ps[-1]["id"]


def _doc(d: dict, book: epub.Book, model, current: dict, log, progress=None) -> Counter:
    stats = Counter()
    anchors = d["anchors"]
    seq_of = {a["id"]: a["seq"] for a in anchors}
    if any(book.blocks[a["seq"]].fingerprint != a["fp"] for a in anchors[:: max(1, len(anchors) // 20)]
           if a["seq"] < len(book.blocks)):
        raise ValueError(f"{d['work']['title']}: the EPUB is not the edition this index was built from")
    anchor_seqs = [a["seq"] for a in anchors]
    lo = anchors[0]["seq"]
    hi = seq_of.get(d.get("coverage", {}).get("through"), anchors[-1]["seq"])
    text = {b.seq: b.raw for b in book.blocks[lo:hi + 1]}
    shingles = text_.shingles([w for b in book.blocks for w in b.words], MAX_COPY)
    marks = [c.start - 1 for c in book.chapters if lo < c.start <= hi] + [hi]
    ents = {e["id"]: e for e in d["entities"]}
    pats = _patterns(d)
    hits: dict[str, list[tuple[int, int]]] = {}   # entity → [(seq, seq the name is revealed at)]
    for eid, ps in pats.items():
        found = {}
        for p, at in ps:
            for s, t in text.items():
                if p.search(t):
                    found[s] = min(found.get(s, 1 << 30), seq_of.get(at, hi))
        hits[eid] = sorted(found.items())
    caps = {s: set(_CAPS.findall(t)) for s, t in text.items()}
    known: set[str] = set()       # capitalised words of the book up to the current mark
    upto = lo - 1
    done: Counter = Counter()     # mentions at the entity's last portrait
    since: dict[str, int] = {}    # entity → index of the mark of its last portrait
    last_text: dict[str, str] = {}
    written: dict[str, list[dict]] = {}
    marks = sorted(set(marks))
    for step, mark in enumerate(marks, 1):
        if progress:
            progress(step - 1, len(marks))
        due = []
        for eid, hs in hits.items():
            if seq_of.get(ents[eid]["from"], hi) > mark:
                continue
            seen = [s for s, at in hs if s <= mark and at <= mark]
            n = len(seen)
            if n < MIN_MENTIONS:
                continue
            if done[eid]:
                thin = done[eid] < FULL_MENTIONS <= n   # first portrait came from thin evidence
                grown = n >= max(done[eid] * GROWTH, done[eid] + FULL_MENTIONS)
                stale = step - since[eid] >= STALE and n >= done[eid] + FULL_MENTIONS
                if not (thin or grown or stale):
                    continue
            due.append((eid, seen))
        for s in range(upto + 1, mark + 1):
            known |= caps.get(s, set())
        upto = mark
        if not due:
            continue

        def one(job):
            eid, seen = job
            e = ents[eid]
            visible = [n for n in e["names"] if seq_of.get(n["from"], hi) <= mark]
            names = [n["value"] for n in visible]
            passages, last = _windows(text, seen, lo, mark)
            # Never before the entity, or any name the prompt gives it, is itself revealed.
            last = max([last, seq_of.get(e["from"], hi)] + [seq_of.get(n["from"], hi) for n in visible])
            prev = last_text.get(eid)
            prompt = PORTRAIT.format(
                name=names[0] if names else e["names"][0]["value"], kind=KINDS.get(e["type"], e["type"]),
                also=f"Also called: {', '.join(names[1:6])}." if len(names) > 1 else "",
                sheet=" (as on a character sheet)" if e["type"] in ("person", "creature") else "",
                previous=f"\nYour description from earlier in the book (keep what still holds; correct and extend it):\n{prev}\n" if prev else "",
                passages=passages)
            out = model.complete(SYSTEM, prompt, SCHEMA)
            got = _checked(str((out or {}).get("text", "")), known, passages, shingles)
            if got[1].startswith("tie not in the text"):
                word = got[1].split(": ", 1)[1]
                out = model.complete(SYSTEM, prompt + f'\n\nYour first answer used "{word}": "{out.get("text", "")}"\n'
                                     f'No passage says {word}. Rewrite it without that tie, stating only what the passages show.', SCHEMA)
                got = _checked(str((out or {}).get("text", "")), known, passages, shingles)
            elif got[1].startswith("copies the book"):
                run = got[1].split(": ", 1)[1]
                out = model.complete(SYSTEM, prompt + f'\n\nYour first answer copied the book\'s wording ("{run}"): "{out.get("text", "")}"\n'
                                     "Rewrite it in your own words, quoting at most 5 consecutive words of the passages.", SCHEMA)
                got = _checked(str((out or {}).get("text", "")), known, passages, shingles)
            elif got[1].startswith("wording"):
                # Usually a lasting fact phrased as news ("now works with dragons"): one rewrite.
                word = got[1].split(": ", 1)[1]
                out = model.complete(SYSTEM, prompt + f'\n\nYour first answer used "{word}": "{out.get("text", "")}"\n'
                                     "Rewrite it as a timeless description without that wording.", SCHEMA)
                got = _checked(str((out or {}).get("text", "")), known, passages, shingles)
            return eid, seen, last, got

        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(one, due))
        for eid, seen, last, (t, why) in results:
            if t is None:
                stats["dropped"] += 1
                log(f"    dropped {ents[eid]['names'][0]['value']}: {why}")
                continue
            # Never before the previous portrait: it is the one this replaces.
            last = max(last, seq_of[written[eid][-1]["from"]]) if eid in written else last
            i = min(bisect_left(anchor_seqs, last), len(anchors) - 1)
            at = anchors[i]["id"]
            ev = sorted({a for a in (anchors[j]["id"] for j in (bisect_left(anchor_seqs, s) for s in seen[-8:]))
                         if seq_of[a] <= seq_of[at]} if seen else set(), key=seq_of.get)
            written.setdefault(eid, []).append({"from": at, "evidence": ev or [at], "text": t})
            last_text[eid] = t
            done[eid] = len(seen)
            since[eid] = step
            stats["portraits"] += 1
        log(f"  {d['work']['title']}: through block {mark}, {stats['portraits']} portraits")

    # Extraction profiles from before an entity's first portrait stay as its early description;
    # later ones are superseded.
    for eid, ps in written.items():
        e = ents[eid]
        first = seq_of[ps[0]["from"]]
        old = [f for f in e["facets"] if f.get("kind") == "profile"]
        keep = [f for f in old if seq_of.get(f["from"], hi) <= first]
        gone = {f["id"] for f in old} - {f["id"] for f in keep}
        e["facets"] = [f for f in e["facets"] if f["id"] not in gone]
        stats["superseded"] += len(gone)
        prev = max(keep, key=lambda f: seq_of[f["from"]])["id"] if keep else current.get(eid)
        for n, p in enumerate(ps, 1):
            facet = {"id": f"{d['id']}.{eid}.portrait-{n}", "kind": "profile", "from": p["from"], "basis": "stated",
                     "evidence": p["evidence"], "text": p["text"]}
            if prev:
                facet["replaces"] = [prev]
            e["facets"].append(facet)
            prev = facet["id"]
    _latest(d, current)
    if progress:
        progress(len(marks), len(marks))
    return stats


def clear(d: dict) -> int:
    """Remove a doc's portraits, so they can be written again."""
    gone = {f["id"] for e in d["entities"] for f in e.get("facets", []) if ".portrait-" in f["id"]}
    for e in d["entities"]:
        e["facets"] = [f for f in e.get("facets", []) if f["id"] not in gone]
    return len(gone)
