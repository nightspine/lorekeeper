"""Locators (SPEC §3.7): every form and rule, on a tiny EPUB, plus the conformance locator cases."""

import json
import zipfile

import pytest

from lorekeeper import cli, conformance, epub, locators, text
from lorekeeper.locators import LocatorError, locate

from .conftest import ROOT

W10 = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"      # ten words


def page(body: str, head: str = "<title>t</title>") -> str:
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head>{head}</head><body>{body}</body></html>'


DOCS = {
    # itemref 0 · spine 0 · no blocks at all
    "cover": page('<div id="cv"><img id="img" src="c.jpg"/></div>'),
    # itemref 1 · spine 1 · seq 0..3
    "d1": page(f'<div id="d1">\n<h1 id="h">Chapter One</h1>\n<p id="a">{W10}</p>\n<hr id="rule"/>\n'
               f'<p id="b">{W10} <em id="em">emph</em> tail</p>\n<p id="c">{W10}</p>\n</div>'),
    # itemref 2 · linear="no": not part of the text model
    "notes": page(f"<p>{W10}</p>"),
    # itemref 3 · spine 2 · seq 4..6 · a block inside a block
    "d2": page('<ul id="u"><li id="l">one two <p id="ip">inner words</p> tail words</li><li id="l2">second item</li></ul>'),
    # itemref 4 · spine 3 · seq 7..9 · no <p>: the div-block fallback of §3.1
    "d3": page(f'<div class="wrap"><div id="x1">{W10}</div><div id="x2">{W10}</div><div id="x3">{W10}</div></div>'),
    # itemref 5 · spine 4 · seq 10..11 · not well-formed XML (an HTML entity), so the DOM can't be read
    "d4": page('<p id="e1">alpha&nbsp;bravo</p><p id="e2">charlie</p>'),
}
ORDER = ["cover", "d1", "notes", "d2", "d3", "d4"]


@pytest.fixture
def book(tmp_path):
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata><manifest>'
           + "".join(f'<item id="{i}" href="{i}.xhtml" media-type="application/xhtml+xml"/>' for i in ORDER)
           + "</manifest><spine>"
           + "".join('<itemref idref="%s"%s/>' % (i, ' linear="no"' if i == "notes" else "") for i in ORDER)
           + "</spine></package>")
    path = tmp_path / "t.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        for name, src in DOCS.items():
            z.writestr(f"OEBPS/{name}.xhtml", src)
    return epub.read(path)


def seq(book, loc):
    return locate(book, loc).seq


def cfi(step, inner):
    return {"cfi": f"epubcfi(/6/{step}!{inner})"}


def test_fixture_layout(book):
    assert [(d.idref, d.spine, d.first, d.spine_child_index) for d in book.docs] == [
        ("cover", 0, 0, 0), ("d1", 1, 0, 1), ("d2", 2, 4, 3), ("d3", 3, 7, 4), ("d4", 4, 10, 5)]
    assert len(book.blocks) == 12


# ---- block -------------------------------------------------------------------------------------

def test_block_locator(book):
    assert locate(book, {"block": 5}) == locators.Located(5, "block", True)
    assert locate(book, {"block": -1}) == locators.Located(-1, "block", True)


@pytest.mark.parametrize("bad", [12, -2, "3", 1.5, True, None])
def test_block_out_of_range_or_wrong_type(book, bad):
    with pytest.raises(LocatorError):
        locate(book, {"block": bad})


# ---- CFI ---------------------------------------------------------------------------------------

def test_cfi_text_node_and_element(book):
    # d1 is itemref 1 → /6/4; html > body (/4) > div (/2) > third child p#b (/8)
    r = locate(book, cfi(4, "/4/2/8/1:3"))
    assert r == locators.Located(2, "cfi", True)
    assert seq(book, cfi(4, "/4/2/8")) == 2            # the element itself
    assert seq(book, cfi(4, "/4/2/8/1:9999")) == 2     # a character offset never moves the block
    assert seq(book, cfi(4, "/4/2/10/1:0")) == 3


def test_cfi_inline_element_and_its_tail_belong_to_the_block(book):
    # <em> inside p#b, and the text after it (tail of em = gap 1 of p#b)
    assert seq(book, cfi(4, "/4/2/8/2/1:2")) == 2
    assert seq(book, cfi(4, "/4/2/8/3:1")) == 2


def test_cfi_spine_step_counts_linear_no_items(book):
    # d2 is the 4th itemref (index 3) because the non-linear notes item counts → /6/8
    assert seq(book, cfi(8, "/4/2/2/1:0")) == 4
    assert seq(book, cfi(10, "/4/2/4")) == 8           # d3, itemref 4: second div


def test_cfi_into_a_linear_no_item_is_an_error(book):
    with pytest.raises(LocatorError):
        locate(book, cfi(6, "/4/2/2"))


def test_cfi_id_assertion_names_the_element_when_the_path_is_stale(book):
    r = locate(book, cfi(4, "/4/2/98[c]/1:0"))
    assert r == locators.Located(3, "cfi", True)


def test_cfi_id_assertion_wins_over_a_disagreeing_path(book):
    # the path says p#a but the asserted id on the last element step says p#c
    assert seq(book, cfi(4, "/4/2/4[c]/1:0")) == 3
    # consistent assertions on every step change nothing
    assert seq(book, cfi(4, "/4[body]/2[d1]/4[a]/1:5")) == 1


def test_cfi_id_with_parameters_and_escapes(book):
    assert seq(book, cfi(4, "/4/2/98[c;s=b]/1:0")) == 3
    assert seq(book, cfi(4, "/4/2/4[a]/1:0[hel^]lo]")) == 1


def test_cfi_unknown_id_on_a_stale_path_falls_back(book):
    r = locate(book, cfi(4, "/4/2/98[nope]/1:0"))
    assert r == locators.Located(-1, "cfi", False)     # the block before d1's first (the edition's start)


def test_cfi_between_blocks_maps_to_the_preceding_block(book):
    # the whitespace after p#a (gap 2 of the div) lies between blocks 1 and 2
    assert seq(book, cfi(4, "/4/2/5:0")) == 1
    # the <hr/> sits between blocks 1 and 2 as well
    assert seq(book, cfi(4, "/4/2/6")) == 1
    # after the last block of d1, still d1's last
    assert seq(book, cfi(4, "/4/2/11:0")) == 3


def test_cfi_before_the_first_block_of_a_document(book):
    # d2's <title> text: before d2's first block → the last block of the previous document
    assert seq(book, cfi(8, "/2/2/1:0")) == 3
    # d3's <html><head> element
    assert seq(book, cfi(10, "/2")) == 6


def test_cfi_before_the_first_block_of_the_edition(book):
    assert locate(book, cfi(2, "/4/2/2")) == locators.Located(-1, "cfi", True)     # the cover image
    assert seq(book, cfi(4, "/2/2/1:0")) == -1                                       # d1's <title>


def test_cfi_to_a_wrapper_element_is_its_first_block(book):
    assert seq(book, cfi(4, "/4/2")) == 0              # div#d1 → the heading
    assert seq(book, cfi(4, "/4")) == 0                # body
    assert seq(book, cfi(10, "/4/2")) == 7             # div.wrap in the fallback document


def test_cfi_with_no_path_is_the_start_of_the_document(book):
    assert seq(book, {"cfi": "epubcfi(/6/8)"}) == 4
    assert seq(book, {"cfi": "epubcfi(/6/8!)"}) == 4


def test_cfi_nested_blocks(book):
    # li#l (block 4) holds p#ip (block 5); text of li on either side of the p belongs to the li
    assert seq(book, cfi(8, "/4/2/2")) == 4
    assert seq(book, cfi(8, "/4/2/2/1:2")) == 4
    assert seq(book, cfi(8, "/4/2/2/2/1:0")) == 5
    assert seq(book, cfi(8, "/4/2/2/3:1")) == 4
    assert seq(book, cfi(8, "/4/2/4/1:0")) == 6


def test_cfi_in_a_div_fallback_document(book):
    assert seq(book, cfi(10, "/4/2/6/1:0")) == 9


def test_cfi_path_that_runs_out_is_approximate(book):
    for inner in ("/4/2/900/2/1:0", "/4/2/8/50", "/4/2/8/9:0", "/6"):
        r = locate(book, cfi(8, inner))
        assert r == locators.Located(3, "cfi", False), inner     # the block before d2


def test_cfi_text_step_must_be_last(book):
    assert locate(book, cfi(4, "/4/2/1/2")).exact is False


def test_cfi_range_starts_at_its_start(book):
    r = locate(book, {"cfi": "epubcfi(/6/4!/4/2,/8/1:0,/10/1:4)"})
    assert r.seq == 2


def test_cfi_ignores_temporal_and_spatial_parts(book):
    assert seq(book, cfi(4, "/4/2/8/1:3~12.5@10:20")) == 2


def test_cfi_indirection_stops_at_its_host(book):
    assert seq(book, cfi(4, "/4/2/8!/4/2")) == 2


def test_cfi_unreadable_document_is_approximate_but_may_use_the_id_shortcut(book):
    # d4 isn't well-formed XML (a named entity); its block ids still resolve through the edition's ids
    assert locate(book, cfi(12, "/4/2/1:0")) == locators.Located(9, "cfi", False)
    assert locate(book, cfi(12, "/4/4[e2]/1:0")) == locators.Located(11, "cfi", False)


@pytest.mark.parametrize("bad", [
    "", "epubcfi()", "epubcfi(garbage", "garbage", "epubcfi(/4/6!/4)", "epubcfi(/6/7!/4)", "epubcfi(/6/0!/4)",
    "epubcfi(/6/998!/4)", "epubcfi(/6/4!/4/x)", "epubcfi(/6/4!/4//2)", "epubcfi(/6!/4)", "epubcfi(/6/4!/4,/2,/2,/2)",
    None, 7])
def test_malformed_cfi_or_missing_document_raises(book, bad):
    with pytest.raises(LocatorError):
        locate(book, {"cfi": bad})


def test_cfi_without_the_file_raises(book):
    book.path = ""
    with pytest.raises(LocatorError):
        locate(book, cfi(4, "/4/2/8"))


# ---- XPointer ----------------------------------------------------------------------------------

def xp(fragment, inner):
    return {"xpointer": f"/body/DocFragment[{fragment}]{inner}"}


def test_xpointer_basics(book):
    # DocFragment counts every itemref: d1 is [2]
    assert locate(book, xp(2, "/body/div/p[2]/text().0")) == locators.Located(2, "xpointer", True)
    assert seq(book, xp(2, "/body/div/p[1]/text().4")) == 1
    assert seq(book, xp(2, "/body/div/h1")) == 0
    assert seq(book, xp(2, "/body/div/p[2]/em/text().1")) == 2


def test_xpointer_index_counts_same_named_siblings_only(book):
    assert seq(book, xp(2, "/body/div/p[3]")) == 3          # h1 and hr don't count
    assert seq(book, xp(2, "/body/div/p")) == 1             # no index = the first


def test_xpointer_docfragment_counts_linear_no_items(book):
    assert seq(book, xp(4, "/body/ul/li[2]/text().0")) == 6      # d2 is itemref 3 → DocFragment[4]
    assert seq(book, xp(5, "/body/div/div[3]")) == 9


def test_xpointer_text_nodes(book):
    # li#l has two text nodes of its own: "one two " and " tail words", with p#ip between them
    assert seq(book, xp(4, "/body/ul/li[1]/text()[1].0")) == 4
    assert seq(book, xp(4, "/body/ul/li[1]/text()[2].3")) == 4
    assert seq(book, xp(4, "/body/ul/li[1]/p/text().0")) == 5
    assert seq(book, xp(4, "/body/ul/li[1]/text()[9].0")) == 4        # no such text node: the element


def test_xpointer_wrapper_and_between_blocks(book):
    assert seq(book, xp(2, "/body/div")) == 0
    assert seq(book, xp(2, "/body/div/hr")) == 1
    assert seq(book, xp(2, "/body")) == 0
    assert seq(book, xp(2, "")) == 0


def test_xpointer_before_the_first_block_of_the_edition(book):
    assert locate(book, xp(1, "/body/div/img")) == locators.Located(-1, "xpointer", True)


def test_xpointer_path_that_runs_out_is_approximate(book):
    for inner in ("/body/div/p[9]/text().0", "/body/section", "/body/div/p[0]"):
        assert locate(book, xp(2, inner)) == locators.Located(-1, "xpointer", False), inner
    assert locate(book, xp(4, "/body/ul/li[7]")) == locators.Located(3, "xpointer", False)


@pytest.mark.parametrize("bad", [
    "", "/body/div", "/body/DocFragment/body", "/body/DocFragment[0]/body", "/body/DocFragment[99]/body",
    "/body/DocFragment[3]/body",          # the non-linear notes item
    "/body/DocFragment[2]/body/div/p[x]", "/body/DocFragment[2]/body/div/text(1)", "/body/DocFragment[2]/body/*[1]",
    None, 3])
def test_malformed_xpointer_or_missing_document_raises(book, bad):
    with pytest.raises(LocatorError):
        locate(book, {"xpointer": bad})


# ---- spine + word / progress -------------------------------------------------------------------

def test_word_offsets(book):
    # d1 blocks: h1 (2 words), p#a (10), p#b (12), p#c (10)
    assert locate(book, {"spine": 1, "word": 0}) == locators.Located(0, "word", True)
    assert seq(book, {"spine": 1, "word": 1}) == 0
    assert seq(book, {"spine": 1, "word": 2}) == 1
    assert seq(book, {"spine": 1, "word": 11}) == 1
    assert seq(book, {"spine": 1, "word": 12}) == 2
    assert seq(book, {"spine": 1, "word": 23}) == 2
    assert seq(book, {"spine": 1, "word": 24}) == 3
    assert seq(book, {"spine": 1, "word": 33}) == 3
    assert seq(book, {"spine": 4, "word": 2}) == 11           # spine is the linear index: d4


def test_word_past_the_end_clamps_to_the_last_block(book):
    assert locate(book, {"spine": 1, "word": 34}) == locators.Located(3, "word", False)
    assert locate(book, {"spine": 1, "word": 10 ** 9}) == locators.Located(3, "word", False)


def test_word_in_a_document_without_blocks(book):
    assert locate(book, {"spine": 0, "word": 0}) == locators.Located(-1, "word", False)


def test_progress_rounds_down_to_the_block(book):
    assert locate(book, {"spine": 1, "progress": 0}) == locators.Located(0, "progress", False)
    assert seq(book, {"spine": 1, "progress": 1}) == 3
    assert seq(book, {"spine": 1, "progress": 1.0}) == 3
    assert seq(book, {"spine": 3, "progress": 0.33}) == 7          # 9.9 of 30 words: still the first block
    assert seq(book, {"spine": 3, "progress": 0.34}) == 8          # 10.2 words: the second block starts at 10
    assert seq(book, {"spine": 3, "progress": 0.99}) == 9


def test_progress_is_never_exact(book):
    for f in (0, 0.3, 1):
        assert locate(book, {"spine": 1, "progress": f}).exact is False


def test_progress_in_a_document_without_blocks(book):
    assert locate(book, {"spine": 0, "progress": 0.5}) == locators.Located(-1, "progress", False)


@pytest.mark.parametrize("loc", [
    {"spine": 9, "word": 0}, {"spine": -1, "word": 0}, {"spine": "1", "word": 0}, {"spine": True, "word": 0},
    {"spine": 1, "word": -1}, {"spine": 1, "word": 1.5}, {"spine": 1, "word": "3"}, {"spine": 1, "word": True},
    {"spine": 1, "progress": -0.1}, {"spine": 1, "progress": 1.01}, {"spine": 1, "progress": 42},
    {"spine": 1, "progress": "0.5"}, {"spine": 1, "progress": None}, {"spine": 1, "progress": True},
])
def test_bad_spine_word_progress(book, loc):
    with pytest.raises(LocatorError):
        locate(book, loc)


@pytest.mark.parametrize("loc", [
    {}, {"spine": 1}, {"word": 3}, {"progress": 0.1}, {"spine": 1, "word": 1, "progress": 0.1},
    {"block": 1, "cfi": "epubcfi(/6/4)"}, {"cfi": "epubcfi(/6/4)", "extra": 1}, {"foo": 1}, [], "epubcfi(/6/4)", None, 5])
def test_a_locator_has_exactly_one_form(book, loc):
    with pytest.raises(LocatorError):
        locate(book, loc)


def test_locator_error_is_a_value_error():
    assert issubclass(LocatorError, ValueError)


# ---- the current anchor ------------------------------------------------------------------------

def anchors_for(book, seqs):
    """An index's anchors for the given block seqs, as the generator would write them."""
    return [{"id": f"a{s}", "fp": text.fingerprint(book.blocks[s].words)} for s in seqs]


def test_position_finished_and_inclusive(book):
    anchors = anchors_for(book, [1, 3, 5])
    resolved = locators.resolve_edition({"anchors": anchors}, book)
    assert resolved == {"a1": 1, "a3": 3, "a5": 5}
    pos = lambda s, **kw: locators.position(anchors, resolved, s, **kw)  # noqa: E731
    assert pos(0) is None and pos(1) is None and pos(0, inclusive=True) is None
    assert pos(1, inclusive=True) == "a1"                   # on the page of a1, asking who is this?
    assert pos(2) == "a1" and pos(2, inclusive=True) == "a1"
    assert pos(3) == "a1" and pos(3, inclusive=True) == "a3"
    assert pos(99) == "a5"
    assert pos(-1) is None and pos(-1, inclusive=True) is None


def test_position_unresolved_anchors_wait_for_the_next_resolved_one():
    anchors = [{"id": "x"}, {"id": "y"}, {"id": "z"}]
    resolved = {"x": 2, "y": None, "z": 6}
    assert locators.position(anchors, resolved, 6, inclusive=True) == "z"
    assert locators.position(anchors, resolved, 6) == "x"           # y takes z's place, so it isn't passed yet


def test_locate_then_position_end_to_end(book):
    anchors = anchors_for(book, [1, 2, 4])
    resolved = locators.resolve_edition({"anchors": anchors}, book)
    found = locate(book, cfi(4, "/4/2/8/1:3"))               # inside p#b = block 2
    assert locators.position(anchors, resolved, found.seq) == "a1"
    assert locators.position(anchors, resolved, found.seq, inclusive=True) == "a2"


# ---- CLI ---------------------------------------------------------------------------------------

def test_cli_locate(book, tmp_path, capsys):
    index = tmp_path / "i.json"
    index.write_text(json.dumps({"anchors": anchors_for(book, [1, 3])}))
    run = lambda *a: cli.main(["locate", str(index), "--epub", book.path, *a])  # noqa: E731
    assert run("--cfi", "epubcfi(/6/4!/4/2/10/1:0)") == 0
    out = capsys.readouterr().out
    assert "block    3" in out and "exact" in out and "anchor   a1" in out
    assert run("--cfi", "epubcfi(/6/4!/4/2/10/1:0)", "--inclusive") == 0
    assert "anchor   a3" in capsys.readouterr().out
    assert run("--xpointer", "/body/DocFragment[2]/body/div/p[1]") == 0
    assert "block    1" in capsys.readouterr().out
    assert run("--spine", "1", "--word", "12") == 0
    assert "block    2" in capsys.readouterr().out
    assert run("--spine", "1", "--progress", "1") == 0
    assert "block    3" in capsys.readouterr().out
    assert run("--seq", "7") == 0
    assert "basis    block" in capsys.readouterr().out
    assert run("--spine", "1", "--word", "9999") == 0
    assert "approximate" in capsys.readouterr().out


@pytest.mark.parametrize("args", [[], ["--seq", "1", "--cfi", "epubcfi(/6/4)"], ["--spine", "1"], ["--word", "3"],
                                  ["--cfi", "garbage"], ["--spine", "99", "--word", "1"]])
def test_cli_locate_errors(book, tmp_path, capsys, args):
    index = tmp_path / "i.json"
    index.write_text(json.dumps({"anchors": []}))
    assert cli.main(["locate", str(index), "--epub", book.path, *args]) == 1
    assert capsys.readouterr().err.startswith("error:")


# ---- conformance -------------------------------------------------------------------------------

CASES = conformance.locator_cases(ROOT / "conformance")


def test_locator_cases_exist():
    assert len(CASES) >= 12


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_locator_case(path):
    ok, expected, actual = conformance.check_locator(path)
    if ok is None:
        pytest.skip("edition not in corpus/")
    assert actual == expected


def test_locator_cases_cover_every_form_and_both_modes():
    cases = [json.loads(p.read_text()) for p in CASES]
    forms = {next(k for k in ("block", "cfi", "xpointer", "word", "progress") if k in c["locator"]) for c in cases}
    assert forms == {"block", "cfi", "xpointer", "word", "progress"}
    assert any(c["expect"]["seq"] == -1 for c in cases)
    assert any(c["expect"]["exact"] is False for c in cases)
    assert any(c["expect"]["finished"] != c["expect"]["inclusive"] for c in cases)
    assert len({c["edition"] for c in cases}) >= 3


def test_locator_case_with_a_missing_edition_is_skipped(tmp_path):
    case = {"description": "x", "edition": "corpus/nope.epub", "index": "examples/sherlock-holmes/study-in-scarlet.json",
            "locator": {"block": 1}, "expect": {}}
    d = tmp_path / "conformance" / "locators"
    d.mkdir(parents=True)
    (d / "x.json").write_text(json.dumps(case))
    assert conformance.check_locator(d / "x.json", tmp_path)[0] is None
    assert conformance.regenerate_locator(d / "x.json", tmp_path) is False
