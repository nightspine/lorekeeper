"""Book arcs: a few plain sentences on what has happened to an entity in one book (SPEC.md §4.2, kind "arc").

Arcs are written from the index, not from the book text: at each chapter end, for every person,
creature and group that this book's checked events name often enough, a cheap model gets the
entity's description, those events (up to that chapter end, none later) and the ties that changed,
and writes one to three sentences on what happened to it in this book. It costs a few cents a
novel and works on any index, with or without the EPUB.

Python decides everything but the words: which entities are due, `from` (the anchor of the latest
event told, never past the chapter end), `evidence` (those events' anchors), `replaces` (this book's
previous arc of the entity) and the ids. The text goes through the portrait checks (no name the
index has not revealed by then, no hindsight or "now" wording, no tie the events and notes do not
state, a length cap), gets one rewrite when it fails one, and is dropped otherwise.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import epub
from .generate import ABOUT_THE_TEXT, MIN_SENTENCE_WORDS
from .portraits import KINDS, SCHEMA, WORKERS, _CAPS, _checked, name_of

MAX_CHARS = 400        # SPEC §6.3 limit for an arc facet
MAX_SENTENCES = 3
MIN_EVENTS = 2         # non-minor events naming an entity before its first arc (one turning point is enough)
NEW_EVENTS = 2         # new non-minor events since its last arc that earn a rewrite (a turning point always does)
MAX_EVENTS = 40        # events sent per arc, sampled evenly, first and last always kept
MAX_EVIDENCE = 12
FALLBACK_MARKS = 12    # chapter ends to use when the index has no chapter recaps and no EPUB is given
TYPES = ("person", "creature", "organization")
DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM = """You keep a spoiler-free reader's companion for a novel. You are given the checked events of one book that
involve one character, up to the end of a chapter, and you write what has happened to that character in this book.
Use ONLY what you are given. Never use outside knowledge of this book, its author or its sequels.
Describe things as a careful reader understands them at this point: no hindsight, no hints about what comes later."""

ARC = """These are the events of {book}, in order, that involve {name} ({kind}), up to the end of {where}.
{also}{profile}{ties}{previous}
Write what has happened to {name} in this book, as a reader who has read this far knows it: 1 to 3 sentences, at most
60 words, plain and general. Keep the main events that happened to {name}: what {name} did, what was done to {name}, and
where things stand, in the order they happened.

Rules:
- Use ONLY the events below. The description is context for who {name} is; do not repeat it or events of earlier books.
- Past tense, in your own words (quote at most 5 words). Never write "now", "currently", "so far" or hint at anything later.
- What characters merely claim or suspect is told as such. Name others as the events do.
- State a tie (wife, brother, heir...) ONLY if the events or notes say it. Do not upgrade a connection.
- Never mention events, notes, passages, chapters or "the book". Do not list; compress.

Events:
{events}"""

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _ord(d: dict) -> dict[str, int]:
    return {a["id"]: i for i, a in enumerate(d["anchors"])}


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text.strip()) if s]


def clear(d: dict) -> int:
    """Remove a doc's arcs, so they can be written again."""
    n = 0
    for e in d["entities"]:
        keep = [f for f in e.get("facets", []) if f.get("kind") != "arc"]
        n += len(e.get("facets", [])) - len(keep)
        e["facets"] = keep
    return n


def marks_for(d: dict, book: epub.Book | None = None) -> list[int]:
    """Anchor ordinals of the chapter ends: from the EPUB's chapters when given, else from the index's
    chapter recaps (one per chapter, the last of a long chapter's parts), else evenly spaced."""
    ords = _ord(d)
    last = len(d["anchors"]) - 1
    if book is not None:
        seqs = [a["seq"] for a in d["anchors"]]
        starts = sorted(c.start for c in book.chapters if d["anchors"][0]["seq"] < c.start)
        found = {bisect_left(seqs, s) - 1 for s in starts}
        marks = {m for m in found if 0 <= m < last}
    else:
        marks, prev = set(), None
        recaps = [r for r in d.get("recaps", []) if r.get("scope") == "chapter" and r.get("from") in ords]
        for r in recaps:
            key = r.get("title") or r["id"]
            if prev is not None and key == prev[0]:
                marks.discard(prev[1])
            marks.add(ords[r["from"]])
            prev = (key, ords[r["from"]])
        if not marks:
            step = max(1, len(d["anchors"]) // FALLBACK_MARKS)
            marks = set(range(step - 1, last, step))
    through = ords.get(d.get("coverage", {}).get("through"), last)
    return sorted({m for m in marks if m < through} | {through})


def _words(*texts: str) -> set[str]:
    return {w for t in texts for w in _CAPS.findall(t)}


def _prior_context(prior: list[dict]) -> tuple[set[str], dict[str, str]]:
    """Capitalised words of the earlier books (all finished before this one) and each entity's last profile."""
    known: set[str] = set()
    profile: dict[str, str] = {}
    for d in prior:
        ords = _ord(d)
        for e in d["entities"]:
            known |= _words(*(n["value"] for n in e.get("names", [])))
            ps = sorted((f for f in e.get("facets", []) if f.get("kind") == "profile" and f["from"] in ords),
                        key=lambda f: ords[f["from"]])
            if ps:
                profile[e["id"]] = ps[-1]["text"]
                known |= _words(ps[-1]["text"])
        for ev in d.get("events", []):
            known |= _words(ev.get("summary", ""))
        for r in d.get("recaps", []):
            known |= _words(r.get("text", ""))
    return known, profile


def arcs(docs: list[tuple[dict, epub.Book | None]], model, log=print, prior: list[dict] = (), progress=None) -> dict:
    """Write arcs into each doc (series reading order), in place. `prior` are earlier series books' docs,
    read only for the descriptions and names that are already public. `progress(done, total)` is called
    after each chapter end."""
    stats = Counter()
    seen = list(prior)
    for d, book in docs:
        stats += _doc(d, book, model, seen, log, progress)
        seen.append(d)
    return dict(stats)


def _doc(d: dict, book: epub.Book | None, model, prior: list[dict], log, progress=None) -> Counter:
    stats = Counter()
    ords = _ord(d)
    anchors = d["anchors"]
    marks = marks_for(d, book)
    title = d["work"]["title"]
    ents = {e["id"]: e for e in d["entities"]}
    prior_known, prior_profile = _prior_context(prior)

    def names_at(e: dict, upto: int) -> list[str]:
        return [n["value"] for n in e["names"] if ords.get(n["from"], 1 << 30) <= upto]

    events = sorted((ev for ev in d.get("events", []) if ev.get("from") in ords), key=lambda ev: ords[ev["from"]])
    by_ent: dict[str, list[dict]] = {}
    for ev in events:
        if ev.get("kind") == "minor":
            continue
        for eid in dict.fromkeys(ev.get("entities", [])):
            if eid in ents and ents[eid]["type"] in TYPES:
                by_ent.setdefault(eid, []).append(ev)

    # Where an earlier run left off: the entity's last arc in this book.
    written: dict[str, list[dict]] = {}
    covered: dict[str, int] = {}
    for eid, e in ents.items():
        mine = sorted((f for f in e.get("facets", []) if f.get("kind") == "arc" and f["from"] in ords),
                      key=lambda f: ords[f["from"]])
        if mine:
            written[eid] = mine
            covered[eid] = ords[mine[-1]["from"]]
    count = Counter({eid: len(v) for eid, v in written.items()})

    def label(ordinal: int) -> str:
        return f"chapter {bisect_left(marks, ordinal) + 1}"

    for step, mark in enumerate(marks, 1):
        if progress:
            progress(step - 1, len(marks))
        known = set(prior_known)
        for e in d["entities"]:
            known |= _words(*names_at(e, mark))
        known |= _words(*(ev.get("summary", "") for ev in events if ords[ev["from"]] <= mark))
        known |= _words(*(r.get("text", "") for r in d.get("recaps", []) if ords.get(r.get("from"), 1 << 30) <= mark))
        due = []
        for eid, evs in by_ent.items():
            vis = [ev for ev in evs if ords[ev["from"]] <= mark]
            new = [ev for ev in vis if ords[ev["from"]] > covered.get(eid, -1)]
            if not new or ords.get(ents[eid]["from"], 1 << 30) > mark:
                continue
            turns = [ev for ev in new if ev.get("kind") == "turn"]
            if eid in written:
                if not (turns or len(new) >= NEW_EVENTS):
                    continue
            elif not (len(vis) >= MIN_EVENTS or turns):
                continue
            due.append((eid, vis))
        if not due:
            continue

        def one(job):
            eid, vis = job
            e = ents[eid]
            picked = _sample(vis, MAX_EVENTS)
            last = max(ords[ev["from"]] for ev in picked)
            names = names_at(e, last)
            shown = name_of(e, names)
            prev = written[eid][-1]["text"] if eid in written else None
            profile = _profile(e, ords, last) or prior_profile.get(eid)
            ties = _ties(d, eid, ents, ords, last, names_at)
            lines = [f"- ({label(ords[ev['from']])}{', turning point' if ev.get('kind') == 'turn' else ''}) {ev['summary']}"
                     for ev in picked]
            prompt = ARC.format(
                book=title, name=shown, kind=KINDS.get(e["type"], e["type"]), where=label(mark),
                also=f"Also called: {', '.join(names[1:6])}.\n" if len(names) > 1 else "",
                profile=f"\nWho {shown} is (context only): {profile}\n" if profile else "",
                ties=f"\nTies that came up in this book: {'; '.join(ties)}\n" if ties else "",
                previous=f"\nYour summary from earlier in this book (keep what still matters, add what happened since):\n{prev}\n" if prev else "",
                events="\n".join(lines))
            source = "\n".join([ev["summary"] for ev in picked] + ties + [profile or "", prev or ""]
                               + [f["text"] for f in e.get("facets", []) if f.get("kind") != "arc" and ords.get(f["from"], 1 << 30) <= last])
            got = _write(model, prompt, known, source)
            return eid, picked, last, got

        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(one, due))
        for eid, picked, last, (t, why) in results:
            if t is not None and why:
                stats[why] += 1
            if t is None:
                stats["dropped"] += 1
                stats[f"dropped: {why.split(':')[0]}"] += 1
                log(f"    dropped {ents[eid]['names'][0]['value']}: {why}")
                continue
            e = ents[eid]
            # Never before the entity or the names the prompt gave it are themselves revealed.
            at = max([last, ords.get(e["from"], 0)] + [ords[n["from"]] for n in e["names"] if ords.get(n["from"], 1 << 30) <= last])
            evid = sorted({a for ev in picked for a in ev.get("evidence", [ev["from"]]) if ords.get(a, 1 << 30) <= at}
                          | {anchors[at]["id"]}, key=ords.get)[-MAX_EVIDENCE:]
            prev_arc = written.get(eid, [])
            if prev_arc and prev_arc[-1]["from"] == anchors[at]["id"]:
                stats["superseded"] += 1   # the same reveal point: the rewrite stands in for the earlier one
                _remove(e, prev_arc.pop())
            count[eid] += 1
            facet = {"id": f"{d['id']}.{eid}.arc-{count[eid]}", "kind": "arc", "from": anchors[at]["id"], "basis": "stated",
                     "evidence": evid, "text": t}
            if prev_arc:
                facet["replaces"] = [prev_arc[-1]["id"]]
            e.setdefault("facets", []).append(facet)
            written.setdefault(eid, []).append(facet)
            covered[eid] = max(ords[ev["from"]] for ev in picked)
            stats["arcs"] += 1
        log(f"  {title}: through {label(mark)}, {stats['arcs']} arcs")
    if progress:
        progress(len(marks), len(marks))
    return stats


def _remove(e: dict, facet: dict) -> None:
    e["facets"] = [f for f in e["facets"] if f is not facet]


def _sample(items: list, k: int) -> list:
    if len(items) <= k:
        return items
    step = (len(items) - 1) / (k - 1)
    return [items[i] for i in sorted({round(j * step) for j in range(k)})]


def _profile(e: dict, ords: dict, upto: int) -> str | None:
    ps = sorted((f for f in e.get("facets", []) if f.get("kind") == "profile" and ords.get(f["from"], 1 << 30) <= upto),
                key=lambda f: ords[f["from"]])
    return ps[-1]["text"] if ps else None


def _ties(d: dict, eid: str, ents: dict, ords: dict, upto: int, names_at) -> list[str]:
    """The entity's ties up to a point, as short sentences, newer ties replacing the ones they replace."""
    rels = [r for r in d.get("relations", []) if eid in (r["src"], r["dst"]) and ords.get(r["from"], 1 << 30) <= upto]
    gone = {x for r in rels for x in r.get("replaces", [])}
    out = []
    for r in rels:
        if r["id"] in gone or r["src"] not in ents or r["dst"] not in ents:
            continue
        a, b = (name_of(ents[x], names_at(ents[x], upto)) for x in (r["src"], r["dst"]))
        out.append(f"{a}, {r['label']}, {b}")
    return out[-8:]


def _problem(text: str) -> str | None:
    if len(sentences(text)) > MAX_SENTENCES:
        return "too many sentences"
    if any(len(s.split()) < MIN_SENTENCE_WORDS for s in sentences(text)):
        return "not a sentence"
    if ABOUT_THE_TEXT.search(text):
        return "about the text"
    return None


def _write(model, prompt: str, known: set[str], source: str) -> tuple[str | None, str]:
    """(text, "") or (text, "rewritten: ...") if usable, else (None, reason). One rewrite for a failed check."""
    def check(out) -> tuple[str | None, str]:
        t = str((out or {}).get("text", ""))
        got = _checked(t, known, source, None, limit=MAX_CHARS)
        if got[0] and (why := _problem(got[0])):
            return None, why
        return got

    out = model.complete(SYSTEM, prompt, SCHEMA)
    got = check(out)
    if got[0] or got[1] == "empty":
        return got
    why = got[1]
    advice = {
        "too long": "It is over 400 characters. Rewrite it shorter, in at most 3 sentences.",
        "too many sentences": "It has more than 3 sentences. Rewrite it in at most 3.",
        "not a sentence": "It has a fragment or a label. Rewrite it as complete sentences.",
        "about the text": "It talks about the text. Tell what happened to the character instead.",
    }.get(why)
    if advice is None:
        kind = why.split(":")[0]
        word = why.split(": ", 1)[1] if ": " in why else ""
        advice = {
            "tie not in the text": f'No event says "{word}". Rewrite it without that tie, stating only what the events show.',
            "wording": f'It used "{word}". Rewrite it as plain past events without that wording.',
            "unknown name": f'"{word}" is not named in the events. Rewrite it without that name.',
            "copies the book": "Rewrite it in your own words.",
        }.get(kind)
    if advice is None:
        return got
    again = model.complete(SYSTEM, prompt + f'\n\nYour first answer was: "{(out or {}).get("text", "")}"\n{advice}', SCHEMA)
    retry = check(again)
    return (retry[0], f"rewritten: {why.split(':')[0]}") if retry[0] else retry
