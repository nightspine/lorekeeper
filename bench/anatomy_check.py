"""How often does the drawing-fault check flag a picture, what does it cost, and does a fix by edit keep the picture better
than a redraw with the faults to avoid?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/anatomy_check.py --data ~/projects/lorekeeper/lorekeeper-data [--book KEY]... \
        [--limit 40] [--clean 5] [--picker claude] [--also-picker gemini] [--cap 5] --out DIR
    # no pictures there yet: draw some first, into DIR only (INDEX:EPUB, N scenes each)
    uv run --extra claude python bench/anatomy_check.py --draw examples/dracula/dracula.json:corpus/pg345/pg345.epub \
        --draw INDEX:EPUB --per-book 10 --image gemini --quality draft --out DIR

The pictures are read from the data dir's art sets (`art/<textHash>/<style>/art.json`; `--book` keeps the sets of those
library keys' editions) or drawn into DIR/sets by the pipeline (Claude's cheap model writes, `--image` draws, no portraits).
Every input is copied to DIR/img; nothing is ever written into the data dir. Each picture is checked by `--picker` (the
product's picker) and, for comparison, by every `--also-picker`. The ones the picker flags, and `--clean` that it calls clean,
are fixed both ways on the image model and quality they were drawn with: an edit (`art.fix_prompt`, the picture its one
reference: what the product does) and a redraw (the stored prompt plus "Avoid: <faults>", with the references it was drawn
with). Each fixed picture is checked again (measurement only: the product never re-checks) and the picker scores how much of
the original it keeps (0 to 10). DIR gets `index.html`, a contact sheet, and `summary.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lorekeeper import art, epub, llm

KEEP = """You compare two pictures. Picture 1 is an original; picture 2 is meant to be the same picture with a few drawing faults corrected.
Score how much of picture 1 picture 2 keeps: the composition and framing, the faces, the clothing, the colours, the light and the drawing style. 10 is the same picture with only small corrections; 5 keeps the scene and figures but redraws them; 0 is a different picture.
Return "score" (0 to 10) and "changed": one short phrase on what changed most."""
KEEP_SCHEMA = {"type": "object", "required": ["score", "changed"],
               "properties": {"score": {"type": "integer"}, "changed": {"type": "string"}}}
CLEAN_NOTE = "look at every figure's hands, arms and head, and correct any that are drawn wrong"


def text_model(spec: str, tier: str = "cheap"):
    provider, _, model = spec.partition(":")
    model = model or llm.PROVIDERS[provider][tier]
    return llm.make(provider, model, llm.env_key(provider))


class Spend:
    def __init__(self, cap: float, models: list):
        self.cap, self.models, self.images, self.held = cap, models, 0.0, 0.0

    def text(self) -> float:
        return sum(m.usage.cost_usd for m in self.models)

    def total(self) -> float:
        return self.images + self.text()

    def room(self, price: float) -> bool:
        return self.total() + price <= self.cap


def library_sets(data: Path, keys: list[str]) -> list[Path]:
    """The art set folders of the data dir, only those of the given library keys' editions when any are named."""
    root = data / "art"
    sets = sorted(p.parent for p in root.glob("*/*/art.json")) if root.is_dir() else []
    if not keys:
        return sets
    hashes = set()
    for k in keys:
        f = data / "indexes" / f"{k}.lore.json"
        doc = json.loads(f.read_text())
        hashes |= {e["textHash"].removeprefix("sha256:") for e in doc.get("editions", []) if e.get("builtFrom")}
    return [s for s in sets if s.parent.name.removeprefix("sha256:") in hashes]


def draw_sets(pairs: list[str], out: Path, per_book: int, image_spec: str, quality: str, writer, picker, spend: Spend,
              log) -> list[Path]:
    """Draw `per_book` chapter pictures of each INDEX:EPUB into out/sets, with no portraits and no check."""
    sets = []
    for pair in pairs:
        index, _, ep = pair.partition(":")
        doc = json.loads(Path(index).read_text())
        bk = epub.read(ep)
        folder = out / "sets" / Path(index).name.split(".")[0] / "ink"
        image = llm.make_image(image_spec.partition(":")[0], image_spec.partition(":")[2] or None)
        room = spend.cap - spend.total()
        il = art.Illustrator([art.Book(doc, bk, art.FileSink(folder), "main")], writer, image, "ink", portraits=False,
                             quality=quality, cap=room, max_pictures=per_book, log=log, cheap_text=picker)
        rep = il.run()
        spend.images += rep.cost
        log(f"{doc['work']['title']}: " + "; ".join(rep.lines()[:1]))
        sets.append(folder)
    return sets


def copy_in(rec: dict, folder: Path, img: Path) -> Path | None:
    src = folder / rec["file"]
    if not src.is_file():
        return None
    dst = img / f"{folder.parent.name[:10]}-{rec['file']}"
    shutil.copyfile(src, dst)
    return dst


def save(img: Path, name: str, pic) -> Path:
    p = img / f"{name}.{art._ext(pic.mime)}"
    p.write_bytes(pic.data)
    return p


def keep_score(model, a: bytes, am: str, b: bytes, bm: str) -> dict:
    try:
        out = model.complete(KEEP, "Picture 1, then picture 2, are above.", KEEP_SCHEMA, images=[(a, am), (b, bm)])
        return {"score": int(out.get("score")), "changed": str(out.get("changed") or "")[:160]}
    except Exception as e:     # noqa: BLE001 - a measurement only
        return {"score": None, "changed": f"failed: {type(e).__name__}: {str(e)[:120]}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=Path.home() / "projects/lorekeeper/lorekeeper-data")
    ap.add_argument("--book", action="append", default=[], help="library key (index file name without .lore.json)")
    ap.add_argument("--draw", action="append", default=[], help="INDEX:EPUB to draw scenes of when no set has pictures")
    ap.add_argument("--per-book", type=int, default=10)
    ap.add_argument("--image", default="gemini", help="PROVIDER[:MODEL] that draws with --draw")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--writer", default="claude", help="PROVIDER[:MODEL] that writes the prompts with --draw")
    ap.add_argument("--picker", default="claude", help="PROVIDER[:MODEL] of the check (the product's picker)")
    ap.add_argument("--also-picker", action="append", default=[], help="more pickers, checking the originals for comparison")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--clean", type=int, default=5)
    ap.add_argument("--cap", type=float, default=5.0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    out, img = a.out, a.out / "img"
    img.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(m):
        lines.append(str(m))
        print(m, flush=True)

    picker = text_model(a.picker)
    others = [text_model(s) for s in a.also_picker]
    writer = text_model(a.writer)
    spend = Spend(a.cap, [picker, writer, *others])
    sets = library_sets(a.data, a.book) if a.data.is_dir() else []
    sets = [s for s in sets if json.loads((s / "art.json").read_text()).get("pictures")]
    source = "library"
    if not sets:
        if not a.draw:
            print("no pictures in the data dir's art sets: give --draw INDEX:EPUB", file=sys.stderr)
            return 2
        sets, source = draw_sets(a.draw, out, a.per_book, a.image, a.quality, writer, picker, spend, log), "drawn"
    recs = []
    for s in sets:
        man = json.loads((s / "art.json").read_text())
        for r in man.get("pictures", []):
            if r.get("kind") in ("plate", "event", "portrait", "place", "thing") and r.get("prompt"):
                recs.append((s, r))
    recs = recs[:a.limit]
    log(f"{len(recs)} pictures from {len(sets)} sets ({source})")

    rows = []
    check_costs = []
    for s, r in recs:
        p = copy_in(r, s, img)
        if p is None:
            continue
        data, mime = p.read_bytes(), r.get("mime") or "image/png"
        before = picker.usage.cost_usd
        tokens0 = (picker.usage.prompt_tokens, picker.usage.output_tokens)
        c = art.check_picture(picker, data, mime, r["prompt"])
        check_costs.append(picker.usage.cost_usd - before)
        row = {"id": r["id"], "set": str(s), "kind": r["kind"], "target": r["target"], "provider": r.get("provider"),
               "model": r.get("model"), "quality": r.get("quality"), "shape": r.get("shape"), "prompt": r["prompt"],
               "file": p.name, "checked": c.checked, "why": c.why, "faults": c.faults, "check_cost": c.cost,
               "check_tokens": [picker.usage.prompt_tokens - tokens0[0], picker.usage.output_tokens - tokens0[1]],
               "also": []}
        for m in others:
            try:
                o = art.check_picture(m, data, mime, r["prompt"])
                row["also"].append({"picker": m.name, "checked": o.checked, "faults": o.faults, "cost": o.cost, "why": o.why})
            except llm.NoVision as e:
                row["also"].append({"picker": m.name, "checked": False, "faults": [], "why": str(e)[:160]})
        rows.append(row)
        log(f"  {r['id']} {r['kind']} {r['target'][:30]}: " + ("not checked: " + c.why if not c.checked else
                                                                 f"{len(c.faults)} faults " + "; ".join(f["what"] for f in c.faults)))

    flagged = [x for x in rows if x["faults"]]
    clean = [x for x in rows if x["checked"] and not x["faults"]][:a.clean]
    lock = threading.Lock()

    def fix_both(x: dict) -> None:
        s = Path(x["set"])
        man = json.loads((s / "art.json").read_text())
        rec = next(r for r in man["pictures"] if r["id"] == x["id"])
        prov = rec.get("provider") if rec.get("provider") in llm.IMAGE_PROVIDERS else a.image.partition(":")[0]
        image = llm.make_image(prov, rec.get("model") if llm.image_model(prov, rec.get("model") or "") else None)
        quality = llm.nearest_quality(prov, image.name, rec.get("quality") or a.quality)
        shape = rec.get("shape") or art.SHAPE_OF[rec["kind"]]
        price = llm.image_price(prov, image.name, quality, shape) or 0.05
        orig = (img / x["file"]).read_bytes()
        mime = rec.get("mime") or "image/png"
        refs = [(fb, r2.get("mime") or "image/png") for ref in rec.get("references") or [] if isinstance(ref, str)
                for r2 in man["pictures"] if r2["id"] == ref and (fb := (s / r2["file"]).read_bytes() if (s / r2["file"]).is_file() else None)]
        faults, note = (x["faults"], "") if x["faults"] else ([], CLEAN_NOTE)
        avoid = "; ".join(f"{f['where']}: {f['what']}" if f["where"] else f["what"] for f in faults) or "badly drawn hands, arms or heads"
        ways = {
            "edit": (art.fix_prompt(faults, note), [(orig, mime)]),
            "redraw": (f"{rec['prompt']} Avoid: {avoid}.", refs[:4]),
        }
        x["fixes"] = {}
        for way, (prompt, refs_) in ways.items():
            with lock:
                if not spend.room(price + 0.01 + spend.held):
                    log(f"  cap reached before the {way} of {x['id']}")
                    x["fixes"][way] = {"skipped": "cap"}
                    continue
                spend.held += price
            t = time.time()
            try:
                pic = image.draw(prompt, shape=shape, quality=quality, references=refs_)
            except Exception as e:     # noqa: BLE001 - recorded, the bench goes on
                with lock:
                    spend.held -= price
                    spend.images += price      # it may have been billed
                x["fixes"][way] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                log(f"  {way} of {x['id']} failed: {e}")
                continue
            with lock:
                spend.held -= price
                spend.images += pic.cost or price
            name = f"{x['id']}-{way}-" + hashlib.sha256(pic.data).hexdigest()[:6]
            f = save(img, name, pic)
            re_ = art.check_picture(picker, pic.data, pic.mime, rec["prompt"])
            keep = keep_score(picker, orig, mime, pic.data, pic.mime)
            x["fixes"][way] = {"file": f.name, "cost": pic.cost, "seconds": round(time.time() - t, 1), "prompt": prompt,
                               "recheck": {"checked": re_.checked, "faults": re_.faults}, "keep": keep}
            log(f"  {way} {x['id']}: recheck {len(re_.faults)} faults, keep {keep['score']} ({keep['changed']})")

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(fix_both, flagged + clean))

    checked = [x for x in rows if x["checked"]]

    def rate(way: str, group: list[dict]) -> dict:
        done = [x["fixes"][way] for x in group if x.get("fixes", {}).get(way, {}).get("file")]
        scores = [d["keep"]["score"] for d in done if isinstance(d["keep"]["score"], int)]
        return {"fixed": len(done), "clean_on_recheck": sum(1 for d in done if d["recheck"]["checked"] and not d["recheck"]["faults"]),
                "keep_mean": round(statistics.mean(scores), 2) if scores else None,
                "keep_median": statistics.median(scores) if scores else None,
                "cost_mean": round(statistics.mean(d["cost"] for d in done), 4) if done else None}

    tok = [x["check_tokens"] for x in checked]
    summary = {
        "source": source, "sets": [str(s) for s in sets], "picker": {"provider": a.picker, "model": picker.name},
        "also_pickers": [m.name for m in others], "pictures": len(rows), "checked": len(checked),
        "flagged": len(flagged), "fault_rate": round(len(flagged) / len(checked), 3) if checked else None,
        "faults": [f for x in flagged for f in x["faults"]],
        "check_cost_per_100": round(100 * statistics.mean(check_costs), 4) if check_costs else None,
        "check_tokens_mean": [round(statistics.mean(t[0] for t in tok)), round(statistics.mean(t[1] for t in tok))] if tok else None,
        "flagged_fixes": {w: rate(w, flagged) for w in ("edit", "redraw")},
        "clean_fixes": {w: rate(w, clean) for w in ("edit", "redraw")},
        "also": {m.name: {"flagged": sum(1 for x in rows for o in x["also"] if o["picker"] == m.name and o["faults"]),
                          "agree_flag": sum(1 for x in rows for o in x["also"] if o["picker"] == m.name and bool(o["faults"]) == bool(x["faults"]) and o["checked"]),
                          "checked": sum(1 for x in rows for o in x["also"] if o["picker"] == m.name and o["checked"]),
                          "cost": round(sum(o.get("cost") or 0 for x in rows for o in x["also"] if o["picker"] == m.name), 4)}
                 for m in others},
        "spent": {"images": round(spend.images, 4), "text": round(spend.text(), 4), "total": round(spend.total(), 4)},
    }
    (out / "summary.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    (out / "log.txt").write_text("\n".join(lines))
    (out / "index.html").write_text(sheet(summary, rows))
    print(json.dumps(summary, indent=1))
    return 0


def faults_html(faults: list[dict]) -> str:
    if not faults:
        return "<p class=ok>No drawing faults found.</p>"
    return "<ul>" + "".join(f"<li>{html.escape(f['what'])}<br><small>{html.escape(f['where'])}</small></li>" for f in faults) + "</ul>"


def sheet(summary: dict, rows: list[dict]) -> str:
    e = html.escape
    head = (f"<h1>Drawing-fault check</h1><p>{summary['pictures']} pictures, {summary['flagged']} flagged by {e(summary['picker']['model'])}"
            f" (rate {summary['fault_rate']}); check ${summary['check_cost_per_100']} per 100 pictures; spent ${summary['spent']['total']}.</p>"
            f"<pre>{e(json.dumps({k: summary[k] for k in ('flagged_fixes', 'clean_fixes', 'also')}, indent=1))}</pre>")
    body = []
    for x in rows:
        cells = [f"<td><img src='img/{e(x['file'])}'><p><b>original</b> {e(x['kind'])} {e(x['target'][:40])} "
                 f"({e(str(x['model']))}, {e(str(x['quality']))})</p>{faults_html(x['faults']) if x['checked'] else '<p>not checked: ' + e(x['why']) + '</p>'}"
                 + "".join(f"<p><small>{e(o['picker'])}: {len(o['faults'])} faults: {e('; '.join(f['what'] for f in o['faults']))}</small></p>" for o in x["also"])
                 + f"<details><summary>prompt</summary><small>{e(x['prompt'])}</small></details></td>"]
        for way in ("edit", "redraw"):
            d = (x.get("fixes") or {}).get(way)
            if not d:
                continue
            if not d.get("file"):
                cells.append(f"<td><p>{e(way)}: {e(d.get('error') or d.get('skipped') or '')}</p></td>")
                continue
            cells.append(f"<td><img src='img/{e(d['file'])}'><p><b>{e(way)}</b> keeps {d['keep']['score']}/10: "
                         f"{e(d['keep']['changed'])}</p><p>re-check:</p>{faults_html(d['recheck']['faults'])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return ("<!doctype html><meta charset=utf-8><title>anatomy check</title><style>body{font:13px system-ui;background:#111;color:#ddd}"
            "td{vertical-align:top;width:33%;padding:6px}img{width:100%}ul{margin:4px 0;padding-left:16px}.ok{color:#7c7}"
            "small{color:#999}pre{font-size:11px}</style>" + head + "<table>" + "".join(body) + "</table>")


if __name__ == "__main__":
    sys.exit(main())
