"""Lay out `bench/style_reference.py`'s results as one labelled PNG contact sheet.

    uv run --with pillow python bench/style_reference_sheet.py DIR

One row per reference picture: the reference, then the portrait and the scene in versions (a) described sentence only,
(b) sentence plus the picture, (c) the built-in style it came from. Writes DIR/contact-sheet.png. Pillow is only borrowed for
this script (`--with`), never a dependency of Lorekeeper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL = 300
LABEL = 28
COLS = [("reference", None), ("portrait (a) text", "portrait-a"), ("portrait (b) text+picture", "portrait-b"),
        ("portrait (c) built-in", "portrait-c"), ("scene (a) text", "scene-a"), ("scene (b) text+picture", "scene-b"),
        ("scene (c) built-in", "scene-c")]


def fit(path: Path) -> Image.Image:
    im = Image.open(path).convert("RGB")
    im.thumbnail((CELL, CELL))
    cell = Image.new("RGB", (CELL, CELL), (24, 24, 28))
    cell.paste(im, ((CELL - im.width) // 2, (CELL - im.height) // 2))
    return cell


def main() -> int:
    out = Path(sys.argv[1])
    res = json.loads((out / "results.json").read_text())
    rows = res["rows"]
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    sheet = Image.new("RGB", (CELL * len(COLS), (CELL + LABEL) * len(rows) + LABEL), (12, 12, 16))
    d = ImageDraw.Draw(sheet)
    for c, (title, _) in enumerate(COLS):
        d.text((c * CELL + 8, 6), title, fill=(230, 230, 230), font=font)
    for r, row in enumerate(rows):
        y = LABEL + r * (CELL + LABEL)
        d.text((8, y + 6), f"{row['name']}: {row['description'][:150]}", fill=(200, 200, 140), font=font)
        for c, (_, key) in enumerate(COLS):
            p = Path(row["picture"]) if key is None else (out / row["drawn"][key]["file"]
                                                          if row["drawn"].get(key, {}).get("file") else None)
            if p is not None and p.is_file():
                sheet.paste(fit(p), (c * CELL, y + LABEL))
    sheet.save(out / "contact-sheet.png")
    print(out / "contact-sheet.png")
    if (out / "consistency.json").is_file():
        consistency(out, json.loads((out / "consistency.json").read_text()), res, font)
    return 0


def consistency(out: Path, con: dict, res: dict, font) -> None:
    """DIR/consistency-sheet.png: per reference, the reference, then the scene three times under (a) and under (b); then the
    crowded scene with its figure portraits."""
    lines = []
    for name, got in con["repeats"].items():
        ref = next(r["picture"] for r in res["rows"] if r["name"] == name)
        lines.append((f"{name}: same scene, (a) text only x{len(got['a'])}, then (b) text + picture x{len(got['b'])}",
                      [Path(ref)] + [out / f for f in got["a"] + got["b"] if f]))
    if con.get("crowd") and con["crowd"].get("file"):
        c = con["crowd"]
        ref = next(r["picture"] for r in res["rows"] if r["name"] == c["ref"])
        lines.append((f"crowded scene under (b) {c['ref']}: {len(c['portraits'])} portraits, the scene "
                      f"({c['references']} pictures sent), the reference",
                      [out / f for f in c["portraits"].values() if f] + [out / c["file"], Path(ref)]))
    width = max(len(p) for _, p in lines)
    sheet = Image.new("RGB", (CELL * width, (CELL + LABEL) * len(lines)), (12, 12, 16))
    d = ImageDraw.Draw(sheet)
    for r, (title, paths) in enumerate(lines):
        y = r * (CELL + LABEL)
        d.text((8, y + 6), title, fill=(200, 200, 140), font=font)
        for c, p in enumerate(paths):
            if p.is_file():
                sheet.paste(fit(p), (c * CELL, y + LABEL))
    sheet.save(out / "consistency-sheet.png")
    print(out / "consistency-sheet.png")


if __name__ == "__main__":
    raise SystemExit(main())
