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
             "events": 0 if i in (3, 12) else 4, "drawable": None, "screened": False} for i in range(1, 13)]


# What each chapter gets per level. Chapter 3 has only minor events (`events` 0) yet gets its recap plate; chapter 9 has
# events but gets nothing at level "all" (pictures 0 there only); chapter 12 gets nothing at any level.
def pictures_at(c: dict, level: str) -> int:
    if level == "none" or c["n"] == 12:
        return 0
    if c["n"] == 3:
        return 1
    if c["n"] == 9:
        return 1 if level == "main" else 0
    return 1 if level == "main" else c["events"]
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
        pics = pictures_at(c, level)
        drawn = (pics if c["n"] == 5 else 1 if c["n"] == 6 and pics else 0)
        rows.append({**c, "pictures": pics, "drawn": drawn})
    # As the server answers since web round 2: `pictures` and `price` count only what is still to draw, `planned` all of it.
    picked = [r for r in rows if chosen is None or r["n"] in chosen]
    plates = sum(r["pictures"] for r in picked)
    todo = sum(max(0, r["pictures"] - r["drawn"]) for r in picked)
    ports = PORTRAITS.get(q.get("portraits", ["main"])[0], 0)
    return {"pictures": todo + ports, "plates": plates, "portraits": ports, "price": round((todo + ports) * 0.035, 4),
            "planned": {"pictures": plates + ports}, "provider": "gemini", "model": "nano", "chapters": rows}


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
            check(rows.nth(11).locator("input").is_disabled() and "nothing to draw" in rows.nth(11).inner_text(), f"{tag}: no pictures, box off")
            check(rows.nth(2).locator("input").is_checked() and "1 picture" in rows.nth(2).inner_text(),
                  f"{tag}: events 0 with a recap plate is choosable ({rows.nth(2).inner_text()})")
            check("11 of 11 chosen" in dlg.locator(".art-ch-count").inner_text(), f"{tag}: chosen count at main")
            check("already drawn" in rows.nth(4).inner_text(), f"{tag}: drawn chapter says so")
            check("1 picture" in rows.nth(0).inner_text(), f"{tag}: count at main level")
            check("chapters" not in asked[-1], f"{tag}: all chosen sends no chapter list")
            check(line.inner_text().startswith("About 14 pictures"), f"{tag}: estimate with all, drawn ones left out ({line.inner_text()})")
            no_scroll = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check(no_scroll, f"{tag}: nothing scrolls sideways")
            page.wait_for_timeout(400)   # the dialog's open animation
            page.screenshot(path=str(out / f"{tag}-dialog.png"))

            dlg.get_by_text("Every moment worth drawing").click()
            page.wait_for_function("document.querySelector('.art-ch-row .art-ch-n')?.textContent === '4 pictures'")
            check("4 pictures, 1 already drawn" in rows.nth(5).inner_text(), f"{tag}: partly drawn chapter at all level")
            check(rows.nth(8).locator("input").is_disabled() and not rows.nth(8).locator("input").is_checked()
                  and "nothing to draw" in rows.nth(8).inner_text(), f"{tag}: events but no pictures at this level, box off")
            check("10 of 10 chosen" in dlg.locator(".art-ch-count").inner_text(), f"{tag}: chosen count follows the level")
            rows.nth(1).locator("input").click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            check(asked[-1].get("chapters") == ["1,3,4,5,6,7,8,10,11"], f"{tag}: unticked and unchoosable chapters left out ({asked[-1].get('chapters')})")
            check("9 of 10 chosen" in dlg.locator(".art-ch-count").inner_text(), f"{tag}: chosen count")
            dlg.get_by_text("The main moment of each chapter").click()
            page.wait_for_function("document.querySelector('.art-ch-row .art-ch-n')?.textContent === '1 picture'")
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            page.wait_for_timeout(500)   # the second ask, made when the answer changed which rows can be ticked
            check(rows.nth(8).locator("input").is_checked() and not rows.nth(1).locator("input").is_checked(),
                  f"{tag}: back at main, the ticks are kept")
            check(asked[-1].get("chapters") == ["1,3,4,5,6,7,8,9,10,11"], f"{tag}: asked again with the choice as it is now ({asked[-1].get('chapters')})")
            dlg.get_by_text("Every moment worth drawing").click()
            page.wait_for_function("document.querySelector('.art-ch-row .art-ch-n')?.textContent === '4 pictures'")

            dlg.get_by_role("button", name="None", exact=True).click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About 5 ')")
            check(asked[-1].get("chapters") == [""], f"{tag}: none sends an empty list")
            check(not start.is_disabled(), f"{tag}: portraits alone can start")
            dlg.get_by_text("No portraits").click()
            check(start.is_disabled(), f"{tag}: nothing chosen disables Start")
            check(line.inner_text() == "Choose at least one chapter, or portraits.", f"{tag}: the reason is said ({line.inner_text()})")
            page.screenshot(path=str(out / f"{tag}-nothing.png"))

            # Only chapter 5, whose pictures are all drawn, and no portraits: the estimate answers 0 still to draw.
            rows.nth(4).locator("input").click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent === 'Everything you chose is already drawn.'")
            check(asked[-1].get("chapters") == ["5"], f"{tag}: asked for the drawn chapter alone ({asked[-1].get('chapters')})")
            check(start.is_disabled(), f"{tag}: everything already drawn disables Start")
            page.screenshot(path=str(out / f"{tag}-drawn.png"))

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


def run_real(base: str, out: Path, key: str) -> int:
    """Against a real server and a real book (catalog `key` with an index built from its EPUB): the book, the index and
    the estimate are the server's; only the provider list (an image key) and the run itself are mocked."""
    out.mkdir(parents=True, exist_ok=True)
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + what)
        fails += 0 if ok else 1

    with sync_playwright() as p:
        browser = p.webkit.launch()
        for width, theme in [(1280, "dark"), (393, "light")]:
            tag = f"real-{width}-{theme}"
            ctx = browser.new_context(viewport={"width": width, "height": 860 if width > 640 else 800}, device_scale_factor=2)
            ctx.add_init_script(f"localStorage.setItem('lk-theme', '{theme}');")
            page = ctx.new_page()
            answers, runs = [], []

            def route(r):
                u = urlparse(r.request.url)
                if u.path == "/api/providers":   # the server's own list and image models, with Gemini marked as set up
                    resp = r.fetch()
                    body = resp.json()
                    for prov in body.get("providers", []):
                        if prov.get("id") == "gemini":
                            prov.update(ok=True, configured=True)
                    return r.fulfill(response=resp, json=body)
                if u.path.endswith("/art/run"):
                    runs.append(json.loads(r.request.post_data or "{}"))
                    return r.fulfill(json={"id": "job1"})
                if u.path == "/api/art/estimate":
                    resp = r.fetch()
                    try:
                        answers.append((parse_qs(u.query, keep_blank_values=True), resp.status, resp.json()))
                    except Exception:
                        answers.append((parse_qs(u.query, keep_blank_values=True), resp.status, None))
                    return r.fulfill(response=resp)
                return r.continue_()

            page.route("**/api/**", route)
            page.goto(f"{base}/#/book/{key}")
            page.get_by_role("button", name="pictures").click()
            dlg = page.locator("dialog.modal")
            rows = dlg.locator(".art-ch-row")
            rows.first.wait_for()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            page.wait_for_timeout(400)
            q, status, body = answers[-1]
            chs = (body or {}).get("chapters") or []
            check(status == 200 and rows.count() == len(chs), f"{tag}: one row per chapter the estimate lists ({rows.count()} of {len(chs)})")
            boxes = rows.locator("input")
            agree = all(boxes.nth(i).is_disabled() == (not (c.get("pictures") or 0) > 0) for i, c in enumerate(chs))
            check(agree, f"{tag}: a box is off exactly where the chapter gets no picture at the level")
            odd = [c["n"] for c in chs if (c.get("pictures") or 0) > 0 and not c.get("events")]
            print(f"     {len(chs)} chapters, {sum(1 for c in chs if (c.get('pictures') or 0) > 0)} choosable, events 0 but a picture: {odd}")
            print(f"     estimate line: {dlg.locator('.art-est').inner_text()!r}")
            page.screenshot(path=str(out / f"{tag}-dialog.png"))
            first = next(i for i, c in enumerate(chs) if (c.get("pictures") or 0) > 0)
            boxes.nth(first).click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            page.wait_for_timeout(300)
            q2, status2, body2 = answers[-1]
            sent = q2.get("chapters", [None])[0]
            check(status2 == 200 and sent is not None and str(chs[first]["n"]) not in sent.split(","),
                  f"{tag}: unticking asks without that chapter (status {status2})")
            check((body2 or {}).get("pictures", 0) < body["pictures"], f"{tag}: total drops ({body['pictures']} → {(body2 or {}).get('pictures')})")
            dlg.get_by_role("button", name="None", exact=True).click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            page.wait_for_timeout(300)
            q3, status3, body3 = answers[-1]
            check(status3 == 200 and q3.get("chapters") == [""] and (body3 or {}).get("plates") == 0,
                  f"{tag}: no chapters, portraits only ({(body3 or {}).get('pictures')} pictures)")
            dlg.get_by_text("No portraits").click()
            check(dlg.get_by_role("button", name="Start drawing").is_disabled(), f"{tag}: nothing chosen disables Start")
            dlg.get_by_role("button", name="All", exact=True).click()
            dlg.get_by_text("No chapter pictures").click()
            dlg.get_by_text("Main characters").click()
            page.wait_for_function("document.querySelector('.art-est')?.textContent.startsWith('About')")
            page.wait_for_timeout(300)
            q4, status4, body4 = answers[-1]
            check(status4 == 200 and q4.get("level") == ["none"] and (body4 or {}).get("plates") == 0 and (body4 or {}).get("portraits", 0) > 0,
                  f"{tag}: level none, main characters: {(body4 or {}).get('portraits')} portraits, no plates")
            page.screenshot(path=str(out / f"{tag}-portraits.png"))
            dlg.get_by_role("button", name="Start drawing").click()
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
    ap.add_argument("--real", metavar="CATALOG_KEY", help="check a real book on a real server instead of the mocks")
    a = ap.parse_args()
    sys.exit(run_real(a.base, a.out, a.real) if a.real else run(a.base, a.out))
