"""Registry formats (SPEC.md §8.7): listing, share bundle, and the protocol additions in api.schema.json."""

import copy
import hashlib
import json

import pytest

from lorekeeper import validate

from .conftest import ROOT

EXAMPLES = ROOT / "examples"
AT = "2026-11-13T20:00:00Z"


def _load(rel):
    return json.loads((EXAMPLES / rel).read_text())


def _digest(doc):
    """How the registry stores an index: written once, hashed as stored."""
    data = (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _scrub(index):
    index = copy.deepcopy(index)
    for ed in index["editions"]:
        ed.pop("label", None)
        ed.pop("fileHash", None)
    return index


def _errors(doc, name, definition=None):
    return [i for i in validate.check_schema(doc, name, definition) if i.level == "error"]


def _builder_attestations(text_hash):
    return [
        {"kind": "book-checks", "by": "builder", "at": AT, "textHash": text_hash,
         "anchors": 412, "resolved": 412, "errors": 0},
        {"kind": "spoiler-gate", "by": "builder", "at": AT, "textHash": text_hash,
         "names": 96, "found": 91, "early": 0},
    ]


@pytest.fixture
def dracula():
    return _load("dracula/dracula.json")


@pytest.fixture
def listing(dracula):
    th = dracula["editions"][0]["textHash"]
    d = _digest(_scrub(dracula))
    href = "/lorekeeper-registry/v0/indexes/mina-m/dracula/v1.lore.json"
    return {
        "lorekeeper": "0.1", "kind": "listing",
        "id": "mina-m/dracula", "publisher": "mina-m", "slug": "dracula",
        "work": {"title": "Dracula", "authors": ["Bram Stoker"], "language": "en"},
        "editions": [{"textHash": th, "ids": {"gutenberg": "345"}}],
        "version": 1, "digest": d, "href": href, "updated": AT,
        "complete": dracula["coverage"]["complete"],
        "license": dracula["provenance"]["license"],
        "generator": dracula["provenance"]["generator"],
        "edited": False,
        "attestations": [{"kind": "validates", "by": "registry", "at": AT}, *_builder_attestations(th)],
        "versions": [{"version": 1, "digest": d, "href": href, "updated": AT}],
    }


@pytest.fixture
def series_listing():
    four = _load("sherlock-holmes/sign-of-four.json")
    d = _digest(_scrub(four))
    href = "/v0/indexes/mina-m/sherlock-holmes-sign-of-four/v2.lore.json"
    return {
        "lorekeeper": "0.1", "kind": "listing",
        "id": "mina-m/sherlock-holmes-sign-of-four", "publisher": "mina-m",
        "slug": "sherlock-holmes-sign-of-four",
        "work": {k: four["work"][k] for k in ("title", "authors", "language")},
        "editions": [{"textHash": ed["textHash"], "ids": four["work"]["ids"]} for ed in four["editions"]],
        "version": 2, "digest": d, "href": href, "updated": AT,
        "complete": four["coverage"]["complete"], "license": "CC0-1.0", "edited": True,
        "series": {"id": "sherlock-holmes", "book": "sign-of-four",
                   "requires": ["mina-m/sherlock-holmes-study-in-scarlet"]},
        "derivedFrom": "sha256:" + "0" * 64,
        "attestations": [{"kind": "validates", "by": "registry", "at": AT}],
        "versions": [{"version": 1, "digest": "sha256:" + "1" * 64,
                      "href": "/v0/indexes/mina-m/sherlock-holmes-sign-of-four/v1.lore.json",
                      "updated": "2026-11-01T09:00:00Z"},
                     {"version": 2, "digest": d, "href": href, "updated": AT}],
    }


@pytest.fixture
def bundle(dracula):
    index = _scrub(dracula)
    return {"lorekeeper": "0.1", "kind": "share", "index": index,
            "attestations": _builder_attestations(index["editions"][0]["textHash"])}


@pytest.fixture
def series_bundle():
    return {"lorekeeper": "0.1", "kind": "share",
            "index": _scrub(_load("sherlock-holmes/sign-of-four.json")),
            "series": _load("sherlock-holmes/series.json"),
            "derivedFrom": "sha256:" + "a" * 64}


# Listings

def test_listing_examples_validate(listing, series_listing):
    assert _errors(listing, "listing") == []
    assert _errors(series_listing, "listing") == []


@pytest.mark.parametrize("field,value", [
    ("fileHash", "4d40635636002720bdf187af3c074d1521629ca3487f07fd812dced6bac2797f"),
    ("label", "pg345.epub"),
    ("builtFrom", True),
])
def test_listing_edition_carries_only_text_hash_and_ids(listing, field, value):
    listing["editions"][0][field] = value
    errs = _errors(listing, "listing")
    assert errs and errs[0].where == "/editions/0"
    assert field in errs[0].message


def test_listing_work_has_no_ids(listing):
    listing["work"]["ids"] = {"gutenberg": "345"}
    assert _errors(listing, "listing")


@pytest.mark.parametrize("kind,missing", [
    ("book-checks", "resolved"),
    ("book-checks", "textHash"),
    ("spoiler-gate", "early"),
    ("spoiler-gate", "found"),
])
def test_attestation_missing_count_rejected(listing, kind, missing):
    att = next(a for a in listing["attestations"] if a["kind"] == kind)
    del att[missing]
    errs = _errors(listing, "listing")
    assert any(missing in e.message for e in errs), errs


def test_audit_attestation(listing):
    th = listing["editions"][0]["textHash"]
    audit = {"kind": "audit", "by": "reader", "at": AT, "textHash": th, "sampled": 50, "supported": 48,
             "model": "claude-haiku-5-5"}
    listing["attestations"].append(audit)
    assert _errors(listing, "listing") == []
    del audit["model"]
    assert any("model" in e.message for e in _errors(listing, "listing"))


@pytest.mark.parametrize("change", [
    {"kind": "rating"},
    {"by": "someone"},
    {"at": None},
    {"anchors": -1},
    {"anchors": 1.5},
    {"note": "extra member"},
])
def test_attestation_shape(listing, change):
    att = listing["attestations"][1]  # book-checks
    for k, v in change.items():
        if v is None:
            del att[k]
        else:
            att[k] = v
    assert _errors(listing, "listing")


def test_validates_attestation_takes_no_counts(listing):
    listing["attestations"][0]["textHash"] = listing["editions"][0]["textHash"]
    assert _errors(listing, "listing")


@pytest.mark.parametrize("publisher,ok", [
    ("mina-m", True), ("a", True), ("x" * 39, True), ("m1na", True),
    ("Mina-M", False), ("-mina", False), ("mina-", False), ("x" * 40, False),
    ("mina_m", False), ("../etc", False), ("", False),
])
def test_publisher_pattern(listing, publisher, ok):
    listing["publisher"] = publisher
    listing["id"] = f"{publisher}/dracula"
    assert (_errors(listing, "listing") == []) is ok


@pytest.mark.parametrize("slug", ["sherlock-holmes.study-in-scarlet", "Dracula", "", "a/b", ".."])
def test_slug_pattern(listing, slug):
    listing["slug"] = slug
    assert _errors(listing, "listing")


def test_listing_requires_versions_and_attestations(listing):
    for field in ("versions", "attestations", "edited", "digest"):
        broken = copy.deepcopy(listing)
        del broken[field]
        assert _errors(broken, "listing"), field
    listing["versions"] = []
    assert _errors(listing, "listing")


def test_series_requires_listing_ids(series_listing):
    series_listing["series"]["requires"] = ["sha256:" + "1" * 64]
    assert _errors(series_listing, "listing")
    del series_listing["series"]["requires"]
    assert _errors(series_listing, "listing")


# Share bundles

def test_bundle_examples_validate(bundle, series_bundle):
    assert _errors(bundle, "share") == []
    assert _errors(series_bundle, "share") == []
    bundle.pop("attestations")
    assert _errors(bundle, "share") == []


def test_bundle_without_index_rejected(bundle):
    del bundle["index"]
    assert any("'index' is a required property" in e.message for e in _errors(bundle, "share"))


def test_bundle_index_must_be_valid(bundle):
    del bundle["index"]["anchors"]
    assert _errors(bundle, "share")


@pytest.mark.parametrize("field", ["fileHash", "label"])
def test_bundle_index_must_be_scrubbed(bundle, field):
    bundle["index"]["editions"][0][field] = "pg345.epub" if field == "label" else "0" * 64
    errs = _errors(bundle, "share")
    assert errs and errs[0].where == "/index/editions/0"


def test_bundle_keeps_built_from(bundle):
    assert bundle["index"]["editions"][0]["builtFrom"] is True
    assert _errors(bundle, "share") == []


def test_series_book_needs_manifest(series_bundle):
    del series_bundle["series"]
    assert any("'series' is a required property" in e.message for e in _errors(series_bundle, "share"))


@pytest.mark.parametrize("by", ["registry", "reader"])
def test_bundle_attestations_are_builder_only(bundle, by):
    bundle["attestations"][0]["by"] = by
    errs = _errors(bundle, "share")
    assert errs and errs[0].where == "/attestations/0/by"


def test_bundle_rejects_unknown_members(bundle):
    bundle["listing"] = {}
    assert _errors(bundle, "share")


# Protocol additions

def test_static_discovery():
    doc = {"lorekeeper": "0.1", "name": "Lorekeeper community registry", "api": "/lorekeeper-registry/v0",
           "capabilities": ["lookup", "index", "series", "listings"], "auth": {"type": "none"}, "static": True}
    assert _errors(doc, "api", "discovery") == []
    doc["static"] = "yes"
    assert _errors(doc, "api", "discovery")


def test_lookup_result_carries_listing(listing):
    result = {"id": listing["id"], "title": "Dracula", "match": "text", "complete": listing["complete"],
              "updated": listing["updated"], "digest": listing["digest"], "href": listing["href"],
              "listing": listing}
    assert _errors({"results": [result]}, "api", "lookup") == []
    listing["editions"][0]["label"] = "pg345.epub"
    assert _errors({"results": [result]}, "api", "lookup")


def test_removed_log():
    doc = [{"id": "mina-m/dracula", "removed": AT, "reason": "Takedown notice from the rights holder"}]
    assert _errors(doc, "api", "removed") == []
    doc[0]["id"] = "dracula"
    assert _errors(doc, "api", "removed")


def test_every_schema_ref_resolves():
    """Every cross-file $ref in schema/ points at a schema that exists."""
    ids = {json.loads(p.read_text())["$id"].rsplit("/", 1)[1] for p in (ROOT / "schema").glob("*.schema.json")}

    def refs(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                yield node["$ref"]
            for v in node.values():
                yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)

    for p in (ROOT / "schema").glob("*.schema.json"):
        for ref in refs(json.loads(p.read_text())):
            target = ref.split("#", 1)[0]
            assert not target or target in ids, (p.name, ref)
