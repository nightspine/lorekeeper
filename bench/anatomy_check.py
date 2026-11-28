"""How often does the drawing-fault check flag a picture, how often is a fix by edit put right, and how often does the
verification of a fix call it unchanged or partial?

    set -a; . ~/projects/lorekeeper/.env; set +a
    uv run --extra claude python bench/anatomy_check.py --sets .claude/work/anatomy-check/bench/sets \
        --picker claude --strong claude:claude-sonnet-5-5 --old --cap 4 --out DIR
    uv run --extra claude python bench/anatomy_check.py --data ~/projects/lorekeeper/lorekeeper-data [--book KEY]... --out DIR
    # no pictures yet: draw some first, into DIR only (INDEX:EPUB, N scenes each)
    uv run --extra claude python bench/anatomy_check.py --draw examples/dracula/dracula.json:corpus/pg345/pg345.epub \
        --per-book 10 --image gemini --quality draft --out DIR

The pictures are read from `--sets` (every `art.json` under that folder: an earlier bench's sets), else from the data dir's
art sets (`art/<textHash>/<style>/art.json`; `--book` keeps the sets of those library keys' editions), else drawn into
DIR/sets by the pipeline (Claude's cheap model writes, `--image` draws, no portraits). Every input is copied to DIR/img;
nothing is ever written into the data dir or `--sets`. Each picture is checked by `--picker` (what a run's automatic check
uses) and `--strong` (the lightbox's default checker) with `art.ANATOMY`, and with `--old` also with the check prompt of
0.27.0 (`OLD_ANATOMY`, frozen here) for comparison. The pictures `--strong` flags are fixed by edit on the image model and
quality they were drawn with, with every fault it found (what the lightbox sends), `--clean` of the ones it calls clean
with a general note; with `--redraw` also by a redraw with the faults to avoid. Each edit is verified (`art.verify_fix`) by
both checkers, checked again by `--strong`, and scored by the picker on how much of the original it keeps (0 to 10). DIR
gets `index.html`, a contact sheet, and `summary.json`.
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

# the check prompt of 0.27.0, frozen here so a bench can compare the two
OLD_ANATOMY = """You check a picture an image model drew for a reader's illustrated companion to a novel, for gross drawing faults only: the ones a reader would notice at a glance.

Report only these:
- an extra or a missing hand, arm, leg or head;
- a limb attached to the wrong body or in the wrong place;
- bodies fused or merged into one another;
- a head or a body far out of proportion to the rest of the figure (a head far too small for the body, an arm far too long);
- a hand with clearly wrong fingers (too many, too few, melted together);
- one person the prompt describes drawn twice.

Never report style, taste, mood, composition or colour, a detail the prompt asks for that is merely small, how well anyone resembles anyone, or text and lettering. These are not faults either: a pose or a long reach, foreshortening, a limb or hand hidden behind something or cut off by the frame, a hand that is small or rough because the drawing style is loose, a crowd or a team of animals whose legs overlap but can be counted. The prompt the picture was drawn from comes with it: what the prompt asks for is never a fault (a figure the prompt says has lost an arm is drawn without it).
Report a fault only when you can point to it and are sure of it; when unsure, it is not a fault. Most pictures have none.

For each fault, "what" is a short phrase saying what is wrong, written so it can be handed to the image model as something to correct ("the head is far too small for the body and robe", "the left hand has six fingers"), and "where" names the figure by how it looks or by its label from the prompt ("figure B, the robed man on the right"), never by a name. At most 5 faults, the most visible first. A picture with none of these faults gets an empty list.
Return "faults"."""
OLD_ANATOMY_SCHEMA = {"type": "object", "required": ["faults"], "properties": {"faults": {"type": "array", "items": {
    "type": "object", "required": ["what", "where"],
    "properties": {"what": {"type": "string"}, "where": {"type": "string"}}}}}}
OLD_ANATOMY_PROMPT = """The picture above was drawn from this prompt (context only: what it asks for is not a fault):
<prompt>{prompt}</prompt>

List its gross drawing faults, if any."""


def text_model(spec: str, tier: str = "cheap"):
    provider, _, model = spec.partition(":")
    model = model or llm.PROVIDERS[provider][tier]
    return llm.make(provider, model, llm.env_key(provider))


class Spend:
    def __init__(self, cap: float, models: list):
        self.cap, self.models, self.images, self.held = cap, models, 0.0, 0.0

    def text(self) -> float:
        return sum(m.usage.cost_usd for m in self.models) + self.calls

    calls = 0.0     # text spent on per-call models (`Calls`)

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



def sets_under(root: Path) -> list[Path]:
    """Every art set folder under `root` (an earlier bench's DIR/sets)."""
    return sorted(p.parent for p in root.rglob("art.json"))


def old_check(model, data: bytes, mime: str, prompt: str) -> art.Check:
    """The check as 0.27.0 made it (OLD_ANATOMY, no stock-taking, no severity), for comparison only."""
    before = art._cost_of(model)
    prompt = " ".join(str(prompt or "").split())[:art.CHECK_PROMPT_CHARS]
    try:
        out = model.complete(OLD_ANATOMY, OLD_ANATOMY_PROMPT.format(prompt=prompt), OLD_ANATOMY_SCHEMA, images=[(data, mime)])
    except Exception as e:     # noqa: BLE001 - a measurement only
        return art.Check([], False, art._cost_of(model) - before, f"{type(e).__name__}: {str(e)[:160]}")
    cost = art._cost_of(model) - before
    if not isinstance(out, dict) or not isinstance(out.get("faults"), list):
        return art.Check([], False, cost, "no list of faults in the reply")
    return art.Check([{k: v for k, v in f.items() if k != "severity"} for f in art.clean_faults(out["faults"], limit=5)],
                     True, cost)


def tokens(model) -> tuple[int, int]:
    return model.usage.prompt_tokens, model.usage.output_tokens


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", type=Path, help="a folder of art sets (every art.json under it), read only")
    ap.add_argument("--data", type=Path, default=Path.home() / "projects/lorekeeper/lorekeeper-data")
    ap.add_argument("--book", action="append", default=[], help="library key (index file name without .lore.json)")
    ap.add_argument("--draw", action="append", default=[], help="INDEX:EPUB to draw scenes of when no set has pictures")
    ap.add_argument("--per-book", type=int, default=10)
    ap.add_argument("--image", default="gemini", help="PROVIDER[:MODEL] that draws with --draw")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--writer", default="claude", help="PROVIDER[:MODEL] that writes the prompts with --draw")
    ap.add_argument("--picker", default="claude", help="PROVIDER[:MODEL] of a run's automatic check (its cheap tier)")
    ap.add_argument("--strong", default="claude:claude-sonnet-5-5", help="PROVIDER[:MODEL] of the lightbox's check")
    ap.add_argument("--old", action="store_true", help="also check with the 0.27.0 prompt")
    ap.add_argument("--redraw", action="store_true", help="also fix by a redraw with the faults to avoid")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--clean", type=int, default=0)
    ap.add_argument("--no-fix", action="store_true", help="check only")
    ap.add_argument("--cap", type=float, default=4.0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    out, img = a.out, a.out / "img"
    img.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(m):
        lines.append(str(m))
        print(m, flush=True)

    picker = text_model(a.picker)
    strong = text_model(a.strong, "strong")
    checkers = {"picker": picker, "strong": strong}
    specs = {"picker": (a.picker, "cheap"), "strong": (a.strong, "strong")}
    lock = threading.Lock()

    def fresh(who: str):
        """A model of its own for one call, so its usage is that call's tokens and cost (the bench runs calls in threads)."""
        return text_model(*specs[who])

    def spent(m) -> dict:
        with lock:
            spend.calls += m.usage.cost_usd
        return {"cost": m.usage.cost_usd, "tokens": [m.usage.prompt_tokens, m.usage.output_tokens]}
    writer = text_model(a.writer)
    spend = Spend(a.cap, [picker, strong, writer])
    if a.sets:
        sets, source = sets_under(a.sets), "sets"
    else:
        sets, source = (library_sets(a.data, a.book) if a.data.is_dir() else []), "library"
    sets = [s for s in sets if json.loads((s / "art.json").read_text()).get("pictures")]
    if not sets:
        if not a.draw:
            print("no pictures in the art sets: give --sets DIR or --draw INDEX:EPUB", file=sys.stderr)
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

    def check_one(sr) -> dict | None:
        s, r = sr
        p = copy_in(r, s, img)
        if p is None:
            return None
        data, mime = p.read_bytes(), r.get("mime") or "image/png"
        row = {"id": r["id"], "set": str(s), "kind": r["kind"], "target": r["target"], "provider": r.get("provider"),
               "model": r.get("model"), "quality": r.get("quality"), "shape": r.get("shape"), "prompt": r["prompt"],
               "file": p.name, "checks": {}}
        for who in checkers:
            for way in ("new", "old") if a.old else ("new",):
                with lock:
                    if not spend.room(0.03):
                        row["checks"][f"{who}/{way}"] = {"checked": False, "why": "cap", "faults": []}
                        continue
                m = fresh(who)
                c = (art.check_picture if way == "new" else old_check)(m, data, mime, r["prompt"])
                row["checks"][f"{who}/{way}"] = {"checked": c.checked, "why": c.why, "faults": c.faults, **spent(m)}
        got = row["checks"].get("strong/new", {})
        log(f"  {r['id']} {r['kind']} {r['target'][:30]}: " + "; ".join(
            f"{k} {len(v['faults'])}" + ("" if v["checked"] else " (not checked)") for k, v in row["checks"].items())
            + (": " + "; ".join(f"[{f['severity']}] {f['what']}" for f in got.get("faults", [])) if got.get("faults") else ""))
        return row

    with ThreadPoolExecutor(4) as pool:
        rows = [x for x in pool.map(check_one, recs) if x]

    def strong_faults(x):
        return x["checks"].get("strong/new", {}).get("faults") or []

    flagged = [x for x in rows if strong_faults(x)]
    clean = [x for x in rows if x["checks"].get("strong/new", {}).get("checked") and not strong_faults(x)][:a.clean]

    def fix_one(x: dict) -> None:
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
        faults = strong_faults(x)
        note = "" if faults else CLEAN_NOTE
        refs = [(fb, r2.get("mime") or "image/png") for ref in rec.get("references") or [] if isinstance(ref, str)
                for r2 in man["pictures"] if r2["id"] == ref and (fb := (s / r2["file"]).read_bytes() if (s / r2["file"]).is_file() else None)]
        avoid = "; ".join(f"{f['where']}: {f['what']}" if f["where"] else f["what"] for f in faults) or "badly drawn hands, arms or heads"
        ways = {"edit": (art.fix_prompt(faults, note), [(orig, mime)])}
        if a.redraw:
            ways["redraw"] = (f"{rec['prompt']} Avoid: {avoid}.", refs[:4])
        x["fixes"] = {}
        for way, (prompt, refs_) in ways.items():
            with lock:
                if not spend.room(price + 0.08 + spend.held):
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
            d = {"file": f.name, "cost": pic.cost, "seconds": round(time.time() - t, 1), "prompt": prompt,
                 "identical": pic.data == orig, "verify": {}}
            for who in checkers:
                m = fresh(who)
                v = art.verify_fix(m, (orig, mime), (pic.data, pic.mime or "image/png"), faults, note)
                d["verify"][who] = {"checked": v.checked, "why": v.why, "changed": v.changed, "results": v.results,
                                    "new": v.new, "unchanged": v.unchanged(), **spent(m),
                                    **({"counts": v.counts()} if v.checked else {})}
            m = fresh("strong")
            re_ = art.check_picture(m, pic.data, pic.mime, rec["prompt"])
            d["recheck"] = {"checked": re_.checked, "faults": re_.faults, **spent(m)}
            d["keep"] = keep_score(picker, orig, mime, pic.data, pic.mime)
            x["fixes"][way] = d
            vs = d["verify"]["strong"]
            log(f"  {way} {x['id']}: " + ("identical bytes; " if d["identical"] else "")
                + (f"strong verdict {[r['fixed'] for r in vs['results']]} changed={vs['changed']} new={len(vs['new'])}"
                   if vs["checked"] else f"strong verify failed ({vs['why']})")
                + f"; picker verdict {[r['fixed'] for r in d['verify']['picker']['results']]}"
                + f"; recheck {len(re_.faults)} faults, keep {d['keep']['score']}")

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(fix_one, [] if a.no_fix else flagged + clean))

    def stats(key: str) -> dict:
        got = [x["checks"][key] for x in rows if key in x["checks"]]
        ok = [c for c in got if c["checked"]]
        fl = [c for c in ok if c["faults"]]
        faults = [f for c in fl for f in c["faults"]]
        return {"checked": len(ok), "flagged": len(fl), "flag_rate": round(len(fl) / len(ok), 3) if ok else None,
                "with_major": sum(1 for c in fl if any(f.get("severity") == "major" for f in c["faults"])),
                "major_rate": round(sum(1 for c in fl if any(f.get("severity") == "major" for f in c["faults"])) / len(ok), 3)
                if ok else None,
                "faults": len(faults), "major": sum(f.get("severity") == "major" for f in faults),
                "minor": sum(f.get("severity") == "minor" for f in faults),
                "cost_per_check": round(statistics.mean(c["cost"] for c in ok), 5) if ok else None,
                "tokens_mean": [round(statistics.mean(c["tokens"][i] for c in ok)) for i in (0, 1)] if ok else None,
                "tokens_max": [max(c["tokens"][i] for c in ok) for i in (0, 1)] if ok else None}

    def verdicts(who: str, group: list[dict]) -> dict:
        done = [x["fixes"]["edit"] for x in group if x.get("fixes", {}).get("edit", {}).get("file")]
        vs = [d["verify"][who] for d in done]
        ok = [v for v in vs if v["checked"]]
        res = [r["fixed"] for v in ok for r in v["results"]]
        return {"fixes": len(done), "identical_bytes": sum(d["identical"] for d in done), "verified": len(ok),
                "unchanged": sum(v["unchanged"] for v in ok),
                "partial": sum(1 for v in ok if not v["unchanged"] and any(r["fixed"] != "yes" for r in v["results"])),
                "all_fixed": sum(1 for v in ok if not v["unchanged"] and all(r["fixed"] == "yes" for r in v["results"])),
                "faults": {k: res.count(k) for k in ("yes", "partly", "no")},
                "added_faults": sum(len(v["new"]) for v in ok),
                "cost_per_verify": round(statistics.mean(v["cost"] for v in ok), 5) if ok else None,
                "tokens_mean": [round(statistics.mean(v["tokens"][i] for v in ok)) for i in (0, 1)] if ok else None,
                "tokens_max": [max(v["tokens"][i] for v in ok) for i in (0, 1)] if ok else None}

    done = [x["fixes"]["edit"] for x in flagged if x.get("fixes", {}).get("edit", {}).get("file")]
    scores = [d["keep"]["score"] for d in done if isinstance(d["keep"]["score"], int)]
    summary = {
        "source": source, "sets": [str(s) for s in sets], "pictures": len(rows),
        "picker": picker.name, "strong": strong.name,
        "checks": {k: stats(k) for k in sorted({k for x in rows for k in x["checks"]})},
        "verify": {who: verdicts(who, flagged) for who in checkers},
        "agree_unchanged": sum(1 for d in done if d["verify"]["picker"]["checked"] and d["verify"]["strong"]["checked"]
                               and d["verify"]["picker"]["unchanged"] == d["verify"]["strong"]["unchanged"]),
        "recheck_clean": sum(1 for d in done if d["recheck"]["checked"] and not d["recheck"]["faults"]),
        "keep_mean": round(statistics.mean(scores), 2) if scores else None,
        "edit_cost_mean": round(statistics.mean(d["cost"] for d in done), 4) if done else None,
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
    return "<ul>" + "".join(f"<li><b>{html.escape(f.get('severity', ''))}</b> {html.escape(f['what'])}<br>"
                            f"<small>{html.escape(f['where'])}</small></li>" for f in faults) + "</ul>"


def sheet(summary: dict, rows: list[dict]) -> str:
    e = html.escape
    head = (f"<h1>Drawing-fault check and fix verification</h1><p>{summary['pictures']} pictures; picker "
            f"{e(summary['picker'])}, strong {e(summary['strong'])}; spent ${summary['spent']['total']}.</p>"
            f"<pre>{e(json.dumps({k: summary[k] for k in ('checks', 'verify', 'agree_unchanged', 'recheck_clean', 'keep_mean')}, indent=1))}</pre>")
    body = []
    for x in rows:
        checks = "".join(f"<p><b>{e(k)}</b>{'' if c['checked'] else ' not checked: ' + e(c['why'])}</p>{faults_html(c['faults'])}"
                         for k, c in x["checks"].items())
        cells = [f"<td><img src='img/{e(x['file'])}'><p><b>original</b> {e(x['kind'])} {e(x['target'][:40])} "
                 f"({e(str(x['model']))}, {e(str(x['quality']))})</p>{checks}"
                 f"<details><summary>prompt</summary><small>{e(x['prompt'])}</small></details></td>"]
        for way, d in (x.get("fixes") or {}).items():
            if not d.get("file"):
                cells.append(f"<td><p>{e(way)}: {e(d.get('error') or d.get('skipped') or '')}</p></td>")
                continue
            ver = "".join(
                f"<p><b>{e(who)} verdict</b>: " + (f"{e(', '.join(r['fixed'] for r in v['results']))}; changed {v['changed']}"
                                                   + ("; <b>unchanged</b>" if v["unchanged"] else "")
                                                   if v["checked"] else "failed: " + e(v["why"])) + "</p>"
                + (f"<p>added:</p>{faults_html(v['new'])}" if v.get("new") else "")
                for who, v in d.get("verify", {}).items())
            cells.append(f"<td><img src='img/{e(d['file'])}'><p><b>{e(way)}</b>{' (identical bytes)' if d.get('identical') else ''}"
                         f", keeps {d['keep']['score']}/10: {e(d['keep']['changed'])}</p>{ver}"
                         f"<p>re-check (strong):</p>{faults_html(d['recheck']['faults'])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return ("<!doctype html><meta charset=utf-8><title>anatomy check</title><style>body{font:13px system-ui;background:#111;color:#ddd}"
            "td{vertical-align:top;width:50%;padding:6px}img{width:100%}ul{margin:4px 0;padding-left:16px}.ok{color:#7c7}"
            "small{color:#999}pre{font-size:11px}</style>" + head + "<table>" + "".join(body) + "</table>")


if __name__ == "__main__":
    sys.exit(main())
