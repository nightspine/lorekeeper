import json

from lorekeeper import anchors, epub, text
from lorekeeper.epub import Block

from .conftest import EXAMPLES, corpus_epub


def _blocks(*paras: str) -> list[Block]:
    return [Block(seq=i, spine=0, words=text.words(p), raw=p) for i, p in enumerate(paras)]


def _anchor(aid: str, para: str) -> dict:
    return {"id": aid, "fp": text.fingerprint(text.words(para))}


P = [f"paragraph number {n} has enough words to be an anchor here" for n in range(6)]


def test_resolve_in_order():
    a = [_anchor("a", P[1]), _anchor("b", P[3])]
    assert anchors.resolve(a, _blocks(*P)) == {"a": 1, "b": 3}


def test_repeated_fingerprint_resolves_after_previous_anchor():
    blocks = _blocks(P[0], P[1], P[0], P[2])
    a = [_anchor("x", P[1]), _anchor("y", P[0])]
    assert anchors.resolve(a, blocks) == {"x": 1, "y": 2}


def test_unresolved_anchor_reveals_no_earlier_than_next_resolved():
    a = [_anchor("a", P[1]), _anchor("missing", "text not in this edition at all ok"), _anchor("c", P[4])]
    blocks = _blocks(*P)
    r = anchors.resolve(a, blocks)
    assert r["missing"] is None
    # Reader finished block 2 or 3: past "a" but "missing" waits until "c" is reached.
    assert anchors.frontier(a, r, 3) == "a"
    assert anchors.frontier(a, r, 4) == "c"


def test_trailing_unresolved_anchor_never_reveals():
    a = [_anchor("a", P[1]), _anchor("missing", "text not in this edition at all ok")]
    r = anchors.resolve(a, _blocks(*P))
    assert anchors.frontier(a, r, 10_000) == "a"


def test_before_first_anchor():
    a = [_anchor("a", P[2])]
    assert anchors.frontier(a, anchors.resolve(a, _blocks(*P)), 1) is None


def test_example_anchors_resolve_in_built_edition():
    ix = json.loads((EXAMPLES / "study-in-scarlet.json").read_text())
    blocks = epub.blocks(corpus_epub("pg244/pg244.epub"))
    r = anchors.resolve(ix["anchors"], blocks)
    assert all(r[a["id"]] == a["seq"] for a in ix["anchors"])


def test_example_anchors_survive_a_different_edition():
    """Standard Ebooks modernizes spelling and splits Watson's list into paragraphs."""
    ix = json.loads((EXAMPLES / "study-in-scarlet.json").read_text())
    blocks = epub.blocks(corpus_epub("se-scarlet/scarlet.epub"))
    r = anchors.resolve(ix["anchors"], blocks)
    unresolved = [a["id"] for a in ix["anchors"] if r[a["id"]] is None]
    assert unresolved == ["ch2.para14"]
    # Inside the split list the reader stays at the previous anchor (conservative).
    loc = {k: v for k, v in r.items() if v is not None}
    assert anchors.frontier(ix["anchors"], r, loc["ch2.para16"] - 1) == "ch2.para11"
