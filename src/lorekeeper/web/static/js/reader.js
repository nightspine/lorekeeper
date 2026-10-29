import { api } from "./api.js";
import { parseHash, setParams } from "./app.js";
import { announce, clear, describe, h, icon, relTime, TYPE_LABEL, TYPE_PLURAL, toast, typeTag } from "./dom.js";
import { view as gate } from "./gating.js";
import { confirmDialog, cover, modal } from "./build.js";
import { mergeWith, openPairs, reviewDuplicates, visiblePairs } from "./dupes.js";
import { renderConnections } from "./graph.js";

const TYPES = ["person", "creature", "place", "group", "object", "concept", "term"];

/**
 * Maps a reading position (0..1 of the book's words) to the last anchor passed, following
 * SPEC.md §3.6–3.7: an anchor counts as passed once its whole block has been read, and an anchor
 * without a usable location takes the next located anchor's position (never earlier).
 */
class PositionMap {
  constructor(doc, edition) {
    this.anchors = doc.anchors;
    this.exact = Boolean(edition);
    if (edition) {
      this.cum = [];
      let total = 0;
      for (const w of edition.blockWords) { total += w; this.cum.push(total); }
      this.total = total;
      this.chapters = edition.chapters;
    } else {
      // Without the book file, spread the known anchor positions evenly: approximate but monotonic.
      const seqs = this.anchors.map((a) => a.seq).filter((s) => s != null);
      this.total = Math.max(1, (seqs.length ? Math.max(...seqs) : this.anchors.length) + 1);
      this.cum = null;
      this.chapters = [];
    }
    // Exact: seq hints are block numbers in this very edition. Approximate: fall back to anchor order.
    const loc = this.anchors.map((a, i) => (this.exact ? (a.seq != null ? this.blockEnd(a.seq) : Infinity)
      : this.blockEnd(a.seq ?? i)));
    this.eff = new Array(loc.length);
    let next = Infinity;
    for (let i = loc.length - 1; i >= 0; i--) { if (Number.isFinite(loc[i])) next = loc[i]; this.eff[i] = next; }
  }
  /** Fraction of the book read once block `seq` is finished. */
  blockEnd(seq) {
    if (seq == null) return Infinity;
    if (!this.cum) return (seq + 1) / this.total;
    return seq < this.cum.length ? this.cum[seq] / this.total : Infinity;
  }
  blockStart(seq) { return seq <= 0 ? 0 : this.blockEnd(seq - 1); }
  frontier(p) {
    let lo = 0, hi = this.eff.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.eff[mid] <= p + 1e-12) lo = mid + 1; else hi = mid; }
    return lo ? this.anchors[lo - 1].id : null;
  }
  anchorPos(id) { const i = this.anchors.findIndex((a) => a.id === id); return i < 0 ? null : this.eff[i]; }
  chapterAt(p) {
    if (!this.cum) return null;
    let ch = null;
    for (const c of this.chapters) if (this.blockStart(c.start) <= p + 1e-12) ch = c; else break;
    return ch;
  }
}

export function mountReader(root) {
  const { params } = parseHash();
  const s = {
    lib: null, key: params.get("i"), doc: null, map: null, series: null, seriesDocs: new Map(),
    book: params.get("book"), order: params.get("order"), finished: params.get("fin"),
    p: params.has("p") ? Math.min(1, Math.max(0, +params.get("p") / 1000)) : 0,
    tab: "chapters", types: new Set(), q: "", sort: "first", newestFirst: true, chaptersNewest: true, prevIds: null, prevP: null, view: null, drawer: null,
    sync: { on: false, override: false }, graph: { sel: null, mode: "map", hops: 1, together: false, things: false, hideMinor: false, history: false, list: false, q: "" }, reveals: null,
  };
  const wrap = h("div.reader");
  root.append(wrap);
  let disposed = false;

  (async () => {
    try { s.lib = await api.library(); } catch (e) { toast(`Could not load the library: ${e.message}`, "error"); return; }
    if (!s.lib.indexes.some((i) => i.key === s.key)) {
      clear(wrap, h("div.card.empty", h("div.art", icon("book-open")), h("h3", "This index isn't in the library any more"),
        h("p", "It may have been deleted or rebuilt. Pick a book to read from the Library."),
        h("a.btn.primary", { href: "#/library?show=ready" }, icon("layers"), "Books ready to read")));
      return;
    }
    api.catalog().then((c) => { s.catalog = c; if (el.crumbs) paintHead(); }).catch(() => {});
    await load(s.key, { keepPosition: true });
  })();

  async function load(key, { keepPosition = false, book = null } = {}) {
    s.key = key;
    const meta = s.lib.indexes.find((i) => i.key === key);
    try { s.doc = await api.index(key); } catch (e) { return toast(`Could not open index: ${e.message}`, "error"); }
    if (disposed) return;
    s.series = null; s.seriesDocs = new Map();
    const sid = s.doc.series?.id;
    if (sid) {
      const man = s.lib.series.find((x) => x.id === sid);
      if (man) {
        s.series = man;
        const siblings = s.lib.indexes.filter((i) => i.series?.id === sid);
        for (const b of man.books) {
          const cands = siblings.filter((i) => i.series.book === b.key);
          const choice = b.key === s.doc.series.book ? meta : cands.find((c) => c.source === meta.source) || cands[0];
          if (!choice) continue;
          s.seriesDocs.set(b.key, choice.key === key ? s.doc : await api.index(choice.key).catch(() => null));
        }
        if (!s.order || !man.orders[s.order]) s.order = man.defaultOrder;
      }
    }
    s.book = book || s.doc.series?.book || null;
    // seq hints are only valid for the edition the index was built from.
    const hashes = (s.doc.editions || []).filter((e) => e.builtFrom).map((e) => e.textHash);
    let edition = null;
    for (const hsh of hashes) { edition = await api.edition(hsh); if (edition) break; }
    s.map = new PositionMap(s.doc, edition);
    if (!keepPosition) s.p = 0;
    s.prevIds = null; s.prevP = null; s.fresh = new Set();
    s.reveals = null;
    s.sync = { on: false, override: false, ahead: false, wantAhead: null };
    s.dupes = await api.duplicates(key).catch(() => null);
    watchDupes(key);
    s.graph.pos = null; s.graph.sel = null; s.graph.userView = false; s.graph.arch = null;
    render();
    await syncPosition(key);
  }

  // ---- Book Orbit position sync ------------------------------------------------------------

  /** Convert a position response to a 0..1 reading position (the block being read counts as unread). */
  function syncedP(r) {
    const p = typeof r.seq === "number" && s.map.exact ? s.map.blockStart(r.seq) : r.p;
    return Number.isFinite(p) ? Math.min(1, Math.max(0, p)) : null;
  }

  const lastKey = () => `lk-synced-${s.key}`;
  function readLast() {
    try { const v = localStorage.getItem(lastKey()); return v == null ? null : Math.min(1, Math.max(0, +v)); } catch { return null; }
  }
  function writeLast(p) { try { localStorage.setItem(lastKey(), String(p)); } catch {} }

  /** Gated ids at position p, to mark what was revealed since then as new. */
  function idsAt(p) {
    const anchor = s.map.frontier(p);
    let v;
    try { v = gate(currentIndexes(), { book: s.book, anchor }, s.series, s.order, finishedSet()); }
    catch { v = gate([s.doc], { book: null, anchor }, null); }
    return new Set([...v.entities.map((e) => e.id), ...v.events.map((e) => e.id), ...v.chapters.map((c) => c.id),
      ...v.entities.flatMap((e) => e.facets.map((f) => f.id)), ...v.entities.filter((e) => e.profile).map((e) => e.profile.id)]);
  }

  async function syncPosition(key, { poll = false } = {}) {
    let r;
    try { r = await api.position(key); } catch { r = { synced: false, reason: "unreachable" }; }
    if (disposed || s.key !== key) return;
    if (!r || !r.synced) {
      if (!poll || !s.sync.on) { s.sync = { on: false, override: false, reason: r?.reason }; paintSource(); }
      return;
    }
    const p = syncedP(r);
    if (p == null) return;
    const first = !s.sync.on;
    if (!first && s.sync.updatedAt === r.updatedAt) { s.sync.fetchedAt = Date.now(); return; }
    const override = s.sync.override === true, ahead = s.sync.ahead === true;
    s.sync = { on: true, override, ahead, p, updatedAt: r.updatedAt, basis: r.basis, percentage: r.percentage, fetchedAt: Date.now() };
    if (override) { paintSource(); return; }
    // "New" needs something to compare against: the position this device last synced, when it is behind.
    let prev = null;
    if (first) {
      const last = readLast();
      if (last != null && p > last + 1e-9) {
        try { s.prevIds = idsAt(last); prev = last; } catch {}
      }
    }
    setP(p, first ? (prev != null ? { prev } : { quiet: true }) : {});
    if (!first) announce("Position updated from Book Orbit");
    writeLast(p);
  }

  let pollTimer;
  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (!document.hidden && s.sync.on) syncPosition(s.key, { poll: true }); }, 60000);
  }
  const onVisible = () => { if (!document.hidden && s.sync.on) syncPosition(s.key, { poll: true }); };
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("focus", onVisible);
  startPolling();

  /** Called for every manual move. */
  function markOverride() {
    // Counts before the first sync response too, so a move made while it loads is not overwritten.
    if (!s.sync.override) { s.sync.override = true; if (s.sync.on) paintSource(); }
  }
  /** The position Book Orbit holds, when going past it would spoil: synced, not yet allowed ahead, book unfinished. */
  function aheadLine() {
    const y = s.sync;
    return y.on && !y.ahead && (y.percentage == null || y.percentage < 99.5) ? y.p : null;
  }
  /** Every deliberate move goes through here. Past Book Orbit's line it asks first; "no" returns to the line. */
  async function moveTo(p) {
    const line = aheadLine();
    if (line != null && p > line + 1e-9) {
      const t = s.sync.updatedAt ? Date.parse(s.sync.updatedAt) / 1000 : NaN;
      const ok = await confirmDialog({
        title: "Go past where you're reading?",
        message: `Book Orbit last saw you at ${Math.round(line * 100)}%${Number.isFinite(t) ? `, ${relTime(t)}` : ""}. `
          + `Moving to ${Math.round(p * 100)}% shows characters, facts and events from pages you haven't read yet.`,
        confirmLabel: "Show me anyway", cancelLabel: `Stay at ${Math.round(line * 100)}%`, danger: true });
      if (!ok) { s.sync.override = false; setP(line); paintSource(); return; }
      s.sync.ahead = true;
    }
    markOverride();
    setP(p);
  }
  function backToSynced() {
    if (!s.sync.on) return;
    s.sync.override = false; s.sync.ahead = false; s.sync.wantAhead = null;
    setP(s.sync.p);
    writeLast(s.sync.p);
    syncPosition(s.key, { poll: true });
  }

  function paintSource() {
    if (!el.source) return;
    const y = s.sync;
    const back = () => h("button.linkish", { type: "button", on: { click: backToSynced } },
      `Back to Book Orbit's position (${Math.round(y.p * 100)}%)`);
    if (y.on && y.override) clear(el.source, y.ahead && s.p > y.p + 1e-9 ? "Ahead of where Book Orbit says you are" : "Moved by hand", back());
    else if (y.on) {
      const t = y.updatedAt ? Date.parse(y.updatedAt) / 1000 : NaN;
      clear(el.source, h("span.dot-live", { "aria-hidden": "true" }),
        `From Book Orbit${Number.isFinite(t) ? `, ${relTime(t)}` : ""}`);
    } else if (y.reason === "unreachable") {
      clear(el.source, "Couldn't reach Book Orbit",
        h("button.linkish", { type: "button", on: { click: () => syncPosition(s.key) } }, "Try again"));
    } else clear(el.source);
  }

  function currentIndexes() {
    if (!s.series) return [s.doc];
    return [...s.seriesDocs.values()].filter(Boolean);
  }

  function finishedSet() {
    if (!s.series) return null;
    const order = s.series.orders[s.order];
    const before = order.slice(0, order.indexOf(s.book));
    if (s.finished == null) return before;
    const chosen = new Set(s.finished.split(",").filter(Boolean));
    return before.filter((b) => chosen.has(b));
  }

  function compute() {
    const anchor = s.map.frontier(s.p);
    try {
      s.view = gate(currentIndexes(), { book: s.book, anchor }, s.series, s.order, finishedSet());
    } catch (e) {
      s.view = gate([s.doc], { book: null, anchor }, null);   // series data incomplete: gate this book alone
    }
    return anchor;
  }

  let urlTimer;
  function saveUrl() {
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => {
      const q = { i: s.key, p: String(Math.round(s.p * 1000)) };
      if (s.series) { q.book = s.book; q.order = s.order; if (s.finished != null) q.fin = s.finished; }
      setParams(q);
    }, 250);
  }

  // ---- layout ----------------------------------------------------------------------------

  const el = {};
  function render() {
    el.crumbs = h("nav.crumbs", { "aria-label": "Breadcrumb" });
    el.headFacts = h("div.head-facts");
    const head = h("div.reader-head",
      h("div.title", el.crumbs, h("h1", s.doc.work.title), h("p.by", `by ${s.doc.work.authors.join(", ")}`), el.headFacts),
      h("div.reader-switch", h("button.btn", { type: "button", on: { click: pickBook } }, icon("layers"), "Switch book")));
    paintHead();

    el.pctNum = document.createTextNode("");
    el.pct = h("span.pct.num", el.pctNum, h("span.pct-unit", "%"));
    el.chap = h("span.chap");
    el.source = h("div.pos-source");
    el.range = h("input.range", { type: "range", min: "0", max: "1000", step: "1", value: String(Math.round(s.p * 1000)),
      "aria-label": "Reading position in the book" });
    el.ticks = h("div.ticks");
    el.labels = h("div.hz-labels", { "aria-hidden": "true" });
    el.pctInput = h("input.input.pct-input", { type: "number", min: "0", max: "100", step: "1", "aria-label": "Reading position, percent" });
    el.chapSel = h("select.select#jump-chapter", { "aria-label": "Jump to chapter" });
    el.hint = h("p.hint.pos-hint");
    el.notice = h("div.pos-notices");
    el.series = h("div.pos-series");

    const prev = h("button.icon-btn", { type: "button", "aria-label": "Previous reveal", title: "Previous reveal" }, icon("skip-back"));
    const next = h("button.icon-btn", { type: "button", "aria-label": "Next reveal", title: "Next reveal" }, icon("skip-fwd"));
    prev.addEventListener("click", () => stepReveal(-1));
    next.addEventListener("click", () => stepReveal(1));
    // Dragging past where Book Orbit says you are stops at that line; letting go there asks first (moveTo).
    el.range.addEventListener("input", () => {
      const want = +el.range.value / 1000;
      const line = aheadLine();
      if (line != null && want > line + 1e-9) {
        el.range.value = String(Math.round(line * 1000));
        s.sync.wantAhead = want;
        markOverride();
        setP(line, { defer: true });
        return;
      }
      s.sync.wantAhead = null;
      markOverride();
      setP(want, { defer: true });
    });
    el.range.addEventListener("change", () => {
      const want = s.sync.wantAhead;
      s.sync.wantAhead = null;
      if (want != null) moveTo(want);
    });
    el.pctInput.addEventListener("change", () => moveTo(Math.min(100, Math.max(0, +el.pctInput.value || 0)) / 100));
    el.chapSel.addEventListener("change", () => {
      const v = el.chapSel.value;
      if (!v) return;
      const [kind, seq] = v.split(":");
      const c = s.map.chapters.find((x) => String(x.start) === seq);
      const idx = s.map.chapters.indexOf(c);
      const nextStart = s.map.chapters[idx + 1]?.start;
      moveTo(kind === "start" ? s.map.blockStart(c.start) : nextStart != null ? s.map.blockStart(nextStart) : 1);
      el.chapSel.value = "";
    });

    // The horizon: what you've read is lit, what is ahead stays dark. The real range input sits transparent over a
    // decorative rail (glow, tick marks), so keyboard and touch work as for any slider.
    el.track = h("div.track", h("div.rail", { "aria-hidden": "true" }, h("span.glow"), el.ticks), el.range);
    el.position = h("section.position", { "aria-label": "Reading position" },
      h("div.position-row", h("div.where", el.pct, h("div.where-text", el.chap, el.source)),
        h("div.pos-controls", h("div.step-btns", prev, next),
          h("div.jump-group", h("label.jump-label", { for: "jump-chapter" }, "Jump to"), el.chapSel, h("span.pct-group", el.pctInput, h("span.subtle", "%"))))),
      el.track, el.labels,
      el.hint,
      el.series, el.notice);
    el.sentinel = h("div.pos-sentinel", { "aria-hidden": "true" });
    const position = el.position;

    el.tabs = h("div.view-tabs", { role: "tablist", "aria-label": "Index views" });
    el.panel = h("div", { role: "tabpanel", id: "panel-view", tabindex: "0", style: { display: "grid", gap: "16px", paddingTop: "16px" } });

    clear(wrap, head, el.sentinel, position, h("div.reader-body", h("section", el.tabs, el.panel)));
    watchCompact();
    paintTicks();
    paintChapters();
    paint();
  }

  /** The catalog book this index belongs to (via its versions), once the catalog has loaded. */
  const catalogBook = () => s.catalog?.books.find((b) => b.versions.some((v) => v.key === s.key)) || null;

  function paintHead() {
    const book = catalogBook();
    const series = book?.series ? s.catalog.series.find((g) => g.id === book.series.id) : null;
    clear(el.crumbs, h("a", { href: "#/library" }, "Library"),
      series ? [" › ", h("a", { href: `#/series/${encodeURIComponent(series.id)}` }, series.name)] : null,
      book ? [" › ", h("a", { href: `#/book/${encodeURIComponent(book.key)}` }, book.title)] : null);
    // Provenance (who built the index, review state, versions) lives on the book page.
    const cov = s.doc.coverage || {};
    const through = !cov.complete && cov.through ? s.map.anchorPos(cov.through) : null;
    clear(el.headFacts, Number.isFinite(through) && through < 0.995
      ? h("p.hint.cover-line", `This index covers the first ${Math.max(1, Math.round(through * 100))}% of the book.`) : null);
  }

  /** Pick another book to read: a searchable list of every book with an index. */
  async function pickBook() {
    if (!s.catalog) { try { s.catalog = await api.catalog(); } catch (e) { return toast(e.message, "error"); } }
    const ready = s.catalog.books.filter((b) => b.versions.length)
      .sort((a, b) => (a.series?.name || a.title).localeCompare(b.series?.name || b.title) || (a.series?.num ?? 0) - (b.series?.num ?? 0));
    const m = modal("Switch book", { width: 560 });
    const search = h("input.input", { type: "search", placeholder: "Search books with an index", "aria-label": "Search books with an index" });
    const list = h("ul.switch-list");
    const here = catalogBook();
    const fill = () => {
      const q = search.value.trim().toLowerCase();
      const found = ready.filter((b) => !q || [b.title, ...b.authors, b.series?.name || ""].join(" ").toLowerCase().includes(q));
      clear(list, found.length ? found.map((b) => {
        const d = b.versions.find((v) => v.isDefault) || b.versions[0];
        return h(`li${here && here.key === b.key ? ".here" : ""}`, h("a", { href: `#/reader?i=${encodeURIComponent(d.key)}`, on: { click: () => m.close() } },
          cover(b, "xs"), h("span.switch-text", h("b", b.title),
            h("span.hint", [b.authors[0], b.series ? `${b.series.name} #${b.series.index ?? "?"}` : null].filter(Boolean).join(" · "))),
          here && here.key === b.key ? h("span.chip", "Reading") : null));
      }) : h("li.hint", "No book with an index matches. Build one from the Library."));
    };
    search.addEventListener("input", fill);
    clear(m.body, search, list, h("p.hint", h("a", { href: "#/library", on: { click: () => m.close() } }, "Browse the whole library"), " to build an index for another book."));
    fill();
    search.focus();
  }

  function setP(p, { defer = false, quiet = false, prev = null } = {}) {
    if (quiet) { s.prevIds = null; s.prevP = null; }   // no earlier position to compare: nothing is "new"
    else s.prevP = prev ?? s.p;
    s.p = Math.min(1, Math.max(0, p));
    paint({ defer });
    saveUrl();
  }

  function stepReveal(dir) {
    const cur = s.map.frontier(s.p);
    const positions = [...new Set(s.map.eff.filter(Number.isFinite))].sort((a, b) => a - b);
    if (dir > 0) {
      const nxt = positions.find((x) => x > s.p + 1e-9);
      if (nxt != null) moveTo(nxt);
    } else {
      markOverride();
      const curPos = cur ? s.map.anchorPos(cur) : 0;
      const prv = [...positions].reverse().find((x) => x < curPos - 1e-9);
      setP(prv ?? 0);
    }
  }

  /** Tick marks on the rail, chapter names under it (Part/Book starts, or every chapter in a short book), and the end of the index. */
  function paintTicks() {
    const ticks = [], items = [];
    let end = null;
    if (!s.doc.coverage.complete) {
      const at = s.map.anchorPos(s.doc.coverage.through);
      if (Number.isFinite(at) && at < 0.995) end = at;
    }
    if (s.map.exact) {
      const chs = s.map.chapters;
      const isPart = (c) => /^(part|book)\b/i.test(c.title);
      for (const c of chs) {
        ticks.push(h(`span.tick${isPart(c) ? ".part" : ""}`, { style: { left: `${s.map.blockStart(c.start) * 100}%` } }));
      }
      for (const c of chs.length <= 8 ? chs : chs.filter(isPart)) {
        const x = s.map.blockStart(c.start);
        if (end == null || x < end - 0.02) items.push({ x, start: c.start, text: tidyTitle(c.title) });
      }
    }
    const labels = [];
    el.labelItems = [];
    items.forEach((it, i) => {
      const limit = Math.min(items[i + 1]?.x ?? 1, end != null ? end - 0.1 : 1);
      const room = (limit - it.x) * 100;
      if (room < 5) return;
      const lab = h("span.hz-label", { style: { left: `${it.x * 100}%`, maxWidth: `calc(${room}% - 8px)` } }, it.text);
      el.labelItems.push({ el: lab, start: it.start });
      labels.push(lab);
    });
    if (end != null) {
      ticks.push(h("span.end-mark", { style: { left: `${end * 100}%` } }));
      labels.push(h(`span.hz-label.end${end < 0.1 ? ".start" : ""}`, { style: { left: `${end * 100}%` } }, "Index ends"));
    }
    clear(el.ticks, ticks);
    clear(el.labels, labels);
  }

  // The horizon shrinks to a slim strip under the top bar once the page scrolls past it. A zero-height sentinel above
  // the panel tells us when; `--slack` keeps the layout height unchanged so nothing below jumps.
  let compactObs;
  function setCompact(on) {
    const p = el.position;
    if (!p || p.classList.contains("compact") === on) return;
    if (on) {
      const full = p.offsetHeight;
      p.classList.add("compact");
      p.style.setProperty("--slack", `${Math.max(0, full - p.offsetHeight)}px`);
    } else {
      p.classList.remove("compact");
      p.style.removeProperty("--slack");
    }
  }
  function watchCompact() {
    compactObs?.disconnect();
    const narrow = window.matchMedia("(max-width: 640px)");
    if (narrow.matches || !("IntersectionObserver" in window)) { setCompact(false); return; }
    const top = (document.querySelector(".topbar")?.offsetHeight ?? 61) - 1;
    el.position.style.setProperty("--hz-top", `${top}px`);
    compactObs = new IntersectionObserver(([e]) => setCompact(!e.isIntersecting && e.boundingClientRect.top < top),
      { rootMargin: `-${top}px 0px 0px 0px`, threshold: 0 });
    compactObs.observe(el.sentinel);
  }
  let resizeTimer;
  const onResize = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (el.sentinel) watchCompact(); }, 150); };
  window.addEventListener("resize", onResize);

  function paintChapters() {
    if (!s.map.exact) { el.chapSel.hidden = true; return; }
    el.chapSel.hidden = false;
    clear(el.chapSel, h("option", { value: "" }, "Chapter…"),
      s.map.chapters.map((c) => h("optgroup", { label: c.title },
        h("option", { value: `start:${c.start}` }, `Start of ${c.title}`),
        h("option", { value: `end:${c.start}` }, `End of ${c.title}`))));
  }

  function paintSeries() {
    if (!s.series) return clear(el.series);
    const order = s.series.orders[s.order];
    const seg = h("div.series-chips", { role: "group", "aria-label": "Book in series" }, order.map((k, i) => {
      const b = s.series.books.find((x) => x.key === k);
      const has = s.seriesDocs.get(k);
      const text = `${i + 1}. ${b?.title || k}`;
      return k === s.book ? h("span.chip", { "aria-current": "true" }, text)
        : h("button.chip", { type: "button", disabled: !has, title: has ? b.title : "No index for this book", on: { click: () => switchBook(k) } }, text);
    }));
    const orderSel = Object.keys(s.series.orders).length > 1 && h("select.select", { "aria-label": "Reading order", style: { width: "auto" },
      on: { change: (e) => { s.order = e.target.value; render(); saveUrl(); } } },
      Object.keys(s.series.orders).map((o) => h("option", { value: o, selected: o === s.order }, `${o} order`)));
    const before = order.slice(0, order.indexOf(s.book));
    const fin = new Set(finishedSet());
    const boxes = before.map((k) => {
      const b = s.series.books.find((x) => x.key === k);
      const box = h("input", { type: "checkbox", checked: fin.has(k) });
      box.addEventListener("change", () => {
        const now = new Set(finishedSet());
        box.checked ? now.add(k) : now.delete(k);
        s.finished = [...now].join(",");
        paint(); saveUrl();
      });
      return h("label.chip", { style: { cursor: "pointer" } }, box, `Finished ${b?.title || k}`);
    });
    clear(el.series, h("div.series-bar", h("span.eyebrow", { title: s.series.title }, "In this series"), seg, orderSel, boxes));
  }

  async function switchBook(k) {
    const target = s.lib.indexes.find((i) => i.series?.id === s.series.id && i.series.book === k
      && s.seriesDocs.get(k) && i.id === s.seriesDocs.get(k).id);
    if (target) { s.finished = null; await load(target.key, { book: k }); saveUrl(); }
  }

  let frame = 0;
  let announceTimer;
  let announceCount = 0;
  function paint({ defer = false } = {}) {
    const anchor = compute();
    const v = s.view;
    el.range.value = String(Math.round(s.p * 1000));
    el.range.style.setProperty("--fill", `${s.p * 100}%`);
    el.position.style.setProperty("--f", String(s.p));
    el.range.setAttribute("aria-valuetext", `${(s.p * 100).toFixed(1)} percent${s.map.chapterAt(s.p) ? ", " + s.map.chapterAt(s.p).title : ""}`);
    el.pctNum.textContent = (s.p * 100).toFixed(s.p < 0.1 ? 1 : 0);
    el.pctInput.value = String(Math.round(s.p * 100));
    el.hint.hidden = !(s.p === 0 && !v.entities.length && !v.events.length && !v.chapters.length);
    el.hint.textContent = el.hint.hidden ? "" : "Drag the slider to where you are in the book. Everything below updates to what you've read, and nothing further.";
    const ch = s.map.chapterAt(s.p);
    el.chap.textContent = ch ? ch.title : anchor ? `after paragraph ${anchor}` : "before the first reveal";
    for (const l of el.labelItems || []) l.el.classList.toggle("here", Boolean(ch) && l.start === ch.start);
    paintSource();

    const notices = [];
    if (!s.map.exact) notices.push(h("div.notice.info", icon("info"),
      h("div", h("b", "Approximate positions. "), "The book file for this index isn't in the library, so percentages are estimated from the index itself. Upload the EPUB in the Studio for exact positions and chapter marks.")));
    if (v.coverage.exhausted) notices.push(h("div.notice.warn", { role: "status" }, icon("alert"),
      h("div", h("b", "You've reached the end of this index. "), "It doesn't cover the rest of the book yet, so nothing beyond this point is shown.")));
    clear(el.notice, notices);
    paintSeries();

    const ids = new Set([...v.entities.map((e) => e.id), ...v.events.map((e) => e.id), ...v.chapters.map((c) => c.id),
      ...v.entities.flatMap((e) => e.facets.map((f) => f.id)), ...v.entities.filter((e) => e.profile).map((e) => e.profile.id)]);
    const forward = s.prevP != null && s.p > s.prevP;
    s.fresh = s.prevIds && forward ? new Set([...ids].filter((x) => !s.prevIds.has(x))) : new Set();
    if (s.fresh.size && forward) {
      announceCount += s.fresh.size;
      clearTimeout(announceTimer);
      announceTimer = setTimeout(() => {
        announce(`${announceCount} new item${announceCount === 1 ? "" : "s"} revealed.`);
        announceCount = 0;
      }, 400);
    }
    s.prevIds = ids;
    const repaint = () => {
      frame = 0;
      paintTabs();
      if (s.drawer) openDrawer(s.drawer, { refresh: true });
    };
    if (defer) { if (!frame) frame = requestAnimationFrame(repaint); }
    else { if (frame) cancelAnimationFrame(frame); repaint(); }
  }

  function paintTabs() {
    const v = s.view;
    wrap.classList.toggle("wide", s.tab === "links");   // the map alone gets the full width
    const tabs = [["chapters", "Chapters", chapterGroups().length], ["cast", "Cast & World", v.entities.length],
      ["timeline", "Timeline", v.events.length], ["links", "Connections", v.relations.length]];
    const keys = tabs.map((x) => x[0]);
    const select = (k, focus) => { s.tab = k; paintTabs(); if (focus) document.getElementById(`tab-${k}`)?.focus(); };
    clear(el.tabs, tabs.map(([k, label, n]) => h("button", { type: "button", role: "tab", id: `tab-${k}`, "aria-controls": "panel-view",
      "aria-selected": String(s.tab === k), tabindex: s.tab === k ? "0" : "-1",
      on: { click: () => select(k, false), keydown: (ev) => {
        const i = keys.indexOf(k);
        const to = { ArrowRight: keys[(i + 1) % keys.length], ArrowLeft: keys[(i + keys.length - 1) % keys.length], Home: keys[0], End: keys[keys.length - 1] }[ev.key];
        if (to) { ev.preventDefault(); select(to, true); }
      } } }, label, h("span.count", String(n)))));
    el.panel.setAttribute("aria-labelledby", `tab-${s.tab}`);
    ({ chapters: paintChapterRecaps, cast: paintCast, timeline: paintTimeline, links: paintLinks })[s.tab]();
    el.panel.prepend(h("h2.sr-only", tabs.find((x) => x[0] === s.tab)[1]));
  }

  function entityFresh(e) { return s.fresh.has(e.id) || e.facets.some((f) => s.fresh.has(f.id)) || (e.profile && s.fresh.has(e.profile.id)); }

  const SORTS = { first: "First appearance", az: "A to Z", known: "Most known" };

  function paintCast() {
    const v = s.view;
    const counts = Object.fromEntries(TYPES.map((t) => [t, v.entities.filter((e) => e.type === t).length]));
    const search = h("input.input", { type: "search", placeholder: "Search names", value: s.q, "aria-label": "Search names and aliases" });
    search.addEventListener("input", () => { s.q = search.value; paintList(); });
    const chips = TYPES.filter((t) => counts[t]).map((t) => h(`button.filter-chip.t-${t}`, { type: "button", "aria-pressed": String(s.types.has(t)),
      on: { click: (e) => { s.types.has(t) ? s.types.delete(t) : s.types.add(t); e.currentTarget.setAttribute("aria-pressed", String(s.types.has(t))); paintList(); } } },
      TYPE_PLURAL[t], h("span.n", String(counts[t]))));
    const sort = h("select.select.sort-select", { "aria-label": "Sort entries" },
      Object.entries(SORTS).map(([k, label]) => h("option", { value: k, selected: s.sort === k }, label)));
    sort.addEventListener("change", () => { s.sort = sort.value; paintList(); });
    const list = h("div.cast-list");
    function paintList() {
      const q = s.q.trim().toLowerCase();
      const shown = v.entities.filter((e) => (!s.types.size || s.types.has(e.type))
        && (!q || [e.name, ...e.aliases].some((n) => n.toLowerCase().includes(q))));
      const order = { first: () => 0, az: (a, b) => a.name.localeCompare(b.name),
        known: (a, b) => b.facets.length - a.facets.length }[s.sort];
      if (!shown.length) {
        return clear(list, h("div.empty", h("div.art", icon(v.entities.length ? "search" : "book-open")),
          h("h3", v.entities.length ? "No matches" : "Nothing revealed yet"),
          h("p", v.entities.length ? "Try another name or clear the filters." : "Move the slider forward to start meeting the cast.")));
      }
      clear(list, TYPES.map((t) => {
        const rows = shown.filter((e) => e.type === t).sort(order);   // stable: ties keep first appearance
        if (!rows.length) return null;
        const fresh = rows.filter(entityFresh).length;
        return h(`section.cast-group.t-${t}`, { "aria-label": TYPE_PLURAL[t] },
          h("h2.cast-group-head", h("span.cast-group-dot", { "aria-hidden": "true" }), TYPE_PLURAL[t], h("span.n", String(rows.length)),
            fresh ? h("span.new-badge", `${fresh} new`) : null),
          h("ul.cast-rows", rows.map((e) => h("li", row(e)))));
      }));
    }
    clear(el.panel, h("div.filters", h("div.search", icon("search"), search), chips, sort), dupesNotice(), list);
    paintList();
  }

  /** After a merge or undo: reload the (rewritten) indexes and stay where the reader is. */
  const reloadAfterMerge = () => load(s.key, { keepPosition: true, book: s.book });

  // A duplicates check may be running in the background; when it ends, obvious duplicates have
  // been merged on the server, so reload to show them as one.
  function watchDupes(key) {
    clearTimeout(s.dupesTimer);
    if (!s.dupes?.checking) return;
    const before = s.dupes.undo?.id;
    s.dupesTimer = setTimeout(async () => {
      if (s.key !== key) return;
      const next = await api.duplicates(key).catch(() => null);
      if (!next) return;
      if (next.undo?.auto && next.undo.id !== before) { s.autoShown = null; return reloadAfterMerge(); }
      s.dupes = next;
      if (next.checking) watchDupes(key); else paintTabs();
    }, 3000);
  }

  function dupesNotice() {
    const auto = s.dupes?.undo?.auto && s.autoShown !== s.dupes.undo.id ? s.dupes.undo : null;
    let merged = null;
    if (auto) {
      const dismiss = () => { clearTimeout(s.autoTimer); s.autoShown = auto.id; paintTabs(); };
      if (s.autoTimerFor !== auto.id) { s.autoTimerFor = auto.id; clearTimeout(s.autoTimer); s.autoTimer = setTimeout(() => { if (!disposed && s.autoShown !== auto.id) dismiss(); }, 20000); }
      const n = auto.keepNames.length;
      merged = h("div.notice.notice-line", { role: "status" },
        h("span.nl-text", `Merged ${n} obvious duplicate${n === 1 ? "" : "s"}.`),
        h("button.linkish", { type: "button", on: { click: async () => {
          try { await api.undoMerge(s.key, auto.id); toast("Undone"); clearTimeout(s.autoTimer); s.autoShown = auto.id; reloadAfterMerge(); }
          catch (e) { toast(e.message, "error"); }
        } } }, "Undo"),
        h("button.linkish", { type: "button", on: { click: dismiss } }, "OK"));
    }
    const pairs = s.dupes?.writable ? openPairs(visiblePairs(s.dupes, s.view)) : [];
    if (!pairs.length || s.dupesDismissed) return merged;
    return [merged, h("div.notice.notice-line", { role: "status" },
      h("span.nl-text", pairs.length === 1 ? "1 entry may be a duplicate." : `${pairs.length} entries may be duplicates.`),
      h("button.linkish", { type: "button", on: { click: () => reviewDuplicates({ key: s.key, view: s.view, dupes: s.dupes,
        onChanged: async (didMerge) => {
          if (didMerge) return reloadAfterMerge();
          s.dupes = await api.duplicates(s.key).catch(() => s.dupes);   // pick up verdicts and "not the same"
          paintTabs();
        } }) } }, "Review"),
      h("button.linkish", { type: "button", on: { click: () => { s.dupesDismissed = true; paintTabs(); } } }, "Not now"))];
  }

  // A fact that opens with a verb ("Is engaged to...", "Marries a...") is a fragment, not a description.
  const OPENING_VERBS = new Set(["is", "was", "has", "had", "will", "can", "does", "wants", "intends", "plans",
    "travels", "goes", "meets", "writes", "marries", "becomes", "seeks", "hopes", "tells", "asks", "says", "must"]);
  function startsWithVerb(text) {
    const [first = "", second = ""] = String(text || "").trim().split(/\s+/);
    const w = first.toLowerCase().replace(/[^a-z]/g, "");
    if (OPENING_VERBS.has(w)) return true;
    return /^[A-Z][a-z]+(s|es|ed)$/.test(first) && /^(a|an|the|to|his|her|their|with|in|of|for|that)$/.test(second.toLowerCase());
  }

  function row(e) {
    const fresh = entityFresh(e);
    const about = e.profile ? describe(e) : (e.facets.find((f) => !startsWithVerb(f.text))?.text || describe(e));
    const rowEl = h(`button.ent-row.t-${e.type}${fresh ? ".fresh.arrive" : ""}`, { type: "button", "aria-label": `${e.name}, ${TYPE_LABEL[e.type]}${fresh ? ", new" : ""}`,
      on: { click: (ev) => openDrawer(e.id, { from: ev.currentTarget }) } },
      h(`span.ent-dot${fresh ? ".fresh" : ""}`, { "aria-hidden": "true", title: fresh ? "New" : null }),
      h("span.ent-name", h("span.nm", e.name),
        e.aliases.length ? h("span.aka", e.aliases.slice(0, 3).join(" · ")) : null),
      h("span.ent-fact", about || h("span.subtle", "Named, but nothing more is known yet.")),
      h("span.ent-meta", h("span.num", String(e.facets.length)), e.facets.length === 1 ? " fact" : " facts",
        e.history.length ? h("span.hist", { title: `${e.history.length} earlier impression${e.history.length === 1 ? "" : "s"}` }, icon("refresh")) : null));
    if (fresh) rowEl.addEventListener("animationend", () => rowEl.classList.remove("arrive"), { once: true });
    return rowEl;
  }

  // ---- chapters ----------------------------------------------------------------------------

  /** Visible chapter recaps, with the parts of one long chapter joined: [{ key, title, book, parts, ids }]. */
  function chapterGroups() {
    const out = [];
    for (const c of s.view.chapters || []) {
      const last = out[out.length - 1];
      if (last && last.title === c.title && last.book === c.book) { last.parts.push(c.text); last.ids.push(c.id); }
      else out.push({ key: c.id, title: c.title || "Untitled section", book: c.book, parts: [c.text], ids: [c.id] });
    }
    return out;
  }

  const MINOR = new Set(["a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"]);
  /** "CHAPTER III. THE LAURISTON GARDENS MYSTERY" → "Chapter III. The Lauriston Gardens Mystery" (roman numerals kept). */
  const tidyTitle = (t) => (t && t === t.toUpperCase() && /[A-Z]{4}/.test(t)
    ? t.toLowerCase().replace(/[\p{L}']+/gu, (w, i, all) => (i && MINOR.has(w) && !/[.:]\s*$/.test(all.slice(0, i)) ? w : w[0].toUpperCase() + w.slice(1)))
      .replace(/\b(Chapter|Part|Book) ([ivxlc]+)\b/gi, (_, a, n) => `${a} ${n.toUpperCase()}`).replace(/\.$/, "")
    : t);

  /** Entities named in a piece of text: whole names and aliases, or a surname no other entity shares. */
  function mentions(text) {
    const norm = (x) => x.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    const low = ` ${norm(text)} `;
    const last = new Map();
    for (const e of s.view.entities) {
      if (e.type !== "person") continue;
      for (const n of [e.name, ...e.aliases]) {
        const w = norm(n).split(" ");
        if (w.length > 1) { const k = w[w.length - 1]; last.set(k, last.has(k) && last.get(k) !== e.id ? null : e.id); }
      }
    }
    return s.view.entities.filter((e) => [e.name, ...e.aliases].some((n) => {
      const w = norm(n);
      if (w.length > 2 && low.includes(` ${w} `)) return true;
      const k = w.split(" ").pop();
      return w.includes(" ") && k.length > 2 && last.get(k) === e.id && low.includes(` ${k} `);
    }));
  }

  function paintChapterRecaps() {
    const v = s.view;
    const groups = chapterGroups();
    const here = s.map.chapterAt(s.p);
    const hasAny = (s.doc.recaps || []).some((r) => r.scope === "chapter") || [...s.seriesDocs.values()].some((d) => (d?.recaps || []).some((r) => r.scope === "chapter"));
    const story = h("section.story-now", { "aria-label": "Story so far" },
      h("span.eyebrow", "Story so far"),
      h("p.recap-text", v.recap ? v.recap.text : "Nothing to recap yet. Move forward in the book to see the story so far."),
      v.recap && bookLabel(v.recap.book));
    if (!groups.length) {
      return clear(el.panel, story, h("div.empty", h("div.art", icon("book-open")),
        h("h3", hasAny ? "No chapters finished yet" : "No chapter recaps in this index"),
        h("p", hasAny ? "Each chapter's summary appears once you've read past it." : "This index was built before chapter recaps existed. Build a new version to get a summary of every chapter.")));
    }
    const toggle = h("button.btn.ghost", { type: "button", "aria-pressed": String(s.chaptersNewest),
      on: { click: () => { s.chaptersNewest = !s.chaptersNewest; paintChapterRecaps(); } } }, icon("refresh"), s.chaptersNewest ? "Newest first" : "In order");
    const numbered = groups.map((g, i) => ({ ...g, n: i + 1, last: i === groups.length - 1 }));
    const ordered = s.chaptersNewest ? [...numbered].reverse() : numbered;
    clear(el.panel, story,
      h("div.toolbar.chapters-bar", h("span.hint", `${groups.length} chapter${groups.length === 1 ? "" : "s"} so far`), toggle),
      h("ol.chapter-list", ordered.map((g) => {
        // Still inside this chapter: its summary covers only what has been read.
        const ongoing = g.last && here && here.title === g.title && (!s.series || g.book === s.book);
        const fresh = g.ids.some((id) => s.fresh.has(id));
        const who = mentions(g.parts.join(" "));
        const det = h(`details.chapter${fresh ? ".fresh" : ""}`, { "data-chapter": g.key, open: g.last || ordered.length <= 3 ? "" : null },
          h("summary", h("span.ch-num.num", String(g.n)), h("span.ch-title", tidyTitle(g.title)),
            ongoing ? h("span.chip.ch-ongoing", "So far") : null, fresh ? h("span.new-badge", "New") : null,
            bookLabel(g.book)),
          h("div.ch-body", g.parts.map((t) => h("p", t)),
            who.length ? h("div.ch-who", h("span.eyebrow", "In this chapter"), h("div.ents", who.slice(0, 14).map((e) => entityChip(e.id)))) : null));
        return h("li", det);
      })));
  }

  const nameOf = (id) => s.view.entities.find((e) => e.id === id);
  const entityChip = (id) => {
    const e = nameOf(id);
    if (!e) return null;
    return h(`button.link-chip.t-${e.type}`, { type: "button", on: { click: (ev) => openDrawer(id, { from: ev.currentTarget }) } }, e.name);
  };
  const bookLabel = (b) => (s.series && b && b !== s.book ? h("span.book-label", `from ${s.series.books.find((x) => x.key === b)?.title || b}`) : null);

  function paintTimeline() {
    const evs = s.newestFirst ? [...s.view.events].reverse() : s.view.events;
    const toggle = h("button.btn.ghost", { type: "button", "aria-pressed": String(s.newestFirst),
      on: { click: () => { s.newestFirst = !s.newestFirst; paintTimeline(); } } }, icon("refresh"), s.newestFirst ? "Newest first" : "Oldest first");
    clear(el.panel, h("div.toolbar", toggle),
      evs.length ? h("ol.timeline", evs.map((ev) => h(`li${s.fresh.has(ev.id) ? ".fresh" : ""}`,
        h("p.ev", ev.summary), h("div.ents", ev.entities.map(entityChip), bookLabel(ev.book)))))
        : h("div.empty", h("div.art", icon("timeline")), h("h3", "No events yet"), h("p", "Key moments appear here as you read.")));
  }

  function relationRow(r) {
    return h("li", entityChip(r.src), h("span.lbl", r.label), entityChip(r.dst));
  }

  /** Short name for a chapter: "Ch. III" when the title starts with a chapter number, else the title. */
  function shortChapter(c) {
    const t = tidyTitle(c.title);
    const m = /^(?:chapter|ch\.?)\s+([\w]+)/i.exec(t);
    return m ? `Ch. ${m[1]}` : t.length > 26 ? `${t.slice(0, 25)}…` : t;
  }

  /** Where an entity or relation was first revealed, and whether that's in the chapter being read. */
  function revealed(id) {
    if (!s.reveals) {
      s.reveals = new Map();
      const docs = s.series ? [...s.seriesDocs.entries()] : [[null, s.doc]];
      for (const [book, d] of docs) {
        if (!d) continue;
        for (const e of d.entities || []) if (!s.reveals.has(e.id)) s.reveals.set(e.id, { book, from: e.from });
        for (const r of d.relations || []) s.reveals.set(r.id, { book, from: r.from });
      }
    }
    const r = s.reveals.get(id);
    if (!r) return null;
    if (s.series && r.book !== s.book) return { where: `in ${s.series.books.find((x) => x.key === r.book)?.title || r.book}`, isNew: false };
    const at = s.map.anchorPos(r.from);
    const ch = Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
    const here = s.map.chapterAt(s.p);
    return { where: ch ? `in ${shortChapter(ch)}` : null, isNew: Boolean(here && ch && ch.start === here.start) };
  }

  function paintLinks() {
    const here = s.map.chapterAt(s.p);
    renderConnections(el.panel, {
      view: s.view, types: s.types, gs: s.graph,
      chapterMentions: chapterGroups().map((g) => mentions(g.parts.join(" ")).map((e) => e.id)),
      revealed, hereLabel: here ? shortChapter(here) : "this chapter",
      openEntity: (id, from) => openDrawer(id, { from }),
      repaint: () => paintTabs(),
    });
  }

  // ---- drawer ----------------------------------------------------------------------------

  let lastFocus = null;
  function closeDrawer() {
    const d = document.querySelector("dialog.drawer");
    s.drawer = null;
    if (d) { d.removeEventListener("close", onDialogClose); if (d.open) d.close(); d.remove(); }
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }
  // Escape closes a native dialog without going through closeDrawer.
  function onDialogClose() { closeDrawer(); }

  function openDrawer(id, { from = null, refresh = false } = {}) {
    const e = nameOf(id);
    if (!e) { if (refresh) closeDrawer(); return; }
    if (!refresh) lastFocus = from || document.activeElement;
    s.drawer = id;
    const v = s.view;
    const rels = v.relations.filter((r) => r.src === id || r.dst === id);
    const evs = v.events.filter((ev) => ev.entities.includes(id));
    const closeBtn = h("button.icon-btn", { type: "button", "aria-label": "Close", on: { click: closeDrawer } }, icon("x"));
    const facts = (list) => h("ul.fact-list", list.map((f) => h("li", f.text,
      f.basis === "inferred" ? h("span.inferred", { title: "A careful reader would conclude this; the text doesn't say it outright." }, "inferred") : null,
      bookLabel(f.book) && h("span", " ", bookLabel(f.book)))));
    const body = h("div.body",
      e.profile ? h("div.profile", h("span.eyebrow", e.type === "person" ? "Who they are" : e.type === "group" ? "Who they are" : "What it is"),
        h("p", e.profile.text, e.profile.basis === "inferred" ? h("span.inferred", "inferred") : null),
        bookLabel(e.profile.book)) : null,
      e.aliases.length ? h("div.d-sec", h("span.eyebrow", "Also known as"), h("p.aka-line", e.aliases.join(" · "))) : null,
      h("div.d-sec", h("span.eyebrow", "What you know so far"),
        e.facets.length ? facts(e.facets) : h("p.muted", "Nothing beyond the name yet.")),
      e.history.length ? h("details.history", h("summary", `Earlier impressions (${e.history.length})`), facts(e.history)) : null,
      rels.length ? h("div.d-sec", h("span.eyebrow", "Connections"),
        h("ul.relations", rels.map(relationRow))) : null,
      evs.length ? h("div.d-sec", h("span.eyebrow", "Appears in"),
        h("ol.timeline", evs.map((ev) => h("li", h("p.ev", ev.summary), bookLabel(ev.book))))) : null,
      s.dupes?.writable ? h("div.drawer-foot", h("button.btn.ghost", { type: "button", on: { click: () => {
        closeDrawer();
        mergeWith({ key: s.key, view: s.view, id, onChanged: reloadAfterMerge });
      } } }, icon("layers"), "Same as another entry…")) : null);
    const content = [h("header", h("div.d-title", typeTag(e.type), h("h2#drawer-title", e.name)), closeBtn), body];
    let dlg = document.querySelector("dialog.drawer");
    if (refresh && dlg) {
      const keep = dlg.contains(document.activeElement) && document.activeElement.id ? document.activeElement.id : null;
      dlg.className = `drawer t-${e.type}`;
      clear(dlg, content);
      if (keep) document.getElementById(keep)?.focus();
      return;
    }
    dlg?.remove();
    dlg = h(`dialog.drawer.t-${e.type}`, { "aria-labelledby": "drawer-title",
      on: { close: onDialogClose, click: (ev) => { if (ev.target === dlg) closeDrawer(); } } }, content);
    document.body.append(dlg);
    dlg.showModal();
    if (!refresh) closeBtn.focus();
  }

  // ---- command palette ----------------------------------------------------------------------
  const clip = (x, n = 60) => (x.length > n ? `${x.slice(0, n - 1).trimEnd()}…` : x);
  function openChapter(key) {
    s.tab = "chapters"; paintTabs();
    const det = [...el.panel.querySelectorAll("details.chapter")].find((d) => d.dataset.chapter === key);
    if (!det) return;
    det.open = true;
    det.scrollIntoView({ block: "start" });
    det.querySelector("summary")?.focus({ preventScroll: true });
  }
  function paletteItems() {
    if (disposed || !s.view || !s.map) return [];
    const out = [];
    for (const e of s.view.entities) {
      out.push({ id: `reader:ent:${e.id}`, label: e.name, hint: clip(describe(e)) || undefined, group: "Cast and world", type: e.type,
        keywords: e.aliases.join(" "), run: () => openDrawer(e.id) });
    }
    for (const g of chapterGroups()) {
      out.push({ id: `reader:ch:${g.key}`, label: tidyTitle(g.title), group: "Chapters", run: () => openChapter(g.key) });
    }
    out.push(
      { id: "reader:jump", label: "Jump to chapter…", group: "Reading", keywords: "go chapter position",
        run: () => setTimeout(() => el.chapSel?.focus(), 60) },
      { id: "reader:next", label: "Next reveal", group: "Reading", keywords: "forward step", run: () => stepReveal(1) },
      { id: "reader:prev", label: "Previous reveal", group: "Reading", keywords: "back step", run: () => stepReveal(-1) });
    return out;
  }
  let unregister = null;
  import("./palette.js").then((m) => { if (!disposed) unregister = m.registerProvider("reader", paletteItems); }).catch((e) => { console.warn("palette provider", e); });

  return () => {
    disposed = true; unregister?.(); clearTimeout(s.autoTimer); clearTimeout(s.dupesTimer); closeDrawer(); clearTimeout(urlTimer); clearInterval(pollTimer);
    compactObs?.disconnect(); clearTimeout(resizeTimer); window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVisible); window.removeEventListener("focus", onVisible);
  };
}
