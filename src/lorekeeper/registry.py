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


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


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
        if len(raw) > MAX_BUNDLE_BYTES:
            return None, [_err(what, "file is over the 5 MB limit")]
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        json.dumps(doc, ensure_ascii=False, allow_nan=False).encode("utf-8")  # a lone surrogate fails here
        return doc, []
    except (ValueError, OSError) as e:
        return None, [_err(what, f"not valid JSON for a registry: {e}")]
    except RecursionError:
        return None, [_err(what, "not valid JSON for a registry: nested too deeply")]


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
    by_word: dict[str, list[int]] = defaultdict(list)
    for b in blocks:
        for w in set(b.words):
            by_word[w].append(b.seq)  # ascending, since blocks are in order

    def first_use(name_words: list[str]) -> int | None:
        n = len(name_words)
        for seq in by_word.get(name_words[0], ()):
            ws = blocks[seq].words
            for i in range(len(ws) - n + 1):
                if ws[i:i + n] == name_words:
                    return seq
        return None

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


def read_listing(source: Path, publisher: str, slug: str) -> dict | None:
    p = _listing_dir(source, publisher, slug) / "listing.json"
    if not p.is_file():
        return None
    data, issues = _read_json_capped(p, str(p))
    if issues or not isinstance(data, dict):
        raise RegistryError(f"{p} is not readable")
    return data


def _publisher_listings(source: Path, publisher: str) -> list[dict]:
    root = source / "indexes" / publisher
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            p = d / "listing.json"
            if d.is_dir() and p.is_file():
                data, issues = _read_json_capped(p, str(p))
                if not issues and isinstance(data, dict):
                    out.append(data)
    return out


def _latest_index(source: Path, listing: dict) -> dict:
    pub, slug = listing["publisher"], listing["slug"]
    path = _listing_dir(source, pub, slug) / f"v{int(listing['version'])}.lore.json"
    return json.loads(path.read_text(encoding="utf-8"))


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
        priors.append(_latest_index(source, listing))
    return ids, priors, issues


def check(bundle_path: str | Path, publisher: str, source: str | Path) -> tuple[dict | None, list[Issue]]:
    """Everything `add` verifies, writing nothing. Returns (context for add or None, issues)."""
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
    shape = validate.check_schema(bundle, "share")
    if shape:
        return None, [Issue("error", f"bundle{i.where}", i.message) for i in shape]
    index = bundle["index"]
    series = bundle.get("series")

    for i, ed in enumerate(index["editions"]):  # also caught by the schema; kept as the clear message
        if "label" in ed or "fileHash" in ed:
            issues.append(_err(f"index.editions[{i}]", "still carries label or fileHash: scrub the bundle (lorekeeper share)"))
    for i, a in enumerate(bundle.get("attestations", [])):
        if a.get("by") != "builder":
            issues.append(_err(f"attestations[{i}]", "bundle attestations must be by builder"))
        elif a.get("kind") == "validates":
            issues.append(_err(f"attestations[{i}]", "a bundle cannot carry a validates attestation: only the registry adds that one"))

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
    digest = digest_of(data)
    existing = read_listing(source, publisher, slug)
    if existing is not None and _latest_index(source, existing).get("id") != index["id"]:
        issues.append(_err("index.id", f"{listing_id} already belongs to a different index "
                                       f"({_latest_index(source, existing).get('id')!r}); {index['id']!r} would share its slug"))
    if index.get("series"):
        for l in _publisher_listings(source, publisher):
            ls = l.get("series") or {}
            if l.get("id") != listing_id and ls.get("id") == index["series"]["id"] and ls.get("book") == index["series"]["book"]:
                issues.append(_err("series", f"{l['id']} is already this publisher's listing for book "
                                             f"{ls.get('book')!r} of series {ls.get('id')!r}"))
    if existing is not None and any(v.get("digest") == digest for v in existing.get("versions", [])):
        issues.append(_err("digest", f"already shared as {listing_id} ({digest})"))
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
    return {"publisher": publisher, "slug": slug, "id": listing_id, "dir": dir_, "bundle": bundle,
            "data": data, "digest": digest, "requires": requires, "existing": existing}, issues


def _listing_editions(index: dict) -> list[dict]:
    work_ids = index["work"].get("ids") or {}
    out = []
    for ed in index["editions"]:
        ids = {**work_ids, **(ed.get("ids") or {})}
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
    now = now or now_iso()
    listing = make_listing(ctx, now)
    bad = validate.check_schema(listing, "listing")
    if bad:
        return None, issues + [Issue("error", f"listing{i.where}", i.message) for i in bad]
    d: Path = ctx["dir"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{listing['version']}.lore.json").write_bytes(ctx["data"])
    if ctx["bundle"].get("series") is not None:
        (d / "series.json").write_text(dumps(ctx["bundle"]["series"]), encoding="utf-8")
    (d / "listing.json").write_text(dumps(listing), encoding="utf-8")
    return listing, issues


def remove(listing_id: str, source: str | Path, reason: str, now: str | None = None) -> None:
    """Delete a listing's directory and append it to removed.json."""
    source = Path(source)
    m = re.fullmatch(r"([A-Za-z0-9-]+)/([a-z0-9-]+)", listing_id or "")
    if not m or not reason or not reason.strip():
        raise RegistryError("give a listing id like publisher/slug and a reason")
    pub, slug = m.group(1), m.group(2)
    d = _listing_dir(source, pub, slug)
    if not (d / "listing.json").is_file():
        raise RegistryError(f"no listing {listing_id}")
    log = _removed(source)
    shutil.rmtree(d)
    parent = d.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    log.append({"id": f"{pub}/{slug}", "removed": now or now_iso(), "reason": reason.strip()})
    (source / "removed.json").write_text(dumps(log), encoding="utf-8")


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
                bad = validate.check_schema(data, "listing")
                if bad:
                    problems += [f"{where}: listing{i.where}: {i.message}" for i in bad]
                    continue
                if data["id"] != where or data["publisher"] != pdir.name or data["slug"] != d.name:
                    problems.append(f"{where}: listing id {data['id']!r} does not match its directory")
                    continue
                for v in data["versions"]:
                    f = d / f"v{v['version']}.lore.json"
                    if not f.is_file():
                        problems.append(f"{where}: missing {f.name}")
                    elif digest_of(f.read_bytes()) != v["digest"]:
                        problems.append(f"{where}: {f.name} does not match its digest")
                if data["versions"][-1]["version"] != data["version"] or data["versions"][-1]["digest"] != data["digest"]:
                    problems.append(f"{where}: the listing's latest version differs from its versions list")
                listings.append((data, d))
    removed_raw = None
    if (source / "removed.json").is_file():
        removed_raw = (source / "removed.json").read_bytes()
        try:
            removed = json.loads(removed_raw)
            bad = validate.check_schema(removed, "api", "removed")
            problems += [f"removed.json{i.where}: {i.message}" for i in bad]
        except ValueError as e:
            problems.append(f"removed.json: not valid JSON: {e}")
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
                        keys.setdefault(f"{folder}/{val.lower() if folder == 'wikidata' else val}", "identifier")
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
