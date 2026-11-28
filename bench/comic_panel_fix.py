"""Bench: the shipped `Illustrator.fix_panel` on three sample comic pages (one panel each), once each.

    set -a; . ~/projects/lorekeeper/.env; set +a
    uv run --extra web --extra claude python bench/comic_panel_fix.py OUT_DIR SET_DIR [--cap 0.5] [--provider claude]

SET_DIR is an art set folder holding comic pages (the chapter-comic samples). The index and EPUB are Dracula's
(examples/dracula/dracula.json, corpus/pg345/pg345.epub). Prints per case: pasted yes/no, the verification's verdict, cost;
writes a contact sheet `sheet.jpg` (the original page, then the fixed one) and results.json.
"""
import argparse
import io
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

from lorekeeper import art, epub, llm

ROOT = Path(__file__).resolve().parent.parent
CASES = [("p23cbcdd7", 5, "", "The young man is drawn with three hands and two razors. Give him exactly two hands: one hand holds a single "
                             "razor to his cut chin, the other hand rests at his side. Remove the extra hand and the second razor."),
         ("pf8df8204", 4, "The young man in the bed is awake: sitting up against the pillows, looking toward the nun at the table.", ""),
         ("p2979287c", 3, "The small child sits up on the grass, crying.", "")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("set")
    ap.add_argument("--cap", type=float, default=0.5)
    ap.add_argument("--provider", default="claude")
    ap.add_argument("--epub", default=str(ROOT / "corpus/pg345/pg345.epub"))
    a = ap.parse_args()
    out = Path(a.out)
    folder = out / "set"
    shutil.copytree(a.set, folder, dirs_exist_ok=True)
    doc = json.loads((ROOT / "examples/dracula/dracula.json").read_text())
    bk = epub.read(a.epub)
    image = llm.make_image("gemini", "gemini-nano-banana-2.1")
    checker = llm.make(a.provider, llm.PROVIDERS[a.provider]["cheap"], llm.env_key(a.provider))
    il = art.Illustrator([art.Book(doc, bk, art.FileSink(folder), "main")], checker, image, "comic-strip", quality="draft",
                         portraits=False, cap=a.cap, log=print)
    man = json.loads((folder / "art.json").read_text())
    res, tiles = [], []
    for pid, k, text, note in CASES:
        rec = next(r for r in man["pictures"] if r["id"] == pid)
        before = (folder / rec["file"]).read_bytes()
        row = {"page": pid, "panel": k}
        try:
            new = il.fix_panel(rec, k, text=text, note=note)
            e = new["fixes"][-1]
            row.update(pasted=e["pasted"], verified=e["verified"], cost=e["cost"])
            after = il.books[0].b.sink.read(new["file"])
        except art.FixUnchanged as e:
            row.update(unchanged=True, verified=e.verdict.counts(), cost=e.cost)
            after = before
        print(row, flush=True)
        res.append(row)
        tiles.append((Image.open(io.BytesIO(before)).convert("RGB"), Image.open(io.BytesIO(after)).convert("RGB")))
    h = 700
    sheet = Image.new("RGB", (sum(2 * round(h * x.width / x.height) + 20 for x, _ in tiles), h), "white")
    x = 0
    for b, f in tiles:
        for im in (b, f):
            im = im.resize((round(h * im.width / im.height), h))
            sheet.paste(im, (x, 0))
            x += im.width + 10
        x += 10
    sheet.save(out / "sheet.jpg", quality=88)
    total = sum(r["cost"] for r in res)
    print(f"total ${total:.3f}")
    (out / "results.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
