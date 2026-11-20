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

/** Every id a gated view shows (entries, their details and profiles, connections, events, chapter recaps). */
export function viewIds(view) {
  const ids = new Set();
  for (const e of view?.entities || []) {
    ids.add(e.id);
    if (e.profile) ids.add(e.profile.id);
    for (const f of e.facets || []) ids.add(f.id);
    for (const f of e.history || []) ids.add(f.id);
  }
  for (const r of view?.relations || []) ids.add(r.id);
  for (const ev of view?.events || []) ids.add(ev.id);
  for (const c of view?.chapters || []) ids.add(c.id);
  if (view?.recap) ids.add(view.recap.id);
  return ids;
}
const covers = (now, then) => { for (const id of then) if (!now.has(id)) return false; return true; };

/**
 * ctx: key() the library key; position() { anchor, finished, order } the view on screen was computed with;
 * view() the gated view; examples() three question strings; canOpen(cite), open(cite) for the sources.
 */
export function createAsk(ctx) {
  let info = null;                 // GET /api/ask
  let infoFor = 0;                 // request counter, so a late status never overwrites a newer one
  let turns = [];                  // { q, state: "pending" | "ok" | "error", a, err, el }
  let snap = null;                 // { key, ids }: what the view held at the latest answer
  let busy = null;                 // { turn, controller, ids, key }
  let clearedLine = "";
  let disposed = false;

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
  const root = h("div.ask", log, cleared, empty, unavailable, composer, via);

  function grow() {
    field.style.height = "auto";
    const line = parseFloat(getComputedStyle(field).lineHeight) || 20;
    const pad = field.offsetHeight - field.clientHeight;
    field.style.height = `${Math.min(field.scrollHeight + pad, line * 4 + 16)}px`;
    count.textContent = field.value.length >= 420 ? `${MAX_Q - field.value.length} left` : "";
  }
  field.addEventListener("input", grow);
  field.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); ask(field.value); }
  });

  // ---- where the question goes -------------------------------------------------------------------

  const company = (i) => (i.provider === "claude" ? "Anthropic" : i.label || "the provider");
  function paintVia() {
    if (!info?.available) { clear(via); return; }
    clear(via, info.local ? `Answered on this computer by ${info.modelLabel || info.model}.`
      : `Answered by ${info.modelLabel || info.model} from the index's notes. Your question and those notes are sent to ${company(info)}; the book's text is not.`);
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
    clear(empty, h("p", "Ask about anyone or anything you've read so far. Answers come from this index up to your position, not from the book's text."),
      ex.length ? h("div.ask-examples", ex.map((q) => h("button.ask-example", { type: "button", on: { click: () => ask(q) } }, q))) : null);
  }

  // ---- the thread --------------------------------------------------------------------------------

  function citeLinks(cites) {
    const seen = new Set();
    const list = (cites || []).filter((c) => { const k = `${c.kind}:${c.id}`; if (seen.has(k)) return false; seen.add(k); return true; });
    if (!list.length) return null;
    const label = (c) => String(c.label || c.kind || "source");
    const items = list.map((c) => {
      const plain = () => h("span.ask-cite", { title: label(c) }, label(c));
      if (!ctx.canOpen(c)) return h("li", plain());
      const li = h("li");
      const btn = h("button.linkish.ask-cite", { type: "button", title: label(c), on: { click: () => {
        if (ctx.canOpen(c)) ctx.open(c);
        else btn.replaceWith(plain());   // no longer in the view on screen
      } } }, label(c));
      li.append(btn);
      return li;
    });
    return h("div.ask-from", h("span.ask-from-lbl", "From:"), h("ul.ask-cites", items));
  }

  function paintTurn(t) {
    const body = [h("p.ask-q", t.q)];
    if (t.state === "pending") {
      body.push(h("div.ask-wait", loading("Reading the index…"),
        h("button.btn.quiet.ask-cancel", { type: "button", on: { click: cancel } }, "Cancel")));
    } else if (t.state === "error") {
      body.push(h("p.ask-err", { role: "alert" }, t.err, " ",
        h("button.linkish", { type: "button", on: { click: () => retry(t) } }, "Try again")));
    } else {
      body.push(h("p.ask-a", t.a.answer), t.a.found ? citeLinks(t.a.cites) : null);
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
  function restore(q) { if (!field.value.trim()) { field.value = q; grow(); } }

  function drop(t) { turns = turns.filter((x) => x !== t); t.el?.remove(); }

  function cancel() {
    if (!busy) return;
    const { turn, controller } = busy;
    busy.cancelled = true;
    controller.abort();
    busy = null;
    drop(turn);
    restore(turn.q);
    setBusy(false);
    paintEmpty();
    field.focus();
  }

  function retry(t) { const q = t.q; drop(t); ask(q); }

  async function ask(text) {
    const q = String(text || "").replace(/\s+/g, " ").trim().slice(0, MAX_Q);
    if (!q || busy || !info?.available) return;
    const pos = ctx.position();
    const ids = viewIds(ctx.view());
    const key = ctx.key();
    const history = turns.filter((t) => t.state === "ok").slice(-KEEP_TURNS)
      .map((t) => ({ question: t.q, answer: t.a.answer, cites: (t.a.cites || []).map((c) => c.id) }));
    clearedLine = ""; clear(cleared);
    const turn = { q, state: "pending", a: null, err: "", el: null };
    turns.push(turn);
    const controller = new AbortController();
    const mine = busy = { turn, controller, ids, key, cancelled: false };
    field.value = ""; grow();
    paintTurn(turn);
    paintEmpty();
    setBusy(true);
    let res = null, failure = null;
    try { res = await api.ask(key, { question: q, anchor: pos.anchor ?? null, finished: pos.finished ?? null, order: pos.order ?? null, history }, controller.signal); }
    catch (e) { failure = e; }
    if (disposed || mine.cancelled || busy !== mine) return;
    busy = null;
    setBusy(false);
    // The position may have moved back while the answer was on its way: it must not appear then.
    if (!failure && !covers(viewIds(ctx.view()), ids)) { reset(); return; }
    if (failure) {
      if (failure.code === "no-model" || failure.code === "ollama-busy") {
        drop(turn); restore(q);
        info = { ...(info || {}), available: false, reason: failure.code };
        paintState();
        return;
      }
      turn.state = "error";
      turn.err = failure.code === "busy" ? "Two questions are already being answered."
        : failure.code === "model-failed" ? (failure.message || "The model could not answer.")
        : failure.code === "bad-position" ? "The index doesn't recognise where you are. Move the position and try again."
        : failure.status ? (failure.message || "The question could not be answered.") : "Couldn't reach the server.";
      restore(q);
      paintTurn(turn);
    } else {
      turn.state = "ok";
      turn.a = { answer: String(res.answer || ""), found: Boolean(res.found), cites: Array.isArray(res.cites) ? res.cites : [] };
      snap = { key, ids };
      paintTurn(turn);
    }
    if (document.activeElement === document.body || root.contains(document.activeElement) || !document.activeElement) field.focus();
    turn.el.scrollIntoView?.({ block: "nearest" });
  }

  /** Forget the thread. With `say`, one line tells why (only when there was something to forget). */
  function reset({ say = true } = {}) {
    const had = turns.length > 0 || busy;
    if (busy) { busy.cancelled = true; busy.controller.abort(); busy = null; setBusy(false); }
    turns = []; snap = null;
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
    check(key) {
      if (!turns.length && !busy) return;
      const now = viewIds(ctx.view());
      if ((snap && (snap.key !== key || !covers(now, snap.ids))) || (busy && (busy.key !== key || !covers(now, busy.ids)))) reset();
    },
    reset,
    /** Type a question into the field without sending it, and focus it. */
    prefill(text) {
      field.value = String(text || "").slice(0, MAX_Q);
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
