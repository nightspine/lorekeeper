"""EPUB text model (SPEC.md §3.1): spine order → blocks → normalized words.

Also reads the table of contents (EPUB 3 nav or EPUB 2 NCX) as chapter start
positions, and basic metadata. Standard library only. EPUB files are
untrusted input: we never execute or resolve external entities, and we only
read files named by the OPF manifest.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from . import text

BLOCK_TAGS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th",
     "pre", "figcaption", "caption"}
)
SKIP_TAGS = frozenset({"script", "style", "head", "title"})
# Some producers (Calibre conversions, older house styles) put every paragraph in a <div> and never use
# <p>. A document that yields no blocks under the standard set but still holds text is parsed again
# with these added (SPEC §3.1 rule 6), so such books are not silently reduced to their front matter.
FALLBACK_BLOCK_TAGS = BLOCK_TAGS | {"div", "blockquote", "section", "article", "aside"}
FALLBACK_MIN_WORDS = 20

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}


@dataclass(frozen=True)
class Block:
    seq: int          # index among all non-empty blocks in the book
    spine: int        # spine item index
    words: list[str]
    raw: str          # display text, for tooling only; never published

    @property
    def fingerprint(self) -> str:
        return text.fingerprint(self.words)


@dataclass(frozen=True)
class Chapter:
    title: str
    start: int        # seq of the chapter's first block


@dataclass(frozen=True)
class Doc:
    """One linear spine document: where it lives and which blocks it holds (tooling only)."""
    path: str                      # zip path of the XHTML file
    idref: str
    spine: int                     # index among linear spine items (Block.spine)
    first: int                     # seq of the document's first block
    ids: dict[str, int]            # element id → seq of the block it starts or sits in
    spine_child_index: int = -1    # 0-based index among ALL <itemref>s, linear="no" included (EPUB CFI steps)


@dataclass
class Book:
    blocks: list[Block]
    chapters: list[Chapter]        # table-of-contents entries, in reading order
    title: str = ""
    authors: list[str] = field(default_factory=list)
    language: str = ""
    series: str | None = None          # from EPUB 3 belongs-to-collection or Calibre's series meta
    series_index: str | None = None
    isbns: list[str] = field(default_factory=list)   # ISBN-13s from the package identifiers
    docs: list[Doc] = field(default_factory=list)    # linear spine documents, in reading order
    path: str = ""                                   # the file this was read from (to reopen documents)


class _BlockParser(HTMLParser):
    """Collects block-level text; text inside nested blocks belongs to the innermost one."""

    def __init__(self, block_tags: frozenset[str] = BLOCK_TAGS) -> None:
        super().__init__(convert_charrefs=True)
        self._block_tags = block_tags
        self._stack: list[tuple[int, list[str]]] = []
        self._skip = 0
        self._order = 0
        self.blocks: list[tuple[int, str]] = []
        self.ids: dict[str, int] = {}   # element id → order of the block it starts or sits in

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag in self._block_tags:
            self._stack.append((self._order, []))
            self._order += 1
        elif tag == "br" and self._stack:
            self._stack[-1][1].append(" ")
        for name, value in attrs:
            if name == "id" and value and value not in self.ids:
                self.ids[value] = self._stack[-1][0] if self._stack else self._order

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._stack:
            self._stack[-1][1].append(" ")
        for name, value in attrs:
            if name == "id" and value and value not in self.ids:
                self.ids[value] = self._stack[-1][0] if self._stack else self._order

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in self._block_tags and self._stack:
            order, parts = self._stack.pop()
            self.blocks.append((order, "".join(parts)))

    def handle_data(self, data):
        if not self._skip and self._stack:
            self._stack[-1][1].append(data)

    def close(self):
        super().close()
        while self._stack:
            order, parts = self._stack.pop()
            self.blocks.append((order, "".join(parts)))
        self.blocks.sort()


class _NavParser(HTMLParser):
    """Collects (href, label) from the <nav epub:type="toc"> of an EPUB 3 nav document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0          # >0 while inside the toc nav
        self._href: str | None = None
        self._label: list[str] = []
        self.entries: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav":
            if self._depth:
                self._depth += 1
            elif "toc" in (a.get("epub:type") or "").split():
                self._depth = 1
        elif self._depth and tag == "a" and a.get("href"):
            self._href, self._label = a["href"], []

    def handle_endtag(self, tag):
        if tag == "nav" and self._depth:
            self._depth -= 1
        elif tag == "a" and self._href is not None:
            self.entries.append((self._href, " ".join("".join(self._label).split())))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._label.append(data)


def _resolve(base_file: str, href: str) -> tuple[str, str]:
    """Resolve an href relative to the zip path of the file containing it → (zip path, fragment)."""
    path, _, frag = href.partition("#")
    full = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), unquote(path))) if path else base_file
    return full, unquote(frag)


def _toc_entries(z: zipfile.ZipFile, opf: ET.Element, opf_path: str) -> list[tuple[str, str, str]]:
    """(zip path, fragment, label) for each table-of-contents entry, in document order."""
    items = opf.findall(".//opf:manifest/opf:item", _NS)
    nav = next((i for i in items if "nav" in (i.attrib.get("properties") or "").split()), None)
    if nav is not None:
        nav_path, _ = _resolve(opf_path, nav.attrib["href"])
        p = _NavParser()
        p.feed(z.read(nav_path).decode("utf-8", errors="replace"))
        p.close()
        if p.entries:
            return [(*_resolve(nav_path, h), label) for h, label in p.entries]
    spine = opf.find(".//opf:spine", _NS)
    ncx_id = spine.attrib.get("toc") if spine is not None else None
    ncx = next((i for i in items if i.attrib.get("id") == ncx_id
                or i.attrib.get("media-type") == "application/x-dtbncx+xml"), None)
    if ncx is None:
        return []
    ncx_path, _ = _resolve(opf_path, ncx.attrib["href"])
    root = ET.fromstring(z.read(ncx_path))
    out = []
    for np in root.iter(f"{{{_NS['ncx']}}}navPoint"):   # document order = reading order
        content = np.find("ncx:content", _NS)
        label = np.find("ncx:navLabel/ncx:text", _NS)
        if content is not None:
            out.append((*_resolve(ncx_path, content.attrib["src"]),
                        " ".join((label.text or "").split()) if label is not None else ""))
    return out


def _parse_blocks(source: str) -> "_BlockParser":
    """Parse one document into blocks; fall back to div-style blocks when the standard set finds none."""
    parser = _BlockParser()
    parser.feed(source)
    parser.close()
    if any(text.words(raw) for _, raw in parser.blocks):
        return parser
    probe = _BlockParser(FALLBACK_BLOCK_TAGS)
    probe.feed(source)
    probe.close()
    if sum(len(text.words(raw)) for _, raw in probe.blocks) >= FALLBACK_MIN_WORDS:
        return probe
    return parser


def read(path: str | Path) -> Book:
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", _NS)
        if rootfile is None:
            raise ValueError("EPUB container.xml has no rootfile")
        opf_path = rootfile.attrib["full-path"]
        opf = ET.fromstring(z.read(opf_path))
        manifest = {item.attrib["id"]: _resolve(opf_path, item.attrib["href"])[0]
                    for item in opf.findall(".//opf:manifest/opf:item", _NS)}

        blocks: list[Block] = []
        # zip path → (first seq in doc, {element id → seq})
        anchors: dict[str, tuple[int, dict[str, int]]] = {}
        docs: list[Doc] = []
        spine_idx = 0
        for child_idx, ref in enumerate(opf.findall(".//opf:spine/opf:itemref", _NS)):
            if ref.attrib.get("linear", "yes") == "no":
                continue
            full = manifest[ref.attrib["idref"]]
            source = z.read(full).decode("utf-8", errors="replace")
            parser = _parse_blocks(source)
            first = len(blocks)
            kept: list[tuple[int, int]] = []   # (parser order, seq)
            for order, raw in parser.blocks:
                w = text.words(raw)
                if w:
                    kept.append((order, len(blocks)))
                    blocks.append(Block(seq=len(blocks), spine=spine_idx, words=w,
                                        raw=" ".join(raw.split())))
            ids = {}
            for el_id, order in parser.ids.items():
                seq = next((s for o, s in kept if o >= order), None)
                if seq is not None:
                    ids[el_id] = seq
            anchors[full] = (first, ids)
            docs.append(Doc(path=full, idref=ref.attrib["idref"], spine=spine_idx, first=first, ids=ids,
                            spine_child_index=child_idx))
            spine_idx += 1

        chapters: list[Chapter] = []
        for doc, frag, label in _toc_entries(z, opf, opf_path):
            if doc not in anchors:
                continue
            first, ids = anchors[doc]
            start = ids.get(frag, first) if frag else first
            if start >= len(blocks):
                continue
            if not chapters or start > chapters[-1].start:
                chapters.append(Chapter(title=label, start=start))

        md = opf.find(".//opf:metadata", _NS)
        def dc(tag: str) -> list[str]:
            return [" ".join((e.text or "").split()) for e in md.findall(f"dc:{tag}", _NS)] if md is not None else []
        titles, langs = dc("title"), dc("language")
        series, series_index = _series(md)
        return Book(blocks=blocks, chapters=chapters, title=titles[0] if titles else "",
                    authors=dc("creator"), language=langs[0] if langs else "",
                    series=series, series_index=series_index, isbns=isbns_from(dc("identifier")), docs=docs, path=str(path))


def _series(md: ET.Element | None) -> tuple[str | None, str | None]:
    """Series name and position: EPUB 3 collection (type series) first, then Calibre's meta tags."""
    if md is None:
        return None, None
    metas = md.findall("opf:meta", _NS)
    refines: dict[str, dict[str, str]] = {}
    for m in metas:
        if m.attrib.get("refines", "").startswith("#"):
            refines.setdefault(m.attrib["refines"][1:], {})[m.attrib.get("property", "")] = (m.text or "").strip()
    for m in metas:
        if m.attrib.get("property") == "belongs-to-collection" and (m.text or "").strip():
            r = refines.get(m.attrib.get("id", ""), {})
            if r.get("collection-type", "series") == "series":
                return " ".join(m.text.split()), _clean_index(r.get("group-position"))
    named = {m.attrib.get("name"): m.attrib.get("content", "") for m in metas if m.attrib.get("name")}
    if named.get("calibre:series", "").strip():
        return " ".join(named["calibre:series"].split()), _clean_index(named.get("calibre:series_index"))
    return None, None


def isbn10_to_13(isbn10: str) -> str | None:
    if not re.fullmatch(r"[0-9]{9}[0-9Xx]", isbn10):
        return None
    core = "978" + isbn10[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


def _isbn13_ok(isbn: str) -> bool:
    if not re.fullmatch(r"97[89][0-9]{10}", isbn):
        return False
    return sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(isbn)) % 10 == 0


def isbns_from(identifiers: list[str]) -> list[str]:
    """ISBN-13s found in dc:identifier values: bare, urn:isbn:, isbn:, or a book ASIN (= ISBN-10)."""
    out: list[str] = []
    for raw in identifiers:
        v = raw.strip()
        low = v.lower()
        for prefix in ("urn:isbn:", "isbn:", "asin:"):
            if low.startswith(prefix):
                v = v[len(prefix):]
                break
        v = v.replace("-", "").replace(" ", "")
        cand = v if len(v) == 13 else isbn10_to_13(v) if len(v) == 10 else None
        if cand and _isbn13_ok(cand) and cand not in out:
            out.append(cand)
    return out


def _clean_index(value: str | None) -> str | None:
    """'1.0' → '1', '2.5' → '2.5'; anything that isn't a number is dropped."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return str(int(f)) if f == int(f) else str(f)


MAX_COVER_BYTES = 15 * 1024 * 1024
COVER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def cover(path: str | Path) -> tuple[bytes, str] | None:
    """The book's cover image and its media type, if the package declares one.

    EPUB 3 marks it with properties="cover-image"; EPUB 2 points at it from <meta name="cover">.
    Only raster images named in the manifest are returned (never SVG or HTML).
    """
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", _NS)
        if rootfile is None:
            return None
        opf_path = rootfile.attrib["full-path"]
        opf = ET.fromstring(z.read(opf_path))
        items = opf.findall(".//opf:manifest/opf:item", _NS)
        item = next((i for i in items if "cover-image" in (i.attrib.get("properties") or "").split()), None)
        if item is None:
            meta = next((m for m in opf.findall(".//opf:metadata/opf:meta", _NS) if m.attrib.get("name") == "cover"), None)
            ref = meta.attrib.get("content") if meta is not None else None
            item = next((i for i in items if ref and i.attrib.get("id") == ref), None)
        if item is None or item.attrib.get("media-type") not in COVER_TYPES:
            return None
        name = _resolve(opf_path, item.attrib["href"])[0]
        try:
            info = z.getinfo(name)
        except KeyError:
            return None
        if info.file_size > MAX_COVER_BYTES:
            return None
        return z.read(name), item.attrib["media-type"]


def blocks(path: str | Path) -> list[Block]:
    return read(path).blocks
