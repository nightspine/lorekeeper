"""Reference spoiler-gating algorithm (SPEC.md §7).

`view()` turns one or more index documents plus a reading position into the
"view" object every conforming client must be able to reproduce. The
conformance suite in conformance/ is generated from, and checked against,
this function.
"""

from __future__ import annotations

from dataclasses import dataclass

SPEC_VERSION = "0.1"


@dataclass(frozen=True)
class Position:
    book: str | None   # series book key; None for a standalone index
    anchor: str | None  # last anchor passed; None = before the first anchor


def _ordinals(index: dict) -> dict[str, int]:
    return {a["id"]: i for i, a in enumerate(index["anchors"])}


def _book_key(index: dict) -> str | None:
    s = index.get("series")
    return s["book"] if s else None


def view(
    indexes: list[dict],
    position: Position,
    series: dict | None = None,
    order: str | None = None,
    finished: set[str] | None = None,
) -> dict:
    """Compute what a reader at `position` may see.

    finished: book keys the reader has completed. Defaults to every book that
    precedes `position.book` in the chosen reading order.
    """
    if series is None:
        if len(indexes) != 1:
            raise ValueError("multiple indexes require a series manifest")
        reading_order = [_book_key(indexes[0])]
        current = reading_order[0]
    else:
        reading_order = series["orders"][order or series["defaultOrder"]]
        current = position.book
        if current not in reading_order:
            raise ValueError(f"book {current!r} is not in reading order {order!r}")

    if finished is None:
        finished = set(reading_order[: reading_order.index(current)])
    finished = set(finished) - {current}
    book_rank = {k: i for i, k in enumerate(reading_order)}

    by_book = {_book_key(ix): ix for ix in indexes}
    if current not in by_book:
        raise ValueError(f"no index supplied for current book {current!r}")
    cur_index = by_book[current]
    cur_ords = _ordinals(cur_index)
    if position.anchor is not None and position.anchor not in cur_ords:
        raise ValueError(f"unknown anchor {position.anchor!r}")
    limit = cur_ords[position.anchor] if position.anchor is not None else -1

    # Each visible atom gets a sort key (book rank, anchor ordinal, file order).
    def visible_key(book: str | None, ords: dict[str, int], anchor: str | None, n: int):
        if book == current:
            if anchor is None:
                return None
            o = ords[anchor]
            return (book_rank[book], o, n) if o <= limit else None
        if book in finished:
            o = ords[anchor] if anchor is not None else -1
            return (book_rank.get(book, len(book_rank)), o, n)
        return None

    entities: dict[str, dict] = {}
    names: dict[str, list] = {}
    facets: list[tuple] = []
    relations: list[tuple] = []
    events: list[tuple] = []
    recaps: list[tuple] = []
    replaced: set[str] = set()
    counter = 0

    def take(book, ords, item, field="from"):
        nonlocal counter
        counter += 1
        return visible_key(book, ords, item.get(field), counter)

    sources: list[tuple[str | None, dict[str, int], dict, bool]] = []
    for ix in indexes:
        sources.append((_book_key(ix), _ordinals(ix), ix, False))
    # Carried entries stand in for earlier books whose indexes were not supplied.
    for entry in cur_index.get("carried", []):
        if entry["origin"] not in by_book and entry["origin"] in finished:
            sources.append((entry["origin"], {}, {"entities": [entry]}, True))

    for book, ords, ix, carried in sources:
        for e in ix.get("entities", []):
            k = visible_key(book, ords, None if carried else e["from"], counter)
            if k is None:
                continue
            ent = entities.setdefault(e["id"], {"id": e["id"], "type": e["type"], "_key": k})
            ent["_key"] = min(ent["_key"], k)
            for nm in e.get("names", []):
                nk = take(book, ords, nm)
                if nk is not None:
                    names.setdefault(e["id"], []).append((nk, nm))
            for f in e.get("facets", []):
                fk = take(book, ords, f)
                if fk is not None:
                    facets.append((fk, e["id"], f, book))
                    replaced.update(f.get("replaces", []))
        for r in ix.get("relations", []):
            rk = take(book, ords, r)
            if rk is not None:
                relations.append((rk, r, book))
                replaced.update(r.get("replaces", []))
        for ev in ix.get("events", []):
            ek = take(book, ords, ev)
            if ek is not None:
                events.append((ek, ev, book))
        for rc in ix.get("recaps", []):
            rck = take(book, ords, rc)
            if rck is not None:
                recaps.append((rck, rc, book))

    out_entities = []
    for ent in sorted(entities.values(), key=lambda e: (e["_key"], e["id"])):
        eid = ent["id"]
        visible_names = [nm for _, nm in sorted(names.get(eid, []), key=lambda t: t[0])]
        primaries = [nm["value"] for nm in visible_names if nm.get("kind") == "primary"]
        display = primaries[-1] if primaries else (visible_names[0]["value"] if visible_names else eid)
        aliases: list[str] = []
        for nm in visible_names:
            if nm["value"] != display and nm["value"] not in aliases:
                aliases.append(nm["value"])
        mine = sorted((t for t in facets if t[1] == eid), key=lambda t: t[0])
        # The last visible profile describes the entity, inherited from earlier books until a
        # later one is visible; earlier profiles are history.
        profiles = [t for t in mine if t[2].get("kind") == "profile"]
        profile = profiles[-1] if profiles else None
        out_entities.append({
            "id": eid,
            "type": ent["type"],
            "name": display,
            "aliases": aliases,
            "profile": _facet(profile[2], profile[3]) if profile else None,
            "facets": [_facet(f, b) for _, _, f, b in mine if f["id"] not in replaced and f.get("kind") != "profile"],
            "history": [_facet(f, b) for t in mine for _, _, f, b in [t]
                        if t is not profile and (f["id"] in replaced or f.get("kind") == "profile")],
        })

    visible_ids = {e["id"] for e in out_entities}
    rel_sorted = [(r, b) for _, r, b in sorted(relations, key=lambda t: t[0])
                  if r["src"] in visible_ids and r["dst"] in visible_ids]
    story = [t for t in recaps if t[1].get("scope", "story") == "story"]
    latest_recap = max(story, key=lambda t: t[0]) if story else None

    coverage = cur_index["coverage"]
    exhausted = (not coverage["complete"] and position.anchor is not None
                 and limit >= cur_ords[coverage["through"]])

    return {
        "lorekeeper": SPEC_VERSION,
        "kind": "view",
        "position": {"book": position.book if series else None, "anchor": position.anchor},
        "coverage": {"complete": coverage["complete"], "through": coverage["through"],
                     "exhausted": exhausted},
        "entities": out_entities,
        "relations": [_relation(r, b) for r, b in rel_sorted if r["id"] not in replaced],
        "relationHistory": [_relation(r, b) for r, b in rel_sorted if r["id"] in replaced],
        "events": [{"id": ev["id"], "summary": ev["summary"], "entities": ev["entities"], "book": b}
                   for _, ev, b in sorted(events, key=lambda t: t[0])],
        "recap": ({"id": latest_recap[1]["id"], "text": latest_recap[1]["text"],
                   "book": latest_recap[2]} if latest_recap else None),
        "chapters": [{"id": rc["id"], "title": rc.get("title"), "text": rc["text"], "book": b}
                     for _, rc, b in sorted(recaps, key=lambda t: t[0]) if rc.get("scope") == "chapter"],
    }


def _facet(f: dict, book: str | None) -> dict:
    return {"id": f["id"], "text": f["text"], "basis": f["basis"], "book": book}


def _relation(r: dict, book: str | None) -> dict:
    return {"id": r["id"], "src": r["src"], "dst": r["dst"], "label": r["label"], "book": book}
