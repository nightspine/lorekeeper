// Tiny DOM helpers. Index content is untrusted (indexes may come from anyone), so text always
// goes through textContent — there is no innerHTML anywhere in the app except fixed SVG icons.

const ICONS = {
  "book-open": '<path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z"/>',
  wand: '<path d="m15 4 1.5-1.5M20 9l1.5-1.5M18 4l-1 1M14 2v2M22 10h-2M4 20 15 9"/><path d="m17 7-2-2"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5M12 3v12"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>',
  play: '<path d="m6 4 14 8-14 8z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
  store: '<path d="M3 9 5 4h14l2 5"/><path d="M4 9v11h16V9"/><path d="M3 9h18M9 20v-6h6v6"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  "chevron-left": '<path d="m15 6-6 6 6 6"/>',
  "skip-back": '<path d="M19 20 9 12l10-8z"/><path d="M5 19V5"/>',
  "skip-fwd": '<path d="m5 4 10 8-10 8z"/><path d="M19 5v14"/>',
  person: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  place: '<path d="M12 22s7-6.1 7-12a7 7 0 1 0-14 0c0 5.9 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/>',
  creature: '<circle cx="6" cy="10" r="1.8"/><circle cx="10" cy="6" r="1.8"/><circle cx="14" cy="6" r="1.8"/><circle cx="18" cy="10" r="1.8"/><path d="M8.5 17.5c0-2.5 1.6-4.5 3.5-4.5s3.5 2 3.5 4.5c0 1.4-1 2.5-2.3 2.5-.5 0-.8-.2-1.2-.2s-.7.2-1.2.2c-1.3 0-2.3-1.1-2.3-2.5z"/>',
  group: '<circle cx="9" cy="8" r="3.5"/><path d="M2 21a7 7 0 0 1 14 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M22 21a7 7 0 0 0-4-6.3"/>',
  object: '<path d="m21 8-9-5-9 5 9 5z"/><path d="M3 8v8l9 5 9-5V8M12 13v8"/>',
  concept: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
  term: '<path d="M4 19.5V5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2z"/><path d="M8 7h8M8 11h6"/>',
  sparkle: '<path d="M12 3 13.8 9.2 20 11l-6.2 1.8L12 19l-1.8-6.2L4 11l6.2-1.8z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  terminal: '<path d="m4 17 6-5-6-5M12 19h8"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.4 8.4 8 9 4.6-.6 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  scroll: '<path d="M8 21h12a2 2 0 0 0 2-2v-2H10v2a2 2 0 1 1-4 0V5a2 2 0 0 0-2-2 2 2 0 0 0-2 2v3h4"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  timeline: '<path d="M12 3v18"/><circle cx="12" cy="7" r="2"/><circle cx="12" cy="17" r="2"/><path d="M14 7h6M4 17h6"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5M2 17l10 5 10-5"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  library: '<path d="M4 19V5M8 19V5M12 19l3-14 5 1-3 14z"/>',
  pulse: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.800 12 21 12 21z"/><circle cx="12" cy="9.500" r="2.500"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  feather: '<path d="M20.2 12.2a6 6 0 0 0-8.5-8.5L5 10.5V19h8.5z"/><path d="M16 8 2 22M17.5 15H9"/>',
};

export function icon(name, label) {
  const span = document.createElement("span");
  span.style.display = "inline-flex";
  span.innerHTML = `<svg class="svg-icon" viewBox="0 0 24 24" ${label ? `role="img" aria-label="${label}"` : 'aria-hidden="true"'}>${ICONS[name] || ICONS.info}</svg>`;
  return span.firstElementChild;
}

/**
 * A long list shown short: the first `show` nodes (the last `show` with `fromEnd`) and one quiet
 * "Show all N" control that expands it in place. `make(nodes)` builds the list element. Lists
 * barely over the limit are shown whole: a control hiding one item is worse than the item.
 */
export function foldList(nodes, make, { show = 4, fromEnd = false, noun = "", open = false, onToggle = null, keep = [] } = {}) {
  if (nodes.length <= show + 1) return make(nodes);
  const wrap = h("div.fold");
  const paint = (all) => {
    // `open` and `onToggle` let a caller that repaints the list keep it open; `keep` indexes stay visible when it is short.
    const base = fromEnd ? nodes.slice(-show) : nodes.slice(0, show);
    const shown = all ? nodes : nodes.filter((n, i) => base.includes(n) || keep.includes(i));
    const more = h("button.linkish.fold-more", { type: "button", "aria-expanded": String(all), on: { click: () => { onToggle?.(!all); paint(!all); } } },
      all ? "Show fewer" : `Show all ${nodes.length}${noun ? ` ${noun}` : ""}`);
    clear(wrap, make(shown), more);
  };
  paint(open);
  return wrap;
}

/** h("div.card#id", {attrs, on: {click}}, ...children) */
export function h(spec, props, ...children) {
  if (props instanceof Node || typeof props === "string" || typeof props === "function" || Array.isArray(props) || props == null) {
    if (props != null) children.unshift(props);
    props = {};
  }
  const [tag, ...rest] = spec.split(/(?=[.#])/);
  const el = document.createElement(tag || "div");
  for (const r of rest) {
    if (r[0] === ".") el.classList.add(r.slice(1));
    else el.id = r.slice(1);
  }
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "class") el.className += " " + v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

// A function is never content: passing one (a component instead of what it returns) would print its source as text.
// It is skipped with a console error; tests set globalThis.LK_STRICT_DOM to make it throw.
function badChild(fn) {
  const msg = `h(): a function (${fn.name || "anonymous"}) was passed as a child; it is not rendered`;
  if (globalThis.LK_STRICT_DOM) throw new TypeError(msg);
  console.error(msg);
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    if (typeof c === "function") { badChild(c); continue; }
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => el.replaceWith(icon(el.dataset.icon)));
}

export const TYPE_LABEL = { person: "Person", creature: "Creature", place: "Place", group: "Group", object: "Object", concept: "Concept", term: "Term" };
export const TYPE_PLURAL = { person: "Cast", creature: "Creatures", place: "Places", group: "Groups", object: "Objects", concept: "Ideas", term: "Lexicon" };

export function typeTag(type) {
  return h(`span.type-tag.t-${type}`, TYPE_LABEL[type] || type);
}

export function fmtDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

export const fmtInt = (n) => (n ?? 0).toLocaleString();

const MAX_TOASTS = 4;

/** Show a notice. kind "busy" stays until dismissed or the returned function is called; so does an
 *  error (a person may be reading something else when it arrives). `host` is the element the toast
 *  goes into (the page's stack by default; the entry drawer passes its own, since a modal dialog
 *  makes the rest of the page inert). At most four toasts are held: the oldest non-busy one goes first. */
export function toast(message, kind = "ok", { host = null, action = null } = {}) {
  const dismiss = h("button.icon-btn.small.toast-dismiss", { type: "button", "aria-label": "Dismiss", title: "Dismiss" }, icon("x"));
  const el = h(`div.toast.${kind}`, { role: kind === "error" ? "alert" : "status" },
    kind === "busy" ? h("span.spinner", { "aria-hidden": "true" }) : icon(kind === "error" ? "alert" : "check-circle"),
    h("div.toast-msg", message),
    action ? h("button.linkish", { type: "button", on: { click: async () => { el.remove(); await action.run(); } } }, action.label) : null,
    dismiss);
  dismiss.addEventListener("click", () => el.remove());
  const stack = host || document.getElementById("toasts");
  stack.append(el);
  const old = [...stack.children].filter((t) => t !== el && !t.classList.contains("busy"));
  for (const t of old.slice(0, Math.max(0, stack.children.length - MAX_TOASTS))) t.remove();
  if (kind === "ok") setTimeout(() => el.remove(), action ? 9000 : 3800);
  return () => el.remove();
}

/** A failed first load, in the page where the content would have been: what failed, the server's
 *  words, and a way to try again. `retry` is called when the person asks; the caller re-renders. */
export function loadError(what, error, retry) {
  const detail = typeof error === "string" ? error : error?.message || "";
  const again = h("button.btn.primary", { type: "button", on: { click: () => retry() } }, "Try again");
  return h("div.load-error", { role: "alert" },
    h("h2", `Couldn't ${what}`),
    detail ? h("p.muted", detail) : null,
    again);
}

/** A first-load placeholder: the spinner and one line, announced as a status. It only becomes
 *  visible after about 300 ms (see .loading in base.css), so a fast load never flashes it. */
export function loading(text) {
  return h("div.loading", { role: "status" }, h("span.spinner", { "aria-hidden": "true" }), h("span", text));
}

/** Breadcrumb trail: `nav.crumbs` with "/" between items and the last one marked as the current
 *  page. Items are `{ label, href? }`; an item without `href` is plain text. Pass an existing `nav`
 *  to repaint it in place (the Reader does, once the catalog has arrived). */
export function crumbs(items, nav = h("nav.crumbs", { "aria-label": "Breadcrumb" })) {
  nav.replaceChildren();
  items.forEach((it, i) => {
    const current = i === items.length - 1 ? "page" : null;
    if (i) nav.append(h("span.sep", { "aria-hidden": "true" }, "/"));
    // Only the page itself is "current"; a link to another page is not, even in the last place.
    nav.append(it.href ? h("a", { href: it.href }, it.label) : h("span", { "aria-current": current }, it.label));
  });
  return nav;
}

/** `dlg.showModal()` with the page locked behind it; released once when the dialog closes or is removed while open
 *  (a removed open dialog fires no close event). Use this for every modal dialog instead of calling showModal() directly. */
const releases = new WeakMap();
export function showModalLocked(dlg) {
  if (dlg.open) return;
  lockScroll();
  let released = false;
  const release = () => { if (released) return; released = true; mo.disconnect(); dlg.removeEventListener("close", release); releases.delete(dlg); unlockScroll(); };
  releases.set(dlg, release);
  const mo = new MutationObserver(() => { if (!dlg.isConnected) release(); });
  mo.observe(document.body, { childList: true, subtree: true });
  dlg.addEventListener("close", release);
  dlg.showModal();
}

/** Close a dialog opened with showModalLocked and release the page lock at once (the `close` event comes later, and a
 *  command run right after closing, such as the palette scrolling to a chapter, must not meet a pinned page). */
export function closeLocked(dlg) {
  releases.get(dlg)?.();
  if (dlg.open) dlg.close();
}

/**
 * A bottom sheet on a native <dialog> (focus trap, Escape, inert page and scroll lock come from
 * showModal). Full width, rounded top corners, a grab handle, its own scroll, closed by the handle
 * area's Close button, Escape, a tap on the scrim, or a swipe down on the grab handle or header.
 * Focus goes to `focus` (an element or selector inside), else the title, and returns to the
 * element that had it when the sheet opened.
 *
 *   const s = sheet({ title: "Jump to", content: node, onClose, label, footer, focus, beforeClose });
 *   s.el          the <dialog class="sheet">
 *   s.body        the scrolling body element (content is already in it); repaint with s.set(...nodes)
 *   s.toasts      a host for toast(msg, kind, { host: s.toasts }) (a modal makes the page inert)
 *   s.close()     close it (runs beforeClose first; returns false if it refused)
 *   s.setTitle(t) change the title
 *   s.closed      a Promise that resolves when the sheet has closed
 * `beforeClose()` may return false to keep the sheet open (a dirty form asking first); `onClose()`
 * runs once after it closed. Only one sheet is open at a time: opening another closes the first, unless the new one
 * is opened with `stack: true` (the Styles sheet over the picture sheet): it then sits on top, and the one under it is
 * the open sheet again once it closes.
 */
let openSheet = null;

/** Lock the page behind an open modal (iOS Safari lets the page move behind a <dialog> otherwise): the body is
 *  pinned where it was and put back, at the same scroll position, when the last lock is released. */
let locks = 0, lockedAt = 0;
export function lockScroll() {
  if (locks++) return;
  lockedAt = window.scrollY;
  const b = document.body;
  b.style.top = `-${lockedAt}px`;
  b.classList.add("scroll-locked");
}
export function unlockScroll() {
  if (!locks || --locks) return;
  const b = document.body;
  b.classList.remove("scroll-locked");
  b.style.top = "";
  window.scrollTo(0, lockedAt);
}
export function sheet({ title = "", content = null, footer = null, onClose = null, beforeClose = null, label = null, focus = null, className = "", stack = false } = {}) {
  const under = stack ? openSheet : null;
  if (openSheet && !stack) openSheet.close(true);
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const titleEl = h("h2.sheet-title", { tabindex: "-1" }, title);
  const closeBtn = h("button.icon-btn.sheet-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const grab = h("div.sheet-grab", { "aria-hidden": "true" }, h("span.sheet-handle"));
  const head = h("header.sheet-head", grab, titleEl, closeBtn);
  const body = h("div.sheet-body", content);
  const toasts = h("div.toasts.sheet-toasts", { "aria-live": "polite" });
  const panel = h("div.sheet-panel", head, body, footer ? h("footer.sheet-foot", footer) : null, toasts);
  const dlg = h("dialog.sheet", { "aria-label": label || title || "Panel", class: className }, panel);
  let done, finished = false, closing = false;
  const closed = new Promise((r) => { done = r; });
  const api = {
    el: dlg, body, toasts, closed,
    get finished() { return finished; },
    set: (...nodes) => clear(body, ...nodes),
    setTitle: (t) => { titleEl.textContent = t; if (!label) dlg.setAttribute("aria-label", t || "Panel"); },
    close(force = false) {
      if (finished) return true;
      // A forced close during the slide-out ends it now: what the sheet shows may have to go at once (a spoiler rule).
      if (closing) { if (force && dlg.open) closeLocked(dlg); return true; }
      if (!force && beforeClose && beforeClose() === false) return false;
      if (!dlg.open) { finish(); return true; }
      // Slide out for 200 ms (not under reduced motion, and not when another sheet replaces this one), then close.
      if (force || matchMedia("(prefers-reduced-motion: reduce)").matches) { closeLocked(dlg); return true; }
      closing = true;
      panel.style.transition = "transform 200ms ease-in, opacity 200ms ease-in";
      panel.style.transform = "translateY(100%)";
      panel.style.opacity = "0";
      dlg.classList.add("closing");
      setTimeout(() => { if (dlg.open) closeLocked(dlg); else finish(); }, 200);
      return true;
    },
  };
  function finish() {
    if (finished) return;
    finished = true;
    if (openSheet === api) openSheet = under && !under.finished ? under : null;
    dlg.remove();
    if (returnTo?.isConnected) { try { returnTo.focus({ preventScroll: true }); } catch { /* element gone */ } }
    done();
    onClose?.();
  }
  dlg.addEventListener("close", finish);
  // Escape asks first, like the close button (the browser's own Escape would close it unasked).
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); api.close(); });
  closeBtn.addEventListener("click", () => api.close());
  // A tap on the scrim lands on the dialog itself: the panel fills it, the scrim is the rest.
  dlg.addEventListener("click", (e) => { if (e.target === dlg) api.close(); });

  // Swipe down from the grab handle or header: the panel follows the finger; past 90px or a quick flick it closes.
  let drag = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button, a, input, select, textarea") || (e.pointerType === "mouse" && e.button !== 0)) return;
    drag = { y: e.clientY, t: e.timeStamp, dy: 0, id: e.pointerId };
    head.setPointerCapture?.(e.pointerId);
    panel.style.transition = "none";
  });
  head.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dy = Math.max(0, e.clientY - drag.y);
    panel.style.transform = `translateY(${drag.dy}px)`;
  });
  const release = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const { dy, t } = drag; drag = null;
    panel.style.transition = "";
    const fast = dy > 30 && dy / Math.max(1, e.timeStamp - t) > 0.5;
    if ((dy > 90 || fast) && api.close()) return;
    panel.style.transform = "";
  };
  head.addEventListener("pointerup", release);
  head.addEventListener("pointercancel", release);

  document.body.append(dlg);
  showModalLocked(dlg);
  openSheet = api;
  const target = typeof focus === "string" ? dlg.querySelector(focus) : focus;
  (target || titleEl).focus({ preventScroll: true });
  return api;
}

export const STATUS_LABEL = { queued: "Queued", running: "Running", stopping: "Stopping", stopped: "Stopped", done: "Done", failed: "Failed", interrupted: "Interrupted" };

/** "never", "just now", "5 min ago", "3 h ago", else the date. Takes unix seconds. */
export function relTime(unixSeconds) {
  if (!unixSeconds) return "never";
  const d = Date.now() / 1000 - unixSeconds;
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return new Date(unixSeconds * 1000).toLocaleDateString();
}

export function announce(message) {
  const el = document.getElementById("announcer");
  if (!el) return;   // a debounced announcement can fire after the page was torn down
  el.textContent = "";
  requestAnimationFrame(() => { el.textContent = message; });
}

/** The line that says what an entity is: its profile, or for indexes without profiles its first fact
 *  (closer to "who" than the latest one, which is usually a plot beat). */
export function describe(e) {
  return e.profile?.text || e.facets[0]?.text || "";
}

const MINOR = new Set(["a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"]);
const ROMAN = /^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$/;
/** "CHAPTER VIII MINA MURRAY'S JOURNAL" → "Chapter VIII Mina Murray's Journal". A title in capitals is title-cased:
 *  a word starts after a space, a dash or an opening quote (never after an apostrophe), small words stay small
 *  unless they open the title or follow a full stop or colon, roman numerals stay capitals, a trailing full stop
 *  goes. Anything not in capitals is returned as it is. */
export function tidyTitle(t) {
  if (!t || !/[A-Z]{4}/.test(t)) return t;
  // Mostly capitals ("CHAPTER III JONATHAN HARKER’S JOURNAL—continued"): words in capitals are title-cased, words
  // already in lower or mixed case are kept as written.
  const letters = t.replace(/[^\p{L}]/gu, "");
  if ([...letters].filter((c) => c === c.toUpperCase()).length < letters.length * 0.7) return t;
  return t.replace(/\p{L}[\p{L}'’]*/gu, (orig, i, all) => {
    if (orig !== orig.toUpperCase()) return orig;
    const w = orig.toLowerCase();
    const before = all.slice(0, i).toLowerCase();
    // A numeral after Chapter, Part, Book, Volume, Act or Scene, one that opens the title ("IV. The Visit"), or a run of
    // two or more of I, V and X anywhere ("World War II"); "Mix" or "Civil" are words, not numerals.
    if (ROMAN.test(w) && (/\b(chapter|part|book|volume|act|scene)\s+$/.test(before) || (i === 0 && /^[.:\s]/.test(all.slice(orig.length)))
      || /^[ivx]{2,}$/.test(w))) return orig;
    if (i && MINOR.has(w) && /\s$/.test(before) && !/[.:;!?]\s*$/.test(before)) return w;
    return w[0].toUpperCase() + w.slice(1);
  }).replace(/\.$/, "");
}
