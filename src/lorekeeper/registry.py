"""Registries (SPEC.md §8.7): share bundles, listings, and the static build a file-only registry serves.

A registry's working tree is plain files::

    indexes/<publisher>/<slug>/listing.json
    indexes/<publisher>/<slug>/v<N>.lore.json     immutable
    indexes/<publisher>/<slug>/series.json        when the book is in a series
    removed.json

A bundle comes from a stranger and `check`/`add` may run with write access, so
paths are built only from the validated publisher and the computed slug, the
size cap applies before parsing, and nothing found inside a bundle is ever
used as a path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import anchors as anchor_mod
from . import text, validate
from .epub import Block
from .validate import Issue

MAX_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_DEPTH = 32  # no real index nests this deep; deeper data would only break copying and re-serialising later
LICENSES = ("CC0-1.0", "CC-BY-4.0")
PUBLISHER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
LOOKUP_VALUE_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SLUG = 128
EXAMPLES_SHOWN = 5


class RegistryError(Exception):
    """A refusal that is not an index finding (bad arguments, unreadable files)."""


# ---- small helpers --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")


def valid_timestamp(value) -> bool:
    """YYYY-MM-DDTHH:MM:SSZ, optional fractional seconds, UTC `Z` only, and a real date and time."""
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def dumps(doc) -> str:
    """How every stored index is written, once: its bytes are what the digest covers."""
    return json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def slugify(index_id: str) -> str:
    """The index's `id` with every character outside [a-z0-9-] replaced by "-" after lowercasing."""
    return re.sub(r"[^a-z0-9-]", "-", str(index_id).lower())


def normalize_publisher(publisher: str) -> str:
    if not isinstance(publisher, str) or not PUBLISHER_RE.fullmatch(publisher):
        raise RegistryError("publisher must be 1 to 39 letters, digits or hyphens, not starting or ending with a hyphen")
    return publisher.lower()


def _err(where: str, message: str) -> Issue:
    return Issue("error", where, message)


def errors_of(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]


def _too_deep(doc) -> str | None:
    """Path of the first value nested deeper than MAX_DEPTH, found without recursion."""
    stack = [(doc, 0, "$")]
    while stack:
        node, depth, path = stack.pop()
        if isinstance(node, dict):
            kids = [(v, f"{path}.{k}") for k, v in node.items()]
        elif isinstance(node, list):
            kids = [(v, f"{path}[{i}]") for i, v in enumerate(node)]
        else:
            continue
        if depth + 1 > MAX_DEPTH:
            return path
        stack += [(v, depth + 1, q) for v, q in kids]
    return None


def _rel(source: Path, p: Path) -> str:
    """A source-tree file as a path relative to the source directory: reports end up in public places."""
    try:
        return p.relative_to(source).as_posix()
    except ValueError:
        return p.name


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def parse_json_capped(raw: bytes, what: str) -> tuple[object | None, list[Issue]]:
    """Parse JSON bytes the way registry files are read: the size cap before parsing, no NaN/Infinity,
    nesting limited, no lone surrogates. Never raises."""
    if len(raw) > MAX_BUNDLE_BYTES:
        return None, [_err(what, f"file is over the {MAX_BUNDLE_BYTES // (1024 * 1024)} MB limit")]
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        deep = _too_deep(doc)
        if deep:
            return None, [_err(what, f"not valid JSON for a registry: nested deeper than {MAX_DEPTH} levels at {deep}")]
        json.dumps(doc, ensure_ascii=False, allow_nan=False).encode("utf-8")  # a lone surrogate fails here
        return doc, []
    except ValueError as e:
        return None, [_err(what, f"not valid JSON for a registry: {e}")]
    except RecursionError:
        return None, [_err(what, "not valid JSON for a registry: nested too deeply")]


def _read_json_capped(path: Path, what: str) -> tuple[object | None, list[Issue]]:
    """Read a JSON file, refusing one over the size cap before it is parsed."""
    try:
        size = path.stat().st_size
    except OSError as e:
        return None, [_err(what, f"cannot read: {e.strerror or e}")]
    if size > MAX_BUNDLE_BYTES:
        return None, [_err(what, f"file is {size} bytes, over the {MAX_BUNDLE_BYTES // (1024 * 1024)} MB limit")]
    try:
        with path.open("rb") as f:
            raw = f.read(MAX_BUNDLE_BYTES + 1)
    except OSError as e:
        return None, [_err(what, f"cannot read: {e.strerror or 'error'}")]
    return parse_json_capped(raw, what)


def scrub(index: dict) -> dict:
    """A copy of the index with every edition's `label` and `fileHash` removed (`builtFrom` stays)."""
    out = copy.deepcopy(index)
    for ed in out.get("editions", []):
        ed.pop("label", None)
        ed.pop("fileHash", None)
    return out


# ---- spoiler gate ---------------------------------------------------------------------------

def spoiler_gate(index: dict, blocks: list[Block]) -> dict:
    """How early does the index say each name compared with where the book first uses it?

    For every name of every entity, find the first block whose words contain the name's words as
    a contiguous run. `found` counts names that occur; `early` counts names whose own `from`
    anchor sits at a lower seq than that block. Anchor locations come from `anchors.resolve`
    (an unresolved anchor takes the next resolved one's, as gating does).
    """
    resolved = anchor_mod.resolve(index["anchors"], blocks)
    eff = anchor_mod.effective_locations(index["anchors"], resolved)
    loc = {a["id"]: eff[i] for i, a in enumerate(index["anchors"])}
    # One pass over the book with a trie of the names' words: cost is the book's words times the longest
    # match actually present, however many names (or names sharing a first word) an index lists.
    root: list = [{}, None, False]       # [children, first seq, ends a name]
    wanted: dict[tuple, list] = {}

    def node_of(name_words: list[str]) -> list:
        key = tuple(name_words)
        if key not in wanted:
            node = root
            for w in key:
                node = node[0].setdefault(w, [{}, None, False])
            node[2] = True
            wanted[key] = node
        return wanted[key]

    for ent in index["entities"]:
        for nm in ent.get("names", []):
            nw = text.words(nm.get("value", ""))
            if nw:
                node_of(nw)
    pending = len({id(n) for n in wanted.values()})
    for b in blocks:
        if not pending:
            break
        ws = b.words
        for i in range(len(ws)):
            node = root[0].get(ws[i])
            j = i
            while node is not None:
                if node[2] and node[1] is None:
                    node[1] = b.seq
                    pending -= 1
                j += 1
                node = node[0].get(ws[j]) if j < len(ws) else None

    def first_use(name_words: list[str]) -> int | None:
        return wanted[tuple(name_words)][1]

    names = found = early = 0
    examples: list[dict] = []
    for ent in index["entities"]:
        for nm in ent.get("names", []):
            names += 1
            nw = text.words(nm.get("value", ""))
            if not nw:
                continue
            seq = first_use(nw)
            if seq is None:
                continue
            found += 1
            if loc.get(nm.get("from"), float("inf")) < seq:
                early += 1
                if len(examples) < EXAMPLES_SHOWN:
                    examples.append({"entity": ent["id"], "name": nm["value"]})
    return {"names": names, "found": found, "early": early, "examples": examples}


# ---- share ----------------------------------------------------------------------------------

def make_bundle(index: dict, *, series: dict | None = None, derived_from: str | None = None,
                blocks: list[Block] | None = None, now: str | None = None) -> tuple[dict | None, dict]:
    """Build a share bundle. Returns (bundle or None, report); the report always lists the findings."""
    if now is not None and not valid_timestamp(now):
        return None, {"issues": [_err("now", f"{now!r} is not a UTC timestamp like 2026-11-13T20:00:00Z")]}
    now = now or now_iso()
    issues: list[Issue] = validate.check_schema(index, "index")
    report: dict = {"issues": issues}
    if errors_of(issues):
        return None, report
    index = scrub(index)
    in_series = bool(index.get("series"))
    issues += validate.check_index(index, None if in_series else [])
    lic = index.get("provenance", {}).get("license")
    if lic not in LICENSES:
        issues.append(_err("provenance.license", f"licence {lic!r} cannot be shared: use one of {', '.join(LICENSES)}"))
    if in_series and series is None:
        issues.append(_err("series", "the index belongs to a series: pass the series manifest (--series)"))
    if series is not None:
        if not in_series:
            issues.append(_err("series", "the index has no `series`, but a series manifest was given"))
        else:
            issues += [i for i in validate.check_schema(series, "series")]
            if series.get("id") != index["series"]["id"]:
                issues.append(_err("series", "the manifest is not the index's series"))
            elif index["series"]["book"] not in [b.get("key") for b in series.get("books", [])]:
                issues.append(_err("series", f"book {index['series']['book']!r} is not in the manifest"))
    if derived_from is not None and not DIGEST_RE.fullmatch(derived_from):
        issues.append(_err("derivedFrom", "must be sha256: followed by 64 hex digits"))
    attestations: list[dict] = []
    if blocks is not None:
        th = text.text_hash(b.words for b in blocks)
        if th not in {e["textHash"] for e in index["editions"]}:
            issues.append(_err("epub", "the EPUB's text hash is not one of the index's editions"))
        else:
            book_issues = validate.check_against_book(index, blocks)
            bad = errors_of(book_issues)
            issues += book_issues
            resolved = anchor_mod.resolve(index["anchors"], blocks)
            gate = spoiler_gate(index, blocks)
            report["spoilerGate"] = gate
            if not bad:
                attestations = [
                    {"kind": "book-checks", "by": "builder", "at": now, "textHash": th,
                     "anchors": len(index["anchors"]),
                     "resolved": sum(1 for v in resolved.values() if v is not None), "errors": 0},
                    {"kind": "spoiler-gate", "by": "builder", "at": now, "textHash": th,
                     "names": gate["names"], "found": gate["found"], "early": gate["early"]},
                ]
    if errors_of(issues):
        return None, report
    bundle: dict = {"lorekeeper": "0.1", "kind": "share", "index": index}
    if series is not None:
        bundle["series"] = series
    if derived_from:
        bundle["derivedFrom"] = derived_from
    if attestations:
        bundle["attestations"] = attestations
    shape = validate.check_schema(bundle, "share")
    if shape:  # should be unreachable; never write a bundle the schema would refuse
        issues += shape
        return None, report
    return bundle, report


# ---- the registry source tree ---------------------------------------------------------------

def _listing_dir(source: Path, publisher: str, slug: str) -> Path:
    """The only way a path under indexes/ is made: validated publisher, computed slug."""
    if not PUBLISHER_RE.fullmatch(publisher) or publisher != publisher.lower():
        raise RegistryError("invalid publisher")
    if not slug or not re.fullmatch(r"[a-z0-9-]+", slug) or len(slug) > MAX_SLUG:
        raise RegistryError("the index id gives no usable slug")
    return source / "indexes" / publisher / slug


def _removed(source: Path) -> list[dict]:
    p = source / "removed.json"
    if not p.is_file():
        return []
    data, issues = _read_json_capped(p, "removed.json")
    if issues or not isinstance(data, list):
        raise RegistryError("removed.json is not a list")
    return data


def _n(a: dict, key: str) -> int:
    v = a.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def attestation_problems(a) -> list[str]:
    """Counts that cannot be true: an audit whose parts exceed its sample, checks that resolved more anchors
    than exist, a spoiler gate that found more names than it counted. Schema-valid, but never honest."""
    if not isinstance(a, dict):
        return []
    kind = a.get("kind")
    if kind == "audit":
        parts = _n(a, "supported") + _n(a, "unsupported") + _n(a, "unsure")
        if _n(a, "sampled") == 0 or parts > _n(a, "sampled"):
            return ["audit counts do not add up: supported + unsupported + unsure must be within a non-empty sample"]
    elif kind == "book-checks":
        if _n(a, "resolved") > _n(a, "anchors"):
            return ["book-checks counts do not add up: resolved is more than anchors"]
    elif kind == "spoiler-gate":
        if _n(a, "found") > _n(a, "names") or _n(a, "early") > _n(a, "found"):
            return ["spoiler-gate counts do not add up: early must be within found, found within names"]
    return []


def listing_problems(data, expected_id: str | None = None) -> list[str]:
    """Why a stored listing is not sound: its schema, its timestamps, and (when given) its directory."""
    bad = validate.check_schema(data, "listing")
    if bad:
        return [f"listing{i.where}: {i.message}" for i in bad]
    out = []
    if expected_id is not None and (data["id"] != expected_id or f"{data['publisher']}/{data['slug']}" != expected_id):
        out.append(f"listing id {data['id']!r} does not match its directory")
    stamps = [("updated", data["updated"])] + [(f"versions[{n}].updated", v["updated"]) for n, v in enumerate(data["versions"])] \
        + [(f"attestations[{n}].at", a["at"]) for n, a in enumerate(data["attestations"])]
    out += [f"{w}: {v!r} is not a UTC timestamp like 2026-11-13T20:00:00Z" for w, v in stamps if not valid_timestamp(v)]
    for n, a in enumerate(data["attestations"]):
        out += [f"attestations[{n}]: {m}" for m in attestation_problems(a)]
    return out


def _read_listing_file(source: Path, p: Path, expected_id: str) -> dict:
    rel = _rel(source, p)
    data, issues = _read_json_capped(p, rel)
    if issues:
        raise RegistryError("; ".join(f"{i.where}: {i.message}" for i in issues))
    problems = listing_problems(data, expected_id)
    if problems:
        raise RegistryError("\n".join(f"{rel}: {m}" for m in problems))
    return data


def read_listing(source: Path, publisher: str, slug: str) -> dict | None:
    p = _listing_dir(source, publisher, slug) / "listing.json"
    if not p.is_file():
        return None
    return _read_listing_file(source, p, f"{publisher}/{slug}")


def _publisher_listings(source: Path, publisher: str, skipped: list[str] | None = None) -> list[dict]:
    """Every sound listing of a publisher. An unsound one raises, unless `skipped` is given: then it is
    left out and its problem is appended there (takedown must work on a damaged tree)."""
    root = source / "indexes" / publisher
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            p = d / "listing.json"
            if d.is_dir() and p.is_file():
                try:
                    out.append(_read_listing_file(source, p, f"{publisher}/{d.name}"))
                except RegistryError as e:
                    if skipped is None:
                        raise
                    skipped.append(str(e))
    return out


def _latest_index(source: Path, listing: dict) -> dict:
    pub, slug = listing["publisher"], listing["slug"]
    path = _listing_dir(source, pub, slug) / f"v{int(listing['version'])}.lore.json"
    rel = _rel(source, path)
    doc, issues = _read_json_capped(path, rel)
    if issues or not isinstance(doc, dict):
        raise RegistryError("; ".join(f"{i.where}: {i.message}" for i in issues) or f"{rel} is not an index")
    return doc


def requires_for(index: dict, series: dict, source: Path, publisher: str) -> tuple[list[str], list[dict], list[Issue]]:
    """Listing ids of every earlier book in the manifest's publication order, their latest indexes, and
    an error for each earlier book the publisher has not shared yet."""
    order = series.get("orders", {}).get("publication") or series.get("orders", {}).get(series.get("defaultOrder"), [])
    book = index["series"]["book"]
    if book not in order:
        return [], [], [_err("series", f"book {book!r} is not in the manifest's publication order")]
    earlier = order[:order.index(book)]
    mine = {(l.get("series") or {}).get("book"): l for l in _publisher_listings(source, publisher)
            if (l.get("series") or {}).get("id") == series["id"]}
    ids, priors, issues = [], [], []
    for key in earlier:
        listing = mine.get(key)
        if listing is None:
            issues.append(_err("series", f"earlier book {key!r} of series {series['id']!r} has not been shared by "
                                         f"{publisher} yet: add it first"))
            continue
        ids.append(listing["id"])
        try:
            priors.append(_latest_index(source, listing))
        except RegistryError as e:
            issues.append(_err("series", f"latest version of {listing['id']} is unreadable: {e}"))
    return ids, priors, issues


def check(bundle_path: str | Path, publisher: str, source: str | Path) -> tuple[dict | None, list[Issue]]:
    """Everything `add` verifies, writing nothing. Returns (context for add or None, issues)."""
    try:
        return _check(bundle_path, publisher, source)
    except RegistryError as e:  # an unreadable or unsound source tree is a finding, not a crash
        return None, [_err("source", m) for m in str(e).splitlines()]


def _preflight(bundle: dict) -> list[Issue]:
    """The checks with the clearest messages, run before the schema so these are what the person reads."""
    out: list[Issue] = []
    index = bundle.get("index")
    editions = index.get("editions") if isinstance(index, dict) else None
    for i, ed in enumerate(editions if isinstance(editions, list) else []):
        if isinstance(ed, dict) and ("label" in ed or "fileHash" in ed):
            out.append(_err(f"index.editions[{i}]", "still carries label or fileHash: scrub the bundle (lorekeeper share)"))
    atts = bundle.get("attestations")
    for i, a in enumerate(atts if isinstance(atts, list) else []):
        if not isinstance(a, dict):
            continue
        if a.get("by") != "builder":
            out.append(_err(f"attestations[{i}]", "bundle attestations must be by builder"))
        elif a.get("kind") == "validates":
            out.append(_err(f"attestations[{i}]", "a bundle cannot carry a validates attestation: only the registry adds that one"))
        if "at" in a and not valid_timestamp(a["at"]):
            out.append(_err(f"attestations[{i}].at", f"{a['at']!r} is not a UTC timestamp like 2026-11-13T20:00:00Z"))
        out += [_err(f"attestations[{i}]", m) for m in attestation_problems(a)]
    if bundle.get("series") is not None and isinstance(index, dict) and not index.get("series"):
        out.append(_err("series", "a series manifest came with an index that has no `series`"))
    return out


def _check(bundle_path: str | Path, publisher: str, source: str | Path) -> tuple[dict | None, list[Issue]]:
    source = Path(source)
    try:
        publisher = normalize_publisher(publisher)
    except RegistryError as e:
        return None, [_err("publisher", str(e))]
    bundle, issues = _read_json_capped(Path(bundle_path), "bundle")
    if issues:
        return None, issues
    if not isinstance(bundle, dict):
        return None, [_err("bundle", "not a JSON object")]
    pre = _preflight(bundle)
    if pre:
        return None, pre
    shape = validate.check_schema(bundle, "share")
    if shape:
        return None, [Issue("error", f"bundle{i.where}", i.message) for i in shape]
    index = bundle["index"]
    series = bundle.get("series")


    lic = index.get("provenance", {}).get("license")
    if lic not in LICENSES:
        issues.append(_err("index.provenance.license", f"licence {lic!r} not accepted: use one of {', '.join(LICENSES)}"))

    slug = slugify(index["id"])
    try:
        dir_ = _listing_dir(source, publisher, slug)
    except RegistryError as e:
        issues.append(_err("index.id", str(e)))
        return None, issues
    listing_id = f"{publisher}/{slug}"
    try:
        removed = {r.get("id") for r in _removed(source)}
    except RegistryError as e:
        issues.append(_err("removed.json", str(e)))
        removed = set()
    if listing_id in removed:
        issues.append(_err("id", f"{listing_id} was removed from this registry and cannot be shared again"))

    requires: list[str] = []
    priors: list[dict] = []
    if index.get("series"):
        requires, priors, series_issues = requires_for(index, series, source, publisher)
        issues += series_issues
        if not series_issues:
            issues += [Issue(i.level, f"series: {i.where}", i.message)
                       for i in validate.check_series(series, [*priors, index])]
    else:
        issues += validate.check_index(index, [])
    data = dumps(index).encode("utf-8")
    if len(data) > MAX_BUNDLE_BYTES:
        issues.append(_err("index", f"the index would be stored as {len(data)} bytes, over the "
                                    f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MB limit"))
    digest = digest_of(data)
    existing = read_listing(source, publisher, slug)
    if existing is not None:
        try:
            latest_id = _latest_index(source, existing).get("id")
            if latest_id != index["id"]:
                issues.append(_err("index.id", f"{listing_id} already belongs to a different index "
                                               f"({latest_id!r}); {index['id']!r} would share its slug"))
        except RegistryError as e:
            issues.append(_err("id", f"{listing_id}: latest version is unreadable: {e}"))
    if index.get("series"):
        for l in _publisher_listings(source, publisher):
            ls = l.get("series") or {}
            if l.get("id") != listing_id and ls.get("id") == index["series"]["id"] and ls.get("book") == index["series"]["book"]:
                issues.append(_err("series", f"{l['id']} is already this publisher's listing for book "
                                             f"{ls.get('book')!r} of series {ls.get('id')!r}"))
    if existing is not None and any(v.get("digest") == digest for v in existing.get("versions", [])):
        issues.append(_err("digest", f"already shared as {listing_id} ({digest})"))
    if existing is not None and index.get("series"):
        issues += _dependents_still_hold(index, series, existing, source, publisher)
    for i in bundle.get("attestations", []):
        if "textHash" in i and i["textHash"] not in {e["textHash"] for e in index["editions"]}:
            issues.append(_err("attestations", "an attestation names a text hash that is not one of the index's editions"))
    for ed in index["editions"]:
        for scheme, v in (ed.get("ids") or {}).items():
            vals = v if isinstance(v, list) else [v]
            if scheme in ("gutenberg", "openlibrary", "wikidata", "isbn13") and \
                    any(not isinstance(x, str) or not LOOKUP_VALUE_RE.fullmatch(x) for x in vals):
                issues.append(Issue("warning", "index.editions.ids", f"{scheme} id is not usable as a lookup key: {v!r}"))

    if errors_of(issues):
        return None, issues
    ctx = {"publisher": publisher, "slug": slug, "id": listing_id, "dir": dir_, "bundle": bundle,
           "data": data, "digest": digest, "requires": requires, "existing": existing}
    try:  # the listing `add` would store: schema and size are checked here, so `check` and `add` agree
        listing = make_listing(ctx, now_iso())
        bad = validate.check_schema(listing, "listing")
        if bad:
            issues += [Issue("error", f"listing{i.where}", i.message) for i in bad]
        elif len(dumps(listing).encode("utf-8")) > MAX_BUNDLE_BYTES:
            issues.append(_err("listing", f"the listing would be stored over the {MAX_BUNDLE_BYTES // (1024 * 1024)} MB limit"))
    except (ValueError, RecursionError) as e:
        issues.append(_err("listing", f"cannot be serialised: {e}"))
    if errors_of(issues):
        return None, issues
    return ctx, issues


def _dependents_still_hold(index: dict, series: dict, existing: dict, source: Path, publisher: str) -> list[Issue]:
    """A new version of a book other listings require must leave their series checks passing."""
    mine = [l for l in _publisher_listings(source, publisher)
            if (l.get("series") or {}).get("id") == index["series"]["id"] and l["id"] != existing["id"]]
    dependents = [l for l in mine if existing["id"] in (l["series"].get("requires") or [])]
    if not dependents:
        return []
    order = series.get("orders", {}).get("publication") or series.get("orders", {}).get(series.get("defaultOrder"), [])
    rank = {k: n for n, k in enumerate(order)}
    out: list[Issue] = []
    docs: list[tuple[int, dict]] = [(rank.get(index["series"]["book"], -1), index)]
    for l in mine:
        try:
            docs.append((rank.get(l["series"]["book"], len(order)), _latest_index(source, l)))
        except RegistryError as e:
            out.append(_err("series", f"latest version of {l['id']} is unreadable: {e}"))
    if out:
        return out
    names = ", ".join(sorted(l["id"] for l in dependents))
    return [Issue(i.level, f"series: {i.where}", f"a new version would break {names}: {i.message}")
            for i in validate.check_series(series, [d for _, d in sorted(docs, key=lambda t: t[0])]) if i.level == "error"]


def _merge_ids(work_ids: dict, ed_ids: dict) -> dict:
    """Join identifiers per scheme: both carrying one gives a de-duplicated list (a single shared value stays a string)."""
    out = {}
    for scheme in [*work_ids, *(k for k in ed_ids if k not in work_ids)]:
        a, b = work_ids.get(scheme), ed_ids.get(scheme)
        if a is None or b is None:
            out[scheme] = a if b is None else b
            continue
        vals: list = []
        for v in [*(a if isinstance(a, list) else [a]), *(b if isinstance(b, list) else [b])]:
            if v not in vals:
                vals.append(v)
        out[scheme] = vals[0] if len(vals) == 1 and not isinstance(a, list) and not isinstance(b, list) else vals
    return out


def _listing_editions(index: dict) -> list[dict]:
    work_ids = index["work"].get("ids") or {}
    out = []
    for ed in index["editions"]:
        ids = _merge_ids(work_ids, ed.get("ids") or {})
        item = {"textHash": ed["textHash"]}
        if ids:
            item["ids"] = ids
        out.append(item)
    return out


def make_listing(ctx: dict, now: str) -> dict:
    bundle, index, existing = ctx["bundle"], ctx["bundle"]["index"], ctx["existing"]
    version = (existing["version"] + 1) if existing else 1
    pub, slug = ctx["publisher"], ctx["slug"]
    href = f"/v0/indexes/{pub}/{slug}/v{version}.lore.json"
    prov = index["provenance"]
    contributors = prov.get("contributors", [])
    listing: dict = {
        "lorekeeper": "0.1", "kind": "listing", "id": ctx["id"], "publisher": pub, "slug": slug,
        "work": {k: index["work"][k] for k in ("title", "authors", "language")},
        "editions": _listing_editions(index),
        "version": version, "digest": ctx["digest"], "href": href, "updated": now,
        "complete": bool(index["coverage"]["complete"]),
        "license": prov["license"],
        "edited": any(c.get("role") in ("editor", "author") for c in contributors),
    }
    if index.get("bookKind"):
        listing["bookKind"] = index["bookKind"]
    if prov.get("generator"):
        listing["generator"] = prov["generator"]
    if index.get("series"):
        listing["series"] = {"id": index["series"]["id"], "book": index["series"]["book"],
                             "requires": ctx["requires"]}
    if bundle.get("derivedFrom"):
        listing["derivedFrom"] = bundle["derivedFrom"]
    listing["attestations"] = [{"kind": "validates", "by": "registry", "at": now},
                               *bundle.get("attestations", [])]
    listing["versions"] = [*(existing["versions"] if existing else []),
                           {"version": version, "digest": ctx["digest"], "href": href, "updated": now}]
    return listing


def add(bundle_path: str | Path, publisher: str, source: str | Path, now: str | None = None) -> tuple[dict | None, list[Issue]]:
    """`check`, then write the new version, the series manifest and the listing."""
    ctx, issues = check(bundle_path, publisher, source)
    if ctx is None:
        return None, issues
    if now is not None and not valid_timestamp(now):
        return None, [_err("now", f"{now!r} is not a UTC timestamp like 2026-11-13T20:00:00Z")]
    now = now or now_iso()
    listing = make_listing(ctx, now)
    bad = validate.check_schema(listing, "listing")
    if bad:
        return None, issues + [Issue("error", f"listing{i.where}", i.message) for i in bad]
    d: Path = ctx["dir"]
    try:  # serialise everything before the first write, so a failure leaves nothing half-written
        listing_text = dumps(listing)
        series_text = dumps(ctx["bundle"]["series"]) if ctx["bundle"].get("series") is not None else None
    except (ValueError, RecursionError) as e:
        return None, issues + [_err("listing", f"cannot be serialised: {e}")]
    if len(listing_text.encode("utf-8")) > MAX_BUNDLE_BYTES:
        return None, issues + [_err("listing", f"the listing would be stored over the {MAX_BUNDLE_BYTES // (1024 * 1024)} MB limit")]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{listing['version']}.lore.json").write_bytes(ctx["data"])
    if series_text is not None:
        (d / "series.json").write_text(series_text, encoding="utf-8")
    (d / "listing.json").write_text(listing_text, encoding="utf-8")
    return listing, issues


def remove(listing_id: str, source: str | Path, reason: str, now: str | None = None,
           with_dependents: bool = False, notes: list[str] | None = None) -> list[str]:
    """Delete a listing's directory and append it to removed.json. Later books of the same publisher
    that require it are refused unless `with_dependents`, which removes them too. Returns the ids removed."""
    source = Path(source)
    m = re.fullmatch(r"([A-Za-z0-9-]+)/([a-z0-9-]+)", listing_id or "")
    if not m or not reason or not reason.strip():
        raise RegistryError("give a listing id like publisher/slug and a reason")
    if now is not None and not valid_timestamp(now):
        raise RegistryError(f"--now {now!r} is not a UTC timestamp like 2026-11-13T20:00:00Z")
    pub, slug = m.group(1).lower(), m.group(2)
    d = _listing_dir(source, pub, slug)
    if not (d / "listing.json").is_file():
        raise RegistryError(f"no listing {listing_id}")
    target = f"{pub}/{slug}"
    skipped: list[str] = []
    dependents = sorted(l["id"] for l in _publisher_listings(source, pub, skipped)
                        if target in ((l.get("series") or {}).get("requires") or []))
    if notes is not None:
        notes += [f"skipped an unsound listing while looking for dependents: {m}" for m in skipped]
    if dependents and not with_dependents:
        raise RegistryError(f"{target} is required by {', '.join(dependents)}: remove those too with --with-dependents")
    log = _removed(source)
    stamp = now or now_iso()
    gone = [*dependents, target]
    for lid in gone:
        pb, sl = lid.split("/")
        dd = _listing_dir(source, pb, sl)
        shutil.rmtree(dd)
        if dd.parent.is_dir() and not any(dd.parent.iterdir()):
            dd.parent.rmdir()
        log.append({"id": lid, "removed": stamp, "reason": reason.strip()})
    (source / "removed.json").write_text(dumps(log), encoding="utf-8")
    return gone


# ---- build ----------------------------------------------------------------------------------

def _based(base: str, href: str) -> str:
    return href if re.match(r"^[a-z][a-z0-9+.-]*://", href) else base + href


def _sorted_json(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n"


def build(source: str | Path, out: str | Path, base_path: str = "", name: str = "Lorekeeper registry") -> dict:
    """Write the static registry. Validates everything first and raises RegistryError listing every problem;
    nothing is written (and OUT/v0 is not cleared) unless the source is sound."""
    source, out = Path(source), Path(out)
    base = base_path.rstrip("/")
    if base and not base.startswith("/"):
        base = "/" + base
    problems: list[str] = []
    listings: list[tuple[dict, Path]] = []
    root = source / "indexes"
    if root.is_dir():
        for pdir in sorted(p for p in root.iterdir() if p.is_dir()):
            for d in sorted(p for p in pdir.iterdir() if p.is_dir()):
                lp = d / "listing.json"
                where = f"{pdir.name}/{d.name}"
                if not lp.is_file():
                    problems.append(f"{where}: no listing.json")
                    continue
                data, issues = _read_json_capped(lp, where)
                problems += [f"{i.where}: {i.message}" for i in issues]
                if issues:
                    continue
                bad = listing_problems(data, where)
                if bad:
                    problems += [f"{where}: {m}" for m in bad]
                    continue
                if data["publisher"] != pdir.name or data["slug"] != d.name:
                    problems.append(f"{where}: listing id {data['id']!r} does not match its directory")
                    continue
                for v in data["versions"]:
                    f = d / f"v{v['version']}.lore.json"
                    if not f.is_file():
                        problems.append(f"{where}: missing {f.name}")
                    elif digest_of(f.read_bytes()) != v["digest"]:
                        problems.append(f"{where}: {f.name} does not match its digest")
                    else:
                        _, idx_issues = _read_json_capped(f, f"{where}/{f.name}")
                        problems += [f"{i.where}: {i.message}" for i in idx_issues]
                if data["versions"][-1]["version"] != data["version"] or data["versions"][-1]["digest"] != data["digest"]:
                    problems.append(f"{where}: the listing's latest version differs from its versions list")
                listings.append((data, d))
    removed_raw = None
    if (source / "removed.json").is_file():
        removed, rissues = _read_json_capped(source / "removed.json", "removed.json")
        if rissues:
            problems += [f"{i.where}: {i.message}" for i in rissues]
        else:
            removed_raw = (source / "removed.json").read_bytes()
            bad = validate.check_schema(removed, "api", "removed")
            problems += [f"removed.json{i.where}: {i.message}" for i in bad]
            if not bad:
                problems += [f"removed.json[{n}].removed: {r['removed']!r} is not a UTC timestamp like 2026-11-13T20:00:00Z"
                             for n, r in enumerate(removed) if not valid_timestamp(r.get("removed"))]
    known = {l["id"] for l, _ in listings}
    for l, _ in listings:
        for req in (l.get("series") or {}).get("requires", []):
            if req not in known:
                problems.append(f"{l['id']}: requires {req}, which has no listing")
    if problems:
        raise RegistryError("\n".join(problems))

    # Build everything in memory first.
    files: dict[str, bytes] = {}
    lookups: dict[str, list[dict]] = defaultdict(list)
    for listing, d in listings:
        pub, slug = listing["publisher"], listing["slug"]
        served = copy.deepcopy(listing)
        served["href"] = _based(base, served["href"])
        for v in served["versions"]:
            v["href"] = _based(base, v["href"])
        files[f"v0/listings/{pub}/{slug}.json"] = _sorted_json(served).encode()
        for v in listing["versions"]:
            files[f"v0/indexes/{pub}/{slug}/v{v['version']}.lore.json"] = \
                (d / f"v{v['version']}.lore.json").read_bytes()
        if (d / "series.json").is_file():
            files[f"v0/series/{pub}/{slug}.json"] = (d / "series.json").read_bytes()
        result = {"id": listing["id"], "title": listing["work"]["title"], "match": "text",
                  "complete": listing["complete"], "updated": listing["updated"],
                  "digest": listing["digest"], "href": served["href"], "listing": served}
        if listing.get("bookKind"):
            result["bookKind"] = listing["bookKind"]
        if listing.get("series"):
            result["series"] = {"id": listing["series"]["id"], "book": listing["series"]["book"]}
        keys: dict[str, str] = {}
        for ed in listing["editions"]:
            keys[f"text/{ed['textHash']}"] = "text"
            for scheme, v in (ed.get("ids") or {}).items():
                folder = {"isbn13": "isbn", "isbn": "isbn"}.get(scheme, scheme)
                if folder not in ("gutenberg", "isbn", "openlibrary", "wikidata"):
                    continue
                for val in (v if isinstance(v, list) else [v]):
                    if isinstance(val, str) and LOOKUP_VALUE_RE.fullmatch(val):
                        keys.setdefault(f"{folder}/{val}", "identifier")
        for key, match in keys.items():
            lookups[key].append({**result, "match": match})
    for key, results in lookups.items():
        results.sort(key=lambda r: (not r["complete"], _neg(r["updated"]), r["id"]))
        files[f"v0/lookup/{key}.json"] = _sorted_json({"results": results}).encode()
    files["v0/removed.json"] = removed_raw if removed_raw is not None else b"[]\n"
    discovery = {"lorekeeper": "0.1", "name": name, "api": f"{base}/v0",
                 "capabilities": ["lookup", "index", "series", "listings"],
                 "auth": {"type": "none"}, "static": True}
    files[".well-known/lorekeeper"] = _sorted_json(discovery).encode()
    files[".nojekyll"] = b""

    for rel, data in files.items():
        if rel.startswith("v0/lookup/"):
            body = json.loads(data)
            bad = validate.check_schema(body, "api", "lookup")
            if bad:
                raise RegistryError(f"{rel}: {bad[0].where}: {bad[0].message}")
    bad = validate.check_schema(discovery, "api", "discovery")
    if bad:
        raise RegistryError(f"discovery: {bad[0].message}")

    out.mkdir(parents=True, exist_ok=True)
    if (out / "v0").exists():
        shutil.rmtree(out / "v0")
    for rel, data in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return {"listings": len(listings), "lookups": len(lookups), "files": len(files)}


def _neg(updated: str) -> tuple:
    """Sort key putting newer `updated` first without parsing: invert each character."""
    return tuple(-ord(c) for c in updated)
