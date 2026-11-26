"""Browser check of the entry's "Portraits" section and the pictures estimate range, against mocked art routes on the
bundled examples (Dracula standalone, Sherlock Holmes as a series). No book, provider or picture needs to exist.

    uv run --extra web lorekeeper web --no-auth --port 8793 --data /tmp/somewhere &
    uv run --with playwright python bench/check_entry_portraits.py --base http://localhost:8793 --out /tmp/shots

Runs in WebKit at 1280 (the pane), 1000 (the drawer), 393 and 375 (the phone sheet), dark and light; prints one line per
check and exits 1 on a failure.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DRACULA = "examples--dracula--dracula"
SCARLET = "examples--sherlock-holmes--study-in-scarlet"
SIGN = "examples--sherlock-holmes--sign-of-four"
# A 3x4 grey PNG stands in for every picture.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAMAAAAECAIAAADETxJQAAAAEklEQVR4nGNoaGhgQAMMQwMHAPxVBPHk8EtbAAAAAElFTkSuQmCC")


def doc(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text())


def built(d: dict) -> str:
    return next(e["textHash"] for e in d["editions"] if e.get("builtFrom"))


def pic(i: int, kind: str, target: str, frm: str, chapter: str | None = None) -> dict:
    return {"id": f"p{i:08x}", "kind": kind, "target": target, "from": frm, "chapter": chapter, "sources": [frm], "prompt": "x",
            "provider": "gemini", "model": "nano", "quality": "draft", "shape": "portrait", "cost": 0.03, "created": "2026-11-25T10:00:00Z",
            "notes": 3, "ext": "png", "url": None}


def art_set(d: dict, pics: list[dict]) -> dict:
    h = built(d)
    for p in pics:
        p["url"] = f"/api/art/{h}/ink/{p['id']}"
    return {"manifest": {"lorekeeperArt": "0.1", "textHash": h, "style": "Ink", "pictures": pics, "screened": {}}, "slug": "ink",
            "styles": [{"slug": "ink", "style": "Ink", "pictures": len(pics)}], "size": 1}


def sets() -> dict:
    dr, sc, sg = doc("dracula/dracula.json"), doc("sherlock-holmes/study-in-scarlet.json"), doc("sherlock-holmes/sign-of-four.json")
    # Van Helsing: three portraits by p=600 and one at the last anchor of the book, which must never show or count.
    last = dr["anchors"][-1]["id"]
    vh = [pic(1, "portrait", "van-helsing", "toc14.para12"), pic(2, "portrait", "van-helsing", "toc17.para35"),
          pic(3, "portrait", "van-helsing", "toc20.para03", "CHAPTER XVII SEWARD’S DIARY—continued"), pic(4, "portrait", "van-helsing", last)]
    # Mina has one: no section. Castle Dracula (a place) has two: "Pictures".
    mina = [pic(5, "portrait", "mina", "toc17.para35")]
    castle = [pic(6, "place", "castle-dracula", "toc06.para39"), pic(7, "place", "castle-dracula", "toc10.para41")]
    holmes1 = [pic(11, "portrait", "holmes", sc["anchors"][10]["id"]), pic(12, "portrait", "holmes", sc["anchors"][40]["id"])]
    holmes2 = [pic(13, "portrait", "holmes", sg["anchors"][3]["id"]), pic(14, "portrait", "holmes", sg["anchors"][-1]["id"])]
    return {DRACULA: art_set(dr, vh + mina + castle), SCARLET: art_set(sc, holmes1), SIGN: art_set(sg, holmes2)}


PROVIDERS = {"providers": [{"id": "gemini", "label": "Gemini", "ok": True, "configured": True,
                            "images": {"default": "nano", "models": [{"id": "nano", "label": "Nano Banana", "levels": ["draft", "standard", "best"],
                                                                       "price": {"draft": 0.02, "standard": 0.035, "best": 0.1}}]}}]}


def run(base: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    fails = 0
    art = sets()

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + what)
        fails += 0 if ok else 1

    with sync_playwright() as p:
        browser = p.webkit.launch()
        for width, theme in [(1280, "dark"), (1280, "light"), (1000, "dark"), (393, "dark"), (393, "light"), (375, "dark")]:
            tag = f"{width}-{theme}"
            phone = width <= 640
            ctx = browser.new_context(viewport={"width": width, "height": 860 if not phone else 800}, device_scale_factor=2,
                                      is_mobile=phone, has_touch=phone)
            ctx.add_init_script(f"localStorage.setItem('lk-theme', '{theme}'); localStorage.setItem('lk-art-on', '1');")
            page = ctx.new_page()

            def route(r):
                u = urlparse(r.request.url)
                parts = u.path.split("/")
                if u.path.startswith("/api/art/") and len(parts) == 6:
                    return r.fulfill(body=PNG, content_type="image/png")
                if u.path == "/api/providers":
                    return r.fulfill(json=PROVIDERS)
                if len(parts) == 5 and parts[1:3] == ["api", "library"] and parts[4] == "art":
                    got = art.get(parts[3])
                    return r.fulfill(json=got) if got else r.fulfill(status=404, json={"detail": "none"})
                return r.continue_()

            page.route("**/api/**", route)

            def open_entry(eid: str) -> None:
                page.locator("#tab-cast").click()
                page.locator(f'button.ent-row[data-ent="{eid}"]').first.click()
                page.wait_for_timeout(700)

            box = "dialog.drawer" if width <= 1100 else ".entry-pane"
            # ---- standalone: Van Helsing at 60% ----
            page.goto(f"{base}/#/reader?i={DRACULA}&p=600")
            page.locator("#tab-cast").wait_for()
            page.wait_for_timeout(1200)
            open_entry("van-helsing")
            sec = page.locator(f"{box} .art-portraits")
            check(sec.count() == 1, f"{tag}: Van Helsing has a Portraits section")
            tiles = sec.locator(".portrait-tile")
            check(tiles.count() == 3, f"{tag}: three portraits by the position, the later one neither shown nor counted ({tiles.count()})")
            check(sec.locator(".eyebrow").inner_text().replace("\n", "").replace(" ", "") == "Portraits3", f"{tag}: label and count ({sec.locator('.eyebrow').inner_text()!r})")
            caps = [tiles.nth(i).locator(".portrait-cap").inner_text() if tiles.nth(i).locator(".portrait-cap").count() else "" for i in range(tiles.count())]
            check(all(c for c in caps), f"{tag}: every portrait names its chapter {caps}")
            check(all(len(c) <= 16 for c in caps), f"{tag}: captions are short {caps}")
            check(tiles.nth(2).locator(".portrait-now").inner_text() == "Current" and tiles.locator(".portrait-now").count() == 1,
                  f"{tag}: the latest is marked current, only it")
            check(tiles.nth(2).locator("button").get_attribute("aria-current") == "true", f"{tag}: current has aria-current")
            prof = page.locator(f"{box} .profile")
            check(prof.count() and sec.evaluate("(n) => n.previousElementSibling?.classList.contains('profile')"), f"{tag}: Portraits sits right under Who they are")
            check(page.locator(f"{box} details.history .art-earlier, {box} details.history .art-thumb").count() == 0, f"{tag}: Earlier impressions is text only")
            check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{tag}: nothing scrolls sideways")
            sec.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / f"{tag}-dracula-entry.png"))
            tiles.nth(0).locator("button").click()
            lb = page.locator("dialog.lightbox")
            lb.wait_for()
            check(caps[0] in lb.locator(".lb-caption").inner_text(), f"{tag}: the lightbox opens at the tapped one ({lb.locator('.lb-caption').inner_text()})")
            check("1" in (lb.locator(".lb-head").inner_text()) and "3" in lb.locator(".lb-head").inner_text(), f"{tag}: lightbox steps through 3 ({lb.locator('.lb-head').inner_text()!r})")
            page.wait_for_timeout(300)
            page.screenshot(path=str(out / f"{tag}-dracula-lightbox.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            # Mina: one portrait, no section.
            if width <= 1100:
                page.keyboard.press("Escape"); page.wait_for_timeout(500)
            open_entry("mina")
            check(page.locator(f"{box} .art-portraits").count() == 0, f"{tag}: one portrait, no section")
            if width <= 1100:
                page.keyboard.press("Escape"); page.wait_for_timeout(500)
            open_entry("castle-dracula")
            sec = page.locator(f"{box} .art-portraits")
            check(sec.count() == 1 and sec.locator(".eyebrow").inner_text().startswith("Pictures"), f"{tag}: a place's section is Pictures")

            # Back before the second portrait: the section goes (one left).
            page.goto(f"{base}/#/reader?i={DRACULA}&p=330")
            page.reload()
            page.locator("#tab-cast").wait_for(); page.wait_for_timeout(1200)
            open_entry("van-helsing")
            n = page.locator(f"{box} .art-portraits .portrait-tile").count()
            check(n in (0, 2), f"{tag}: earlier position shows fewer portraits ({n})")

            # ---- series: Sign of Four with A Study in Scarlet finished ----
            page.goto(f"{base}/#/reader?i={SIGN}&p=500&fin=study-in-scarlet")
            page.reload()
            page.locator("#tab-cast").wait_for(); page.wait_for_timeout(1500)
            open_entry("holmes")
            sec = page.locator(f"{box} .art-portraits")
            books = sec.locator(".portrait-book")
            check(books.count() == 2, f"{tag}: series portraits grouped per book ({books.count()})")
            if books.count() == 2:
                check(books.nth(0).locator(".portrait-tile").count() == 2, f"{tag}: every portrait of the finished book, not only its latest")
                check(books.nth(1).locator(".portrait-tile").count() == 1, f"{tag}: the current book up to the position only")
                check("Study in Scarlet" in books.nth(0).locator(".portrait-book-title").inner_text(), f"{tag}: oldest book first ({books.nth(0).locator('.portrait-book-title').inner_text()})")
            check(page.locator(f"{box} .arc-card .art-thumb").count() == 0, f"{tag}: the series cards carry no pictures")
            check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{tag}: series, nothing scrolls sideways")
            sec.scroll_into_view_if_needed(); page.wait_for_timeout(400)
            page.screenshot(path=str(out / f"{tag}-series-entry.png"))
            ctx.close()
        browser.close()
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8793")
    ap.add_argument("--out", type=Path, default=Path("/tmp/entry-portraits"))
    a = ap.parse_args()
    sys.exit(run(a.base, a.out))


if __name__ == "__main__":
    main()
