"""How fast do the Reader's pictures load on a phone? Cold and warm, per tab.

    uv run --with playwright python bench/picture_load.py --data /tmp/pl-data [--port 8766] [--label before] [--json out.json]

Builds a scratch data dir the first time (the bundled Dracula index plus an art set merged from bench sets: --source DIR...,
each holding art.json and the picture files), starts its own `lorekeeper web --no-auth` on --port (never the person's
server), and drives Chromium at 390x844, DPR 3, CPU 4x slower, network about 1.6 Mbps down / 150 ms RTT, with the position
at the end of the book. Per tab: bytes of pictures, picture requests, time from navigation to the first and the last
picture painted, URLs fetched twice; then a second visit on the warm cache.
Needs: pip playwright + `playwright install chromium`; the Dracula art sets (default sources below, in the main checkout).
"""
import argparse, json, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path("/Users/gperrone/projects/lorekeeper/.claude/work")
SOURCES = [WORK / "anatomy-check/bench/sets/dracula/ink", WORK / "consistent-scene-places/bench/dracula/set",
           *sorted((WORK / "portrait-chest-up/bench/sets/drac").glob("*"))]
DRACULA = ROOT / "examples/dracula/dracula.json"
TEXT_HASH_LEN = 16


def build_set(data: Path, sources):
    doc = json.loads(DRACULA.read_text())
    th = next(e["textHash"] for e in doc["editions"] if e.get("builtFrom"))
    dest = data / "art" / th / "ink"
    if (dest / "art.json").exists():
        return th
    dest.mkdir(parents=True, exist_ok=True)
    merged = None
    for src in sources:
        if not (Path(src) / "art.json").exists():
            continue
        m = json.loads((Path(src) / "art.json").read_text())
        if m.get("textHash") != th:
            continue
        if merged is None:
            merged = {**m, "style": "ink", "pictures": []}
        have = {p["id"] for p in merged["pictures"]}
        for p in m.get("pictures", []):
            f = Path(src) / p["file"]
            if p["id"] in have or not f.exists():
                continue
            shutil.copy(f, dest / p["file"])
            merged["pictures"].append(p)
    assert merged and len(merged["pictures"]) >= 25, f"only {len(merged['pictures']) if merged else 0} pictures"
    (dest / "art.json").write_text(json.dumps(merged))
    return th


def wait_up(port):
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/about", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("server did not start")


# a 1x1 gif stands in for every picture while the Overview is only the way to the tab being measured
GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
PAINT_JS = """() => { const t0 = performance.now(); window.__paint = { first: null, last: null, t0 };
  const seen = new Set(); const tick = () => { const now = performance.now() - t0;
    for (const i of document.querySelectorAll('img')) { const u = i.currentSrc || i.src; if (!u.includes('/api/art/') || !i.complete || !i.naturalWidth) continue;
      const r = i.getBoundingClientRect(); if (r.width < 1) continue; const k = u + '@' + (i.className); if (seen.has(k)) continue; seen.add(k);
      if (window.__paint.first == null) window.__paint.first = now; window.__paint.last = now; }
    window.__paint.n = seen.size; requestAnimationFrame(tick); }; tick(); }"""


def run(args):
    from playwright.sync_api import sync_playwright
    data = Path(args.data); data.mkdir(parents=True, exist_ok=True)
    th = build_set(data, args.source or SOURCES)
    # `lorekeeper web` itself, not through `uv run`, so terminate() ends the server and not only its launcher.
    srv = None if args.external else subprocess.Popen([sys.executable, "-m", "lorekeeper", "web", "--no-auth", "--port", str(args.port), "--data", str(data)],
                           cwd=ROOT, stdout=open(data / "server.log", "w"), stderr=subprocess.STDOUT)
    results = {}
    try:
        wait_up(args.port)
        base = f"http://127.0.0.1:{args.port}"
        lib = json.load(urllib.request.urlopen(f"{base}/api/library"))
        books = lib.get("indexes") or lib.get("books") or lib
        key = next(b["key"] for b in books if "dracula" in (b.get("key", "") + b.get("title", "")).lower())
        with sync_playwright() as p:
            br = p.chromium.launch()
            tabs = ["overview", "cast", "timeline", "chapters", "entry", "lightbox"]
            for tab in tabs:
                ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
                for visit in ("cold", "warm"):
                    page = ctx.new_page()
                    cdp = ctx.new_cdp_session(page)
                    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
                    cdp.send("Network.enable")
                    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 150, "downloadThroughput": 1.6e6 / 8, "uploadThroughput": 0.75e6 / 8})
                    reqs, sizes, order = {}, {}, []
                    def on_req(e):
                        if "/api/art/" in e["request"]["url"]:
                            reqs[e["requestId"]] = e["request"]["url"]; order.append(e["request"]["url"])
                    def on_fin(e):
                        if e["requestId"] in reqs:
                            sizes[e["requestId"]] = e["encodedDataLength"]
                    cdp.on("Network.requestWillBeSent", on_req); cdp.on("Network.loadingFinished", on_fin)
                    TAB = {"cast": "cast", "timeline": "timeline", "chapters": "chapters", "entry": "cast", "lightbox": "chapters"}
                    url = f"{base}/#/reader?i={key}&p=1000"
                    if tab != "overview":
                        # Land on the tab with Pictures switched off (nothing is fetched), then switch them on: what is counted
                        # is everything that tab asks for, none of it shared with another tab's earlier load.
                        page.add_init_script("if (!sessionStorage.__pl) { localStorage.setItem('lk-art-on', '0'); sessionStorage.__pl = 1; }")
                        page.goto(url)
                        page.wait_for_selector("[role=tablist]", timeout=60000)
                        page.click(f"#tab-{TAB[tab]}")
                        if tab in ("chapters", "lightbox"):
                            page.get_by_role("button", name="Oldest first").click()
                        page.wait_for_selector(".art-toggle", state="attached", timeout=60000)
                        page.wait_for_timeout(1500)
                        reqs.clear(); order.clear(); sizes.clear()
                        page.evaluate(PAINT_JS)
                        page.evaluate("document.querySelector('.art-toggle').click()")
                    else:
                        page.goto(url)
                        page.evaluate(PAINT_JS)
                    if tab == "entry":
                        page.wait_for_selector(".ent-row.has-face", timeout=90000)
                        page.evaluate(PAINT_JS); reqs.clear(); order.clear(); sizes.clear()
                        page.click(".ent-row.has-face")
                    if tab == "lightbox":
                        page.wait_for_selector(".art-plate, .art-thumb", timeout=60000)
                        page.evaluate(PAINT_JS); reqs.clear(); order.clear(); sizes.clear()
                        page.click(".art-plate, .art-thumb")
                        page.wait_for_selector(".lb-img", timeout=60000)
                    # until every picture request has finished and nothing new has started for a while
                    quiet, waited = 0, 0
                    while quiet < 4 and waited < 180:
                        page.wait_for_timeout(500); waited += 0.5
                        # not "done" before the first picture request: the manifest and the repaint come first
                        quiet = quiet + 1 if (len(sizes) == len(reqs) and (reqs or waited > 25)) else 0
                    paint = page.evaluate("window.__paint")
                    rows = {i: u for i, u in reqs.items()}
                    dup = sorted({u for u in order if order.count(u) > 1})
                    results[f"{tab}/{visit}"] = {"requests": len(reqs), "network": sum(1 for v in sizes.values() if v > 0), "kb": round(sum(sizes.values()) / 1024), "first_ms": round(paint["first"]) if paint["first"] is not None else None,
                                                 "last_ms": round(paint["last"]) if paint["last"] is not None else None, "painted": paint.get("n"), "duplicates": len(dup)}
                    page.close()
                ctx.close()
            br.close()
    finally:
        if srv:
            srv.terminate()
    print(f"\n{args.label}: pictures on a throttled phone (390x844, DPR 3, CPU 4x, 1.6 Mbps / 150 ms)")
    print(f"{'tab':<10}{'visit':<6}{'requests':>9}{'from net':>9}{'KB':>8}{'first ms':>10}{'last ms':>9}{'painted':>8}{'urls twice':>11}")
    for k, v in results.items():
        t, vi = k.split("/")
        print(f"{t:<10}{vi:<6}{v['requests']:>9}{v['network']:>9}{v['kb']:>8}{str(v['first_ms']):>10}{str(v['last_ms']):>9}{str(v['painted']):>8}{v['duplicates']:>11}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--label", default="run"); ap.add_argument("--json"); ap.add_argument("--settle", type=int, default=6)
    ap.add_argument("--external", action="store_true", help="use a server already running on --port with this --data")
    ap.add_argument("--source", action="append", type=Path)
    run(ap.parse_args())
