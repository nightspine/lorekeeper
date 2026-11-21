"""Render the app icons (apple-touch-icon 180px, manifest 192 and 512) from the brand mark with Playwright.
Run: uv run --with playwright python bench/make_touch_icon.py"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent.parent / "src/lorekeeper/web/static/icons"
# The rail's horizon mark (a lit line, the orb, an unlit line) on the dark ground, with room around it (iOS rounds the corners itself).
SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100' width='{n}' height='{n}'>
<rect width='100' height='100' fill='#0C0E12'/>
<defs><radialGradient id='g'><stop offset='0' stop-color='#5CE1D6' stop-opacity='.55'/><stop offset='1' stop-color='#5CE1D6' stop-opacity='0'/></radialGradient></defs>
<circle cx='55' cy='50' r='30' fill='url(#g)'/>
<path d='M16 50h39' stroke='#5CE1D6' stroke-width='7' stroke-linecap='round'/>
<path d='M64 50h20' stroke='#5CE1D6' stroke-opacity='.28' stroke-width='7' stroke-linecap='round'/>
<circle cx='55' cy='50' r='10' fill='#5CE1D6'/></svg>"""

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for n, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"), (512, "icon-512.png")):
            pg = await b.new_page(viewport={"width": n, "height": n})
            await pg.set_content(f"<body style='margin:0'>{SVG.format(n=n)}</body>")
            await pg.screenshot(path=str(OUT / name))
        await b.close()

asyncio.run(main())
