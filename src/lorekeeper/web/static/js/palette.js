// Command palette (Cmd/Ctrl+K). Providers register sync functions returning items; see registerProvider.
import { h, icon, clear, announce } from "./dom.js";
import { api } from "./api.js";

const MAX_PER_GROUP = 8, MAX_TOTAL = 24;
const providers = new Map();
let dialog, input, list, items = [], active = 0, books = null, booksAt = 0, composing = false, announceTimer;

/** provider: (query) => Item[]. Registering the same id replaces it. Returns an unregister function. */
export function registerProvider(id, provider) {
  providers.set(id, provider);
  return () => { if (providers.get(id) === provider) providers.delete(id); };
}

// ---- matching ------------------------------------------------------------------------------
function score(item, q) {
  if (!q) return 1;
  const label = item.label.toLowerCase();
  const hay = `${label} ${(item.hint || "").toLowerCase()} ${(item.keywords || "").toLowerCase()}`;
  if (label === q) return 100;
  if (label.startsWith(q)) return 90;
  if (label.split(/\s+/).some((w) => w.startsWith(q))) return 80;
  if (label.includes(q)) return 70;
  if (hay.includes(q)) return 50;
  // subsequence fallback: label only, and the letters must sit close together (a typo-tolerant match, not a scatter)
  if (q.length < 3) return 0;
  const first = label.indexOf(q[0]);
  if (first < 0) return 0;
  let i = 0, end = first;
  for (let k = first; k < label.length && i < q.length; k++) if (label[k] === q[i]) { i++; end = k; }
  return i === q.length && end - first < q.length * 2 ? 20 : 0;
}

function collect(query) {
  const q = query.trim().toLowerCase();
  const groups = new Map();
  for (const [id, p] of providers) {
    let out = [];
    try { out = p(query.trim()) || []; } catch (e) { console.error(`palette provider ${id}`, e); }
    out.forEach((it, n) => {
      let s = score(it, q);
      if (!s) return;
      if (q && it._rank) s = Math.max(s, it._rank);   // built-in commands that name themselves in keywords win ties
      if (!groups.has(it.group)) groups.set(it.group, []);
      groups.get(it.group).push({ it, s, n });
    });
  }
  let total = 0;
  const result = [];
  const order = [...groups].sort((a, b) => Math.max(...b[1].map((x) => x.s)) - Math.max(...a[1].map((x) => x.s)));
  for (const [group, arr] of order) {
    arr.sort((a, b) => b.s - a.s || a.n - b.n);
    for (const { it } of arr.slice(0, MAX_PER_GROUP)) {
      if (total >= MAX_TOTAL) break;
      result.push({ ...it, group });
      total++;
    }
  }
  return result;
}

// ---- built-in providers ---------------------------------------------------------------------
const go = (hash) => () => { location.hash = hash; };
const NAV = [
  { id: "nav-library", label: "Library", hint: "All books", hash: "#/library" },
  { id: "nav-ready", label: "Ready to read", hint: "Books with an index", hash: "#/library?show=ready" },
  { id: "nav-runs", label: "Runs", hint: "Generation jobs", hash: "#/studio" },
  { id: "nav-settings", label: "Settings", hint: "Models and keys", hash: "#/settings" },
];
registerProvider("go-to", () => NAV.map((n) => ({ id: n.id, label: n.label, hint: n.hint, group: "Go to", run: go(n.hash) })));

function defaultVersion(b) {
  return (b.versions || []).find((v) => v.key === b.default) || (b.default ? { key: b.default } : null);
}
function latest(b) {
  return Math.max(0, ...(b.versions || []).map((v) => Date.parse(v.updated) || 0));
}
registerProvider("books", (query) => {
  if (!books) return [];
  const mk = (b) => {
    const v = defaultVersion(b);
    return v
      ? { id: `book-${b.key}`, label: `Read ${b.title}`, hint: (b.authors || [])[0] || "", group: "Books", keywords: b.title, run: () => { location.hash = "#/reader?i=" + encodeURIComponent(v.key); } }
      : { id: `book-${b.key}`, label: b.title, hint: "No index yet", group: "Books", run: () => { location.hash = "#/book/" + encodeURIComponent(b.key); } };
  };
  if (!query) {
    return books.filter((b) => defaultVersion(b)).sort((a, b) => latest(b) - latest(a)).slice(0, 5).map(mk);
  }
  return books.map(mk);
});

registerProvider("theme", () => {
  const dark = (document.documentElement.dataset.theme || "dark") === "dark";
  return [{
    id: "theme-toggle", label: dark ? "Switch to light theme" : "Switch to dark theme", group: "Theme", keywords: "dark light appearance mode", _rank: 95,
    run: () => {
      const btn = document.getElementById("theme-toggle");
      if (btn) btn.click();   // app.js owns the toggle: it sets the theme, persists it and repaints the button
      else {
        const next = dark ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem("lk-theme", next); } catch {}
      }
    },
  }];
});

async function loadBooks() {
  if (books && Date.now() - booksAt < 60000) return;
  try {
    const d = await api.catalog();
    books = d.books || [];
    booksAt = Date.now();
    if (dialog?.open) render();
  } catch { /* the palette still works without books */ }
}

// ---- rendering -----------------------------------------------------------------------------
function build() {
  input = h("input.palette-input", {
    type: "text", role: "combobox", placeholder: "Search books, characters, commands", autocomplete: "off", spellcheck: false,
    "aria-label": "Search and commands", "aria-expanded": "true", "aria-controls": "palette-list", "aria-autocomplete": "list",
    on: { input: render, keydown: onKey },
  });
  list = h("div.palette-list#palette-list", { role: "listbox", "aria-label": "Results" });
  dialog = h("dialog.palette", { "aria-label": "Command palette" },
    h("div.palette-field", icon("search"), input), list);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) closePalette(); });
  dialog.addEventListener("close", () => { input.setAttribute("aria-expanded", "false"); });
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  document.body.append(dialog);
}

function render() {
  items = collect(input.value);
  active = Math.min(active, Math.max(0, items.length - 1));
  list.replaceChildren();
  let lastGroup = null, group = null;
  items.forEach((it, i) => {
    if (it.group !== lastGroup) {
      lastGroup = it.group;
      const gid = `palette-g-${i}`;
      list.append(h("div.palette-heading", { id: gid, role: "presentation" }, it.group));
      group = h("div.palette-group", { role: "group", "aria-labelledby": gid });
      list.append(group);
    }
    const row = h("div.palette-row", {
      id: `palette-opt-${i}`, role: "option", "aria-selected": String(i === active),
      class: i === active ? "active" : "",
      on: { click: () => choose(i), mousemove: () => { if (active !== i) setActive(i, false); } },
    },
      it.type ? h(`span.palette-dot.t-${it.type}`, { "aria-hidden": "true" }) : null,
      h("span.palette-label", it.label),
      it.hint ? h("span.palette-hint", it.hint) : null);
    group.append(row);
  });
  if (!items.length) list.append(h("div.palette-empty", "No results"));
  input.setAttribute("aria-activedescendant", items.length ? `palette-opt-${active}` : "");
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => announce(`${items.length} result${items.length === 1 ? "" : "s"}`), 300);
}

function setActive(i, scroll = true) {
  const rows = list.querySelectorAll('[role="option"]');
  if (!rows.length) return;
  active = (i + rows.length) % rows.length;
  rows.forEach((r, n) => { r.classList.toggle("active", n === active); r.setAttribute("aria-selected", String(n === active)); });
  input.setAttribute("aria-activedescendant", `palette-opt-${active}`);
  if (scroll) rows[active].scrollIntoView({ block: "nearest" });
}

function choose(i) {
  const it = items[i];
  if (!it) return;
  closePalette();
  try { it.run(); } catch (e) { console.error(e); }
}

function onKey(e) {
  if (e.isComposing) return;
  if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
  else if (e.key === "Enter") { e.preventDefault(); choose(active); }
}

export function openPalette(initialQuery = "") {
  if (!dialog) build();
  if (!dialog.open) dialog.showModal();
  input.setAttribute("aria-expanded", "true");
  input.value = initialQuery;
  active = 0;
  render();
  input.focus();
  input.select();
  loadBooks();
}

export function closePalette() {
  if (dialog?.open) dialog.close();
}

// ---- wiring --------------------------------------------------------------------------------
document.addEventListener("keydown", (e) => {
  if (!((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k")) return;
  if (e.isComposing || composing) return;
  const other = [...document.querySelectorAll("dialog[open]")].some((d) => d !== dialog);
  if (other) return;
  e.preventDefault();
  if (dialog?.open) closePalette(); else openPalette();
});
window.addEventListener("hashchange", closePalette);
document.getElementById("palette-btn")?.addEventListener("click", () => openPalette());
// Warm the book list so the first open is instant; the palette works without it.
setTimeout(loadBooks, 300);
