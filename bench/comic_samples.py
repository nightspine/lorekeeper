"""Comic pages of a few chapters, with the main figures' portraits drawn first so the page labels them, and a contact sheet.

    set -a; . ./.env; set +a
    uv run --extra claude python bench/comic_samples.py INDEX --epub EPUB --out DIR \
        --chapter N[:FRACTION] [--chapter ...] [--portraits 2] [--style comic-strip] [--quality draft] [--cap 1.50]

Each `--chapter` is a plan number (`lorekeeper toc`; Dracula's Chapter I is 4), optionally with the fraction of its events the
reader has reached ("19:0.5": the current chapter so far). For each, the `--portraits` people its panels name most get a portrait
at the chapter's anchor (so the page has labelled figures with references), then one comic page is drawn with
`Illustrator.job("comic", ...)`. Everything goes into DIR: the art set (`set/`), `results.json` (panels, prompts, notes, costs)
and `index.html`, a contact sheet with each page's captions under it. The writer is Claude's strong model (the on-demand tier),
the pictures Gemini's default image model unless `--image-model` says otherwise.
"""

from __future__ import annotations

import argparse
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


class Keeping:
    """The writer, keeping every comic reply (for the sheet and for a page that was dropped)."""

    def __init__(self, model):
        self.model, self.replies = model, []
        self.usage = getattr(model, "usage", None)
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def __getattr__(self, k):
        return getattr(self.model, k)

    def complete(self, system, prompt, schema, *args, **kw):
        out = self.model.complete(system, prompt, schema, *args, **kw)
        if system == art.COMIC_WRITER:
            self.replies.append(out)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chapter", action="append", required=True)
    ap.add_argument("--portraits", type=int, default=2)
    ap.add_argument("--style", default="comic-strip")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--image-model")
    ap.add_argument("--cap", type=float, default=1.50)
    a = ap.parse_args()

    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sink = art.FileSink(out / "set")
    text = Keeping(llm.make("claude", llm.PROVIDERS["claude"]["strong"], api_key=llm.env_key("claude")))
    cheap = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", a.image_model, api_key=llm.env_key("gemini")))
    il = art.Illustrator([art.Book(doc, bk, sink, None)], text, image, a.style, portraits=None, quality=a.quality, cap=a.cap,
                         cheap_text=cheap, workers=1, log=lambda m: print(m, file=sys.stderr))
    b = il.books[0]
    results = []
    for spec in a.chapter:
        n, _, frac = spec.partition(":")
        ch = next(c for c in b.chapters if c["n"] == int(n))
        evs = sorted((e for e in ch["events"] if e.get("kind") != "minor"), key=lambda e: b.ord_of[e["from"]])
        at = ch["last"] if not frac else evs[max(1, round(float(frac) * len(evs))) - 1]["from"]
        panels = il.comic_panels(0, ch, at)
        named = Counter(x for eid in panels for x in b.events[eid]["entities"] if b.ents.get(x, {}).get("type") == "person")
        portraits = []
        for eid, _ in named.most_common(a.portraits):
            got = il.draw(il.job("portrait", eid, at=at, looks="cached"))
            if got:
                portraits.append({"entity": eid, "name": b.ents[eid]["names"][0]["value"], "file": got["file"],
                                  "cost": got["cost"]})
        job = il.job("comic", ch["title"], at=at)
        before = len(text.replies)
        try:
            pr = il.make_prompt(job)
        except art.Dropped as e:
            print(f"{ch['title']}: {e} ({e.detail})", json.dumps(text.replies[before:], indent=1), file=sys.stderr)
            results.append({"chapter": ch["title"], "at": at, "dropped": f"{e} ({e.detail})", "replies": text.replies[before:]})
            continue
        rec = il.draw(job, pr)
        results.append({"chapter": ch["title"], "at": at, "panels": [{"id": x, "summary": b.events[x]["summary"],
                                                                     "kind": b.events[x].get("kind")} for x in job.panels],
                        "portraits": portraits, "prompt": pr.text, "figures": rec and rec.get("figures"),
                        "note": image.notes[-1] if image.notes else "", "file": rec and rec["file"],
                        "cost": rec and rec["cost"], "replies": text.replies[before:]})
        print(f"{ch['title']}: {len(job.panels)} panels, {rec and rec['file']}", file=sys.stderr)
    text_cost = il.text_spent()
    (out / "results.json").write_text(json.dumps({"results": results, "report": il.report.lines(), "text": text_cost},
                                                 indent=2, ensure_ascii=False))
    rows = []
    for r in results:
        if r.get("dropped"):
            rows.append(f"<section><h2>{html.escape(r['chapter'])}</h2><p>{html.escape(r['dropped'])}</p></section>")
            continue
        caps = "".join(f"<li>{html.escape(p['summary'])}</li>" for p in r["panels"])
        faces = "".join(f'<figure><img src="set/{html.escape(p["file"])}" width="160"><figcaption>{html.escape(p["name"])}'
                        f"</figcaption></figure>" for p in r["portraits"])
        page = f'<img src="set/{html.escape(r["file"])}" width="560">' if r["file"] else "<p>not drawn</p>"
        rows.append(f"<section><h2>{html.escape(r['chapter'])} (to {html.escape(r['at'])})</h2><div class=row>{page}"
                    f"<div><ol>{caps}</ol><div class=row>{faces}</div><details><summary>Prompt</summary>"
                    f"<p>{html.escape(r['prompt'])}</p><p><i>{html.escape(r['note'])}</i></p></details></div></div></section>")
    (out / "index.html").write_text("<!doctype html><meta charset=utf-8><title>Comic pages</title><style>body{font:14px sans-serif;"
                                    "margin:24px}.row{display:flex;gap:16px;align-items:flex-start}figure{margin:0}"
                                    "</style>" + "".join(rows) + f"<pre>{html.escape(chr(10).join(il.report.lines()))}</pre>")
    print("\n".join(il.report.lines()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
