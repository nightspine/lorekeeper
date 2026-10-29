"""Entity identity: when two entries are the same thing, and how to merge them.

The generator uses `extends` to recognize a fuller (or shorter) form of a known name as the same
entity ("Dior" → "Dior LaChance", "Greyhand" → "Frère Greyhand"). `candidates` finds pairs that
slipped through in finished indexes, and `merge` folds one entity into another across a set of
indexes (a whole series, since entity ids are series-scoped).

Merging is spoiler-safe in the cases `extends` accepts: every name and facet keeps its own `from`
anchor, so nothing of the merged-in entity shows before the point where the book first used it.
It is not safe for hidden identities ("the stranger" turns out to be Gabriel): merging those would
put the stranger's facts under Gabriel's name before the reveal. Those need a facet at the reveal
point instead, and `extends` never proposes them.
"""

from __future__ import annotations

import re
import unicodedata

from . import text

# Forms of address and titles that don't distinguish one person from another.
HONORIFICS = {"mr", "mrs", "miss", "ms", "dr", "doctor", "sir", "lady", "lord", "captain", "major",
              "colonel", "sergeant", "inspector", "professor", "young", "old", "the",
              "brother", "sister", "father", "mother", "frere", "pere", "abbot", "abbess", "saint", "st",
              "prior", "king", "queen", "prince", "princess", "dame", "master", "mistress", "madame",
              "monsieur", "mademoiselle", "uncle", "aunt", "lieutenant", "general", "commander", "chevalier",
              "capitaine", "captaine", "marquis", "marquise", "viscount", "viscontessa", "baron", "baroness",
              "duke", "duchess", "count", "countess", "emperor", "empress", "prioress", "seraph", "forgemaster"}


POSSESSIVE = re.compile(r"\w['’]s\b")

# Titles that say who someone is not: "Mrs. Dursley" is never "Mr. Dursley", nor "Lady Arlynn" a lord.
MASCULINE = {"mr", "sir", "lord", "king", "prince", "master", "monsieur", "uncle", "brother", "father", "frere",
             "pere", "duke", "count", "baron", "emperor", "marquis", "viscount", "abbot", "prior", "chevalier", "laird",
             "senor", "signor", "herr", "khal"}
FEMININE = {"mrs", "miss", "ms", "lady", "dame", "queen", "princess", "mistress", "madame", "mademoiselle", "mlle",
            "aunt", "sister", "mother", "duchess", "countess", "baroness", "empress", "marquise", "viscontessa",
            "abbess", "prioress", "soeur", "senora", "signora", "frau"}
_STOP = {"the", "of", "a", "an", "and"}


def _titles(name: str) -> list[str]:
    """The leading titles of a name, normalized: "Mrs. Dursley" → ["mrs"]."""
    w = text.words(re.sub(r"\([^)]*\)", " ", name))
    out = []
    while len(w) > 1 and _plain(w[0]) in HONORIFICS:
        out.append(_plain(w[0]))
        w = w[1:]
    return out


def compatible(a: str, b: str) -> bool:
    """False when the two names' titles say different people (a man's title and a woman's)."""
    ga = {("m" if t in MASCULINE else "f") for t in _titles(a) if t in MASCULINE | FEMININE}
    gb = {("m" if t in MASCULINE else "f") for t in _titles(b) if t in MASCULINE | FEMININE}
    return not (ga and gb and ga != gb)


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def same_words(x: tuple[str, ...], y: tuple[str, ...]) -> bool:
    """The same name in another form: singular or plural, words reordered around "of"
    ("Muggles" / "Muggle", "Mirror of Erised" / "Erised mirror")."""
    a, b = {_stem(w) for w in x if w not in _STOP}, {_stem(w) for w in y if w not in _STOP}
    return bool(a) and a == b and any(len(w) > 3 for w in a)


def titled_surname(a: str, b: str) -> bool:
    """A title and surname naming the person whose full name ends with it ("Professor Snape" /
    "Severus Snape", "Mr. Filch" / "Argus Filch"). Family surnames are filtered out by callers."""
    for short, full in ((a, b), (b, a)):
        cs, cf = core(short), core(full)
        if len(cs) == 1 and _titles(short) and len(cf) > 1 and cf[-1] == cs[0] and len(cs[0]) > 2:
            return True
    return False


def _plain(word: str) -> str:
    """Without accents, for comparing against HONORIFICS ("frère" → "frere")."""
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


def core(name: str) -> tuple[str, ...]:
    """A name's distinguishing words: normalized, without leading honorifics or a parenthetical note."""
    w = text.words(re.sub(r"\([^)]*\)", " ", name))
    while len(w) > 1 and _plain(w[0]) in HONORIFICS:
        w = w[1:]
    return tuple(w)


# Words that join a surname without being one ("Gabriel de León", "Abraham Van Helsing").
PARTICLES = {"de", "del", "della", "di", "da", "van", "von", "der", "den", "la", "le", "du", "des", "of", "the",
             "al", "el", "bin", "ibn", "ben", "st", "á", "y", "e", "ter", "ten", "zu", "af"}


def _middle_fits(x: tuple[str, ...], y: tuple[str, ...]) -> bool:
    """Middle names of two forms of one name agree: none on one side, or the same ones, each possibly
    given only as an initial ("P." / "Peter")."""
    if not x or not y:
        return True
    return len(x) == len(y) and all(a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))
                                    for a, b in zip(x, y))


def same_person_name(a: str, b: str) -> bool:
    """Two forms of one person's name that differ only in middle names or initials: "Quincey Morris" /
    "Quincey P. Morris" / "Quincey Peter Morris". The first and last words must be equal; whatever
    sits between them may be missing on either side, or be an initial of the other's."""
    if not compatible(a, b) or POSSESSIVE.search(a) or POSSESSIVE.search(b):
        return False
    x, y = core(a), core(b)
    if len(x) < 2 or len(y) < 2 or x[0] != y[0] or x[-1] != y[-1] or len(x[0]) < 2 or len(x[-1]) < 2:
        return False
    return x != y and _middle_fits(x[1:-1], y[1:-1])


def _tail_of(short: tuple[str, ...], long_: tuple[str, ...]) -> bool:
    """The shorter name is how the longer one ends ("Van Helsing" / "Abraham Van Helsing"), with at
    least one real word in it: particles alone ("de", "von") say nothing."""
    return (len(short) >= 2 and len(short) < len(long_) and long_[-len(short):] == short
            and any(len(w) > 2 and _plain(w) not in PARTICLES for w in short))


def extends(a: str, b: str, person: bool = True, loose: bool = False) -> bool:
    """True when two names name the same thing: the same words without titles or, for people, the
    shorter is how the longer begins ("Dior" / "Dior LaChance", "Gabriel" / "Gabriel de León") or ends
    ("Van Helsing" / "Abraham Van Helsing"), or they differ only in middle names and initials
    ("Quincey Morris" / "Quincey P. Morris").

    Places and things need the same words: "Coste" is a town, "Coste Cathedral" a building in it.
    Possessive descriptions ("Gabriel's daughter") are never a fuller form of a name.
    """
    x, y = core(a), core(b)
    if not x or not y or not compatible(a, b):
        return False
    if x == y or same_words(x, y):
        return True
    if not person or POSSESSIVE.search(a) or POSSESSIVE.search(b):
        return False
    if titled_surname(a, b):
        return True
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    if len(short) < len(long_) and long_[:len(short)] == short and len(short[0]) > 2:
        return True
    if _tail_of(short, long_) or same_person_name(a, b):
        return True
    # Suggestions only (never automatic): the shorter's words all appear in the longer, in any
    # position ("Kiara" / "Wolfmother Kiara"). Also true of "Voss" / "Laure Voss", so a person decides.
    return loose and len(short) < len(long_) and set(short) <= set(long_) and all(len(w) > 2 for w in short)


def reason(names_a: list[str], names_b: list[str]) -> str:
    """Why two entities look alike, for people deciding: "same name", "shared alias" or "fuller name"."""
    if core(names_a[0]) == core(names_b[0]) or same_words(core(names_a[0]), core(names_b[0])):
        return "same name"
    if any(core(a) == core(b) for a in names_a for b in names_b):
        return "shared alias"
    return "fuller name"


def same_names(names_a: list[str], names_b: list[str], person: bool = True, loose: bool = False) -> bool:
    return any(extends(a, b, person, loose) for a in names_a for b in names_b)


def _entities(indexes: list[dict]) -> dict[str, dict]:
    """Every entity across the indexes: id → {type, names, facts, first: (book rank, ordinal)}."""
    out: dict[str, dict] = {}
    for rank, ix in enumerate(indexes):
        ords = {a["id"]: i for i, a in enumerate(ix.get("anchors", []))}
        for e in ix.get("entities", []):
            ent = out.setdefault(e["id"], {"type": e["type"], "names": [], "facts": [], "profile": None,
                                           "first": (rank, ords.get(e["from"], 0))})
            ent["facts"] += [f["text"] for f in e.get("facets", []) if f["text"] not in ent["facts"] and f.get("kind") != "profile"]
            profiles = [f for f in e.get("facets", []) if f.get("kind") == "profile"]
            if profiles:
                ent["profile"] = max(profiles, key=lambda f: ords.get(f["from"], 0))["text"]
            for n in e.get("names", []):
                if n["value"] not in ent["names"]:
                    ent["names"].append(n["value"])
    return out


def candidates(indexes: list[dict], distinct: set[frozenset] = frozenset()) -> list[dict]:
    """Pairs of entities that look like the same thing, as {keep, drop, sure, keepNames, dropNames}.

    Matching entities are grouped. When every member matches every other the pairs are `sure`; when
    not (a first name shared by two people, an alias that is also a wolf's name) each matching pair is
    offered unsure. The earlier-introduced entity is kept.
    """
    ents = _entities(indexes)
    counts = word_counts(indexes)
    ids = sorted(ents, key=lambda i: ents[i]["first"])
    # A family name shared by several people ("Chastain": Margot, Nicolette, Jean-François) says
    # nothing about which of them a bare "Chastain" is, so it is no evidence of identity.
    surnames = family_names(indexes)
    # Likewise a bare title: "Capitaine" alone could be any captain.
    bare = lambda n: len(core(n)) == 1 and (core(n)[0] in surnames or _plain(core(n)[0]) in HONORIFICS)  # noqa: E731
    names = {i: [n for n in ents[i]["names"] if not (ents[i]["type"] == "person" and bare(n))] for i in ids}
    match = {i: set() for i in ids}
    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            if ents[a]["type"] == ents[b]["type"] and frozenset((a, b)) not in distinct \
                    and same_names(names[a], names[b], ents[a]["type"] == "person", loose=True):
                match[a].add(b)
                match[b].add(a)
    out, done = [], set()
    for a in ids:
        if a in done or not match[a]:
            continue
        group, stack = {a}, [a]
        while stack:
            for o in match[stack.pop()] - group:
                group.add(o)
                stack.append(o)
        done |= group
        members = sorted(group, key=lambda i: ents[i]["first"])
        if all(match[x] | {x} == group for x in group):
            # Everyone matches everyone: fold the group into its earliest member.
            for d in members[1:]:
                out.append({"keep": members[0], "drop": d, "sure": True,
                            "reason": reason(ents[members[0]]["names"], ents[d]["names"]),
                            "keepNames": ents[members[0]]["names"], "dropNames": ents[d]["names"]})
        else:
            # Some members don't match each other, so some name is shared by different things.
            # Offer each matching pair, unticked, for a person to decide.
            for i, x in enumerate(members):
                for y in members[i + 1:]:
                    if y in match[x]:
                        out.append({"keep": x, "drop": y, "sure": False,
                                    "reason": reason(ents[x]["names"], ents[y]["names"]),
                                    "keepNames": ents[x]["names"], "dropNames": ents[y]["names"]})
    for p in out:
        p["ranked"] = rank_names([*ents[p["keep"]]["names"], *ents[p["drop"]]["names"]], counts, surnames)
    return out


def _fold_names(keep: dict, drop: dict, ords: dict[str, int]) -> None:
    """Add drop's names to keep as aliases (canonicalize picks the display name afterwards)."""
    have = {core(n["value"]): n for n in keep.get("names", [])}
    for n in drop.get("names", []):
        same = have.get(core(n["value"]))
        if same is not None:
            # Both entries had this name: it has been known since the earlier of the two.
            if ords.get(n["from"], 0) < ords.get(same["from"], 0):
                same["from"] = n["from"]
            continue
        n = {**n, "kind": "alias" if n.get("kind") == "primary" else n.get("kind", "alias")}
        keep.setdefault("names", []).append(n)
        have[core(n["value"])] = n


# ---- display names -----------------------------------------------------------------------------

def word_counts(indexes: list[dict]) -> dict[str, int]:
    """How often each word occurs in what the indexes say (facts, events, recaps)."""
    counts: dict[str, int] = {}
    for ix in indexes:
        texts = [f["text"] for e in ix.get("entities", []) for f in e.get("facets", [])]
        texts += [ev.get("summary", "") for ev in ix.get("events", [])] + [r.get("text", "") for r in ix.get("recaps", [])]
        for t in texts:
            for w in text.words(t):
                counts[w] = counts.get(w, 0) + 1
    return counts


def family_names(indexes: list[dict]) -> set[str]:
    """Surnames: the last word of full names that two or more different people with different first
    names share ("voss" for Fabién, Danton and Laure Voss; not "dior" for "Dior" and a stray
    "San Dior" entry). A name ending in one is someone's real name, not a title."""
    seen: dict[str, set[str]] = {}
    firsts: dict[str, set[str]] = {}
    for ix in indexes:
        for e in ix.get("entities", []):
            if e["type"] != "person":
                continue
            for n in e.get("names", []):
                c = core(n["value"])
                if len(c) > 1 and clean(n["value"]):
                    seen.setdefault(c[-1], set()).add(e["id"])
                    firsts.setdefault(c[-1], set()).add(c[0])
    return {w for w, ids in seen.items() if len(ids) > 1 and len(firsts[w]) > 1}


def clean(name: str) -> bool:
    """A name a reader would recognize as a name: no bracketed note and not a possessive description."""
    return "(" not in name and not POSSESSIVE.search(name)


def rank_names(names: list[str], counts: dict[str, int], surnames: set[str] = frozenset()) -> list[str]:
    """Best display name first: the name a reader knows someone by, their actual name over any title.

    Clean names first; then plain names over epithets and titles: "Kiara the Wolfmother", "The Black
    Lion of Lorson", "Empress", and titles that put the title word last ("Forever King"). Then real
    full names, recognised by a family surname others share ("Fabién Voss"). Then the entity's key
    word, the one most of its names share (ties to the word the book mentions most): "Kiara" for
    Kiara Dyvok / Kiara / Wolfmother Kiara; key word first, the fullest, and no title over a title
    ("Jean-François Chastain" over "Marquis Jean-François Chastain")."""
    cores = {n: core(n) for n in dict.fromkeys(names)}
    shared: dict[str, int] = {}
    for c in cores.values():
        for w in set(c):
            shared[w] = shared.get(w, 0) + 1
    key = max(shared, key=lambda w: (shared[w], counts.get(w, 0), len(w))) if shared else ""

    def score(n: str):
        c, words = cores[n], text.words(n)
        joined = f" {' '.join(words)} "
        epithet = (bool(words) and words[0] == "the") or " the " in joined or " of " in joined \
            or "," in n or "/" in n or any(_plain(w) in HONORIFICS for w in c)
        personal = len(c) > 1 and c[-1] in surnames
        titled = len(words) > len(c)
        return (clean(n), not epithet, personal, key in c, not titled, bool(c) and c[0] == key, len(c), -len(n))
    return sorted(cores, key=score, reverse=True)


def canonicalize(indexes: list[dict], eid: str, name: str | None = None, counts: dict[str, int] | None = None,
                 surnames: set[str] | None = None) -> str | None:
    """Make one name the entity's display name in every index: `name`, or the best-ranked one.

    Only that name keeps kind "primary"; other primaries become aliases. Gating shows the latest
    visible primary, else the earliest visible name, so before the chosen name is revealed readers
    see the earliest name instead: never one from later in the book.
    """
    names = [n["value"] for ix in indexes for e in ix.get("entities", []) if e["id"] == eid for n in e.get("names", [])]
    if not names:
        return None
    if name is None or name.casefold() not in {n.casefold() for n in names}:
        name = rank_names(names, counts if counts is not None else word_counts(indexes),
                          surnames if surnames is not None else family_names(indexes))[0]
    for ix in indexes:
        for e in ix.get("entities", []):
            if e["id"] != eid:
                continue
            for n in e.get("names", []):
                if n["value"].casefold() == name.casefold():
                    n["kind"] = "primary"
                elif n.get("kind") == "primary":
                    n["kind"] = "alias"
    return name


def merge(indexes: list[dict], keep: str, drop: str, name: str | None = None,
          counts: dict[str, int] | None = None) -> list[int]:
    """Fold entity `drop` into `keep` in every index that mentions it, then give the result one
    display name (`name`, or the best-ranked). Returns the positions of the changed indexes."""
    changed = []
    for i, ix in enumerate(indexes):
        touched = False
        ords = {a["id"]: n for n, a in enumerate(ix.get("anchors", []))}
        ents = ix.get("entities", [])
        d = next((e for e in ents if e["id"] == drop), None)
        k = next((e for e in ents if e["id"] == keep), None)
        if d is not None:
            touched = True
            if k is None:
                d["id"] = keep
            else:
                _fold_names(k, d, ords)
                # The merged entity appears with its earliest name (SPEC: entity.from = earliest name).
                firsts = [n["from"] for n in k["names"]] or [k["from"], d["from"]]
                k["from"] = min(firsts, key=lambda a: ords.get(a, 0))
                k["facets"] = sorted([*k.get("facets", []), *d.get("facets", [])], key=lambda f: ords.get(f["from"], 0))
                ents.remove(d)
        for c in ix.get("carried", []):
            if c["id"] == drop:
                touched = True
                other = next((x for x in ix["carried"] if x["id"] == keep), None)
                if other is None:
                    c["id"] = keep
                else:
                    other["names"] = list(dict.fromkeys([*other["names"], *c["names"]]))
                    other["facets"] = [*other["facets"], *c["facets"]]
                    ix["carried"].remove(c)
                break
        rels, seen, gone = [], set(), set()
        for r in ix.get("relations", []):
            if drop in (r["src"], r["dst"]):
                touched = True
                r["src"], r["dst"] = (keep if r["src"] == drop else r["src"]), (keep if r["dst"] == drop else r["dst"])
            sig = (r["src"], r["dst"], r["label"].casefold())
            if r["src"] == r["dst"] or sig in seen:   # "Dior knows Dior LaChance" says nothing once they're one
                gone.add(r["id"])
                touched = True
                continue
            seen.add(sig)
            rels.append(r)
        if gone:
            for r in rels:
                if "replaces" in r:
                    r["replaces"] = [x for x in r["replaces"] if x not in gone]
                    if not r["replaces"]:
                        del r["replaces"]
        if "relations" in ix:
            ix["relations"] = rels
        for ev in ix.get("events", []):
            if drop in ev["entities"]:
                touched = True
                ev["entities"] = list(dict.fromkeys(keep if x == drop else x for x in ev["entities"]))
        if touched:
            changed.append(i)
    if changed:
        canonicalize(indexes, keep, name, counts)
    return changed


# ---- judging suggestions with a model ---------------------------------------------------------

NAME_PARTICLES = {"de", "del", "della", "di", "da", "des", "du", "la", "le", "van", "von", "der", "den", "bin", "ibn",
                  "al", "el", "of", "y", "ap", "af", "dos", "do", "á", "à", "ó", "ua", "mac"}


def proper_name(name: str) -> bool:
    """True for a name that picks out one individual: after the leading titles every word is capitalised
    (name particles like "de" and "of" excepted), and there is no "the", possessive, bracketed note or
    title word left. "Aaron", "Van Helsing", "Sister Chloe" and "Emperor Alexandre III" are proper names;
    "the Professor", "Gabriel's grandfather", "Forever King" and a bare "Emperor" are not."""
    if "(" in name or POSSESSIVE.search(name):
        return False
    w = [x.strip(".,;:") for x in name.split()]
    w = [x for x in w if x]
    if any(x.lower() == "the" for x in w):
        return False
    while len(w) > 1 and _plain(w[0].lower()) in HONORIFICS:
        w = w[1:]
    if not w or _plain(w[0].lower()) in HONORIFICS:
        return False
    if any(_plain(x.lower()) in HONORIFICS for x in w):
        return False   # a title inside the name: "Forever King"
    if not w[0][0].isupper():
        return False
    return all(x[0].isupper() or x.lower() in NAME_PARTICLES or not x[0].isalpha() for x in w)


JUDGE_SYSTEM = """You help tidy a reader's companion for a novel series. Judge only from the entries and book passages you are given."""
JUDGE_VERSION = 5   # bump when the prompt changes: cached verdicts from an older prompt are re-checked
JUDGE = """Each pair below shows two entries from a companion to a novel series, with similar names. Entries were
written from different passages, so their wording differs; judge whether they describe ONE individual (or one place
or thing), as a careful reader who knew the book would. Each pair also shows what the companion says about each
entry (profile, details, ties to others) and, where available, the book's own text where the second entry's name
first occurs.
- same: both entries fit one individual: the same ties (betrothed, parent, order, master), role, distinctive traits
  or history, and nothing conflicts. A fuller name, a title, a first name or a nickname of the same person is the same
  person, even if one entry says much less than the other.
- different: a concrete conflict. One is the other's relative, rival or servant; they act in one scene as two people;
  their roles, sexes, deaths or places are incompatible; or the shared word is a family name or title that several
  people carry. A place and a building in it, or a person and an object named after them, are different too.
- unsure: neither entry says anything distinctive, or the only evidence is the name.
For each pair give the verdict and, in "because", one short sentence naming the detail that decided it. Set
"hidden" to true whenever the book does not make the link plain where the second entry first appears: one is a
disguise, a cover name or an assumed identity ("posed as", "calls herself", "was really"), a true name or title
kept back until a later reveal, or the book first presents them as two people and shows only later that they are
one. Such a pair may be the same individual, but merging it would give away the reveal early, so say true.
Say false only when a reader would already know at that point that the two names are one person.

{pairs}"""
JUDGE_SCHEMA = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "because", "hidden"],
    "properties": {"id": {"type": "string"}, "verdict": {"enum": ["same", "different", "unsure"]},
                   "because": {"type": "string"}, "hidden": {"type": "boolean"}}}}}}


PROPOSE = """Below are the people and creatures in a reader's companion for a novel, each with what the companion says
about them. Some may be the same individual listed twice under different names: a first name and a title
("Mr. Dursley" and "Uncle Vernon"), a nickname, a role ("the keeper") and a name. List the pairs you are
confident are one individual, judging by what the entries say (same family ties, role, look, history).
Never pair relatives, twins, people who merely share a title or role, or two members of one family.

{entries}"""
PROPOSE_SCHEMA = {"type": "object", "required": ["pairs"], "properties": {"pairs": {"type": "array", "items": {
    "type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}}}}


def propose(indexes: list[dict], model, known: set[frozenset] = frozenset(), limit: int = 400) -> list[dict]:
    """Pairs a model thinks are one individual under different names, which name rules can't see.
    They are judged afterwards like any other pair (`judge`); only pairs whose names are all proper
    names are merged without asking, since a pairing like this may be a hidden identity.
    Returns [{keep, drop, reason: "described alike"}], the earlier-introduced entity kept."""
    ents = _entities(indexes)
    ids = [i for i in sorted(ents, key=lambda i: ents[i]["first"])
           if ents[i]["type"] in ("person", "creature") and (ents[i]["facts"] or ents[i].get("profile"))][:limit]
    if len(ids) < 2:
        return []
    lines = []
    for n, i in enumerate(ids):
        e = ents[i]
        about = e.get("profile") or " / ".join(e["facts"][:3])
        lines.append(f"E{n + 1}: {', '.join(e['names'][:4])}: {about[:220]}")
    raw = model.complete(JUDGE_SYSTEM, PROPOSE.format(entries="\n".join(lines)), PROPOSE_SCHEMA)
    out, seen = [], set()
    for p in raw.get("pairs", []) if isinstance(raw, dict) else []:
        try:
            a, b = (ids[int(str(p.get(k, "")).lstrip("Ee")) - 1] for k in ("a", "b"))
        except (ValueError, IndexError):
            continue
        pair = frozenset((a, b))
        if a == b or pair in known or pair in seen or ents[a]["type"] != ents[b]["type"]:
            continue
        seen.add(pair)
        keep, drop = sorted((a, b), key=lambda i: ents[i]["first"])
        out.append({"keep": keep, "drop": drop, "sure": False, "reason": "described alike",
                    "keepNames": ents[keep]["names"], "dropNames": ents[drop]["names"]})
    return out


def _name_pattern(name: str):
    n = re.sub(r"\([^)]*\)", " ", name).strip()
    if len(n) < 3:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(x) for x in n.split()) + r"(?!\w)", re.I)


class _Evidence:
    """What a judge is shown about an entity: the whole entry, its ties, and the book's own text where
    its name first occurs. Built once per `judge` call; parsed books are loaded lazily and kept."""

    def __init__(self, indexes: list[dict], books=None) -> None:
        self.indexes, self.books = indexes, books
        self.ents = _entities(indexes)
        self.where: dict[str, tuple[int, str]] = {}   # entity → (index rank, anchor id it is introduced at)
        replaced = {r for ix in indexes for rel in ix.get("relations", []) for r in rel.get("replaces", [])}
        self.ties: dict[str, list[str]] = {}
        for rank, ix in enumerate(indexes):
            for e in ix.get("entities", []):
                self.where.setdefault(e["id"], (rank, e["from"]))
            for rel in ix.get("relations", []):
                if rel["id"] in replaced:
                    continue
                s, d = (self.ents[rel[k]]["names"][0] if rel[k] in self.ents and self.ents[rel[k]]["names"] else rel[k]
                        for k in ("src", "dst"))
                sentence = f"{s}: {rel['label']} {d}"
                for who in (rel["src"], rel["dst"]):
                    if sentence not in self.ties.setdefault(who, []):
                        self.ties[who].append(sentence)
        self._texts: dict[int, tuple[list[str], list[str]] | None] = {}
        self._patterns: dict[str, list] = {}

    def _book(self, rank: int):
        if rank not in self._texts:
            book = self.books(self.indexes[rank]) if self.books else None
            blocks = [b.raw for b in book.blocks] if book is not None else None
            self._texts[rank] = (blocks, [b.casefold() for b in blocks]) if blocks else None
        return self._texts[rank]

    def _pats(self, eid: str) -> list[tuple[str, object]]:
        if eid not in self._patterns:
            self._patterns[eid] = [(n, p) for n in self.ents[eid]["names"] if (p := _name_pattern(n)) is not None]
        return self._patterns[eid]

    def has_text(self, eid: str) -> bool:
        return self._book(self.where[eid][0]) is not None if eid in self.where else False

    def introduction(self, eid: str) -> tuple[int, list[str]] | None:
        """The first paragraph of the entity's own book that uses one of its names, with a paragraph either side."""
        text = self._book(self.where[eid][0]) if eid in self.where else None
        if text is None:
            return None
        blocks, lower = text
        pats = self._pats(eid)
        for n, low in enumerate(lower):
            if any(name.casefold() in low and p.search(blocks[n]) for name, p in pats):
                return n, blocks[max(0, n - 1):n + 2]
        return None

    def together(self, a: str, b: str) -> tuple[int, str] | None:
        """A paragraph in which a name of `a` and a name of `b` both occur as separate mentions: the
        longest names are claimed first, so "Aaron" inside "Aaron de Coste" is not a second mention."""
        text = self._book(self.where[b][0]) if b in self.where else None
        if text is None:
            return None
        blocks, lower = text
        pa, pb = self._pats(a), self._pats(b)
        shared = {core(n) for n, _ in pa} & {core(n) for n, _ in pb}
        allp = sorted([(n, p, "a") for n, p in pa] + [(n, p, "b") for n, p in pb], key=lambda t: -len(t[0]))
        for n, low in enumerate(lower):
            if not (any(x.casefold() in low for x, _ in pa) and any(x.casefold() in low for x, _ in pb)):
                continue
            taken: list[tuple[int, int]] = []
            owners = set()
            for name, p, side in allp:
                for m in p.finditer(blocks[n]):
                    if any(m.start() < e and s < m.end() for s, e in taken):
                        continue
                    taken.append((m.start(), m.end()))
                    if core(name) not in shared:
                        owners.add(side)
            if owners == {"a", "b"}:
                return n, blocks[n]
        return None


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def _describe(ent: dict, ev: _Evidence | None = None, eid: str | None = None) -> str:
    facts = ent["facts"] if len(ent["facts"]) <= 8 else ent["facts"][:4] + ent["facts"][-4:]
    out = f"{ent['type']}; names: {', '.join(ent['names'][:8])}"
    if ent.get("profile"):
        out += f"\n     profile: {ent['profile']}"
    out += "\n     details: " + (" / ".join(facts) or "nothing beyond the name")
    if ev is not None and eid is not None:
        if ev.ties.get(eid):
            out += "\n     ties: " + "; ".join(ev.ties[eid][-8:])
        if eid in ev.where:
            rank, anchor = ev.where[eid]
            title = (ev.indexes[rank].get("work") or {}).get("title", "")
            out += f"\n     introduced: {title + ', ' if title else ''}at {anchor}"
    return out


def judge(indexes: list[dict], pairs: list[dict], model, batch: int = 8, books=None, meta: dict | None = None) -> dict[str, str]:
    """Ask a model about each pair. Returns {"keep|drop": "same" | "different" | "unsure"}.

    The model sees everything the companion says about both entries (the whole book's worth), their ties
    to others, and, when `books` (a function from an index document to its parsed EPUB, or None) can
    supply it, the paragraph where the later entry's name first occurs, plus any paragraph where both
    names occur. `meta`, when given, is filled with {"keep|drop": {"because": str, "text": bool, "hidden": bool}}
    where `text` says the book's own text was part of the evidence and `hidden` that the book conceals the
    link at first (a disguise or secret identity), which keeps a pair out of automatic merging. The model's reasoning is for the record
    only: it may state whole-book facts, so it must never be shown to a reader who is partway through.
    """
    ev = _Evidence(indexes, books)
    out: dict[str, str] = {}
    for i in range(0, len(pairs), batch):
        chunk = pairs[i:i + batch]
        lines, texted = [], []
        for n, p in enumerate(chunk):
            a, b = p["keep"], p["drop"]
            lines.append(f"P{n + 1}:\n  A: {_describe(ev.ents[a], ev, a)}\n  B: {_describe(ev.ents[b], ev, b)}")
            intro = ev.introduction(b)
            if intro:
                lines.append("  Where B's name first occurs in the book:\n    " + "\n    ".join(_clip(t, 700) for t in intro[1]))
            both = ev.together(a, b)
            if both:
                lines.append(f"  Both names occur in paragraph {both[0] + 1} (that alone does not mean two people: an "
                             f"author may use a full name and a short form together): {_clip(both[1], 900)}")
            texted.append(bool(intro or both or ev.has_text(b)))
        raw = model.complete(JUDGE_SYSTEM, JUDGE.format(pairs="\n".join(lines)), JUDGE_SCHEMA)
        for v in raw.get("verdicts", []) if isinstance(raw, dict) else []:
            try:
                k = int(str(v.get("id", "")).lstrip("Pp")) - 1
                if k < 0:
                    continue
                p = chunk[k]
            except (ValueError, IndexError):
                continue
            if v.get("verdict") in ("same", "different", "unsure"):
                key = f"{p['keep']}|{p['drop']}"
                out[key] = v["verdict"]
                if meta is not None:
                    meta[key] = {"because": str(v.get("because") or "")[:300], "text": texted[k], "hidden": v.get("hidden") is True}
    return out
