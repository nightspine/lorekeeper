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
MAX_NOTES = 8        # the entity's checked facts shown with the passages

# A fact that records a lasting change in standing: it earns the entity a new portrait at the next
# chapter end, however few new mentions there were, so the description never lags the story.
STATUS = re.compile(r"\b(dead|dies|died|death|killed|murdered|slain|buried|married|marries|marriage|wedding|wed|"
                    r"widow(ed|er)?|engaged|betrothed|captured|captive|prisoner|imprisoned|escaped|freed|"
                    r"wounded|injured|crippled|blinded|recovered|turned|transformed|knighted|promoted|"
                    r"appointed|crowned|exiled|banished|disgraced|betrayed|revealed|unmasked|disguised|"
                    r"orphaned|adopted|pregnant|arrested|sentenced|pardoned|vanished|missing|destroyed)\b", re.IGNORECASE)
# "near death", "feared dead", "as if dead": a status word that states no change. Quoting "found Lucy near death"
# as a change in standing made Haiku write "Dead, ... buried the same day as her mother" chapters early.
HEDGED = re.compile(r"\b(near|nearly|almost|close to|on the point of|feared|fears?|thought|believed|seem(ed|s)?|as if|"
                    r"like|half|await(s|ed|ing)?|fac(e|es|ed|ing)|risk(s|ed|ing)?|escap(e|es|ed|ing)|cheat(s|ed|ing)?|"
                    r"feign(s|ed|ing)?)\s+(\w+\s+){0,2}?(dead|death|dying|dies|died)\b", re.IGNORECASE)


# A death never comes undone (an Un-Dead Lucy is still dead), so a portrait that knows of one must open with it;
# other changes in standing can reverse ("captured", then "escaped") and only need some status word.
DEATH = re.compile(r"\b(dead|dies|died|death|killed|murdered|slain|buried|destroyed)\b", re.IGNORECASE)


_OBJECT = re.compile(r"\s+(?:(?:the|a|an|his|her|their|its|my|our|your|him|them|it|one|this|that|these|those|some|all|"
                     r"every)\b|[A-Z])")
# A death verb tells the subject's own death only in the passive or opening a clause: "is killed", "was buried",
# "Killed by...", "Dead, slain by...".
_PASSIVE = re.compile(r"(\b(?:is|was|were|are|be|been|being|got|gets|lies|lay)|^|[.!?:;,\u2014(])$", re.IGNORECASE)
_TRANSITIVE = {"killed", "murdered", "slain", "buried", "destroyed"}


def states_death(text: str, at: list[int] | None = None) -> bool:
    """Whether `text` states a death, read by code without a list of deaths: a DEATH word that is not hedged ("near death",
    "awaiting death"), not a proper noun (a capital inside a sentence: "a hunter of the Dead", the vampires of Empire of the
    Vampire), and not a verb with an object ("buried the silver"). `at`, when given, collects the match positions."""
    text = HEDGED.sub(lambda m: " " * len(m.group(0)), text)
    found = False
    for m in DEATH.finditer(text):
        w, before = m.group(0), text[:m.start()].rstrip()
        if w[0].isupper() and before and before[-1] not in ".!?:;\"“(":
            continue
        if text[m.start() - 1:m.start()] == "-" or text[m.end():m.end() + 1] == "-":
            continue                    # a compound: "death-pale", "half-dead"
        if w.lower() in _TRANSITIVE and not re.match(r"\s+by\b", text[m.end():]):
            passive = _PASSIVE.search(before)
            if not passive or (not passive.group(1).strip().isalpha() and _OBJECT.match(text, m.end())):
                continue                # a deed: "the locals destroyed", "Buried her mother" (a note's subject is its entity)
        found = True
        if at is None:
            return True
        at.append(m.start())
    return found


def _opens_with(change: str, text: str) -> bool:
    """Whether `text`'s first sentence states the kind of change `change` records."""
    first = _first(text)
    return states_death(first) if states_death(change) else bool(STATUS.search(HEDGED.sub(" ", first)))


def opens_with_death(text: str) -> bool:
    """Whether a description's first sentence states a death (`states_death`: not "a hunter of the Dead"), as the prompt asks
    a dead character's to."""
    return states_death(_first(text))


# Whose death a first sentence states is grammar ("suspects Aaron wants him dead", "named among the dead"), so when code sees
# a death word there a cheap call answers one yes or no, cached per text; nothing it writes is kept.
OWN_DEATH = """Below is the first sentence of a description of {name} ({kind}) from a reader's companion to a novel. Does it say
that {name} is dead (died, was killed, slain or destroyed)? Answer "dead": true only when it says {name} itself is dead;
false when it speaks of others' deaths, of the dead or the undead as a group, of danger, wishes, fears or rumours.

{sentence}"""
OWN_DEATH_SCHEMA = {"type": "object", "required": ["dead"], "properties": {"dead": {"type": "boolean"}}}


def own_death(model, name: str, kind: str, text: str) -> bool:
    """Whether a description's first sentence says the entity itself is dead: code first (`opens_with_death`), then one
    cheap yes or no. A failed call keeps the death (the rule as it was)."""
    if not opens_with_death(text):
        return False
    try:
        out = model.complete(LOOKS_SYSTEM, OWN_DEATH.format(name=name, kind=kind, sentence=_first(text)), OWN_DEATH_SCHEMA) or {}
    except Exception:   # noqa: BLE001
        return True
    return out.get("dead") is not False


def changes_standing(text: str) -> bool:
    """Whether a fact or turning point records a lasting change in standing (not a hedged "near death")."""
    return bool(STATUS.search(HEDGED.sub(" ", text)))


# A stated look: a rewrite is free to leave out what the previous description said a character looks like, and over a long
# book the lasting details fell away one rewrite at a time (Empire of the Vampire: "dark skin" in five descriptions of
# Baptiste in a row, then in none). The previous description is cut into short pieces by punctuation and joining words,
# a cheap call picks the pieces that tell a lasting look (numbers only, nothing it writes is kept), the new description is
# told to keep them unless the passages show a change, and code checks that each picked piece's content words are still
# there, or that the reply names it as changed and still speaks of the same thing. No list of look words anywhere.
LOOKS = """Below is a short description of {name} ({kind}) from a reader's companion to a novel, cut into numbered pieces.
Which pieces tell how {name} lastingly looks to the eye: body, build, face, skin, hair, eyes, size, age as it shows, the
marks, scars or tattoos on it, how it dresses or what it always carries; for a group, how its members look? Leave out
pieces about character, voice, manner, rank, role, history, ties or deeds, and how it looks at one passing moment.
Return "looks": the numbers of those pieces (an empty list when none).

{pieces}"""
LOOKS_SCHEMA = {"type": "object", "required": ["looks"],
                "properties": {"looks": {"type": "array", "items": {"type": "integer"}}}}
LOOKS_SYSTEM = "You sort the sentences of a reader's companion. Answer only with the numbers asked for."
# The portrait's own reply when it is given looks to keep: the text, and the numbers of the looks the passages show changed.
LOOK_SCHEMA = {"type": "object", "required": ["text"],
               "properties": {"text": {"type": "string"}, "changed": {"type": "array", "items": {"type": "integer"}}}}
# Code reads words, so "bent-backed" for "stooped" looks like a loss: a loss code finds is confirmed by a second cheap call
# that only picks numbers among the ones code flagged, so it can clear a flag and never add one.
LOST = """Below is a new description of {name} ({kind}) from a reader's companion to a novel, and a numbered list of details an
earlier description gave of how {name} looks. Which numbered details does the new description not carry at all? One it
states in other words is carried, and so is one it states as changed. Return "lost": those numbers (an empty list when it
carries every one).

New description: {text}

{looks}"""
LOST_SCHEMA = {"type": "object", "required": ["lost"],
               "properties": {"lost": {"type": "array", "items": {"type": "integer"}}}}
_JOINS = re.compile(r"(?<=[.!?])\s+|\s*[,;:()\u2014\u2013]\s*|\s+(?:and|with|but|while|who|whose|which)\s+", re.IGNORECASE)
MAX_LOOKS = 12       # pieces of the previous description kept as looks, at most


def _pieces(text: str) -> list[str]:
    """The previous description cut at sentence ends, punctuation and joining words, each piece with a content word."""
    return [x.strip(" .") for x in _JOINS.split(" ".join(text.split())) if x and _content(x)]


def _words(text: str) -> list[str]:
    """Lowercased words, split at hyphens too ("dark-skinned" is "dark", "skinned")."""
    return re.findall(r"[^\W_]+", text.casefold())


def _content(text: str) -> list[str]:
    from .ask import STOP
    return [w for w in _words(text) if len(w) >= 3 and w not in STOP]


def _same(w: str, t: str) -> bool:
    """One word, allowing an ending ("skin", "skinned"; "scarred", "scars")."""
    if w == t:
        return True
    n = 0
    for a, b in zip(w, t):
        if a != b:
            break
        n += 1
    return n >= 4 and n >= min(len(w), len(t)) - 2


def _missing(piece: str, words: list[str]) -> list[str]:
    return [w for w in _content(piece) if not any(_same(w, t) for t in words)]


def kept(piece: str, text: str) -> bool:
    """Whether `text` still carries `piece`: at most a third of its content words gone."""
    gone = _missing(piece, _words(text))
    return len(gone) <= len(_content(piece)) // 3


def lost_looks(looks: list[str], text: str, changed: set[int] = frozenset()) -> list[int]:
    """The numbers (from 1) of `looks` that `text` no longer carries. One the reply names as changed counts as kept when the
    text still speaks of the same thing (a content word in common: "grey hair" for "black hair")."""
    words = _words(text)
    out = []
    for n, piece in enumerate(looks, 1):
        if kept(piece, text):
            continue
        if n in changed and len(_missing(piece, words)) < len(_content(piece)):
            continue
        out.append(n)
    return out


def _numbers(out: dict, key: str, top: int) -> list[int]:
    got = []
    for x in out.get(key) or [] if isinstance(out.get(key), list) else []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= top and n not in got:
            got.append(n)
    return got


def confirm_lost(model, name: str, kind: str, looks: list[str], text: str, flagged: list[int]) -> list[int]:
    """Of the looks code flagged as gone from `text`, those a cheap call confirms are not carried in any wording. A failed
    call confirms them all."""
    if not flagged:
        return []
    try:
        out = model.complete(LOOKS_SYSTEM, LOST.format(name=name, kind=kind, text=text, looks="\n".join(
            f"{i}. {looks[n - 1]}" for i, n in enumerate(flagged, 1))), LOST_SCHEMA) or {}   # numbered as shown
    except Exception:   # noqa: BLE001 - no answer: what code found stands
        return flagged
    return [flagged[i - 1] for i in _numbers(out, "lost", len(flagged))]


def look_pieces(model, name: str, kind: str, text: str) -> list[str]:
    """The pieces of a description that tell a lasting look, picked by one cheap call (numbers only), all of them; the caller
    keeps MAX_LOOKS and counts the rest."""
    pieces = list(dict.fromkeys(_pieces(text)))
    if not pieces:
        return []
    out = model.complete(LOOKS_SYSTEM, LOOKS.format(name=name, kind=kind, pieces="\n".join(
        f"{n}. {x}" for n, x in enumerate(pieces, 1))), LOOKS_SCHEMA) or {}
    return [pieces[n - 1] for n in _numbers(out, "looks", len(pieces))]


BUDGET = 24000       # characters of passage text per portrait
CONTEXT = 1          # paragraphs of context either side of a mention
WORKERS = 6

PORTRAIT = """Below are passages from a novel, in reading order: the places so far where {name} ({kind}) appears, each with
a little context. {also}{notes}

Write the description of {name} a reader would want in a companion at this point in the book{sheet}. It
describes who or what {name} is, not what has happened: a description, never a plot summary.
- Identity and role in the story, look, nature and manner, abilities, as the passages show them.
- The ties that define it ("Ron's older brother", "Head of Slytherin House"), naming others as the passages do.
- Events only where they lastingly define it (orphaned as a baby, sorted into Gryffindor, revealed to serve
  someone), one short clause each. Leave out errands, trips, scenes and how they turned out.
- A lasting change in standing that the passages show (a death, a marriage, a capture or an escape, a turning,
  a revealed identity, a new rank) goes in the FIRST sentence, stated plainly ("Dead, killed by..."; "Married to
  Jonathan Harker"), before who {name} was until then. A reader opening the entry must see at once where things
  stand, never a description that stopped being true chapters ago.

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
{previous}{looks}
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


def _windows(text: dict[int, str], hits: list[int], lo: int, hi: int, since: int | None = None) -> tuple[str, int]:
    """Passages around the sampled hits, merged where they touch, within the budget.
    Returns (text, last seq used). With `since` (the mark of the previous description, which the new one builds on), the
    mentions after it get up to half the windows and the budget first: sampled evenly over the whole book so far they were
    a few windows among forty, and a character's new look (Baptiste's cropped hair, Empire of the Vampire) was never sent."""
    new = [s for s in hits if since is not None and s > since]
    old = [s for s in hits if since is None or s <= since]
    k = min(len(new), MAX_WINDOWS // 2)
    ranges: list[list[int]] = []
    for s in sorted(_sample(old, MAX_WINDOWS - k) + _sample(new, k)):
        a, b = max(lo, s - CONTEXT), min(hi, s + CONTEXT)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    first = [r for r in ranges if since is not None and r[1] > since] + [r for r in ranges if since is None or r[1] <= since]
    out, used, last = [], 0, lo
    for a, b in first:
        chunk = "\n".join(text[s] for s in range(a, b + 1) if s in text)
        if used + len(chunk) > BUDGET:
            continue  # skip rather than stop, so later passages still get in
        out.append((a, chunk))
        used += len(chunk)
        last = max(last, b)
    return "\n…\n".join(c for _, c in sorted(out)), last


def _first(text: str) -> str:
    """The first sentence."""
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]


def name_of(e: dict, names: list[str]) -> str:
    return names[0] if names else e["names"][0]["value"]


def _checked(text: str, known: set[str], source: str | None = None,
             book: set[tuple[str, ...]] | None = None, limit: int = MAX_CHARS) -> tuple[str | None, str]:
    """(text, "") if the portrait is usable, else (None, reason). `known` holds the capitalised
    words of the book up to this point; `source` is the passage text the portrait was written
    from, when given, and a tie word (wife, brother...) must occur in it; `book` is the book's
    word shingles, and a portrait may not copy more than 8 consecutive words of it (SPEC §6.3,
    the rule the validator enforces)."""
    text = " ".join(text.split())
    if not text:
        return None, "empty"
    if len(text) > limit:
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
    upto_mark: dict[str, int] = {}   # entity → the mark its last portrait was written at
    # entity → seqs of its checked facts that record a change in standing
    # A change in standing is a checked fact ("Has died") or a turning point naming the entity ("Lucy dies"):
    # the extraction records a death as either, and Lucy's was only an event in one Dracula build.
    turns: dict[str, list[tuple[int, str]]] = {}
    for ev in d.get("events", []):
        if ev.get("kind") == "turn" and ev["from"] in seq_of and changes_standing(ev.get("summary", "")):
            for eid in ev.get("entities", []):
                turns.setdefault(eid, []).append((seq_of[ev["from"]], ev["summary"]))
    standing: dict[str, list[tuple[int, str]]] = {
        e["id"]: sorted([(seq_of[f["from"]], f["text"]) for f in e["facets"]
                         if f.get("kind") not in ("profile", "arc") and f["from"] in seq_of and changes_standing(f["text"])]
                        + turns.get(e["id"], []))
        for e in d["entities"]}
    status: dict[str, list[int]] = {eid: [q for q, _ in xs] for eid, xs in standing.items()}
    # entity → seqs where the companion records its own death: a checked fact of its that states a death, or a turning point
    # whose death word comes nearest after this entity's name among the entities it names ("Arthur watches as Lucy dies")
    deaths: dict[str, list[int]] = {
        e["id"]: [seq_of[f["from"]] for f in e["facets"] if f.get("kind") not in ("profile", "arc") and f["from"] in seq_of
                  and states_death(f["text"])] for e in d["entities"]}
    for ev in d.get("events", []):
        if ev.get("kind") != "turn" or ev["from"] not in seq_of:
            continue
        at: list[int] = []
        if not states_death(ev.get("summary", ""), at):
            continue
        named = [x for x in dict.fromkeys(ev.get("entities", [])) if x in ents]
        for p in at:
            if len(named) == 1:
                dead = named[0]
            else:
                last = {x: max((m.end() for pat, _ in pats.get(x, ()) for m in pat.finditer(ev["summary"][:p])), default=-1)
                        for x in named}
                dead = max(last, key=last.get) if max(last.values(), default=-1) >= 0 else None
            if dead:
                deaths.setdefault(dead, []).append(seq_of[ev["from"]])
    # "Aaron killed Danton": a death verb with a name as its object is the death of the entity named there
    for ev in d.get("events", []):
        if ev.get("kind") != "turn" or ev["from"] not in seq_of:
            continue
        summary = HEDGED.sub(lambda m: " " * len(m.group(0)), ev.get("summary", ""))
        for m in DEATH.finditer(summary):
            if m.group(0).lower() not in _TRANSITIVE:
                continue
            rest = summary[m.end():]
            at = m.end() + len(rest) - len(rest.lstrip())
            for x in dict.fromkeys(ev.get("entities", [])):
                if x in ents and any(pat.match(summary, at) for pat, _ in pats.get(x, ())):
                    deaths.setdefault(x, []).append(seq_of[ev["from"]])
    last_text: dict[str, str] = {}
    look_cache: dict[str, list[str]] = {}    # description → its look pieces
    dead_cache: dict[str, bool] = {}         # description → whether its first sentence says the entity is dead
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
                changed = any(upto_mark[eid] < s <= mark for s in status.get(eid, ()))
                if not (thin or grown or stale or changed):
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
            passages, last = _windows(text, seen, lo, mark, upto_mark.get(eid))
            # Never before the entity, or any name the prompt gives it, is itself revealed.
            last = max([last, seq_of.get(e["from"], hi)] + [seq_of.get(n["from"], hi) for n in visible])
            prev = last_text.get(eid)
            stats_one: Counter = Counter()
            # What the previous description of this book said it looks like (the last portrait, else the latest extraction
            # profile up to here; an earlier book's is never used, it may tell what a reader of this one has not read).
            told, told_seq = prev, None
            if not told:
                ps = [f for f in e["facets"] if f.get("kind") == "profile" and seq_of.get(f["from"], hi) <= mark]
                if ps:
                    f = max(ps, key=lambda f: seq_of[f["from"]])
                    told, told_seq = f["text"], seq_of[f["from"]]
            looks: list[str] = []
            if told:
                if told not in look_cache:
                    try:
                        picked = look_pieces(model, name_of(e, names), KINDS.get(e["type"], e["type"]), told)
                    except Exception as err:   # noqa: BLE001 - a failed pick only means no looks to keep
                        log(f"    looks of {e['names'][0]['value']}: {err}")
                        picked = []
                    if len(picked) > MAX_LOOKS:     # never a silent loss: counted once per description
                        stats_one["looks over the cap"] += len(picked) - MAX_LOOKS
                    look_cache[told] = picked[:MAX_LOOKS]
                looks = look_cache[told]
            if looks and told_seq is not None:
                last = max(last, told_seq)    # never before the description whose looks it keeps
            # The companion's own checked notes up to here: a status fact ("has died") may sit in a
            # paragraph that never names the entity, so the passages alone can miss it.
            noted = [f["text"] for f in e["facets"] if f.get("kind") not in ("profile", "arc")
                     and seq_of.get(f["from"], hi) <= mark][-MAX_NOTES:]
            prompt = PORTRAIT.format(
                name=names[0] if names else e["names"][0]["value"], kind=KINDS.get(e["type"], e["type"]),
                also=f"Also called: {', '.join(names[1:6])}." if len(names) > 1 else "",
                notes=("\nWhat the companion has noted about " + (names[0] if names else e["names"][0]["value"])
                       + " from the book so far: " + " / ".join(noted)) if noted else "",
                sheet=" (as on a character sheet)" if e["type"] in ("person", "creature") else "",
                previous=f"\nYour description from earlier in the book (keep what still holds; correct and extend it):\n{prev}\n" if prev else "",
                looks=("\nHow earlier descriptions said " + name_of(e, names) + " looks, numbered:\n"
                       + "\n".join(f"{n}. {x}" for n, x in enumerate(looks, 1))
                       + "\nKeep every one of these in your description (their words are yours to reuse) unless these passages show it "
                       "changed; then say how it is now and give its number in \"changed\". Shorten other things first.\n")
                if looks else "",
                passages=passages)
            # A change in standing the notes record since the last portrait is spelled out, not left among the
            # notes: told only "her death is reported", Haiku still described Lucy as recovering.
            since_mark = upto_mark.get(eid, lo - 1)
            recent = [(q, t) for q, t in standing.get(eid, ()) if since_mark < q <= mark][-3:]
            changes = [t for _, t in recent]
            # A portrait that is told of a change is never revealed before it: the paragraphs around Lucy's death
            # need not name her, and the last mention alone would have put "Dead" before she dies.
            if recent:
                last = max(last, recent[-1][0])
            if changes:
                prompt += ("\n\nThe companion's checked notes and turning points record a change in standing since your last description: "
                           + " / ".join(f'"{c}"' for c in changes)
                           + f"\nIf the passages show it is {name_of(e, names)}'s own, your FIRST sentence states where "
                           f"{name_of(e, names)} stands now (\"Dead, ...\"), before anything else. Never mention these notes, and state "
                           "nothing the passages and notes do not show.")
            # A tie may rest on what the companion already checked up to here (notes, earlier profiles, the last
            # portrait), not only on the sampled passages: Lucy's "engaged" sits in letters the sample can miss.
            source = "\n".join([passages, prev or ""] + [f["text"] for f in e["facets"]
                                                    if f.get("kind") != "arc" and seq_of.get(f["from"], hi) <= mark])
            schema = LOOK_SCHEMA if looks else SCHEMA
            changed: dict[str, set[int]] = {}    # reply text → the looks it names as changed

            def ask(p: str) -> dict:
                r = model.complete(SYSTEM, p, schema) or {}
                c = r.get("changed") if isinstance(r.get("changed"), list) else []
                changed[" ".join(str(r.get("text", "")).split())] = {x for x in c if isinstance(x, int)}
                return r

            out = ask(prompt)
            got = _checked(str((out or {}).get("text", "")), known, source, shingles)
            if changes and got[0] and not _opens_with(" ".join(changes), got[0]):
                again = ask(prompt + f'\n\nYour first answer was: "{got[0]}"\nIts first sentence does '
                                       "not state the change in standing. Rewrite it so the first sentence does.")
                retry = _checked(str((again or {}).get("text", "")), known, source, shingles)
                if retry[0]:
                    got = (retry[0], "rewritten: change in standing")
            if got[1].startswith("tie not in the text"):
                word = got[1].split(": ", 1)[1]
                out = ask(prompt + f'\n\nYour first answer used "{word}": "{out.get("text", "")}"\n'
                                     f'No passage says {word}. Rewrite it without that tie, stating only what the passages show.')
                got = _checked(str((out or {}).get("text", "")), known, source, shingles)
            elif got[1].startswith("copies the book"):
                run = got[1].split(": ", 1)[1]
                out = ask(prompt + f'\n\nYour first answer copied the book\'s wording ("{run}"): "{out.get("text", "")}"\n'
                                     "Rewrite it in your own words, quoting at most 5 consecutive words of the passages.")
                got = _checked(str((out or {}).get("text", "")), known, source, shingles)
            elif got[1].startswith("wording"):
                # Usually a lasting fact phrased as news ("now works with dragons"): one rewrite.
                word = got[1].split(": ", 1)[1]
                out = ask(prompt + f'\n\nYour first answer used "{word}": "{out.get("text", "")}"\n'
                                     "Rewrite it as a timeless description without that wording.")
                got = _checked(str((out or {}).get("text", "")), known, source, shingles)
            # One gate decides whether a text may replace the previous description, whatever rewrite produced it:
            # - it may not drop the death the previous one opened with (after the stake, Lucy's went from "Dead, after a long
            #   decline" back to "her health has lately failed"), when the companion records that death (a checked fact or a
            #   turning point that says this entity died): "a hunter of the Dead" in a first sentence froze Dior's
            #   description for forty chapters (Empire of the Vampire);
            # - it keeps the looks the previous description stated unless the passages show them changed.
            # One rewrite naming every fault, then the previous description stands.
            died = any(q <= mark for q in deaths.get(eid, ()))
            who, kind = name_of(e, names), KINDS.get(e["type"], e["type"])
            if prev and died and prev not in dead_cache:
                dead_cache[prev] = own_death(model, who, kind, prev)
            opened_dead = bool(prev and died and dead_cache.get(prev))
            cleared = [False]

            def faults(t: str) -> tuple[bool, list[int]]:
                standing_lost = opened_dead and not _opens_with(_first(prev), t)
                flagged = lost_looks(looks, t, changed.get(t, set())) if looks else []
                gone = confirm_lost(model, who, kind, looks, t, flagged)
                if flagged and not gone and not cleared[0]:
                    stats_one["kept: look in other words"] += 1
                    cleared[0] = True
                return standing_lost, gone

            if got[0]:
                standing_lost, gone = faults(got[0])
                if standing_lost or gone:
                    asks = []
                    if standing_lost:
                        asks.append(f'Your earlier description began: "{_first(prev)}" That still holds unless the passages '
                                    "show it changed; open with where things stand now.")
                    if gone:
                        asks.append(f"It no longer says how {who} looks: " + "; ".join(f'{n}. "{looks[n - 1]}"' for n in gone)
                                    + ". Keep each of these (their words are yours to reuse) unless the passages show it "
                                    'changed; then say how it is now and give its number in "changed".')
                    again = ask(prompt + f'\n\nYour first answer was: "{got[0]}"\n' + "\n".join(asks))
                    retry = _checked(str(again.get("text", "")), known, source, shingles)
                    left = faults(retry[0]) if retry[0] else (standing_lost, gone)
                    if retry[0] and not left[0] and not left[1]:
                        got = (retry[0], "rewritten: kept the change in standing" if standing_lost else "rewritten: kept a stated look")
                    elif left[0] or (standing_lost and not left[1]):
                        got = (None, "lost the change in standing")
                    else:
                        got = (None, "lost a stated look: " + "; ".join(looks[n - 1] for n in left[1] or gone))
            return eid, seen, last, got, stats_one

        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(one, due))
        for eid, seen, last, (t, why), more in results:
            stats += more
            if t is not None and why:
                stats[why] += 1
            if t is None:
                stats["dropped"] += 1
                stats[f"dropped: {why.split(':')[0]}"] += 1
                log(f"    dropped {ents[eid]['names'][0]['value']}: {why}")
                continue
            # Never before the previous portrait: it is the one this replaces.
            last = max(last, seq_of[written[eid][-1]["from"]]) if eid in written else last
            i = min(bisect_left(anchor_seqs, last), len(anchors) - 1)
            at = anchors[i]["id"]
            ev = sorted({a for a in (anchors[j]["id"] for j in (bisect_left(anchor_seqs, s) for s in seen[-8:]))
                         if seq_of[a] <= seq_of[at]} if seen else set(), key=seq_of.get)
            if eid in written and written[eid][-1]["from"] == at:
                written[eid].pop()   # the same reveal point: the rewrite stands in for the earlier one
            written.setdefault(eid, []).append({"from": at, "evidence": ev or [at], "text": t})
            last_text[eid] = t
            done[eid] = len(seen)
            since[eid] = step
            upto_mark[eid] = mark
            stats["portraits"] += 1
        log(f"  {d['work']['title']}: through block {mark}, {stats['portraits']} portraits")

    # Extraction profiles from before an entity's first portrait stay as its early description;
    # later ones are superseded.
    for eid, ps in written.items():
        e = ents[eid]
        first = seq_of[ps[0]["from"]]
        old = [f for f in e["facets"] if f.get("kind") == "profile"]
        keep = [f for f in old if seq_of.get(f["from"], hi) < first]
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
