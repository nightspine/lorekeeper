import { api } from "./api.js";
import { parseHash, setParams } from "./app.js";
import { readingList, rememberReading } from "./reading.js";
import { announce, clear, describe, tidyTitle, foldList, h, icon, loadError, loading, relTime, sheet, showModalLocked, TYPE_LABEL, TYPE_PLURAL, toast, typeTag } from "./dom.js";
import { view as gate } from "./gating.js";
import { numbersByStart, recapNumbers } from "./chapters.js";
import { nextName, SELF_NAMED_SHORT, stepList, stepName, statedAge, stepState as stepStateAt, tickInfo, whereText as whereAt } from "./steps.js";
import { bookNo, confirmDialog, cover, modal } from "./build.js";
import { mergeWith, openPairs, reviewDuplicates, visiblePairs } from "./dupes.js";
import { renderConnections } from "./graph.js";
import { families } from "./kinship.js";
import { createEditor } from "./editor.js";
import { mentionsSection, forgetMentions } from "./mentions.js";
import { fetchPreview, previewError, previewStrip } from "./community.js";
import { createAsk } from "./ask.js";
import { createReading } from "./read.js";
import { artImg, artOn, lightboxHides, refParts, artStyle, artUrl, ENTITY_KINDS, forgetArt, imageProviders, KIND_OF_TYPE, loadArt, openLightbox, openPictureSheet, modalToasts, setArtOn, setArtStyle, sheetWorld as worldForSheet, styleLabel, unfinishedSources, visibleArt } from "./art.js";

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

const toastedMerges = new Set();   // merge ids whose phone toast has been shown

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
    tab: !preview && ["ask", "read"].includes(params.get("tab")) ? params.get("tab") : "overview", types: new Set(), q: "", sort: "first", newestFirst: true, chaptersNewest: true, prevIds: null, prevP: null, view: null, drawer: null, bkAll: new Set(), bkOpen: new Set(),
    sync: { on: false, override: false }, graph: { sel: null, mode: "map", hops: 1, together: false, things: false, hideMinor: false, history: false, list: false, q: "" }, reveals: null,
    edit: params.get("edit") === "1", hash: null,
    earlier: (() => { try { return localStorage.getItem("lk-earlier") === "1"; } catch { return false; } })(),
    // Timeline filters, remembered per browser: only turning points; show small moments too.
    tlTurns: (() => { try { return localStorage.getItem("lk-tl-turns") === "1"; } catch { return false; } })(),
    tlMinor: (() => { try { return localStorage.getItem("lk-tl-minor") === "1"; } catch { return false; } })(),
    // Chapters tab in a series: show the earlier books' chapters too (as their own blocks), or this book only.
    chSeries: (() => { try { return localStorage.getItem("lk-ch-series") !== "0"; } catch { return true; } })(),
    // Pictures (art.js): on/off and the style are remembered per viewer; `sets` holds each book's art set in that style.
    art: { on: artOn(), style: artStyle(), styles: [], sets: new Map(), canDraw: false, rev: 0, memo: null },
    bookKeys: new Map(),
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

  // Read: the book's own text, the position following the scroll (read.js). It is handed the Reader's state and moves.
  const canRead = () => !preview && Boolean(s.map?.exact && s.hash);
  const reading = createReading({
    key: () => s.key, map: () => s.map, view: () => s.view, p: () => s.p, moveTo: (p) => moveTo(p), settle: (p) => setP(p, { quiet: true }), line: () => aheadLine(),
    doc: () => s.doc, series: () => Boolean(s.series),
    priorDocs: () => (s.series ? (finishedSet() || []).map((b) => s.seriesDocs.get(b)).filter(Boolean) : []),
    entity: (id) => nameOf(id) || null,
    portrait: (id) => { const p = latestPicture(id); return p ? picSrc(p, true) : null; },
    plates: () => visiblePictures().filter((p) => p.rec.kind === "plate" && (!s.series || p.book === s.book)),
    platesNode: (pics, title) => platesNode(pics, title),
    openEntry: (id, from) => openDrawer(id, { from }),
    canAsk: () => !preview && Boolean(ask.available?.available), ask: (text) => showAsk({ text }),
    jump: () => openJump(),
  });

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
      api.catalogShared().then((c) => { s.catalog = c; if (el.headLine) paintHead(); if (s.view && !s.edit && ["overview", "chapters", "timeline"].includes(s.tab)) paintTabs(); }).catch(() => {});
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
    api.catalogShared().then((c) => { s.catalog = c; if (el.headLine) paintHead(); if (s.view && !s.edit && ["overview", "chapters", "timeline"].includes(s.tab)) paintTabs(); }).catch(() => {});
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
    // Book Orbit's answer is slow and needs nothing from the index: ask now so it overlaps everything below.
    const posReq = preview ? null : api.position(key).catch(() => ({ synced: false, reason: "unreachable" }));
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
    s.bookKeys = new Map([[bkKey(preview ? null : s.doc.series?.book || null), key]]);
    s.art.sets = new Map(); s.art.styles = []; s.art.memo = null; s.art.canDraw = false; s.art.loadedBooks = null;
    const sid = preview ? null : s.doc.series?.id;
    // The other books of the series and the edition do not depend on each other: fetch them together.
    const man = sid ? s.lib.series.find((x) => x.id === sid) : null;
    const siblingReqs = [];
    if (man) {
      s.series = man;
      const siblings = s.lib.indexes.filter((i) => i.series?.id === sid);
      for (const b of man.books) {
        const cands = siblings.filter((i) => i.series.book === b.key);
        const choice = b.key === s.doc.series.book ? meta : cands.find((c) => c.source === meta.source) || cands[0];
        if (!choice) continue;
        s.bookKeys.set(b.key, choice.key);
        siblingReqs.push([b.key, choice.key === key ? Promise.resolve(s.doc) : api.index(choice.key).catch(() => null)]);
      }
    }
    // seq hints are only valid for the edition the index was built from.
    const hashes = (s.doc.editions || []).filter((e) => e.builtFrom).map((e) => e.textHash);
    const findEdition = async () => {
      for (const hsh of hashes) { const ed = await api.edition(hsh); if (ed) return { ed, hsh }; }
      return { ed: null, hsh: null };
    };
    const [sibDocs, found] = await Promise.all([Promise.all(siblingReqs.map(([, p]) => p)), findEdition()]);
    if (stale()) return false;
    // Not preview: the duplicates check is slow on a big series and only paints a notice, so it never blocks the page;
    // it starts once the edition has settled so the two do not compete for the server's CPU on a cold start.
    const dupReq = preview ? null : api.duplicates(key).catch(() => null);
    siblingReqs.forEach(([bk], i) => s.seriesDocs.set(bk, sibDocs[i]));
    if (man && (!s.order || !man.orders[s.order])) s.order = man.defaultOrder;
    s.book = book || s.doc.series?.book || null;
    const edition = found.ed;
    s.hash = found.hsh;
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
    s.dupes = null;   // writableNow() uses the library entry until the reply comes
    s.editAsked = s.edit;   // render() drops edit mode when the book cannot be edited; remember it was asked for
    clearTimeout(s.dupesTimer);
    s.graph.pos = null; s.graph.sel = null; s.graph.userView = false; s.graph.arch = null;
    ask.check();   // another index: nothing asked about the last one stays
    reading.reset();   // and nothing of the last one's text
    render();
    if (!preview) loadPictures(stale);   // pictures are an extra: they arrive after the page and repaint what shows them
    if (!preview) loadToc(key, stale);
    saveUrl();   // also puts the book at the top of the rail's Reading list (never a preview)
    if (dupReq) dupReq.then((d) => {
      if (stale() || !d) return;
      const was = canEdit();
      s.dupes = d;
      watchDupes(key);
      syncEditAvailability(was);
      repaintDupes();   // only the notice, and only when it differs: the tabs (and a search being typed in) stay as they are
    });
    if (!preview) await syncPosition(key, { reply: posReq, stale });
    return !stale();
  }

  // ---- Book Orbit position sync ------------------------------------------------------------

  /** Convert a position response to a 0..1 reading position (the block being read counts as unread). */
  function syncedP(r) {
    if (r.finished === true) return 1;   // finished in Book Orbit: the end, whatever the last progress row says
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

  async function syncPosition(key, { poll = false, reply = null, stale = () => false } = {}) {
    if (preview) return;
    let r;
    try { r = await (reply || api.position(key)); } catch { r = { synced: false, reason: "unreachable" }; }
    if (disposed || s.key !== key || stale()) return;   // a reply for a load that has been replaced never touches s.sync or pendingMove
    if (!r || !r.synced) {
      if (!poll || !s.sync.on) { s.sync = { on: false, override: false, reason: r?.reason }; paintSource(); }
      return;
    }
    const p = syncedP(r);
    if (p == null) return;
    const first = !s.sync.on;
    const finished = r.finished === true;
    if (!first && s.sync.updatedAt === r.updatedAt && s.sync.finished === finished) { s.sync.fetchedAt = Date.now(); return; }
    const wasFinished = s.sync.finished === true;
    const override = s.sync.override === true, ahead = s.sync.ahead === true;
    s.sync = { on: true, override, ahead, p, finished, updatedAt: r.updatedAt, basis: r.basis, percentage: finished ? 100 : r.percentage, fetchedAt: Date.now() };
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
      if (!first && !(finished && wasFinished)) announce(finished ? "Finished in Book Orbit" : "Position updated from Book Orbit");
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
    return y.on && !y.finished && !y.ahead && (y.percentage == null || y.percentage < 99.5) ? y.p : null;
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
    else if (y.on && y.finished) {
      clear(el.source, h("span.dot-live", { "aria-hidden": "true" }), "Finished in Book Orbit");
    } else if (y.on) {
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
      if (s.tab === "ask" || s.tab === "read") q.tab = s.tab;
      setParams(q);
      // The rail's "Reading" list: the title and the position the person set, nothing from the index.
      const back = { ...q };
      delete back.edit;   // reopening from the rail reads; it does not reopen edit mode
      if (back.tab !== "read") delete back.tab;   // reopening from the rail goes back to the text when it was being read
      rememberReading({ key: s.key, title: s.doc.work.title, p: s.p, chapter: s.map.chapterAt(s.p) ? tidyTitle(s.map.chapterAt(s.p).title) : null,
        hash: `#/reader?${new URLSearchParams(back)}` });
    }, 250);
  }

  // ---- phone: the ruler's Jump sheet, the overflow menu and the series line ----------------------

  const phone = () => matchMedia("(max-width: 640px)").matches;
  /** Two small icons the shared set lacks (a chevron down, three dots), built as SVG nodes. */
  function glyph(d) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "svg-icon"); svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    for (const part of d) { const n = document.createElementNS(ns, part.startsWith("c") ? "circle" : "path"); if (part.startsWith("c")) { const [cx, cy] = part.slice(1).split(","); n.setAttribute("cx", cx); n.setAttribute("cy", cy); n.setAttribute("r", "1.6"); n.setAttribute("fill", "currentColor"); } else n.setAttribute("d", part); svg.append(n); }
    return svg;
  }
  const chevronDown = () => glyph(["m6 9 6 6 6-6"]);
  const dots = () => glyph(["c5,12", "c12,12", "c19,12"]);
  /** The chapter the ruler names: a span beside the track on desktop, the Jump button's text on a phone. */
  function setChap(text) {
    el.chap.textContent = text;
    if (el.chapText) el.chapText.textContent = text;
  }
  let top = null;   // the one Reader sheet that is open (Jump, More, series)
  function openSheet(opts) {
    top = sheet({ ...opts, onClose: () => { top = null; opts.onClose?.(); } });
    return top;
  }
  function closeTop() { top?.close(true); }

  /** Jump to a chapter or a percentage. Every move goes through moveTo, which asks before dropping an edit form. */
  function openJump() {
    const go = (p) => { closeTop(); moveTo(p); };
    const pct = h("input.input.jump-pct", { type: "number", inputmode: "numeric", min: "0", max: "100", step: "1", value: String(Math.round(s.p * 100)), "aria-label": "Percent of the book" });
    const submit = () => go(Math.min(100, Math.max(0, +pct.value || 0)) / 100);
    pct.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); submit(); } });
    const here = s.map.exact ? s.map.chapterAt(s.p) : null;
    const rows = s.map.exact ? s.map.chapters.map((c, i) => {
      const next = s.map.chapters[i + 1]?.start;
      const nowHere = here && here.start === c.start;
      return h(`li.jump-row${nowHere ? ".now" : ""}`,
        h("button.jump-go", { type: "button", "aria-current": nowHere ? "true" : null, on: { click: () => go(s.map.blockStart(c.start)) } }, h("span.jump-name", tidyTitle(c.title))),
        h("button.jump-end", { type: "button", "aria-label": `End of ${tidyTitle(c.title)}`, on: { click: () => go(next != null ? s.map.blockStart(next) : 1) } }, "End"));
    }) : [];
    const list = rows.length ? h("ol.jump-list", rows) : h("p.hint", "Chapter marks need the book file in the library.");
    const sh = openSheet({ title: "Jump to", focus: pct,
      content: [h("div.jump-field", h("label.label", { for: "jump-pct" }, "Percent of the book"), h("div.jump-pct-row", pct, h("button.btn.primary", { type: "button", on: { click: submit } }, "Go"))), list] });
    pct.id = "jump-pct";
    queueMicrotask(() => sh.el.querySelector(".jump-row.now .jump-go")?.scrollIntoView({ block: "center" }));
    return sh;
  }

  /** The overflow: Edit and Switch book, which have no room on the phone's ruler. */
  function openMore() {
    const items = [];
    if (canEdit()) items.push(h("button.more-item", { type: "button", on: { click: () => { closeTop(); setEdit(!s.edit); } } }, icon(s.edit ? "check" : "pen"), s.edit ? "Done editing" : "Edit this index"));
    if (!preview) items.push(h("button.more-item", { type: "button", on: { click: () => { closeTop(); pickBook(); } } }, icon("layers"), "Switch book"));
    if (!preview && s.art.styles.length) {
      items.push(h("button.more-item", { type: "button", "aria-pressed": String(s.art.on), on: { click: () => { closeTop(); setPicturesOn(!s.art.on); } } },
        icon("eye"), s.art.on ? "Hide the pictures" : "Show the pictures"));
      if (s.art.styles.length > 1 && s.art.on) items.push(h("label.more-item.more-select", h("span", "Picture style"),
        h("select.select", { on: { change: (e) => { closeTop(); setPictureStyle(e.target.value); } } },
          s.art.styles.map((x) => h("option", { value: x.slug, selected: x.slug === s.art.slug }, styleLabel(x.slug, x.text))))));
    }
    openSheet({ title: s.doc.work.title, content: h("div.more-list", items) });
  }

  // ---- layout ----------------------------------------------------------------------------

  const el = {};
  function render() {
    // One compact head row: the title, one quiet line (author, series), Edit and Switch book.
    el.headLine = h("p.rd-by");
    el.headFacts = h("span.rd-cover");
    const head = h("header.page-head.rd-head",
      h("div.rd-title", h("h1", s.doc.work.title), el.headLine),
      el.headActions = h("div.reader-switch", el.artCtl = h("span.art-ctl"), editButton(), preview ? null : h("button.btn.quiet.switch-book", { type: "button", "aria-label": "Switch book", title: "Switch book", on: { click: pickBook } }, icon("layers"), h("span.btn-lbl", "Switch book"))));
    paintHead();
    wrap.classList.toggle("editing", s.edit);

    el.pctNum = document.createTextNode("");
    el.pct = h("span.pct.num", el.pctNum, h("span.pct-unit", "%"));
    el.chap = h("span.chap");
    // On a phone the chapter name is the way into the Jump sheet (chapter list and percent field); the span stays for desktop.
    el.chapText = h("span.jt-text");
    el.jumpBtn = h("button.jump-btn", { type: "button", "aria-haspopup": "dialog", "aria-label": "Jump to a chapter or a percentage", on: { click: openJump } },
      el.chapText, chevronDown());
    el.moreBtn = h("button.icon-btn.rbtn.more-btn", { type: "button", "aria-haspopup": "dialog", "aria-label": "More", title: "More", on: { click: openMore } }, dots());
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
      setChap(c ? tidyTitle(c.title) : "Before the first chapter");
      el.chap.classList.add("hovering");
    });
    el.range.addEventListener("pointerleave", () => {
      if (!el.chap.classList.contains("hovering")) return;
      el.chap.classList.remove("hovering");
      const ch = s.map.chapterAt(s.p);
      setChap(ch ? tidyTitle(ch.title) : el.chap.title);
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
      h("div.step-btns", prev, next), el.pct, el.chap, el.jumpBtn, el.track, el.jump);
    // The chapter steps: one tick per chapter and "I finished Chapter N", the main way to move for a book read elsewhere.
    el.stepTrack = h("div.step-track");
    el.stepWhere = h("span.step-where");
    el.finishBtn = h("button.btn.primary.small.step-finish", { type: "button", on: { click: () => { const c = stepState().next; if (c) moveTo(c.to); } } });
    el.fineBtn = h("button.btn.quiet.small.step-fine", { type: "button", "aria-expanded": "false", on: { click: () => {
      const on = !wrap.classList.contains("fine");
      wrap.classList.toggle("fine", on);
      el.fineBtn.setAttribute("aria-expanded", String(on));
      if (on) el.range.focus({ preventScroll: true });
    } } }, icon("sliders"), "Fine position");
    el.steps = h("section.steps", { "aria-label": "Chapters you have finished" }, el.stepWhere, el.stepTrack,
      h("div.step-acts", el.finishBtn, h("button.btn.quiet.small.step-part", { type: "button", "aria-haspopup": "dialog", on: { click: openJump } }, "Part way"), el.fineBtn));
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

    // The overflow (Edit, Switch book) lives in the phone app bar's slot; the slot is hidden beside the rail and emptied on every route change.
    // A read-only community preview has neither Edit nor Switch book: no button then.
    document.getElementById("appbar-slot")?.replaceChildren(...(canEdit() || !preview ? [el.moreBtn] : []));
    el.stepTicks = null;
    clear(wrap, head, preview ? previewStrip(preview.info) : null, el.position, el.steps, el.sub, el.series, el.split);
    paintTicks();
    paintChapters();
    paint();
    paintArtControl();
  }

  /** The catalog book this index belongs to (via its versions), once the catalog has loaded. */
  const catalogBook = () => (preview ? s.catalog?.books.find((b) => b.key === preview.catalog)
    : s.catalog?.books.find((b) => b.versions.some((v) => v.key === s.key))) || null;

  // ---- edit mode ---------------------------------------------------------------------------
  // Only an index built here can be changed, and only with its book file on disk (reveal points are
  // chosen by paragraph). Turning it on adds pen and trash controls to every row and "Add" buttons.
  // `writable` comes with the library entry (the server's rule for the book's series group) so the first render
  // already knows; the duplicates reply, when it arrives, has the final word.
  const writableNow = () => (s.dupes ? s.dupes.writable === true : s.lib.indexes.find((i) => i.key === s.key)?.writable === true);
  const canEdit = () => Boolean(writableNow() && s.map?.exact && s.hash);
  /** The duplicates reply may disagree with the library entry: bring the Edit control, edit mode and the open
   *  entry's merge footer in line. Nothing here rebuilds the tabs. */
  function syncEditAvailability(was) {
    if (was !== canEdit() && el.headActions) {
      const old = el.headActions.querySelector(".edit-toggle");
      if (canEdit()) {
        const b = editButton(); if (b && !old) el.headActions.prepend(b);
        if (s.editAsked && !s.edit) setEdit(true);   // the address asked for edit mode and the first answer said no
      } else {
        old?.remove();
        if (s.edit) {
          // Typed text is not dropped silently: the entry closes through the usual refresh, with a toast saying why.
          const dirty = drawerDirty();
          s.edit = false; wrap.classList.remove("editing"); paintTabs();
          // A dirty form closes the entry the way Close does (focus back to its row); a clean one refreshes into view mode.
          if (s.drawer) { if (dirty) closeDrawer(); else openDrawer(s.drawer, { refresh: true }); }
          saveUrl();
          if (dirty) toast("Editing is not available for this book, so the entry was closed.", "error");
        }
      }
    }
    syncFooter();
  }
  /** The open entry's "Same as another entry" footer follows `writable` without rebuilding the entry. */
  function syncFooter() {
    const box = host();
    if (!box || !s.drawer || s.edit) return;
    const foot = box.querySelector(".drawer-foot");
    if (!writableNow()) { foot?.remove(); return; }
    const body = box.querySelector(".body");
    if (!foot && body) body.append(mergeFooter(s.drawer));
  }
  const mergeFooter = (id) => h("div.drawer-foot", h("button.btn.ghost", { type: "button", on: { click: () => {
    closeDrawer({ focus: false });
    mergeWith({ key: s.key, view: s.view, id, onChanged: reloadAfterMerge });
  } } }, icon("layers"), "Same as another entry…"));
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
    s.editAsked = false;
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

  // ---- chapter steps: one tick per chapter and "I finished Chapter N" ---------------------------
  // For a book read elsewhere the person says which chapter they finished; the slider stays for anything finer. Every
  // move goes through moveTo (the edit-form question and the Book Orbit line). Past the position a tick names nothing.

  /** The table of contents from the read route (same numbering as the chapter map); without it the chapter map is used. */
  async function loadToc(key, stale) {
    if (!s.map?.exact) return;
    let list = null;
    try {
      const res = await fetch(`/api/library/${encodeURIComponent(key)}/read/toc`, { headers: { "X-Lorekeeper": "1" } });
      if (!res.ok) return;
      const j = await res.json();
      const raw = Array.isArray(j) ? j : j?.chapters;
      if (!Array.isArray(raw) || !raw.length) return;
      list = raw.filter((c) => c && Number.isInteger(c.n) && Number.isInteger(c.firstSeq) && Number.isInteger(c.lastSeq) && c.lastSeq >= c.firstSeq)
        .map((c) => ({ n: c.n, title: typeof c.title === "string" ? c.title : "", firstSeq: c.firstSeq, lastSeq: c.lastSeq,
          // the book's own chapter number (chapters.py); absent from an older server, which steps.js then works out
          ...(Number.isInteger(c.number) && c.number > 0 ? { number: c.number } : c.number === null ? { number: null } : {}) }));
    } catch { return; }
    if (stale() || !list?.length || s.key !== key) return;
    s.toc = { key, list };
    s.stepMemo = null;
    paintSteps();
    if (s.tab === "overview") paintTabs();
  }

  /** The story chapters as steps (steps.js): entered at the chapter map's start, the key every per-chapter lookup uses. */
  function stepChapters() {
    if (!s.map?.exact) return [];
    const toc = s.toc?.key === s.key ? s.toc.list : null;
    if (s.stepMemo && s.stepMemo.map === s.map && s.stepMemo.toc === toc) return s.stepMemo.list;
    const list = stepList(s.map.chapters, toc, s.map, tidyTitle);
    s.stepMemo = { map: s.map, toc, list };
    return list;
  }
  const stepState = () => stepStateAt(stepChapters(), s.p);
  const whereText = () => whereAt(stepState(), s.p);
  /** The ticks: finished ones lit, the one being read in ember, the rest dark and unnamed. One tab stop; arrows move. */
  function stepTicks(cls = "") {
    const list = stepChapters();
    const ol = h(`ol.step-ticks${cls}`, { "aria-label": "Chapters", on: { keydown: (ev) => {
      const bs = [...ol.querySelectorAll(".step-tick")];
      const i = bs.indexOf(ev.target);
      if (i < 0) return;
      const to = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: bs.length - 1 }[ev.key];
      if (to == null || !bs[to]) return;
      ev.preventDefault();
      bs[i].tabIndex = -1; bs[to].tabIndex = 0; bs[to].focus();
    } } }, list.map((c) => h("li", h("button.step-tick", { type: "button", tabindex: "-1", on: { click: () => moveTo(c.to) } }))));
    markTicks(ol);
    return ol;
  }
  function markTicks(ol) {
    const st = stepState(), { list, next } = st;
    const bs = ol.querySelectorAll(".step-tick");
    const focusAt = list.indexOf(next || list[list.length - 1]);
    const keep = ol.contains(document.activeElement);
    bs.forEach((b, i) => {
      const c = list[i];
      if (!c) return;
      if (!keep) b.tabIndex = i === focusAt ? 0 : -1;
      const t = tickInfo(c, st, s.p);
      if (b.dataset.state === t.state) return;
      b.dataset.state = t.state;
      b.title = t.title;
      b.setAttribute("aria-label", t.label);
    });
  }
  /** The steps row under the ruler, painted on every position change without rebuilding the ticks. */
  function paintSteps() {
    if (!el.steps) return;
    const list = stepChapters();
    el.steps.hidden = !list.length;
    wrap.classList.toggle("has-steps", list.length > 0);
    if (!list.length) return;
    if (!el.stepTicks || el.stepTicks.childElementCount !== list.length) clear(el.stepTrack, el.stepTicks = stepTicks());
    else markTicks(el.stepTicks);
    el.stepWhere.textContent = whereText();
    const { next } = stepState();
    el.finishBtn.hidden = !next;
    if (next) el.finishBtn.textContent = `I finished ${nextName(stepState())}`;
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

  /** The series controls: which book, the reading order, and which earlier books are finished. They sit in a bar on
   *  a wide screen and in a sheet on a phone; each change goes through okToMove. */
  function seriesControls() {
    const order = s.series.orders[s.order];
    const seg = h("div.series-chips", { role: "group", "aria-label": "Book in series" }, order.map((k, i) => {
      const b = s.series.books.find((x) => x.key === k);
      const has = s.seriesDocs.get(k);
      const text = `${i + 1}. ${b?.title || k}`;
      return k === s.book ? h("span.chip", { "aria-current": "true" }, text)
        : h("button.chip", { type: "button", disabled: !has, title: has ? b.title : "No index for this book", on: { click: () => { closeTop(); switchBook(k); } } }, text);
    }));
    const orderSel = Object.keys(s.series.orders).length > 1 && h("select.select", { "aria-label": "Reading order", style: { width: "auto" },
      on: { change: async (e) => {
        const want = e.target.value;
        if (!(await okToMove("Changing the reading order closes the form you were editing."))) { e.target.value = s.order; return; }
        closeTop(); s.order = want; render(); saveUrl();
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
    return { seg, orderSel, boxes };
  }

  function paintSeries() {
    if (!s.series) return clear(el.series);
    if (phone()) {
      // One line: "Book 2 of 7 in Harry Potter", a tap opens the books, the reading order and the Finished boxes.
      const order = s.series.orders[s.order];
      const at = order.indexOf(s.book);
      const name = s.series.title || s.series.name || "this series";
      const btn = h("button.series-line", { type: "button", "aria-haspopup": "dialog",
        on: { click: () => {
          const { seg, orderSel, boxes } = seriesControls();
          openSheet({ title: name, content: h("div.series-sheet", h("p.hint", "Pick the book to read. Earlier books you have finished add what you know from them."),
            seg, orderSel ? h("div.series-order", h("label.label", "Reading order"), orderSel) : null,
            boxes.length ? h("div.series-fin", h("p.label", "Earlier books you have finished"), boxes) : null) });
        } } },
        h("span.sl-text", at >= 0 ? `Book ${at + 1} of ${order.length} in ${name}` : `In the series ${name}`), chevronDown());
      return clear(el.series, btn);
    }
    const { seg, orderSel, boxes } = seriesControls();
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
    el.hint.textContent = el.hint.hidden ? "" : "Tap the last chapter you finished, or drag the slider to where you are. Everything below updates to what you've read, and nothing further.";
    const ch = s.map.chapterAt(s.p);
    setChap(ch ? tidyTitle(ch.title) : anchor ? `after paragraph ${anchor}` : "before the first reveal");
    el.chap.title = el.chap.textContent;
    paintSource();
    paintLastSetting();
    paintSteps();

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
    // Pictures: a book ticked or unticked as finished changes which art sets may show; an open lightbox that could
    // step to a picture the position no longer reveals closes (the spoiler rule outranks it), as does a picture sheet.
    const books = pictureBooks().map(bkKey).join(",");
    if (!preview && s.art.loadedBooks != null && s.art.loadedBooks !== books) loadPictures();
    if (s.art.lightbox) {
      const shown = new Set(visiblePictures().map((p) => p.rec.id));
      // each picture by its id now or the one it opened with (a fix files a new id before the art set is fetched again)
      if (lightboxHides(s.art.lightbox.ids(), shown)) { s.art.lightbox.close(); s.art.lightbox = null; }
    }
    if (s.art.sheet && !sheetVisible(s.art.sheet)) { const o = s.art.sheet.opened; s.art.sheet = null; o.then((sh) => sh?.close(true)); }
    else if (s.art.sheet?.sh && !sheetTextVisible(s.art.sheet.sh)) {
      // The moments are still revealed but the prompt or the World line was written from further on: drop both and write
      // the prompt again at the position now (the server refuses it if the picture itself is held past here).
      s.art.sheet.sh.reposition(sheetWorld(s.art.sets.get(bkKey(s.series ? s.book : null))));
    }
    // Names and thumbnails on an open lightbox or sheet follow the view now, not the one they were painted from.
    s.art.lightbox?.refresh?.();
    s.art.sheet?.sh?.refreshFigures?.();
    s.art.sheet?.sh?.moved?.();   // "Through the book so far" counts again up to the position now
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
    wrap.classList.toggle("asking-tab", s.tab === "ask");
    wrap.classList.toggle("on-overview", s.tab === "overview");   // the Overview's card carries the steps
    // The Overview's "All N people" opens Cast filtered for that one visit: leaving Cast puts the person's own filter back.
    if (s.tab !== "cast" && s.typesRestore) { s.types = s.typesRestore; s.typesRestore = null; }
    if (s.tab === "read" && !canRead()) s.tab = "overview";
    wrap.classList.toggle("reading-tab", s.tab === "read");
    if (s.tab !== "read") reading.leave();
    // The short names are for a phone, where all four tabs share one row without scrolling sideways.
    const tabs = [["overview", "Overview", null, "Home"], ...(canRead() ? [["read", "Read", null, "Read"]] : []), ["chapters", "Chapters", chapterGroups().length, "Chapters"], ["cast", "Cast & World", castEntities().length, "Cast"],
      ["timeline", "Timeline", v.events.length, "Timeline"], ["links", "Connections", v.relations.length, "Map"]];
    // Ask needs a library key (not a community preview) and a model; opened by address it explains instead of hiding.
    const askInfo = ask.available;
    if (!preview && (s.tab === "ask" || (askInfo && (askInfo.available || askInfo.reason === "ollama-busy")))) tabs.push(["ask", "Ask", null, "Ask"]);
    const keys = tabs.map((x) => x[0]);
    el.tabs.classList.toggle("five", tabs.length > 4);   // a phone has no room for the counts beside five names
    const select = (k, focus) => {
      const was = s.tab;
      s.tab = k; paintTabs();
      if (k === "ask" || was === "ask" || k === "read" || was === "read") saveUrl();
      if (focus) document.getElementById(`tab-${k}`)?.focus();
    };
    clear(el.tabs, tabs.map(([k, label, n, short]) => h("button", { type: "button", role: "tab", id: `tab-${k}`, "aria-controls": "panel-view",
      "aria-selected": String(s.tab === k), tabindex: s.tab === k ? "0" : "-1",
      on: { click: () => select(k, false), keydown: (ev) => {
        const i = keys.indexOf(k);
        const to = { ArrowRight: keys[(i + 1) % keys.length], ArrowLeft: keys[(i + keys.length - 1) % keys.length], Home: keys[0], End: keys[keys.length - 1] }[ev.key];
        if (to) { ev.preventDefault(); select(to, true); }
      } } }, h("span.tab-full", label), h("span.tab-short", { "aria-hidden": "true" }, short), n == null ? null : h("span.count", String(n)))));
    // A row that scrolls sideways (a phone, large text) keeps the selected tab in view, without moving the page.
    const sel = el.tabs.querySelector('[aria-selected="true"]');
    if (sel && el.tabs.scrollWidth > el.tabs.clientWidth) {
      const left = sel.getBoundingClientRect().left - el.tabs.getBoundingClientRect().left + el.tabs.scrollLeft, right = left + sel.offsetWidth;
      if (left < el.tabs.scrollLeft) el.tabs.scrollLeft = left - 12;
      else if (right > el.tabs.scrollLeft + el.tabs.clientWidth) el.tabs.scrollLeft = right - el.tabs.clientWidth + 12;
    }
    el.panel.setAttribute("aria-labelledby", `tab-${s.tab}`);
    if (s.tab !== "links") { clear(el.mapSide); el.split.classList.remove("has-side"); }
    ({ overview: paintOverview, chapters: paintChapterRecaps, cast: paintCast, timeline: paintTimeline, links: paintLinks, ask: () => ask.mount(el.panel), read: () => reading.mount(el.panel) })[s.tab]();
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
      on: { click: (e) => { s.typesRestore = null; s.types.has(t) ? s.types.delete(t) : s.types.add(t); e.currentTarget.setAttribute("aria-pressed", String(s.types.has(t))); paintList(); } } },
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
    clear(el.panel, h("div.filters", h("div.search", icon("search"), search), h("div.type-chips", { role: "group", "aria-label": "Show types" }, chips, earlierChip), sort), el.dupesSlot = h("div.dupes-slot", dupesNotice()),
      s.edit ? editor.addEntityButton() : null, list);
    paintList();
    el.dupesSlot.dataset.key = dupesKey();
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
      if (next.checking) watchDupes(key); else { syncEditAvailability(canEdit()); repaintDupes(); }
    }, 3000);
  }

  /** What dupesNotice() would show, as a string: the slot is rebuilt only when this changes. */
  function dupesKey() {
    const auto = s.dupes?.undo?.auto && s.autoShown !== s.dupes.undo.id ? s.dupes.undo.id : "";
    const n = s.dupes?.writable && !s.dupesDismissed ? openPairs(visiblePairs(s.dupes, s.view)).length : 0;
    return `${auto}|${n}`;
  }
  function repaintDupes() {
    if (!el.dupesSlot?.isConnected || el.dupesSlot.dataset.key === dupesKey()) return;
    clear(el.dupesSlot, dupesNotice());
    el.dupesSlot.dataset.key = dupesKey();
  }
  /** The phone's "Merged N obvious duplicates" toast with Undo: in the open entry sheet's own host while that modal
   *  is up (the page behind it is inert), gone after about 10 seconds or when the Reader is left. If the sheet closes
   *  first the toast moves to the page for the time it has left; if the Reader is left while it is still showing, the
   *  merge is forgotten so the toast comes back on return. */
  function showMergeToast(auto, remaining = 10000) {
    const n = auto.keepNames.length;
    const dlg = document.querySelector("dialog.drawer[open]");
    let host = null;
    if (dlg) { host = dlg.querySelector(".drawer-toasts"); if (!host) { host = h("div.drawer-toasts"); dlg.append(host); } }
    s.dropMergeToast?.();
    const dismiss = toast(`Merged ${n} obvious duplicate${n === 1 ? "" : "s"}.`, "ok", { host, action: { label: "Undo", run: async () => {
      try { await api.undoMerge(s.key, auto.id); toast("Undone"); reloadAfterMerge(); } catch (e) { toast(e.message, "error"); }
    } } });
    const node = (host || document.getElementById("toasts")).lastElementChild;
    const timer = setTimeout(() => { dismiss(); s.mergeToast = null; }, remaining);
    s.mergeToast = { auto, host, node, until: Date.now() + remaining };
    s.dropMergeToast = () => { clearTimeout(timer); dismiss(); s.mergeToast = null; s.dropMergeToast = null; };
  }
  /** The entry sheet opened or closed: a merge toast still showing moves to where it can be reached (into the open
   *  sheet, or back to the page), keeping the time it has left. */
  function rehomeMergeToast() {
    const m = s.mergeToast;
    if (!m || disposed) return;
    const wantSheet = Boolean(document.querySelector("dialog.drawer[open]"));
    if (wantSheet === Boolean(m.host)) return;
    const left = m.until - Date.now();
    const kept = m.host ? m.host.contains(m.node) : m.node.isConnected;   // false once the person dismissed it
    s.dropMergeToast?.();
    if (kept && left > 500) showMergeToast(m.auto, left);
  }

  function dupesNotice() {
    const auto = s.dupes?.undo?.auto && s.autoShown !== s.dupes.undo.id ? s.dupes.undo : null;
    let merged = null;
    if (auto && phone()) {
      // On a phone the merge is a toast with Undo, never a line in the middle of the list.
      // Once per merge (autoShown), gone after about 10 seconds, and gone when the Reader is left.
      s.autoShown = auto.id;
      if (!toastedMerges.has(auto.id)) { toastedMerges.add(auto.id); showMergeToast(auto); }   // once per merge in this visit
    } else if (auto) {
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
    // A visible portrait takes the type mark's place (the type colour stays as a ring round it).
    const face = latestPicture(e.id);
    const faceImg = face ? h("span.ent-face", artImg(picSrc(face, true), "", "ent-face-img")) : null;
    const rowEl = h(`button.ent-row.t-${e.type}${fresh ? ".fresh.arrive" : ""}${sleeping ? ".dormant" : ""}${open ? ".sel" : ""}${faceImg ? ".has-face" : ""}`, { type: "button",
      dataset: { ent: e.id }, "aria-current": open ? "true" : null,
      "aria-label": `${e.name}, ${TYPE_LABEL[e.type]}${fresh ? ", new" : ""}${from ? `, from ${bookShort(from)}${sleeping ? ", not in this book yet" : ""}` : ""}`,
      on: { click: (ev) => openDrawer(e.id, { from: ev.currentTarget }) } },
      h(`span.ent-dot${fresh ? ".fresh" : ""}`, { "aria-hidden": "true", title: fresh ? "New" : null }),
      h("span.ent-name", h("span.nm", faceImg, e.name),
        e.aliases.length ? h("span.aka", e.aliases.slice(0, 3).join(", ")) : null),
      h("span.ent-fact", about || h("span.subtle", "Named, but nothing more is known yet.")),
      h("span.ent-meta",
        from ? h("span.book-label.ent-from", { title: sleeping ? "Met in an earlier book; not in this one yet" : "Met in an earlier book" }, `from ${bookShort(from)}`) : null,
        e.facets.length ? [h("span.num", String(e.facets.length)), e.facets.length === 1 ? " detail" : " details"] : null,
        impressionsOf(e).length ? h("span.hist", { title: `${impressionsOf(e).length} earlier impression${impressionsOf(e).length === 1 ? "" : "s"}` }, icon("refresh")) : null));
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

  /** One book on the rail. `items` ([{ label, title, body, cls, id, badge, action, pin }]) are in display order: the fold keeps the first `NEAR`
   *  and any `pin`ned item. `newest` only picks the word in the expanded button; `raw` is an earlier book's events block ({ total, build(full) }). */
  function bookBlock(key, items, { current, newest, noun = "chapter", repaint, none, raw }) {
    const all = s.bkAll.has(bkKey(key));
    const folded = !all && items.length > NEAR;
    const shown = !folded ? items : items.filter((it, i) => i < NEAR || it.pin);
    const rail = items.length ? h("ol.ch-rail", shown.map((it) => h(`li.ch-item${it.cls ? `.${it.cls}` : ""}`, { "data-chapter": it.id || null },
      it.label ? h("span.ch-label", it.label) : null,
      it.title ? h("div.ch-name", it.title, it.badge || null, it.action || null) : null,
      it.body))) : none ? h("p.muted.book-none", none) : null;
    const total = raw ? raw.total : items.length;
    const toggle = total > NEAR ? h("button.btn.ch-all", { type: "button", dataset: { book: bkKey(key) }, "aria-expanded": String(all),
      on: { click: () => {
        all ? s.bkAll.delete(bkKey(key)) : s.bkAll.add(bkKey(key));
        repaint();
        el.panel.querySelector(`.ch-all[data-book="${CSS.escape(bkKey(key))}"]`)?.focus({ preventScroll: true });
      } } }, all ? `Show only the ${newest ? "latest" : "first"} ${NEAR}` : `Show all ${total} ${noun}${total === 1 ? "" : "s"}`) : null;
    const content = raw ? raw.build(all) : rail;
    if (current) return h("section.book-block.stack-book.current", { "aria-label": bookTitleOf(key) }, bookBanner(key), content, toggle);
    return h("details.book-block.stack-book.earlier", { "aria-label": bookTitleOf(key), open: s.bkOpen.has(bkKey(key)) ? "" : null,
      on: { toggle: (ev) => { ev.currentTarget.open ? s.bkOpen.add(bkKey(key)) : s.bkOpen.delete(bkKey(key)); } } },
      bookBanner(key, { summary: true }), content, toggle);
  }

  /** "Chapter 7" over a title that does not say what it is, 7 being the book's own number (chapters.js), never the row's
   *  place; no label when the title carries its own number or kind, or the book gives it no number. */
  const SELF_NAMED = /^\s*(chapter|part|book|prologue|epilogue|preface|introduction|foreword|afterword|section|letter)\b/i;
  const chapterLabel = (title, number) => (number == null || SELF_NAMED.test(tidyTitle(title) || "") ? null : `Chapter ${number}`);

  /** The chapter map's numbers by chapter start (chapters.js), worked out once per map. */
  function mapNumbers() {
    if (s.numMemo?.map !== s.map) s.numMemo = { map: s.map, byStart: numbersByStart(s.map?.chapters) };
    return s.numMemo.byStart;
  }
  /** A book's numbers from all its chapter recaps (chapters.js), when there is no chapter map; once per document. The
   *  titles are chapter titles, which name nothing past the position. */
  const recapNumMemo = new WeakMap();
  function recapNumbersOf(book) {
    const d = docOf(book);
    if (!d) return new Map();
    if (!recapNumMemo.has(d)) recapNumMemo.set(d, recapNumbers(d));
    return recapNumMemo.get(d);
  }
  /** The number of a chapter row (currentRows or earlierRows) of `book` (null: the book being read). */
  function rowNumber(r, book) {
    const current = !s.series || book == null || book === s.book;
    if (current && (s.map?.chapters || []).length) return r.ch ? mapNumbers().get(r.ch.start) ?? null : null;
    const ids = r.group?.ids || [];
    const nums = recapNumbersOf(current ? (s.series ? s.book : null) : book);
    for (const id of ids) if (nums.has(id)) return nums.get(id);
    return null;
  }
  const rowLabel = (r, book) => chapterLabel(r.title, rowNumber(r, book));

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
  function chapterItems(rows, book = s.series ? s.book : null) {
    const plates = platesByRow(rows, book);
    const current = !s.series || book === s.book;
    // The chapter just finished says who was met in it: its own row, else the row its recap landed in (a recap is revealed
    // at the first anchor after its chapter's last paragraph, which can be in the next chapter).
    const lastStep = current ? stepState().last : null;
    const justDone = lastStep ? rows.find((r) => r.group && r.ch && r.ch.start === lastStep.seq) || rows.find((r) => r.group && r.now) || null : null;
    return rows.map((r, i) => {
      const g = r.group, parts = g ? g.parts : [];
      const fresh = (g && g.ids.some((id) => s.fresh.has(id))) || false;
      const who = parts.length ? mentions(parts.join(" ")) : [];
      const pics = plates.get(r) || [];
      const draw = current ? chapterDrawButton(r, pics) : null;
      return { id: g ? g.key : null, label: rowLabel(r, book), title: tidyTitle(r.title), cls: r.now ? "now" : fresh ? "fresh" : "",
        badge: r.now ? h("span.chip.ch-ongoing", "So far") : fresh ? h("span.new-badge", "New") : null,
        body: parts.length || pics.length || draw ? h("div.ch-body", platesNode(pics, tidyTitle(r.title)), parts.map((t, k) => {
          if (!s.edit) return h("p", t);
          const part = h("div.ch-part");
          clear(part, h("p", t), editor.recapRowActions({ id: g.ids[k], text: t }, part, { scope: "chapter", title: g.title }));
          return part;
        }), r === justDone ? metLine({ start: lastStep.seq }) : null, draw ? h("div.art-draw-row", draw) : null,
        who.length ? h("div.ch-who", h("span.eyebrow", "In this chapter"), h("div.ents", who.slice(0, 14).map((e) => entityChip(e.id)))) : null) : null };
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
    const earlierBlocks = earlierBooks.map((b) => bookBlock(b, ordered(chapterItems(earlierRowsBy.get(b), b)), { current: false, newest: s.chaptersNewest, repaint }));
    // Newest first puts the book being read at the top and the earlier books beneath it, latest first.
    const blocks = s.chSeries ? (s.chaptersNewest ? [currentBlock, ...earlierBlocks.reverse()] : [...earlierBlocks, currentBlock]) : [currentBlock];
    clear(el.panel, story,
      h("div.toolbar.chapters-bar", h("span.hint", s.chSeries ? `${count} in this book, ${earlierTotal} in earlier books` : count), seriesChip, addRecap, toggle),
      h("div.book-stack", blocks));
  }

  /** People and creatures of the book being read by the map chapter their first name is revealed in: Map(chapter start →
   *  [entity]), from the view at the position only (so every one is revealed), in order of first appearance. */
  function metByChapter() {
    if (s.metMemo?.view === s.view && s.metMemo.map === s.map) return s.metMemo.by;
    const by = new Map();
    if (s.map.exact) {
      for (const e of s.view.entities) {
        if (e.type !== "person" && e.type !== "creature") continue;
        const r = revealInfo(e.id);
        if (!r || (s.series && r.book !== s.book)) continue;
        const at = s.map.anchorPos(r.from);
        const ch = Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
        if (!ch) continue;
        if (!by.has(ch.start)) by.set(ch.start, []);
        by.get(ch.start).push(e);
      }
    }
    s.metMemo = { view: s.view, map: s.map, by };
    return by;
  }
  /** "You met Lucy, Arthur and Quincey." for a chapter, each name opening the entry; null when nobody was met in it. */
  function metLine(ch) {
    const met = metByChapter().get(ch.start) || [];
    if (!met.length) return null;
    const shown = met.slice(0, 6);
    const names = shown.map((e) => h(`button.linkish.met-name.t-${e.type}`, { type: "button", on: { click: (ev) => openDrawer(e.id, { from: ev.currentTarget }) } }, e.name));
    const parts = [];
    names.forEach((n, i) => {
      if (i) parts.push(i === names.length - 1 && met.length === shown.length ? " and " : ", ");
      parts.push(n);
    });
    if (met.length > shown.length) parts.push(` and ${met.length - shown.length} more`);
    return h("p.ch-met", "You met ", parts, ".");
  }

  // ---- Overview: the book's home -----------------------------------------------------------------
  // A header picture with the story so far and where the reader is, then who they have met and the chapters so far.
  // Everything comes from the gated view and visiblePictures(); every count counts what is visible now.

  /** The first one or two sentences of the story so far. */
  function firstSentences(text, n = 2) {
    const parts = String(text || "").match(/[^.!?]+[.!?]+["'’”)]*\s*/g) || [String(text || "")];
    return parts.slice(0, n).join("").trim();
  }
  /** People and creatures most present first: named by the most visible events, then the most details, then first met. */
  function whoIsWho() {
    const ents = castEntities().filter((e) => e.type === "person" || e.type === "creature");
    const n = new Map(ents.map((e) => [e.id, 0]));
    for (const ev of s.view.events) for (const id of ev.entities) if (n.has(id)) n.set(id, n.get(id) + 1);
    return ents.map((e, i) => ({ e, i })).sort((a, b) => n.get(b.e.id) - n.get(a.e.id) || b.e.facets.length - a.e.facets.length || a.i - b.i).map((x) => x.e);
  }
  const initials = (name) => (name.replace(/\(.*?\)/g, "").match(/\p{L}[\p{L}'’-]*/gu) || []).filter((w) => /^\p{Lu}/u.test(w)).slice(0, 2).map((w) => w[0]).join("") || name.trim().slice(0, 1).toUpperCase();
  /** "Book 2 of the Red Rising Saga, by Pierce Brown", or "By Bram Stoker". */
  function overviewByline() {
    const authors = s.doc.work.authors.join(", ");
    const by = authors ? `by ${authors}` : "";
    if (s.series) {
      const order = s.series.orders[s.order] || [];
      const i = order.indexOf(s.book);
      const name = s.series.title || s.series.name || "";
      if (i >= 0 && name) return `Book ${i + 1} of ${/^the\b/i.test(name) ? "" : "the "}${name}${by ? `, ${by}` : ""}`;
    }
    return by ? capFirst(by) : "";
  }
  /** The spoiler card: where the reader is, the chapter ticks, and the way forward. */
  function overviewCard() {
    const list = stepChapters();
    const st = stepState(), { next } = st;
    const finish = next ? h("button.btn.primary.ov-finish", { type: "button", on: { click: () => moveTo(next.to) } }, `I finished ${nextName(st)}`) : null;
    const jump = h("button.btn.quiet.ov-jump", { type: "button", "aria-haspopup": "dialog", on: { click: openJump } }, "Jump");
    return h("section.ov-card", { "aria-label": "Where you are" },
      h("p.ov-where", icon("shield"), h("span", whereText())),
      list.length ? stepTicks(".ov-ticks") : null,
      h("p.ov-dark", s.p >= 1 - 1e-9 ? "Everything in the book is lit." : "Everything after it stays dark."),
      h("div.ov-acts", finish, jump));
  }
  function paintOverview() {
    const v = s.view;
    const book = s.series ? s.book : null;
    // The header picture: the latest visible plate of the book being read, else its cover, else a plain band.
    const plate = visiblePictures().filter((p) => p.rec.kind === "plate" && (!s.series || p.book === s.book)).sort((a, b) => a.ord - b.ord).at(-1);
    const cov = plate ? null : bookCover(book);
    const img = plate ? artImg(picSrc(plate), "", "ov-hero-img", { eager: true }) : cov ? h("img.ov-hero-img", { src: cov, alt: "", decoding: "async" }) : null;
    if (img && !plate) img.addEventListener("error", () => img.remove());
    const story = v.recap ? firstSentences(v.recap.text) : "";
    const byline = overviewByline();
    const hero = h(`section.ov-hero${img ? ".has-img" : ""}${plate ? ".is-plate" : ""}`, { "aria-label": "This book" },
      img, h("div.ov-hero-text", h("h2.ov-title", tidyTitle(bookTitleOf(book))), byline ? h("p.ov-by", byline) : null,
        story ? h("p.ov-story", story) : h("p.ov-story.muted", "The story so far appears here as you read."),
        story && v.recap.text.length > story.length + 2 ? h("button.linkish.ov-more", { type: "button", on: { click: () => { s.recapOpen = true; selectTab("chapters"); } } }, "Read the whole recap") : null,
        plate ? h("p.ov-plate-note", "Drawn from a chapter you have read.") : null),
      overviewCard());

    // Who's who so far: a portrait grid, 8 shown.
    const people = whoIsWho();
    const { last } = stepState();
    const fresh = new Set(last ? (metByChapter().get(last.seq) || []).map((e) => e.id) : []);
    const newLabel = last ? `New in ${stepName(last).replace(/^Chapter\b/, "Ch.")}` : "";
    const card = (e) => {
      const pic = latestPicture(e.id);
      const face = pic ? artImg(picSrc(pic, true), "", "ov-face-img") : null;
      return h("li", h(`button.ov-person.t-${e.type}${face ? ".has-face" : ""}`, { type: "button", dataset: { ent: e.id },
        "aria-label": `${e.name}${fresh.has(e.id) ? `, ${newLabel.toLowerCase()}` : ""}`, on: { click: (ev) => openDrawer(e.id, { from: ev.currentTarget }) } },
        h("span.ov-face", { "aria-hidden": "true" }, face, h("span.ov-initials", initials(e.name))),
        h("span.ov-name", e.name),
        fresh.has(e.id) ? h("span.ov-new", newLabel) : null));
    };
    const who = h("section.ov-sec", { "aria-labelledby": "ov-who" },
      h("div.ov-sec-head", h("h3#ov-who", "Who's who so far"), people.length > 8 ? h("button.linkish", { type: "button", on: { click: () => { s.typesRestore ??= s.types; s.types = new Set(["person", "creature"]); selectTab("cast"); } } },
        `All ${people.length} ${people.every((e) => e.type === "person") ? "people" : "people and creatures"} you've met`) : null),
      people.length ? h("ol.ov-people", people.slice(0, 8).map(card)) : h("p.muted", "Nobody yet. The people you meet appear here as you read."));

    // Chapters so far: the latest 4 finished, each with its picture and recap.
    const rows = currentRows(chapterGroups().filter((g) => !s.series || !g.book || g.book === s.book));
    const done = rows.filter((r) => r.group);   // a chapter whose recap is revealed
    const plates = platesByRow(rows, book);
    const latest = done.slice(-4).reverse();
    const anyPic = latest.some((r) => (plates.get(r) || []).length);
    const chCard = (r) => {
      const pic = (plates.get(r) || []).at(-1);
      const thumb = pic ? artImg(picSrc(pic, true), "", "ov-ch-img") : null;
      const label = rowLabel(r, book);
      // Beside a chapter with a picture, one without shows more of its recap.
      return h("li", h(`button.ov-chapter${thumb ? ".has-pic" : anyPic ? ".long" : ""}`, { type: "button", on: { click: () => openChapter(r.group.key) } },
        thumb ? h("span.ov-ch-pic", thumb) : null,
        h("span.ov-ch-text", label ? h("span.ov-ch-label", label) : null,
          h("span.ov-ch-title", tidyTitle(r.title)), h("span.ov-ch-recap", r.group.parts.join(" ")))));
    };
    const chapters = h("section.ov-sec", { "aria-labelledby": "ov-ch" },
      h("div.ov-sec-head", h("h3#ov-ch", "Chapters so far"), done.length > 4 ? h("button.linkish", { type: "button", on: { click: () => selectTab("chapters") } },
        `All ${done.length} chapters`) : null),
      latest.length ? h("ol.ov-chapters", latest.map(chCard)) : h("p.muted", "No chapter finished yet. Each chapter's summary appears once you've read past it."));
    clear(el.panel, h("div.overview", hero, who, chapters));
  }
  /** Switch tabs from inside a tab (the Overview's links), keeping the tab row and the address in step. */
  function selectTab(k) {
    s.tab = k;
    paintTabs();
    if (k === "ask") saveUrl();
    document.getElementById(`tab-${k}`)?.focus({ preventScroll: true });
    el.panel.scrollIntoView?.({ block: "nearest" });
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
  function eventRow(ev, { chips = true, book = true, draw = false } = {}) {
    const pic = eventPicture(ev);
    // The 64px picture at the left; "Draw a picture" sits quietly at the row's end (shown on hover or focus where there is hover).
    const canDraw = draw && !pic && !s.edit && s.art.canDraw && ev.kind !== "minor" && (!s.series || !ev.book || ev.book === s.book);
    const li = h(`li.tl-ev${ev.kind ? `.${ev.kind}` : ""}${s.fresh.has(ev.id) ? ".fresh" : ""}${pic ? ".has-pic" : ""}${canDraw ? ".can-draw" : ""}`, { dataset: { ev: ev.id }, tabindex: "-1" });
    const text = [ev.kind === "turn" ? h("span.tl-kind", "Turning point") : null, h("p.ev", ev.summary),
      chips ? h("div.ents", ev.entities.map(entityChip), bookLabel(ev.book)) : book ? bookLabel(ev.book) : null,
      s.edit ? editor.eventRowActions(ev, li) : null];
    const thumb = pic ? pictureButton([pic], 0, () => ev.summary.length > 80 ? `${ev.summary.slice(0, 79)}…` : ev.summary, "art-thumb.tl-thumb") : null;
    const more = canDraw ? h("button.icon-btn.small.tl-draw", { type: "button", "aria-label": "Draw a picture of this moment", title: "Draw a picture",
      on: { click: () => drawPicture({ kind: "event", target: ev.id, title: tidyTitle(eventChapter(ev)?.title || "") || "This moment" }) } }, icon("sparkle")) : null;
    if (thumb || more) clear(li, thumb, h("div.tl-text", text), more);
    else clear(li, text);
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
    const rowBody = (events, total = events.length) => (events.length ? h("ol.tl-list", sceneRuns(events).map((run) => [run.setting ? settingHeading(run.setting) : null, run.events.map((ev) => eventRow(ev, { draw: true }))]))
      : h("p.muted.tl-none", total ? "Nothing to show with these filters." : "No events in this chapter."));
    // A chapter's latest comic sits at the top of its row (the book being read, or a finished earlier one), and the book
    // being read has "Make a comic" beside the chapter title.
    const rowItems = (rows, book = s.series ? s.book : null) => rows.map((r, i) => {
      const evs = inRow(r.events);
      const current = !s.series || book === s.book;
      const comic = comicNode(comicsOf(r, book), tidyTitle(r.title));
      const body = rowBody(evs, r.events.length);
      return { id: null, label: rowLabel(r, book), title: tidyTitle(r.title), action: current ? comicButton(r) : null,
        cls: r.now ? "now" : r.events.some((ev) => s.fresh.has(ev.id)) ? "fresh" : "", body: comic ? h("div.tl-row-body", comic, body) : body };
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
      const item = { label: null, title: null, cls: "", pin: true, body: rowBody(s.newestFirst ? [...loose].reverse() : loose) };
      currentItems[s.newestFirst ? "unshift" : "push"](item);
    }
    const currentBlock = currentItems.length ? bookBlock(s.series ? s.book : null, currentItems, { current: true, newest: s.newestFirst, repaint }) : null;
    const order = s.series ? s.series.orders[s.order] || [] : [];
    const earlierKeys = order.filter((b) => b !== s.book && all.some((ev) => ev.book === b));
    const earlierBlocks = (s.newestFirst ? [...earlierKeys].reverse() : earlierKeys).map((b) => {
      const rowsB = earlierRows(b, chapterGroups().filter((g) => g.book === b));
      if (rowsB.length) return bookBlock(b, ordered(rowItems(rowsB, b)), { current: false, newest: s.newestFirst, repaint });
      // That index has no chapter recaps: its events as one block with scene runs.
      const evs = inRow(all.filter((ev) => ev.book === b));
      if (!evs.length) return null;   // nothing to show under these filters: no block, no banner
      return bookBlock(b, [], { current: false, newest: s.newestFirst, noun: "event", repaint,
        raw: { total: evs.length, build: (full) => h("div.ch-body.tl-flat", rowBody(full ? evs : evs.slice(0, NEAR))) } });
    });
    const blocks = (s.newestFirst ? [currentBlock, ...earlierBlocks] : [...earlierBlocks, currentBlock]).filter(Boolean);
    clear(el.panel, h("div.toolbar.tl-bar", toggle, turnChip, minorChip, s.edit ? editor.addEventButton() : null),
      blocks.some(Boolean) ? h("div.book-stack", blocks.filter(Boolean))
        : h("div.empty", h("div.art", icon("timeline")), h("h3", empty[0]), h("p", empty[1])));
  }

  function relationRow(r) {
    return h("li", entityChip(r.src), h("span.lbl", r.label), entityChip(r.dst));
  }

  /** Short name for a map chapter: "Ch. III" when the title starts with a chapter number, "Ch. 2" for one the book
   *  numbers ("Two"), else the title. */
  function shortChapter(c) {
    const t = tidyTitle(c.title);
    const m = /^(?:chapter|ch\.?)\s+([\w]+)/i.exec(t);
    if (m) return `Ch. ${m[1]}`;
    const n = c.start != null ? mapNumbers().get(c.start) : null;
    return n != null ? `Ch. ${n}` : t.length > 26 ? `${t.slice(0, 25)}…` : t;
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
      portrait: (id) => { const pics = entityPictures(id); return pics.length ? h("div.g-face", pictureButton(pics, pics.length - 1, () => nameOf(id)?.name || "", "art-face-btn", { thumb: false })) : null; },
    });
    // Beside the list (1101px and up) the map's details take the entry's place on the right; narrower they stay under the map.
    const side = el.panel.querySelector(".g-panel");
    const keep = el.mapSide.contains(document.activeElement) ? document.activeElement : null;
    if (side && paneMode()) { const scroll = el.mapSide.scrollTop; clear(el.mapSide, side); el.mapSide.scrollTop = scroll; el.split.classList.add("has-side"); }
    else { clear(el.mapSide); el.split.classList.remove("has-side"); }
    if (keep && !document.contains(keep)) el.mapSide.querySelector("button")?.focus({ preventScroll: true });
  }

  // ---- pictures (art.js) ---------------------------------------------------------------------
  // An art set sits beside each book's index; the server sends its manifest whole and every picture is gated here by its
  // `from` anchor (visibleArt): the book being read up to the frontier, a finished earlier book whole, any other book not at
  // all. Every placement, count and lightbox step reads from visiblePictures(), so nothing past the position is shown.

  /** The document of a book of the series (the one being read for a standalone index). */
  const docOf = (book) => (!s.series || !book || book === s.book ? s.doc : s.seriesDocs.get(book) || null);
  const ordsOf = (d) => { if (!d) return new Map(); if (!d.__ords) Object.defineProperty(d, "__ords", { value: new Map((d.anchors || []).map((a, i) => [a.id, i])) }); return d.__ords; };
  /** The books whose pictures may show: this one, and the finished earlier ones. */
  const pictureBooks = () => (s.series ? [s.book, ...(finishedSet() || [])] : [null]);
  /** The art sets of the books that may show, fetched in the chosen style. Runs after every load and style change. */
  async function loadPictures(stale = () => false) {
    const mine = (s.art.loadSeq = (s.art.loadSeq || 0) + 1);
    s.art.loadedBooks = pictureBooks().map(bkKey).join(",");
    const gone = () => stale() || disposed || mine !== s.art.loadSeq;
    // "Draw a picture" shows when an image provider is set up, or when the book already has pictures (the sheet then says a key
    // is needed). Drawing needs the book file (the prompt is written from the passage), so never without it.
    const decide = () => {
      const could = s.art.canDraw;
      s.art.canDraw = (s.art.hasProvider || s.art.styles.length > 0) && Boolean(s.hash && s.map?.exact) && !preview;
      return could !== s.art.canDraw;
    };
    imageProviders().then((list) => {
      if (gone()) return;
      s.art.hasProvider = list.length > 0;
      if (decide()) repaintPictures();
    });
    const first = await loadArt(s.key, s.art.style);
    if (gone()) return;
    s.art.styles = first?.styles || [];
    const slugs = s.art.styles.map((x) => x.slug);
    // The remembered style when this book has it, else the first one it has.
    const want = slugs.includes(s.art.style) ? s.art.style : first?.slug && slugs.includes(first.slug) ? first.slug : slugs[0] || null;
    const sets = new Map();
    for (const book of pictureBooks()) {
      const key = s.bookKeys.get(bkKey(book)) || (book == null || book === s.book ? s.key : null);
      if (!key || !want) continue;
      const got = key === s.key && first?.slug === want ? first : await loadArt(key, want);
      if (gone()) return;
      const d = docOf(book);
      const hashes = new Set((d?.editions || []).filter((e) => e.builtFrom).map((e) => e.textHash));
      // The art set must be for this index's own edition: its anchors are the ones its pictures are compared against.
      if (got && got.manifest && hashes.has(got.manifest.textHash)) sets.set(bkKey(book), { ...got, key, slug: want });
    }
    s.art.sets = sets;
    s.art.slug = want;
    s.art.fresh = null;
    decide();
    s.art.memo = null; s.art.rev++;
    repaintPictures();
    // A chapter's screening (`screened[title] = {key, events}`) counts only while it still matches the chapter's events; the
    // key is the server's, so ask it which chapters' screenings are fresh now (the estimate reports that, with no model call).
    const own = sets.get(bkKey(s.series ? s.book : null));
    if (own && want && Object.keys(own.screened || {}).length) {
      api.artEstimate({ key: s.key, style: want }).then((r) => {
        if (gone() || !Array.isArray(r?.chapters)) return;
        s.art.fresh = new Set(r.chapters.filter((c) => c && c.screened === true && typeof c.title === "string").map((c) => c.title));
        repaintPictures();
      }).catch(() => {});
    }
  }
  /** The finished set or the style changed: fetch what is needed and repaint. */
  function refreshPictures({ forget = null } = {}) {
    if (forget) forgetArt(forget);
    if (!preview) loadPictures();
  }
  /** Repaint what shows pictures: the toolbar control, the tab, the open entry (only when no form holds typed text). */
  function repaintPictures() {
    if (disposed || !s.view || !el.tabs) return;
    paintArtControl();
    paintTabs();
    if (s.drawer && !drawerDirty()) openDrawer(s.drawer, { refresh: true });
    else if (s.drawer) {
      // An entry with typed text is not rebuilt: its pictures (another style, or Pictures off) leave it instead, and stop
      // counting as something it shows, so the safety net never reads a picture change as the position moving back.
      const box = host();
      box?.querySelectorAll(".art-face-btn, .art-earlier, .art-portraits, .tl-thumb, .profile.face-only").forEach((n) => n.remove());
      box?.querySelectorAll(".has-face, .has-pic").forEach((n) => n.classList.remove("has-face", "has-pic"));
      s.drawerShows = (s.drawerShows || []).filter((x) => !x.startsWith("pic:"));
    }
  }
  /** Every picture the reader may see now, as [{ rec, book, set }]; memoised per view and art revision. */
  function visiblePictures() {
    if (!s.art.on || !s.art.sets.size || !s.view) return [];
    const m = s.art.memo;
    if (m && m.view === s.view && m.rev === s.art.rev && m.on === s.art.on) return m.list;
    const list = [];
    const fin = new Set(finishedSet() || []);
    for (const book of pictureBooks()) {
      const set = s.art.sets.get(bkKey(book));
      if (!set) continue;
      const d = docOf(book);
      const ords = ordsOf(d);
      const current = !s.series || book === s.book;
      if (!current && !fin.has(book)) continue;
      const front = current ? s.map.frontier(s.p) : null;
      const pos = current ? (front == null ? -1 : ords.get(front) ?? -1) : -1;
      for (const rec of visibleArt(set.manifest, ords, pos, !current)) list.push({ rec, book, set, ord: ords.get(rec.from) });
    }
    s.art.memo = { view: s.view, rev: s.art.rev, on: s.art.on, list };
    return list;
  }
  const picSrc = (p, thumb = false) => artUrl(p.rec, p.set.manifest.textHash, p.set.slug, { thumb });
  const bookRank = (b) => { if (!s.series) return 0; const o = s.series.orders[s.order] || []; const i = o.indexOf(b); return i < 0 ? -1 : i; };
  /** An entry's visible pictures (portraits, places, things), oldest first: by book in reading order, then by anchor. */
  function entityPictures(id) {
    return visiblePictures().filter((p) => ENTITY_KINDS.has(p.rec.kind) && p.rec.target === id)
      .sort((a, b) => bookRank(a.book) - bookRank(b.book) || a.ord - b.ord);
  }
  const latestPicture = (id) => entityPictures(id).at(-1) || null;
  /** An event's latest visible picture. */
  const eventPicture = (ev) => visiblePictures().filter((p) => p.rec.kind === "event" && p.rec.target === ev.id && (!s.series || !ev.book || p.book === ev.book)).at(-1) || null;
  /** The plates of a book's chapter rows: Map(row → [picture]) (rows from currentRows or earlierRows). */
  function platesByRow(rows, book) {
    const out = new Map();
    const plates = visiblePictures().filter((p) => p.rec.kind === "plate" && (!s.series || p.book === (book ?? s.book)));
    if (!plates.length) return out;
    const current = !s.series || book == null || book === s.book;
    const d = docOf(book);
    const ords = ordsOf(d);
    const recapOrd = new Map((d?.recaps || []).filter((r) => r.scope === "chapter").map((r) => [r.id, ords.get(r.from) ?? Infinity]));
    const rowOrd = rows.map((r) => (r.group ? Math.max(...r.group.ids.map((id) => recapOrd.get(id) ?? -1)) : -1));
    for (const p of plates) {
      let row = null;
      if (current && s.map.exact) {
        const at = s.map.anchorPos(p.rec.from);
        const ch = Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
        row = ch ? rows.find((r) => r.ch && r.ch.start === ch.start) : null;
      }
      if (!row) { const i = rowOrd.findIndex((o) => o >= p.ord); row = i >= 0 ? rows[i] : null; }
      if (!row) continue;   // no row for it at this position: not shown (a row is listed only once it is reached)
      if (!out.has(row)) out.set(row, []);
      out.get(row).push(p);
    }
    return out;
  }
  /** The chapter key the pipeline files a chapter under (its title, " (2)" for a repeated one), for the current book. */
  function artChapterKey(r) {
    if (!r.ch || !s.map.exact) return r.title;
    if (!s.art.chKeys || s.art.chKeys.doc !== s.doc) {
      const seqs = (s.doc.anchors || []).map((a) => a.seq);
      const ord = ordsOf(s.doc);
      const at = (id) => { const i = ord.get(id); return i == null ? null : seqs[i]; };
      const marks = [...(s.doc.events || []).map((e) => at(e.from)), ...(s.doc.recaps || []).filter((x) => x.scope === "chapter").map((x) => at(x.from))].filter((x) => x != null);
      const seen = new Map(), keys = new Map();
      s.map.chapters.forEach((c, i) => {
        const end = i + 1 < s.map.chapters.length ? s.map.chapters[i + 1].start - 1 : Infinity;
        if (!marks.some((q) => q >= c.start && q <= end)) return;
        const n = (seen.get(c.title) || 0) + 1;
        seen.set(c.title, n);
        keys.set(c.start, n === 1 ? c.title : `${c.title} (${n})`);
      });
      s.art.chKeys = { doc: s.doc, keys };
    }
    return s.art.chKeys.keys.get(r.ch.start) || r.ch.title;
  }
  /** Lightbox items for pictures, with their captions (a chapter title or the entry's name, never the prompt). */
  function lightboxItems(pics, caption) {
    return pics.map((p) => {
      const canEdit = s.art.canDraw && p.set.key === s.key;
      return { rec: p.rec, src: picSrc(p), srcFor: (rec) => picSrc({ ...p, rec }), caption: caption(p), key: p.set.key,
        style: p.set.slug, styleText: p.set.manifest.style, canEdit, canRedraw: Boolean(s.art.hasProvider),
        newPrompt: canEdit && s.art.hasProvider && canRewrite(p.rec) ? (rec) => redrawPicture(rec, caption(p), p.set.slug) : null,
        promptHiddenUntil: (rec) => promptHiddenUntil(rec, p.book),
        // the reader's frontier for a same-prompt redraw (a picture of the book being read; an earlier book's is whole)
        position: p.set.key === s.key ? () => s.map.frontier(s.p) : null,
        // the finished earlier books, sent with a check and a fix (the same gate as a redraw)
        finished: () => (s.series ? finishedSet() || [] : []),
        figureName: figureName, figureThumb: (rec, f) => figureThumb(p, f), panelText: (id) => eventSummary(id, p.book) };
    });
  }
  /** A figure's name for the sheet and the lightbox: only from the view at the position, null when not visible there. */
  const figureName = (id) => nameOf(id)?.name || null;
  /** A figure's reference picture, only when that picture itself passes the gate: found among the pictures visible now in the
   *  picture's own set. A likeness in another style's set cannot be gated here, so it shows no thumbnail. */
  function figureThumb(p, f) {
    const ref = refParts(f.ref);
    if (!ref || (ref.set && ref.set !== p.set.slug)) return null;
    const hit = visiblePictures().find((x) => x.set === p.set && x.rec.id === ref.id);
    return hit ? picSrc(hit, true) : null;
  }
  /** May a picture be redrawn from a new prompt: its kind and target are an item of the book being read, revealed now. */
  function canRewrite(rec) {
    if (!rec || typeof rec.target !== "string" || !(rec.kind === "plate" || rec.kind === "event" || rec.kind === "comic" || ENTITY_KINDS.has(rec.kind))) return false;
    if (ENTITY_KINDS.has(rec.kind) && s.series && !inThisBook(rec.target)) return false;
    return sheetItemVisible({ kind: rec.kind, target: rec.target });
  }
  /** Redraw with a new prompt: the picture sheet for the same kind and target at the position, replacing `rec`. */
  function redrawPicture(rec, title, slug) {
    if (!canRewrite(rec)) return;
    drawPicture({ kind: rec.kind, target: rec.target, title: rec.kind === "plate" ? tidyTitle(title) : rec.kind === "comic" ? tidyTitle(String(title).replace(/^Comic: /, "")) : title, replaces: rec.id, replacesStyle: slug,
      refs: Array.isArray(rec.userRefs) ? rec.userRefs : null });   // the person's own reference pictures start in the row
  }
  /** Open the lightbox; it closes itself if a later repaint would hide any picture it can step to. */
  function showPictures(pics, index, caption) {
    s.art.lightbox?.close();
    const items = lightboxItems(pics, caption);
    const box = openLightbox(items, index, { onChanged: () => refreshPictures({ forget: s.key }) });
    s.art.lightbox = box && { close: box.close, refresh: box.refresh, ids: box.ids };
  }
  /** A picture as a button that opens the lightbox on it. */
  function pictureButton(pics, i, caption, cls, { thumb = true } = {}) {
    const p = pics[i];
    const img = artImg(picSrc(p, thumb), caption(p), "art-img");
    if (!img) return null;
    return h(`button.${cls}`, { type: "button", "aria-label": `Open the picture: ${caption(p)}`, on: { click: () => showPictures(pics, i, caption) } }, img);
  }
  /** A chapter's plates: one full width, two or three as thumbnails, "N more" past three. */
  function platesNode(pics, title) {
    if (!pics.length) return null;
    const cap = () => title;
    if (pics.length === 1) return h("div.art-plates.one", pictureButton(pics, 0, cap, "art-plate", { thumb: false }));
    const shown = pics.slice(0, 3);
    return h("div.art-plates.many", shown.map((p, i) => pictureButton(pics, i, cap, "art-thumb")),
      pics.length > 3 ? h("button.linkish.art-more", { type: "button", on: { click: () => showPictures(pics, 3, cap) } }, `${pics.length - 3} more`) : null);
  }
  /** "Draw a picture" for a chapter row of the book being read: the main moment once the chapter is finished, or any
   *  visible drawable moment without a picture. Counts come from visible content only. */
  function chapterDrawButton(r, plates) {
    if (!s.art.canDraw || s.edit) return null;
    const key = artChapterKey(r);
    const set = s.art.sets.get(bkKey(s.series ? s.book : null));
    // The screened moments ({key, events: [ids]}), only when the server reports that screening fresh for this chapter now;
    // otherwise every visible non-minor event is offered and no "N moments" line is shown.
    const entry = set?.screened?.[key];
    const screened = s.art.fresh?.has(key) && entry && Array.isArray(entry.events) ? entry.events.filter((x) => typeof x === "string") : null;
    const visibleEvs = new Map(r.events.map((ev) => [ev.id, ev]));
    const moments = screened ? screened.filter((id) => visibleEvs.has(id)).map((id) => ({ event: id }))
      : r.events.filter((ev) => ev.kind !== "minor").map((ev) => ({ event: ev.id }));
    const drawnEv = new Set(r.events.filter((ev) => eventPicture(ev)).map((ev) => ev.id));
    const choices = [];
    if (r.group && !r.now) choices.push({ kind: "plate", target: key, label: "The chapter's main moment" });
    for (const m of moments) if (!drawnEv.has(m.event)) choices.push({ kind: "event", target: m.event, label: visibleEvs.get(m.event).summary });
    if (!choices.length) return null;
    const drawn = plates.length + drawnEv.size;
    const line = screened ? `${moments.length} moment${moments.length === 1 ? "" : "s"} in this chapter so far, ${drawn} drawn` : null;
    return h("button.linkish.art-draw", { type: "button", on: { click: (ev) => drawPicture({ ...choices[0], title: tidyTitle(r.title), moments: choices, drawnLine: line, from: ev.currentTarget }) } },
      icon("sparkle"), "Draw a picture");
  }
  /** The moments a comic of a chapter row can draw: its visible events that are not small moments (every one in an index
   *  that weighs none). The server makes the same choice from the same gated events, at most 6 of them. */
  const comicMoments = (r) => {
    const weighed = s.view.events.some((ev) => ev.kind);
    return r.events.filter((ev) => !weighed || ev.kind !== "minor");
  };
  /** "Make a comic" beside a Timeline chapter title of the book being read: one page of the chapter's moments up to the
   *  position, with 2 or more of them visible. */
  function comicButton(r) {
    if (!s.art.canDraw || s.edit || comicMoments(r).length < 2) return null;
    const title = tidyTitle(r.title);
    return h("button.linkish.art-draw.tl-comic-btn", { type: "button", "aria-label": `Make a comic of ${title || "this chapter"}`,
      on: { click: () => drawPicture({ kind: "comic", target: artChapterKey(r), title }) } }, icon("sparkle"), "Make a comic");
  }
  /** A chapter row's visible comics, oldest first (the book being read by the pipeline's chapter key; an earlier book's,
   *  which has no chapter map here, by the row's title). */
  function comicsOf(r, book = s.series ? s.book : null) {
    const current = !s.series || book == null || book === s.book;
    const key = current ? artChapterKey(r) : r.title;
    return visiblePictures().filter((p) => p.rec.kind === "comic" && (!s.series || p.book === (book ?? s.book))
      && (p.rec.target === key || (!current && tidyTitle(p.rec.target) === tidyTitle(r.title))))
      .sort((a, b) => a.ord - b.ord);
  }
  /** The latest comic of a row, full width of the column, labelled "Comic"; the lightbox's arrows reach the older ones. */
  function comicNode(pics, title) {
    if (!pics.length) return null;
    const cap = () => (title ? `Comic: ${title}` : "Comic");
    return h("div.tl-comic", h("span.tl-comic-label", "Comic"), pictureButton(pics, pics.length - 1, cap, "art-plate.tl-comic-pic", { thumb: false }));
  }
  /** A panel's caption: its event's summary from the view at the position (an earlier book's by that book), else null. */
  function eventSummary(id, book = null) {
    const evs = s.view.events.filter((ev) => ev.id === id);
    const ev = evs.find((x) => !s.series || !book || !x.book || x.book === book) || null;
    return ev && typeof ev.summary === "string" ? ev.summary : null;
  }
  /** The books of the reading order before `book` (the ones a later book's text may carry content from). */
  const booksBefore = (book) => { if (!s.series) return []; const o = s.series.orders[s.order] || []; const i = o.indexOf(book); return i > 0 ? o.slice(0, i) : []; };
  /** The background note the picture sheet shows and whether it may be written (artgate.sheetWorld), at the position now. */
  function sheetWorld(set) {
    const ords = ordsOf(s.doc);
    const front = s.map.frontier(s.p);
    return worldForSheet(set?.manifest?.world, { ords, position: front == null ? -1 : ords.get(front) ?? -1,
      book: s.series ? s.book : null, finished: finishedSet() || [], earlier: booksBefore(s.book) });
  }
  /** The title of the first earlier book a picture's prompt carries content from and the reader has not finished, or null. */
  function promptHiddenUntil(rec, book) {
    if (!s.series) return null;
    const earlier = booksBefore(book);
    const missing = unfinishedSources(rec?.inherits, { book: s.book, finished: finishedSet() || [], earlier, fallback: earlier.length > 0 });
    return missing.length ? bookTitleOf(missing[0]) : null;
  }
  /** Open the picture sheet for one item; after a picture is filed, the art set is fetched again and the page repaints. */
  function drawPicture({ kind, target, title, moments = null, drawnLine = null, replaces = null, replacesStyle = null, through = false, refs = null }) {
    const set = s.art.sets.get(bkKey(s.series ? s.book : null));
    const { world, canEditWorld } = sheetWorld(set);
    // The position and finished books are asked at every call, so "Write it again" after a move writes for where the reader is.
    const opened = openPictureSheet({ key: s.key, kind, target, title, moments, drawnLine, replaces, replacesStyle, figureName, through, refs,
      panelText: (id) => eventSummary(id, s.series ? s.book : null),
      position: () => ({ p: s.map.frontier(s.p), finished: s.series ? finishedSet() || [] : [] }),
      // A prompt written for a position the reader has since moved back from is never put on screen.
      reached: anchorReached,
      style: s.art.slug || null, styles: s.art.styles, world, canEditWorld,
      // The art set's text hash only (never the server's guess for another edition), the style by the server's slug.
      srcFor: (rec, slug) => (set?.manifest?.textHash ? artUrl(rec, set.manifest.textHash, slug) : null),
      canShow: (rec) => {
        const ords = ordsOf(s.doc);
        const front = s.map.frontier(s.p);
        return visibleArt({ pictures: [rec] }, ords, front == null ? -1 : ords.get(front) ?? -1, false).length === 1;
      },
      onDrawn: (rec, slug) => {
        forgetArt(s.key);
        if (slug && slug !== s.art.slug) { s.art.style = slug; setArtStyle(slug); }
        refreshPictures();
      },
      onUndone: () => refreshPictures({ forget: s.key }),
      onStarted: (job) => watchArtRun(job, s.key) });
    const entry = { kind, target, opened, sh: null };
    s.art.sheet = entry;
    opened.then((sh) => { entry.sh = sh; sh?.closed?.then(() => { if (s.art.sheet === entry) s.art.sheet = null; }); });
  }
  /** A pictures run started from the sheet ("Through the book so far"): the art set is fetched again as its pictures come
   *  in and once it ends, so the entry and the scene references pick the new portraits up. Gated as every picture is. */
  function watchArtRun(job, key) {
    if (!job?.id || preview) return;
    const EVERY = 15000, MOST = 120000;
    let seen = 0, wait = EVERY;
    const tick = async () => {
      if (disposed || s.key !== key) return;
      let j = null;
      try { j = await api.job(job.id); } catch (e) {
        // The run was removed: nothing more will come. Anything else (a blip, a restart): ask again, less often each time.
        if (e?.status === 404) { refreshPictures({ forget: key }); return; }
        wait = Math.min(MOST, wait * 2);
        setTimeout(tick, wait);
        return;
      }
      wait = EVERY;
      if (disposed || s.key !== key) return;
      // A pictures job keeps its progress as `art: [done, total, cost]` (the Runs monitor reads it the same way).
      const art = j?.progress?.art;
      const done = Array.isArray(art) ? +art[0] || 0 : 0;
      const live = ["queued", "running", "stopping"].includes(j?.status);
      if (done !== seen || !live) refreshPictures({ forget: key });
      seen = done;
      if (live) setTimeout(tick, EVERY);
    };
    setTimeout(tick, EVERY);
  }
  /** Everything an open picture sheet names (the moment selected now and every moment listed) is still revealed. */
  function sheetVisible(entry) {
    const items = entry.sh?.listed ? entry.sh.listed() : [{ kind: entry.kind, target: entry.target }];
    return items.every(sheetItemVisible);
  }
  /** Every anchor the sheet's text was written up to (the prompt's, the World line's) is reached in the book being read. */
  function sheetTextVisible(sh) {
    return (sh.anchors ? sh.anchors() : []).every(anchorReached);
  }
  /** Is an anchor of the book being read at or before the position now? (An unknown anchor never is.) */
  function anchorReached(a) {
    const ords = ordsOf(s.doc);
    const front = s.map.frontier(s.p);
    const pos = front == null ? -1 : ords.get(front) ?? -1;
    return ords.has(a) && ords.get(a) <= pos;
  }
  /** Is the item a picture sheet was opened for still revealed? (A Book Orbit sync may move the position back.) */
  function sheetItemVisible({ kind, target }) {
    if (kind === "event") return s.view.events.some((ev) => ev.id === target);
    if (ENTITY_KINDS.has(kind)) return Boolean(nameOf(target));
    const rows = currentRows(chapterGroups().filter((g) => !s.series || !g.book || g.book === s.book));
    // A comic needs its chapter reached with two of its moments visible (the chapter may be the one being read).
    if (kind === "comic") return rows.some((r) => artChapterKey(r) === target && comicMoments(r).length >= 2);
    return currentRows(chapterGroups().filter((g) => !s.series || !g.book || g.book === s.book)).some((r) => r.group && !r.now && artChapterKey(r) === target);
  }
  /** The toolbar's "Pictures" switch and style select: only for a book with an art set. */
  function paintArtControl() {
    if (!el.artCtl) return;
    const styles = s.art.styles;
    if (!styles.length || preview) return clear(el.artCtl);
    const toggle = h("button.btn.quiet.art-toggle", { type: "button", "aria-pressed": String(s.art.on), title: s.art.on ? "Hide the pictures" : "Show the pictures",
      on: { click: () => setPicturesOn(!s.art.on) } }, icon("eye"), h("span.btn-lbl", "Pictures"));
    const sel = styles.length > 1 ? h("select.select.art-style", { "aria-label": "Picture style", disabled: !s.art.on,
      on: { change: (e) => setPictureStyle(e.target.value) } },
      styles.map((x) => h("option", { value: x.slug, selected: x.slug === s.art.slug }, styleLabel(x.slug, x.text)))) : null;
    clear(el.artCtl, toggle, sel);
  }
  function setPicturesOn(on) {
    s.art.on = on; setArtOn(on);
    s.art.memo = null;
    announce(on ? "Pictures shown" : "Pictures hidden");
    repaintPictures();
    el.artCtl?.querySelector(".art-toggle")?.focus();
  }
  function setPictureStyle(slug) {
    s.art.style = slug; setArtStyle(slug);
    loadPictures().then(() => el.artCtl?.querySelector(".art-style")?.focus());
  }
  /** The entry's portrait beside "Who they are": 140px in the pane, 96px on a phone; a click opens every visible version. */
  function entryFace(e) {
    const pics = entityPictures(e.id);
    if (!pics.length) return null;
    const cap = () => e.name;
    return pictureButton(pics, pics.length - 1, cap, "art-face-btn", { thumb: false });
  }
  /** The chapter a picture of a book is from, worded as the Chapters tab words its row ("Chapter 3", or the title when it
   *  carries its own number or kind); null when no listed row holds it. Rows are only chapters the position has reached,
   *  and a visible picture's anchor is at or before the position, so nothing ahead is named. */
  function pictureChapter(p) {
    const memo = (s.art.chMemo && s.art.chMemo.view === s.view && s.art.chMemo.map === s.map) ? s.art.chMemo : (s.art.chMemo = { view: s.view, map: s.map, rows: new Map() });
    const current = !s.series || p.book == null || p.book === s.book;
    const k = current ? "\u0000now" : p.book;
    if (!memo.rows.has(k)) {
      const groups = chapterGroups();
      memo.rows.set(k, current ? currentRows(groups.filter((g) => !s.series || !g.book || g.book === s.book)) : earlierRows(p.book, groups.filter((g) => g.book === p.book)));
    }
    const rows = memo.rows.get(k);
    let i = -1;
    if (current && s.map.exact) {
      const at = s.map.anchorPos(p.rec.from);
      const ch = Number.isFinite(at) ? s.map.chapterAt(Math.max(0, at - 1e-9)) : null;
      i = ch ? rows.findIndex((r) => r.ch && r.ch.start === ch.start) : -1;
    }
    if (i < 0) {
      // By the recaps' anchors: the first row whose recap is revealed at or after the picture.
      const ords = ordsOf(docOf(p.book));
      const recapOrd = new Map((docOf(p.book)?.recaps || []).filter((r) => r.scope === "chapter").map((r) => [r.id, ords.get(r.from) ?? Infinity]));
      i = rows.findIndex((r) => r.group && Math.max(...r.group.ids.map((id) => recapOrd.get(id) ?? -1)) >= p.ord);
    }
    // No listed row (the chapter being read, without the book file): the chapter the picture was filed under. It holds the
    // picture's own anchor, which is at or before the position.
    const title = i >= 0 ? rows[i].title : typeof p.rec.chapter === "string" ? p.rec.chapter : null;
    const full = tidyTitle(title) || null;
    if (!full) return null;
    const short = (i >= 0 ? rowLabel(rows[i], current ? null : p.book) : null) || full.match(SELF_NAMED_SHORT)?.[0].trim() || full;
    return { short, full };
  }
  /** "Portraits" under "Who they are": every visible picture of the entry, oldest to newest, each captioned with its
   *  chapter, the latest marked as the current one; in a series one row per book. Only with two or more. */
  function portraitsSection(e, { looks = false } = {}) {
    const pics = entityPictures(e.id);
    if (pics.length < 2) return null;
    const order = s.series ? s.series.orders[s.order] || [] : [];
    const caption = (p) => { const ch = pictureChapter(p); return ch ? `${e.name}, ${ch.full}` : e.name; };
    const last = pics.at(-1);
    const tile = (p) => {
      const i = pics.indexOf(p);
      const ch = pictureChapter(p);
      const btn = pictureButton(pics, i, caption, "art-thumb.portrait-thumb");
      if (!btn) return null;
      if (p === last) btn.setAttribute("aria-current", "true");
      return h(`li.portrait-tile${p === last ? ".current" : ""}`, btn,
        ch ? h("span.portrait-cap", { title: ch.short !== ch.full ? ch.full : null }, ch.short) : null, p === last ? h("span.portrait-now", "Current") : null);
    };
    const row = (list, label) => h("ol.portrait-row", { "aria-label": label }, list.map(tile));
    const books = s.series ? [...new Set(pics.map((p) => p.book))] : [null];
    const label = ["person", "creature", "group"].includes(e.type) ? "Portraits" : "Pictures";
    const groups = books.length > 1
      ? books.map((b) => {
        const full = s.series.books.find((x) => x.key === b)?.title || b;
        return h("div.portrait-book", h("h3.portrait-book-title", `${bookNumber(b, order)}${full}`), row(pics.filter((p) => p.book === b), `${label} from ${full}`));
      })
      : [row(pics, label)];
    // Under a picture-first entry's portrait the versions are its looks so far.
    const head = looks ? h("span.eyebrow", label === "Portraits" ? `${pics.length} looks so far` : `${pics.length} pictures so far`)
      : h("span.eyebrow", label, h("span.n", String(pics.length)));
    return h(`div.d-sec.art-portraits${looks ? ".looks" : ""}`, { dataset: { jump: "portraits" } }, head, groups);
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
      ...e.facets.map((f) => `f:${f.id}`), ...e.history.map((f) => `h:${f.id}`), ...(e.arcs || []).map((a) => `a:${a.id}`),
      ...rels.map((r) => `r:${r.id}`), ...evs.map((ev) => `e:${ev.id}`),
      ...others.map((x) => `other:${x}:${nameOf(x)?.name ?? ""}`),
      // Pictures only where the entry shows them: not in edit mode, and none with Pictures off (visiblePictures is then empty).
      ...(s.edit ? [] : [...entityPictures(e.id), ...evs.map(eventPicture).filter(Boolean)].map((p) => `pic:${p.rec.id}`))].filter(Boolean);
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
    if (d) { d.removeEventListener("close", onDialogClose); if (d.open) d.close(); d.remove(); rehomeMergeToast(); }
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
  /** On a phone the ruler is two lines and the chapter select and percent field give way to the Jump sheet (css hides
   *  them); a sheet left open across the 640px line closes, and the series line is painted in the form that fits. */
  let phoneWas = phone();
  function placeJump() {
    const now = phone();
    if (now === phoneWas) return;
    phoneWas = now;
    closeTop();
    if (el.series && s.series) paintSeries();
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

  /** On a phone the entry is a bottom sheet: a swipe down on its header (the handle) follows the finger and closes it
   *  past 90px or on a quick flick, through requestCloseDrawer so typed text still asks first. */
  function swipeToClose(dlg) {
    let drag = null;
    dlg.addEventListener("pointerdown", (ev) => {
      if (!phone() || !ev.target.closest("header") || ev.target.closest("button, a, input, textarea, select") || (ev.pointerType === "mouse" && ev.button !== 0)) return;
      drag = { y: ev.clientY, t: ev.timeStamp, dy: 0, id: ev.pointerId };
      // captured on the header, not the dialog: the click that follows a tap or a short drag then lands on the header and is not read as a tap on the scrim
      ev.target.closest("header").setPointerCapture?.(ev.pointerId);
      dlg.style.transition = "none";
    });
    dlg.addEventListener("pointermove", (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      drag.dy = Math.max(0, ev.clientY - drag.y);
      dlg.style.transform = `translateY(${drag.dy}px)`;
    });
    const end = (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      const { dy, t } = drag; drag = null;
      dlg.style.transition = ""; dlg.style.transform = "";
      if (dy > 90 || (dy > 30 && dy / Math.max(1, ev.timeStamp - t) > 0.5)) requestCloseDrawer();
    };
    dlg.addEventListener("pointerup", end);
    dlg.addEventListener("pointercancel", end);
  }

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
      rehomeMergeToast();
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
      swipeToClose(d);
      showModalLocked(d);
      rehomeMergeToast();
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
  function factKind(f) {
    if (!s.profileIds) s.profileIds = new Map();
    if (!s.profileIds.has(f.book)) {
      const kinds = new Map();
      const d = !f.book || f.book === s.book ? s.doc : s.seriesDocs.get(f.book);
      for (const en of [...(d?.entities || []), ...(d?.carried || []), ...(s.doc?.carried || [])]) for (const x of en.facets || []) if (x.kind === "profile" || x.kind === "arc") kinds.set(x.id, x.kind);
      s.profileIds.set(f.book, kinds);
    }
    return s.profileIds.get(f.book).get(f.id) || "";
  }
  const isProfile = (f) => factKind(f) === "profile";
  /** Earlier arcs of a book sit in `history` beside earlier profiles; they are replaced text, never an impression. */
  const impressionsOf = (e) => e.history.filter((f) => factKind(f) !== "arc");
  /** "In this book" for the book being read or a standalone index; another book's arc names its own book. */
  const arcLabel = (a) => (a.book && a.book !== s.book ? `In ${s.series?.books.find((x) => x.key === a.book)?.title || a.book}` : "In this book");
  /** The arc paragraph, clamped to 4 lines behind "Read more" (shown only while the text is cut off). */
  function arcParagraph(text, eyebrow) {
    const p = h("p.arc-text.clamp", text);
    const more = h("button.linkish.arc-more", { type: "button", "aria-expanded": "false", on: { click: () => {
      const open = p.classList.toggle("clamp") === false;
      more.setAttribute("aria-expanded", String(open)); more.textContent = open ? "Show less" : "Read more"; } } }, "Read more");
    // The width changes (pane, drawer, rotation), so it is measured again whenever the paragraph is resized, and the
    // watcher goes when the card leaves the page.
    if (typeof ResizeObserver === "function") {
      let seen = false;
      const ro = new ResizeObserver(() => {
        if (!p.isConnected) { if (seen) ro.disconnect(); return; }
        seen = true;
        if (p.classList.contains("clamp")) more.hidden = p.scrollHeight <= p.clientHeight + 1;
      });
      ro.observe(p);
    }
    return h("div.arc-desc", eyebrow ? h("span.eyebrow", eyebrow) : null, p, more);
  }

  /** The cards of an entry that spans books, newest first (the book being read on top), as `{ nodes, count }` (the
   *  quiet cards do not count), or null when the entry is shown as before: not a series, or nothing it shows comes from
   *  another book, or no book has anything to put in a card. Decided from the `book` of the view's own items only. */
  function arcCards(e, evs) {
    if (!s.series) return null;
    const order = s.series.orders[s.order] || [];
    const evIn = (b) => evs.filter((ev) => ev.book === b);
    const present = new Set([...e.facets, ...e.history, ...(e.arcs || []), ...evs, ...(e.profile ? [e.profile] : [])].map((x) => x.book).filter(Boolean));
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
    else if (inThisBook(e.id) && !real.some((c) => c.classList.contains("current"))) {
      // This book's pictures of the entry sit in "Portraits" above; a card with nothing else says so rather than "nothing new".
      const n = entityPictures(e.id).filter((p) => p.book === s.book).length;
      const noun = ["person", "creature", "group"].includes(e.type) ? "portrait" : "picture";
      nodes.unshift(quiet(n ? `${bookNumber(s.book, order)}${title}, so far: ${n} ${noun}${n === 1 ? "" : "s"}, shown above.`
        : `${bookNumber(s.book, order)}${title}, so far: appears, nothing new yet.`));
    }
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
    const impressions = impressionsOf(e).filter((f) => f.book === b && f !== ending);
    const details = e.facets.filter((f) => f.book === b);
    const cardFact = (f) => h("li", f.text, f.basis === "inferred" ? h("span.inferred", { title: "A careful reader would conclude this; the text doesn't say it outright." }, "inferred") : null);
    const list = (xs) => foldList(xs.map(cardFact), (nodes) => h("ul.fact-list", nodes), { show: 6, noun: "details" });

    // The book's arc (written from the index) comes first; then, for a finished book, how its last profile ended.
    const arc = (e.arcs || []).find((a) => a.book === b);
    const arcNode = arc ? arcParagraph(arc.text) : null;
    const desc = ending ? arcParagraph(ending.text, `At the end of ${full}`) : null;

    // Main events: turning points first (up to 4), then the latest ordinary ones; small moments sit behind the fold.
    const kinds = bookEvents.some((ev) => ev.kind);
    const main = bookEvents.filter((ev) => !kinds || ev.kind !== "minor");
    const turns = main.filter((ev) => ev.kind === "turn").slice(-4);
    const shownEv = turns.length >= 4 ? new Set(turns) : new Set([...turns, ...main.filter((ev) => ev.kind !== "turn").slice(-(4 - turns.length))]);
    const rows = bookEvents.map((ev) => eventRow(ev, { chips: false, book: false }));
    const eventsNode = bookEvents.length ? eventFold(rows, bookEvents.map((ev) => shownEv.has(ev)), `Show all ${bookEvents.length} events in ${full}`) : null;

    // The book's pictures of the entry are in "Portraits" under "Who they are", grouped by book there.
    if (!arcNode && !desc && !bookEvents.length && !details.length && !impressions.length) return null;
    return h(`section.book-block.arc-card.${now ? "current" : "earlier"}`, { "aria-label": full, dataset: { book: b } },
      h("div.book-head", h("h3.book-title", { title: full }, heading)),
      arcNode, desc, eventsNode,
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

  /** The top of a picture-first entry: the latest visible portrait filling the width, with the type, the name and
   *  "Also called" over its foot. A picture that fails to load leaves the name on plain ground. */
  function entryHero(e, pics) {
    const btn = pictureButton(pics, pics.length - 1, () => e.name, "art-face-btn.hero-img", { thumb: false });
    if (!btn) return null;
    const hero = h(`div.entry-hero.t-${e.type}`, btn, h("div.hero-foot", typeTag(e.type), h("h2#drawer-title", { tabindex: "-1" }, e.name),
      e.aliases.length ? h("p.hero-aka", `Also called ${e.aliases.slice(0, 3).join(", ")}`) : null));
    btn.querySelector("img")?.addEventListener("error", () => hero.classList.add("no-img"));
    return hero;
  }
  /** Up to three tiles of what the index states about an entry: an age its own text gives, a group a visible connection
   *  ties it to, the chapter (or earlier book) where it was first met. Nothing is filled in that the view does not hold. */
  function factTiles(e, rels) {
    const tiles = [];
    const age = statedAge(e.profile?.text, e.facets.map((f) => f.text));
    if (age) tiles.push({ label: "Age", value: capFirst(age) });
    for (const r of rels) {
      const other = nameOf(r.src === e.id ? r.dst : r.src);
      // Only a tie that says it belongs: an alliance or service tie, or an older untyped one worded as membership.
      const belongs = r.kind ? r.kind === "alliance" || r.kind === "service" : /\b(member|part|leader|head|captain|soldier|servant|founder)\b/i.test(r.label || "");
      if (other?.type === "group" && belongs) { tiles.push({ label: "Belongs to", value: other.name, id: other.id }); break; }
    }
    const r = revealInfo(e.id);
    if (r && s.series && r.book && r.book !== s.book) tiles.push({ label: "First met", value: bookShort(r.book) });
    else if (r) {
      const at = s.map.anchorPos(r.from);
      const step = Number.isFinite(at) ? stepChapters().find((c) => c.from < at + 1e-9 && at <= c.to + 1e-9) : null;
      if (step) tiles.push({ label: "First met", value: stepName(step) });
    }
    if (!tiles.length) return null;
    return h("dl.fact-tiles", tiles.map((t) => h("div.fact-tile", h("dt", t.label), h("dd", t.id
      ? h("button.linkish", { type: "button", on: { click: (ev) => openDrawer(t.id, { from: ev.currentTarget }) } }, t.value) : t.value))));
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
    // A standalone book's arc sits under the profile as "In this book", unless it only repeats it. (Hand-read on the Sherlock
    // example: the profile says who he is, the arc what happens to him, so it is kept.)
    const inThisBookArc = !cards && (e.arcs || [])[0] && !(e.profile && e.profile.text.includes(e.arcs[0].text)) ? e.arcs[0] : null;
    const connections = rels.length ? h("div.d-sec", { dataset: { jump: "rels" } }, eyebrow("Connections", rels.length),
      foldList(rels.map(relationRow), (nodes) => h("ul.relations", nodes), { show: 3, noun: "connections" }),
      currentFamilies().some((f) => f.members.includes(id)) ? h("p.d-kin", h("button.linkish", { type: "button", on: { click: () => showFamilyTree(id) } }, "See the family tree")) : null) : null;
    // Picture-first: with a visible portrait the entry opens on it, the name over its foot; else it is as it always was.
    const heroPics = s.edit ? [] : entityPictures(id);
    const hero = heroPics.length ? entryHero(e, heroPics) : null;
    const face = s.edit || hero ? null : entryFace(e);
    const portraits = s.edit ? null : portraitsSection(e, { looks: Boolean(hero) });
    const body = s.edit ? editor.drawerBody({ ...e, history: impressionsOf(e) }, rels, evs, { entityChip, bookLabel }) : h("div.body",
      e.profile ? h(`div.profile${face ? ".has-face" : ""}`, face, h("div.profile-text", h("span.eyebrow", e.type === "person" ? "Who they are" : e.type === "group" ? "Who they are" : "What it is"),
        h("p", e.profile.text, e.profile.basis === "inferred" ? h("span.inferred", "inferred") : null),
        cards && e.profile.book ? h("span.book-label", `From ${s.series.books.find((x) => x.key === e.profile.book)?.title || e.profile.book}`) : bookLabel(e.profile.book))) : face ? h("div.profile.has-face.face-only", face) : null,
      hero ? null : portraits,
      inThisBookArc ? h("div.d-sec", { dataset: { jump: "arc" } }, arcParagraph(inThisBookArc.text, arcLabel(inThisBookArc))) : null,
      e.aliases.length && !(hero && e.aliases.length <= 3) ? h("div.d-sec", h("span.eyebrow", "Also known as"), h("p.aka-line", e.aliases.join(", "))) : null,
      cards ? h("div.d-sec", { dataset: { jump: "books" } }, h("span.eyebrow", "Through the series", cards.count ? h("span.n", String(cards.count)) : null),
        h("div.series-rail.newest.arc-rail", cards.nodes))
        : [h("div.d-sec", { dataset: { jump: "facts" } }, eyebrow("What you know so far", e.facets.length),
          e.facets.length ? facts(e.facets) : h("p.muted", "Nothing beyond the name yet.")),
        impressionsOf(e).length ? h("details.history", h("summary", `Earlier impressions (${impressionsOf(e).length})`), facts(impressionsOf(e), 6)) : null],
      connections,
      cards ? null : evs.length ? h("div.d-sec", { dataset: { jump: "events" } }, eyebrow("Appears in", evs.length),
        foldList(evs.map((ev) => eventRow(ev, { chips: false })), (nodes) => h("ol.timeline", nodes), { show: 3, fromEnd: true, noun: "events" })) : null,
      writableNow() ? mergeFooter(id) : null);
    // Arcs are written by the generator and the server refuses edits to them, so in edit mode they show read-only.
    if (s.edit && (e.arcs || []).length) body.querySelector(".edit-help")?.after(h("div.d-sec", { dataset: { jump: "arc" } },
      ...e.arcs.map((a) => arcParagraph(a.text, `${arcLabel(a)}, written by the generator`))));
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
    // "Draw a picture", also when the entry has one (the new one is another version beside it; Redraw in the lightbox
    // replaces one). Only for an entry of the book being read (the prompt is written from this book's passages).
    const drawLink = !s.edit && s.art.canDraw && KIND_OF_TYPE[e.type] && (!s.series || inThisBook(e.id))
      ? h("p.d-art", h("button.linkish.art-draw", { type: "button", on: { click: () => drawPicture({ kind: KIND_OF_TYPE[e.type], target: e.id, title: e.name,
        // a person or creature can also be drawn through the book so far: every look up to the position, as a run
        through: e.type === "person" || e.type === "creature" }) } }, icon("sparkle"), "Draw a picture")) : null;
    const title = s.edit ? h("div.d-title", h("h2#drawer-title.sr-only", { tabindex: "-1" }, e.name), editor.drawerTitle(e), origin, askLink)
      : h("div.d-title", typeTag(e.type), h("h2#drawer-title", { tabindex: "-1" }, e.name), origin, jumpNav, drawLink, askLink);
    let content = [h("header", title, closeBtn), body];
    if (hero) {
      // The header keeps only Close (and the grab handle on a phone) over the picture; the name sits on the picture's foot.
      body.prepend(hero, h("div.d-title.hero-title", origin, jumpNav, drawLink, askLink), portraits || "", factTiles(e, rels) || "");
      content = [h("header.hero-head", closeBtn), body];
    }
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
      const toasts = dlg.querySelector(":scope > .drawer-toasts");   // an Undo toast shown in the drawer outlives the refresh
      clear(dlg, content, toasts);
      if (keep) document.getElementById(keep)?.focus();
      return;
    }
    dlg?.remove();
    dlg = h(`dialog.drawer.t-${e.type}`, { "aria-labelledby": "drawer-title",
      on: { input: markDirty, change: markDirty, keyup: checkPending, close: onDialogClose, cancel: (ev) => { if (drawerDirty()) { ev.preventDefault(); requestCloseDrawer(); } },
        click: (ev) => { checkPending(); if (ev.target === dlg) requestCloseDrawer(); } } }, content);
    document.body.append(dlg);
    swipeToClose(dlg);
    showModalLocked(dlg);
    rehomeMergeToast();
    if (!refresh) dlg.querySelector("#drawer-title")?.focus({ preventScroll: true });   // the heading is announced; × shows a ring only for keyboard focus
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
    if (c.kind === "arc") return arcEntity(c.id);
    return null;
  };
  /** The entity whose current view carries this arc (an arc replaced since, or past the position, is not there). */
  const arcEntity = (id) => { const e = s.view.entities.find((x) => (x.arcs || []).some((a) => a.id === id)); return e && nameOf(e.id) ? e.id : null; };
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
    } else if (c.kind === "arc") {
      const id = arcEntity(c.id);
      const arc = s.view.entities.find((x) => x.id === id).arcs.find((a) => a.id === c.id);
      openDrawer(id, { from: document.activeElement });
      // The book's card, or "In this book" in the flat view.
      const box = host();
      const target = (arc.book && [...(box?.querySelectorAll(".arc-card[data-book]") || [])].find((n) => n.dataset.book === arc.book)) || box?.querySelector('[data-jump="arc"]');
      target?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
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
    if (!item) {   // behind "Show all" in the current book
      s.bkAll.add(bkKey(s.series ? s.book : null));
      paintTabs();
      item = find();
    }
    if (!item) {   // in a collapsed earlier book or with the earlier books hidden
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
        run: () => setTimeout(() => (phone() ? openJump() : el.chapSel?.focus()), 60) },
      { id: "reader:next", label: "Next reveal", group: "Reading", keywords: "forward step", run: () => stepReveal(1) },
      { id: "reader:prev", label: "Previous reveal", group: "Reading", keywords: "back step", run: () => stepReveal(-1) });
    if (!preview && ask.available?.available) out.push({ id: "reader:ask", label: "Ask about this book", group: "Reading",
      keywords: "ask question who what happened answer", run: () => showAsk() });
    if (!preview && s.art.styles.length) out.push({ id: "reader:pictures", label: s.art.on ? "Hide the pictures" : "Show the pictures", group: "Reading",
      keywords: "pictures images art illustrations portraits", run: () => setPicturesOn(!s.art.on) });
    if (currentFamilies().length) out.push({ id: "reader:trees", label: "Family trees", group: "Reading", keywords: "family tree kin relatives genealogy connections", run: () => showFamilyTree() });
    if (canEdit()) out.push({ id: "reader:edit", label: s.edit ? "Stop editing this index" : "Edit this index", group: "Reading",
      keywords: "edit change fix entry", run: () => setEdit(!s.edit) });
    return out;
  }
  // For tests only: with window.__lkTest set before the page loads, the safety net can be exercised by changing the
  // position past every guard (setP directly).
  if (window.__lkTest) window.__lkTest.reader = { setP: (p) => setP(p), state: s, mergeToast: () => showMergeToast({ id: "test", keepNames: ["a", "b"] }) };
  let unregister = null;
  import("./palette.js").then((m) => { if (!disposed) unregister = m.registerProvider("reader", paletteItems); }).catch((e) => { console.warn("palette provider", e); });

  return () => {
    disposed = true; s.art.lightbox?.close(); s.art.sheet?.opened.then((sh) => sh?.close(true)); if (s.mergeToast && s.mergeToast.host?.contains(s.mergeToast.node) !== false && s.mergeToast.node?.isConnected) toastedMerges.delete(s.mergeToast.auto.id); s.dropMergeToast?.(); ask.dispose(); reading.dispose(); dropPending(); unregister?.(); clearTimeout(s.autoTimer); clearTimeout(s.dupesTimer); closeDrawer({ focus: false }); clearTimeout(urlTimer); clearInterval(pollTimer);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVisible); window.removeEventListener("focus", onVisible);
  };
}
