// Landing page behaviour: the hero demo (a Reader in miniature over the bundled example index),
// the pipeline animation trigger, and the nav's current-section marker.
// Index text is rendered through textContent only, like the app.
import { DEMO } from "./demo-data.js";

const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);

/* ---------- hero demo ---------- */
const last = DEMO.anchors.length - 1;
const el = { range: $("demo-range"), pct: $("demo-pct"), ch: $("demo-ch"), lit: $("demo-lit"), orb: $("demo-orb"),
  recap: $("demo-recap"), events: $("demo-events"), evCount: $("demo-ev-count"), cast: $("demo-cast"), castCount: $("demo-cast-count"), empty: $("demo-empty"), more: $("demo-more") };

function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }

const CAST_ROWS = 6;
const state = { p: 0, shown: new Map(), events: 0 };

// Gating in one line (SPEC §7): an item is visible when the ordinal of its `from` anchor is at or before the position.
function ordinal(p) { return Math.max(0, Math.min(last, Math.floor(p * last))); }

function paint(p) {
  state.p = p;
  const ord = ordinal(p);
  el.pct.value = String(Math.round(p * 100));
  el.pct.textContent = String(Math.round(p * 100));
  el.range.setAttribute("aria-valuetext", `${Math.round(p * 100)} percent`);
  const a = DEMO.anchors[ord];
  el.ch.textContent = p === 0 ? "Before the first page" : a.ch;
  el.lit.style.width = `${p * 100}%`;
  el.orb.style.left = `${p * 100}%`;

  // story so far: the latest recap at or before the position
  const recap = DEMO.recaps.filter((r) => r.from <= ord).at(-1);
  el.recap.textContent = recap ? recap.text : "Nothing to recap yet. Move forward to see the story so far.";
  el.recap.classList.toggle("is-empty", !recap);

  // events, newest first
  const evs = DEMO.events.filter((e) => e.from <= ord);
  el.evCount.textContent = evs.length ? String(evs.length) : "";
  if (evs.length !== state.events) {
    el.events.replaceChildren(...evs.slice().reverse().slice(0, 3).map((e) => h("li", null, e.text)));
    state.events = evs.length;
  }

  // cast: the entries with the most known so far, newest first among equals; rows are keyed by id so one that stays keeps its node
  const visible = DEMO.entities.filter((e) => e.from <= ord);
  const known = (e) => e.facets.filter((f) => f.from <= ord).length;
  const cast = (e) => (e.type === "person" || e.type === "creature" ? 1 : 0);
  const shown = visible.slice().sort((a, b) => cast(b) - cast(a) || known(b) - known(a) || b.from - a.from || a.id.localeCompare(b.id)).slice(0, CAST_ROWS);
  const ids = new Set(shown.map((e) => e.id));
  for (const [id, node] of state.shown) if (!ids.has(id)) { node.remove(); state.shown.delete(id); }
  for (const e of shown) {
    const names = e.names.filter((n) => n.from <= ord);
    const all = e.facets.filter((f) => f.from <= ord);
    const facets = all.filter((f) => !f.k);
    const profile = all.filter((f) => f.k === "p").at(-1);
    let li = state.shown.get(e.id);
    if (!li) {
      li = h("li");
      li.append(h("span", `dot ${e.type}`), h("span", "name"), h("span", "n"), h("span", "fact"));
      state.shown.set(e.id, li);
    }
    el.cast.append(li);  // re-appending keeps the list in rank order
    // Like gating: the latest visible primary name, else the earliest visible name.
    const primary = names.filter((n) => n.p).sort((a, b) => b.from - a.from)[0];
    li.querySelector(".name").textContent = (primary || names[0] || e.names[0]).v;
    const n = li.querySelector(".n");
    n.textContent = facets.length ? `${facets.length} fact${facets.length === 1 ? "" : "s"}` : "named";
    n.classList.toggle("new", e.from === ord);
    li.querySelector(".fact").textContent = profile?.text || facets.at(-1)?.text || "Named, but nothing more is known yet.";
  }
  el.castCount.textContent = visible.length ? String(visible.length) : "";
  el.empty.hidden = visible.length > 0;
  const rest = visible.length - shown.length;
  el.more.textContent = rest > 0 ? `and ${rest} more named so far` : "";
}

el.range.addEventListener("input", () => { stopSweep(); paint(+el.range.value / 1000); });
paint(0);

// One orchestrated moment: the marker sweeps forward once on load, then it is the reader's.
let sweep = null;
function stopSweep() { if (sweep) { cancelAnimationFrame(sweep); sweep = null; } }
function startSweep() {
  if (reduced) { el.range.value = "400"; paint(0.4); return; }
  const to = 0.4, ms = 7000, t0 = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const tick = (now) => {
    const t = Math.min(1, Math.max(0, (now - t0) / ms));
    const p = ease(t) * to;
    el.range.value = String(Math.round(p * 1000));
    paint(p);
    sweep = t < 1 ? requestAnimationFrame(tick) : null;
  };
  sweep = requestAnimationFrame(tick);
}
if (document.visibilityState === "visible") setTimeout(startSweep, 600);
else document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !sweep && state.p === 0) startSweep(); }, { once: true });
for (const ev of ["pointerdown", "keydown"]) el.range.addEventListener(ev, stopSweep);

/* ---------- pipeline: play once when it scrolls into view ---------- */
const pipeline = $("pipeline");
if (pipeline) {
  if (reduced || !("IntersectionObserver" in window)) pipeline.classList.add("play");
  else {
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { pipeline.classList.add("play"); io.disconnect(); }
    }, { threshold: 0.35 });
    io.observe(pipeline);
  }
}

/* ---------- nav: mark the section in view ---------- */
const links = [...document.querySelectorAll(".nav-links a[href^='#']")];
if (links.length && "IntersectionObserver" in window) {
  const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const a = byId.get(e.target.id);
      if (!a) continue;
      if (e.isIntersecting) { for (const l of links) l.removeAttribute("aria-current"); a.setAttribute("aria-current", "true"); }
    }
  }, { rootMargin: "-40% 0px -55% 0px" });
  for (const id of byId.keys()) { const s = document.getElementById(id); if (s) io.observe(s); }
}
