// The reading view: the book's own text inside the Reader, one chapter at a time with the next one loaded at the end
// and the previous one when scrolling up. The reading position follows the scroll, and names the reader has already met
// are lit and open a small card.
//
// The spoiler rule (SPEC §7.4) holds here as everywhere in the Reader:
// - The position is taken in finished mode from the block at the top of the reading viewport: the blocks above it are
//   read, the top block itself is not. It is handed to the Reader through `ctx.moveTo`, so every tab, count, picture and
//   Ask follow it, and a move past Book Orbit's line asks first as every deliberate move does.
// - Only names of the gated view at that position are lit, and a name is lit in a block only if that name's own `from`
//   anchor is at or before the block (through the Reader's anchor map, where an unresolved anchor takes the next resolved
//   one's place, so it can only light later, never earlier). The card shows the entry as gated at the position.
// - The book text is the person's own EPUB served back to them, and it is rendered through text nodes only.

import { api } from "./api.js";
import { setAppbarTitle } from "./app.js";
import { clear, describe, h, icon, sheet, tidyTitle, typeTag } from "./dom.js";
import { ALWAYS, WITH_TOGGLE, litForms, litSpans, matcher } from "./readnames.js";

const PREFS_KEY = "lk-read-prefs";
const AT_KEY = "lk-read-at";
const WPM = 250;
const THROTTLE = 250;
const SIZES = [15, 16, 17, 18, 19, 20, 21, 22];
const LINES = { tight: ["Compact", 1.45], normal: ["Normal", 1.65], loose: ["Loose", 1.85] };
const WIDTHS = { narrow: ["Narrow", "32em"], medium: ["Medium", "38em"], wide: ["Wide", "46em"] };
function readPrefs() {
  try { const v = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch { return {}; }
}
function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch {} }
function readAt() {
  try { const v = JSON.parse(localStorage.getItem(AT_KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch { return {}; }
}

/**
 * The reading view. `ctx` is what the Reader hands over:
 *   key(), map(), view(), p()          the index key, its PositionMap, the gated view and the position (0..1)
 *   moveTo(p)                          a deliberate move (asks before discarding typed text, and past Book Orbit's line)
 *   settle(p)                          put the position at p quietly (only within the address's rounding, on opening,
 *                                      and never past `line()`)
 *   line()                             the spoiler line a move may not pass without asking (Book Orbit's), or null
 *   doc(), priorDocs(), series()       the index being read, the finished earlier books' indexes, whether it is a series
 *   entity(id)                         the entry as the view shows it now, or null
 *   portrait(id)                       the URL of its latest visible picture, or null
 *   plates()                           the visible chapter plates of the book being read ([{rec, ...}] from visiblePictures)
 *   plateChapter(p)                    the map chapter ({title, start}) plate `p` was drawn for, or null
 *   platesNode(pics, title)            the Reader's plate block (opens the lightbox)
 *   openEntry(id, from)                the entry pane or drawer
 *   canAsk(), ask(text)                Ask, when a model is available
 *   jump()                             the Reader's Jump sheet (chapters and a percent field)
 */
export function createReading(ctx) {
  // For tests only: with window.__lkTest set before the page loads, the time each piece of work takes is kept.
  const times = window.__lkTest ? (window.__lkTest.readTimes = []) : null;
  const timed = (name, fn) => (times ? (...a) => { const t = performance.now(); try { return fn(...a); } finally { times.push([name, performance.now() - t, t]); } } : fn);
  const prefs = { size: null, line: "normal", font: "serif", width: "medium", places: false, ...readPrefs() };
  const root = h("div.read-view", { dataset: { font: prefs.font } });
  const flow = h("div.read-flow", { role: "document", "aria-label": "Book text" });
  const status = h("div.read-status");
  const barTrack = h("span.read-track", { "aria-hidden": "true" }, h("span.read-fill"));
  const barPct = h("span.read-pct");
  const barLeft = h("span.read-left");
  const aaBtn = h("button.btn.quiet.read-aa", { type: "button", "aria-haspopup": "dialog", "aria-label": "Text settings", title: "Text settings" }, h("span.read-aa-glyph", { "aria-hidden": "true" }, "Aa"));
  const jumpBtn = h("button.btn.quiet.read-jump", { type: "button", "aria-haspopup": "dialog", title: "Jump to a chapter" }, "Jump");
  const bar = h("div.read-bar", barTrack, h("div.read-bar-row", h("p.read-progress", { "aria-live": "off" }, barPct, barLeft), jumpBtn, aaBtn));
  root.append(status, flow, bar);

  const st = {
    active: false, chapters: [], loading: new Set(), topSeq: null, myP: null, moving: null, wantP: null,
    lightKey: null, forms: [], re: null, owners: new Map(), card: null, lastView: null, plateKey: new Map(), gen: 0,
    timer: 0, last: 0, topY: null, pending: null, failed: null, total: 0, appbar: null,
  };

  // ---- preferences -------------------------------------------------------------------------------
  const phone = () => matchMedia("(max-width: 640px)").matches;
  function applyPrefs() {
    const size = SIZES.includes(prefs.size) ? prefs.size : phone() ? 17 : 18;
    root.style.setProperty("--read-size", `${size}px`);
    root.style.setProperty("--read-lh", String((LINES[prefs.line] || LINES.normal)[1]));
    root.style.setProperty("--read-width", (WIDTHS[prefs.width] || WIDTHS.medium)[1]);
    root.dataset.font = prefs.font === "sans" ? "sans" : "serif";
  }
  applyPrefs();
  function setPref(k, v) {
    keepPlace(() => { prefs[k] = v; savePrefs(prefs); applyPrefs(); });
    if (k === "places") { st.lightKey = null; relight(); }
  }

  // ---- geometry ----------------------------------------------------------------------------------
  /** The line under everything pinned at the top (the app bar, the ruler, the phone's tabs): the reading viewport starts here. */
  function topLine() {
    // Where each pinned bar sits once it is stuck (its `top` plus its height), so the line is the same before the page
    // has scrolled far enough for them to stick: a jump made from the top of the page lands where reading will see it.
    let y = 0;
    const reader = root.closest(".reader");
    for (const n of [document.getElementById("appbar"), reader?.querySelector(".ruler"), reader?.querySelector(".rd-list > .view-tabs")]) {
      if (!n || !n.getClientRects().length) continue;
      const cs = getComputedStyle(n);
      if (cs.position !== "sticky" && cs.position !== "fixed") continue;
      const top = parseFloat(cs.top);
      if (Number.isFinite(top)) y = Math.max(y, top + n.offsetHeight);
    }
    return y;
  }
  const bottomLine = () => Math.min(window.innerHeight, bar.getBoundingClientRect().top || window.innerHeight);
  const blockEls = () => st.chapters.flatMap((c) => c.blocks.map((b) => b.el));
  /** The first block whose bottom lies below the top line: the block being read. null when every block is above it. */
  function topBlock(line = topLine()) {
    const els = blockEls();
    let lo = 0, hi = els.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (els[mid].getBoundingClientRect().bottom > line + 0.5) hi = mid; else lo = mid + 1; }
    return lo < els.length ? els[lo] : null;
  }
  /** Keep the block being read where it is on screen while `fn` changes what is above it (Safari has no scroll anchoring). */
  function keepPlace(fn) {
    const ref = st.active ? topBlock() : null;
    const before = ref?.getBoundingClientRect().top;
    fn();
    if (ref && ref.isConnected) {
      const d = ref.getBoundingClientRect().top - before;
      if (Math.abs(d) > 0.5) window.scrollBy(0, d);
    }
  }

  // ---- chapters ----------------------------------------------------------------------------------
  const map = () => ctx.map();
  const totalBlocks = () => map()?.cum?.length || 0;
  function chapterOfSeq(seq) { return st.chapters.find((c) => c.firstSeq <= seq && seq <= c.lastSeq) || null; }

  async function fetchChapter(q) {
    const gen = st.gen;
    const r = await api.readChapter(ctx.key(), q);
    if (gen !== st.gen) return null;
    return r;
  }
  const SLICE = 30;
  const MAX_CHAPTERS = 4;
  /** `drip`: a chapter put below the reader is added a slice of paragraphs per frame (it loads two screens ahead, so
   *  the slices are in before they are seen), so no single frame lays out a whole chapter. */
  function build(r, { drip = false } = {}) {
    const c = { n: r.chapter.n, title: r.chapter.title, firstSeq: r.chapter.firstSeq, lastSeq: r.chapter.lastSeq, prev: r.prev, next: r.next, blocks: [] };
    const title = tidyTitle(c.title) || `Section ${c.n}`;
    c.plates = h("div.read-plates");
    const blocks = (Array.isArray(r.blocks) ? r.blocks : []).filter((b) => Number.isInteger(b?.seq) && typeof b.text === "string");
    c.blocks = blocks.map((b) => {
      const el = h(b.heading ? "h3.read-h" : "p.read-p", { dataset: { seq: String(b.seq) } }, b.text);
      return { seq: b.seq, text: b.text, el, sig: "" };
    });
    c.rest = drip ? c.blocks.splice(SLICE) : [];
    c.el = h("section.read-ch", { "aria-label": title, dataset: { n: String(c.n) } },
      h("div.read-divider", h("span", title)), c.blocks.map((b) => b.el), c.plates);
    return c;
  }
  const place = timed("place", placeNow);
  function placeNow(c) {
    const i = st.chapters.findIndex((x) => x.n > c.n);
    if (i < 0) { st.chapters.push(c); flow.append(c.el); } else { st.chapters.splice(i, 0, c); flow.insertBefore(c.el, st.chapters[i + 1].el); }
    paintEnd();
    timed("light", lightChapter)(c);
    timed("plates", paintPlates)(c, { fresh: true });
    if (c.rest.length) requestAnimationFrame(() => drip(c));
  }
  function drip(c, all = false) {
    if (!c.rest.length || !c.el.isConnected) return;
    const next = c.rest.splice(0, all ? c.rest.length : SLICE);
    c.plates.before(...next.map((b) => b.el));
    c.blocks.push(...next);
    lightBlocks(next);
    if (c.rest.length) requestAnimationFrame(() => drip(c));
  }
  function paintEnd() {
    flow.querySelector(".read-end")?.remove();
    const last = st.chapters.at(-1);
    if (last && !last.next) flow.append(h("div.read-end", h("p", "The end of the book.")));
  }
  /** Open at the block holding `seq` (a fresh start: every loaded chapter goes). */
  async function openAt(seq) {
    st.gen++;
    st.chapters = []; st.plateKey = new Map(); st.loading.clear(); st.pending = null;
    clear(flow);
    clear(status, h("p.read-loading", "Opening the book…"));
    const total = totalBlocks();
    const at = Math.max(0, Math.min(seq, total - 1));
    let r;
    try { r = await fetchChapter({ seq: at }); } catch (e) { if (st.active) showError(e, () => openAt(seq)); return; }
    if (!r || !st.active) return;
    clear(status);
    place(build(r));
    scrollToSeq(seq);
    st.topSeq = seq;
    maybeLoadMore();
    paintBar();
  }
  function showError(e, retry) {
    const missing = e?.code === "no-book-file" || e?.status === 404;
    clear(status, h("div.notice.warn", icon("alert"), h("div",
      h("b", missing ? "The book file isn't here. " : "Couldn't open the book text. "),
      missing ? "Reading in Lorekeeper needs the EPUB this index was built from in the library." : e?.message || "",
      missing ? null : h("button.linkish", { type: "button", on: { click: retry } }, " Try again"))));
  }
  /** Put block `seq` at the top of the reading viewport (past the last block: the end of the book). */
  function scrollToSeq(seq) {
    const c = chapterOfSeq(seq);
    if (c?.rest.length) drip(c, true);
    const b = c?.blocks.find((x) => x.seq >= seq);
    const target = b?.el || (seq >= totalBlocks() ? flow.querySelector(".read-end") : null) || flow;
    const line = topLine() + (b ? 2 : 0);
    window.scrollTo(0, Math.max(0, target.getBoundingClientRect().top + window.scrollY - line));
    // Too near the end of what is loaded to reach the top: the page is too short until the next chapter comes. Until
    // then the scroll says nothing about the position, and the jump is made again once there is room.
    const short = Math.abs(target.getBoundingClientRect().top - line) > 2 && st.chapters.at(-1)?.next;
    st.pending = short ? seq : null;
  }
  async function loadNeighbour(dir) {
    const edge = dir > 0 ? st.chapters.at(-1) : st.chapters[0];
    const want = dir > 0 ? edge?.next : edge?.prev;
    if (!want || st.loading.has(want.n) || st.chapters.some((c) => c.n === want.n)) return;
    if (!roomFor(dir)) return;
    st.loading.add(want.n);
    let r;
    try { r = await fetchChapter({ chapter: want.n }); } catch { st.loading.delete(want.n); if (dir > 0) st.pending = null; return; }
    st.loading.delete(want.n);
    if (!r || !st.active || st.chapters.some((c) => c.n === r.chapter.n)) return;
    const c = timed("build", build)(r, { drip: dir > 0 });
    if (dir < 0) keepPlace(() => place(c)); else place(c);
    trim(dir);
    if (dir > 0 && st.pending != null) scrollToSeq(st.pending);
  }
  /** At most MAX_CHAPTERS stay in the page: the one farthest from the reader goes (kept in place when it is above). While
   *  that one is the chapter being read, nothing more is loaded on that side until the reader moves on, so a run of
   *  short chapters never keeps loading. The one exception: the text in the page ends on screen (forward) or starts
   *  below the top line (back), so the reader could not scroll on without the next chapter; that is a screenful of text
   *  at most. */
  function roomFor(dir) {
    if (st.chapters.length < MAX_CHAPTERS) return true;
    const drop = dir > 0 ? st.chapters[0] : st.chapters.at(-1);
    if (drop !== chapterOfSeq(st.topSeq ?? -1)) return true;
    const r = flow.getBoundingClientRect();
    return dir > 0 ? r.bottom <= window.innerHeight : r.top >= topLine();
  }
  function trim(dir) {
    while (st.chapters.length > MAX_CHAPTERS) {
      const drop = dir > 0 ? st.chapters[0] : st.chapters.at(-1);
      const cur = chapterOfSeq(st.topSeq ?? -1);
      if (drop === cur) break;
      if (dir > 0) keepPlace(() => { drop.el.remove(); st.chapters.shift(); });
      else { drop.el.remove(); st.chapters.pop(); paintEnd(); }
    }
  }
  function maybeLoadMore() {
    if (!st.active || !st.chapters.length) return;
    const r = flow.getBoundingClientRect();
    if (r.bottom - window.innerHeight < window.innerHeight * 2) loadNeighbour(1);
    if (r.top > -window.innerHeight) loadNeighbour(-1);
  }

  // ---- the position follows the scroll -----------------------------------------------------------
  const check = timed("check", checkNow);
  function checkNow() {
    st.last = performance.now();
    if (!st.active || !st.chapters.length || document.querySelector("dialog[open]")) return;
    if (st.pending != null) { maybeLoadMore(); return; }
    const line = topLine();
    const el = topBlock(line);
    st.topY = el ? el.getBoundingClientRect().top : null;
    const seq = el ? +el.dataset.seq : (st.chapters.at(-1).blocks.at(-1)?.seq ?? st.chapters.at(-1).firstSeq - 1) + 1;
    maybeLoadMore();
    if (seq === st.topSeq) return;
    st.topSeq = seq;
    rememberAt(seq);
    commit(map().blockStart(seq));
    paintBar();
  }
  const onScroll = () => {
    if (st.timer) return;
    const wait = Math.max(0, THROTTLE - (performance.now() - st.last));
    st.timer = setTimeout(() => { st.timer = 0; check(); }, wait);
  };
  const onScrollEnd = () => { clearTimeout(st.timer); st.timer = 0; check(); };
  /** Hand the position to the Reader, one move at a time; a refused move (typed text kept, Book Orbit's line) brings the
   *  text back to where the Reader's position is. */
  async function commit(p) {
    st.myP = p;
    if (Math.abs(p - ctx.p()) < 1e-12) return;
    if (st.moving) { st.wantP = p; return; }
    st.moving = true;
    try { await ctx.moveTo(p); } finally { st.moving = false; }
    if (!st.active) return;
    if (st.wantP != null) { const next = st.wantP; st.wantP = null; return commit(next); }
    if (Math.abs(ctx.p() - p) > 1e-9) follow();
  }
  /** The Reader's position moved by another way (the ruler, a step, Jump, a Book Orbit sync, a refusal): go there. */
  function follow() {
    const p = ctx.p();
    st.myP = p;
    const m = map();
    const seq = p >= 1 - 1e-12 ? totalBlocks() : m.seqAt(p);
    if (seq == null) return;
    st.topSeq = seq;
    rememberAt(seq);
    if (chapterOfSeq(seq) || (seq >= totalBlocks() && st.chapters.at(-1) && !st.chapters.at(-1).next)) { scrollToSeq(seq); maybeLoadMore(); }
    else openAt(seq);
    paintBar();
  }
  /** The exact block the reader was at, so reopening (whose address rounds the position) finds the same paragraph. */
  function rememberAt(seq) {
    try { const all = readAt(); all[ctx.key()] = { seq, p: map().blockStart(seq) }; localStorage.setItem(AT_KEY, JSON.stringify(all)); } catch {}
  }
  function startSeq() {
    const p = ctx.p();
    const m = map();
    const saved = readAt()[ctx.key()];
    // The address keeps the position to a tenth of a percent: the remembered block wins while it agrees with that, and
    // never when it lies past a spoiler line (Book Orbit's position, not yet allowed ahead): going past that asks first,
    // and opening the text is not an answer to that question.
    const line = ctx.line();
    const pastLine = (q) => line != null && q > line + 1e-9;
    if (saved && Number.isInteger(saved.seq) && saved.seq < totalBlocks() && Math.abs(saved.p - p) < 0.0011
      && !pastLine(m.blockStart(saved.seq))) return saved.seq;
    return p >= 1 - 1e-12 ? totalBlocks() : m.seqAt(p) ?? 0;
  }

  // ---- lit names ---------------------------------------------------------------------------------
  /** Where a visible name is revealed, as a fraction of the book (the end of the block its anchor resolves to). */
  function nameAt(id, value) {
    const m = map();
    for (const d of ctx.priorDocs()) if ((d?.entities || []).some((e) => e.id === id && (e.names || []).some((n) => n.value === value))) return -Infinity;
    if (!st.ords || st.ords.doc !== ctx.doc()) st.ords = { doc: ctx.doc(), map: new Map((ctx.doc().anchors || []).map((a, i) => [a.id, i])) };
    const ent = (ctx.doc().entities || []).find((e) => e.id === id);
    let thr = Infinity;
    for (const n of ent?.names || []) {
      if (n.value !== value) continue;
      const i = st.ords.map.get(n.from);
      if (i != null) thr = Math.min(thr, m.eff[i]);
    }
    if (thr !== Infinity || (ent?.names || []).some((n) => n.value === value)) return thr;
    // Not this book's own name: in a series it comes from a finished earlier book (or its carried copy). Otherwise it is
    // lit only from the position on, which is always safe.
    return ctx.series() ? -Infinity : ctx.p();
  }
  const relight = timed("relight", relightNow);
  function relightNow() {
    const v = ctx.view();
    if (!v) return;
    if (v !== st.lastView || st.lightKey == null) {
      st.lastView = v;
      const types = new Set([...ALWAYS, ...(prefs.places ? WITH_TOGGLE : [])]);
      const forms = litForms(v, types, nameAt);
      const key = forms.map(([f, o]) => `${f}\u0001${[...o].map(([id, t]) => `${id}:${t}`).join(",")}`).join("\u0002");
      if (key === st.lightKey) return;
      st.lightKey = key;
      st.forms = forms;
      st.owners = new Map(forms);
      st.re = matcher(forms);
      st.types = new Map(v.entities.map((e) => [e.id, e.type]));
      for (const c of st.chapters) lightChapter(c);
    }
  }
  function lightChapter(c) { lightBlocks(c.blocks); }
  function lightBlocks(blocks) {
    const m = map();
    for (const b of blocks) {
      const spans = litSpans(b.text, st.re, st.owners, m.blockEnd(b.seq));
      const sig = spans.map((x) => `${x.start}-${x.end}-${x.ids.join("+")}`).join(" ");
      if (sig === b.sig) continue;
      b.sig = sig;
      const parts = [];
      let at = 0;
      for (const x of spans) {
        if (x.start > at) parts.push(b.text.slice(at, x.start));
        const type = st.types?.get(x.ids[0]) || "person";
        parts.push(h(`span.rd-name.t-${type}`, { role: "button", tabindex: "0", dataset: { ids: x.ids.join(" ") } }, b.text.slice(x.start, x.end)));
        at = x.end;
      }
      if (at < b.text.length) parts.push(b.text.slice(at));
      // A card opened from a name in this block keeps its name to return focus to; it may be a new element now.
      clear(b.el, parts);
    }
  }

  // ---- chapter plates ----------------------------------------------------------------------------
  // A chapter's plate sits at its end, before the next chapter: a generated plate is revealed at the chapter's last anchor,
  // so it comes in as the reader crosses into the next chapter, and scrolling up a little to look at it does not move the
  // position back (it is not a paragraph). At the chapter's start it could only be seen by going back into the chapter,
  // which takes it away again.
  /** `fresh`: the chapter was just put in the page (a chapter put above the reader is kept in place by its caller). */
  function paintPlates(c, { fresh = false } = {}) {
    const m = map();
    const lo = m.blockStart(c.firstSeq), hi = m.blockEnd(c.lastSeq);
    const inChapter = (id) => { const at = m.anchorPos(id); return Number.isFinite(at) && at > lo + 1e-12 && at <= hi + 1e-12; };
    // A plate belongs to the chapter it was drawn for (ctx.plateChapter: its own recap's anchor is often the next
    // chapter's first long paragraph), else to the chapter its anchor is in.
    const mine = (p) => {
      const ch = ctx.plateChapter ? ctx.plateChapter(p) : null;
      return ch ? c.firstSeq <= ch.start && ch.start <= c.lastSeq : inChapter(p.rec.from);
    };
    const pics = ctx.plates().filter(mine);
    const key = pics.map((p) => p.rec.id).join(",");
    if (st.plateKey.get(c.n) === key || (!key && !c.plates.firstChild)) { st.plateKey.set(c.n, key); return; }
    st.plateKey.set(c.n, key);
    const title = tidyTitle(c.title) || `Section ${c.n}`;
    // Said only when it is so: every passage the picture was written from lies in this chapter (a visible picture's
    // anchors are all at or before the position).
    const own = pics.length && pics.every((p) => [p.rec.from, ...(Array.isArray(p.rec.sources) ? p.rec.sources : [])].every(inChapter));
    const node = pics.length ? ctx.platesNode(pics, title) : null;
    const paint = () => clear(c.plates, node, node && own ? h("p.read-plate-note", "Drawn from this chapter, up to where you are.") : null);
    if (fresh) paint(); else keepPlace(paint);
  }

  // ---- the card ----------------------------------------------------------------------------------
  function cardBody(ids) {
    const ents = ids.map((id) => ctx.entity(id)).filter(Boolean);
    if (!ents.length) return null;
    return ents.map((e) => {
      const src = ctx.portrait(e.id);
      const line = describe(e);
      const img = src ? h("img.rd-card-face", { src, alt: "", loading: "lazy", decoding: "async", draggable: "false", on: { error: (ev) => ev.target.remove() } }) : null;
      return h(`div.rd-card-ent.t-${e.type}`,
        h("div.rd-card-head", img, h("div.rd-card-names", h("h3.rd-card-name", { tabindex: "-1" }, e.name), typeTag(e.type))),
        line ? h("p.rd-card-line", line) : null,
        h("div.rd-card-actions",
          h("button.btn.primary", { type: "button", on: { click: () => { const from = st.card ? cardFrom(st.card) : null; closeCard(false); ctx.openEntry(e.id, from); } } }, "Open entry"),
          ctx.canAsk() ? h("button.btn.quiet", { type: "button", on: { click: () => { closeCard(false); ctx.ask(`What do I know about ${e.name} so far?`); } } }, `Ask about ${e.name}`) : null));
    });
  }
  /** The name a card was opened from, found again when a relight has rebuilt its paragraph (by block and place in it). */
  function cardFrom(c) {
    if (c.from?.isConnected) return c.from;
    const block = flow.querySelector(`[data-seq="${c.seq}"]`);
    const same = block ? [...block.querySelectorAll(".rd-name")].filter((n) => n.dataset.ids === c.ids.join(" ")) : [];
    c.from = same[Math.min(c.nth, same.length - 1)] || block || null;
    return c.from;
  }
  function openCard(nameEl) {
    const seq = +nameEl.closest("[data-seq]")?.dataset.seq;
    const nth = [...(nameEl.closest("[data-seq]")?.querySelectorAll(".rd-name") || [])].filter((n) => n.dataset.ids === nameEl.dataset.ids).indexOf(nameEl);
    const ids = (nameEl.dataset.ids || "").split(" ").filter(Boolean);
    const body = cardBody(ids);
    if (!body) return;
    closeCard(false);
    const title = ctx.entity(ids[0])?.name || nameEl.textContent;
    if (phone()) {
      const sh = sheet({ title, content: h("div.rd-card.in-sheet", body), className: "rd-card-sheet", onClose: () => { if (st.card?.sheet === sh) st.card = null; } });
      st.card = { ids, from: nameEl, seq, nth, sheet: sh };
      return;
    }
    const pop = h("div.rd-card.pop", { role: "dialog", "aria-label": title },
      h("button.icon-btn.rd-card-close", { type: "button", "aria-label": "Close", title: "Close", on: { click: () => closeCard(true) } }, icon("x")), body);
    root.append(pop);
    st.card = { ids, from: nameEl, seq, nth, pop };
    placeCard();
    pop.querySelector(".rd-card-name")?.focus({ preventScroll: true });
  }
  /** Beside the name: under its line, else above it, never past the reading column's edges. */
  function placeCard() {
    const c = st.card;
    if (!c?.pop) return;
    const box = root.getBoundingClientRect();
    const r = (cardFrom(c) || flow).getBoundingClientRect();
    const w = Math.min(340, box.width - 16);
    c.pop.style.width = `${w}px`;
    const left = Math.max(8, Math.min(r.left - box.left, box.width - w - 8));
    const ph = c.pop.offsetHeight;
    const below = r.bottom + 8 + ph < bottomLine();
    c.pop.style.left = `${left}px`;
    c.pop.style.top = `${(below ? r.bottom + 8 : Math.max(topLine() + 8, r.top - 8 - ph)) - box.top}px`;
  }
  function closeCard(focus = true) {
    const c = st.card;
    if (!c) return;
    st.card = null;
    if (c.sheet) c.sheet.close(true);
    c.pop?.remove();
    if (focus) cardFrom(c)?.focus({ preventScroll: true });
  }
  /** The position moved: the card shows the entry as it is now, or closes when the entry is no longer revealed. */
  function refreshCard() {
    const c = st.card;
    if (!c) return;
    const body = cardBody(c.ids);
    if (!body) { closeCard(false); return; }
    const host = c.sheet ? c.sheet.el.querySelector(".rd-card") : c.pop;
    const had = host?.contains(document.activeElement) ? document.activeElement.textContent : null;
    if (c.sheet) clear(host, body);
    else { clear(host, host.firstChild, body); placeCard(); }
    if (had != null) [...host.querySelectorAll("button, h3")].find((n) => n.textContent === had)?.focus({ preventScroll: true });
  }
  flow.addEventListener("click", (ev) => { const n = ev.target.closest?.(".rd-name"); if (n) openCard(n); });
  flow.addEventListener("keydown", (ev) => {
    const n = ev.target.closest?.(".rd-name");
    if (n && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); openCard(n); }
  });
  const onDocDown = (ev) => { if (st.card?.pop && !st.card.pop.contains(ev.target) && !ev.target.closest?.(".rd-name")) closeCard(false); };

  // ---- the progress line -------------------------------------------------------------------------
  function paintBar() {
    const p = ctx.p();
    root.style.setProperty("--read-f", String(p));
    barPct.textContent = `${p >= 0.995 && p < 1 ? 99 : Math.floor(p * 100)}% of the book`;
    const m = map();
    const seq = st.topSeq;
    const c = seq != null ? chapterOfSeq(seq) : null;
    let left = "";
    if (c && m?.cum) {
      const words = m.cum[c.lastSeq] - (seq > 0 ? m.cum[seq - 1] : 0);
      const min = Math.ceil(words / WPM);
      left = words <= 0 ? "" : min <= 1 ? "Under a minute left in this chapter" : `${min} minutes left in this chapter`;
    } else if (seq != null && seq >= totalBlocks()) left = "Finished";
    barLeft.textContent = left;
    const title = c ? tidyTitle(c.title) : null;
    if (st.active && phone()) setAppbarTitle(title);
  }

  // ---- the Aa menu -------------------------------------------------------------------------------
  function prefsPanel() {
    const seg = (label, choices, k) => h("div.read-pref", h("span.read-pref-label", label),
      h("div.read-seg", { role: "group", "aria-label": label }, Object.entries(choices).map(([v, [text]]) =>
        h("button.read-seg-btn", { type: "button", "aria-pressed": String(prefs[k] === v), on: { click: (ev) => {
          setPref(k, v);
          ev.currentTarget.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === ev.currentTarget)));
        } } }, text))));
    const size = () => (SIZES.includes(prefs.size) ? prefs.size : phone() ? 17 : 18);
    const sizeOut = h("output.read-size-out", `${size()}px`);
    const step = (d) => { const n = Math.min(22, Math.max(15, size() + d)); setPref("size", n); sizeOut.textContent = `${n}px`; };
    const places = h("input#read-places", { type: "checkbox", checked: Boolean(prefs.places), on: { change: (ev) => setPref("places", ev.target.checked) } });
    return h("div.read-prefs",
      h("div.read-pref", h("span.read-pref-label", "Text size"), h("div.read-size",
        h("button.btn.quiet.read-size-btn", { type: "button", "aria-label": "Smaller text", on: { click: () => step(-1) } }, "A−"), sizeOut,
        h("button.btn.quiet.read-size-btn", { type: "button", "aria-label": "Larger text", on: { click: () => step(1) } }, "A+"))),
      seg("Typeface", { serif: ["Book serif"], sans: ["Manrope"] }, "font"),
      seg("Line spacing", LINES, "line"),
      seg("Width", WIDTHS, "width"),
      h("label.read-pref.read-check", { for: "read-places" }, places, h("span", "Light places and groups")));
  }
  function openPrefs() {
    closeCard(false);
    if (phone()) { sheet({ title: "Text", content: prefsPanel() }); return; }
    if (st.prefsPop) { closePrefs(); return; }
    const pop = h("div.rd-card.pop.read-prefs-pop", { role: "dialog", "aria-label": "Text settings" },
      h("button.icon-btn.rd-card-close", { type: "button", "aria-label": "Close", title: "Close", on: { click: () => closePrefs(true) } }, icon("x")),
      h("h3.rd-card-name", { tabindex: "-1" }, "Text"), prefsPanel());
    root.append(pop);
    st.prefsPop = pop;
    aaBtn.setAttribute("aria-expanded", "true");
    const b = aaBtn.getBoundingClientRect(), box = root.getBoundingClientRect();
    const w = Math.min(340, box.width - 16);
    pop.style.width = `${w}px`;
    pop.style.left = `${Math.max(8, b.right - box.left - w)}px`;
    pop.style.top = `${b.top - box.top - pop.offsetHeight - 8}px`;
    pop.querySelector(".rd-card-name").focus({ preventScroll: true });
  }
  function closePrefs(focus = false) {
    st.prefsPop?.remove(); st.prefsPop = null;
    aaBtn.setAttribute("aria-expanded", "false");
    if (focus) aaBtn.focus();
  }
  aaBtn.addEventListener("click", openPrefs);
  jumpBtn.addEventListener("click", () => { closeCard(false); closePrefs(); ctx.jump(); });
  const onPrefsDown = (ev) => { if (st.prefsPop && !st.prefsPop.contains(ev.target) && !aaBtn.contains(ev.target)) closePrefs(); };

  // ---- keys --------------------------------------------------------------------------------------
  function onKey(ev) {
    if (!st.active || ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    if (ev.key === "Escape") {
      if (st.prefsPop) { ev.preventDefault(); closePrefs(true); return; }
      if (st.card?.pop) { ev.preventDefault(); closeCard(true); return; }
      return;
    }
    const t = ev.target;
    if (document.querySelector("dialog[open]") || t.closest?.("input, textarea, select, [contenteditable], .rd-card, .entry-pane, .read-bar")) return;
    const page = Math.max(80, bottomLine() - topLine() - 48);
    const by = { PageDown: page, PageUp: -page, " ": ev.shiftKey ? -page : page }[ev.key];
    if (by == null) return;
    if (ev.key === " " && t.closest?.("button, a, [role='button'], summary")) return;
    ev.preventDefault();
    window.scrollBy({ top: by, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  }

  // ---- life cycle --------------------------------------------------------------------------------
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    // The column changed width (the entry pane opened, the window was resized): the block being read stays where it was.
    if (!st.active || st.topSeq == null) return;
    const b = chapterOfSeq(st.topSeq)?.blocks.find((x) => x.seq === st.topSeq);
    if (b && st.topY != null) { const d = b.el.getBoundingClientRect().top - st.topY; if (Math.abs(d) > 0.5) window.scrollBy(0, d); }
    placeCard();
  }) : null;
  function activate() {
    st.active = true;
    st.appbar = document.getElementById("appbar-title")?.textContent ?? null;
    document.documentElement.classList.add("lk-reading");
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("scrollend", onScrollEnd);
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDocDown, true);
    document.addEventListener("pointerdown", onPrefsDown, true);
    ro?.observe(flow);
    applyPrefs();
    st.lightKey = null; st.lastView = null;
    relight();
    const seq = startSeq();
    st.topSeq = seq; st.myP = ctx.p();
    // The remembered block, when the address rounded the position into the block before it: the position is put at that
    // block's start quietly (not a move by hand, so a Book Orbit sync still applies).
    const exact = seq < totalBlocks() ? map().blockStart(seq) : 1;
    const line = ctx.line();
    if (map().seqAt(ctx.p()) !== seq && seq < totalBlocks() && Math.abs(exact - ctx.p()) < 0.0011 && (line == null || exact <= line + 1e-9)) { st.myP = exact; queueMicrotask(() => { const l = ctx.line(); if (st.active && st.myP === exact && (l == null || exact <= l + 1e-9)) ctx.settle(exact); }); }
    const loaded = chapterOfSeq(seq);
    if (loaded) { requestAnimationFrame(() => { if (st.active) { scrollToSeq(seq); maybeLoadMore(); } }); }
    else openAt(seq);
    paintBar();
  }
  function leave() {
    if (!st.active) return;
    st.active = false;
    clearTimeout(st.timer); st.timer = 0;
    closeCard(false); closePrefs();
    document.documentElement.classList.remove("lk-reading");
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("scrollend", onScrollEnd);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("pointerdown", onDocDown, true);
    document.removeEventListener("pointerdown", onPrefsDown, true);
    ro?.disconnect();
    setAppbarTitle(null);
  }

  return {
    root,
    /** Called on every Reader repaint while the Read tab is chosen. The text is never rebuilt: only what changed is. */
    mount: timed("mount", function (host) {
      for (const n of [...host.childNodes]) if (n !== root) n.remove();
      if (root.parentNode !== host) host.append(root);
      if (!st.active) { activate(); return; }
      if (st.myP != null && Math.abs(ctx.p() - st.myP) > 1e-9 && !st.moving) follow();
      relight();
      for (const c of st.chapters) paintPlates(c);
      refreshCard();
      paintBar();
    }),
    /** Another tab was chosen. */
    leave,
    /** Another index was opened: nothing loaded for the last one stays. */
    reset() {
      leave();
      st.gen++;
      st.chapters = []; st.plateKey = new Map(); st.lightKey = null; st.lastView = null; st.topSeq = null; st.myP = null; st.ords = null;
      clear(flow); clear(status);
    },
    dispose() { this.reset(); root.remove(); },
  };
}
