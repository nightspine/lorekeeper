"""Does a place's (or a thing's) own picture, sent as a reference, keep it the same across the scenes that happen there?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/scene_places.py INDEX --epub EPUB --out DIR \
        --place ID [--place ID] [--thing ID] [--events 3] [--portraits 3] [--style ink] [--quality draft] [--cap 1.50]

For each `--place` (an entity id linked from at least `--events` events' `setting.place`) and each `--thing` (an object the
events' summaries name), the script draws the entity once, then up to `--events` of its non-minor events twice from one
prompt: once with the figures' references only, once with the place's or thing's picture in the spare slot
(`Illustrator(place_refs=True)`). The main figures of those events (`--portraits`, most named first) get a portrait first,
so the scenes have labelled figures and the sheet shows whether the place reference crowds out their likeness. Everything
goes into DIR: the art set (`set/`), `results.json` (prompts, notes sent, references, costs) and `index.html`, a contact
sheet. The writer is Claude's cheap model, the pictures Gemini's default image model unless `--image-model` says otherwise.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import sys
from collections import Counter
from pathlib import Path

from lorekeeper import art, epub, llm


class Noting:
    """The image model, keeping the note sent with each call."""

    def __init__(self, model):
        self.model, self.notes = model, []
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def draw(self, prompt, **kw):
        self.notes.append(kw.get("note", ""))
        return self.model.draw(prompt, **kw)


def events_of(il: art.Illustrator, doc: dict, eid: str, kind: str, n: int) -> list[dict]:
    """The first `n` non-minor events at the place (its setting link) or naming the thing (its visible name in the summary)."""
    b = il.books[0]
    out = []
    for ev in sorted(doc["events"], key=lambda e: b.ord_of.get(e["from"], 10 ** 9)):
        if ev.get("kind") == "minor" or ev["from"] not in b.ord_of:
            continue
        if kind == "place":
            hit = (ev.get("setting") or {}).get("place") == eid
        else:
            vis = {x["id"]: x for x in il.view(0, ev["from"])["entities"]}
            hit = eid in vis and (eid in ev["entities"] or il._names_in(il._visible_names(vis[eid]), ev["summary"]))
        if hit:
            out.append(ev)
    return out[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--place", action="append", default=[])
    ap.add_argument("--thing", action="append", default=[])
    ap.add_argument("--events", type=int, default=3)
    ap.add_argument("--portraits", type=int, default=3)
    ap.add_argument("--style", default="ink")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--image-model")
    ap.add_argument("--cap", type=float, default=1.50)
    a = ap.parse_args()

    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sink = art.FileSink(out / "set")
    text = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", a.image_model, api_key=llm.env_key("gemini")))
    il = art.Illustrator([art.Book(doc, bk, sink, None)], text, image, a.style, portraits=None, quality=a.quality, cap=a.cap,
                         workers=1, log=lambda m: print(m, file=sys.stderr), place_refs=True)
    b = il.books[0]
    name = {e["id"]: e["names"][0]["value"] for e in doc["entities"]}
    targets = [(x, "place") for x in a.place] + [(x, "thing") for x in a.thing]
    plan = {(eid, kind): events_of(il, doc, eid, kind, a.events) for eid, kind in targets}
    for (eid, kind), evs in plan.items():
        print(f"{kind} {name.get(eid, eid)}: {len(evs)} events: {', '.join(e['id'] for e in evs)}", file=sys.stderr)

    # the figures first, so the scenes are labelled: the people and creatures these events name most, each drawn as of the
    # first chosen event that names them (a portrait after that is no reference for it)
    first: dict[str, str] = {}
    count: Counter = Counter()
    types = {e["id"]: e["type"] for e in doc["entities"]}
    for evs in plan.values():
        for ev in evs:
            for x in ev["entities"]:
                if types.get(x) in art.PORTRAIT_TYPES:
                    count[x] += 1
                    if x not in first or b.ord_of[ev["from"]] < b.ord_of[first[x]]:
                        first[x] = ev["from"]
    drawn: dict[str, dict] = {}
    for x, _ in count.most_common(a.portraits):
        try:
            rec = il.draw(il.job("portrait", x, at=first[x]))
        except (art.NotVisible, ValueError) as e:
            print(f"no portrait of {x}: {e}", file=sys.stderr)
            continue
        if rec:
            drawn[x] = rec
    rows = []
    for (eid, kind), evs in plan.items():
        if not evs:
            continue
        own = il.draw(il.job(kind, eid, at=evs[0]["from"]))
        scenes = []
        for ev in evs:
            job = il.job("event", ev["id"])
            try:
                pr = il.make_prompt(job)
            except ValueError as e:
                scenes.append({"event": ev["id"], "summary": ev["summary"], "skipped": str(e)})
                continue
            got = {}
            for mode in ("without", "with"):
                p = copy.deepcopy(pr)
                if mode == "without":
                    p.references = [(j, r) for j, r in p.references if r.get("kind") not in art.SETTING_KINDS]
                rec = il.draw(job, p)
                got[mode] = {"file": rec["file"] if rec else None, "references": rec["references"] if rec else [],
                             "note": image.notes[-1] if rec and image.notes else "", "cost": rec["cost"] if rec else 0}
            scenes.append({"event": ev["id"], "summary": ev["summary"], "setting": (ev.get("setting") or {}).get("text"),
                           "figures": pr.figures, "prompt": pr.text, "writer": pr.writer, **got})
        rows.append({"entity": eid, "kind": kind, "name": name.get(eid, eid), "picture": own["file"] if own else None,
                     "prompt": own["prompt"] if own else "", "scenes": scenes})
    cost = {"pictures": round(il.report.cost, 4), "text": round(il.text_spent(), 4)}
    cost["total"] = round(cost["pictures"] + cost["text"], 4)
    result = {"index": a.index, "style": a.style, "quality": a.quality, "image_model": image.name, "cost": cost,
              "portraits": {x: r["file"] for x, r in drawn.items()}, "rows": rows, "report": il.report.lines()}
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (out / "index.html").write_text(sheet(result, name))
    print(json.dumps(cost))
    return 0


def sheet(r: dict, name: dict) -> str:
    e = html.escape
    img = lambda f, w=300: f'<img src="set/{e(f)}" width="{w}">' if f else "<i>not drawn</i>"
    parts = [f"<!doctype html><meta charset=utf-8><title>Scene places</title><style>body{{font:13px system-ui;margin:24px;"
             f"background:#111;color:#ddd}}td{{vertical-align:top;padding:6px}}details{{max-width:620px}}h2{{margin-top:40px}}"
             f"</style><h1>Scene places and things: with and without their picture as a reference</h1>"
             f"<p>{e(r['index'])}, style {e(r['style'])}, {e(r['quality'])}, {e(r['image_model'])}. Cost ${r['cost']['total']:.2f} "
             f"(pictures ${r['cost']['pictures']:.2f}, text ${r['cost']['text']:.2f}).</p><h2>Figures</h2><p>"]
    parts += [f'<span style="display:inline-block;margin:4px">{img(f, 160)}<br>{e(name.get(x, x))}</span>'
              for x, f in r["portraits"].items()]
    parts.append("</p>")
    for row in r["rows"]:
        parts.append(f"<h2>{e(row['kind'].title())}: {e(row['name'])}</h2><table><tr><td>{img(row['picture'])}<br>"
                     f"<details><summary>its prompt</summary>{e(row['prompt'])}</details></td></tr></table><table>"
                     "<tr><th>moment</th><th>without its picture</th><th>with its picture</th></tr>")
        for s in row["scenes"]:
            if s.get("skipped"):
                parts.append(f"<tr><td>{e(s['summary'])}</td><td colspan=2>skipped: {e(s['skipped'])}</td></tr>")
                continue
            figs = ", ".join(f"{f['label']} = {name.get(f['entity'], f['entity'])}" for f in s["figures"] or []) or "none"
            parts.append(f"<tr><td width=260><b>{e(s['event'])}</b> {e(s['summary'])}<br><i>at {e(s['setting'] or '?')}</i><br>"
                         f"figures: {e(figs)}<details><summary>prompt</summary>{e(s['prompt'])}</details>"
                         f"<details><summary>note sent (with)</summary>{e(s['with']['note'])}</details></td>"
                         f"<td>{img(s['without']['file'], 420)}</td><td>{img(s['with']['file'], 420)}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


if __name__ == "__main__":
    sys.exit(main())
