"""Does the drawing-fault check find writing and a wrong style, and does Fix take them out?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/stray_check.py --out DIR [--cap 0.8] \
        --sets .claude/work/anatomy-check/bench/sets --letters DIR_OF_stray_letters_scenes \
        --stray .claude/work/sharpen-action-prompts/bench4 --scenes DIR_OF_stray_letters_scenes

Pictures checked, each with `art.check_picture(model, ..., style=)` on Claude Sonnet 5.5 (the lightbox's default checker),
the raw reply kept beside what code keeps (a minor style fault is dropped by code, so the raw count is what shows false
alarms): the pictures of an earlier bench's sets (`--sets`, with their stored prompts and style), the picture of an earlier
bench that shows a letter beside each figure (`--stray`, side c2, dracula.ev.52, picture 1), the pictures the letters bench's
judge flagged for writing and a few it did not (`--scenes`), and three pictures drawn here on purpose in the wrong style (an
ink prompt with a photograph sentence), checked against ink. Then Fix on the picture with the letters and on one wrong-style
picture (`art.fix_prompt`, an edit on Gemini at standard, `art.verify_fix` on Sonnet). DIR gets `results.json` and
`index.html`, a contact sheet with each fix before and after.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lorekeeper import art, llm

WRONG_STYLE = "Photograph, natural light, sharp focus, realistic colour, shallow depth of field."


class Tap:
    """A model that keeps the last raw reply of complete() and the tokens of its calls."""

    def __init__(self, model):
        self.model, self.raw = model, None

    def complete(self, *a, **k):
        self.raw = self.model.complete(*a, **k)
        return self.raw

    def __getattr__(self, name):
        return getattr(self.model, name)


def sonnet():
    return Tap(llm.make("claude", "claude-sonnet-5-5", llm.env_key("claude")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sets", type=Path)
    ap.add_argument("--stray", type=Path)
    ap.add_argument("--scenes", type=Path)
    ap.add_argument("--cap", type=float, default=0.8)
    ap.add_argument("--flagged", type=int, default=6)
    ap.add_argument("--clean", type=int, default=4)
    a = ap.parse_args()
    out, img = a.out, a.out / "img"
    img.mkdir(parents=True, exist_ok=True)
    lock, spent = threading.Lock(), {"text": 0.0, "images": 0.0}
    ink = art.style_sentence("ink")
    items: list[dict] = []     # {name, file, mime, prompt, style}

    if a.sets:
        for art_json in sorted(a.sets.rglob("art.json")):
            man = json.loads(art_json.read_text())
            for r in man.get("pictures", []):
                if r.get("kind") in ("plate", "event") and (art_json.parent / r["file"]).is_file():
                    dst = img / f"set-{art_json.parent.parent.name[:8]}-{r['file']}"
                    shutil.copyfile(art_json.parent / r["file"], dst)
                    items.append({"name": dst.name, "file": dst.name, "mime": r.get("mime") or "image/png",
                                  "prompt": r["prompt"], "style": art.style_sentence(man.get("style") or "ink"), "group": "anatomy bench"})
    if a.stray:
        res = json.loads((a.stray / "results.json").read_text())["sides"]["c2"]["dracula.ev.52"]
        dst = img / "stray-c2-drac-52-1.jpg"
        shutil.copyfile(a.stray / "img" / "c2-drac-52-1-f59435.jpg", dst)
        items.append({"name": dst.name, "file": dst.name, "mime": "image/jpeg", "prompt": res["prompt"], "style": ink,
                      "group": "letters (earlier bench)"})
    scene_rows = []
    if a.scenes:
        rows = json.loads((a.scenes / "results.json").read_text())["rows"].values()
        done = [r for r in rows if r.get("judge") and r.get("file")]
        flagged = [r for r in done if r["judge"]["found"]][:a.flagged]
        clean = [r for r in done if not r["judge"]["found"] and r["style"] == "ink"][:a.clean]
        for r in flagged + clean:
            dst = img / f"scn-{r['file']}"
            shutil.copyfile(a.scenes / "img" / r["file"], dst)
            items.append({"name": dst.name, "file": dst.name, "mime": r.get("mime") or "image/jpeg", "prompt": r["prompt"],
                          "style": art.style_sentence(r["style"]),
                          "group": "letters bench, judge flagged writing" if r["judge"]["found"] else "letters bench, judge found none",
                          "judge": r["judge"]})
        scene_rows = json.loads((a.scenes / "prompts.json").read_text())["scenes"][:3]

    # three pictures in the wrong style on purpose: an ink prompt drawn with a photograph sentence, checked against ink; when
    # the image provider will not draw (a spending cap), three comic-strip pictures of the letters bench (flat colour, halftone)
    # checked against ink stand in: another medium all the same
    image = llm.make_image("gemini", "gemini-nano-banana-2.1")
    drawn = 0
    for s in scene_rows:
        text = art.Illustrator._assemble(s["body"], style=WRONG_STYLE)
        try:
            pic = image.draw(text, shape="landscape", quality="standard")
        except llm.ProviderError as e:
            print("cannot draw the wrong-style pictures:", str(e)[:100])
            break
        spent["images"] += pic.cost
        name = f"wrong-{s['id']}.jpg"
        (img / name).write_bytes(pic.data)
        drawn += 1
        items.append({"name": name, "file": name, "mime": pic.mime, "prompt": art.Illustrator._assemble(s["body"], style=ink),
                      "style": ink, "group": "wrong style on purpose (photograph drawn for ink)"})
    if not drawn and a.scenes:
        rows = json.loads((a.scenes / "results.json").read_text())["rows"].values()
        for r in [r for r in rows if r["style"] == "comic-strip" and r["arm"] == "old" and r.get("file")][:3]:
            dst = img / f"substitute-{r['file']}"
            shutil.copyfile(a.scenes / "img" / r["file"], dst)
            items.append({"name": dst.name, "file": dst.name, "mime": r.get("mime") or "image/jpeg",
                          "prompt": art.Illustrator._assemble(r["prompt"].split(" " + art.style_sentence("comic-strip"))[0], style=ink),
                          "style": ink, "group": "wrong style, substitute (comic-strip picture checked against ink)"})

    def one(it: dict) -> dict:
        with lock:
            if spent["text"] + spent["images"] > a.cap:
                it["skipped"] = "cap"
                return it
        m = sonnet()
        c = art.check_picture(m, (img / it["file"]).read_bytes(), it["mime"], it["prompt"], it["style"])
        raw = (m.raw or {}).get("faults") if isinstance(m.raw, dict) else None
        it.update({"checked": c.checked, "faults": c.faults, "raw_faults": raw, "tokens": [m.usage.prompt_tokens, m.usage.output_tokens],
                   "cost": m.usage.cost_usd})
        with lock:
            spent["text"] += m.usage.cost_usd
        print(it["name"], [(f["kind"], f["severity"], f["what"][:60]) for f in c.faults], flush=True)
        return it

    with ThreadPoolExecutor(4) as pool:
        items = list(pool.map(one, items))

    # Fix on the picture with the letters, and on one wrong-style picture
    fixes = []
    for it in items:
        want = ("text", "style")
        if it["name"] in ("stray-c2-drac-52-1.jpg", next((i["name"] for i in items if i["name"].startswith(("wrong-", "substitute-"))), "")):
            faults = [f for f in it.get("faults") or [] if f["kind"] in want and f["severity"] == "major"]
            if not faults:
                fixes.append({"name": it["name"], "skipped": "no text or style fault found"})
                continue
            prompt = art.fix_prompt(faults, "", ink)
            before = (img / it["file"]).read_bytes()
            try:
                pic = image.draw(prompt, shape="landscape", quality="standard", references=[(before, it["mime"])])
            except llm.ProviderError as e:
                fixes.append({"name": it["name"], "fix_prompt": prompt, "faults": faults, "skipped": f"not drawn: {str(e)[:120]}"})
                print("fix not drawn:", str(e)[:100])
                continue
            spent["images"] += pic.cost
            after = f"fix-{it['name']}"
            (img / after).write_bytes(pic.data)
            m = sonnet()
            v = art.verify_fix(m, (before, it["mime"]), (pic.data, pic.mime), faults)
            spent["text"] += m.usage.cost_usd
            again = sonnet()
            c2 = art.check_picture(again, pic.data, pic.mime, it["prompt"], it["style"])
            spent["text"] += again.usage.cost_usd
            fixes.append({"name": it["name"], "after": after, "fix_prompt": prompt, "faults": faults, "verdict": v.counts() if v.checked else None,
                          "changed": v.changed, "recheck": c2.faults})
            print("fix", it["name"], v.counts() if v.checked else v.why, "recheck:", [(f["kind"], f["what"][:50]) for f in c2.faults])

    ok = [i for i in items if i.get("checked")]
    by = {}
    for i in ok:
        g = by.setdefault(i["group"], {"n": 0, "text": 0, "style_major": 0, "style_raw": 0, "style_raw_major": 0, "drawing": 0})
        g["n"] += 1
        g["text"] += any(f["kind"] == "text" for f in i["faults"])
        g["style_major"] += any(f["kind"] == "style" for f in i["faults"])
        raw = [f for f in i.get("raw_faults") or [] if isinstance(f, dict) and f.get("kind") == "style"]
        g["style_raw"] += bool(raw)
        g["style_raw_major"] += any(f.get("severity") == "major" for f in raw)
        g["drawing"] += any(f["kind"] == "drawing" for f in i["faults"])
    tok = [i["tokens"] for i in ok]
    summary = {"by_group": by, "checks": len(ok), "tokens_in_mean": round(sum(t[0] for t in tok) / max(1, len(tok))),
               "tokens_out_mean": round(sum(t[1] for t in tok) / max(1, len(tok))), "tokens_out_max": max([t[1] for t in tok] or [0]),
               "check_cost_mean": round(sum(i["cost"] for i in ok) / max(1, len(ok)), 5), "spent": {k: round(v, 4) for k, v in spent.items()}}
    (out / "results.json").write_text(json.dumps({"summary": summary, "items": items, "fixes": fixes}, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))
    rows = []
    for it in items:
        fs = "; ".join(f"[{f['kind']}/{f['severity']}] {f['what']}" for f in it.get("faults") or []) or "none"
        rows.append(f"<tr><td><img src='img/{it['file']}' width=320></td><td><b>{html.escape(it['group'])}</b><br>{html.escape(it['name'])}"
                    f"<br>{html.escape(fs)}</td></tr>")
    for f in fixes:
        if "after" in f:
            rows.append(f"<tr><td><img src='img/{f['name']}' width=400><br>before</td><td><img src='img/{f['after']}' width=400><br>after the fix: "
                        f"{html.escape(json.dumps(f['verdict']))}<br>recheck: {html.escape(json.dumps(f['recheck']))}</td></tr>")
    (out / "index.html").write_text("<!doctype html><meta charset=utf-8><style>body{font:13px sans-serif;background:#222;color:#ddd}"
                                    "td{vertical-align:top;padding:6px}</style><table>" + "".join(rows) + "</table>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
