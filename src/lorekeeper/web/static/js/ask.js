import { api } from "./api.js";
import { clear, h, loading } from "./dom.js";

// The Reader's Ask tab: a question answered by the server from the index's notes at the reader's position.
// The conversation lives in this closure only (never localStorage, never the URL). Every answer remembers the
// ids the view held when it was asked; when the view stops holding any of them (position moved back, a
// "Finished" box unticked, another book or index) the whole thread is cleared, because a later answer or
// question could carry what the earlier position never revealed.

const MAX_Q = 500;
const KEEP_TURNS = 6;
export const CLEARED = "Cleared, because your position moved back.";

/** The paragraphs of a chapter reply (`api.readChapter`) a passage sheet may show: the cited ones, `seq..end`, that are
 *  also before the frontier block at the time of showing. */
export function passageBlocks(blocks, c, frontier) {
  if (!Number.isInteger(frontier) || !Number.isInteger(c?.seq)) return [];
  const end = Number.isInteger(c.end) ? c.end : c.seq;
  return (Array.isArray(blocks) ? blocks : []).filter((b) => Number.isInteger(b?.seq) && typeof b.text === "string"
    && b.seq >= c.seq && b.seq <= end && b.seq < frontier);
}

// Where answers come from: the index's notes, the book's own text before the position, or both. Offered only when the
// book file is here; remembered per viewer.
export const SOURCES = ["both", "index", "book"];
export const SOURCE_LABEL = { both: "Index and book", index: "Index only", book: "Book only" };
const SOURCE_KEY = "lk-ask-source";
/** The remembered choice, "both" when none (or an unknown value) is stored. */
export function readSource(storage = globalThis.localStorage) {
  try { const v = storage?.getItem(SOURCE_KEY); return SOURCES.includes(v) ? v : "both"; } catch { return "both"; }
}
function saveSource(v) { try { localStorage.setItem(SOURCE_KEY, v); } catch {} }

/** A passage cite opens only when it lies wholly before the frontier block of the book being read (the block being read
 *  never counts) and is from that book: an earlier book's passage, a bad seq or no frontier means plain text.
 *  frontier: the first seq that is not yet read (the block count at the end of the book), or null without the book file. */
export function passageOpenable(c, { book = null, frontier = null } = {}) {
  if (!c || c.kind !== "passage" || !Number.isInteger(frontier)) return false;
  if ((c.book ?? null) !== (book ?? null)) return false;
  if (!Number.isInteger(c.seq) || c.seq < 0 || c.seq >= frontier) return false;
  if (c.end != null && (!Number.isInteger(c.end) || c.end < c.seq || c.end >= frontier)) return false;
  return true;
}
/** "Chapter VI" for a passage of the book being read (chapter(c) gives the label from the book's own number, else
 *  null), else its title; an earlier book's passage is prefixed with that book's short title. A passage of the book being
 *  read that is not wholly before `frontier` is only "The book": a label never names a chapter past the position (the
 *  server sends none; this is the second line of defence). */
export function passageLabel(c, { book = null, frontier = null, chapter = () => null, short = (k) => k } = {}) {
  if ((c?.book ?? null) === (book ?? null) && !passageOpenable(c, { book, frontier })) return "The book";
  const title = typeof c?.chapter === "string" ? c.chapter.replace(/\s+/g, " ").trim() : "";
  const ch = chapter(c) || title || null;
  const other = c?.book != null && c.book !== (book ?? null);
  if (other) { const b = short(c.book) || "An earlier book"; return ch ? `${b}, ${ch}` : b; }
  return ch || "The book";
}

/** Everything a gated view shows that could be a spoiler if the position moved back: ids of entries, profiles, details,
 *  earlier impressions, connections (current and replaced), events and chapter recaps, and every name and alias of
 *  an entry (a later name has its own reveal point while the entry id stays). The story recap is left out on purpose:
 *  the view holds only the newest one, so moving forward replaces it. */
export function viewIds(view) {
  const ids = new Set();
  for (const e of view?.entities || []) {
    ids.add(e.id);
    ids.add(`${e.id}\u0000${e.name}`);
    for (const a of e.aliases || []) ids.add(`${e.id}\u0000${a}`);
    if (e.profile) ids.add(e.profile.id);
    for (const f of e.facets || []) ids.add(f.id);
    for (const f of e.history || []) ids.add(f.id);
  }
  for (const r of [...(view?.relations || []), ...(view?.relationHistory || [])]) ids.add(r.id);
  for (const ev of view?.events || []) ids.add(ev.id);
  for (const c of view?.chapters || []) ids.add(c.id);
  return ids;
}
const covers = (now, then) => { for (const id of then) if (!now.has(id)) return false; return true; };

/** What was on screen when something was asked or typed: the ids above and where the view was computed.
 *  pos: { key, book, rank (the book's place in the reading order used), ord (the anchor's ordinal in that book), p (the word
 *  fraction of that book), order, finished: [book keys] }. */
export function snapshot(view, pos) {
  return { ids: viewIds(view), key: pos.key ?? null, series: pos.series ?? null, book: pos.book ?? null, rank: pos.rank ?? 0, ord: pos.ord ?? -1,
    p: Number.isFinite(pos.p) ? pos.p : null, order: pos.order ?? null, finished: [...(pos.finished || [])] };
}

/** True when the view now reveals less than it did at `old` (so what was asked there must go): something it held is gone,
 *  another index or reading order, an earlier book, an earlier anchor in the same book, or a finished book unticked.
 *  Moving forward only adds, so it keeps the thread. */
export function revealsLess(old, view, pos) {
  if (!old) return false;
  // Each book of a series has its own key, so a different key clears only outside one series; inside it the rules below decide.
  const sameSeries = old.series != null && old.series === (pos.series ?? null);
  if ((old.key !== (pos.key ?? null) && !sameSeries) || old.order !== (pos.order ?? null)) return true;
  const rank = pos.rank ?? 0, ord = pos.ord ?? -1;
  if (rank < old.rank) return true;
  if (rank === old.rank && (pos.book ?? null) === old.book && ord < old.ord) return true;
  // Two positions inside one anchor's span differ in which paragraphs of the book are before them: any move back counts.
  if (rank === old.rank && (pos.book ?? null) === old.book && old.p != null && Number.isFinite(pos.p) && pos.p < old.p - 1e-12) return true;
  const fin = new Set(pos.finished || []);
  if (old.finished.some((b) => !fin.has(b))) return true;
  return !covers(viewIds(view), old.ids);
}

/** The draft's snapshot after the field changed: taken when it goes from empty to non-empty (typed by hand, prefilled
 *  or restored), kept while text stays, dropped when the field is emptied. `take` makes a snapshot of the view now. */
export function draftAfterInput(draft, text, take) {
  if (!String(text || "").trim()) return null;
  return draft || take();
}
/** True when any snapshot (answers, the pending question, the draft) shows more than the view does now. */
export const anyRevealsLess = (snaps, view, pos) => snaps.filter(Boolean).some((x) => revealsLess(x, view, pos));

/** The turns sent as history: answered ones, the last `keep`. A question the server refused for a name the reader has not
 *  met (`unknown name`, `refused: outside knowledge`) stays on screen but is never sent again. */
const NOT_SENT = new Set(["unknown name", "refused: outside knowledge"]);
export function historyFor(turns, keep = KEEP_TURNS) {
  return turns.filter((t) => t.state === "ok" && !NOT_SENT.has(t.a?.note)).slice(-keep)
    // Passage cites stay out: what an earlier answer quoted from the book is never named again (the server ignores them too).
    .map((t) => ({ question: t.q, answer: t.a.answer, cites: (t.a.cites || []).filter((c) => c?.kind !== "passage").map((c) => c.id) }));
}

/**
 * ctx: key() the library key; position() { anchor, finished, order, key, book, rank, ord } the view on screen was computed with;
 * view() the gated view; examples() three question strings; canOpen(cite), open(cite) for the sources; citeLabel(cite) a label
 * the Reader writes (passages), else the server's; hasBook() true when the book file is here (the source choice is offered).
 */
export function createAsk(ctx) {
  let info = null;                 // GET /api/ask
  let infoFor = 0;                 // request counter, so a late status never overwrites a newer one
  let turns = [];                  // { q, state: "pending" | "ok" | "error", a, err, el }
  let draft = null;                // snapshot for text put in the field by prefill or restored after a failure
  let busy = null;                 // { turn, controller, ids, key }
  let clearedLine = "";
  let disposed = false;
  let chosen = readSource();       // the person's choice, used only while the book file is here
  const source = () => (ctx.hasBook?.() ? chosen : "index");

  const log = h("div.ask-log", { role: "log", "aria-live": "polite", "aria-label": "Questions and answers" });
  const cleared = h("p.ask-cleared", { role: "status" });
  const empty = h("div.ask-empty");
  const unavailable = h("div.ask-off", { role: "status" });
  const field = h("textarea.input.ask-field", { rows: "1", maxlength: String(MAX_Q), placeholder: "Ask about this book so far",
    "aria-label": "Your question", enterkeyhint: "send" });
  const count = h("span.ask-count.hint", { "aria-hidden": "true" });
  const send = h("button.btn.primary.ask-send", { type: "submit" }, "Ask");
  const composer = h("form.ask-composer", { on: { submit: (ev) => { ev.preventDefault(); ask(field.value); } } },
    h("div.ask-row", field, send), count);
  const via = h("p.ask-via.hint");
  const srcBtns = SOURCES.map((v) => h("button", { type: "button", dataset: { src: v }, on: { click: () => pick(v) } }, SOURCE_LABEL[v]));
  const srcLine = h("p.ask-src-line.hint");
  const srcBox = h("div.ask-src", h("div.seg.ask-seg", { role: "group", "aria-label": "Answer from" }, srcBtns), srcLine);
  const root = h("div.ask", srcBox, log, cleared, empty, unavailable, composer, via);

  function pick(v) {
    if (!SOURCES.includes(v) || v === chosen) return;
    chosen = v; saveSource(v);
    paintSource(); paintVia(); paintEmpty();
  }
  function paintSource() {
    const show = Boolean(ctx.hasBook?.()) && Boolean(info?.available);
    srcBox.hidden = !show;
    if (!show) return;
    const now = source();
    for (const b of srcBtns) b.setAttribute("aria-pressed", String(b.dataset.src === now));
    srcLine.textContent = now === "index" ? ""
      : info.local ? "Passages of the book are read by the local model." : `Passages of the book are sent to ${company(info)}.`;
  }

  function grow() {
    field.style.height = "auto";
    const line = parseFloat(getComputedStyle(field).lineHeight) || 20;
    const pad = field.offsetHeight - field.clientHeight;
    field.style.height = `${Math.min(field.scrollHeight + pad, line * 4 + 16)}px`;
    count.textContent = field.value.length >= 420 ? `${MAX_Q - field.value.length} left` : "";
  }
  field.addEventListener("input", () => { draft = draftAfterInput(draft, field.value, () => snapshot(ctx.view(), ctx.position())); grow(); });
  field.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); ask(field.value); }
  });

  // ---- where the question goes -------------------------------------------------------------------

  const company = (i) => (i.provider === "claude" ? "Anthropic" : i.label || "the provider");
  function paintVia() {
    if (!info?.available) { clear(via); return; }
    const m = info.modelLabel || info.model, src = source();
    clear(via, info.local ? `Answered on this computer by ${m}.`
      : src === "both" ? `Answered by ${m} from the index's notes and the book's own text, up to where you are.`
      : src === "book" ? `Answered by ${m} from the book's own text, up to where you are.`
      : `Answered by ${m} from the index's notes. Your question and those notes are sent to ${company(info)}; the book's text is not.`);
  }
  function paintState() {
    const off = info && !info.available;
    unavailable.hidden = !off;
    composer.hidden = !info || off;
    via.hidden = !info || off;
    if (off) {
      clear(unavailable, info.reason === "ollama-busy"
        ? h("p", "The local model is busy building an index. Try again when the run finishes.")
        : h("p", "Ask needs a model to write the answers. Add a key under ", h("a", { href: "#/settings/cloud" }, "Cloud models in Settings"),
          ", or start Ollama on this computer."));
    } else clear(unavailable);
    paintSource();
    paintVia();
    paintEmpty();
  }
  let statusAt = 0;
  async function refreshStatus() {
    statusAt = Date.now();
    const mine = ++infoFor;
    let next;
    try { next = await api.askStatus(); } catch { next = { available: false, reason: "no-model" }; }
    if (disposed || mine !== infoFor) return;
    const changed = JSON.stringify(next) !== JSON.stringify(info);
    info = next;
    if (changed) { paintState(); ctx.onStatus?.(info); }
  }

  function paintEmpty() {
    const show = !turns.length && !busy && info?.available;
    empty.hidden = !show;
    if (!show) return clear(empty);
    const ex = ctx.examples();
    const src = source();
    clear(empty, h("p", `Ask about anyone or anything you've read so far. ${src === "both" ? "Answers come from this index and the book's own text, up to your position."
      : src === "book" ? "Answers come from the book's own text, up to your position." : "Answers come from this index up to your position, not from the book's text."}`),
      ex.length ? h("div.ask-examples", ex.map((q) => h("button.ask-example", { type: "button", on: { click: () => ask(q) } }, q))) : null);
  }

  // ---- the thread --------------------------------------------------------------------------------

  function citeLinks(cites, reg) {
    const seen = new Set();
    const list = (cites || []).filter((c) => { const k = `${c.kind}:${c.id}`; if (seen.has(k)) return false; seen.add(k); return true; });
    if (!list.length) return null;
    const label = (c) => String(ctx.citeLabel?.(c) || c.label || c.kind || "source");
    const items = list.map((c) => {
      const plain = () => h("span.ask-cite", { title: label(c) }, label(c));
      if (!ctx.canOpen(c)) return h("li", plain());
      const li = h("li");
      let btn;
      btn = h("button.linkish.ask-cite", { type: "button", title: label(c), on: { click: () => {
        if (ctx.canOpen(c)) ctx.open(c);
        else btn.replaceWith(plain());   // no longer in the view on screen
      } } }, label(c));
      li.append(btn);
      reg.push({ btn, c, plain });
      return li;
    });
    return h("div.ask-from", h("span.ask-from-lbl", "From:"), h("ul.ask-cites", items));
  }

  function paintTurn(t) {
    const body = [h("p.ask-q", t.q)];
    if (t.state === "pending") {
      body.push(h("div.ask-wait", loading(t.source === "index" ? "Reading the index…" : t.source === "book" ? "Reading the book…" : "Reading the index and the book…"),
        h("button.btn.quiet.ask-cancel", { type: "button", on: { click: cancel } }, "Cancel")));
    } else if (t.state === "error") {
      body.push(h("p.ask-err", { role: "alert" }, t.err, " ",
        h("button.linkish", { type: "button", on: { click: () => retry(t) } }, "Try again")));
    } else {
      body.push(h("p.ask-a", t.a.answer), t.a.found ? citeLinks(t.a.cites, t.links = []) : null);
    }
    if (!t.el) { t.el = h("div.ask-turn"); log.append(t.el); }
    t.el.dataset.state = t.state;
    clear(t.el, body);
  }

  function setBusy(on) {
    field.disabled = on;
    send.disabled = on;
    root.classList.toggle("asking", on);
  }

  /** Put the typed question back when the field is free and empty, so a failure never loses it. */
  function restore(q, at) { if (!field.value.trim()) { field.value = q; draft = at; grow(); } }

  function drop(t) { turns = turns.filter((x) => x !== t); t.el?.remove(); }

  function cancel() {
    if (!busy) return;
    const { turn, controller } = busy;
    busy.cancelled = true;
    controller.abort();
    busy = null;
    drop(turn);
    restore(turn.q, turn.at);
    setBusy(false);
    paintEmpty();
    field.focus();
  }

  function retry(t) { const q = t.q; drop(t); ask(q); }

  async function ask(text) {
    const q = String(text || "").replace(/\s+/g, " ").trim().slice(0, MAX_Q);
    if (!q || busy || !info?.available) return;
    const pos = ctx.position();
    const at = snapshot(ctx.view(), pos);
    const key = ctx.key();
    draft = null;
    const history = historyFor(turns);
    clearedLine = ""; clear(cleared);
    const src = source();
    const turn = { q, state: "pending", a: null, err: "", el: null, at, source: src };
    turns.push(turn);
    const controller = new AbortController();
    const mine = busy = { turn, controller, at, cancelled: false };
    field.value = ""; grow();
    paintTurn(turn);
    paintEmpty();
    setBusy(true);
    let res = null, failure = null;
    const body = { question: q, anchor: pos.anchor ?? null, book: pos.book ?? null, finished: pos.finished ?? null, order: pos.order ?? null, history };
    if (src !== "index") { body.source = src; body.p = pos.p; }   // left out for "Index only", so an older server is asked as before
    try { res = await api.ask(key, body, controller.signal); }
    catch (e) { failure = e; }
    if (disposed || mine.cancelled || busy !== mine) return;
    busy = null;
    setBusy(false);
    // The position may have moved back while the answer was on its way: it must not appear then.
    if (revealsLess(at, ctx.view(), ctx.position())) { reset(); return; }
    if (failure) {
      if (failure.code === "no-model" || failure.code === "ollama-busy") {
        drop(turn); restore(q, at);
        info = { ...(info || {}), available: false, reason: failure.code };
        paintState();
        return;
      }
      turn.state = "error";
      turn.err = failure.code === "busy" ? "Two questions are already being answered."
        : failure.code === "model-failed" ? (failure.message || "The model could not answer.")
        : failure.code === "bad-position" ? "The index doesn't recognise where you are. Move the position and try again."
        : failure.code === "no-book-file" ? "The book file for this index is not here. Choose Index only to ask the index."
        : failure.code === "bad-source" ? "This server can't read the book for Ask yet. Choose Index only."
        : failure.status ? (failure.message || "The question could not be answered.") : "Couldn't reach the server.";
      restore(q, at);
      paintTurn(turn);
    } else {
      turn.state = "ok";
      turn.a = { answer: String(res.answer || ""), found: Boolean(res.found), cites: Array.isArray(res.cites) ? res.cites : [], note: res.note ?? null,
        source: typeof res.source === "string" ? res.source : src };
      paintTurn(turn);
    }
    if (document.activeElement === document.body || root.contains(document.activeElement) || !document.activeElement) field.focus();
    turn.el.scrollIntoView?.({ block: "nearest" });
  }

  /** Forget the thread. With `say`, one line tells why (only when there was something to forget). */
  function reset({ say = true } = {}) {
    const had = turns.length > 0 || busy || field.value.trim() !== "";
    if (busy) { busy.cancelled = true; busy.controller.abort(); busy = null; setBusy(false); }
    turns = []; draft = null;
    field.value = ""; grow();   // typed, prefilled or restored text may name what the position no longer reveals
    log.replaceChildren();
    clearedLine = had && say ? CLEARED : "";
    clear(cleared, clearedLine);
    paintEmpty();
  }

  return {
    root,
    /** Put the tab's content in `host`; the field and the thread survive repaints because they are the same nodes. */
    mount(host) {
      const had = root.contains(document.activeElement);
      clear(host, root);
      if (Date.now() - statusAt > 5000) refreshStatus();   // not on every repaint while the slider is dragged
      paintState();
      grow();
      if (had) field.focus();
    },
    /** Called on every Reader repaint: the thread goes when the view no longer holds what it was asked at. */
    check() {
      const snaps = [...turns.map((t) => t.at), busy?.at, draft].filter(Boolean);
      if (snaps.length && anyRevealsLess(snaps, ctx.view(), ctx.position())) { reset(); return; }
      // The thread stays: a source whose item has left the view (a replaced recap, an edit, a passage now at or past the
      // position) becomes plain text now, focused or not; focus stays on the replacement, inside the thread.
      for (const t of turns) for (const x of t.links || []) {
        if (!x.btn.isConnected || ctx.canOpen(x.c)) continue;
        const had = document.activeElement === x.btn;
        const plain = x.plain();
        if (had) plain.tabIndex = -1;
        x.btn.replaceWith(plain);
        if (had) plain.focus({ preventScroll: true });
      }
    },
    reset,
    /** Type a question into the field without sending it, and focus it. */
    prefill(text) {
      field.value = String(text || "").slice(0, MAX_Q);
      draft = field.value ? snapshot(ctx.view(), ctx.position()) : null;
      grow();
      field.focus();
      field.setSelectionRange(field.value.length, field.value.length);
    },
    focus() { if (!field.disabled && !composer.hidden) field.focus(); },
    get available() { return info; },
    refreshStatus,
    dispose() { disposed = true; if (busy) { busy.cancelled = true; busy.controller.abort(); } busy = null; },
  };
}
