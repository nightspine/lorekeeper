"""Lay out `bench/user_refs.py`'s results as one labelled PNG contact sheet.

    uv run --with pillow python bench/user_refs_sheet.py DIR

One row per picture: the person's reference picture, (a) drawn without it, (b) drawn with it and its note; a last row for
the describe path. Writes DIR/contact-sheet.png. Pillow is only borrowed for this script (`--with`), never a dependency.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL = 320
LABEL = 44


def fit(path: Path | None) -> Image.Image:
    cell = Image.new("RGB", (CELL, CELL), (24, 24, 28))
    if path and path.exists():
        im = Image.open(path).convert("RGB")
        im.thumbnail((CELL, CELL))
        cell.paste(im, ((CELL - im.width) // 2, (CELL - im.height) // 2))
    return cell


def main() -> int:
    out = Path(sys.argv[1])
    res = json.loads((out / "results.json").read_text())
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    rows = [(f"{r['kind']} {r['target']}", out / res["refs"][r["ref"]]["file"], out / r["a"] if r["a"] else None,
             out / r["b"] if r["b"] else None, r["note"]) for r in res["rows"]]
    d = res["described"]
    rows.append((f"described: portrait {d['target']}", out / res["refs"][d["ref"]]["file"], None,
                 out / d["file"] if d["file"] else None, (d.get("userRefs") or [{}])[0].get("description", "")))
    strong = out / "strong.json"       # the scenes drawn again with the stronger USER_REF line, if that rerun was made
    if strong.exists():
        for ev, r in json.loads(strong.read_text()).items():
            row = next(x for x in res["rows"] if x["target"] == ev)
            rows.append((f"stronger line: event {ev}", out / res["refs"][row["ref"]]["file"], out / row["b"],
                         out / "sets" / Path(row["b"]).parent.name / r["file"], row["note"] + " (b: before, right: after)"))
    heads = ["the person's picture", "(a) without", "(b) with it"]
    sheet = Image.new("RGB", (CELL * 3, (CELL + LABEL) * len(rows) + 24), (12, 12, 14))
    draw = ImageDraw.Draw(sheet)
    for c, h in enumerate(heads):
        draw.text((c * CELL + 8, 4), h, fill=(230, 230, 230), font=font)
    for n, (title, ref, a, b, note) in enumerate(rows):
        y = 24 + n * (CELL + LABEL)
        lines = textwrap.wrap(f"{title}: {note}", 130)[:2]
        draw.text((8, y + 4), "\n".join(lines), fill=(200, 200, 200), font=font)
        for c, p in enumerate((ref, a, b)):
            sheet.paste(fit(p), (c * CELL, y + LABEL))
    sheet.save(out / "contact-sheet.png")
    print(out / "contact-sheet.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
