from lorekeeper import epub, validate

from .conftest import corpus_epub


def _errors(issues):
    return [str(i) for i in issues if i.level == "error"]


def _facet(index, fid):
    return next(f for e in index["entities"] for f in e["facets"] if f["id"] == fid)


def test_examples_are_valid(scarlet, four, series):
    assert _errors(validate.check_index(scarlet)) == []
    assert _errors(validate.check_series(series, [scarlet, four])) == []


def test_examples_valid_against_their_books(scarlet, four):
    assert _errors(validate.check_against_book(scarlet, epub.blocks(corpus_epub("pg244/pg244.epub")))) == []
    assert _errors(validate.check_against_book(four, epub.blocks(corpus_epub("pg2097/pg2097.epub")))) == []


def test_evidence_after_reveal(scarlet):
    _facet(scarlet, "sis.holmes.1")["evidence"].append("ch2.para50")
    assert any("comes after the reveal point" in e for e in _errors(validate.check_index(scarlet)))


def test_unknown_anchor(scarlet):
    _facet(scarlet, "sis.holmes.1")["from"] = "ch9.para99"
    assert any("unknown anchor" in e for e in _errors(validate.check_index(scarlet)))


def test_replaces_must_point_backwards(scarlet):
    _facet(scarlet, "sis.holmes.1")["replaces"] = ["sis.holmes.14"]
    assert any("revealed later" in e for e in _errors(validate.check_index(scarlet)))


def test_duplicate_item_ids(scarlet):
    _facet(scarlet, "sis.holmes.2")["id"] = "sis.holmes.1"
    assert any("duplicate item id" in e for e in _errors(validate.check_index(scarlet)))


def test_reveal_past_coverage(scarlet):
    scarlet["coverage"]["through"] = "ch2.para01"
    assert any("after coverage.through" in e for e in _errors(validate.check_index(scarlet)))


def test_relation_before_entity(scarlet, four, series):
    scarlet["relations"][0]["from"] = "pt1.note"  # Murray not yet introduced
    assert any("before it is revealed" in e for e in _errors(validate.check_series(series, [scarlet, four])))
    del scarlet["series"]  # standalone: no earlier book could have introduced him
    assert any("before it is revealed" in e for e in _errors(validate.check_index(scarlet)))


def test_cross_book_reference_needs_prior_book(scarlet, four, series):
    # Validated alone, book 2's reference into book 1 is only a warning...
    assert _errors(validate.check_index(four)) == []
    # ...but with book 1 present, a bad reference is an error.
    four["relations"][0]["replaces"] = ["sis.rel.does-not-exist"]
    assert _errors(validate.check_series(series, [scarlet, four]))


def test_verbatim_quote_rejected(scarlet):
    _facet(scarlet, "sis.bakerst.1")["text"] = "I have my eye on a suite in Baker Street, he said."
    issues = validate.check_against_book(scarlet, epub.blocks(corpus_epub("pg244/pg244.epub")))
    assert any("consecutive words" in e for e in _errors(issues))


def test_schema_errors_short_circuit(scarlet):
    del scarlet["anchors"]
    assert _errors(validate.check_index(scarlet))


def test_book_kind_known_unknown_and_empty(scarlet):
    assert not _errors(validate.check_index(scarlet))
    scarlet["bookKind"] = "self-help"
    assert not _errors(validate.check_index(scarlet))
    scarlet["bookKind"] = "cookbook"
    assert not _errors(validate.check_index(scarlet))
    scarlet["bookKind"] = ""
    assert _errors(validate.check_index(scarlet))


def test_book_kind_on_series_manifest(scarlet, four, series):
    series["bookKind"] = "fiction"
    assert not _errors(validate.check_series(series, [scarlet, four]))
    series["bookKind"] = ""
    assert _errors(validate.check_series(series, [scarlet, four]))


def test_book_kind_in_view_and_lookup_schemas():
    view = {"lorekeeper": "0.1", "kind": "view", "bookKind": "non-fiction",
            "position": {"book": None, "anchor": None}, "coverage": {"complete": True, "through": "a1", "exhausted": False},
            "entities": [], "relations": [], "relationHistory": [], "events": [], "recap": None, "chapters": []}
    assert not _errors(validate.check_schema(view, "view"))
    view["bookKind"] = ""
    assert _errors(validate.check_schema(view, "view"))
    row = {"id": "a.b", "title": "T", "bookKind": "self-help", "match": "text", "complete": True,
           "digest": "sha256:" + "0" * 64, "href": "/v0/indexes/a.b"}
    lookup = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "$defs": validate.schema("api")["$defs"], "$ref": "#/$defs/lookup"}
    from jsonschema import Draft202012Validator as V
    assert not list(V(lookup).iter_errors({"results": [row]}))
    row["bookKind"] = ""
    assert list(V(lookup).iter_errors({"results": [row]}))
