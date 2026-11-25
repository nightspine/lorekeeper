"""Browser check of the book page's "Add pictures" dialog: the chapter choice, "No chapter pictures", the live estimate
and the disabled Start, against mocked routes (no book, index or provider needed).

    uv run --extra web lorekeeper web --no-auth --port 8791 --data /tmp/somewhere &
    uv run --with playwright python bench/check_art_chapters.py --base http://localhost:8791 --out /tmp/shots

Runs in WebKit at 1280, 393 and 375 wide, dark and light; prints one line per check and exits 1 on a failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

KEY = "demo-book"
IDX = "demo-index"
CHAPTERS = [{"n": i, "title": f"Chapter {i}: {'A very long chapter title that has to be cut short somewhere' if i == 7 else 'The road'}",
             "events": 0 if i == 3 else 4, "drawable": None, "screened": False} for i in range(1, 13)]
PORTRAITS = {"main": 5, "all": 9}

BOOK = {"key": KEY, "title": "The Demo Book", "authors": ["A. Writer"], "source": "upload", "status": "complete", "canBuild": True,
        "words": 90000, "cover": None, "series": None, "seriesInfo": None, "preflight": {}, "runs": [],
        "chapters": [{"n": c["n"], "title": c["title"], "story": True, "start": c["n"] * 100, "words": 7000} for c in CHAPTERS],
        "versions": [{"key": IDX, "isDefault": True, "model": "claude-haiku-5-5", "source": "generated", "complete": True,
                      "entities": 40, "facts": 300, "updated": "2026-11-20T10:00:00Z", "throughSeq": 2000}]}
PROVIDERS = {"providers": [{"id": "gemini", "label": "Gemini", "ok": True, "configured": True,
                            "images": {"default": "nano", "models": [{"id": "nano", "label": "Nano Banana", "levels": ["draft", "standard", "best"],
                                                                       "price": {"draft": 0.02, "standard": 0.035, "best": 0.1}}]}}]}


def estimate(q: dict) -> dict:
    level = q.get("level", ["main"])[0]
    raw = q.get("chapters")
    chosen = None if raw is None else {int(x) for x in raw[0].split(",") if x}
    rows = []
    for c in CHAPTERS:
        pics = 0 if level == "none" or not c["events"] else (1 if level == "main" else c["events"])
        drawn = (pics if c["n"] == 5 else 1 if c["n"] == 6 and pics else 0)
        rows.append({**c, "pictures": pics, "drawn": drawn})
    plates = sum(r["pictures"] for r in rows if chosen is None or r["n"] in chosen)
    ports = PORTRAITS.get(q.get("portraits", ["main"])[0], 0)
    return {"pictures": plates + ports, "plates": plates, "portraits": ports, "price": round((plates + ports) * 0.035, 4),
            "provider": "gemini", "model": "nano", "chapters": rows}


def run(base: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + what)
        fails += 0 if ok else 1

    with sync_playwright() as p:
        browser = p.webkit.launch()
        for width, theme in [(1280, "dark"), (1280, "light"), (393, "dark"), (393, "light"), (375, "dark")]:
            tag = f"{width}-{theme}"
            ctx = browser.new_context(viewport={"width": width, "height": 860 if width > 640 else 800}, device_scale_factor=2)
            ctx.add_init_script(f"localStorage.setItem('lk-theme', '{theme}');")
            page = ctx.new_page()
            asked, runs = [], []

            def route(r, req=None):
                u = urlparse(r.request.url)
                if u.path == "/api/art/estimate":
                    q = parse_qs(u.query, keep_blank_values=True)
                    asked.append(q)
                    return r.fulfill(json=estimate(q))
                if u.path == f"/api/catalog/books/{KEY}":
                    return r.fulfill(json=BOOK)
                if u.path == "/api/providers":
                    return r.fulfill(json=PROVIDERS)
                if u.path == f"/api/library/{IDX}/art":
                    return r.fulfill(json={"pictures": [], "styles": ["ink"], "size": 0})
                if u.path == f"/api/library/{IDX}/art/run":
                    runs.append(json.loads(r.request.post_data or "{}"))
                    return r.fulfill(json={"id": "job1"})
                return r.continue_()

            page.route("**/api/**", route)
            page.goto(f"{base}/#/book/{KEY}")
            page.get_by_role("button", name="More pictures").click()
            dlg = page.locator("dialog.modal")
            rows = dlg.locator(".art-ch-row")
            rows.first.wait_for()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            start = dlg.get_by_role("button", name="Start drawing")
            line = dlg.locator(".art-est")
            check(rows.count() == 12, f"{tag}: every chapter listed ({rows.count()})")
            check(dlg.locator(".art-ch-row input:checked").count() == 11, f"{tag}: every chapter that can get a picture is on")
            check(rows.nth(2).locator("input").is_disabled() and "nothing to draw" in rows.nth(2).inner_text(), f"{tag}: no events, box off")
            check("already drawn" in rows.nth(4).inner_text(), f"{tag}: drawn chapter says so")
            check("1 picture" in rows.nth(0).inner_text(), f"{tag}: count at main level")
            check("chapters" not in asked[-1], f"{tag}: all chosen sends no chapter list")
            check(line.inner_text().startswith("About 16 pictures"), f"{tag}: estimate with all ({line.inner_text()})")
            no_scroll = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check(no_scroll, f"{tag}: nothing scrolls sideways")
            page.wait_for_timeout(400)   # the dialog's open animation
            page.screenshot(path=str(out / f"{tag}-dialog.png"))

            dlg.get_by_text("Every moment worth drawing").click()
            page.wait_for_function("document.querySelector('.art-ch-row .art-ch-n')?.textContent === '4 pictures'")
            check("4 pictures, 1 already drawn" in rows.nth(5).inner_text(), f"{tag}: partly drawn chapter at all level")
            rows.nth(1).locator("input").click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            check(asked[-1].get("chapters") == ["1,4,5,6,7,8,9,10,11,12"], f"{tag}: unticked chapter left out ({asked[-1].get('chapters')})")
            check("10 of 11 chosen" in dlg.locator(".art-ch-count").inner_text(), f"{tag}: chosen count")

            dlg.get_by_role("button", name="None", exact=True).click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About 5 ')")
            check(asked[-1].get("chapters") == [""], f"{tag}: none sends an empty list")
            check(not start.is_disabled(), f"{tag}: portraits alone can start")
            dlg.get_by_text("No portraits").click()
            check(start.is_disabled(), f"{tag}: nothing chosen disables Start")
            check(line.inner_text() == "Choose at least one chapter, or portraits.", f"{tag}: the reason is said ({line.inner_text()})")
            page.screenshot(path=str(out / f"{tag}-nothing.png"))

            dlg.get_by_role("button", name="All", exact=True).click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            check(not start.is_disabled(), f"{tag}: All enables Start again")
            dlg.get_by_text("No chapter pictures").click()
            check(dlg.locator(".art-ch-field").is_hidden(), f"{tag}: list hidden at no chapter pictures")
            check(start.is_disabled(), f"{tag}: no chapter pictures and no portraits disables Start")
            dlg.get_by_text("Main characters").click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About 5')")
            check(asked[-1].get("level") == ["none"] and "chapters" not in asked[-1], f"{tag}: level none asked without chapters")
            start.click()
            page.wait_for_function("location.hash.includes('job1')")
            art = runs[-1].get("art", {}) if runs else {}
            check(art.get("level") == "none" and art.get("portraits") == "main" and "chapters" not in art, f"{tag}: run config {art}")
            ctx.close()
        browser.close()
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8791")
    ap.add_argument("--out", type=Path, default=Path("/tmp/art-chapters"))
    a = ap.parse_args()
    sys.exit(run(a.base, a.out))
