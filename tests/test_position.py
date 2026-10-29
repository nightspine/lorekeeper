"""Book Orbit reading progress → block seq, on a tiny EPUB with a non-linear spine item."""

import zipfile

import pytest

from lorekeeper import epub
from lorekeeper.web import position
from lorekeeper.web.bookorbit import BookOrbit, Session

WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"      # 10 words per paragraph


def xhtml(cid, wrap=True):
    paras = "".join(f'<p id="{cid}p{i}">{WORDS}</p>' for i in range(1, 5))
    inner = f'<div id="{cid}">{paras}</div>' if wrap else paras
    return (f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{cid}</title></head>'
            f"<body>{inner}</body></html>")


@pytest.fixture
def book(tmp_path):
    """Spine itemrefs: d1 (0), n1 non-linear (1), d2 (2), d3 (3). Blocks: d1 0-3, d2 4-7, d3 8-11."""
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata><manifest>'
           + "".join(f'<item id="{i}" href="{i}.xhtml" media-type="application/xhtml+xml"/>' for i in ("d1", "n1", "d2", "d3"))
           + '</manifest><spine><itemref idref="d1"/><itemref idref="n1" linear="no"/>'
             '<itemref idref="d2"/><itemref idref="d3"/></spine></package>')
    path = tmp_path / "t.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/d1.xhtml", xhtml("d1"))
        z.writestr("OEBPS/n1.xhtml", xhtml("n1"))
        z.writestr("OEBPS/d2.xhtml", xhtml("d2"))
        z.writestr("OEBPS/d3.xhtml", xhtml("d3", wrap=False))
    return epub.read(path)


def test_docs_record_spine_positions(book):
    assert [(d.path, d.spine, d.first, d.spine_child_index) for d in book.docs] == [
        ("OEBPS/d1.xhtml", 0, 0, 0), ("OEBPS/d2.xhtml", 1, 4, 2), ("OEBPS/d3.xhtml", 2, 8, 3)]
    assert book.docs[1].ids["d2p3"] == 6 and len(book.blocks) == 12


def test_cfi_without_id_assertion(book):
    # d2 is itemref 2 → step 6; html > body (4) > div (2) > third p (6) > text
    r = position.resolve(book, {"percentage": 50, "text": {"cfi": "epubcfi(/6/6!/4/2/6/1:0)"}})
    assert r == {"seq": 6, "p": 0.5, "basis": "cfi"}


def test_cfi_with_id_assertion_uses_the_block_directly(book):
    # the path steps are bogus, but the asserted id of the target names its block
    r = position.resolve(book, {"percentage": 50, "text": {"cfi": "epubcfi(/6/6[d2]!/4/2/98[d2p3]/1:0)"}})
    assert r["seq"] == 6 and r["basis"] == "cfi"


def test_cfi_after_a_non_linear_item(book):
    # d3 is itemref 3 (the non-linear item counts) → step 8; second paragraph
    r = position.resolve(book, {"percentage": 76, "text": {"cfi": "epubcfi(/6/8!/4/4/1:3)"}})
    assert r["basis"] == "cfi" and r["seq"] == 9 and r["p"] == 0.75


def test_cfi_text_node_after_an_element(book):
    # offset inside the paragraph still lands in that block
    r = position.resolve(book, {"percentage": 5, "text": {"cfi": "epubcfi(/6/2!/4/2/4/1:4)"}})
    assert r["seq"] == 1 and r["basis"] == "cfi"


def test_kobo_source_and_content_percent(book):
    r = position.resolve(book, {"percentage": 55, "kobo": {"source": "d2.xhtml", "contentPercent": 75}})
    assert r["basis"] == "kobo" and r["seq"] == 7 and r["p"] == pytest.approx(70 / 120)
    r = position.resolve(book, {"percentage": 55, "kobo": {"source": "OEBPS/d2.xhtml", "contentPercent": 50}})
    assert r["seq"] == 6


def test_koreader_docfragment_path(book):
    # DocFragment is 1-based over every itemref: [3] is d2
    xp = "/body/DocFragment[3]/body/div/p[2]/text().0"
    r = position.resolve(book, {"percentage": 42, "koreader": xp})
    assert r["basis"] == "koreader" and r["seq"] == 5


def test_koreader_bad_path_falls_back_inside_the_document(book):
    xp = "/body/DocFragment[3]/body/div/p[9]/text().0"
    r = position.resolve(book, {"percentage": 60, "koreader": xp})
    assert r["basis"] == "koreader" and 4 <= r["seq"] <= 7


def test_percentage_only(book):
    r = position.resolve(book, {"percentage": 50})
    assert r == {"seq": 6, "p": 0.5, "basis": "percentage"}
    assert position.resolve(book, {"percentage": 0})["seq"] == 0
    assert position.resolve(book, {"percentage": 100})["seq"] == 11


def test_stale_exact_position_yields_to_percentage(book):
    r = position.resolve(book, {"percentage": 90, "text": {"cfi": "epubcfi(/6/6!/4/2/6/1:0)"}})
    assert r["basis"] == "percentage" and r["seq"] == 10


@pytest.mark.parametrize("cfi", ["epubcfi(garbage", "epubcfi(/6/999!/4/2)", "epubcfi(/6/7!/4)", "", "epubcfi()",
                                 "epubcfi(/6/6!/4/50/2/1:0)"])
def test_malformed_cfi_falls_back_to_percentage(book, cfi):
    r = position.resolve(book, {"percentage": 30, "text": {"cfi": cfi}})
    assert r["basis"] in ("percentage", "cfi") and r["seq"] is not None
    if r["basis"] == "cfi":                        # a path that runs out lands just before its document
        assert r["seq"] >= 3


def test_nothing_usable(book):
    assert position.resolve(book, {"percentage": None}) == {"seq": None, "p": 0.0, "basis": "percentage"}


def test_progress_is_trimmed():
    bo = BookOrbit(Session("http://x", "r", None, "u"), lambda s: None)
    seen = []
    bo._call = lambda method, path, *a, **k: seen.append((method, path)) or {"items": [{
        "fileId": 64, "percentage": 7.9, "updatedAt": "t", "narration": {"x": 1},
        "text": {"cfi": "epubcfi(/6/2)", "page": 3}, "kobo": {"source": "a.xhtml", "contentPercent": 5, "type": "x"},
        "koreader": "/body/DocFragment[1]"}]}
    assert bo.progress(77) == [{"fileId": 64, "percentage": 7.9, "updatedAt": "t", "text": {"cfi": "epubcfi(/6/2)"},
                                "kobo": {"source": "a.xhtml", "contentPercent": 5}, "koreader": "/body/DocFragment[1]"}]
    assert seen == [("GET", "/reading-progress?bookId=77")]
