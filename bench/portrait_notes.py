"""Build the appearance notes, milieu and full portrait prompts for some characters of an index, and draw nothing.

    uv run python bench/portrait_notes.py INDEX --epub EPUB [--entity NAME ...] [--at FRACTION] --provider P --model M
    uv run python bench/portrait_notes.py INDEX --classify --entity Sevro --entity Darrow --provider gemini   # no EPUB needed

Default: the 8 people named by the most events, at the end of the index (`--at 1.0`). The art set is written to a temporary
folder (never next to the index or into lorekeeper-data). With `--classify` (no book text needed) it prints, per entity, the
look items and look changes the model flags and the groups it is placed in, by anchor; on an `art.py` without a look index
it prints the old `look_points` instead, so the same command measures before and after. The text cost is printed last.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

from lorekeeper import art, epub, llm


class NoImage:
    provider, name = "", "none"

    def draw(self, *a, **k):
        raise RuntimeError("the bench draws nothing")


def pick(doc: dict, names: list[str], n: int = 8) -> list[str]:
    if names:
        out = []
        for want in names:
            hit = next((e["id"] for e in doc["entities"] if any(x["value"].lower() == want.lower() for x in e["names"])), None)
            hit = hit or next((e["id"] for e in doc["entities"]
                               if any(want.lower() in x["value"].lower() for x in e["names"])), None)
            if hit:
                out.append(hit)
            else:
                print(f"no entity named {want!r}", file=sys.stderr)
        return out
    named = Counter(x for ev in doc.get("events", []) for x in ev.get("entities", []))
    people = {e["id"] for e in doc["entities"] if e["type"] == "person"}
    return [x for x, _ in named.most_common() if x in people][:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub")
    ap.add_argument("--entity", action="append", default=[])
    ap.add_argument("--at", type=float, default=1.0, help="position as a fraction of the index's anchors (default the end)")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--model")
    ap.add_argument("--classify", action="store_true", help="only the look index and the group pick (no book text needed)")
    ap.add_argument("--style", default="realism")
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.index).read_text())
    spec = llm.PROVIDERS[a.provider]
    text = llm.make(a.provider, a.model or spec["cheap"], api_key=llm.env_key(a.provider))
    bk = epub.read(a.epub) if a.epub else None
    tmp = Path(tempfile.mkdtemp(prefix="lk-bench-"))
    il = art.Illustrator([art.Book(doc, bk, art.FileSink(tmp / "art"), None)], text, NoImage(), a.style,
                         log=lambda m: print(m, file=sys.stderr), workers=1)
    b = il.books[0]
    at = b.anchors[min(len(b.anchors) - 1, max(0, round(a.at * (len(b.anchors) - 1))))]["id"]
    ents = pick(doc, a.entity)
    title = doc.get("work", {}).get("title", a.index)
    print(f"# {title}: at {at} ({a.at:.0%}), {len(ents)} entities, text model {a.provider}/{text.name}\n")
    if a.classify:
        classify(il, ents, at)
    else:
        if il.world(0):
            print("World:", (il.man[0]["world"] or {}).get("text", ""), "\n")
        for eid in ents:
            name = b.ents[eid]["names"][0]["value"]
            try:
                job = il.job("portrait", eid, at=at)
                pr = il.make_prompt(job)
            except art.NotVisible:
                print(f"## {name}: not visible at {at}\n")
                continue
            except ValueError as e:
                print(f"## {name}: no prompt ({e}{': ' + e.detail if getattr(e, 'detail', '') else ''})\n")
                for n in il.man[0]["appearance"].get(eid, [])[-1:]:
                    print(f"   note at {n['from']}: {n.get('text')!r}")
                print()
                continue
            print(f"## {name} (portrait at {pr.anchor})")
            for x, note in pr.notes:
                print(f"   note: {note}")
            if getattr(pr, "milieu", None):
                print(f"   milieu: {pr.milieu}")
            print(f"   prompt: {pr.text}\n")
    print(f"text cost ${il.text_spent():.4f}")
    if os.environ.get("KEEP"):
        print(f"art set kept in {tmp}")
    return 0


def classify(il, ents: list[str], at: str) -> None:
    b = il.books[0]
    if not hasattr(il, "look_index"):           # art.py before the look index: the word-list look points
        for eid in ents:
            pts = il.look_points(0, eid)
            print(f"## {b.ents[eid]['names'][0]['value']}: {len(pts)} look points (word lists) at {[p[1] for p in pts]}")
        return
    il.classify(0, ents)
    for eid in ents:
        e = b.ents[eid]
        idx = il.look_index(0, eid)
        print(f"## {e['names'][0]['value']} ({e['type']}): {sum(1 for x in idx.values() if x['look'])} look items, "
              f"{sum(1 for x in idx.values() if x['change'])} look changes")
        for item in il.items(0, eid):
            got = idx.get(item["id"])
            if got and (got["look"] or got["change"]):
                flag = ("look" if got["look"] else "") + (f" CHANGE: {got['change']}" if got["change"] else "")
                print(f"   {item['from']:>16} {flag:<40} {item['text'][:110]}")
        if e["type"] in art.PORTRAIT_TYPES:
            picks = il.group_picks(0, eid)
            print("   group picks: " + "; ".join(f"{b.ents[p['group']]['names'][0]['value']} from {p['from']}"
                                                 + (f" until {p['until']}" if p.get("until") else "") for p in picks))
            marks = sorted({p["from"] for p in picks} | {p["until"] for p in picks if p.get("until")} | {at}, key=b.ord_of.get)
            for anchor in marks:
                gs = il.groups_of(0, eid, anchor)
                print(f"   groups at {anchor}: {[b.ents[g]['names'][0]['value'] for g in gs]}")
        print()


if __name__ == "__main__":
    sys.exit(main())
