"""Lay out `bench/user_refs_applies.py`'s results as one labelled PNG contact sheet, and print the blind judge's tally.

    uv run --with pillow python bench/user_refs_applies_sheet.py DIR REFS_DIR

One row per case and condition, the whole-picture condition (as #170 sent it) first, so old is beside new: the person's
reference photographs, then the two takes, each captioned with the judge's verdict (each figure's sex as drawn and whether it
wears each photograph's garment). Writes DIR/contact-sheet.png. Pillow is only borrowed for this script (`--with`).
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL = 300
HEAD = 40
FOOT = 58


def fit(path: Path | None, size: int = CELL) -> Image.Image:
    cell = Image.new("RGB", (size, size), (24, 24, 28))
    if path and path.exists():
        im = Image.open(path).convert("RGB")
        im.thumbnail((size, size))
        cell.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return cell


def verdict(row: dict) -> str:
    j = row.get("judge") or {}
    if "error" in j or not j:
        return "not judged"
    figs = []
    for f in j.get("figures", []):
        worn = [f"#{w['ref'] - 1}" for w in f.get("wears", []) if w.get("value") in ("yes", "partly")]
        figs.append(f"{f.get('label')}: {f.get('sex')}" + (f" wears {'+'.join(worn)}" if worn else ""))
    return "; ".join(figs) + f". sexes as prompt: {'yes' if j.get('sexes_as_prompt') else 'NO'}"


def main() -> int:
    out, refdir = Path(sys.argv[1]), Path(sys.argv[2])
    res = json.loads((out / "results.json").read_text())
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in res["rows"]:
        groups.setdefault((r["case"], r["condition"]), []).append(r)
    sheet = Image.new("RGB", (CELL * 3, (HEAD + CELL + FOOT) * len(groups)), (12, 12, 14))
    draw = ImageDraw.Draw(sheet)
    for n, ((case, cond), rows) in enumerate(groups.items()):
        y = n * (HEAD + CELL + FOOT)
        r0 = rows[0]
        tied = ", ".join(f"{x['ref']} -> {x['applies']}" for x in r0["refs"])
        figs = ", ".join(f"{f['label']}={f['entity']}" for f in r0.get("figures") or [])
        draw.text((8, y + 4), f"{case}  [{cond}]  {tied}  ({figs or 'no labels'})", fill=(235, 235, 235), font=font)
        draw.text((8, y + 21), textwrap.shorten("; ".join(x["note"] for x in r0["refs"]), 140), fill=(170, 170, 170), font=font)
        # the reference photographs, side by side in the first cell
        k = len(r0["refs"])
        for i, x in enumerate(r0["refs"]):
            sheet.paste(fit(refdir / f"{x['ref']}.jpg", CELL // k), (i * (CELL // k), y + HEAD))
        for c, row in enumerate(rows[:2], 1):
            sheet.paste(fit(out / row["file"] if row["file"] else None), (c * CELL, y + HEAD))
            lines = textwrap.wrap(verdict(row), 46)[:4]
            draw.text((c * CELL + 6, y + HEAD + CELL + 2), "\n".join(lines), fill=(200, 200, 200), font=font)
    sheet.save(out / "contact-sheet.png")
    print(out / "contact-sheet.png")
    for (case, cond), rows in groups.items():
        print(f"{case:12} {cond:18} " + " | ".join(verdict(r) for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
