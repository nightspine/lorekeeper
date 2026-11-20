"""Bring indexes generated before profiles (prompt sets before extract-v4) up to date.

`tidy` removes entities that are descriptions rather than names ("Gabriel's straight razor") or only
a kin or role word ("Mama", "Father"), and folds descriptions of a known entity ("Celene's brother
Gabriel", and "Gabriel's sister" when one named person is tied to Gabriel as his sister) into it, using
the same rules the generator now applies (`generate.triage_name`, `bare_kin_word`, `kin_ties`).

`backfill` writes profile facets (SPEC.md §4.2) from the facts an index already has. Between two
recaps, each entity that gained facts gets a profile written from only the facts revealed up to that
point, revealed at the latest of them, so the profile can never say more than the reader could
know there. Each profile replaces the previous one, across the books of a series too, which is how a
character keeps one description that grows. The facts themselves are not changed: indexes whose
facts read like plot beats need a fresh run for better facts.
"""

from __future__ import annotations

from collections import Counter

from . import merge
from .generate import (_POSSESSIVE, HINDSIGHT, MOMENTARY, SYSTEM, bare_kin_word, hidden_identity, is_description,
                       kin_description, kin_ties, sex_of, triage_name)


def generate_possessive(name: str) -> bool:
    return bool(_POSSESSIVE.match(name))

PROFILE = """For each entity below, write its profile: the description a reader would want on a character sheet, of
who or what it is as known from the facts given. Identity and role, look, nature, abilities, and the ties that
define it ("Harry's best friend", "the king's sister"). Only lasting changes in status count (orphaned, knighted,
exiled, dead); never where it is or what it is doing at the moment, and never recent events ("now on the train",
"has begun to trust X"). Timeless present tense: "An orphaned farm boy raised by his uncle who discovers he can
work magic; quick-tempered but loyal", not "Now on the road to the capital, he has begun to trust Mira".
1 to 3 sentences, under 70 words, in your own words. Use ONLY the previous profile and the facts given for that
entity: add nothing, and hint at nothing beyond them.

{entities}"""

PROFILE_SCHEMA = {"type": "object", "required": ["profiles"], "properties": {"profiles": {"type": "array", "items": {
    "type": "object", "required": ["id", "text"], "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}

MAX_FACTS = 14      # facts shown per entity: the most recent ones
MIN_FACTS = 2       # entities with fewer facts get no profile yet
NEW_FACTS = 2       # facts gained since the last profile before writing a new one


# ---- tidy ------------------------------------------------------------------------------------

def _finder(docs: list[dict]):
    """known(name, type, not_id) → the one entity whose primary name that is, or None.

    Primary names only: older indexes carry many wrong aliases, and folding by alias merged
    different people ("Connor" into "Prince", a wolf) and hidden identities. Anything looser is
    left to the duplicates review, where the reader confirms each pair."""
    ents: dict[str, tuple[str, list[str]]] = {}
    by_core: dict[tuple, set[str]] = {}
    for d in docs:
        for e in d.get("entities", []):
            t, ns = ents.setdefault(e["id"], (e["type"], []))
            ns.extend(n["value"] for n in e.get("names", []) if n["value"] not in ns)
            for n in e.get("names", []):
                if n.get("kind") == "primary":
                    by_core.setdefault(merge.core(n["value"]), set()).add(e["id"])

    def known(name: str, etype: str | None, not_id: str) -> str | None:
        hits = by_core.get(merge.core(name), set()) - {not_id}
        return next(iter(hits)) if len(hits) == 1 else None

    def forget(eid: str) -> None:
        """An entity folded into another or removed no longer competes for names."""
        ents.pop(eid, None)
        for ids in by_core.values():
            ids.discard(eid)
    return ents, known, forget


def tidy(docs: list[dict]) -> dict:
    """Remove description-entities and fold wrapped references into their entity, in every doc.
    Returns {"removed": [names], "merged": [(drop name, keep name)], "changed": set of doc positions}."""
    ents, known, forget = _finder(docs)
    removed, merged, changed = [], [], set()
    counts = merge.word_counts(docs)
    for eid, (etype, names) in list(ents.items()):
        if not names or eid not in ents:
            continue
        if etype == "person" and all(bare_kin_word(n) for n in names):
            changed |= set(remove(docs, eid))   # "Mama", "Father", "the boy": never a person of their own
            removed.append(names[0])
            forget(eid)
            continue
        verdicts = [triage_name(n, etype, lambda x: known(x, etype, eid)) for n in names]
        if all(v[0] == "drop" for v in verdicts):
            changed |= set(remove(docs, eid))
            removed.append(names[0])
            forget(eid)
            continue
        # Only a description wrapped around a name ("Celene's brother Gabriel", "Gabriel's sister
        # Celene"): the text itself says who it is.
        kind, value = verdicts[0]
        wrapped = "(" in names[0] or generate_possessive(names[0])
        target = (value if kind == "known" else known(value, etype, eid) if value != names[0] else None) if wrapped else None
        if target and target in ents and target != eid:
            changed |= set(merge.merge(docs, target, eid, None, counts))
            merged.append((names[0], ents[target][1][0]))
            ents[target][1].extend(n for n in names if n not in ents[target][1])
            forget(eid)
    kin_folded = _fold_kin(docs, ents, known, forget, counts)
    for _, _, i in kin_folded:
        changed |= set(i)
    return {"removed": removed, "merged": merged, "kin_folded": [(a, b) for a, b, _ in kin_folded], "changed": changed}


def _fold_kin(docs: list[dict], ents: dict, known, forget, counts) -> list[tuple[str, str, list[int]]]:
    """Fold the one named person the relations tie to X as his <kin> into the person known only as "X's <kin>"
    (the description keeps its id, so no id carries a name the reader has not reached), when
    - exactly one such description exists for X and exactly one named person fits (`kin_ties`, modifiers agreeing);
    - the named person does not first appear in an earlier book than the description (those files are not rewritten
      by a fold into a later one's entry);
    - the tie that identifies them comes at or before the named person's first appearance (anchor order within a
      book, series order across books), so no position shows both names without the tie;
    - nothing ties the two together and the text keeps no identity back (`hidden_identity`).
    Returns (description, named, changed doc positions)."""
    rels = [r for d in docs for r in d.get("relations", [])]
    events = [ev for d in docs for ev in d.get("events", [])]
    facets = {e["id"]: [f["text"] for f in e.get("facets", [])] for d in docs for e in d.get("entities", [])}
    ords = [{a["id"]: n for n, a in enumerate(d.get("anchors", []))} for d in docs]
    first: dict[str, tuple[int, int]] = {}
    for i, d in enumerate(docs):
        for e in d.get("entities", []):
            first.setdefault(e["id"], (i, ords[i].get(e.get("from"), 0)))
    tie_at = {r["id"]: (i, ords[i].get(r.get("from"), 0)) for i, d in enumerate(docs) for r in d.get("relations", [])}
    named = lambda x: x in ents and any(not is_description(n) and not bare_kin_word(n) for n in ents[x][1])  # noqa: E731
    sex = lambda x: sex_of(ents[x][1] if x in ents else [], rels, x)  # noqa: E731
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for eid, (etype, names) in ents.items():
        if etype != "person" or named(eid):
            continue
        for n in names:
            kd = kin_description(n)
            owner = known(kd[0], etype, eid) if kd else None
            if owner and named(owner):
                groups.setdefault((owner, kd[1]["role"], kd[1]["word"]), []).append((eid, kd[1]))
                break
    out = []
    for (owner, _, _), found in groups.items():
        if len(found) != 1:
            continue
        eid, parsed = found[0]
        ties = [(x, r) for x, r in kin_ties(rels, owner, parsed, sex, named) if x != eid]
        fits = list(dict.fromkeys(x for x, _ in ties))
        if len(fits) != 1:
            continue
        target = fits[0]
        if first.get(target, (0, 0))[0] < first.get(eid, (0, 0))[0] \
                or min(tie_at.get(r["id"], (1 << 30, 0)) for _, r in ties) > first.get(target, (-1, -1)) \
                or any({r["src"], r["dst"]} == {target, eid} for r in rels) \
                or hidden_identity(facets.get(eid, []), events, eid, target):
            continue
        label, keep_label = ents[eid][1][0], ents[target][1][0]
        changed = merge.merge(docs, eid, target, None, counts)
        out.append((label, keep_label, changed))
        ents[eid][1].extend(n for n in ents[target][1] if n not in ents[eid][1])
        forget(target)
        ents.pop(target, None)
    return out


def remove(docs: list[dict], eid: str) -> list[int]:
    """Delete an entity and anything that only makes sense with it (its relations, its place in events)."""
    changed = []
    for i, d in enumerate(docs):
        before = (len(d.get("entities", [])), len(d.get("relations", [])), sum(len(ev["entities"]) for ev in d.get("events", [])))
        d["entities"] = [e for e in d.get("entities", []) if e["id"] != eid]
        d["relations"] = [r for r in d.get("relations", []) if eid not in (r["src"], r["dst"])]
        for ev in d.get("events", []):
            ev["entities"] = [x for x in ev["entities"] if x != eid]
        if "carried" in d:
            d["carried"] = [c for c in d["carried"] if c["id"] != eid]
        if before != (len(d["entities"]), len(d["relations"]), sum(len(ev["entities"]) for ev in d.get("events", []))):
            changed.append(i)
    return changed


def clear(docs: list[dict]) -> int:
    """Remove every profile (and any reference to one), so they can be written again."""
    gone = {f["id"] for d in docs for e in d.get("entities", []) for f in e.get("facets", []) if f.get("kind") == "profile"}
    for d in docs:
        for e in d.get("entities", []):
            e["facets"] = [f for f in e.get("facets", []) if f["id"] not in gone]
            for f in e["facets"]:
                if f.get("replaces"):
                    f["replaces"] = [r for r in f["replaces"] if r not in gone]
                    if not f["replaces"]:
                        del f["replaces"]
    return len(gone)


# ---- backfill --------------------------------------------------------------------------------

def backfill(docs: list[dict], model, batch: int = 15, log=print) -> dict:
    """Add profile facets to every doc (series reading order), from the facts each already has.
    Docs that already have profiles are only used as a starting point for later books."""
    current: dict[str, dict] = {}            # entity → its latest profile facet so far in the series
    facts_so_far: dict[str, list[str]] = {}  # entity → texts of current facts from finished books
    stats = Counter()
    for d in docs:
        ords = {a["id"]: i for i, a in enumerate(d["anchors"])}
        has = any(f.get("kind") == "profile" for e in d.get("entities", []) for f in e.get("facets", []))
        if not has:
            stats += _backfill_doc(d, ords, model, current, facts_so_far, batch, log)
        # What later books inherit: this book's last profile and its current facts.
        replaced = {r for e in d.get("entities", []) for f in e.get("facets", []) for r in f.get("replaces", [])}
        for e in d.get("entities", []):
            ps = sorted((f for f in e.get("facets", []) if f.get("kind") == "profile"), key=lambda f: ords[f["from"]])
            if ps:
                current[e["id"]] = ps[-1]
            facts_so_far.setdefault(e["id"], []).extend(
                f["text"] for f in e.get("facets", []) if f.get("kind") != "profile" and f["id"] not in replaced)
    return dict(stats)


def _backfill_doc(d: dict, ords: dict, model, current: dict, earlier: dict, batch: int, log) -> Counter:
    stats = Counter()
    last = len(d["anchors"]) - 1
    marks = sorted({ords[r["from"]] for r in d.get("recaps", []) if r["from"] in ords} | {last})
    names = {e["id"]: [n["value"] for n in e.get("names", [])] for e in d.get("entities", [])}
    ents = {e["id"]: e for e in d.get("entities", [])}
    since = Counter()   # facts gained since the entity's last profile
    prev = -1
    for mark in marks:
        due = []
        for eid, e in ents.items():
            facts = [f for f in e.get("facets", []) if f.get("kind") != "profile"]
            gained = [f for f in facts if prev < ords[f["from"]] <= mark]
            if not gained:
                continue
            since[eid] += len(gained)
            known = [f for f in facts if ords[f["from"]] <= mark]
            if len(known) + len(earlier.get(eid, [])) < MIN_FACTS:
                continue
            if eid in current and since[eid] < NEW_FACTS:
                continue
            due.append((eid, known))
        prev = mark
        for i in range(0, len(due), batch):
            group = due[i:i + batch]
            lines = []
            for n, (eid, known) in enumerate(group):
                e = ents[eid]
                # Facts that a later fact replaced (as of this point) are left out: they're no longer true.
                gone = {r for f in known for r in f.get("replaces", [])}
                texts = (earlier.get(eid, []) + [f["text"] for f in known if f["id"] not in gone])[-MAX_FACTS:]
                lines.append(f"E{n + 1}: {names[eid][0]} ({e['type']}; also {', '.join(names[eid][1:4]) or 'no other names'})"
                             + (f"\n  previous profile: {current[eid]['text']}" if eid in current else "")
                             + "".join(f"\n  - {t}" for t in texts))
            out = model.complete(SYSTEM, PROFILE.format(entities="\n".join(lines)), PROFILE_SCHEMA)
            for p in out.get("profiles", []) if isinstance(out, dict) else []:
                try:
                    eid, known = group[int(str(p.get("id", "")).lstrip("Ee")) - 1]
                except (ValueError, IndexError):
                    continue
                text = " ".join(str(p.get("text", "")).split())
                if not text or len(text) > 480 or HINDSIGHT.search(text) or MOMENTARY.search(text):
                    stats["dropped"] += 1
                    continue
                at = max(known, key=lambda f: ords[f["from"]])
                evidence = sorted({a for f in known for a in f.get("evidence", []) if ords.get(a, last + 1) <= ords[at["from"]]},
                                  key=lambda a: ords[a])
                n = sum(f.get("kind") == "profile" for f in ents[eid]["facets"]) + 1
                facet = {"id": f"{d['id']}.{eid}.profile-{n}", "kind": "profile", "from": at["from"], "basis": "stated",
                         "evidence": evidence or [at["from"]], "text": text}
                if eid in current:
                    facet["replaces"] = [current[eid]["id"]]
                ents[eid]["facets"].append(facet)
                current[eid] = facet
                since[eid] = 0
                stats["profiles"] += 1
            log(f"  {d['work']['title']}: {stats['profiles']} profiles so far")
    return stats

