// The Reader's Connections view: a network of everything revealed so far.
//
// Nodes are entities, shaped and coloured by type. Three kinds of edge: stated relations (solid),
// "appear together" from shared events and chapter recaps (dotted, weighted), and relations a later
// one replaced (dashed, only with history on). The map itself is the archipelago in islands.js;
// focus mode (rings around a selection) and the list view are drawn here. Every layout is computed
// only from what has been read, starting from where nodes sat before, so moving forward fills the
// map in instead of reshuffling it, and nothing about its shape hints at what's still to come
// (SPEC.md §7.4).
import { clear, describe, h, icon, TYPE_LABEL, TYPE_PLURAL } from "./dom.js";
import { archipelago, fit as fitMap, loadLibs, shape, zoomBy } from "./islands.js";

const TYPES = ["person", "creature", "place", "group", "object", "concept", "term"];
const NS = "http://www.w3.org/2000/svg";
// Where each type settles: people in the middle, everything else around them.
const ANCHOR = { person: [0, 0], creature: [0, -0.9], place: [-1, -0.55], group: [1, -0.55], object: [1, 0.6], concept: [-1, 0.6], term: [0, 1] };
const SPREAD = 420;
const KEY_FIGURES = 60;   // larger maps start with the most present entities only

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

/** A stable pseudo-random number in [-0.5, 0.5) per id, so layouts are deterministic. */
function jitter(id, salt = 0) {
  let x = 2166136261 ^ salt;
  for (const ch of id) x = Math.imul(x ^ ch.charCodeAt(0), 16777619);
  return ((x >>> 0) % 10000) / 10000 - 0.5;
}

const pairKey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);

/** Everything the view says about connections, as one graph. Independent of filters, so they never move nodes. */
function buildGraph(view, chapterMentions) {
  const ents = new Map(view.entities.map((e) => [e.id, e]));
  const edges = new Map();
  const edge = (a, b) => {
    const k = pairKey(a, b);
    if (!edges.has(k)) edges.set(k, { key: k, a, b, rels: [], old: [], events: 0, chapters: 0 });
    return edges.get(k);
  };
  for (const r of view.relations) if (ents.has(r.src) && ents.has(r.dst) && r.src !== r.dst) edge(r.src, r.dst).rels.push(r);
  for (const r of view.relationHistory) if (ents.has(r.src) && ents.has(r.dst) && r.src !== r.dst) edge(r.src, r.dst).old.push(r);
  const together = (ids, field) => {
    const u = [...new Set(ids)].filter((id) => ents.has(id));
    for (let i = 0; i < u.length; i++) for (let j = i + 1; j < u.length; j++) edge(u[i], u[j])[field] += 1;
  };
  for (const ev of view.events) together(ev.entities, "events");
  for (const ids of chapterMentions) together(ids, "chapters");
  for (const e of edges.values()) e.weight = e.events + e.chapters * 0.5;
  // Prominence: what the reader knows about it, plus how connected it is.
  const prom = new Map([...ents.keys()].map((id) => [id, ents.get(id).facets.length]));
  for (const e of edges.values()) {
    const w = e.rels.length * 2 + Math.min(e.weight, 4);
    prom.set(e.a, prom.get(e.a) + w);
    prom.set(e.b, prom.get(e.b) + w);
  }
  return { ents, edges: [...edges.values()], prom };
}

/**
 * Force layout over the whole graph, continuing from `pos` (Map id → {x, y}). Nodes placed before
 * move little; new ones start next to what they connect to.
 */
function layout(graph, pos) {
  const ids = [...graph.ents.keys()].filter((id) => graph.edges.some((e) => e.a === id || e.b === id));
  const known = new Set(ids.filter((id) => pos.has(id)));
  const nbrs = new Map(ids.map((id) => [id, []]));
  const links = graph.edges.filter((e) => nbrs.has(e.a) && nbrs.has(e.b) && (e.rels.length || e.old.length || e.weight >= 1));
  for (const e of links) { nbrs.get(e.a).push(e.b); nbrs.get(e.b).push(e.a); }
  const home = (id) => { const [ax, ay] = ANCHOR[graph.ents.get(id).type] || [0, 0]; return [ax * SPREAD, ay * SPREAD]; };
  // New nodes start beside placed neighbours (or at their type's home), slightly apart.
  const fresh = ids.filter((id) => !known.has(id)).sort((a, b) => graph.prom.get(b) - graph.prom.get(a));
  for (const id of fresh) {
    const placed = nbrs.get(id).filter((n) => pos.has(n));
    const [hx, hy] = home(id);
    const bx = placed.length ? placed.reduce((t, n) => t + pos.get(n).x, 0) / placed.length * 0.8 + hx * 0.2 : hx;
    const by = placed.length ? placed.reduce((t, n) => t + pos.get(n).y, 0) / placed.length * 0.8 + hy * 0.2 : hy;
    pos.set(id, { x: bx + jitter(id, 1) * 120, y: by + jitter(id, 2) * 120 });
  }
  if (!fresh.length) return ids;
  const P = ids.map((id) => pos.get(id));
  const index = new Map(ids.map((id, i) => [id, i]));
  const mob = ids.map((id) => (known.has(id) ? 0.15 : 1));
  const L = links.map((e) => [index.get(e.a), index.get(e.b), e.rels.length ? 0.06 : Math.min(0.05, 0.012 * Math.log2(1 + e.weight))]);
  const H = ids.map(home);
  const iters = known.size ? 90 : 260;
  const CELL = 320;
  for (let it = 0; it < iters; it++) {
    const t = 1 - it / iters;
    const F = P.map(() => [0, 0]);
    // Repulsion between nearby nodes only (grid buckets keep big casts fast).
    const grid = new Map();
    P.forEach((p, i) => {
      const k = `${Math.floor(p.x / CELL)},${Math.floor(p.y / CELL)}`;
      if (!grid.has(k)) grid.set(k, []);
      grid.get(k).push(i);
    });
    P.forEach((p, i) => {
      const gx = Math.floor(p.x / CELL), gy = Math.floor(p.y / CELL);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        for (const j of grid.get(`${gx + dx},${gy + dy}`) || []) {
          if (j <= i) continue;
          let vx = p.x - P[j].x, vy = p.y - P[j].y;
          let d2 = vx * vx + vy * vy;
          if (d2 < 1) { vx = jitter(ids[i], it) || 0.3; vy = jitter(ids[j], it) || -0.3; d2 = 1; }
          if (d2 > CELL * CELL) continue;
          const f = 11000 / d2;
          const d = Math.sqrt(d2);
          F[i][0] += vx / d * f; F[i][1] += vy / d * f;
          F[j][0] -= vx / d * f; F[j][1] -= vy / d * f;
        }
      }
    });
    for (const [a, b, k] of L) {
      const vx = P[b].x - P[a].x, vy = P[b].y - P[a].y;
      const d = Math.max(1, Math.hypot(vx, vy));
      const f = (d - 180) * k;
      F[a][0] += vx / d * f; F[a][1] += vy / d * f;
      F[b][0] -= vx / d * f; F[b][1] -= vy / d * f;
    }
    P.forEach((p, i) => {
      F[i][0] += (H[i][0] - p.x) * 0.005;
      F[i][1] += (H[i][1] - p.y) * 0.005;
      const step = Math.min(30 * t + 2, Math.hypot(F[i][0], F[i][1])) * mob[i];
      const n = Math.hypot(F[i][0], F[i][1]) || 1;
      p.x += F[i][0] / n * step;
      p.y += F[i][1] / n * step;
    });
  }
  return ids;
}

/** Hop distances from `root` over the given edges, up to `max`. */
function hops(root, edges, max) {
  const dist = new Map([[root, 0]]);
  let frontier = [root];
  for (let d = 1; d <= max; d++) {
    const next = [];
    for (const id of frontier) for (const e of edges) {
      const o = e.a === id ? e.b : e.b === id ? e.a : null;
      if (o && !dist.has(o)) { dist.set(o, d); next.push(o); }
    }
    frontier = next;
  }
  return dist;
}

/**
 * Paint the Connections view into `panel`.
 * ctx: { view, types (Set, shared with Cast & World), gs (persistent graph state), chapterMentions,
 *        revealed(id) → { where, isNew } | null, hereLabel, openEntity(id, el), repaint() }
 */
export function renderConnections(panel, ctx) {
  const { view, types, gs } = ctx;
  const graph = buildGraph(view, ctx.chapterMentions);
  gs.pos ||= new Map();
  const laid = new Set(layout(graph, gs.pos));
  if (gs.sel && !graph.ents.has(gs.sel)) gs.sel = null;   // moved back before it was revealed

  // What's drawn: filters decide nodes; toggles decide which edges count.
  const typeOn = (id) => !types.size || types.has(graph.ents.get(id).type);
  let shown = new Set([...laid].filter(typeOn));
  const everyoneCount = shown.size;
  // The archipelago folds minor entries itself, so the key-figures cut is for focus and the list.
  const islands = !gs.list && !(gs.mode === "focus" && gs.sel && laid.has(gs.sel));
  const trimmed = !islands && !gs.everyone && shown.size > KEY_FIGURES;
  if (trimmed) {
    const keep = new Set([...shown].sort((a, b) => graph.prom.get(b) - graph.prom.get(a)).slice(0, KEY_FIGURES));
    if (gs.sel && shown.has(gs.sel)) {
      keep.add(gs.sel);
      for (const e of graph.edges) if ((e.a === gs.sel || e.b === gs.sel) && (e.rels.length || e.weight > 0)) {
        const o = e.a === gs.sel ? e.b : e.a;
        if (shown.has(o)) keep.add(o);
      }
    }
    shown = keep;
  }
  // "Appear together" links: the weakest strength that keeps them to a readable number for this graph.
  const coWeights = graph.edges.filter((e) => !e.rels.length && e.weight > 0 && shown.has(e.a) && shown.has(e.b)).map((e) => e.weight);
  const budget = Math.max(24, Math.round(shown.size * 1.2));
  const minCo = [1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24].find((t) => coWeights.filter((w) => w >= t).length <= budget) ?? 32;
  const visibleEdge = (e) => shown.has(e.a) && shown.has(e.b)
    && (e.rels.length || (gs.history && e.old.length) || (gs.together && (e.weight >= minCo || ((e.a === gs.sel || e.b === gs.sel) && e.weight > 0))));
  let edges = graph.edges.filter(visibleEdge);
  const degree = (id) => edges.filter((e) => e.a === id || e.b === id).length;
  if (gs.hideMinor) {
    shown = new Set([...shown].filter((id) => degree(id) > 1 || id === gs.sel));
    edges = edges.filter(visibleEdge);
  }
  shown = new Set([...shown].filter((id) => degree(id) > 0 || id === gs.sel));
  const sel = gs.sel && shown.has(gs.sel) ? gs.sel : null;
  const focus = gs.mode === "focus" && sel;
  const dist = sel ? hops(sel, edges, gs.hops) : null;
  const inHood = (id) => !dist || dist.has(id);

  // Positions: the shared map, or rings around the selection in focus mode.
  const pos = new Map();
  if (focus) {
    pos.set(sel, { x: 0, y: 0 });
    for (let d = 1; d <= gs.hops; d++) {
      const ring = [...dist.keys()].filter((id) => dist.get(id) === d && shown.has(id))
        .sort((a, b) => TYPES.indexOf(graph.ents.get(a).type) - TYPES.indexOf(graph.ents.get(b).type) || graph.prom.get(b) - graph.prom.get(a));
      const R = 190 * d + Math.max(0, ring.length - 14 * d) * 6;
      ring.forEach((id, i) => {
        const t = -Math.PI / 2 + (i + (d % 2 ? 0 : 0.5)) * (2 * Math.PI / ring.length);
        pos.set(id, { x: Math.cos(t) * R * 1.3, y: Math.sin(t) * R });
      });
    }
    shown = new Set(pos.keys());
    // Only lines that lead one ring outward: chords within a ring just cross the picture.
    edges = edges.filter((e) => shown.has(e.a) && shown.has(e.b) && Math.abs(dist.get(e.a) - dist.get(e.b)) === 1);
  } else {
    for (const id of shown) pos.set(id, gs.pos.get(id));
  }

  // Sizes from prominence, relative to the most prominent thing visible.
  const maxProm = Math.max(1, ...[...shown].map((id) => graph.prom.get(id)));
  const radius = (id) => (focus && id === sel ? 30 : 8 + 18 * Math.sqrt(graph.prom.get(id) / maxProm));
  const ranked = [...shown].sort((a, b) => graph.prom.get(b) - graph.prom.get(a));
  const labelled = new Set(ranked.slice(0, Math.max(10, Math.round(ranked.length * 0.3))));

  // ---- toolbar ---------------------------------------------------------------------------
  const counts = Object.fromEntries(TYPES.map((t) => [t, [...laid].filter((id) => graph.ents.get(id).type === t).length]));
  const listId = `g-matches-${Math.random().toString(36).slice(2, 8)}`;
  const search = h("input.input", { type: "search", placeholder: "Find and focus…", value: gs.q || "", "aria-label": "Find a character, place or idea to select",
    role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": listId });
  const matches = h("div.g-matches", { id: listId, role: "listbox", "aria-label": "Matches", hidden: true });
  let found = [], hi = -1, closeTimer = 0;
  const optId = (i) => `${listId}-o${i}`;
  const paintHi = () => {
    [...matches.children].forEach((el, i) => el.setAttribute("aria-selected", String(i === hi)));
    if (hi >= 0) search.setAttribute("aria-activedescendant", optId(hi)); else search.removeAttribute("aria-activedescendant");
  };
  const closeList = () => { found = []; hi = -1; matches.hidden = true; clear(matches); search.setAttribute("aria-expanded", "false"); search.removeAttribute("aria-activedescendant"); };
  const pickMatch = (id) => { gs.q = ""; search.value = ""; closeList(); select(id); };
  search.addEventListener("input", () => {
    gs.q = search.value;
    const q = gs.q.trim().toLowerCase();
    found = q ? [...laid].map((id) => graph.ents.get(id)).filter((e) => [e.name, ...e.aliases].some((n) => n.toLowerCase().includes(q))).slice(0, 7) : [];
    hi = -1;
    matches.hidden = !found.length;
    search.setAttribute("aria-expanded", String(found.length > 0));
    clear(matches, found.map((e, i) => h("div", { id: optId(i), role: "option", "aria-selected": "false", tabindex: -1,
      on: { mousedown: (ev) => ev.preventDefault(), click: () => pickMatch(e.id), mousemove: () => { hi = i; paintHi(); } } },
      h(`span.g-swatch.t-${e.type}`), h("span", e.name), h("span.hint", TYPE_LABEL[e.type]))));
    paintHi();
  });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      if (!found.length) return;
      ev.preventDefault();
      hi = ev.key === "ArrowDown" ? (hi + 1) % found.length : (hi <= 0 ? found.length - 1 : hi - 1);
      paintHi();
      matches.children[hi]?.scrollIntoView?.({ block: "nearest" });
    } else if (ev.key === "Enter") {
      if (found.length) { ev.preventDefault(); pickMatch(found[hi >= 0 ? hi : 0].id); }
    } else if (ev.key === "Escape") {
      if (!matches.hidden) { ev.preventDefault(); ev.stopPropagation(); closeList(); }
    }
  });
  search.addEventListener("blur", () => { clearTimeout(closeTimer); closeTimer = setTimeout(closeList, 150); });
  search.addEventListener("focus", () => { clearTimeout(closeTimer); if (search.value.trim()) search.dispatchEvent(new Event("input")); });
  // ---- Display: one button, one non-modal panel ---------------------------------------------
  const present = TYPES.filter((t) => counts[t]);
  const shownType = (t) => !types.size || types.has(t);
  const setType = (t, on) => {
    const cur = new Set(present.filter(shownType));
    on ? cur.add(t) : cur.delete(t);
    types.clear();
    if (cur.size !== present.length) cur.forEach((x) => types.add(x));   // all shown = no filter
  };
  const refocus = (k) => queueMicrotask(() => panel.querySelector(`[data-k="${k}"]`)?.focus());
  const sw = (k, label, hint) => h("label.switch.g-row-switch", { title: hint },
    h("input", { type: "checkbox", "data-k": k, checked: Boolean(gs[k]), on: { change: (ev) => { gs[k] = ev.target.checked; refocus(k); ctx.repaint(); } } }), h("span", label));
  const seg = (items, key) => h("div.seg", { role: "group", "aria-label": key === "hops" ? "Reach" : "View" }, items.map(([v, label, on, off]) => h("button", { type: "button", "data-k": `${key}-${v}`,
    "aria-pressed": String(key === "hops" ? gs.hops === v : on), disabled: off ? true : null, title: off ? "Select something first" : null,
    on: { click: () => { if (key === "hops") gs.hops = v; else { gs.list = v === "list"; if (v !== "list") gs.mode = v; } refocus(`${key}-${v}`); ctx.repaint(); } } }, label)));
  const row = (label, ...kids) => h("div.g-d-row", h("div.g-d-label", label), h("div.g-d-ctl", ...kids));
  const dispId = `g-display-${Math.random().toString(36).slice(2, 8)}`;
  const dispBtn = h("button.btn.g-display-btn", { type: "button", "aria-expanded": String(Boolean(gs.dispOpen)), "aria-controls": dispId, "aria-haspopup": "dialog",
    on: { click: () => (gs.dispOpen ? closeDisplay(true) : openDisplay()) } },
    s("svg", { class: "svg-icon", viewBox: "0 0 24 24", "aria-hidden": "true", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" },
      s("path", { d: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" }), s("circle", { cx: 15, cy: 6, r: 2 }), s("circle", { cx: 9, cy: 12, r: 2 }), s("circle", { cx: 17, cy: 18, r: 2 })),
    "Display");
  const dispPanel = h("div.g-display-panel", { id: dispId, role: "dialog", "aria-label": "Display options", tabindex: -1, hidden: !gs.dispOpen },
    row("Show", h("div.g-d-types", present.map((t) => h("label.g-d-type", h("input", { type: "checkbox", "data-k": `type-${t}`, checked: shownType(t),
      on: { change: (ev) => { setType(t, ev.target.checked); refocus(`type-${t}`); ctx.repaint(); } } }),
      h(`span.g-swatch.t-${t}`), h("span", TYPE_PLURAL[t]), h("span.hint", String(counts[t])))))),
    row("Reach", seg([[1, "1 step"], [2, "2 steps"]], "hops")),
    row("Lines",
      sw("together", "Appear together", "Dotted lines between characters who share events or chapters. Off: only stated relationships are drawn."),
      sw("history", "Show history", "Show relationships that later changed, dashed."),
      islands ? null : sw("hideMinor", "Hide minor", "Hide anything with one connection or fewer.")),
    islands ? row("Include places and things", sw("things", "Places, groups, objects, ideas", "Also map them, joined only by stated relationships.")) : null,
    row("View", seg([["map", "Map", !gs.list && gs.mode === "map"], ["focus", "Focus", !gs.list && gs.mode === "focus", !sel], ["list", "List", Boolean(gs.list)]], "mode")));
  const outside = (ev) => { if (!dispPanel.isConnected) return off(); if (!dispPanel.contains(ev.target) && !dispBtn.contains(ev.target)) closeDisplay(false); };
  const onKey = (ev) => { if (!dispPanel.isConnected) return off(); if (ev.key === "Escape" && gs.dispOpen) { ev.preventDefault(); ev.stopPropagation(); closeDisplay(true); } };
  const off = () => { document.removeEventListener("mousedown", outside, true); document.removeEventListener("keydown", onKey, true); window.removeEventListener("hashchange", routeOff); };
  const routeOff = () => { gs.dispOpen = false; off(); };
  gs.dispOff?.(); gs.dispOff = off;
  function listen() { document.addEventListener("mousedown", outside, true); document.addEventListener("keydown", onKey, true); window.addEventListener("hashchange", routeOff); }
  function openDisplay() { gs.dispOpen = true; dispPanel.hidden = false; dispBtn.setAttribute("aria-expanded", "true"); listen(); (dispPanel.querySelector("input,button:not([disabled])") || dispPanel).focus(); }
  function closeDisplay(back) { gs.dispOpen = false; dispPanel.hidden = true; dispBtn.setAttribute("aria-expanded", "false"); off(); if (back) dispBtn.focus(); }
  if (gs.dispOpen) listen();
  const summary = [gs.hops === 2 ? "2 steps" : null, gs.together ? "together" : null, gs.history ? "history" : null, islands && gs.things ? "places & things" : null,
    !islands && gs.hideMinor ? "hide minor" : null, types.size && types.size < present.length ? `${types.size} of ${present.length} types` : null, gs.list ? "list" : null].filter(Boolean).join(", ");
  const toolbar = h("div.g-toolbar",
    h("div.filters", h("div.search.g-search", icon("search"), search, matches)),
    h("div.g-display", h("span.g-summary", { "aria-live": "polite" }, summary), dispBtn, dispPanel));

  function select(id) { gs.sel = id; if (id && types.size && !types.has(graph.ents.get(id).type)) types.add(graph.ents.get(id).type); ctx.repaint(); }
  const nameOf = (id) => graph.ents.get(id)?.name || id;

  if (!laid.size) {
    return clear(panel, h("div.empty", h("div.art", icon("link")), h("h3", "No connections yet"),
      h("p", "Relationships, and who appears with whom, show up here as the book reveals them.")));
  }

  // ---- side panel ------------------------------------------------------------------------
  const side = h("aside.g-panel", { "aria-label": "Details" });
  if (sel) {
    const e = graph.ents.get(sel);
    const r = ctx.revealed(sel);
    const mine = graph.edges.filter((x) => (x.a === sel || x.b === sel) && typeOn(x.a === sel ? x.b : x.a));
    const relRows = [];
    for (const x of mine) for (const [rel, old] of [...x.rels.map((q) => [q, false]), ...(gs.history ? x.old.map((q) => [q, true]) : [])]) {
      const other = rel.src === sel ? rel.dst : rel.src;
      const at = ctx.revealed(rel.id);
      const link = h("button.linkish", { type: "button", on: { click: () => select(other) } }, nameOf(other));
      relRows.push(h(`li${old ? ".old" : ""}${at?.isNew ? ".new" : ""}`,
        h("span.g-sentence", rel.src === sel ? [h("b", e.name), " ", h("i", rel.label), " ", link] : [link, " ", h("i", rel.label), " ", h("b", e.name)],
          old ? h("span.hint", " (earlier)") : null),
        at ? h("span.g-when", at.where) : null));
    }
    const co = mine.filter((x) => x.weight > 0).sort((a, b) => b.weight - a.weight).slice(0, 8);
    const coMax = Math.max(1, ...co.map((x) => x.weight));
    const older = mine.reduce((n, x) => n + x.old.length, 0);
    const about = describe(e);
    clear(side,
      h("div.g-head", h(`span.g-badge.t-${e.type}`, icon(e.type)),
        h("div", h("span.eyebrow", TYPE_LABEL[e.type], r?.isNew ? h("span.new-badge", "New") : null), h("h3", e.name)),
        h("button.icon-btn.small", { type: "button", "aria-label": "Clear selection", on: { click: () => { gs.sel = null; gs.mode = "map"; ctx.repaint(); } } }, icon("x"))),
      about ? h("p.g-desc", about) : null,
      h("p.hint", [r ? `First seen ${r.where}` : null, `${degree(sel)} connection${degree(sel) === 1 ? "" : "s"} so far`].filter(Boolean).join(" · ")),
      relRows.length ? h("div.g-sec", h("span.eyebrow", "Relationships"), h("ul.g-rels", relRows)) : null,
      older && !gs.history ? h("button.linkish", { type: "button", on: { click: () => { gs.history = true; ctx.repaint(); } } },
        `Show ${older} earlier relationship${older === 1 ? "" : "s"}`) : null,
      co.length ? h("div.g-sec", h("span.eyebrow", "Appears with"), h("ul.g-co", co.map((x) => {
        const other = x.a === sel ? x.b : x.a;
        const bits = [x.events && `${x.events} event${x.events === 1 ? "" : "s"}`, x.chapters && `${x.chapters} chapter${x.chapters === 1 ? "" : "s"}`].filter(Boolean);
        return h("li", h("button.linkish", { type: "button", on: { click: () => select(other) } }, nameOf(other)),
          h("span.g-bar", h("i", { style: { width: `${Math.round(x.weight / coMax * 100)}%` } })), h("span.hint", bits.join(", ")));
      }))) : null,
      h("div.g-actions",
        h("button.btn.primary", { type: "button", on: { click: () => { gs.mode = focus ? "map" : "focus"; gs.list = false; ctx.repaint(); } } },
          icon(focus ? "layers" : "eye"), focus ? "Back to the full map" : `Focus on ${e.name}`),
        h("button.btn", { type: "button", on: { click: (ev) => ctx.openEntity(sel, ev.currentTarget) } }, icon("person"), "Open details")));
  } else {
    const fresh = ranked.filter((id) => ctx.revealed(id)?.isNew).slice(0, 10);
    clear(side, h("h3.g-pick", "Pick anyone or anything"),
      h("p.g-desc", "Select a node to light up its connections and read them as sentences. Everything here comes from the text up to your position."),
      fresh.length ? h("div.g-sec", h("span.eyebrow", `New in ${ctx.hereLabel}`), h("ul.g-new", fresh.map((id) => {
        const e = graph.ents.get(id);
        return h("li", h("button", { type: "button", on: { click: () => select(id) } }, h(`span.g-swatch.t-${e.type}`), h("span", e.name), h("span.hint", TYPE_LABEL[e.type])));
      }))) : null);
  }

  // ---- list view -------------------------------------------------------------------------
  if (gs.list) {
    const rows = [];
    for (const e of edges) {
      const touches = sel && (e.a === sel || e.b === sel);
      for (const r of e.rels) rows.push([touches, `${nameOf(r.src)} — ${r.label} — ${nameOf(r.dst)}`, "Stated", ctx.revealed(r.id)?.where || ""]);
      if (gs.history) for (const r of e.old) rows.push([touches, `${nameOf(r.src)} — ${r.label} — ${nameOf(r.dst)}`, "Earlier", ctx.revealed(r.id)?.where || ""]);
      if (gs.together && e.weight > 0 && !e.rels.length) rows.push([touches, `${nameOf(e.a)} and ${nameOf(e.b)} appear together`,
        [e.events && `${e.events} event${e.events === 1 ? "" : "s"}`, e.chapters && `${e.chapters} chapter${e.chapters === 1 ? "" : "s"}`].filter(Boolean).join(", "), ""]);
    }
    rows.sort((a, b) => b[0] - a[0]);
    return clear(panel, toolbar, h("div.g-body",
      h("div.g-stage.g-list", h("div.table-wrap", h("table.data",
        h("caption.hint", "The same connections as the map. Display settings still apply."),
        h("thead", h("tr", ["Connection", "Kind", "Revealed"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", rows.map(([mine, text, kind, when]) => h(`tr${mine ? ".selected" : ""}`, h("td.g-sentence", text), h("td", kind), h("td.hint", when))))))),
      side));
  }

  // ---- the map: the archipelago --------------------------------------------------------------
  if (!focus) {
    const zoomBtn = (label, ic, fn) => h("button.icon-btn.small", { type: "button", "aria-label": label, title: label, on: { click: fn } }, ic);
    const head = h("div.g-stage-head", h("h3", "Connections"),
      h("span.hint", "Islands are groups tied by stated relationships."),
      h("span.g-zoom", zoomBtn("Zoom in", h("b", "+"), () => gs.arch && zoomBy(gs.arch, 1.4)), zoomBtn("Zoom out", h("b", "−"), () => gs.arch && zoomBy(gs.arch, 0.7)),
        zoomBtn("Fit everything", icon("layers"), () => gs.arch && fitMap(gs.arch))));
    const legend = h("div.g-legend", { role: "note", "aria-label": "Legend" },
      h("span", h("i.l-isl"), "Island = characters tied together"),
      h("span", h("i.l-rel"), "Stated relationship"),
      gs.together ? h("span", h("i.l-co"), "Appear together") : null,
      gs.history ? h("span", h("i.l-old"), "Earlier relationship") : null,
      h("span", h("i.l-new"), `New in ${ctx.hereLabel}`),
      h("span.g-legend-note", "Ctrl-scroll or pinch to zoom. Only what you've read so far."));
    if (!window.graphologyLibrary) {
      loadLibs().then(() => ctx.repaint(), (err) => clear(panel, h("div.notice.warn", icon("alert"), h("div", "The map couldn't load: ", err.message))));
      return clear(panel, toolbar, h("div.g-body", h("div.g-stage", head, h("div.empty", h("p", "Drawing the map…")), legend), side));
    }
    const maxAll = Math.max(1, ...[...laid].map((id) => graph.prom.get(id)));
    const wrap = archipelago({ gs, graph, laid, typeOn, sel, minCo, revealed: ctx.revealed, select,
      radius: (id) => 8 + 18 * Math.sqrt(graph.prom.get(id) / maxAll) });
    return clear(panel, toolbar, h("div.g-body", h("div.g-stage", head, wrap, legend), side));
  }

  // ---- focus mode: rings around the selection ---------------------------------------------------
  const lit = (e) => !dist || (dist.has(e.a) && dist.has(e.b) && Math.min(dist.get(e.a), dist.get(e.b)) < gs.hops);
  const edgeEls = edges.map((e) => {
    const p = pos.get(e.a), q = pos.get(e.b);
    const kind = e.rels.length ? "rel" : e.old.length && gs.history && !(gs.together && e.weight > 0) ? "old" : "co";
    const w = kind === "co" ? Math.min(1.2 + e.weight * 0.45, 4.5) : 1.8;
    return s("line", { x1: p.x.toFixed(1), y1: p.y.toFixed(1), x2: q.x.toFixed(1), y2: q.y.toFixed(1),
      class: `g-edge ${kind}${dist ? (lit(e) ? " lit" : " dim") : ""}`, "stroke-width": w });
  });
  // Edge labels around the selection: each takes the first spot along its edge that collides with
  // no other label or node; if none is free it's left to the side panel.
  const boxes = [...shown].map((id) => { const p = pos.get(id), r = radius(id) + 4; return { x: p.x - r, y: p.y - r, w: r * 2, h: r * 2 }; });
  const hit = (b) => boxes.some((o) => b.x < o.x + o.w && o.x < b.x + b.w && b.y < o.y + o.h && o.y < b.y + b.h);
  const labelEls = [];
  if (sel) for (const e of edges.filter((x) => (x.a === sel || x.b === sel) && (x.rels.length || (gs.history && x.old.length)))) {
    const p = pos.get(e.a === sel ? e.a : e.b), q = pos.get(e.a === sel ? e.b : e.a);   // p = the selection
    const rel = e.rels[0] || e.old[0];
    let text = (e.rels.length ? "" : "earlier: ") + rel.label + (e.rels.length > 1 ? ` +${e.rels.length - 1}` : "");
    if (text.length > 30) text = `${text.slice(0, 29)}…`;
    const wdt = text.length * 6.3 + 16, hgt = 20;
    for (const t of [0.55, 0.7, 0.4, 0.82, 0.3]) {
      const cx = p.x + (q.x - p.x) * t, cy = p.y + (q.y - p.y) * t;
      const b = { x: cx - wdt / 2, y: cy - hgt / 2, w: wdt, h: hgt };
      if (hit(b)) continue;
      boxes.push(b);
      labelEls.push(s("g", { class: `g-elabel${e.rels.length ? "" : " old"}`, transform: `translate(${cx.toFixed(1)},${cy.toFixed(1)})` },
        s("rect", { x: -wdt / 2, y: -hgt / 2, width: wdt, height: hgt, rx: 10 }), s("text", { "text-anchor": "middle", y: 4 }, text)));
      break;
    }
  }
  const labelBoxes = [];
  const labelFits = new Set();
  for (const id of ranked) {
    const wants = labelled.has(id) || id === sel || (dist && dist.get(id) === 1) || focus;
    if (!wants) continue;
    const p = pos.get(id), r = radius(id), big = r >= 18;
    const w = graph.ents.get(id).name.length * (big ? 8.2 : 6.6) + 6, hh = big ? 18 : 14;
    const b = { x: p.x - w / 2, y: p.y + r + 2, w, h: hh };
    if (id !== sel && labelBoxes.some((o) => b.x < o.x + o.w && o.x < b.x + b.w && b.y < o.y + o.h && o.y < b.y + b.h)) continue;
    labelBoxes.push(b);
    labelFits.add(id);
  }
  const nodeEls = ranked.slice().reverse().map((id) => {
    const e = graph.ents.get(id), p = pos.get(id), r = radius(id);
    const isNew = ctx.revealed(id)?.isNew;
    const showLabel = labelFits.has(id);
    const g = s("g", { class: `g-node t-${e.type}${id === sel ? " sel" : ""}${dist && !inHood(id) ? " dim" : ""}${isNew ? " new" : ""}`,
      transform: `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`, tabindex: 0, role: "button",
      "aria-label": `${e.name}, ${TYPE_LABEL[e.type]}, ${degree(id)} connection${degree(id) === 1 ? "" : "s"}${isNew ? ", new" : ""}`,
      "aria-pressed": String(id === sel) });
    if (isNew) g.append(s("circle", { class: "g-halo", r: r + 10 }));
    g.append(s("circle", { class: "g-ring", r: r + 6 }));
    const sh = shape(e.type, r); sh.setAttribute("class", "g-shape"); g.append(sh);
    if (r >= 13) {
      const ic = icon(e.type);
      const sz = r * 1.05;
      for (const [k, v] of Object.entries({ x: -sz / 2, y: -sz / 2, width: sz, height: sz })) ic.setAttribute(k, v);
      ic.setAttribute("class", "svg-icon g-icon");
      Object.assign(ic.style, { width: `${sz}px`, height: `${sz}px` });
      g.append(ic);
    }
    const big = r >= 18;
    g.append(s("text", { class: `g-label${big ? " big" : ""}${showLabel ? " on" : ""}`, "text-anchor": "middle", y: r + (big ? 18 : 14) }, e.name));
    g.addEventListener("click", (ev) => { ev.stopPropagation(); select(id === gs.sel ? null : id); });
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(id === gs.sel ? null : id); } });
    return g;
  });
  const rings = focus ? Array.from({ length: gs.hops }, (_, i) => {
    const ring = [...pos.keys()].filter((id) => dist.get(id) === i + 1);
    const R = 190 * (i + 1) + Math.max(0, ring.length - 14 * (i + 1)) * 6;
    return s("ellipse", { class: "g-orbit", rx: R * 1.3, ry: R });
  }) : [];

  // Viewport: fit to content unless the reader has zoomed or panned.
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const pad = 70;
  const fit = xs.length ? { x: Math.min(...xs) - pad, y: Math.min(...ys) - pad, w: Math.max(...xs) - Math.min(...xs) + pad * 2, h: Math.max(...ys) - Math.min(...ys) + pad * 2 } : { x: -300, y: -200, w: 600, h: 400 };
  if (fit.w < 960) { fit.x -= (960 - fit.w) / 2; fit.w = 960; }
  if (fit.h < 620) { fit.y -= (620 - fit.h) / 2; fit.h = 620; }
  const vb = gs.userView && gs.vbMode === (focus ? "focus" : "map") ? gs.vb : fit;
  const svg = s("svg", { class: `g-svg${edges.length > 80 ? " busy" : ""}`, viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`, role: "group", "aria-label": "Connections map" },
    s("g", {}, rings), s("g", {}, edgeEls), s("g", {}, nodeEls), s("g", {}, labelEls));
  const setVb = (next) => { gs.vb = next; gs.userView = true; gs.vbMode = focus ? "focus" : "map"; svg.setAttribute("viewBox", `${next.x} ${next.y} ${next.w} ${next.h}`); };
  const zoom = (f, cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2) => {
    const cur = gs.userView ? gs.vb : vb;
    setVb({ x: cx - (cx - cur.x) * f, y: cy - (cy - cur.y) * f, w: cur.w * f, h: cur.h * f });
  };
  svg.addEventListener("wheel", (ev) => {
    if (!ev.ctrlKey && !ev.metaKey) return;   // plain scrolling scrolls the page; pinch and ctrl-wheel zoom
    ev.preventDefault();
    const r = svg.getBoundingClientRect(), cur = gs.userView ? gs.vb : vb;
    const scale = Math.max(cur.w / r.width, cur.h / r.height);
    zoom(Math.exp(ev.deltaY * 0.01), cur.x + (ev.clientX - r.left) * scale, cur.y + (ev.clientY - r.top) * scale);
  }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", (ev) => { if (ev.target.closest(".g-node")) return; drag = { x: ev.clientX, y: ev.clientY, vb: { ...(gs.userView ? gs.vb : vb) }, moved: false }; svg.setPointerCapture(ev.pointerId); });
  svg.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const r = svg.getBoundingClientRect(), scale = Math.max(drag.vb.w / r.width, drag.vb.h / r.height);
    const dx = (ev.clientX - drag.x) * scale, dy = (ev.clientY - drag.y) * scale;
    if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 4) drag.moved = true;
    if (drag.moved) setVb({ ...drag.vb, x: drag.vb.x - dx, y: drag.vb.y - dy });
  });
  svg.addEventListener("pointerup", () => { if (drag && !drag.moved && gs.sel) { gs.sel = null; gs.mode = "map"; ctx.repaint(); } drag = null; });

  const zoomBtn = (label, ic, fn) => h("button.icon-btn.small", { type: "button", "aria-label": label, title: label, on: { click: fn } }, ic);
  const head = h("div.g-stage-head",
    h("h3", focus ? `Around ${nameOf(sel)}` : "Connections"),
    h("span.hint", focus ? `Everything ${gs.hops === 1 ? "one step" : "up to two steps"} away` : "People in the middle; places, groups and ideas around them"),
    trimmed || gs.everyone && everyoneCount > KEY_FIGURES ? h("span.g-trim", trimmed ? `The ${KEY_FIGURES} most present of ${everyoneCount}.` : `All ${everyoneCount}.`,
      " ", h("button.linkish", { type: "button", on: { click: () => { gs.everyone = !gs.everyone; ctx.repaint(); } } }, trimmed ? "Show everyone" : "Key figures only")) : null,
    h("span.g-zoom", zoomBtn("Zoom in", h("b", "+"), () => zoom(0.8)), zoomBtn("Zoom out", h("b", "−"), () => zoom(1.25)),
      zoomBtn("Fit everything", icon("layers"), () => { gs.userView = false; ctx.repaint(); })));
  const legend = h("div.g-legend", { role: "note", "aria-label": "Legend" },
    h("span", h("i.l-rel"), "Stated relationship"),
    gs.together ? h("span", h("i.l-co"), "Appear together (thicker = more often)") : null,
    gs.history ? h("span", h("i.l-old"), "Earlier relationship") : null,
    h("span", h("i.l-new"), `New in ${ctx.hereLabel}`),
    h("span", h("i.l-size"), "Size = how present so far"),
    h("span.g-legend-note", "Only what you've read so far."));
  clear(panel, toolbar, h("div.g-body", h("div.g-stage", head, edges.length ? svg
    : h("div.empty", h("h3", "Nothing connects here yet"), h("p", "Try showing more categories or turning on “Appear together”.")), legend), side));
}
