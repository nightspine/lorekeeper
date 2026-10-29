import { api, hooks, safeNext } from "./api.js";
import { hydrateIcons, icon } from "./dom.js";
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
const toggle = document.getElementById("theme-toggle");
function currentTheme() {
  return document.documentElement.dataset.theme || "dark";
}
function paintToggle() {
  const dark = currentTheme() === "dark";
  toggle.replaceChildren(icon(dark ? "sun" : "moon"));
  toggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
}
toggle.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("lk-theme", next); } catch {}
  paintToggle();
});
paintToggle();

// ---- topbar height ---------------------------------------------------------------------------
// Sticky offsets in the stylesheet are expressed from --topbar-h. The CSS carries a fallback, but the
// bar wraps at narrow widths and its height depends on the font, so the measured value wins.
const topbar = document.querySelector(".topbar");
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--topbar-h", `${Math.round(topbar.getBoundingClientRect().height)}px`);
}).observe(topbar);

// ---- Ollama status -------------------------------------------------------------------------
// No longer shown in the header; Settings and the build screen read state.ollama / onOllama.
async function pollOllama() {
  try {
    state.ollama = await api.ollama();
  } catch {
    state.ollama = { ok: false, models: [], loaded: [], throughput: {} };
  }
  listeners.forEach((fn) => fn(state.ollama));
}

// ---- build queue ---------------------------------------------------------------------------
// The pill appears only while something is running, queued or stopping; otherwise a quiet "Runs" icon stands in.
const queuePill = document.getElementById("queue-pill");
const runsLink = document.getElementById("runs-link");
async function pollQueue() {
  let jobs;
  try { jobs = await api.jobs(); } catch { return; }
  const active = (Array.isArray(jobs) ? jobs : []).filter((j) => ["running", "queued", "stopping"].includes(j.status));
  queuePill.hidden = !active.length;
  runsLink.hidden = active.length > 0;
  if (!active.length) return;
  queuePill.querySelector(".label").textContent = `${active.length} building`;
  // Remaining chunks of the running job, when its progress says how many there are.
  const pr = (active.find((j) => j.status === "running") || active[0]).progress || {};
  const left = Number.isFinite(pr.chunks) && Number.isFinite(pr.done) ? Math.max(0, pr.chunks - pr.done) : null;
  const hint = queuePill.querySelector(".hint");
  hint.textContent = left ? `${left} left` : "";
  queuePill.title = `${active.map((j) => j.book_title).filter(Boolean).join(", ")}${left ? ` · ${left} parts left in the current run` : ""}`;
}

// ---- session and polling -----------------------------------------------------------------------
// Polling runs only while signed in (or when this server has no sign-in), and never before the password is chosen.
let ollamaTimer = null, queueTimer = null;
function startPolling() {
  if (ollamaTimer) return;
  pollOllama(); pollQueue();
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
  ownHash = url;
  if (replace) history.replaceState(null, "", url);
  else location.hash = url;
}
let ownHash = null;   // the last hash a view wrote itself; anything else (links, pasted URLs) re-renders

function render() {
  const { route, parts, params } = parseHash();
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
  const section = { book: "library", series: "library", reader: "library", build: "library" }[route] || route;
  // Dialogs and drawers belong to the page that opened them.
  document.querySelectorAll(".modal-backdrop, .drawer, .drawer-backdrop").forEach((el) => el.remove());
  document.querySelectorAll(".tab").forEach((t) => {
    if (t.dataset.route === section) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
  });
  if (unmount) unmount();
  main.replaceChildren();
  const views = { library: [mountLibrary, "Library"], book: [mountBook, "Book"], series: [mountSeries, "Series"],
    studio: [mountStudio, "Runs"], build: [mountBuild, "Build"], reader: [mountReader, "Reader"], settings: [mountSettings, "Settings"],
    login: [mountLogin, "Sign in"], password: [mountPassword, "New password"] };
  const [mount, title] = views[route] || views.library;
  unmount = mount(main) || null;
  document.title = `${title} · Lorekeeper`;
  document.getElementById("settings-link").toggleAttribute("aria-current", route === "settings");
  for (const x of [runsLink, queuePill]) { if (route === "studio") x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current"); }
  if (ollamaTimer) pollQueue();
}

window.addEventListener("hashchange", () => {
  if (location.hash !== ownHash) render();
});
hydrateIcons();
applySession(await api.session());
render();
