import { api, hooks, safeNext } from "./api.js";
import { announce, clear, fmtInt, h, hydrateIcons, icon } from "./dom.js";
import { keepReading, onReading, readingLine, readingList as recentReading } from "./reading.js";
import { mountStudio, mountBuild } from "./studio.js";
import { mountReader } from "./reader.js";
import { mountSettings } from "./settings.js";
import { mountLibrary } from "./library.js";
import { mountBook, mountSeries } from "./bookpage.js";
import { mountLogin, mountPassword } from "./auth.js";

export const state = { ollama: null, session: { auth: false } };
const listeners = new Set();
export const onOllama = (fn) => { listeners.add(fn); if (state.ollama) fn(state.ollama); return () => listeners.delete(fn); };

// ---- theme ---------------------------------------------------------------------------------
// Every [data-theme-toggle] switches it: the rail's row, the phone app bar's button, the sign-in page's button.
function currentTheme() {
  return document.documentElement.dataset.theme || "dark";
}
function paintToggles() {
  const dark = currentTheme() === "dark";
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  for (const b of document.querySelectorAll("[data-theme-toggle]")) {
    const text = b.querySelector(".nav-lbl");
    b.querySelector(".svg-icon")?.replaceWith(icon(dark ? "sun" : "moon"));
    if (text) { text.textContent = dark ? "Light theme" : "Dark theme"; b.title = label; }
    else { b.setAttribute("aria-label", label); b.title = label; }
  }
}
export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("lk-theme", next); } catch {}
  paintToggles();
}
document.addEventListener("click", (e) => { if (e.target.closest?.("[data-theme-toggle]")) toggleTheme(); });

// ---- the shell: rail, phone app bar and tab bar ------------------------------------------------------
// Sticky offsets in the stylesheets are expressed from --topbar-h: the phone app bar's height, 0 beside the rail.
const appbar = document.getElementById("appbar");
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--topbar-h", `${Math.round(appbar.getBoundingClientRect().height)}px`);
}).observe(appbar);

const kbd = document.getElementById("palette-kbd");
const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
kbd.textContent = mac ? "⌘K" : "Ctrl K";
document.getElementById("palette-btn").title = `Search and commands (${mac ? "⌘K" : "Ctrl K"})`;

const readingList = document.getElementById("rail-reading");
const readingLabel = document.getElementById("rail-reading-label");
const tabBook = document.getElementById("tab-book");
/** The rail's "Reading" group and the tab bar's book: title and the position the person set, nothing else. */
function paintReading() {
  const { route, params } = parseHash();
  const open = route === "reader" ? params.get("i") : null;
  const list = recentReading();
  readingList.hidden = readingLabel.hidden = !list.length;
  clear(readingList, list.map((e) => {
    const line = readingLine(e);
    return h("li", h("a.reading-item", { href: e.hash, title: `${e.title}, ${line}`, "aria-current": e.key === open ? "page" : null },
      h("span.reading-icon", { "aria-hidden": "true" }, icon("book-open")),
      h("span.reading-title", e.title),
      h("span.reading-where", line),
      h("span.reading-bar", { "aria-hidden": "true" }, h("i", { style: { width: `${Math.round(e.p * 1000) / 10}%` } }))));
  }));
  // The phone tab bar offers the book open now, else the one read last.
  const book = list.find((e) => e.key === open) || list[0];
  tabBook.hidden = !book;
  if (book) {
    tabBook.href = book.hash;
    tabBook.querySelector(".tab-book-title").textContent = book.title;
    tabBook.setAttribute("aria-label", `${book.title}, ${readingLine(book)}`);
  }
  paintTitle();
}
onReading(paintReading);

const appbarTitle = document.getElementById("appbar-title");
let pageTitle = "Lorekeeper";
function paintTitle() {
  const { route, params } = parseHash();
  const book = route === "reader" && recentReading().find((e) => e.key === params.get("i"));
  appbarTitle.textContent = book ? book.title : pageTitle;
}

/** Mark the current page in the rail and the tab bar. In the Reader that is the book (its Reading row, its tab). */
function paintCurrent(route, section) {
  const open = route === "reader";
  for (const a of document.querySelectorAll(".app-rail [data-route], .tabbar [data-route]")) {
    const on = a.id === "tab-book" ? open : a.dataset.route === section && !open;
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function paintWho() {
  const who = document.getElementById("rail-who");
  const user = state.session.auth ? state.session.user : null;
  who.hidden = !user;
  if (user) {
    who.querySelector(".nav-lbl").textContent = `Signed in as ${user.name}`;
    who.title = `Signed in as ${user.name}. Account settings`;
  }
}

// The Library row's count: the books in the catalog. Refreshed at most once a minute, and after a run ends.
const railCount = document.getElementById("rail-count");
let countAt = 0;
async function refreshCount(force = false) {
  if (!force && Date.now() - countAt < 60000) return;
  countAt = Date.now();
  try {
    const d = await (force ? api.catalog() : api.catalogShared());
    const n = (d.books || []).length;
    railCount.textContent = n ? fmtInt(n) : "";
    document.querySelector('.app-rail [data-route="library"]').title = n ? `Library, ${fmtInt(n)} books` : "Library";
  } catch { /* the rail works without it */ }
  try {
    // Each time the library data is refreshed, an index deleted or rebuilt since leaves the Reading list.
    const lib = await api.library();
    keepReading(new Set((lib.indexes || []).map((i) => i.key)));
  } catch {}
}

// ---- Ollama status -------------------------------------------------------------------------
// Not shown in the shell; Settings and the build screen read state.ollama / onOllama.
async function pollOllama() {
  try {
    state.ollama = await api.ollama();
  } catch {
    state.ollama = { ok: false, models: [], loaded: [], throughput: {} };
  }
  listeners.forEach((fn) => fn(state.ollama));
}

// ---- build queue ---------------------------------------------------------------------------
// The Runs row says "N building" in ember while anything runs, is queued or is stopping.
const runsLink = document.getElementById("runs-link");
const runsBusy = document.getElementById("runs-busy");
const tabBusy = document.getElementById("tab-busy");
let wasActive = 0;
async function pollQueue() {
  let jobs;
  try { jobs = await api.jobs(); } catch { return; }
  const active = (Array.isArray(jobs) ? jobs : []).filter((j) => ["running", "queued", "stopping"].includes(j.status));
  if (active.length < wasActive) refreshCount(true);   // a run ended: the Library may have a new index
  wasActive = active.length;
  runsBusy.textContent = active.length ? `${active.length} building` : "";
  tabBusy.hidden = !active.length;
  tabBusy.textContent = active.length ? `, ${active.length} building` : "";
  if (!active.length) { runsLink.title = "Runs"; return; }
  // Remaining parts of the running job, when its progress says how many there are.
  const pr = (active.find((j) => j.status === "running") || active[0]).progress || {};
  const left = Number.isFinite(pr.chunks) && Number.isFinite(pr.done) ? Math.max(0, pr.chunks - pr.done) : null;
  runsLink.title = `Runs: ${active.map((j) => j.book_title).filter(Boolean).join(", ")}${left ? `. ${left} parts left in the current run` : ""}`;
}

// ---- session and polling -----------------------------------------------------------------------
// Polling runs only while signed in (or when this server has no sign-in), and never before the password is chosen.
let ollamaTimer = null, queueTimer = null;
function startPolling() {
  if (ollamaTimer) return;
  pollOllama(); pollQueue(); refreshCount(true);
  ollamaTimer = setInterval(pollOllama, 15000);
  queueTimer = setInterval(pollQueue, 15000);
}
function stopPolling() {
  clearInterval(ollamaTimer); clearInterval(queueTimer);
  ollamaTimer = queueTimer = null;
}
const mustChange = () => !!state.session.user?.mustChangePassword;
const signedIn = () => !state.session.auth || !!state.session.user;

function applySession(session) {
  state.session = session || { auth: false };
  document.body.dataset.auth = state.session.auth ? "on" : "off";
  document.body.dataset.signedIn = signedIn() ? "true" : "false";
  document.body.dataset.mustChangePassword = mustChange() ? "true" : "false";
  if (signedIn() && !mustChange()) startPolling(); else stopPolling();
  paintWho();
}

hooks.unauthorized = () => { if (state.session.auth && state.session.user) applySession({ auth: true, user: null, setup: false }); };

/** Re-read who is signed in, then go to `next` (a same-page hash) or the Library. */
export async function refreshSession(next = "") {
  applySession(await api.session());
  const target = safeNext(next) || "#/library";
  if (location.hash === target) render(); else location.hash = target;
}

export async function signOut() {
  try { await api.logout(); } catch { /* the cookie may already be gone; either way we leave */ }
  stopPolling();
  state.ollama = null;
  applySession(await api.session());
  const already = location.hash === "#/login";
  location.hash = "#/login";
  if (already) render();
}

// ---- routing -------------------------------------------------------------------------------
const main = document.getElementById("main");
let unmount = null;

export function parseHash() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  return { route: path.split("/")[0] || "library", parts: path.split("/").slice(1), params: new URLSearchParams(query || "") };
}

export function setParams(params, { replace = true } = {}) {
  const { route, parts } = parseHash();
  const q = new URLSearchParams(params).toString();
  const url = `#/${[route, ...parts].join("/")}${q ? "?" + q : ""}`;
  shownHash = url;
  if (replace) history.replaceState(null, "", url);   // no hashchange follows, so there is nothing to skip
  else { ownHash = url; location.hash = url; }
}
let ownHash = null;   // the last hash a view wrote itself; anything else (links, pasted URLs) re-renders
let shownHash = null;   // the hash of the page on screen, so a stray "#main" can put it back
/** Change the address without adding history or re-rendering (a view that picked what to show for the person). */
export function replaceHash(url) { shownHash = url; history.replaceState(null, "", url); }
let firstRender = true;
const VIEWS = { library: [mountLibrary, "Library"], book: [mountBook, "Book"], series: [mountSeries, "Series"],
  studio: [mountStudio, "Runs"], build: [mountBuild, "Build"], reader: [mountReader, "Reader"], settings: [mountSettings, "Settings"],
  login: [mountLogin, "Sign in"], password: [mountPassword, "New password"] };

/** Go to `url` inside the page on screen (a Settings section): a history entry, no re-render. */
export function showHash(url) {
  shownHash = url; ownHash = url;
  location.hash = url;
  paintReading();
}

function render() {
  const { route, parts, params } = parseHash();
  // "#main" is the skip link's target, not a page: leave the page alone and put the address back.
  if (route === "main") {
    if (shownHash != null) { history.replaceState(null, "", shownHash); main.focus(); return; }
    history.replaceState(null, "", "#/library");   // first load on "#main": nothing is on screen, so open the Library
    return render();
  }
  ownHash = null;   // a hash a view wrote itself is spent once a real navigation arrives
  // An error toast outlives its page only until the person moves on.
  document.querySelectorAll("#toasts .toast.error").forEach((t) => t.remove());
  // Gate the routes on the session: signed out, only the sign-in page renders; before the first password, only that page.
  if (!signedIn() && route !== "login") {
    const next = safeNext(location.hash);
    location.replace("#/login" + (next ? "?next=" + encodeURIComponent(next) : ""));
    return;
  }
  if (signedIn() && state.session.auth && mustChange() && route !== "password") { location.replace("#/password"); return; }
  if (signedIn() && route === "login") { location.replace("#/library"); return; }
  if (route === "password" && !state.session.user) { location.replace(state.session.auth ? "#/login" : "#/library"); return; }
  // Old links to the configure screen now open the build page.
  if (route === "studio" && parts[0] === "book" && parts[1]) {
    const q = location.hash.includes("?") ? location.hash.slice(location.hash.indexOf("?")) : "";
    location.replace(`#/build/${parts[1]}${q}`);
    return;
  }
  // The Reader is opened from the Library with a book; without one, show the books that can be read.
  if (route === "reader" && !params.get("i")) { location.replace("#/library?show=ready"); return; }
  const section = { book: "library", series: "library", reader: "library", build: "library", login: "", password: "" }[route] ?? route;
  // Dialogs and drawers belong to the page that opened them.
  document.querySelectorAll(".modal-backdrop, .drawer, .drawer-backdrop").forEach((el) => el.remove());
  paintCurrent(route, section);
  if (unmount) unmount();
  main.replaceChildren();
  main.dataset.route = route in VIEWS ? route : "library";
  const [mount, title] = VIEWS[route] || VIEWS.library;
  pageTitle = title;
  paintReading();
  unmount = mount(main) || null;
  document.title = `${title} · Lorekeeper`;
  shownHash = location.hash;
  // After the first load a navigation moves focus to the new page and says where we are. A page that
  // already put focus somewhere (a dialog, the sign-in field) keeps it.
  if (!firstRender && !main.contains(document.activeElement) && !document.querySelector("dialog[open]")) {
    main.focus({ preventScroll: true });
    announce(title);
  }
  firstRender = false;
  if (ollamaTimer) { pollQueue(); refreshCount(); }
}

// The skip link moves focus to <main> and leaves the address and the page alone.
document.querySelector(".skip-link")?.addEventListener("click", (e) => { e.preventDefault(); main.focus(); });
window.addEventListener("hashchange", () => {
  if (location.hash === ownHash) { ownHash = null; return; }   // the view's own change, already on screen
  render();
});
hydrateIcons();
paintToggles();
applySession(await api.session());
render();
