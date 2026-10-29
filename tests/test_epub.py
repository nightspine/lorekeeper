

def test_div_paragraph_documents_fall_back_to_div_blocks(tmp_path):
    """A Calibre-style document with every paragraph in a <div> and no <p> must still yield its text,
    while a document that has <p> blocks is never re-parsed (its wrapper divs stay invisible)."""
    import zipfile
    from lorekeeper import epub
    p_doc = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><div class="wrap">stray wrapper text '
             '<p>Alpha beta gamma delta epsilon zeta eta theta.</p><p>Iota kappa lambda mu nu xi omicron pi.</p></div></body></html>')
    div_doc = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><div class="calibre3"> </div>'
               '<div class="ct">Chapter One</div>' + "".join(
                   f'<div class="p">Paragraph number {i} has quite enough words to count as prose here.</div>' for i in range(5))
               + '</body></html>')
    tiny_doc = '<html xmlns="http://www.w3.org/1999/xhtml"><body><div>Only four words here</div></body></html>'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="u">x</dc:identifier><dc:title>T</dc:title></metadata>'
           '<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine></package>')
    path = tmp_path / "divs.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                   '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", opf); z.writestr("a.xhtml", p_doc); z.writestr("b.xhtml", div_doc); z.writestr("c.xhtml", tiny_doc)
    book = epub.read(path)
    by_spine = {}
    for b in book.blocks: by_spine.setdefault(b.spine, []).append(" ".join(b.words))
    assert len(by_spine[0]) == 2 and not any("stray" in t for t in by_spine[0])   # p-doc: wrapper text ignored, no re-parse
    assert len(by_spine[1]) == 6 and by_spine[1][0].startswith("chapter one")       # div-doc: fallback kicked in
    assert 2 not in by_spine                                                        # under 20 words: still nothing
