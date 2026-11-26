"""Are portraits drawn from mid-chest up, hands out of frame, and do scenes that take one as a reference still draw the whole
figure at natural proportions?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/portrait_crop.py --out DIR \
        --book TAG INDEX EPUB --portrait TAG ENTITY [--portrait ...] [--scene TAG EVENT ...] \
        [--variants old-tall,new-tall,new-square] [--style ink] [--quality standard] [--cap 3]

Each `--portrait` is drawn once per variant, each into its own fresh art set so no earlier portrait is sent as a reference:
`old-*` with the framing and portrait writer of the main branch (loaded from `git show main:src/lorekeeper/art.py`), `new-*`
with this branch's; `*-tall` in the portrait shape, `*-square` square. Each `--scene` event is then drawn in the `new-tall`
set of its book, so its labelled figures take those chest-up portraits as references. The writer is Claude's strong model
(the portrait writer's default) for portraits and its cheap one for scenes; the image model is Gemini's default. DIR gets
the pictures, `results.json` (each prompt and reference note as sent, costs) and `contact-sheet.png` (needs Pillow; run
with `uv run --with pillow`).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from lorekeeper import art, epub, llm

ROOT = Path(__file__).resolve().parent.parent


def old_art():
    """The main branch's art.py, for its portrait constants only."""
    src = subprocess.run(["git", "show", "main:src/lorekeeper/art.py"], cwd=ROOT, check=True, capture_output=True,
                         text=True).stdout
    path = Path(sys.argv[0]).resolve().parent / ".art_main.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("lorekeeper._art_main", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod       # dataclasses look their module up there
    spec.loader.exec_module(mod)
    path.unlink()
    return mod


class Noting:
    """The image model, keeping what was sent with each call."""

    def __init__(self, model):
        self.model, self.calls = model, []
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def draw(self, prompt, **kw):
        self.calls.append({"prompt": prompt, "note": kw.get("note", ""), "shape": kw.get("shape"),
                           "references": len(kw.get("references") or [])})
        return self.model.draw(prompt, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--book", nargs=3, action="append", metavar=("TAG", "INDEX", "EPUB"), required=True)
    ap.add_argument("--portrait", nargs=2, action="append", metavar=("TAG", "ENTITY"), default=[])
    ap.add_argument("--scene", nargs=2, action="append", metavar=("TAG", "EVENT"), default=[])
    ap.add_argument("--variants", default="old-tall,new-tall,new-square")
    ap.add_argument("--style", default="ink")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--cap", type=float, default=3.0)
    ap.add_argument("--scene-writers", default="current",
                    help="old (main's SCENE_WRITER), new (this branch's) or current (whatever the module has), comma separated")
    ap.add_argument("--name", default="", help="suffix of results-NAME.json and contact-sheet-NAME.png")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    old = old_art()
    new = {k: getattr(art, k) for k in ("PORTRAIT_FRAME", "PORTRAIT_WRITER", "PORTRAIT_WORDS")}
    new_framed = art.Illustrator._framed
    old_framed = staticmethod(lambda kind, text: f"{old.PORTRAIT_FRAME} {text}" if kind == "portrait" else text)

    strong = llm.make("claude", llm.PROVIDERS["claude"]["strong"], api_key=llm.env_key("claude"))
    cheap = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", None, api_key=llm.env_key("gemini")))
    books = {tag: (json.loads(Path(i).read_text()), epub.read(e)) for tag, i, e in a.book}
    spent = 0.0
    rows, scenes = [], []
    ils: dict[tuple[str, str], art.Illustrator] = {}

    def illustrator(tag: str, variant: str) -> art.Illustrator:
        if (tag, variant) not in ils:
            doc, bk = books[tag]
            sink = art.FileSink(out / "sets" / tag / variant)
            ils[tag, variant] = art.Illustrator([art.Book(doc, bk, sink, None)], cheap, image, a.style, portraits=None,
                                                quality=a.quality, cap=max(0.01, a.cap - spent), workers=1,
                                                portrait_text=strong, log=lambda m: print(m, file=sys.stderr), pause=0)
        return ils[tag, variant]

    def setting(variant: str) -> None:
        src = old if variant.startswith("old") else None
        for k, v in new.items():
            setattr(art, k, getattr(src, k) if src else v)
        art.Illustrator._framed = old_framed if src else new_framed

    for variant in a.variants.split(","):
        setting(variant)
        for tag, eid in a.portrait:
            il = illustrator(tag, variant)
            name = next(e["names"][0]["value"] for e in books[tag][0]["entities"] if e["id"] == eid)
            job = il.job("portrait", eid, shape="square" if variant.endswith("square") else "portrait")
            before = il.report.cost + il.text_spent()
            try:
                rec = il.draw(job)
            except Exception as e:      # a refusal or a dropped prompt: noted, the sheet goes on
                rec, err = None, f"{type(e).__name__}: {e}"
            else:
                err = "" if rec else "; ".join(r["reason"] for r in il.report.reasons[-1:])
            spent += il.report.cost + il.text_spent() - before
            rows.append({"book": tag, "entity": eid, "name": name, "variant": variant,
                         "file": str(Path("sets") / tag / variant / rec["file"]) if rec else None,
                         "prompt": rec["prompt"] if rec else "", "error": err, "cost": rec["cost"] if rec else 0})
            print(f"{variant} {name}: {rows[-1]['file'] or err}  (spent ${spent:.3f})", file=sys.stderr)
            if spent >= a.cap:
                break
    setting("new-tall")
    new_scene = art.SCENE_WRITER
    for writer, (tag, evid) in [(w, s) for w in a.scene_writers.split(",") for s in a.scene]:
        art.SCENE_WRITER = old.SCENE_WRITER if writer == "old" else new_scene
        il = illustrator(tag, "new-tall")
        job = il.job("event", evid)
        before = il.report.cost + il.text_spent()
        pr = il.make_prompt(job)
        rec = il.draw(job, pr)
        spent += il.report.cost + il.text_spent() - before
        call = image.calls[-1] if image.calls else {}
        ev = next(e for e in books[tag][0]["events"] if e["id"] == evid)
        scenes.append({"book": tag, "event": evid, "writer": writer, "summary": ev["summary"], "figures": rec.get("figures") if rec else None,
                       "file": str(Path("sets") / tag / "new-tall" / rec["file"]) if rec else None,
                       "prompt": rec["prompt"] if rec else pr.text, "note": call.get("note", ""),
                       "references": call.get("references", 0)})
        print(f"scene {evid}: {scenes[-1]['file']}  (spent ${spent:.3f})", file=sys.stderr)
    text = sum(il.text_spent() for il in ils.values())
    pics = sum(il.report.cost for il in ils.values())
    result = {"style": a.style, "quality": a.quality, "image_model": image.name, "writer": strong.name,
              "cost": {"pictures": round(pics, 4), "text": round(text, 4), "total": round(pics + text, 4)},
              "portraits": rows, "scenes": scenes, "frames": {"old": old.PORTRAIT_FRAME, "new": art.PORTRAIT_FRAME,
                                                               "crop": art.PORTRAIT_CROP}}
    suffix = f"-{a.name}" if a.name else ""
    (out / f"results{suffix}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    try:
        sheet(out, result, f"contact-sheet{suffix}.png")
    except ImportError:
        print("no Pillow: no contact sheet", file=sys.stderr)
    print(json.dumps(result["cost"]))
    return 0


def sheet(out: Path, result: dict, name: str = "contact-sheet.png") -> None:
    """contact-sheet.png: one row per portrait entity (a column per variant), then the scenes."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    variants = list(dict.fromkeys(r["variant"] for r in result["portraits"]))
    ents = list(dict.fromkeys((r["book"], r["entity"], r["name"]) for r in result["portraits"]))
    cell, head, label = 360, 40, 220
    rows = len(ents) + (1 if result["scenes"] else 0)
    width = label + cell * max(len(variants), len(result["scenes"]) * 2 if result["scenes"] else 1)
    img = Image.new("RGB", (width, head + rows * (cell + 10)), "white")
    d = ImageDraw.Draw(img)
    for i, v in enumerate(variants):
        d.text((label + i * cell + 8, 8), v, fill="black", font=font)
    by = {(r["book"], r["entity"], r["variant"]): r for r in result["portraits"]}

    def paste(path, x, y, w, h):
        if not path:
            d.text((x + 8, y + 8), "not drawn", fill="red", font=font)
            return
        p = Image.open(out / path).convert("RGB")
        p.thumbnail((w - 8, h - 8))
        img.paste(p, (x + (w - p.width) // 2, y + (h - p.height) // 2))

    for r, (book, eid, name) in enumerate(ents):
        y = head + r * (cell + 10)
        d.text((8, y + 8), f"{name}\n({book})", fill="black", font=font)
        for i, v in enumerate(variants):
            row = by.get((book, eid, v))
            paste(row and row["file"], label + i * cell, y, cell, cell)
    if result["scenes"]:
        y = head + len(ents) * (cell + 10)
        d.text((8, y + 8), "scenes with\nnew-tall\nreferences", fill="black", font=font)
        for i, s in enumerate(result["scenes"]):
            paste(s["file"], label + i * 2 * cell, y, 2 * cell, cell)
    img.save(out / name)


if __name__ == "__main__":
    sys.exit(main())
