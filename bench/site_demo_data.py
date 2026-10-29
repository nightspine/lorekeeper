"""Flatten a Lorekeeper index into site/demo-data.js for the landing page's hero demo.

    uv run python bench/site_demo_data.py lorekeeper-data/indexes/dracula-XXXX.lore.json corpus/pg345/pg345.epub

The demo gates the data with the spec's rule (an item is visible when the ordinal of its `from`
anchor is at or before the position), so anchors are referred to by ordinal. Chapter labels come
from the EPUB's table of contents. Story-so-far recaps only; facets keep their kind so the demo can
show the latest visible profile as the entry's description, like the Reader does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lorekeeper import epub  # noqa: E402


def nice(title: str) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    m = re.match(r"CHAPTER ([IVXL]+)\.?\s*(.*)", t, re.I)
    if not m:
        return t.title()
    rest = m.group(2).strip().title().replace("’S", "’s").replace("'S", "'s")
    return f"Chapter {m.group(1).upper()}" + (f", {rest}" if rest else "")


def main(index_path: str, epub_path: str, out: str = "site/demo-data.js") -> None:
    d = json.loads(Path(index_path).read_text())
    book = epub.read(epub_path)
    chapters = sorted(((c.start, nice(c.title)) for c in book.chapters), key=lambda x: x[0])
    seq_of = {a["id"]: a["seq"] for a in d["anchors"]}
    ords = {a["id"]: i for i, a in enumerate(d["anchors"])}

    def chapter_at(seq: int) -> str:
        label = ""
        for start, title in chapters:
            if start <= seq:
                label = title
        return label

    anchors = [{"id": a["id"], "ch": chapter_at(seq_of[a["id"]])} for a in d["anchors"]]
    ents = []
    for e in d["entities"]:
        facets = e.get("facets", [])
        has_profile = any(f.get("kind") == "profile" for f in facets)
        # Plain facts are only shown when an entity has no profile; otherwise they only count.
        ents.append({"id": e["id"], "type": e["type"], "from": ords[e["from"]],
                     "names": [{"v": n["value"], "from": ords[n["from"]], **({"p": 1} if n.get("kind") == "primary" else {})} for n in e["names"]],
                     "facets": [({"from": ords[f["from"]], "text": f["text"], "k": "p"} if f.get("kind") == "profile"
                                 else {"from": ords[f["from"]], **({} if has_profile else {"text": f["text"]})})
                                for f in facets]})
    evs = [{"from": ords[v["from"]], "text": v["summary"]} for v in d["events"]]
    recs = [{"from": ords[r["from"]], "text": r["text"]} for r in d["recaps"] if r.get("scope", "story") != "chapter"]
    data = {"title": d["work"]["title"], "author": (d["work"].get("authors") or [""])[0],
            "anchors": anchors, "entities": ents, "events": evs, "recaps": recs}
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    Path(out).write_text(f"// {d['work']['title']}: an index built by Lorekeeper, flattened for the landing-page demo by bench/site_demo_data.py.\n"
                         "// Anchors are referred to by ordinal; story-so-far recaps only; facets with k: \"p\" are profiles.\n"
                         f"export const DEMO = {s};\n")
    print(f"wrote {out}: {len(s)} bytes, {len(ents)} entities, {len(evs)} events, {len(recs)} recaps")


if __name__ == "__main__":
    main(*sys.argv[1:])
