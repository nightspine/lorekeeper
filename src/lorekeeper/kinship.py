"""Kinship read from relation labels ("daughter of", "half-sister of", "married to").

The tables here are the same ones as `web/static/js/kinship.js`; the two must give the same result for the
same label. Uncertain labels ("possible father of", "killed mother of", "brother-in-arms of") are not kinship:
`parse` returns None and a family tree does not draw them.
"""
from __future__ import annotations

import re

# role → the role seen from the other end of the tie (`role_key` orients ties with it)
ROLES = {"parent": "child", "child": "parent", "spouse": "spouse", "sibling": "sibling",
         "grandparent": "grandchild", "grandchild": "grandparent", "pibling": "nibling", "nibling": "pibling",
         "cousin": "cousin", "guardian": "ward", "ward": "guardian", "related": "related"}

# Every spelling that is kinship → (role, canonical word)
KIN = {}
for _word, _role in (
        ("mother", "parent"), ("father", "parent"), ("parent", "parent"), ("mum", "parent"), ("mom", "parent"),
        ("dad", "parent"), ("papa", "parent"), ("mama", "parent"),
        ("son", "child"), ("daughter", "child"), ("child", "child"),
        ("husband", "spouse"), ("wife", "spouse"), ("spouse", "spouse"), ("consort", "spouse"),
        ("widow", "spouse"), ("widower", "spouse"),
        ("brother", "sibling"), ("sister", "sibling"), ("sibling", "sibling"), ("twin", "sibling"),
        ("grandmother", "grandparent"), ("grandfather", "grandparent"), ("grandparent", "grandparent"),
        ("grandma", "grandparent"), ("grandpa", "grandparent"), ("granny", "grandparent"),
        ("grandson", "grandchild"), ("granddaughter", "grandchild"), ("grandchild", "grandchild"),
        ("aunt", "pibling"), ("uncle", "pibling"), ("niece", "nibling"), ("nephew", "nibling"),
        ("cousin", "cousin"),
        ("guardian", "guardian"), ("godfather", "guardian"), ("godmother", "guardian"),
        ("ward", "ward"), ("godson", "ward"), ("goddaughter", "ward")):
    KIN[_word] = _role

# Spellings that are written one way and kept another.
CANONICAL = {"mum": "mother", "mom": "mother", "mama": "mother", "dad": "father", "papa": "father",
             "grandma": "grandmother", "grandpa": "grandfather", "granny": "grandmother"}

MODIFIERS = ("younger", "older", "elder", "eldest", "youngest", "twin", "half", "step", "adoptive", "adopted",
             "foster", "late", "ex", "former", "blood", "first", "second", "great", "identical")
LEADING = ("is", "the", "a", "an", "her", "his", "their")


def _expand(token: str):
    """A token as a list of words: a kin word or modifier as it stands, or modifiers joined to a kin word
    ("stepson", "greatgrandmother"); None when it is neither."""
    if token in KIN or token in MODIFIERS or token == "in-law":
        return [token]
    for m in sorted(MODIFIERS, key=len, reverse=True):
        if token.startswith(m) and len(token) > len(m):
            rest = _expand(token[len(m):])
            if rest:
                return [m, *rest]
    return None


def parse(label) -> dict | None:
    """{role, modifiers, word} when the label states kinship, else None.
    The relation's `src` has the role toward `dst` ("daughter of": src is the child)."""
    if not isinstance(label, str):
        return None
    s = label.casefold().replace("’", "'").strip()
    s = re.sub("[\u2010-\u2015]", "-", s)   # hyphen, en and em dashes all join like a hyphen
    s = re.sub(r"[-\s]+", " ", s)
    s = re.sub(r" in law\b", " in-law", s)
    if "," in s or ";" in s:
        return None
    tokens = s.split()
    while tokens and tokens[0] in LEADING:
        tokens.pop(0)
    if tokens == ["married", "to"]:
        return {"role": "spouse", "modifiers": [], "word": "married to"}
    if tokens and tokens[-1] == "of":
        tokens.pop()
    if not tokens:
        return None
    words: list[str] = []
    for t in tokens:
        parts = _expand(t)
        if parts is None:
            return None
        words += parts
    in_law = False
    if words and words[-1] == "in-law":
        in_law = True
        words.pop()
    if not words or words[-1] not in KIN:
        return None
    word, mods = words[-1], words[:-1]
    if any(m not in MODIFIERS for m in mods):   # a second kin word, or "in-law" in the middle
        return None
    if word in ("widow", "widower") and "late" not in mods:
        mods.insert(0, "late")   # a widow is the spouse of someone who has died
    if in_law:
        mods.append("in-law")
    return {"role": KIN[word], "modifiers": mods, "word": CANONICAL.get(word, word)}


SEPARATE = ("younger", "older", "elder", "eldest", "youngest", "twin", "identical", "adoptive", "adopted", "foster",
            "late", "former", "blood", "first", "second")   # kept as words, as given, before the kin word


def normalize(label) -> str | None:
    """The canonical form of a kinship label, or None when it is not one: compounds are written the way English
    does ("step mother of" → "stepmother of", "half sister of" → "half-sister of",
    "ex wife of" → "ex-wife of", "great grandmother of" → "great-grandmother of", "mother in law of" →
    "mother-in-law of"); "younger", "older", "elder", "eldest", "youngest", "twin", "identical", "adoptive",
    "adopted", "foster", "late", "former", "blood", "first" and "second" stay separate words, in the order the label gave them; "step" joins the kin word and "half", "ex" and "great" hyphenate in front of it ("step half brother of" → "half-stepbrother of"); "late" is dropped before widow and widower."""
    p = parse(label)
    if p is None:
        return None
    if p["word"] == "married to":
        return "married to"
    mods = [m for m in p["modifiers"] if m != "in-law"]
    if p["word"] in ("widow", "widower"):
        mods = [m for m in mods if m != "late"]   # implied by the word
    head = [m for m in mods if m in SEPARATE]   # in the order the label gave them
    word = p["word"] + ("-in-law" if "in-law" in p["modifiers"] else "")
    if "step" in mods:
        word = "step" + word   # step binds to the kin word first
    for m in reversed([m for m in mods if m in ("half", "ex", "great")]):   # then these, hyphenated, in label order
        word = f"{m}-{word}"
    return " ".join([*head, word, "of"])


def role_key(label, src: str, dst: str):
    """What kind of tie a kinship label states between two entities, independent of modifiers and of which end is
    which: (role as seen from the lower of the two ids, in-law). "wife of" a→b and "husband of" b→a give the same
    key; "stepmother of" and "mother of" in one direction too. None when the label is not kinship."""
    p = parse(label)
    if p is None:
        return None
    role = p["role"]
    if src > dst:
        role = ROLES.get(role, role)
    return role, "in-law" in p["modifiers"]


def is_spouse(label) -> bool:
    p = parse(label)
    return bool(p and p["role"] == "spouse")


def tag(doc: dict) -> dict:
    """Bring the kinship ties of one index to the canonical form, in place: a label that parses is written
    canonically and tagged `family`, whatever kind it had (a label is better evidence than a guess at a kind).
    Labels that are not kinship are left as they are. Returns counts: tagged, normalised, left (not kinship),
    same (already right)."""
    n = {"tagged": 0, "normalised": 0, "left": 0, "same": 0}
    for r in doc.get("relations", []):
        canon = normalize(r.get("label"))
        if canon is None:
            n["left"] += 1
            continue
        changed = False
        if canon != r["label"]:
            r["label"] = canon
            n["normalised"] += 1
            changed = True
        if r.get("kind") != "family":
            r["kind"] = "family"
            n["tagged"] += 1
            changed = True
        if not changed:
            n["same"] += 1
    return n
