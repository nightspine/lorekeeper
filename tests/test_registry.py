"""Registry tooling (SPEC.md §8.7): share, registry check/add/remove/build."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from lorekeeper import cli, epub, registry, validate

from .conftest import CORPUS, ROOT

EX = ROOT / "examples"
NOW = "2026-11-13T20:00:00Z"
LATER = "2026-11-14T08:00:00Z"
DRACULA_HASH = "6211b79150ee12207b34736872d807d9074170062dbc03fee71075ef8a38f00d"


def load(rel):
    return json.loads((EX / rel).read_text())


def write(path, doc):
    Path(path).write_text(registry.dumps(doc), encoding="utf-8")
    return str(path)


def make(index, **kw):
    bundle, rep = registry.make_bundle(index, now=NOW, **kw)
    assert bundle is not None, [str(i) for i in rep["issues"]]
    return bundle


def errs(issues):
    return [str(i) for i in issues if i.level == "error"]


@pytest.fixture
def src(tmp_path):
    return tmp_path / "reg"


@pytest.fixture
def dracula():
    return load("dracula/dracula.json")


def add(tmp_path, src, bundle, publisher="someone", name="b.json", now=NOW):
    listing, issues = registry.add(write(tmp_path / name, bundle), publisher, src, now)
    assert listing is not None, errs(issues)
    return listing


# ---- share ----------------------------------------------------------------------------------

def test_share_scrubs_editions_and_validates(dracula):
    assert "label" in dracula["editions"][0]
    bundle = make(dracula)
    ed = bundle["index"]["editions"][0]
    assert "label" not in ed and "fileHash" not in ed and ed["builtFrom"] is True
    assert validate.check_schema(bundle, "share") == []
    assert "label" in dracula["editions"][0]  # the input is untouched


@pytest.mark.parametrize("license", ["MIT", None, "CC-BY-SA-4.0"])
def test_share_refuses_other_licences(dracula, license):
    if license is None:
        del dracula["provenance"]["license"]
    else:
        dracula["provenance"]["license"] = license
    bundle, rep = registry.make_bundle(dracula)
    assert bundle is None and any("licence" in str(i) or "license" in str(i) for i in rep["issues"])


def test_share_accepts_cc_by(dracula):
    dracula["provenance"]["license"] = "CC-BY-4.0"
    assert make(dracula)["index"]["provenance"]["license"] == "CC-BY-4.0"


def test_share_refuses_invalid_index(dracula):
    del dracula["anchors"]
    bundle, rep = registry.make_bundle(dracula)
    assert bundle is None and errs(rep["issues"])


def test_share_refuses_index_with_check_errors(dracula):
    dracula["entities"][0]["facets"][0]["from"] = "nope"
    bundle, rep = registry.make_bundle(dracula)
    assert bundle is None and errs(rep["issues"])


def test_share_series_book_needs_manifest(four):
    bundle, rep = registry.make_bundle(four)
    assert bundle is None and any("series" in str(i) for i in rep["issues"])


def test_share_series_book_with_manifest(four, series):
    bundle = make(four, series=series, derived_from="sha256:" + "a" * 64)
    assert bundle["series"]["id"] == "sherlock-holmes" and bundle["derivedFrom"].startswith("sha256:")


def test_share_derived_from_must_be_a_digest(dracula):
    bundle, _ = registry.make_bundle(dracula, derived_from="abc")
    assert bundle is None


def test_share_epub_must_match_an_edition(dracula):
    other = [b for b in [epub.Block(0, 0, ["one", "two"], "one two")]]
    bundle, rep = registry.make_bundle(dracula, blocks=other)
    assert bundle is None and any("text hash" in str(i) for i in rep["issues"])


@pytest.mark.skipif(not (CORPUS / "pg345/pg345.epub").exists(), reason="corpus not downloaded")
def test_share_with_epub_adds_attestations(dracula):
    blocks = epub.blocks(CORPUS / "pg345/pg345.epub")
    bundle = make(dracula, blocks=blocks)
    kinds = {a["kind"]: a for a in bundle["attestations"]}
    assert kinds["book-checks"]["errors"] == 0
    assert kinds["book-checks"]["anchors"] == kinds["book-checks"]["resolved"] == len(dracula["anchors"])
    assert kinds["spoiler-gate"]["found"] <= kinds["spoiler-gate"]["names"]
    assert all(a["by"] == "builder" and a["textHash"] == DRACULA_HASH for a in bundle["attestations"])
    assert "examples" not in kinds["spoiler-gate"]
    assert validate.check_schema(bundle, "share") == []


@pytest.mark.skipif(not (CORPUS / "pg345/pg345.epub").exists(), reason="corpus not downloaded")
def test_share_with_epub_refuses_book_check_errors(dracula):
    dracula["anchors"][3]["fp"] = "0" * 16
    bundle, rep = registry.make_bundle(dracula, blocks=epub.blocks(CORPUS / "pg345/pg345.epub"))
    assert bundle is None and errs(rep["issues"])


def _blocks(*texts):
    from lorekeeper import text
    return [epub.Block(i, 0, text.words(t), t) for i, t in enumerate(texts)]


def test_spoiler_gate_counts_early_names():
    from lorekeeper import text
    blocks = _blocks("The quick brown fox jumped over the lazy dog today", "Then Mr Holmes said a few words aloud here",
                     "Later the fox returned to the lazy dog again today")
    fp = [text.fingerprint(b.words) for b in blocks]
    index = {"anchors": [{"id": "a0", "fp": fp[0]}, {"id": "a1", "fp": fp[1]}, {"id": "a2", "fp": fp[2]}],
             "entities": [
                 {"id": "fox", "names": [{"value": "Fox", "from": "a0"}, {"value": "Quick Brown Fox", "from": "a0"}]},
                 {"id": "holmes", "names": [{"value": "Mr Holmes", "from": "a0"}, {"value": "Moriarty", "from": "a0"}]}]}
    gate = registry.spoiler_gate(index, blocks)
    assert (gate["names"], gate["found"], gate["early"]) == (4, 3, 1)
    assert gate["examples"] == [{"entity": "holmes", "name": "Mr Holmes"}]


# ---- check / add ----------------------------------------------------------------------------

def test_add_dracula_writes_source_tree(tmp_path, src, dracula):
    bundle = make(dracula)
    listing = add(tmp_path, src, bundle)
    d = src / "indexes/someone/dracula"
    assert listing["id"] == "someone/dracula" and listing["version"] == 1
    data = (d / "v1.lore.json").read_bytes()
    assert listing["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert json.loads(data) == bundle["index"]
    assert listing["href"] == "/v0/indexes/someone/dracula/v1.lore.json"
    assert json.loads((d / "listing.json").read_text()) == listing
    assert validate.check_schema(listing, "listing") == []
    assert listing["editions"] == [{"textHash": DRACULA_HASH}]
    assert listing["edited"] is False and listing["complete"] is True and listing["license"] == "CC0-1.0"
    assert listing["attestations"] == [{"kind": "validates", "by": "registry", "at": NOW}]
    assert not (d / "series.json").exists()


def test_publisher_is_lowercased(tmp_path, src, dracula):
    assert add(tmp_path, src, make(dracula), publisher="Mina-M")["id"] == "mina-m/dracula"


def test_listing_keeps_bundle_attestations_and_edited(tmp_path, src, dracula):
    dracula["provenance"]["contributors"].append({"name": "Mina", "role": "editor"})
    bundle = make(dracula)
    bundle["attestations"] = [{"kind": "spoiler-gate", "by": "builder", "at": NOW, "textHash": DRACULA_HASH,
                               "names": 10, "found": 9, "early": 1}]
    listing = add(tmp_path, src, bundle)
    assert listing["edited"] is True
    assert [a["kind"] for a in listing["attestations"]] == ["validates", "spoiler-gate"]


def test_listing_merges_work_ids_into_editions(tmp_path, src, scarlet, series):
    listing = add(tmp_path, src, make(scarlet, series=series))
    assert listing["editions"][0]["ids"] == {"gutenberg": "244"}
    assert all(set(e) <= {"textHash", "ids"} for e in listing["editions"])
    assert "ids" not in listing["work"]


def test_series_pair_requires_order_and_requires_field(tmp_path, src, scarlet, four, series):
    b4 = make(four, series=series)
    path = write(tmp_path / "four.json", b4)
    ctx, issues = registry.check(path, "someone", src)
    assert ctx is None and any("study-in-scarlet" in i for i in errs(issues))
    first = add(tmp_path, src, make(scarlet, series=series), name="one.json")
    assert first["series"] == {"id": "sherlock-holmes", "book": "study-in-scarlet", "requires": []}
    second = add(tmp_path, src, b4, name="two.json")
    assert second["id"] == "someone/sherlock-holmes-sign-of-four"
    assert second["series"]["requires"] == ["someone/sherlock-holmes-study-in-scarlet"]
    assert (src / "indexes/someone/sherlock-holmes-sign-of-four/series.json").is_file()
    # another publisher has not shared the first book
    ctx, issues = registry.check(path, "other", src)
    assert ctx is None and errs(issues)


def test_second_version_bumps_and_keeps_versions(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula))
    dracula["provenance"]["updated"] = "2026-11-20T00:00:00Z"
    dracula["entities"][0]["facets"][0]["text"] += " Revised."
    listing = add(tmp_path, src, make(dracula), now=LATER, name="b2.json")
    d = src / "indexes/someone/dracula"
    assert listing["version"] == 2 and listing["href"].endswith("/v2.lore.json")
    assert [v["version"] for v in listing["versions"]] == [1, 2]
    assert listing["versions"][0]["updated"] == NOW and listing["versions"][1]["updated"] == LATER
    assert listing["attestations"] == [{"kind": "validates", "by": "registry", "at": LATER}]
    assert (d / "v1.lore.json").exists() and (d / "v2.lore.json").exists()


def check_error(tmp_path, src, bundle, needle, publisher="someone"):
    ctx, issues = registry.check(write(tmp_path / "c.json", bundle), publisher, src)
    assert ctx is None
    assert any(needle in i for i in errs(issues)), errs(issues)


def test_check_writes_nothing(tmp_path, src, dracula):
    ctx, issues = registry.check(write(tmp_path / "b.json", make(dracula)), "someone", src)
    assert ctx is not None and not errs(issues) and not src.exists()


def test_check_schema_failure(tmp_path, src, dracula):
    bundle = make(dracula)
    del bundle["index"]
    check_error(tmp_path, src, bundle, "index")


def test_check_index_failure(tmp_path, src, dracula):
    bundle = make(dracula)
    bundle["index"]["entities"][0]["facets"][0]["replaces"] = ["no.such.item"]
    check_error(tmp_path, src, bundle, "replaces")


def test_check_licence(tmp_path, src, dracula):
    bundle = make(dracula)
    bundle["index"]["provenance"]["license"] = "MIT"
    check_error(tmp_path, src, bundle, "licence")


def test_check_size_cap_before_parsing(tmp_path, src):
    p = tmp_path / "big.json"
    p.write_bytes(b"{" + b" " * registry.MAX_BUNDLE_BYTES + b"}")
    ctx, issues = registry.check(p, "someone", src)
    assert ctx is None and any("limit" in i for i in errs(issues))


def test_check_unscrubbed_edition(tmp_path, src, dracula):
    bundle = make(dracula)
    bundle["index"]["editions"][0]["label"] = "pg345.epub"
    check_error(tmp_path, src, bundle, "label")
    bundle["index"]["editions"][0] = dracula["editions"][0]
    check_error(tmp_path, src, bundle, "fileHash")


@pytest.mark.parametrize("by", ["registry", "reader"])
def test_check_attestation_not_by_builder(tmp_path, src, dracula, by):
    bundle = make(dracula)
    bundle["attestations"] = [{"kind": "validates", "by": by, "at": NOW}]
    check_error(tmp_path, src, bundle, "builder")


def test_check_already_shared(tmp_path, src, dracula):
    bundle = make(dracula)
    add(tmp_path, src, bundle)
    check_error(tmp_path, src, bundle, "already shared")


def test_check_removed_listing_refused(tmp_path, src, dracula):
    bundle = make(dracula)
    add(tmp_path, src, bundle)
    registry.remove("someone/dracula", src, "takedown request", NOW)
    assert not (src / "indexes/someone").exists()
    log = json.loads((src / "removed.json").read_text())
    assert log == [{"id": "someone/dracula", "removed": NOW, "reason": "takedown request"}]
    check_error(tmp_path, src, bundle, "removed")


@pytest.mark.parametrize("publisher", ["", "-bad", "bad-", "a/b", "../etc", "a" * 40, "sp ace", ".."])
def test_check_invalid_publisher(tmp_path, src, dracula, publisher):
    check_error(tmp_path, src, make(dracula), "publisher", publisher=publisher)
    assert not src.exists()


@pytest.mark.parametrize("index_id", ["../../evil", "a/b/c", "/abs", "..", "x\\y", "***", ""])
def test_path_shaped_index_id_is_refused_and_writes_nothing(tmp_path, src, dracula, index_id):
    bundle = make(dracula)
    bundle["index"]["id"] = index_id  # a hand-made bundle skips `share`
    check_error(tmp_path, src, bundle, "id")
    assert not src.exists() and {p.name for p in tmp_path.iterdir()} == {"c.json"}


def test_slug_replaces_dots_and_paths_stay_inside_indexes(tmp_path, src, dracula):
    dracula["id"] = "my.book_x"
    listing = add(tmp_path, src, make(dracula))
    assert listing["slug"] == "my-book-x"
    assert registry.slugify("Sherlock-Holmes.Study") == "sherlock-holmes-study"
    for bad in ("", "..", "a/b", "A", "x" * 129):
        with pytest.raises(registry.RegistryError):
            registry._listing_dir(src, "someone", bad)
    for bad in ("../x", "A", ""):
        with pytest.raises(registry.RegistryError):
            registry._listing_dir(src, bad, "ok")


def test_remove_unknown_and_malformed(src):
    with pytest.raises(registry.RegistryError):
        registry.remove("someone/nothing", src, "x")
    with pytest.raises(registry.RegistryError):
        registry.remove("../x/y", src, "x")
    with pytest.raises(registry.RegistryError):
        registry.remove("a/b", src, " ")


# ---- build ----------------------------------------------------------------------------------

def sha(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def test_build_dracula(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula), publisher="someone")
    out = tmp_path / "site"
    registry.build(src, out, "/lorekeeper-registry", "Lorekeeper community registry")
    disc = json.loads((out / ".well-known/lorekeeper").read_text())
    assert validate.check_schema(disc, "api", "discovery") == []
    assert disc["api"] == "/lorekeeper-registry/v0" and disc["static"] is True and disc["auth"] == {"type": "none"}
    assert disc["capabilities"] == ["lookup", "index", "series", "listings"]
    assert (out / ".nojekyll").read_bytes() == b""
    f = out / "v0/lookup/text" / f"{DRACULA_HASH}.json"
    body = json.loads(f.read_text())
    assert validate.check_schema(body, "api", "lookup") == []
    (r,) = body["results"]
    assert r["listing"]["id"] == "someone/dracula" and r["match"] == "text" and r["id"] == "someone/dracula"
    assert r["href"] == "/lorekeeper-registry/v0/indexes/someone/dracula/v1.lore.json"
    served = out / r["href"].removeprefix("/lorekeeper-registry/")
    assert sha(served) == r["digest"]
    assert served.read_bytes() == (src / "indexes/someone/dracula/v1.lore.json").read_bytes()
    assert json.loads((out / "v0/listings/someone/dracula.json").read_text())["href"] == r["href"]
    assert json.loads((out / "v0/removed.json").read_text()) == []
    for rec in json.loads((out / "v0/listings/someone/dracula.json").read_text())["editions"]:
        assert "fileHash" not in rec and "label" not in rec
    assert not list(out.glob("v0/lookup/fileHash*"))


def test_build_is_deterministic_and_clears_v0(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula))
    out = tmp_path / "site"
    (out / "v0").mkdir(parents=True)
    (out / "v0/stale.json").write_text("{}")
    registry.build(src, out, "/base")
    first = tree(out)
    assert "v0/stale.json" not in first
    registry.build(src, out, "/base")
    assert tree(out) == first


def test_build_series_and_identifier_lookups(tmp_path, src, scarlet, four, series):
    add(tmp_path, src, make(scarlet, series=series), name="1.json")
    add(tmp_path, src, make(four, series=series), name="2.json")
    out = tmp_path / "site"
    registry.build(src, out)
    g = json.loads((out / "v0/lookup/gutenberg/2097.json").read_text())
    assert validate.check_schema(g, "api", "lookup") == []
    (r,) = g["results"]
    assert r["match"] == "identifier" and r["series"] == {"id": "sherlock-holmes", "book": "sign-of-four"}
    assert r["listing"]["series"]["requires"] == ["someone/sherlock-holmes-study-in-scarlet"]
    assert (out / "v0/series/someone/sherlock-holmes-sign-of-four.json").read_bytes() == \
        (src / "indexes/someone/sherlock-holmes-sign-of-four/series.json").read_bytes()
    assert (out / "v0/lookup/text" / f"{four['editions'][0]['textHash']}.json").is_file()


def test_build_orders_results(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula), publisher="aaa")
    add(tmp_path, src, make(dracula), publisher="bbb", now=LATER)
    partial = copy.deepcopy(dracula)
    partial["coverage"]["complete"] = False
    add(tmp_path, src, make(partial), publisher="ccc", now="2026-12-01T00:00:00Z")
    out = tmp_path / "site"
    registry.build(src, out)
    ids = [r["id"] for r in json.loads((out / "v0/lookup/text" / f"{DRACULA_HASH}.json").read_text())["results"]]
    assert ids == ["bbb/dracula", "aaa/dracula", "ccc/dracula"]


def test_build_fails_on_digest_mismatch_and_writes_nothing(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula))
    (src / "indexes/someone/dracula/v1.lore.json").write_text("{}\n")
    out = tmp_path / "site"
    with pytest.raises(registry.RegistryError, match="digest"):
        registry.build(src, out)
    assert not out.exists()


def test_build_rejects_bad_listing(tmp_path, src, dracula):
    add(tmp_path, src, make(dracula))
    p = src / "indexes/someone/dracula/listing.json"
    doc = json.loads(p.read_text())
    doc["editions"][0]["fileHash"] = "0" * 64
    p.write_text(json.dumps(doc))
    with pytest.raises(registry.RegistryError):
        registry.build(src, tmp_path / "site")


def test_build_empty_source(tmp_path, src):
    src.mkdir()
    out = tmp_path / "site"
    registry.build(src, out)
    assert json.loads((out / "v0/removed.json").read_text()) == []


def test_build_skips_unsafe_identifier_values(tmp_path, src, dracula):
    dracula["editions"][0]["ids"] = {"gutenberg": "../../x"}
    bundle = make(dracula)
    ctx, issues = registry.check(write(tmp_path / "b.json", bundle), "someone", src)
    assert any(i.level == "warning" for i in issues)
    add(tmp_path, src, bundle)
    out = tmp_path / "site"
    registry.build(src, out)
    assert not (tmp_path / "x.json").exists()
    assert {p.name for p in (out / "v0/lookup").iterdir()} == {"text"}


# ---- CLI ------------------------------------------------------------------------------------

def run(*argv):
    return cli.main([str(a) for a in argv])


def test_cli_flow(tmp_path, capsys):
    b, s, o = tmp_path / "b.json", tmp_path / "reg", tmp_path / "site"
    assert run("share", EX / "dracula/dracula.json", "-o", b, "--json", "--now", NOW) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert run("registry", "check", b, "--publisher", "someone", "--source", s, "--json") == 0
    assert run("registry", "add", b, "--publisher", "someone", "--source", s, "--now", NOW, "--json") == 0
    capsys.readouterr()
    assert run("registry", "add", b, "--publisher", "someone", "--source", s, "--json") == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and "already shared" in body["errors"][0]["message"]
    assert run("registry", "build", s, "-o", o, "--base-path", "/lorekeeper-registry", "--json") == 0
    assert (o / "v0/lookup/text" / f"{DRACULA_HASH}.json").is_file()
    assert run("registry", "remove", "someone/dracula", "--source", s, "--reason", "test") == 0
    assert run("registry", "add", b, "--publisher", "someone", "--source", s) == 1


def test_cli_share_refuses_bad_licence(tmp_path, capsys, dracula):
    dracula["provenance"]["license"] = "MIT"
    p = write(tmp_path / "i.json", dracula)
    assert run("share", p, "-o", tmp_path / "b.json") == 1
    assert not (tmp_path / "b.json").exists()
