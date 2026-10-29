"""Writes the fixtures tests/js/run_client.mjs compares against: what the Python reference produces.

Everything here comes from lorekeeper.text / epub / anchors, so the JS client is checked against
the reference implementation and never against itself. Needs corpus/pg244/pg244.epub.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import random
import sys
import unicodedata
from bisect import bisect_left
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from lorekeeper import anchors as anchors_mod
from lorekeeper import epub, text

ROOT = Path(__file__).resolve().parents[2]
EPUB = ROOT / "corpus" / "pg244" / "pg244.epub"
EXAMPLES = ROOT / "examples" / "sherlock-holmes"
NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "opf": "http://www.idpf.org/2007/opf"}

NASTY = [
    "Don’t wine-glass good‐bye non‑breaking soft­hyphen zero‍width",
    "“Quoted,” he said — and then… nothing.",
    "café Café CAFÉ café naïve Zoë",
    "Straße STRASSE ß ẞ ŉ ǰ ΐ ΰ և ﬁnal ﬃ ﬆ",
    "ΣΊΣΥΦΟΣ ΌΣΟΣ σίσυφος ς Σ",
    "İstanbul ISTANBUL ıi İ",
    "x² y³ ½ ①②③ ٣٤ १२३ 1,000.50 3.14 1878",
    "wine-glass wineglass Good-bye Goodbye mother-in-law",
    "ＦＵＬＬＷＩＤＴＨ ｶﾀｶﾅ ㎏ ℃ №",
    "Привет мир ПРИВЕТ, 日本語のテキスト, 한국어, العربية, עברית",
    "mixed скриптов script 混合 mix123abc",
    "😀 emoji 👍🏽 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 𝐛𝐨𝐥𝐝 \U0001f600text",
    "ᏣᎳᎩ ꭰꭱꭲ Ꭰ",
    "a​b a⁠b a﻿b a b a\tb a\nb",
    "tab\tsep;semi:colon/slash\\back|pipe",
    "",
    "   ",
    "'''---’’‐‑",
    "O’Brien's “don’t” ‘quote’",
    "&amp; &lt; &gt; &#8217; stays literal because entities are decoded before words()",
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen",
    "é é ǆ ǅ Ǆ ﬀ Å Å",
]


def _spine_documents(path: Path) -> list[dict]:
    """Every spine itemref in order (linear or not) with its raw HTML."""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//c:rootfile", NS).attrib["full-path"]
        opf = ET.fromstring(z.read(opf_path))
        manifest = {i.attrib["id"]: posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(i.attrib["href"])))
                    for i in opf.findall(".//opf:manifest/opf:item", NS)}
        return [{"spineChild": n, "linear": ref.attrib.get("linear", "yes") != "no",
                 "path": manifest[ref.attrib["idref"]],
                 "html": z.read(manifest[ref.attrib["idref"]]).decode("utf-8", errors="replace")}
                for n, ref in enumerate(opf.findall(".//opf:spine/opf:itemref", NS))]


def _anchor_case(name: str, index: dict, fps: list[str], seqs_of_interest: list[int]) -> dict:
    """What anchors.py computes for this index against an edition with these fingerprints."""
    class B:  # resolve() only reads .seq and .fingerprint
        def __init__(self, seq, fp): self.seq, self.fingerprint = seq, fp
    edition = [B(i, fp) for i, fp in enumerate(fps)]
    resolved = anchors_mod.resolve(index["anchors"], edition)
    eff = anchors_mod.effective_locations(index["anchors"], resolved)
    return {
        "index": name,
        "resolved": resolved,
        "effective": [None if x == float("inf") else x for x in eff],
        "frontier": {str(s): anchors_mod.frontier(index["anchors"], resolved, s) for s in seqs_of_interest},
    }


def _html_cases() -> list[dict]:
    long_div = " ".join(["word"] * 25)
    cases = [
        "<html><head><title>T t t</title><style>p{}</style></head><body><p>Hello <b>big</b> world</p><h1>Head</h1></body></html>",
        "<html><body><p>Outer <em>text</em> before <ul><li>nested one</li><li>nested two</li></ul> after</p></body></html>",
        "<html><body><p>line one<br/>line two<br />three</p><p> </p><p>&amp; &#8217; &nbsp;</p></body></html>",
        f"<html><body><div>{long_div}</div><div>short here</div></body></html>",
        "<html><body><div>too short to count</div><div>just a few words</div></body></html>",
        f"<html><body><div><div>{long_div}</div> trailing words</div><blockquote>{long_div}</blockquote></body></html>",
        "<html><body><div>not used</div><p>standard wins</p></body></html>",
        "<html><body><table><tr><td>cell a</td><td>cell b</td></tr></table><dl><dt>term</dt><dd>def</dd></dl></body></html>",
        "<html><body>text outside <p>inside</p> more outside <script>var x = '<p>no</p>';</script><pre>pre  text</pre></body></html>",
        "<html><body><p><span>a</span><span> b</span><script>skip me</script> c</p><figure><figcaption>cap</figcaption></figure></body></html>",
        "<html><body><p>curly “quotes” and wine-glass and Straße</p><p>ΣΊΣΥΦΟΣ</p></body></html>",
    ]
    out = []
    for html in cases:
        parser = epub._parse_blocks(html)
        out.append({"html": html, "blocks": [text.words(raw) for _, raw in parser.blocks if text.words(raw)]})
    return out


def write(outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    book = epub.read(EPUB)
    docs = _spine_documents(EPUB)
    linear_idx = {d.spine_child_index: d for d in book.docs}
    edition = {
        "textHash": text.text_hash(b.words for b in book.blocks),
        "blocks": [{"seq": b.seq, "spine": b.spine, "words": b.words, "fp": b.fingerprint} for b in book.blocks],
        "docs": [{"spineChild": d["spineChild"], "linear": d["linear"],
                  "spine": linear_idx[d["spineChild"]].spine if d["spineChild"] in linear_idx else None,
                  "first": linear_idx[d["spineChild"]].first if d["spineChild"] in linear_idx else None,
                  "ids": linear_idx[d["spineChild"]].ids if d["spineChild"] in linear_idx else {}}
                 for d in docs],
    }
    (outdir / "edition.json").write_text(json.dumps(edition, ensure_ascii=False))
    (outdir / "documents.json").write_text(json.dumps(docs, ensure_ascii=False))

    rng = random.Random(244)
    sample = rng.sample(book.blocks, 50)
    # words() of every code point this Python's Unicode database knows (the JS engine may know a newer
    # version, and characters assigned since then are not comparable). Hashed in chunks of 4096.
    assigned = [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF and unicodedata.category(chr(cp)) != "Cn"]
    ranges: list[list[int]] = []
    for cp in assigned:
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    cp_hashes = []
    for start in range(0, 0x110000, 4096):
        h = hashlib.sha256()
        for cp in assigned[bisect_left(assigned, start):bisect_left(assigned, start + 4096)]:
            h.update((" ".join(text.words(chr(cp))) + "\n").encode())
        cp_hashes.append(h.hexdigest())
    strings = [{"text": s, "words": text.words(s), "fp": text.fingerprint(text.words(s))} for s in NASTY]
    strings += [{"text": b.raw, "words": text.words(b.raw), "fp": text.fingerprint(text.words(b.raw))} for b in sample]
    (outdir / "strings.json").write_text(json.dumps({"strings": strings, "codepointHashes": cp_hashes, "assignedRanges": ranges,
                                                     "unicode": unicodedata.unidata_version,
                                                     "sha256abc": hashlib.sha256(b"abc").hexdigest()}, ensure_ascii=False))

    fps = [b.fingerprint for b in book.blocks]
    interest = sorted({-1, 0, 1, len(fps) - 1, len(fps)} | set(range(0, len(fps), 7)))
    cases = []
    for name in ("study-in-scarlet", "sign-of-four"):
        index = json.loads((EXAMPLES / f"{name}.json").read_text())
        cases.append({**_anchor_case(name, index, fps, interest), "edition": "pg244"})
        # An edition that lost every 9th block and repeats some: anchors go missing, duplicates appear.
        mangled = [fp for i, fp in enumerate(fps) if i % 9 != 4]
        mangled = mangled[:300] + mangled[250:320] + mangled[300:]
        cases.append({**_anchor_case(name, index, mangled, interest), "edition": "mangled", "fps": mangled})
    (outdir / "anchors.json").write_text(json.dumps(cases, ensure_ascii=False))
    # The editions named by conformance/locators/*.json, as raw documents (Node cannot unzip an EPUB).
    editions = {}
    for case_path in sorted((ROOT / "conformance" / "locators").glob("*.json")):
        name = json.loads(case_path.read_text())["edition"]
        if name not in editions and (ROOT / name).is_file():
            editions[name] = _spine_documents(ROOT / name)
    (outdir / "editions.json").write_text(json.dumps(editions, ensure_ascii=False))
    (outdir / "htmlcases.json").write_text(json.dumps(_html_cases(), ensure_ascii=False))


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "js" / "fixtures")
