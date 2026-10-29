"""Index validation: schema plus the mechanical rules of SPEC.md §6 and §10.

Checks that need only the index run always. Checks that need the book text
(anchor resolution, verbatim-quote limits) run when blocks are supplied.
Cross-book checks run when a series and its other indexes are supplied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

from . import anchors as anchor_mod
from . import text
from .epub import Block

MAX_VERBATIM_WORDS = 8


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


def _schema_dir() -> Path:
    packaged = resources.files("lorekeeper") / "schema"
    if packaged.is_dir():
        return Path(str(packaged))
    return Path(__file__).resolve().parents[2] / "schema"


@cache
def schema(name: str) -> dict:
    return json.loads((_schema_dir() / f"{name}.schema.json").read_text())


def check_schema(doc: dict, name: str) -> list[Issue]:
    v = Draft202012Validator(schema(name), format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [Issue("error", "/" + "/".join(str(p) for p in e.absolute_path), e.message)
            for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))]


def _items(index: dict):
    """Yield (kind, where, item) for every gated item in an index."""
    for i, e in enumerate(index.get("entities", [])):
        yield "entity", f"entities[{i}]", e
        for j, n in enumerate(e.get("names", [])):
            yield "name", f"entities[{i}].names[{j}]", n
        for j, f in enumerate(e.get("facets", [])):
            yield "facet", f"entities[{i}].facets[{j}]", f
    for i, r in enumerate(index.get("relations", [])):
        yield "relation", f"relations[{i}]", r
    for i, ev in enumerate(index.get("events", [])):
        yield "event", f"events[{i}]", ev
    for i, rc in enumerate(index.get("recaps", [])):
        yield "recap", f"recaps[{i}]", rc


def check_index(index: dict, prior: list[dict] | None = None) -> list[Issue]:
    """Rules that need only this index (and, for series, the indexes of earlier books).

    prior=None means the earlier books were not supplied, so references that
    could point into them are downgraded to warnings.
    """
    issues = check_schema(index, "index")
    if issues:
        return issues
    unknown_prior = "series" in index and prior is None
    prior = prior or []

    ords: dict[str, int] = {}
    for i, a in enumerate(index["anchors"]):
        if a["id"] in ords:
            issues.append(Issue("error", f"anchors[{i}]", f"duplicate anchor id {a['id']!r}"))
        ords[a["id"]] = i
    seqs = [a["seq"] for a in index["anchors"] if "seq" in a]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        issues.append(Issue("error", "anchors", "seq hints must be strictly increasing"))

    def ref(where: str, anchor: str) -> int | None:
        if anchor not in ords:
            issues.append(Issue("error", where, f"unknown anchor {anchor!r}"))
            return None
        return ords[anchor]

    through = ref("coverage.through", index["coverage"]["through"])
    if index["coverage"]["complete"] and through is not None and through != len(ords) - 1:
        issues.append(Issue("error", "coverage.through", "a complete index must cover through its last anchor"))

    prior_entities = {e["id"] for p in prior for e in p.get("entities", [])}
    prior_items = {x["id"]: k for p in prior for k, _, x in _items(p) if k in ("facet", "relation")}
    profiles = {x["id"] for p in [*prior, index] for k, _, x in _items(p) if k == "facet" and x.get("kind") == "profile"}
    entity_from = {e["id"]: ords.get(e["from"]) for e in index["entities"]}
    item_kind: dict[str, str] = {}
    item_from: dict[str, int | None] = {}

    for kind, where, item in _items(index):
        if kind in ("entity", "name"):
            continue
        if item["id"] in item_kind or item["id"] in prior_items:
            issues.append(Issue("error", where, f"duplicate item id {item['id']!r}"))
        item_kind[item["id"]] = kind
        item_from[item["id"]] = ords.get(item["from"])

    for kind, where, item in _items(index):
        f = ref(f"{where}.from", item["from"])
        if f is None:
            continue
        if through is not None and f > through:
            issues.append(Issue("error", where, "revealed after coverage.through"))
        for k, ev in enumerate(item.get("evidence", [])):
            e = ref(f"{where}.evidence[{k}]", ev)
            if e is not None and e > f:
                issues.append(Issue("error", f"{where}.evidence[{k}]",
                                    f"evidence {ev!r} comes after the reveal point {item['from']!r}"))
        if kind == "entity":
            parts = [ords.get(x["from"]) for x in item["names"] + item["facets"]]
            if not item["names"] and item["id"] not in prior_entities:
                issues.append(Issue("warning" if unknown_prior else "error", where,
                                    "entity has no names"))
            if any(p is not None and p < f for p in parts):
                issues.append(Issue("error", where, "a name or facet is revealed before the entity"))
            if item["names"] and min(ords.get(n["from"], f) for n in item["names"]) != f \
                    and item["id"] not in prior_entities:
                issues.append(Issue("error", where, "entity.from must equal its earliest name"))
        if kind in ("relation", "event"):
            targets = [item["src"], item["dst"]] if kind == "relation" else item["entities"]
            for t in targets:
                if t in entity_from:
                    tf = entity_from[t]
                    if tf is not None and tf > f and t not in prior_entities:
                        issues.append(Issue("warning" if unknown_prior else "error", where,
                                            f"references {t!r} before it is revealed"))
                elif t not in prior_entities:
                    issues.append(Issue("warning" if unknown_prior else "error", where,
                                        f"unknown entity {t!r}"))
        for r in item.get("replaces", []):
            if kind == "facet" and (r in profiles) != (item.get("kind") == "profile") and (r in item_kind or r in prior_items):
                issues.append(Issue("warning", where, "a profile should replace only profiles, and details only details"))
            if r in item_kind:
                if item_kind[r] != kind:
                    issues.append(Issue("error", where, f"{kind} cannot replace {item_kind[r]} {r!r}"))
                elif (item_from[r] or 0) > f:
                    issues.append(Issue("error", where, f"replaces {r!r}, which is revealed later"))
            elif r in prior_items:
                if prior_items[r] != kind:
                    issues.append(Issue("error", where, f"{kind} cannot replace {prior_items[r]} {r!r}"))
            else:
                issues.append(Issue("warning" if unknown_prior else "error", where,
                                    f"replaces unknown item {r!r}"))
    return issues


def check_against_book(index: dict, blocks: list[Block]) -> list[Issue]:
    """Rules that need the book text: anchors resolve, hints match, no long verbatim runs."""
    issues: list[Issue] = []
    resolved = anchor_mod.resolve(index["anchors"], blocks)
    book_hash = text.text_hash(b.words for b in blocks)
    exact = any(e["textHash"] == book_hash and e.get("builtFrom") for e in index["editions"])
    for i, a in enumerate(index["anchors"]):
        loc = resolved[a["id"]]
        if loc is None:
            issues.append(Issue("error", f"anchors[{i}]", f"anchor {a['id']!r} not found in this edition"))
            continue
        if len(blocks[loc].words) < text.MIN_ANCHOR_WORDS:
            issues.append(Issue("error", f"anchors[{i}]",
                                f"anchor block has fewer than {text.MIN_ANCHOR_WORDS} words"))
        if sum(1 for b in blocks if b.fingerprint == a["fp"]) > 1:
            issues.append(Issue("warning", f"anchors[{i}]", "fingerprint is not unique in this edition"))
        if exact and "seq" in a and a["seq"] != loc:
            issues.append(Issue("error", f"anchors[{i}]", f"seq hint {a['seq']} but found at {loc}"))

    n = MAX_VERBATIM_WORDS + 1
    book = text.shingles([w for b in blocks for w in b.words], n)
    for kind, where, item in _items(index):
        field = {"facet": "text", "event": "summary", "recap": "text", "relation": "label"}.get(kind)
        if field is None:
            continue
        runs = text.copied_runs(item[field], book, n)
        if runs:
            issues.append(Issue("error", where,
                                f"quotes more than {MAX_VERBATIM_WORDS} consecutive words: {runs[0]!r}"))
    return issues


def check_series(series: dict, indexes: list[dict]) -> list[Issue]:
    """Validate a series manifest and each index against the books before it."""
    issues = check_schema(series, "series")
    if issues:
        return issues
    keys = [b["key"] for b in series["books"]]
    for name, order in series["orders"].items():
        for k in order:
            if k not in keys:
                issues.append(Issue("error", f"orders.{name}", f"unknown book {k!r}"))
    if series["defaultOrder"] not in series["orders"]:
        issues.append(Issue("error", "defaultOrder", "not one of the declared orders"))
    by_book = {}
    for ix in indexes:
        s = ix.get("series")
        if not s or s["id"] != series["id"]:
            issues.append(Issue("error", ix.get("id", "?"), "index does not belong to this series"))
            continue
        by_book[s["book"]] = ix
    # An item may only depend on books that come before it in every declared order.
    for k, ix in by_book.items():
        before = set(keys)
        for order in series["orders"].values():
            if k in order:
                before &= set(order[: order.index(k)])
        prior = [by_book[p] for p in keys if p in before and p in by_book]
        for issue in check_index(ix, prior):
            issues.append(Issue(issue.level, f"{k}: {issue.where}", issue.message))
    return issues
