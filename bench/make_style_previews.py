"""Draw the built-in style previews: `art.PREVIEW_SCENE` once in each of `art.STYLES`, so the picker's cards compare the
styles like for like, then shrink each to a small WebP in src/lorekeeper/web/static/styles/<slug>.webp.

    set -a; . ./.env; set +a          # GEMINI_API_KEY
    uv run --with pillow python bench/make_style_previews.py                 # every style missing a preview
    uv run --with pillow python bench/make_style_previews.py ink anime --redo   # draw these again

The prompt is `art.preview_prompt(slug)`: the scene, the style sentence and the no-text line, joined as a real prompt is.
Square (`art.PREVIEW_SHAPE`), because the picker's cards are square and a landscape picture loses its sides in one.
The drawn originals are kept in --raw so a redraw can be compared with the one it replaces. Prints the cost of each
picture and the total, and stops before a call that could take the spend past --cap.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image

from lorekeeper import art, images, llm

OUT = Path(__file__).resolve().parent.parent / "src/lorekeeper/web/static/styles"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("styles", nargs="*", help="style slugs (default: every built-in style)")
    ap.add_argument("--redo", action="store_true", help="draw again even when a preview exists")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--model", default="gemini-nano-banana-2.1")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--size", type=int, default=768, help="longest side of the WebP, in pixels")
    ap.add_argument("--webp-quality", type=int, default=70)
    ap.add_argument("--raw", default="/tmp/lk-style-previews", help="folder for the drawn originals")
    ap.add_argument("--cap", type=float, default=1.0, help="stop before spending more than this, in USD")
    a = ap.parse_args()

    slugs = a.styles or list(art.STYLES)
    unknown = [s for s in slugs if s not in art.STYLES]
    if unknown:
        ap.error(f"not a built-in style: {', '.join(unknown)}")
    price = llm.image_price(a.provider, a.model, a.quality, art.PREVIEW_SHAPE)
    model = llm.make_image(a.provider, a.model)
    OUT.mkdir(parents=True, exist_ok=True)
    raw = Path(a.raw)
    raw.mkdir(parents=True, exist_ok=True)
    spent, failed = 0.0, []
    for slug in slugs:
        dest = OUT / f"{slug}.webp"
        if dest.exists() and not a.redo:
            print(f"{slug}: kept")
            continue
        if spent + (price or 0.0) > a.cap:
            print(f"stopped: the next picture would pass the ${a.cap:.2f} cap", file=sys.stderr)
            return 1
        try:
            d = model.draw(art.preview_prompt(slug), shape=art.PREVIEW_SHAPE, quality=a.quality)
        except images.ImageRefused as e:
            print(f"{slug}: refused ({e.reason})")
            failed.append(slug)
            continue
        except llm.ProviderError as e:
            print(f"{slug}: failed ({e})")
            failed.append(slug)
            continue
        spent += d.cost or (price or 0.0)
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(d.mime, "bin")
        n = len(list(raw.glob(f"{slug}-*.{ext}"))) + 1
        (raw / f"{slug}-{n}.{ext}").write_bytes(d.data)
        img = Image.open(io.BytesIO(d.data)).convert("RGB")
        img.thumbnail((a.size, a.size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=a.webp_quality, method=6)
        dest.write_bytes(buf.getvalue())
        print(f"{slug}: {img.width}x{img.height}, {len(buf.getvalue()) // 1024} KB, ${d.cost:.4f}")
    total = sum(f.stat().st_size for f in OUT.glob("*.webp"))
    print(f"spent ${spent:.4f}; previews {total // 1024} KB together" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
