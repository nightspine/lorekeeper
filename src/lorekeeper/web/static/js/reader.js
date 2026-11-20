import { api } from "./api.js";
import { parseHash, setParams } from "./app.js";
import { readingList, rememberReading } from "./reading.js";
import { announce, clear, describe, tidyTitle, foldList, h, icon, loadError, loading, relTime, TYPE_LABEL, TYPE_PLURAL, toast, typeTag } from "./dom.js";
import { view as gate } from "./gating.js";
import { bookNo, confirmDialog, cover, modal } from "./build.js";
import { mergeWith, openPairs, reviewDuplicates, visiblePairs } from "./dupes.js";
import { renderConnections } from "./graph.js";
import { families } from "./kinship.js";
import { createEditor } from "./editor.js";
import { mentionsSection, forgetMentions } from "./mentions.js";
import { fetchPreview, previewError, previewStrip } from "./community.js";
import { createAsk } from "./ask.js";

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
  /** The block being read at p: the first whose end lies past p (null without the book file). */
  seqAt(p) {
    if (!this.cum) return null;
    let lo = 0, hi = this.cum.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.cum[mid] / this.total <= p + 1e-12) lo = mid + 1; else hi = mid; }
    return Math.min(lo, this.cum.length - 1);
  }
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
  const { route, parts, params } = parseHash();
  // A community index being tried before it is added: #/preview/<digest>?catalog=<book>&listing=<id>.
  // Its position is held at the end of chapter 3 until the person deliberately goes past it.
  const preview = route === "preview" ? { hex: parts[0] || "", catalog: params.get("catalog") || "", listing: params.get("listing") || "", info: null, horizon: 0, what: "the end of chapter 3" } : null;
  const s = {
    lib: null, key: preview ? `preview:${preview.hex}` : params.get("i"), preview, doc: null, map: null, series: null, seriesDocs: new Map(),
    book: params.get("book"), order: params.get("order"), finished: params.get("fin"),
    // No position in the address (Read in the Library, the book page, Switch book, the Runs monitor): the position
    // the person last set for this index, from the rail's Reading list. A Book Orbit sync still wins after load.
    p: params.has("p") ? Math.min(1, Math.max(0, +params.get("p") / 1000)) : (readingList().find((e) => e.key === params.get("i"))?.p ?? 0),
    tab: !preview && params.get("tab") === "ask" ? "ask" : "chapters", types: new Set(), q: "", sort: "first", newestFirst: true, chaptersNewest: true, prevIds: null, prevP: null, view: null, drawer: null, bkAll: new Set(), bkOpen: new Set(),
    sync: { on: false, override: false }, graph: { sel: null, mode: "map", hops: 1, together: false, things: false, hideMinor: false, history: false, list: false, q: "" }, reveals: null,
    edit: params.get("edit") === "1", hash: null,
    earlier: (() => { try { return localStorage.getItem("lk-earlier") === "1"; } catch { return false; } })(),
    // Timeline filters, remembered per browser: only turning points; show small moments too.
    tlTurns: (() => { try { return localStorage.getItem("lk-tl-turns") === "1"; } catch { return false; } })(),
    tlMinor: (() => { try { return localStorage.getItem("lk-tl-minor") === "1"; } catch { return false; } })(),
    // Chapters tab in a series: show the earlier books' chapters too (as their own blocks), or this book only.
    chSeries: (() => { try { return localStorage.getItem("lk-ch-series") !== "0"; } catch { return true; } })(),
  };
  const wrap = h("div.reader");
  root.append(wrap);
  let disposed = false;

  // Ask: questions answered from the index's notes at the position (ask.js). The thread lives in that closure.
  const ask = createAsk({
    key: () => s.key,
    position: askPosition,
    view: () => s.view,
    examples: askExamples, canOpen: canOpenCite, open: openCite,
    onStatus: () => { if (!disposed && el.tabs) paintTabs(); },
  });
  if (!preview) ask.refreshStatus();

  // The first load: a spinner after a moment, and a page that says what failed with a way to try again.
  const openingText = preview ? "Downloading the index and checking it against your copy…" : "Opening the book…";
  const retryStart = () => { clear(wrap, loading(openingText)); start(); };
  clear(wrap, loading(openingText));
  start();
  async function start() {
    if (preview) {
      try { preview.info = { catalog: preview.catalog, ...await fetchPreview(preview.catalog, preview.listing, preview.hex) }; }
      catch (e) { if (!disposed) clear(wrap, previewError(e, preview.catalog)); return; }
      s.lib = { indexes: [], series: [] };
      api.catalogShared().then((c) => { s.catalog = c; if (el.headLine) paintHead(); if (s.view && !s.edit && (s.tab === "chapters" || s.tab === "timeline")) paintTabs(); }).catch(() => {});
      await load(s.key, { keepPosition: true });
      return;
    }
    try { s.lib = await api.library(); } catch (e) { if (!disposed) clear(wrap, loadError("open the book", e, retryStart)); return; }
    if (!s.lib.indexes.some((i) => i.key === s.key)) {
      clear(wrap, h("div.card.empty", h("div.art", icon("book-open")), h("h3", "This index isn't in the library any more"),
        h("p", "It may have been deleted or rebuilt. Pick a book to read from the Library."),
        h("a.btn.primary", { href: "#/library?show=ready" }, icon("layers"), "Books ready to read")));
      return;
    }
    api.catalogShared().then((c) => { s.catalog = c; if (el.headLine) paintHead(); if (s.view && !s.edit && (s.tab === "chapters" || s.tab === "timeline")) paintTabs(); }).catch(() => {});
    await load(s.key, { keepPosition: true });
  }

  /** Open an index. Resolves true once it is on screen; on failure nothing about the current book changes. */
  let loadSeq = 0;
  async function load(key, { keepPosition = false, book = null } = {}) {
    dropPending();
    // Every continuation after an await checks that this page is still mounted and that no later load started.
    const mine = ++loadSeq;
    const stale = () => disposed || mine !== loadSeq;
    const meta = s.lib.indexes.find((i) => i.key === key);
    let doc;
    try { doc = preview ? await api.communityPreview(preview.hex) : await api.index(key); } catch (e) {
      if (stale()) return false;
      if (!s.doc) { clear(wrap, loadError("open the index", e, retryStart)); return false; }
      toast(`Could not open index: ${e.message}`, "error");   // switching books: the current one stays open, key and all
      return false;
    }
    if (stale()) return false;
    s.key = key;
    s.doc = doc;
    forgetMentions();   // an edit or merge reloads the index: counts made for the old one may name what is now hidden
    s.series = null; s.seriesDocs = new Map();
    const sid = preview ? null : s.doc.series?.id;
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
          if (stale()) return false;
        }
        if (!s.order || !man.orders[s.order]) s.order = man.defaultOrder;
      }
    }
    s.book = book || s.doc.series?.book || null;
    // seq hints are only valid for the edition the index was built from.
    const hashes = (s.doc.editions || []).filter((e) => e.builtFrom).map((e) => e.textHash);
    let edition = null;
    s.hash = null;
    for (const hsh of hashes) { edition = await api.edition(hsh); if (stale()) return false; if (edition) { s.hash = hsh; break; } }
    s.map = new PositionMap(s.doc, edition);
    // Another book of the series: the position last set for it, else its start.
    if (!keepPosition) s.p = readingList().find((e) => e.key === key)?.p ?? 0;
    s.prevIds = null; s.prevP = null; s.fresh = new Set();
    s.reveals = null; s.docOrds = null; s.profileIds = null;
    s.sync = { on: false, override: false, ahead: false, wantAhead: null };
    if (preview) {
      // The horizon the preview starts at: the end of the third story chapter. Past it needs a deliberate "Show me anyway".
      const story = (s.map.chapters || []).filter((c) => c.story !== false);
      preview.horizon = Math.min(1, story.length > 3 ? s.map.blockStart(story[3].start) : 0.2);
      // What the line is, in words: chapter 3's end, the first fifth when the book has too few chapters, or the end of a partial index.
      preview.what = story.length > 3 ? "the end of chapter 3" : "the first fifth of the book";
      const cov = !s.doc.coverage?.complete ? s.map.anchorPos(s.doc.coverage?.through) : null;
      if (Number.isFinite(cov) && cov < preview.horizon) { preview.horizon = cov; preview.what = "where this index ends"; }
      s.p = Math.min(preview.horizon, params.has("p") ? s.p : preview.horizon);
      s.sync = { on: true, preview: true, override: false, ahead: false, wantAhead: null, p: preview.horizon, percentage: null };
    }
    s.dupes = preview ? null : await api.duplicates(key).catch(() => null);
    if (stale()) return false;
    watchDupes(key);
    s.graph.pos = null; s.graph.sel = null; s.graph.userView = false; s.graph.arch = null;
    ask.check();   // another index: nothing asked about the last one stays
    render();
    saveUrl();   // also puts the book at the top of the rail's Reading list (never a preview)
    if (!preview) await syncPosition(key);
    return !stale();
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
    if (preview) return;
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
    const apply = () => {
      // A sync waits only for the book and the moment it was made for: gone after unmount, a book switch or a move by hand.
      if (disposed || s.key !== key || s.sync.override) return;
      setP(p, first ? (prev != null ? { prev } : { quiet: true }) : {});
      if (!first) announce("Position updated from Book Orbit");
      writeLast(p);
    };
    // An edit form with typed text keeps the entry as it is: the synced position waits until the form is
    // saved, discarded or closed (applyPending), so the entry never shows what the position no longer reveals.
    if (drawerDirty()) { pendingMove = { key, run: apply }; return; }
    apply();
  }

  // ---- moves while an edit form holds typed text ---------------------------------------------
  // A deliberate move asks "Discard what you wrote?" first and stays put on "Keep editing"; an automatic one
  // (the Book Orbit sync) waits in pendingMove until the form is resolved.
  // Every deliberate change of what the view reveals (the position, the finished earlier books, the reading order,
  // the series book) goes through okToMove first; see the handoff for the list of call sites.
  let pendingMove = null, asking = null;
  const dropPending = () => { pendingMove = null; };
  /** Resolves true when the change may go ahead (nothing typed, or the person discards it). A deliberate change
   *  also drops a waiting sync: the person has just said where they are. */
  function okToMove(message = "Moving your position closes the form you were editing.") {
    if (!drawerDirty()) { dropPending(); return Promise.resolve(true); }
    if (asking) return asking;
    asking = confirmDialog({ title: "Discard what you wrote?", message, confirmLabel: "Discard", cancelLabel: "Keep editing" }).then((ok) => {
      asking = null;
      if (ok) { dropPending(); if (s.drawer) openDrawer(s.drawer, { refresh: true, discard: true }); }   // drops the forms; the change then refreshes or closes the entry
      return ok;
    });
    return asking;
  }
  /** After a click or key in the entry: once no form holds typed text, a waiting sync is applied. */
  function applyPending() {
    if (!pendingMove || disposed || drawerDirty()) return;
    const { key, run } = pendingMove;
    pendingMove = null;
    if (key === s.key) run();
  }
  const checkPending = () => setTimeout(applyPending, 0);
  /** Put the ruler back where the position is (a move that was refused). */
  function rulerBack() {
    el.range.value = String(Math.round(s.p * 1000));
    el.pctInput.value = String(Math.round(s.p * 100));
    el.chapSel.value = "";
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
    dropPending();
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
    if (!(await okToMove())) { rulerBack(); return; }
    const line = aheadLine();
    if (line != null && p > line + 1e-9 && preview) {
      const ok = await confirmDialog({
        title: "Go past the preview line?",
        message: `The preview stops at ${preview.what} so you can try the index without spoilers. `
          + `Moving to ${Math.round(p * 100)}% shows characters, details and events from pages you haven't read yet.`,
        confirmLabel: "Show me anyway", cancelLabel: `Stay at ${Math.round(line * 100)}%`, danger: true });
      if (!ok) { s.sync.override = false; setP(line); paintSource(); return; }
      s.sync.ahead = true;
    } else if (line != null && p > line + 1e-9) {
      const t = s.sync.updatedAt ? Date.parse(s.sync.updatedAt) / 1000 : NaN;
      const ok = await confirmDialog({
        title: "Go past where you're reading?",
        message: `Book Orbit last saw you at ${Math.round(line * 100)}%${Number.isFinite(t) ? `, ${relTime(t)}` : ""}. `
          + `Moving to ${Math.round(p * 100)}% shows characters, details and events from pages you haven't read yet.`,
        confirmLabel: "Show me anyway", cancelLabel: `Stay at ${Math.round(line * 100)}%`, danger: true });
      if (!ok) { s.sync.override = false; setP(line); paintSource(); return; }
      s.sync.ahead = true;
    }
    markOverride();
    setP(p);
  }
  async function backToSynced() {
    if (!s.sync.on || !(await okToMove())) return;
    s.sync.override = false; s.sync.ahead = false; s.sync.wantAhead = null;
    setP(s.sync.p);
    if (preview) return;
    writeLast(s.sync.p);
    syncPosition(s.key, { poll: true });
  }

  function paintSource() {
    if (!el.source) return;
    const y = s.sync;
    if (preview) {
      const past = s.p > y.p + 1e-9;
      return clear(el.source, past ? "Past the preview line" : `Preview held to ${preview.what}`,
        past ? h("button.linkish", { type: "button", on: { click: backToSynced } }, `Back to the preview line (${Math.round(y.p * 100)}%)`) : null);
    }
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

  /** The index (and in a series every book's) last-updated stamps: an edit changes one, a merge does not (mentions.js drops its memo on every reload for that). */
  function revision() {
    const docs = s.series ? [...s.seriesDocs.values()] : [s.doc];
    return docs.map((x) => x?.provenance?.updated || "").join("|");
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
    if (disposed) return;   // a page that is gone never writes the address or the Reading list
    urlTimer = setTimeout(() => {
      if (disposed) return;
      if (preview) {   // a preview keeps its own address and never joins the Reading list
        setParams({ catalog: preview.catalog, listing: preview.listing, p: String(Math.round(s.p * 1000)) });
        return;
      }
      const q = { i: s.key, p: String(Math.round(s.p * 1000)) };
      if (s.series) { q.book = s.book; q.order = s.order; if (s.finished != null) q.fin = s.finished; }
      if (s.edit) q.edit = "1";
      if (s.tab === "ask") q.tab = "ask";
      setParams(q);
      // The rail's "Reading" list: the title and the position the person set, nothing from the index.
      const back = { ...q };
      delete back.edit;   // reopening from the rail reads; it does not reopen edit mode
      delete back.tab;
      rememberReading({ key: s.key, title: s.doc.work.title, p: s.p, chapter: s.map.chapterAt(s.p) ? tidyTitle(s.map.chapterAt(s.p).title) : null,
        hash: `#/reader?${new URLSearchParams(back)}` });
    }, 250);
  }

  // ---- layout ----------------------------------------------------------------------------

  const el = {};
  function render() {
    // One compact head row: the title, one quiet line (author, series), Edit and Switch book.
    el.headLine = h("p.rd-by");
    el.headFacts = h("span.rd-cover");
    const head = h("header.page-head.rd-head",
      h("div.rd-title", h("h1", s.doc.work.title), el.headLine),
      el.headActions = h("div.reader-switch", editButton(), preview ? null : h("button.btn.quiet.switch-book", { type: "button", "aria-label": "Switch book", title: "Switch book", on: { click: pickBook } }, icon("layers"), h("span.btn-lbl", "Switch book"))));
    paintHead();
    wrap.classList.toggle("editing", s.edit);

    el.pctNum = document.createTextNode("");
    el.pct = h("span.pct.num", el.pctNum, h("span.pct-unit", "%"));
    el.chap = h("span.chap");
    el.source = h("div.pos-source");
    el.range = h("input.range", { type: "range", min: "0", max: "1000", step: "1", value: String(Math.round(s.p * 1000)),
      "aria-label": "Reading position in the book" });
    el.ticks = h("div.ticks");
    el.pctInput = h("input.input.pct-input", { type: "number", min: "0", max: "100", step: "1", "aria-label": "Reading position, percent" });
    el.chapSel = h("select.select.chsel#jump-chapter", { "aria-label": "Jump to chapter" });
    el.hint = h("span.pos-hint");
    el.setting = h("span.pos-setting");
    el.notice = h("div.pos-notices");
    el.series = h("div.pos-series");

    const prev = h("button.icon-btn.rbtn", { type: "button", "aria-label": "Previous reveal", title: "Previous reveal" }, icon("skip-back"));
    const next = h("button.icon-btn.rbtn", { type: "button", "aria-label": "Next reveal", title: "Next reveal" }, icon("skip-fwd"));
    prev.addEventListener("click", () => stepReveal(-1));
    next.addEventListener("click", () => stepReveal(1));
    let dragWant = null;
    // The range input sits over the ticks, so their tooltips can't show: hovering the track names the chapter under
    // the pointer in the ruler's chapter text instead (chapter titles come from the book file, not the index).
    el.range.addEventListener("pointermove", (ev) => {
      if (!s.map?.exact || ev.buttons) return;
      const r = el.range.getBoundingClientRect();
      const at = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
      const c = s.map.chapterAt(at);
      el.chap.textContent = c ? tidyTitle(c.title) : "Before the first chapter";
      el.chap.classList.add("hovering");
    });
    el.range.addEventListener("pointerleave", () => {
      if (!el.chap.classList.contains("hovering")) return;
      el.chap.classList.remove("hovering");
      const ch = s.map.chapterAt(s.p);
      el.chap.textContent = ch ? tidyTitle(ch.title) : el.chap.title;
    });
    // Dragging past where Book Orbit says you are stops at that line; letting go there asks first (moveTo).
    el.range.addEventListener("input", () => {
      const want = +el.range.value / 1000;
      // Typed text in an edit form: the ruler stays put until the person says to discard it.
      if (drawerDirty()) {
        rulerBack();
        const first = dragWant == null;
        dragWant = want;   // only the latest place the ruler was dragged to counts
        if (first) okToMove().then((ok) => { const to = dragWant; dragWant = null; if (ok) moveTo(to); });
        return;
      }
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

    // The ruler, pinned under the head on every tab: what you've read is lit, what is ahead stays dark. The real range
    // input sits transparent over a decorative rail (tick marks), so keyboard and touch work as for any slider.
    el.track = h("div.track", h("div.rail", { "aria-hidden": "true" }, el.ticks), el.range);
    el.jump = h("div.jump-group", el.chapSel, h("span.pct-group", el.pctInput, h("span.subtle", "%")));
    el.position = h("section.ruler", { "aria-label": "Reading position" },
      h("div.step-btns", prev, next), el.pct, el.chap, el.track, el.jump);
    // Secondary lines sit under the ruler only when they have something to say.
    el.sub = h("div.ruler-sub", el.source, el.hint, el.setting, el.headFacts, el.notice);
    placeJump();
    el.tabs = h("div.view-tabs", { role: "tablist", "aria-label": "Index views" });
    el.panel = h("div.rd-panel", { role: "tabpanel", id: "panel-view", tabindex: "0" });
    // The entry opens beside the list (1101px and up); below that it is the modal drawer.
    el.pane = h("aside.entry-pane.drawer", { role: "complementary", "aria-labelledby": "drawer-title", hidden: true,
      on: { input: markDirty, change: markDirty, click: checkPending, keyup: checkPending,
        keydown: (ev) => { if (ev.key === "Escape" && !ev.defaultPrevented) { ev.preventDefault(); requestCloseDrawer(); } } } });
    // The Connections tab's side panel sits in the same right-hand place as the entry.
    el.mapSide = h("div.map-side");
    el.split = h("div.rd-split", h("section.rd-list", el.tabs, el.panel), el.pane, el.mapSide);
    el.panel.addEventListener("keydown", onListKey);

    clear(wrap, head, preview ? previewStrip(preview.info) : null, el.position, el.sub, el.series, el.split);
    paintTicks();
    paintChapters();
    paint();
  }

  /** The catalog book this index belongs to (via its versions), once the catalog has loaded. */
  const catalogBook = () => (preview ? s.catalog?.books.find((b) => b.key === preview.catalog)
    : s.catalog?.books.find((b) => b.versions.some((v) => v.key === s.key))) || null;

  // ---- edit mode ---------------------------------------------------------------------------
  // Only an index built here can be changed, and only with its book file on disk (reveal points are
  // chosen by paragraph). Turning it on adds pen and trash controls to every row and "Add" buttons.
  const canEdit = () => Boolean(s.dupes?.writable && s.map?.exact && s.hash);
  const editor = createEditor({
    key: () => s.key, hash: () => s.hash, chapters: () => s.map.chapters, seqAt: () => s.map.seqAt(s.p),
    docs: () => currentIndexes(), view: () => s.view, book: () => s.book,
    reload: async (anchor) => {
      await reloadAfterMerge();
      const at = anchor ? s.map.anchorPos(anchor) : null;   // something new just ahead: read up to it so it shows
      if (Number.isFinite(at) && at > s.p) await moveTo(at);
      if (s.drawer) openDrawer(s.drawer, { refresh: true });   // the drawer shows the index as it is now
      checkPending();
    },
  });
  async function setEdit(on) {
    if (on && !canEdit()) return;
    // Done with typed text in a form: ask, as closing the entry does.
    if (!on && drawerDirty() && !(await confirmDialog({ title: "Discard what you wrote?", message: "Leaving edit mode closes the form you were editing.",
      confirmLabel: "Discard", cancelLabel: "Keep editing" }))) return;
    s.edit = on;
    wrap.classList.toggle("editing", on);
    const btn = wrap.querySelector(".edit-toggle");
    if (btn) { clear(btn, icon(on ? "check" : "pen"), h("span.btn-lbl", on ? "Done" : "Edit")); btn.setAttribute("aria-pressed", String(on)); }
    paintTabs();
    if (s.drawer) openDrawer(s.drawer, { refresh: true });
    saveUrl();
  }
  function editButton() {
    if (!canEdit()) { s.edit = false; return null; }
    return h("button.btn.edit-toggle", { type: "button", "aria-pressed": String(s.edit), on: { click: () => setEdit(!s.edit) } },
      icon(s.edit ? "check" : "pen"), h("span.btn-lbl", s.edit ? "Done" : "Edit"));
  }

  function paintHead() {
    // One quiet line: the author, and the series with this book's number; the book page link sits on the series name.
    const book = catalogBook();
    const series = book?.series ? s.catalog.series.find((g) => g.id === book.series.id) : null;
    const authors = s.doc.work.authors.join(", ") || "Unknown author";
    clear(el.headLine, book ? h("a", { href: `#/book/${encodeURIComponent(book.key)}`, title: "Versions, runs and book settings" }, authors) : authors,
      series ? [", ", h("a", { href: `#/series/${encodeURIComponent(series.id)}` }, series.name), bookNo(book.series) ? `, ${bookNo(book.series)}` : ""] : null);
    // Provenance (who built the index, review state, versions) lives on the book page.
    const cov = s.doc.coverage || {};
    const through = !cov.complete && cov.through ? s.map.anchorPos(cov.through) : null;
    clear(el.headFacts, Number.isFinite(through) && through < 0.995
      ? `This index covers the first ${Math.max(1, Math.round(through * 100))}% of the book.` : null);
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
            h("span.hint", [b.authors[0], b.series ? b.series.name : null, b.series ? bookNo(b.series) : null].filter(Boolean).join(", "))),
          here && here.key === b.key ? h("span.chip", "Reading") : null));
      }) : h("li.hint", "No book with an index matches. Build one from the Library."));
    };
    search.addEventListener("input", fill);
    clear(m.body, search, list, h("p.hint", h("a", { href: "#/library", on: { click: () => m.close() } }, "Browse the whole library"), " to build an index for another book."));
    fill();
    search.focus();
  }

  function setP(p, { defer = false, quiet = false, prev = null } = {}) {
    if (disposed) return;
    if (quiet) { s.prevIds = null; s.prevP = null; }   // no earlier position to compare: nothing is "new"
    else s.prevP = prev ?? s.p;
    s.p = Math.min(1, Math.max(0, p));
    paint({ defer });
    saveUrl();
  }

  async function stepReveal(dir) {
    if (dir < 0 && !(await okToMove())) return;   // forward goes through moveTo, which asks
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

  /** Tick marks on the ruler (each with its chapter as a tooltip; Part and Book starts stronger), and the end of the index. */
  function paintTicks() {
    const ticks = [];
    if (s.map.exact) {
      const isPart = (c) => /^(part|book)\b/i.test(c.title);
      for (const c of s.map.chapters) {
        ticks.push(h(`span.tick${isPart(c) ? ".part" : ""}`, { style: { left: `${s.map.blockStart(c.start) * 100}%` }, title: tidyTitle(c.title) }));
      }
    }
    if (!s.doc.coverage.complete) {
      const at = s.map.anchorPos(s.doc.coverage.through);
      if (Number.isFinite(at) && at < 0.995) ticks.push(h("span.end-mark", { style: { left: `${at * 100}%` }, title: "The index ends here" }));
    }
    if (preview && preview.horizon > 0 && preview.horizon < 0.995) {
      ticks.push(h("span.end-mark", { style: { left: `${preview.horizon * 100}%` }, title: "Preview line" }));
    }
    clear(el.ticks, ticks);
  }

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
      on: { change: async (e) => {
        const want = e.target.value;
        if (!(await okToMove("Changing the reading order closes the form you were editing."))) { e.target.value = s.order; return; }
        s.order = want; render(); saveUrl();
      } } },
      Object.keys(s.series.orders).map((o) => h("option", { value: o, selected: o === s.order }, `${o} order`)));
    const before = order.slice(0, order.indexOf(s.book));
    const fin = new Set(finishedSet());
    const boxes = before.map((k) => {
      const b = s.series.books.find((x) => x.key === k);
      const box = h("input", { type: "checkbox", checked: fin.has(k) });
      box.addEventListener("change", async () => {
        if (!(await okToMove("Changing which books you have finished closes the form you were editing."))) { box.checked = !box.checked; return; }
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
    if (!target || !(await okToMove())) return;
    const was = s.finished;
    s.finished = null;
    if (await load(target.key, { book: k })) saveUrl(); else s.finished = was;
  }

  /** "Last setting: a carriage." from the latest visible event with one, only when it lies in the chapter the position is in. */
  function paintLastSetting() {
    const here = s.map.chapterAt(s.p);
    let found = null;
    if (here) {
      for (let i = s.view.events.length - 1; i >= 0; i--) {
        const ev = s.view.events[i];
        if (!ev.setting?.text) continue;
        const ch = eventChapter(ev);
        if (ch && ch.start === here.start) found = ev;
        break;
      }
    }
    el.setting.textContent = found ? `Last setting: ${found.setting.text}.` : "";
    el.setting.title = found ? found.summary : "";
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
    el.range.setAttribute("aria-valuetext", `${(s.p * 100).toFixed(1)} percent${s.map.chapterAt(s.p) ? ", " + tidyTitle(s.map.chapterAt(s.p).title) : ""}`);
    el.pctNum.textContent = (s.p * 100).toFixed(s.p < 0.1 ? 1 : 0);
    el.pctInput.value = String(Math.round(s.p * 100));
    el.hint.hidden = !(s.p === 0 && !v.entities.length && !v.events.length && !v.chapters.length);
    el.hint.textContent = el.hint.hidden ? "" : "Drag the slider to where you are in the book. Everything below updates to what you've read, and nothing further.";
    const ch = s.map.chapterAt(s.p);
    el.chap.textContent = ch ? tidyTitle(ch.title) : anchor ? `after paragraph ${anchor}` : "before the first reveal";
    el.chap.title = el.chap.textContent;
    paintSource();
    paintLastSetting();

    const notices = [];
    if (!s.map.exact) notices.push(h("div.notice.info", icon("info"),
      h("div", h("b", "Approximate positions. "), "The book file for this index isn't in the library, so percentages are estimated from the index itself. Add the EPUB in the Library for exact positions and chapter marks.")));
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
    ask.check();   // moved back, a book unticked or switched: the thread goes
    const repaint = () => {
      frame = 0;
      paintTabs();
      // A position change must not wipe what someone is typing into an edit form in the drawer. But the spoiler
      // rule outranks the typed text: if the view now hides anything the open entry shows, the entry closes.
      if (s.drawer && drawerDirty()) {
        const e = nameOf(s.drawer);
        const now = e ? new Set(entryIds(e)) : new Set();
        if (!e || [...(s.drawerShows || [])].some((x) => !now.has(x))) {
          closeDrawer({ focus: false });
          toast("The position moved back, so the entry closed. What you typed was not saved.");
        }
      } else if (s.drawer) openDrawer(s.drawer, { refresh: true });
    };
    if (defer) { if (!frame) frame = requestAnimationFrame(repaint); }
    else { if (frame) cancelAnimationFrame(frame); repaint(); }
  }

  function paintTabs() {
    const v = s.view;
    wrap.classList.toggle("wide", s.tab === "links");
    wrap.classList.toggle("asking-tab", s.tab === "ask");   // the map alone gets the full width
    // The short names are for a phone, where all four tabs share one row without scrolling sideways.
    const tabs = [["chapters", "Chapters", chapterGroups().length, "Chapters"], ["cast", "Cast & World", castEntities().length, "Cast"],
      ["timeline", "Timeline", v.events.length, "Timeline"], ["links", "Connections", v.relations.length, "Map"]];
    // Ask needs a library key (not a community preview) and a model; opened by address it explains instead of hiding.
    const askInfo = ask.available;
    if (!preview && (s.tab === "ask" || (askInfo && (askInfo.available || askInfo.reason === "ollama-busy")))) tabs.push(["ask", "Ask", null, "Ask"]);
    const keys = tabs.map((x) => x[0]);
    el.tabs.classList.toggle("five", tabs.length > 4);   // a phone has no room for the counts beside five names
    const select = (k, focus) => {
      const was = s.tab;
      s.tab = k; paintTabs();
      if (k === "ask" || was === "ask") saveUrl();
      if (focus) document.getElementById(`tab-${k}`)?.focus();
    };
    clear(el.tabs, tabs.map(([k, label, n, short]) => h("button", { type: "button", role: "tab", id: `tab-${k}`, "aria-controls": "panel-view",
      "aria-selected": String(s.tab === k), tabindex: s.tab === k ? "0" : "-1",
      on: { click: () => select(k, false), keydown: (ev) => {
        const i = keys.indexOf(k);
        const to = { ArrowRight: keys[(i + 1) % keys.length], ArrowLeft: keys[(i + keys.length - 1) % keys.length], Home: keys[0], End: keys[keys.length - 1] }[ev.key];
        if (to) { ev.preventDefault(); select(to, true); }
      } } }, h("span.tab-full", label), h("span.tab-short", { "aria-hidden": "true" }, short), n == null ? null : h("span.count", String(n)))));
    el.panel.setAttribute("aria-labelledby", `tab-${s.tab}`);
    if (s.tab !== "links") { clear(el.mapSide); el.split.classList.remove("has-side"); }
    ({ chapters: paintChapterRecaps, cast: paintCast, timeline: paintTimeline, links: paintLinks, ask: () => ask.mount(el.panel) })[s.tab]();
    el.panel.prepend(h("h2.sr-only", tabs.find((x) => x[0] === s.tab)[1]));
  }

  function entityFresh(e) { return s.fresh.has(e.id) || e.facets.some((f) => s.fresh.has(f.id)) || (e.profile && s.fresh.has(e.profile.id)); }

  const SORTS = { first: "First appearance", az: "A to Z", known: "Most known" };

  function paintCast() {
    const v = s.view;
    const listed = castEntities();
    const counts = Object.fromEntries(TYPES.map((t) => [t, listed.filter((e) => e.type === t).length]));
    const search = h("input.input", { type: "search", placeholder: "Search names", value: s.q, "aria-label": "Search names and aliases" });
    search.addEventListener("input", () => { s.q = search.value; paintList(); });
    const chips = TYPES.filter((t) => counts[t]).map((t) => h(`button.filter-chip.t-${t}`, { type: "button", "aria-pressed": String(s.types.has(t)),
      on: { click: (e) => { s.types.has(t) ? s.types.delete(t) : s.types.add(t); e.currentTarget.setAttribute("aria-pressed", String(s.types.has(t))); paintList(); } } },
      TYPE_PLURAL[t], h("span.n", String(counts[t]))));
    const sort = h("select.select.sort-select", { "aria-label": "Sort entries" },
      Object.entries(SORTS).map(([k, label]) => h("option", { value: k, selected: s.sort === k }, label)));
    sort.addEventListener("change", () => { s.sort = sort.value; paintList(); });
    // In a series, entries met in earlier books that haven't appeared in this one yet are kept out of
    // the way; one chip brings them back, and a search always looks through them.
    const dormantN = s.series ? v.entities.filter(dormant).length : 0;
    const earlierChip = dormantN ? h("button.filter-chip.earlier", { type: "button", "aria-pressed": String(s.earlier),
      title: s.earlier ? "Hide entries from earlier books that haven't appeared in this one yet" : "Show entries from earlier books that haven't appeared in this one yet",
      on: { click: () => {
        s.earlier = !s.earlier;
        try { localStorage.setItem("lk-earlier", s.earlier ? "1" : "0"); } catch {}
        paintTabs();
      } } }, icon("layers"), "From earlier books", h("span.n", String(dormantN))) : null;
    const list = h("div.cast-list");
    function paintList() {
      const q = s.q.trim().toLowerCase();
      const pool = q ? v.entities : castEntities();
      const shown = pool.filter((e) => (!s.types.size || s.types.has(e.type))
        && (!q || [e.name, ...e.aliases].some((n) => n.toLowerCase().includes(q))));
      const by = { first: () => 0, az: (a, b) => a.name.localeCompare(b.name),
        known: (a, b) => b.facets.length - a.facets.length }[s.sort];
      const order = (a, b) => (s.series ? Number(dormant(a)) - Number(dormant(b)) : 0) || by(a, b);   // this book's cast first
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
    clear(el.panel, h("div.filters", h("div.search", icon("search"), search), h("div.type-chips", { role: "group", "aria-label": "Show types" }, chips, earlierChip), sort), dupesNotice(),
      s.edit ? editor.addEntityButton() : null, list);
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
    const from = carried(e.id) ? revealInfo(e.id).book : null;
    const sleeping = from && !inThisBook(e.id);
    const open = s.drawer === e.id && el.pane && !el.pane.hidden;
    const rowEl = h(`button.ent-row.t-${e.type}${fresh ? ".fresh.arrive" : ""}${sleeping ? ".dormant" : ""}${open ? ".sel" : ""}`, { type: "button",
      dataset: { ent: e.id }, "aria-current": open ? "true" : null,
      "aria-label": `${e.name}, ${TYPE_LABEL[e.type]}${fresh ? ", new" : ""}${from ? `, from ${bookShort(from)}${sleeping ? ", not in this book yet" : ""}` : ""}`,
      on: { click: (ev) => openDrawer(e.id, { from: ev.currentTarget }) } },
      h(`span.ent-dot${fresh ? ".fresh" : ""}`, { "aria-hidden": "true", title: fresh ? "New" : null }),
      h("span.ent-name", h("span.nm", e.name),
        e.aliases.length ? h("span.aka", e.aliases.slice(0, 3).join(", ")) : null),
      h("span.ent-fact", about || h("span.subtle", "Named, but nothing more is known yet.")),
      h("span.ent-meta",
        from ? h("span.book-label.ent-from", { title: sleeping ? "Met in an earlier book; not in this one yet" : "Met in an earlier book" }, `from ${bookShort(from)}`) : null,
        e.facets.length ? [h("span.num", String(e.facets.length)), e.facets.length === 1 ? " detail" : " details"] : null,
        e.history.length ? h("span.hist", { title: `${e.history.length} earlier impression${e.history.length === 1 ? "" : "s"}` }, icon("refresh")) : null));
    if (fresh) rowEl.addEventListener("animationend", () => rowEl.classList.remove("arrive"), { once: true });
    return rowEl;
  }

  // ---- chapters ----------------------------------------------------------------------------

  /** Visible chapter recaps, with the parts of one long chapter joined: [{ key, title, book, parts, ids }]. */
  function chapterGroups() {
    const out = [];
    // The map chapter a recap of the book being read ends in (parts of one long chapter share it; two chapters with
    // the same title do not). Unknown for earlier books and without a position for its anchor.
    const docRecaps = new Map((s.doc.recaps || []).map((r) => [r.id, r]));
    const chapterOf = (c) => {
      if (s.series && c.book && c.book !== s.book) return null;
      const at = s.map.anchorPos(docRecaps.get(c.id)?.from);
      return Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
    };
    for (const c of s.view.chapters || []) {
      const last = out[out.length - 1];
      const ch = chapterOf(c);
      // With a chapter map, parts join only inside one mapped chapter; a recap that has no position there is a row of
      // its own. Without one (an earlier book, or no book file) nothing can tell parts of "Interlude" from two
      // chapters called "Interlude", so consecutive same-title recaps are parts of one chapter.
      const mapped = !(s.series && c.book && c.book !== s.book) && (s.map.chapters || []).length > 0;
      const same = last && last.title === c.title && last.book === c.book && (!mapped || (ch && last.ch && ch.start === last.ch.start));
      if (same) { last.parts.push(c.text); last.ids.push(c.id); last.ch ||= ch; }
      else out.push({ key: c.id, title: c.title || "Untitled section", book: c.book, parts: [c.text], ids: [c.id], ch });
    }
    return out;
  }

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

  // ---- book blocks: the one structure both the Chapters and the Timeline tabs use --------------

  const NEAR = 5;   // chapters shown per book before "Show all"
  const bkKey = (k) => k || "_";

  /** The book's cover URL (from the API, never from the index), or null. */
  function bookCover(key) {
    let b = null;
    if (!s.series || !key || key === s.book) b = catalogBook();
    else {
      const idx = s.lib.indexes.find((i) => i.series?.id === s.series.id && i.series.book === key);
      b = idx && s.catalog?.books.find((x) => x.versions.some((v) => v.key === idx.key));
    }
    const url = b?.cover;
    return typeof url === "string" && url.startsWith("/api/") ? url : null;
  }
  const bookTitleOf = (key) => (s.series && key ? s.series.books.find((x) => x.key === key)?.title || key : s.doc.work.title);
  const bookNoLabel = (key) => {
    if (!s.series || !key) return null;
    const i = (s.series.orders[s.order] || []).indexOf(key);
    return i < 0 ? null : `Book ${i + 1}`;
  };

  /** The cover fading into the ground with the book's title over it. As a summary it opens a collapsed book. */
  function bookBanner(key, { summary = false } = {}) {
    const cov = bookCover(key), label = bookNoLabel(key);
    const img = cov ? h("img.banner-img", { src: cov, alt: "", loading: "lazy" }) : null;
    if (img) img.addEventListener("error", () => img.remove());
    return h(`${summary ? "summary" : "div"}.book-banner${cov ? ".has-cover" : ""}`, img,
      h("span.banner-text", label ? h("span.banner-no", label) : null, h("span.banner-title", bookTitleOf(key))),
      summary ? icon("chevron") : null);
  }

  /** One book on the rail. `items`: [{ label, title, body, cls, id, badge }] in display order, `reached` is the
   *  count in the button ("Show all 12 chapters": what the reader has reached, never the book's total). */
  function bookBlock(key, items, { current, newest, noun = "chapter", repaint, none, raw }) {
    const all = s.bkAll.has(bkKey(key));
    const folded = !all && items.length > NEAR;
    const shown = !folded ? items : newest ? items.slice(0, NEAR) : items.slice(-NEAR);
    const rail = items.length ? h("ol.ch-rail", shown.map((it) => h(`li.ch-item${it.cls ? `.${it.cls}` : ""}`, { "data-chapter": it.id || null },
      it.label ? h("span.ch-label", it.label) : null,
      it.title ? h("div.ch-name", it.title, it.badge || null) : null,
      it.body))) : none ? h("p.muted.book-none", none) : null;
    const total = raw ? raw.total : items.length;
    const toggle = total > NEAR ? h("button.btn.ch-all", { type: "button", dataset: { book: bkKey(key) }, "aria-expanded": String(all),
      on: { click: () => {
        all ? s.bkAll.delete(bkKey(key)) : s.bkAll.add(bkKey(key));
        repaint();
        el.panel.querySelector(`.ch-all[data-book="${CSS.escape(bkKey(key))}"]`)?.focus({ preventScroll: true });
      } } }, all ? `Show only the latest ${NEAR}` : `Show all ${total} ${noun}${total === 1 ? "" : "s"}`) : null;
    const content = raw ? raw.build(all) : rail;
    if (current) return h("section.book-block.stack-book.current", { "aria-label": bookTitleOf(key) }, bookBanner(key), content, toggle);
    return h("details.book-block.stack-book.earlier", { "aria-label": bookTitleOf(key), open: s.bkOpen.has(bkKey(key)) ? "" : null,
      on: { toggle: (ev) => { ev.currentTarget.open ? s.bkOpen.add(bkKey(key)) : s.bkOpen.delete(bkKey(key)); } } },
      bookBanner(key, { summary: true }), content, toggle);
  }

  /** "Chapter 7" over a title that does not say what it is; no label when the title carries its own number or kind. */
  const SELF_NAMED = /^\s*(chapter|part|book|prologue|epilogue|preface|introduction|foreword|afterword|section|letter)\b/i;
  const chapterLabel = (title, n) => (SELF_NAMED.test(tidyTitle(title) || "") ? null : `Chapter ${n}`);

  /** Chapter rows of the book being read, oldest first, the same on both tabs: every revealed recap, the chapter the
   *  position is in (title only until its recap is revealed) and any chapter an event happens in. */
  function currentRows(groups) {
    const rows = [], byKey = new Map();
    const create = (key, title, ch) => { const r = { key, title, ch, group: null, events: [] }; byKey.set(key, r); rows.push(r); return r; };
    // A row is a chapter, never a title: two chapters called "Interlude" stay two rows.
    const forChapter = (ch) => {
      const r = byKey.get(`c${ch.start}`);
      if (r) return r;
      return create(`c${ch.start}`, ch.title, ch);
    };
    for (const g of groups) {
      const r = g.ch ? forChapter(g.ch) : create(`g:${g.key}`, g.title, null);
      if (!r.group) r.title = g.title;   // the recap's own title, as the Chapters tab always showed
      r.group = r.group ? { ...r.group, parts: [...r.group.parts, ...g.parts], ids: [...r.group.ids, ...g.ids] } : g;
    }
    const here = s.map.chapterAt(s.p);
    if (here) forChapter(here).now = true;
    for (const ev of s.view.events) {
      if (s.series && ev.book && ev.book !== s.book) continue;
      const ch = eventChapter(ev);
      if (ch) forChapter(ch).events.push(ev);
      else (rows.loose ||= []).push(ev);
    }
    const chapters = s.map.chapters || [];
    // Book order by chapter; a recap with no position there follows the nearest row before it.
    let prev = -0.5;
    const key = new Map(rows.map((r) => {
      const i = r.ch ? chapters.findIndex((c) => c.start === r.ch.start) : -1;
      if (i >= 0) prev = i; else if (r.ch) prev = Infinity; else prev += 1e-3;
      return [r, r.ch && i < 0 ? Infinity : prev];
    }));
    if (chapters.length) rows.sort((a, b) => key.get(a) - key.get(b));
    return rows;
  }

  /** An earlier book's rows from its own chapter recaps; each event goes to the first recap at or after the point it
   *  is revealed (a recap is revealed where its chapter ends). [] when that index has no chapter recaps. */
  function earlierRows(b, groups) {
    const rows = groups.map((g) => ({ title: g.title, group: g, events: [] }));
    const d = s.seriesDocs.get(b);
    if (!rows.length || !d) return rows;
    const ord = new Map((d.anchors || []).map((a, i) => [a.id, i]));
    const recapOrd = new Map((d.recaps || []).filter((r) => r.scope === "chapter").map((r) => [r.id, ord.get(r.from) ?? Infinity]));
    const rowOrd = rows.map((r) => Math.max(...r.group.ids.map((id) => recapOrd.get(id) ?? -1)));
    for (const ev of s.view.events) {
      if (ev.book !== b) continue;
      const at = ord.get(revealInfo(ev.id)?.from) ?? Infinity;
      const i = rowOrd.findIndex((o) => o >= at);
      rows[i < 0 ? rows.length - 1 : i].events.push(ev);
    }
    return rows;
  }

  /** The chapters tab's rail items for rows. */
  function chapterItems(rows) {
    return rows.map((r, i) => {
      const g = r.group, parts = g ? g.parts : [];
      const fresh = (g && g.ids.some((id) => s.fresh.has(id))) || false;
      const who = parts.length ? mentions(parts.join(" ")) : [];
      return { id: g ? g.key : null, label: chapterLabel(r.title, i + 1), title: tidyTitle(r.title), cls: r.now ? "now" : fresh ? "fresh" : "",
        badge: r.now ? h("span.chip.ch-ongoing", "So far") : fresh ? h("span.new-badge", "New") : null,
        body: parts.length ? h("div.ch-body", parts.map((t, k) => {
          if (!s.edit) return h("p", t);
          const part = h("div.ch-part");
          clear(part, h("p", t), editor.recapRowActions({ id: g.ids[k], text: t }, part, { scope: "chapter", title: g.title }));
          return part;
        }), who.length ? h("div.ch-who", h("span.eyebrow", "In this chapter"), h("div.ents", who.slice(0, 14).map((e) => entityChip(e.id)))) : null) : null };
    });
  }

  /** Two buttons, each with its own pressed state, instead of one that relabels itself. The repaint
   *  replaces the panel, so focus goes back to the button that was used. */
  function orderToggle(newest, set) {
    const btn = (label, value) => h("button", { type: "button", "aria-pressed": String(newest === value), dataset: { order: value ? "newest" : "oldest" },
      on: { click: () => {
        if (newest === value) return;
        set(value);
        el.panel.querySelector(`.order-seg [data-order="${value ? "newest" : "oldest"}"]`)?.focus();
      } } }, label);
    return h("div.seg.order-seg", { role: "group", "aria-label": "Order" }, btn("Newest first", true), btn("Oldest first", false));
  }

  function paintChapterRecaps() {
    const v = s.view;
    const groups = chapterGroups();
    const hasAny = (s.doc.recaps || []).some((r) => r.scope === "chapter") || [...s.seriesDocs.values()].some((d) => (d?.recaps || []).some((r) => r.scope === "chapter"));
    const story = h("section.story-now", { "aria-label": "Story so far" });
    // Three lines, then "Read the whole recap": the lists below are the point of a glance.
    const recapText = h(`p.recap-text${s.recapOpen ? "" : ".clamp"}`, v.recap ? v.recap.text : "Nothing to recap yet. Move forward in the book to see the story so far.");
    const more = v.recap ? h("button.linkish.recap-more", { type: "button", "aria-expanded": String(Boolean(s.recapOpen)),
      on: { click: () => { s.recapOpen = !s.recapOpen; recapText.classList.toggle("clamp", !s.recapOpen); more.setAttribute("aria-expanded", String(s.recapOpen)); more.textContent = s.recapOpen ? "Show less" : "Read the whole recap"; } } },
      s.recapOpen ? "Show less" : "Read the whole recap") : null;
    clear(story, h("span.eyebrow", "Story so far"), recapText, more,
      v.recap && bookLabel(v.recap.book),
      s.edit && v.recap ? editor.recapRowActions(v.recap, story) : null);
    // The control is only worth showing when the recap runs past three lines.
    if (more) requestAnimationFrame(() => { if (!s.recapOpen && recapText.scrollHeight <= recapText.clientHeight + 2) more.hidden = true; });
    const addRecap = s.edit ? editor.addRecapButton() : null;
    // In a series, each book's chapters are their own block on one rail that runs down to the book being read.
    const mine = groups.filter((g) => !s.series || !g.book || g.book === s.book);
    const earlierBooks = s.series ? (s.series.orders[s.order] || []).filter((b) => b !== s.book && groups.some((g) => g.book === b)) : [];
    const rows = currentRows(mine);
    if (!rows.length && !earlierBooks.length) {
      return clear(el.panel, story, addRecap, h("div.empty", h("div.art", icon("book-open")),
        h("h3", hasAny ? "No chapters finished yet" : "No chapter recaps in this index"),
        h("p", hasAny ? "Each chapter's summary appears once you've read past it." : s.edit ? "Add a recap for each chapter as you finish it." : "This index was built before chapter recaps existed. Build a new version to get a summary of every chapter.")));
    }
    const earlierRowsBy = new Map(earlierBooks.map((b) => [b, earlierRows(b, groups.filter((g) => g.book === b))]));
    const earlierTotal = [...earlierRowsBy.values()].reduce((n, r) => n + r.length, 0);
    const toggle = orderToggle(s.chaptersNewest, (v) => { s.chaptersNewest = v; paintChapterRecaps(); });
    const seriesChip = earlierBooks.length ? h("button.filter-chip.ch-series", { type: "button", "aria-pressed": String(s.chSeries),
      title: s.chSeries ? "Show this book's chapters only" : "Show the earlier books' chapters too",
      on: { click: () => { s.chSeries = !s.chSeries; try { localStorage.setItem("lk-ch-series", s.chSeries ? "1" : "0"); } catch {} paintChapterRecaps(); } } },
      icon("layers"), "Whole series", h("span.n", String(earlierTotal))) : null;
    const count = `${rows.length} chapter${rows.length === 1 ? "" : "s"} so far`;
    const repaint = () => paintChapterRecaps();
    const ordered = (items) => (s.chaptersNewest ? [...items].reverse() : items);
    const currentBlock = bookBlock(s.series ? s.book : null, ordered(chapterItems(rows)), { current: true, newest: s.chaptersNewest, repaint,
      none: "No chapter finished in this book yet. Its summaries appear as you read past each chapter." });
    if (!earlierBooks.length) {
      return clear(el.panel, story, h("div.toolbar.chapters-bar", h("span.hint", count), addRecap, toggle), h("div.book-stack", currentBlock));
    }
    const earlierBlocks = earlierBooks.map((b) => bookBlock(b, ordered(chapterItems(earlierRowsBy.get(b))), { current: false, newest: s.chaptersNewest, repaint }));
    // Newest first puts the book being read at the top and the earlier books beneath it, latest first.
    const blocks = s.chSeries ? (s.chaptersNewest ? [currentBlock, ...earlierBlocks.reverse()] : [...earlierBlocks, currentBlock]) : [currentBlock];
    clear(el.panel, story,
      h("div.toolbar.chapters-bar", h("span.hint", s.chSeries ? `${count} in this book, ${earlierTotal} in earlier books` : count), seriesChip, addRecap, toggle),
      h("div.book-stack", blocks));
  }

  const nameOf = (id) => s.view.entities.find((e) => e.id === id);
  const entityChip = (id) => {
    const e = nameOf(id);
    if (!e) return null;
    return h(`button.link-chip.t-${e.type}`, { type: "button", on: { click: (ev) => openDrawer(id, { from: ev.currentTarget }) } }, e.name);
  };
  const bookLabel = (b) => (s.series && b && b !== s.book ? h("span.book-label", `from ${bookShort(b)}`) : null);

  /** The chapter an event of this book happens in (null for another book's events or without the book file). */
  function eventChapter(ev) {
    const r = revealInfo(ev.id);
    if (!r || (s.series && r.book !== s.book)) return null;
    const at = s.map.anchorPos(r.from);
    return Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
  }
  const capFirst = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);
  /** The heading of a scene run: the setting, first letter capitalised; a quiet link when its place is in the view. */
  function settingHeading(setting) {
    const place = setting.place && nameOf(setting.place) ? setting.place : null;
    const label = capFirst(setting.text);
    return h("li.tl-setting", icon("pin"), place
      ? h("button.linkish", { type: "button", on: { click: (ev) => openDrawer(place, { from: ev.currentTarget }) } }, label)
      : h("span", label));
  }
  /** Consecutive events with the same setting (case-insensitive) form a run; events without one never join a neighbour. */
  function sceneRuns(events) {
    const runs = [];
    for (const ev of events) {
      const key = ev.setting?.text ? ev.setting.text.trim().toLowerCase() : null;
      const last = runs[runs.length - 1];
      if (key && last && last.key === key) last.events.push(ev);
      else runs.push({ key, setting: key ? ev.setting : null, events: [ev] });
    }
    return runs;
  }
  /** One timeline row: turning points stand out, small moments stay quiet. */
  function eventRow(ev, { chips = true, book = true } = {}) {
    const li = h(`li.tl-ev${ev.kind ? `.${ev.kind}` : ""}${s.fresh.has(ev.id) ? ".fresh" : ""}`, { dataset: { ev: ev.id }, tabindex: "-1" });
    clear(li, ev.kind === "turn" ? h("span.tl-kind", "Turning point") : null, h("p.ev", ev.summary),
      chips ? h("div.ents", ev.entities.map(entityChip), bookLabel(ev.book)) : book ? bookLabel(ev.book) : null,
      s.edit ? editor.eventRowActions(ev, li) : null);
    return li;
  }

  function paintTimeline() {
    const all = s.view.events;
    const hasKinds = all.some((ev) => ev.kind);   // older indexes weigh nothing: every event shows
    const turns = all.filter((ev) => ev.kind === "turn").length, minor = all.filter((ev) => ev.kind === "minor").length;
    const shown = all.filter((ev) => (!s.tlTurns || ev.kind === "turn") && (s.tlMinor || !hasKinds || ev.kind !== "minor"));
    const evs = s.newestFirst ? [...shown].reverse() : shown;
    const remember = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch {} };
    const toggle = orderToggle(s.newestFirst, (v) => { s.newestFirst = v; paintTimeline(); });
    const turnChip = hasKinds ? h("button.filter-chip.tl-turns", { type: "button", "aria-pressed": String(s.tlTurns),
      title: "Only the revelations, twists and decisions that change the course of the story",
      on: { click: () => { s.tlTurns = !s.tlTurns; remember("lk-tl-turns", s.tlTurns); paintTimeline(); } } },
      icon("sparkle"), "Turning points", h("span.n", String(turns))) : null;
    const minorChip = hasKinds && minor ? h("button.filter-chip.tl-minor", { type: "button", "aria-pressed": String(s.tlMinor),
      title: s.tlMinor ? "Hide the small moments that don't change the story" : "Show the small moments too",
      on: { click: () => { s.tlMinor = !s.tlMinor; remember("lk-tl-minor", s.tlMinor); paintTimeline(); } } },
      "Small moments", h("span.n", String(minor))) : null;
    // The same book -> chapters rows as the Chapters tab. The chips filter the events inside the rows; they never
    // change which chapters are listed or the "Show all" count.
    const keep = new Set(shown);
    const inRow = (events) => { const e = events.filter((ev) => keep.has(ev)); return s.newestFirst ? e.reverse() : e; };
    const rowBody = (events, total = events.length) => (events.length ? h("ol.tl-list", sceneRuns(events).map((run) => [run.setting ? settingHeading(run.setting) : null, run.events.map((ev) => eventRow(ev))]))
      : h("p.muted.tl-none", total ? "Nothing to show with these filters." : "No events in this chapter."));
    const rowItems = (rows) => rows.map((r, i) => {
      const evs = inRow(r.events);
      return { id: null, label: chapterLabel(r.title, i + 1), title: tidyTitle(r.title),
        cls: r.now ? "now" : r.events.some((ev) => s.fresh.has(ev.id)) ? "fresh" : "", body: rowBody(evs, r.events.length) };
    });
    const empty = !all.length ? ["No events yet", s.edit ? "Add the first event from where you are in the book." : "Key moments appear here as you read."]
      : s.tlTurns ? ["No turning points yet", "Nothing read so far has changed the course of the story."]
        : ["Only small moments so far", "Turn on small moments to see them."];
    const repaint = () => paintTimeline();
    const ordered = (items) => (s.newestFirst ? [...items].reverse() : items);
    const rows = currentRows(chapterGroups().filter((g) => !s.series || !g.book || g.book === s.book));
    const loose = (rows.loose || []).filter((ev) => keep.has(ev));
    const currentItems = ordered(rowItems(rows));
    if (loose.length) {   // no chapter to put them in (no book file): one block of their own
      const item = { label: null, title: null, cls: "", body: rowBody(s.newestFirst ? [...loose].reverse() : loose) };
      currentItems[s.newestFirst ? "unshift" : "push"](item);
    }
    const currentBlock = currentItems.length ? bookBlock(s.series ? s.book : null, currentItems, { current: true, newest: s.newestFirst, repaint }) : null;
    const order = s.series ? s.series.orders[s.order] || [] : [];
    const earlierKeys = order.filter((b) => b !== s.book && all.some((ev) => ev.book === b));
    const earlierBlocks = (s.newestFirst ? [...earlierKeys].reverse() : earlierKeys).map((b) => {
      const rowsB = earlierRows(b, chapterGroups().filter((g) => g.book === b));
      if (rowsB.length) return bookBlock(b, ordered(rowItems(rowsB)), { current: false, newest: s.newestFirst, repaint });
      // That index has no chapter recaps: its events as one block with scene runs.
      const evs = inRow(all.filter((ev) => ev.book === b));
      if (!evs.length) return null;   // nothing to show under these filters: no block, no banner
      return bookBlock(b, [], { current: false, newest: s.newestFirst, noun: "event", repaint,
        raw: { total: evs.length, build: (full) => (evs.length ? h("div.ch-body.tl-flat", rowBody(full || evs.length <= NEAR ? evs : s.newestFirst ? evs.slice(0, NEAR) : evs.slice(-NEAR))) : null) } });
    });
    const blocks = (s.newestFirst ? [currentBlock, ...earlierBlocks] : [...earlierBlocks, currentBlock]).filter(Boolean);
    clear(el.panel, h("div.toolbar.tl-bar", toggle, turnChip, minorChip, s.edit ? editor.addEventButton() : null),
      blocks.some(Boolean) ? h("div.book-stack", blocks.filter(Boolean))
        : h("div.empty", h("div.art", icon("timeline")), h("h3", empty[0]), h("p", empty[1])));
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

  /** The series book that introduces an entity (or relation) and the anchor it is revealed at. */
  function revealInfo(id) {
    // Only the book being read and the finished ones count, walked in the reading order: an entity first met in a book the
    // reader has not finished (or that the order puts after this one) is not "from" that book as far as the reader knows.
    const key = s.series ? `${s.order}|${[...(finishedSet() || []), s.book].join(",")}` : "";
    if (!s.reveals || s.revealsKey !== key) {
      s.reveals = new Map(); s.revealsKey = key;
      const known = new Set([...(finishedSet() || []), s.book]);
      const docs = s.series ? (s.series.orders[s.order] || []).filter((b) => known.has(b)).map((b) => [b, b === s.book ? s.doc : s.seriesDocs.get(b)]) : [[null, s.doc]];
      for (const [book, d] of docs) {
        if (!d) continue;
        for (const e of d.entities || []) if (!s.reveals.has(e.id)) s.reveals.set(e.id, { book, from: e.from });
        for (const r of d.relations || []) s.reveals.set(r.id, { book, from: r.from });
        for (const ev of d.events || []) s.reveals.set(ev.id, { book, from: ev.from });
      }
    }
    return s.reveals.get(id) || null;
  }

  /** An entity first met in an earlier book of the series. */
  const carried = (id) => Boolean(s.series && revealInfo(id) && revealInfo(id).book !== s.book);
  /** Has the entity appeared in the book being read, up to the current position? (Its own index lists
   *  it, revealed at or before the frontier.) Carried entities that haven't are "dormant": still known
   *  from earlier books, but not part of this book yet. */
  function inThisBook(id) {
    const e = s.doc.entities.find((x) => x.id === id);
    if (!e) return false;
    if (!s.docOrds) s.docOrds = new Map(s.doc.anchors.map((a, i) => [a.id, i]));
    const front = s.map.frontier(s.p);
    return front != null && (s.docOrds.get(e.from) ?? Infinity) <= s.docOrds.get(front);
  }
  const dormant = (e) => carried(e.id) && !inThisBook(e.id);
  /** A book's title without the part every book of the series shares ("Chamber of Secrets"). */
  function bookShort(key) {
    const books = s.series?.books || [];
    const b = books.find((x) => x.key === key);
    if (!b) return key;
    const words = books.map((x) => (x.title || "").split(/\s+/));
    let n = 0;
    if (words.length > 1) while (words.every((w) => w.length > n + 1 && w[n].toLowerCase() === words[0][n].toLowerCase())) n++;
    return n >= 2 ? b.title.split(/\s+/).slice(n).join(" ") : b.title;
  }
  /** The cast as listed: entries from earlier books that haven't appeared in this one are hidden
   *  unless the reader asks for them (or is searching). */
  function castEntities() {
    return s.series && !s.earlier ? s.view.entities.filter((e) => !dormant(e)) : s.view.entities;
  }
  function castView() {
    if (!s.series || s.earlier) return s.view;
    const ents = castEntities();
    const ids = new Set(ents.map((e) => e.id));
    return { ...s.view, entities: ents, relations: s.view.relations.filter((r) => ids.has(r.src) && ids.has(r.dst)) };
  }

  /** Where an entity or relation was first revealed, and whether that's in the chapter being read. */
  function revealed(id) {
    const r = revealInfo(id);
    if (!r) return null;
    if (s.series && r.book !== s.book) return { where: `in ${bookShort(r.book)}`, isNew: false };
    const at = s.map.anchorPos(r.from);
    const ch = Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
    const here = s.map.chapterAt(s.p);
    return { where: ch ? `in ${shortChapter(ch)}` : null, isNew: Boolean(here && ch && ch.start === here.start) };
  }

  /** The families the Connections tab can draw right now (same input as the tab, so the chip, the palette and the link agree). */
  let famMemo = null;
  function currentFamilies() {
    const history = Boolean(s.graph.history);
    if (!famMemo || famMemo.view !== s.view || famMemo.earlier !== s.earlier || famMemo.history !== history) famMemo = { view: s.view, earlier: s.earlier, history, list: families(castView(), { history }) };
    return famMemo.list;
  }
  /** Open the Connections tab drawn as family trees, optionally on one person's family. */
  async function showFamilyTree(id = null) {
    // The tree's details take the entry's place on the right, so the entry closes; typed text is asked about first.
    if (drawerDirty() && !(await confirmDialog({ title: "Discard what you wrote?", message: "Opening the family tree closes the form you were editing.",
      confirmLabel: "Discard", cancelLabel: "Keep editing" }))) return;
    closeDrawer({ focus: false });
    s.tab = "links";
    s.graph.trees = true;
    s.graph.mode = "map"; s.graph.list = false;
    if (id) { s.graph.sel = id; s.graph.treeTo = id; }
    paintTabs();
    el.panel.scrollIntoView?.({ block: "start" });
  }

  function paintLinks() {
    const here = s.map.chapterAt(s.p);
    renderConnections(el.panel, {
      view: castView(), types: s.types, gs: s.graph,
      chapterMentions: chapterGroups().map((g) => mentions(g.parts.join(" ")).map((e) => e.id)),
      revealed, hereLabel: here ? shortChapter(here) : "this chapter",
      openEntity: (id, from) => openDrawer(id, { from }),
      repaint: () => paintTabs(),
    });
    // Beside the list (1101px and up) the map's details take the entry's place on the right; narrower they stay under the map.
    const side = el.panel.querySelector(".g-panel");
    const keep = el.mapSide.contains(document.activeElement) ? document.activeElement : null;
    if (side && paneMode()) { const scroll = el.mapSide.scrollTop; clear(el.mapSide, side); el.mapSide.scrollTop = scroll; el.split.classList.add("has-side"); }
    else { clear(el.mapSide); el.split.classList.remove("has-side"); }
    if (keep && !document.contains(keep)) el.mapSide.querySelector("button")?.focus({ preventScroll: true });
  }

  // ---- the entry: a pane beside the list, or the modal drawer ----------------------------------

  /** Everything the open entry shows, as ids: names, profile, details, earlier impressions, connections, events. */
  function entryIds(e) {
    const v = s.view;
    const rels = v.relations.filter((r) => r.src === e.id || r.dst === e.id);
    const evs = v.events.filter((ev) => ev.entities.includes(e.id));
    // The other entries the entry names (connection and event chips), by the name they show now: names grow with the
    // position ("Van Helsing" becomes "Abraham Van Helsing"), so a later name must not stay on screen either.
    const others = [...new Set([...rels.flatMap((r) => [r.src, r.dst]), ...evs.flatMap((ev) => ev.entities)])].filter((x) => x !== e.id);
    return [`name:${e.name}`, ...e.aliases.map((a) => `name:${a}`), e.profile ? `p:${e.profile.id}` : null,
      ...e.facets.map((f) => `f:${f.id}`), ...e.history.map((f) => `h:${f.id}`),
      ...rels.map((r) => `r:${r.id}`), ...evs.map((ev) => `e:${ev.id}`),
      ...others.map((x) => `other:${x}:${nameOf(x)?.name ?? ""}`)].filter(Boolean);
  }
  // From 1101px the entry opens in a pane on the right (not modal: the list stays usable, another row swaps it,
  // arrow keys in the list move it, Escape or Close returns focus to the row). Narrower, it is the modal drawer.
  // Both are `.drawer`, so the edit forms, the dirty check and the folds work the same in either.

  const paneMode = () => matchMedia("(min-width: 1101px)").matches;
  const host = () => (el.pane && !el.pane.hidden ? el.pane : document.querySelector("dialog.drawer"));
  let lastFocus = null;
  /** True while an edit form in the open entry holds text that was typed and not saved. */
  function drawerDirty() {
    // A form is dirty once anything in it was edited (see markDirty), or a text field differs from what it opened with.
    const box = host();
    return Boolean(box) && [...box.querySelectorAll(".edit-form, .editing-title")].some((f) =>
      f.dataset.dirty === "1" || [...f.querySelectorAll("textarea, input")].some((i) => i.value !== i.defaultValue));
  }
  /** One delegated listener on the entry: any input or change inside an edit form marks that form dirty.
   *  The flag lives on the form element, so cancelling or saving (which removes the form) clears it. */
  function markDirty(ev) {
    const form = ev.target.closest?.(".edit-form, .editing-title");
    if (form) form.dataset.dirty = "1";
  }
  /** Every way a person dismisses the entry (×, Escape, the backdrop) asks first when it would lose typed text. */
  async function requestCloseDrawer() {
    if (!drawerDirty() || await confirmDialog({ title: "Discard what you wrote?", confirmLabel: "Discard", cancelLabel: "Keep editing" })) closeDrawer();
  }
  /** Close the entry. `focus` hands focus back to the row it was opened from (not when the position moved it away). */
  function closeDrawer({ focus = true } = {}) {
    const id = s.drawer;
    const d = document.querySelector("dialog.drawer");
    s.drawer = null;
    if (d) { d.removeEventListener("close", onDialogClose); if (d.open) d.close(); d.remove(); }
    if (el.pane && !el.pane.hidden) { el.pane.hidden = true; clear(el.pane); el.split.classList.remove("has-entry"); }
    markOpenRow();
    checkPending();
    if (!focus) return;
    // The list repaints as the position moves, so the row it came from may be a new element by now.
    const row = lastFocus && document.contains(lastFocus) ? lastFocus : id ? el.panel?.querySelector(`[data-ent="${CSS.escape(id)}"]`) : null;
    row?.focus();
  }
  // Escape closes a native dialog without going through closeDrawer.
  function onDialogClose() { closeDrawer(); }
  /** The row of the open entry carries `aria-current` and the selected tint. */
  function markOpenRow() {
    el.panel?.querySelectorAll("[data-ent]").forEach((r) => {
      const on = r.dataset.ent === s.drawer && !el.pane.hidden;
      r.classList.toggle("sel", on);
      if (on) r.setAttribute("aria-current", "true"); else r.removeAttribute("aria-current");
    });
  }

  /** Up and down in the Cast list move between entries; with the pane open the entry follows. Escape closes it. */
  function onListKey(ev) {
    if (ev.key === "Escape" && s.drawer && !el.pane.hidden && !ev.defaultPrevented && !/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) {
      ev.preventDefault(); requestCloseDrawer(); return;
    }
    const row = ev.target.closest?.("[data-ent]");
    if (!row || !["ArrowDown", "ArrowUp", "Home", "End"].includes(ev.key)) return;
    const rows = [...el.panel.querySelectorAll("button[data-ent]")];
    const i = rows.indexOf(row);
    const next = rows[{ ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: rows.length - 1 }[ev.key]];
    if (!next) return;
    ev.preventDefault();
    next.focus();
    if (s.drawer && !el.pane.hidden) openDrawer(next.dataset.ent, { from: next, keepFocus: true });
  }
  /** On a phone the ruler is one line; the chapter select and the percent field go to the line under it. */
  function placeJump() {
    if (!el.jump) return;
    const phone = matchMedia("(max-width: 640px)").matches;
    const target = phone ? el.sub : el.position;
    if (el.jump.parentNode !== target) target.append(el.jump);
    // On a phone the head row goes (the app bar has the title): Edit and Switch book end the jump row.
    const actionsAt = phone ? el.jump : el.headActions?.parentNode === el.jump ? wrap.querySelector(".rd-head") : null;
    if (actionsAt && el.headActions && el.headActions.parentNode !== actionsAt) actionsAt.append(el.headActions);
  }
  // The width can cross 1100px with an entry open: move it to the other form.
  let modeWas = paneMode();
  const onResize = () => {
    placeJump();
    const now = paneMode();
    if (now !== modeWas) {
      modeWas = now;
      if (s.drawer) moveEntry(now);
      if (s.tab === "links" && el.panel) paintTabs();
    }
  };
  window.addEventListener("resize", onResize);

  /** Move the open entry between the pane and the modal drawer as it is: the same nodes, so typed text, open
   *  folds and listeners come along. */
  function moveEntry(toPane) {
    const dlg = document.querySelector("dialog.drawer");
    const from = toPane ? dlg : (!el.pane.hidden ? el.pane : null);
    if (!from) return;
    const nodes = [...from.childNodes];
    const type = [...from.classList].find((c) => c.startsWith("t-"));
    if (toPane) {
      dlg.removeEventListener("close", onDialogClose);
      if (dlg.open) dlg.close();
      dlg.remove();
      el.pane.className = `entry-pane drawer ${type || ""}`;
      el.pane.hidden = false;
      el.pane.append(...nodes);
      el.pane.setAttribute("aria-label", el.pane.querySelector("#drawer-title")?.textContent || "");
      el.split.classList.add("has-entry");
      markOpenRow();
    } else {
      el.pane.hidden = true;
      el.split.classList.remove("has-entry");
      const d = h(`dialog.drawer${type ? `.${type}` : ""}`, { "aria-labelledby": "drawer-title",
        on: { input: markDirty, change: markDirty, click: (ev) => { checkPending(); if (ev.target === d) requestCloseDrawer(); }, keyup: checkPending, close: onDialogClose,
          cancel: (ev) => { if (drawerDirty()) { ev.preventDefault(); requestCloseDrawer(); } } } }, nodes);
      document.body.append(d);
      d.showModal();
      markOpenRow();
    }
    // Keep focus inside the entry: the field being typed in, else the heading.
    const target = (toPane ? el.pane : document.querySelector("dialog.drawer"));
    (target.querySelector("[data-dirty='1'] textarea, [data-dirty='1'] input") || target.querySelector("#drawer-title"))?.focus({ preventScroll: true });
  }


  // ---- an entry across a series: who they are, then one card per book -----------------------------

  /** Is this facet a profile? The view does not say which history items are, so the kind is read from the document of
   *  the facet's own book (its facets, plus the current book's carried entries, which stand in for an origin document that is missing). Safe because it is only asked about facets the view
   *  already shows, whose books are the current one or finished ones; no other book's document is ever opened, and
   *  nothing but the kind of an id already on screen is taken from it. */
  function isProfile(f) {
    if (!s.profileIds) s.profileIds = new Map();
    if (!s.profileIds.has(f.book)) {
      const ids = new Set();
      const d = f.book === s.book ? s.doc : s.seriesDocs.get(f.book);
      for (const en of [...(d?.entities || []), ...(d?.carried || []), ...(s.doc?.carried || [])]) for (const x of en.facets || []) if (x.kind === "profile") ids.add(x.id);
      s.profileIds.set(f.book, ids);
    }
    return s.profileIds.get(f.book).has(f.id);
  }

  /** The cards of an entry that spans books, newest first (the book being read on top), as `{ nodes, count }` (the
   *  quiet cards do not count), or null when the entry is shown as before: not a series, or nothing it shows comes from
   *  another book, or no book has anything to put in a card. Decided from the `book` of the view's own items only. */
  function arcCards(e, evs) {
    if (!s.series) return null;
    const order = s.series.orders[s.order] || [];
    const evIn = (b) => evs.filter((ev) => ev.book === b);
    const present = new Set([...e.facets, ...e.history, ...evs, ...(e.profile ? [e.profile] : [])].map((x) => x.book).filter(Boolean));
    const asleep = dormant(e);
    if (!asleep && ![...present].some((b) => b !== s.book)) return null;
    const rank = (b) => { const i = order.indexOf(b); return i < 0 ? order.length : i; };
    const books = [s.book, ...[...present].filter((b) => b !== s.book).sort((a, b) => rank(b) - rank(a))];
    const real = books.map((b) => buildCard(e, b, evIn(b), order)).filter(Boolean);
    if (!real.length && !asleep) return null;
    const title = s.series.books.find((x) => x.key === s.book)?.title || s.book;
    const quiet = (text) => h("section.book-block.arc-card.current.quiet", { "aria-label": title }, h("div.book-head", h("span.book-title", { title }, text)));
    const nodes = [...real];
    if (asleep) nodes.unshift(quiet(`${bookNumber(s.book, order)}${title}: not in this book yet.`));
    else if (inThisBook(e.id) && !real.some((c) => c.classList.contains("current"))) nodes.unshift(quiet(`${bookNumber(s.book, order)}${title}, so far: appears, nothing new yet.`));
    return { nodes, count: real.length };
  }
  const bookNumber = (b, order) => (order.indexOf(b) >= 0 ? `Book ${order.indexOf(b) + 1}, ` : "");

  /** One book's card, or null when it has nothing to show (the profile under "Who they are" does not count). */
  function buildCard(e, b, bookEvents, order) {
    const now = b === s.book;
    const full = s.series.books.find((x) => x.key === b)?.title || b;
    const heading = `${bookNumber(b, order)}${full}${now ? ", so far" : ""}`;
    // Profiles of this book, in view order. The latest one overall is already shown under "Who they are".
    const profiles = [...e.history.filter((f) => f.book === b && isProfile(f)), ...(e.profile && e.profile.book === b ? [e.profile] : [])];
    const ending = !now && profiles.length && profiles[profiles.length - 1] !== e.profile ? profiles[profiles.length - 1] : null;
    const impressions = [...e.history.filter((f) => f.book === b && f !== ending)];
    const details = e.facets.filter((f) => f.book === b);
    const cardFact = (f) => h("li", f.text, f.basis === "inferred" ? h("span.inferred", { title: "A careful reader would conclude this; the text doesn't say it outright." }, "inferred") : null);
    const list = (xs) => foldList(xs.map(cardFact), (nodes) => h("ul.fact-list", nodes), { show: 6, noun: "details" });

    // The slot for a model-written arc of the book (step 2) goes here, first in the card.
    let desc = null;
    if (ending) {
      const p = h("p.arc-text.clamp", ending.text);
      const more = h("button.linkish.arc-more", { type: "button", "aria-expanded": "false", on: { click: () => {
        const open = p.classList.toggle("clamp") === false;
        more.setAttribute("aria-expanded", String(open)); more.textContent = open ? "Show less" : "Read more"; } } }, "Read more");
      desc = h("div.arc-desc", h("span.eyebrow", `At the end of ${full}`), p, more);
      // The control shows only while the text is actually cut off; the width changes (pane, drawer, rotation), so it is
      // measured again whenever the paragraph is resized, and the watcher goes when the card leaves the page.
      if (typeof ResizeObserver === "function") {
        let seen = false;
        const ro = new ResizeObserver(() => {
          if (!p.isConnected) { if (seen) ro.disconnect(); return; }
          seen = true;
          if (p.classList.contains("clamp")) more.hidden = p.scrollHeight <= p.clientHeight + 1;
        });
        ro.observe(p);
      }
    }

    // Main events: turning points first (up to 4), then the latest ordinary ones; small moments sit behind the fold.
    const kinds = bookEvents.some((ev) => ev.kind);
    const main = bookEvents.filter((ev) => !kinds || ev.kind !== "minor");
    const turns = main.filter((ev) => ev.kind === "turn").slice(-4);
    const shownEv = turns.length >= 4 ? new Set(turns) : new Set([...turns, ...main.filter((ev) => ev.kind !== "turn").slice(-(4 - turns.length))]);
    const rows = bookEvents.map((ev) => eventRow(ev, { chips: false, book: false }));
    const eventsNode = bookEvents.length ? eventFold(rows, bookEvents.map((ev) => shownEv.has(ev)), `Show all ${bookEvents.length} events in ${full}`) : null;

    if (!desc && !bookEvents.length && !details.length && !impressions.length) return null;
    return h(`section.book-block.arc-card.${now ? "current" : "earlier"}`, { "aria-label": full },
      h("div.book-head", h("h3.book-title", { title: full }, heading)),
      desc, eventsNode,
      details.length ? h("details.arc-details", h("summary", `${details.length} detail${details.length === 1 ? "" : "s"} from ${full}`), list(details)) : null,
      impressions.length ? h("details.history", h("summary", `Earlier impressions (${impressions.length})`), list(impressions)) : null);
  }

  /** A list of event rows with some hidden behind one control; with nothing to hide it is just the list. */
  function eventFold(rows, visible, label) {
    const make = (all) => h("ol.timeline", rows.filter((r, i) => all || visible[i]));
    if (visible.every(Boolean) || rows.length <= 5) return make(true);   // one over the limit shows whole, like foldList
    const wrap = h("div.fold");
    const paint = (all) => clear(wrap, visible.some(Boolean) || all ? make(all) : null,
      h("button.linkish.fold-more", { type: "button", "aria-expanded": String(all), on: { click: () => paint(!all) } }, all ? "Show fewer" : label));
    paint(false);
    return wrap;
  }

  function openDrawer(id, { from = null, refresh = false, discard = false, keepFocus = false } = {}) {
    // Opening another entry with unsaved text in the open one asks first.
    if (!refresh && !discard && host() && drawerDirty()) {
      confirmDialog({ title: "Discard what you wrote?", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) openDrawer(id, { from, discard: true, keepFocus }); });
      return;
    }
    const e = nameOf(id);
    // Moved back past where it is revealed: the entry closes (nothing past the position may show).
    if (!e) { if (refresh) closeDrawer({ focus: false }); return; }
    if (!refresh) lastFocus = from || document.activeElement;
    s.drawer = id;
    modeWas = paneMode();
    const v = s.view;
    const rels = v.relations.filter((r) => r.src === id || r.dst === id);
    const evs = v.events.filter((ev) => ev.entities.includes(id));
    const closeBtn = h("button.icon-btn", { type: "button", "aria-label": "Close", title: "Close", on: { click: requestCloseDrawer } }, icon("x"));
    // The panel answers "who is this?" first; the long lists behind it are shown short and open on request.
    const factNode = (f) => h("li", f.text,
      f.basis === "inferred" ? h("span.inferred", { title: "A careful reader would conclude this; the text doesn't say it outright." }, "inferred") : null,
      bookLabel(f.book) && h("span", " ", bookLabel(f.book)));
    const facts = (list, show = 4) => foldList(list.map(factNode), (nodes) => h("ul.fact-list", nodes), { show, noun: "details" });
    const eyebrow = (label, n) => h("span.eyebrow", label, n > 1 ? h("span.n", String(n)) : null);
    const cards = s.edit ? null : arcCards(e, evs);
    const connections = rels.length ? h("div.d-sec", { dataset: { jump: "rels" } }, eyebrow("Connections", rels.length),
      foldList(rels.map(relationRow), (nodes) => h("ul.relations", nodes), { show: 3, noun: "connections" }),
      currentFamilies().some((f) => f.members.includes(id)) ? h("p.d-kin", h("button.linkish", { type: "button", on: { click: () => showFamilyTree(id) } }, "See the family tree")) : null) : null;
    const body = s.edit ? editor.drawerBody(e, rels, evs, { entityChip, bookLabel }) : h("div.body",
      e.profile ? h("div.profile", h("span.eyebrow", e.type === "person" ? "Who they are" : e.type === "group" ? "Who they are" : "What it is"),
        h("p", e.profile.text, e.profile.basis === "inferred" ? h("span.inferred", "inferred") : null),
        cards && e.profile.book ? h("span.book-label", `From ${s.series.books.find((x) => x.key === e.profile.book)?.title || e.profile.book}`) : bookLabel(e.profile.book)) : null,
      e.aliases.length ? h("div.d-sec", h("span.eyebrow", "Also known as"), h("p.aka-line", e.aliases.join(", "))) : null,
      cards ? h("div.d-sec", { dataset: { jump: "books" } }, h("span.eyebrow", "Through the series", cards.count ? h("span.n", String(cards.count)) : null),
        h("div.series-rail.newest.arc-rail", cards.nodes))
        : [h("div.d-sec", { dataset: { jump: "facts" } }, eyebrow("What you know so far", e.facets.length),
          e.facets.length ? facts(e.facets) : h("p.muted", "Nothing beyond the name yet.")),
        e.history.length ? h("details.history", h("summary", `Earlier impressions (${e.history.length})`), facts(e.history, 6)) : null],
      connections,
      cards ? null : evs.length ? h("div.d-sec", { dataset: { jump: "events" } }, eyebrow("Appears in", evs.length),
        foldList(evs.map((ev) => eventRow(ev, { chips: false })), (nodes) => h("ol.timeline", nodes), { show: 3, fromEnd: true, noun: "events" })) : null,
      s.dupes?.writable ? h("div.drawer-foot", h("button.btn.ghost", { type: "button", on: { click: () => {
        closeDrawer({ focus: false });
        mergeWith({ key: s.key, view: s.view, id, onChanged: reloadAfterMerge });
      } } }, icon("layers"), "Same as another entry…")) : null);
    // "Where it comes up": counted by the server from the book text, through the last block the reader has finished.
    // Without the book file there is no section. It sits after the details, before Connections (read-only in edit mode too).
    if (s.map?.cum) {
      const seq = s.p >= 1 - 1e-12 ? s.map.cum.length - 1 : s.map.seqAt(s.p) - 1;
      const sec = mentionsSection(s.key, id, seq, { immediate: !refresh, order: s.series ? s.order : null, fin: s.series ? s.finished : null, rev: revision() });
      const nextSec = body.querySelector('[data-jump="rels"], [data-jump="events"]')
        || [...body.querySelectorAll(".d-sec")].find((d) => /^(Connections|Appears in)/.test(d.querySelector(":scope > .eyebrow")?.textContent || ""))
        || body.querySelector(".drawer-foot");
      if (nextSec) nextSec.before(sec); else body.append(sec);
    }
    const origin = carried(id) ? h("p.d-from", `First met in ${bookShort(revealInfo(id).book)}${inThisBook(id) || cards ? "" : ". Not in this book yet."}`) : null;
    // Jump links under the name, so a long entry (most of them, on a phone) is one tap from each section.
    const jumps = (cards ? [["books", "Books", cards.count], ["rels", "Connections", rels.length]]
      : [["facts", "Details", e.facets.length], ["rels", "Connections", rels.length], ["events", "Events", evs.length]]).filter((j) => j[2]);
    const jumpNav = !s.edit && jumps.length > 1 ? h("nav.d-jumps", { "aria-label": "Sections" }, jumps.map(([k, label, n]) =>
      h("button.linkish", { type: "button", on: { click: () => body.querySelector(`[data-jump="${k}"]`)?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }) } },
        label, h("span.n", String(n))))) : null;
    // A quiet way into the Ask tab with this entry's question typed, not sent.
    const askLink = !preview && ask.available?.available ? h("p.d-ask", h("button.linkish", { type: "button",
      on: { click: () => showAsk({ text: `What do I know about ${e.name} so far?` }) } }, `Ask about ${e.name}`)) : null;
    const title = s.edit ? h("div.d-title", h("h2#drawer-title.sr-only", { tabindex: "-1" }, e.name), editor.drawerTitle(e), origin, askLink)
      : h("div.d-title", typeTag(e.type), h("h2#drawer-title", { tabindex: "-1" }, e.name), origin, jumpNav, askLink);
    const content = [h("header", title, closeBtn), body];
    s.drawerShows = entryIds(e);

    if (paneMode()) {
      document.querySelector("dialog.drawer")?.remove();
      const pane = el.pane;
      const was = !pane.hidden;
      const keep = was && refresh && pane.contains(document.activeElement) && document.activeElement.id ? document.activeElement.id : null;
      const scroll = refresh ? pane.scrollTop : 0;
      pane.className = `entry-pane drawer t-${e.type}`;
      pane.setAttribute("aria-label", e.name);
      pane.hidden = false;
      clear(pane, content);
      pane.scrollTop = scroll;
      el.split.classList.add("has-entry");
      markOpenRow();
      if (keep) document.getElementById(keep)?.focus();
      else if (!refresh) {
        if (!keepFocus) pane.querySelector("#drawer-title")?.focus({ preventScroll: true });
        announce(e.name);
      }
      return;
    }

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
      on: { input: markDirty, change: markDirty, keyup: checkPending, close: onDialogClose, cancel: (ev) => { if (drawerDirty()) { ev.preventDefault(); requestCloseDrawer(); } },
        click: (ev) => { checkPending(); if (ev.target === dlg) requestCloseDrawer(); } } }, content);
    document.body.append(dlg);
    dlg.showModal();
    if (!refresh) closeBtn.focus();
  }

  /** Where the view on screen is computed: what the request sends, and what the thread is compared against. */
  function askPosition() {
    const anchor = s.map.frontier(s.p);
    const order = s.series ? s.series.orders[s.order] : null;
    const finished = s.series ? finishedSet() : null;
    return { anchor, finished, order: s.series ? s.order : null, key: s.key, book: s.book, series: s.series?.id ?? null,
      rank: order ? order.indexOf(s.book) : 0, ord: anchor ? s.doc.anchors.findIndex((a) => a.id === anchor) : -1 };
  }
  // ---- Ask: examples, sources, entry points -----------------------------------------------------

  /** Three questions built only from the gated view: the most known person, a second one, the last chapter read. */
  function askExamples() {
    if (!s.view) return [];
    const people = castEntities().filter((e) => e.type === "person").sort((a, b) => b.facets.length - a.facets.length);
    const out = [];
    if (people[0]) out.push(`Who is ${people[0].name}?`);
    const second = people[1] || people[0];
    if (second) out.push(`What has happened to ${second.name} so far?`);
    const last = chapterGroups().filter((g) => !s.series || !g.book || g.book === s.book).pop();
    if (last) {
      const t = tidyTitle(last.title);
      out.push(`What happened in ${/^[IVXLCDM\d]+\.?$/i.test(t) ? `chapter ${t.replace(/\.$/, "")}` : t}?`);
    }
    return out;
  }
  const entryOfCite = (c) => {
    if (c.entity && nameOf(c.entity)) return c.entity;
    if (c.kind === "relation") { const r = s.view.relations.find((x) => x.id === c.id); return r && nameOf(r.src) ? r.src : null; }
    if (c.kind === "facet") return s.view.entities.find((e) => e.profile?.id === c.id || e.facets.some((f) => f.id === c.id) || e.history.some((f) => f.id === c.id))?.id ?? null;
    if (c.kind === "entity") return nameOf(c.id) ? c.id : null;
    return null;
  };
  /** A source is a link only while the item is in the view on screen. */
  function canOpenCite(c) {
    if (disposed || !s.view) return false;
    if (c.kind === "event") return s.view.events.some((ev) => ev.id === c.id);
    if (c.kind === "chapter") return chapterGroups().some((g) => g.ids.includes(c.id));
    if (c.kind === "recap") return s.view.recap?.id === c.id;   // the recap is replaced as the story goes on: the old one is plain text
    return entryOfCite(c) != null;
  }
  async function openCite(c) {
    if (!canOpenCite(c)) return;
    if (c.kind === "event") {
      s.tab = "timeline"; saveUrl();
      paintTabs();
      let li = el.panel.querySelector(`[data-ev="${CSS.escape(c.id)}"]`);
      if (!li) {   // behind a filter or a fold: show it for this visit, without changing what the person remembered
        const ev = s.view.events.find((x) => x.id === c.id);
        s.tlAll = true; s.tlTurns = false; if (ev?.kind === "minor") s.tlMinor = true;
        paintTabs();
        li = el.panel.querySelector(`[data-ev="${CSS.escape(c.id)}"]`);
      }
      li?.scrollIntoView({ block: "center" });
      li?.focus({ preventScroll: true });
    } else if (c.kind === "chapter") {
      const g = chapterGroups().find((x) => x.ids.includes(c.id));
      if (s.series && g.book && g.book !== s.book) s.chSeries = true;
      s.tab = "chapters"; saveUrl();
      openChapter(g.key);
    } else if (c.kind === "recap") {
      s.tab = "chapters"; saveUrl(); paintTabs();
      const story = el.panel.querySelector(".story-now");
      story?.scrollIntoView({ block: "start" });
    } else openDrawer(entryOfCite(c), { from: document.activeElement });
  }
  /** Open the Ask tab (the palette, the entry's link). A modal entry closes first; typed text in a form is asked about. */
  async function showAsk({ text = null } = {}) {
    if (preview) return;
    if (!(await okToMove("Opening Ask closes the form you were editing."))) return;
    if (!paneMode() && s.drawer) closeDrawer({ focus: false });
    s.tab = "ask"; saveUrl();
    paintTabs();
    if (text != null) ask.prefill(text); else ask.focus();
    el.panel.scrollIntoView?.({ block: "nearest" });
  }

  // ---- command palette ----------------------------------------------------------------------
  const clip = (x, n = 60) => (x.length > n ? `${x.slice(0, n - 1).trimEnd()}…` : x);
  function openChapter(key) {
    s.tab = "chapters";
    const find = () => [...el.panel.querySelectorAll("li.ch-item")].find((d) => d.dataset.chapter === key);
    paintTabs();
    let item = find();
    if (!item) {   // behind "Show all", in a collapsed earlier book or with the earlier books hidden
      if (s.series && !s.chSeries) { s.chSeries = true; try { localStorage.setItem("lk-ch-series", "1"); } catch {} }
      for (const b of [s.series ? s.book : null, ...(s.series?.books || []).map((x) => x.key)]) { s.bkAll.add(bkKey(b)); if (b) s.bkOpen.add(bkKey(b)); }
      paintTabs();
      item = find();
    }
    if (!item) return;
    item.closest("details.book-block")?.setAttribute("open", "");
    item.scrollIntoView({ block: "start" });
    item.tabIndex = -1;
    item.focus({ preventScroll: true });
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
    if (!preview && ask.available?.available) out.push({ id: "reader:ask", label: "Ask about this book", group: "Reading",
      keywords: "ask question who what happened answer", run: () => showAsk() });
    if (currentFamilies().length) out.push({ id: "reader:trees", label: "Family trees", group: "Reading", keywords: "family tree kin relatives genealogy connections", run: () => showFamilyTree() });
    if (canEdit()) out.push({ id: "reader:edit", label: s.edit ? "Stop editing this index" : "Edit this index", group: "Reading",
      keywords: "edit change fix entry", run: () => setEdit(!s.edit) });
    return out;
  }
  // For tests only: with window.__lkTest set before the page loads, the safety net can be exercised by changing the
  // position past every guard (setP directly).
  if (window.__lkTest) window.__lkTest.reader = { setP: (p) => setP(p), state: s };
  let unregister = null;
  import("./palette.js").then((m) => { if (!disposed) unregister = m.registerProvider("reader", paletteItems); }).catch((e) => { console.warn("palette provider", e); });

  return () => {
    disposed = true; ask.dispose(); dropPending(); unregister?.(); clearTimeout(s.autoTimer); clearTimeout(s.dupesTimer); closeDrawer({ focus: false }); clearTimeout(urlTimer); clearInterval(pollTimer);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVisible); window.removeEventListener("focus", onVisible);
  };
}
