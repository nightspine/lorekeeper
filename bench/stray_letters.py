"""Are figure labels ("figure A") drawn as letters in the picture, and does a sentence after the no-text line stop it?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/stray_letters.py prompts INDEX EPUB --out DIR [--scenes 6]
    uv run --extra claude python bench/stray_letters.py draw --out DIR --arms old,v1,v2 [--repro BENCH4_RESULTS.json]
    uv run --extra claude python bench/stray_letters.py sheet --out DIR

`prompts` writes labelled multi-figure scenes of one book once with the real writer and keeps them fixed (DIR/prompts.json:
body, figures). `draw` draws every scene in every style, `--draws` times per arm, on Gemini at standard, landscape; an arm
is the no-text line with or without a sentence after it (`ARMS`), so the writer's text never changes between arms. `--repro`
adds a scene taken from an earlier bench's results.json (`--repro-event`, side `--repro-side`) drawn `--repro-draws` times.
Every picture is asked one plain question by a vision model ("Is there any letter, word, number or caption drawn in this
picture? Where?"); DIR/results.json holds every answer and the spend, and `sheet` writes DIR/index.html, old beside new.
Drawings are cached by file name, so a rerun draws only what is missing.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lorekeeper import art, epub, llm

sys.path.insert(0, str(Path(__file__).parent))
from figure_labels import Mem, pick   # noqa: E402

# the arms: what follows the base no-text line. "old" is today's prompt.
ARMS = {
    "old": "",
    "v1": ('In this description "figure A", "figure B" and so on only tell the figures apart; they are not part of the '
           "picture. No letter, label, number or caption is drawn beside, above or on anyone."),
    "v3": ('The words "figure A", "figure B" and so on only tell the figures apart in this description. Nothing is written in the '
           "picture: no letter, label, number or caption beside, above or on anyone."),
    "v4": ("Each figure in this description is called by a label (the word figure and a letter) only so that the figures can be "
           "told apart. Nothing is written in the picture: no letter, label, number or caption beside, above or on anyone."),
    "v2": ('"Figure A", "figure B" and so on are only names for telling the people apart in this description. Around every '
           "person the picture is clean: nothing is written, lettered or marked next to them."),
}
JUDGE = """You look at a picture an image model drew. Answer one plain question.
Is there any letter, word, number or caption drawn in this picture? Where?
Count anything that reads as writing: a single letter beside or on a figure, a number, a label or caption strip, readable words on a sign, a page or a wall, a signature or monogram in a corner. Marks that are only decoration, hatching, or scribbles that could not be read as a letter or a number are not writing.
Return "found" (true or false), "where" (a few words, empty when none) and "what" (what the text says, empty when none)."""
JUDGE_SCHEMA = {"type": "object", "required": ["found", "where", "what"], "properties": {
    "found": {"type": "boolean"}, "where": {"type": "string"}, "what": {"type": "string"}}}


def text_model(spec: str, tier: str = "cheap"):
    provider, _, model = spec.partition(":")
    return llm.make(provider, model or llm.PROVIDERS[provider][tier], llm.env_key(provider))


def cmd_prompts(a) -> None:
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    scenes = pick(doc, bk, a.scenes * 3, 3)
    writer, picker = text_model(a.writer, "strong"), text_model("claude")
    il = art.Illustrator([art.Book(doc, bk, Mem(art.blank(doc["editions"][0]["textHash"], "ink")))], writer, None, "ink",
                         portraits=None, workers=1, log=lambda *_: None, cheap_text=picker)
    rows = []
    for kind, target in scenes:
        if len(rows) >= a.scenes:
            break
        try:
            pr = il.make_prompt(il.job(kind, target))
        except Exception as e:      # noqa: BLE001 - a bench: skip and go on
            print("skip", kind, target, str(e)[:100])
            continue
        labels = art.labels_in(pr.body)
        if len(labels) < 2:
            print("skip (fewer than two labels)", kind, target)
            continue
        rows.append({"id": f"{kind}-{len(rows) + 1}", "kind": kind, "target": target, "body": pr.body, "labels": labels})
        print(rows[-1]["id"], target, labels, len(pr.body.split()), "words")
    (out / "prompts.json").write_text(json.dumps({"scenes": rows, "cost": round(writer.usage.cost_usd + picker.usage.cost_usd, 4)},
                                                 indent=1, ensure_ascii=False))


def cmd_draw(a) -> None:
    out, img = a.out, a.out / "img"
    img.mkdir(parents=True, exist_ok=True)
    scenes = json.loads((out / "prompts.json").read_text())["scenes"] if (out / "prompts.json").is_file() else []
    jobs = []
    for s in scenes:
        for style in a.styles:
            jobs += [(s["id"], s["body"], style, arm, i) for arm in a.arms for i in range(a.draws)]
    if a.repro:
        r = json.loads(Path(a.repro).read_text())["sides"][a.repro_side][a.repro_event]
        jobs += [(f"repro-{a.repro_event.split('.')[-1]}", r["body"], "ink", arm, i) for arm in a.arms for i in range(a.repro_draws)]
    image = llm.make_image("gemini", "gemini-nano-banana-2.1")
    lock, spent = threading.Lock(), {"images": 0.0, "judge": 0.0}
    results = json.loads((out / "results.json").read_text()) if (out / "results.json").is_file() else {"rows": {}}
    rows = results["rows"]
    bodies = {j[0]: j[1] for j in jobs}

    def one(job) -> None:
        sid, body, style, arm, i = job
        key = f"{sid}|{style}|{arm}|{i}"
        if rows.get(key, {}).get("judge"):
            return
        row = rows.setdefault(key, {"scene": sid, "style": style, "arm": arm, "i": i})
        text = art.Illustrator._assemble(body, style=art.style_sentence(style), no_text=art.NO_TEXT)
        if ARMS[arm]:
            text = f"{text} {ARMS[arm]}"
        row["prompt"] = text
        file = img / f"{sid}-{style}-{arm}-{i}.jpg"
        if not file.is_file():
            with lock:
                if spent["images"] + spent["judge"] + 0.04 > a.cap:
                    row["skipped"] = "cap"
                    return
            try:
                pic = image.draw(text, shape="landscape", quality="standard")
            except Exception as e:      # noqa: BLE001
                row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                return
            file.write_bytes(pic.data)
            row["mime"] = pic.mime
            with lock:
                spent["images"] += pic.cost
        row["file"] = file.name
        m = text_model(a.judge, "strong")
        try:
            got = m.complete(JUDGE, "The picture is above.", JUDGE_SCHEMA, images=[(file.read_bytes(), row.get("mime") or "image/jpeg")])
            row["judge"] = {"found": bool(got.get("found")), "where": str(got.get("where") or "")[:120], "what": str(got.get("what") or "")[:120]}
        except Exception as e:      # noqa: BLE001
            row["error"] = f"judge {type(e).__name__}: {str(e)[:120]}"
        with lock:
            spent["judge"] += m.usage.cost_usd
        print(key, row.get("judge"), flush=True)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, jobs))
    results["spent"] = {k: round(results.get("spent", {}).get(k, 0) + v, 4) for k, v in spent.items()}
    results["bodies"] = {**results.get("bodies", {}), **bodies}
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print("spent this run:", {k: round(v, 4) for k, v in spent.items()})
    cmd_summary(a)


def cmd_summary(a) -> None:
    rows = json.loads((a.out / "results.json").read_text())["rows"].values()
    table: dict = {}
    for r in rows:
        if "judge" in r:
            c = table.setdefault((r["scene"].startswith("repro"), r["style"], r["arm"]), [0, 0])
            c[1] += 1
            c[0] += r["judge"]["found"]
    for (repro, style, arm), (hit, n) in sorted(table.items()):
        print(f"{'repro' if repro else 'scenes'} {style:12} {arm:4} letters in {hit} of {n}")


def cmd_sheet(a) -> None:
    res = json.loads((a.out / "results.json").read_text())
    rows = list(res["rows"].values())
    scenes = list(dict.fromkeys((r["scene"], r["style"]) for r in rows))
    arms = list(dict.fromkeys(r["arm"] for r in rows))
    cells = []
    for sid, style in scenes:
        cells.append(f"<h3>{html.escape(sid)}, {html.escape(style)}</h3><div class=g>")
        for arm in arms:
            cells.append(f"<div class=c><h4>{html.escape(arm)}</h4>")
            for r in sorted((r for r in rows if r["scene"] == sid and r["style"] == style and r["arm"] == arm), key=lambda r: r["i"]):
                j = r.get("judge") or {}
                cls = "hit" if j.get("found") else ""
                cells.append(f"<figure class='{cls}'><img src='img/{html.escape(r.get('file', ''))}' loading=lazy>"
                             f"<figcaption>{'LETTERS: ' + html.escape(j.get('where', '') + ' ' + j.get('what', '')) if j.get('found') else 'clean'}"
                             "</figcaption></figure>")
            cells.append("</div>")
        cells.append("</div>")
    (a.out / "index.html").write_text("<!doctype html><meta charset=utf-8><title>stray letters</title><style>"
                                      "body{font:13px sans-serif;margin:16px;background:#222;color:#ddd}.g{display:flex;gap:12px}"
                                      ".c{flex:1}img{width:100%}figure{margin:0 0 8px}.hit img{outline:4px solid #e44}"
                                      "figcaption{font-size:11px}</style>" + "".join(cells))
    print("wrote", a.out / "index.html")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts")
    p.add_argument("index")
    p.add_argument("epub")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--scenes", type=int, default=6)
    p.add_argument("--writer", default="claude:claude-sonnet-5-5")
    d = sub.add_parser("draw")
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--arms", type=lambda s: s.split(","), default=["old", "v1"])
    d.add_argument("--styles", type=lambda s: s.split(","), default=["ink", "comic-strip"])
    d.add_argument("--draws", type=int, default=2)
    d.add_argument("--repro")
    d.add_argument("--repro-side", default="c2")
    d.add_argument("--repro-event", default="dracula.ev.52")
    d.add_argument("--repro-draws", type=int, default=6)
    d.add_argument("--judge", default="claude:claude-sonnet-5-5")
    d.add_argument("--cap", type=float, default=2.5)
    s = sub.add_parser("sheet")
    s.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    {"prompts": cmd_prompts, "draw": cmd_draw, "sheet": cmd_sheet}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
