// The Connections map as an archipelago: groups that keep company become islands.
//
// Groups come from Louvain community detection; the layout has two levels (ForceAtlas2 inside each
// island, then the islands packed with sea between them), because one global layout piles groups
// on top of each other once storylines cross. Both are computed only from the gated view and
// continue from the previous positions and island ids, so reading on grows the map instead of
// reshuffling it, and nothing about it hints at what's still to come (SPEC.md §7.4).
//
// The map is one persistent SVG kept in the graph state across repaints, so zooming, panning and
// dragging stay smooth. Zoom decides the level of detail:
//   overview  islands and the sea routes between them, no names
//   islands   main entries with names where they fit; minor ones fold into "+N minor"
//   close up  everything, plus who appears together within an island
// d3 and graphology are vendored (static/vendor) and loaded the first time the map opens.
import { h, icon, TYPE_LABEL } from "./dom.js";

const NS = "http://www.w3.org/2000/svg";
const ISLAND_COLORS = 10;
const LEVELS = ["overview", "islands", "close"];
const CAST = new Set(["person", "creature"]);

let libs = null;
/** Load d3 and graphology once; resolves when their globals exist. */
export function loadLibs() {
  if (window.d3 && window.graphology && window.graphologyLibrary) return Promise.resolve();
  libs ||= ["d3.min.js", "graphology.umd.min.js", "graphology-library.min.js"].reduce((p, f) => p.then(() => new Promise((ok, fail) => {
    const sc = document.createElement("script");
    sc.src = `/static/vendor/${f}`;
    sc.onload = ok;
    sc.onerror = () => { libs = null; fail(new Error(`couldn't load ${f}`)); };
    document.head.append(sc);
  })), Promise.resolve());
  return libs;
}

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v === true ? "" : String(v));
  for (const c of children.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

/** Outline of a node of radius r, centred on 0,0. Shape is the type's second channel besides colour. */
export function shape(type, r) {
  const poly = (pts) => s("polygon", { points: pts.map(([x, y]) => `${(x * r).toFixed(1)},${(y * r).toFixed(1)}`).join(" ") });
  switch (type) {
    case "creature": return poly([[0, -1.1], [1.05, 0.75], [-1.05, 0.75]]);
    case "place": return s("rect", { x: -r * 0.9, y: -r * 0.9, width: r * 1.8, height: r * 1.8, rx: r * 0.5 });
    case "group": return poly([[-0.5, -0.92], [0.5, -0.92], [1.05, 0], [0.5, 0.92], [-0.5, 0.92], [-1.05, 0]]);
    case "object": return poly([[0, -1.15], [1.15, 0], [0, 1.15], [-1.15, 0]]);
    case "concept": return poly([[-0.42, -1], [0.42, -1], [1, -0.42], [1, 0.42], [0.42, 1], [-0.42, 1], [-1, 0.42], [-1, -0.42]]);
    case "term": return poly([[-1, -0.8], [0.45, -0.8], [1.1, 0], [0.45, 0.8], [-1, 0.8]]);
    default: return s("circle", { r });
  }
}

/** A stable pseudo-random number in [-0.5, 0.5) per id and salt, so layouts are reproducible. */
function jitter(id, salt = 0) {
  let x = 2166136261 ^ salt;
  for (const ch of id) x = Math.imul(x ^ ch.charCodeAt(0), 16777619);
  return ((x >>> 0) % 10000) / 10000 - 0.5;
}
function rng(seed) {
  return () => { seed |= 0; seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// How strongly two entries belong together. A stated relationship is the real tie. Sharing events
// counts a little, from two shared events up: being named in the same chapter summary does not
// count at all (a villain everyone talks about isn't part of every family he's mentioned with).
const sharedEvents = (e) => (e.events >= 2 ? Math.min(e.events, 4) * 0.5 : 0);
const weightOf = (e) => e.rels.length * 4 + e.old.length + sharedEvents(e);
// Titles dropped before reading a family name off a name ("Mr. Dursley" → Dursley).
function findIslands(A, graph, ids, links) {
  const G = new graphology.Graph({ type: "undirected" });
  for (const id of ids) G.addNode(id);
  for (const e of links) G.mergeEdge(e.a, e.b, { weight: weightOf(e) });
  const part = graphologyLibrary.communitiesLouvain(G, { getEdgeWeight: "weight", rng: rng(7), resolution: ids.length > 150 ? 1.6 : 1.2 });
  const groups = new Map();
  for (const id of ids) { const c = part[id]; if (!groups.has(c)) groups.set(c, []); groups.get(c).push(id); }
  const used = new Set(), list = [], of = new Map();
  for (const m of [...groups.values()].sort((a, b) => b.length - a.length)) {
    const votes = new Map();
    for (const id of m) { const v = A.lastIsland.get(id); if (v != null && !used.has(v)) votes.set(v, (votes.get(v) || 0) + graph.prom.get(id) + 1); }
    const best = [...votes.entries()].sort((a, b) => b[1] - a[1])[0];
    const id = best ? best[0] : A.nextIsland++;
    used.add(id);
    // The island's centre is whoever has the most ties inside it, not whoever is most famous
    // overall: a villain mentioned everywhere must not name the family he's tied to once.
    const inner = new Map(m.map((x) => [x, 0]));
    for (const e of links) if (part[e.a] === part[e.b] && inner.has(e.a)) { inner.set(e.a, inner.get(e.a) + weightOf(e)); inner.set(e.b, inner.get(e.b) + weightOf(e)); }
    m.sort((a, b) => inner.get(b) - inner.get(a) || graph.prom.get(b) - graph.prom.get(a));
    const top = graph.ents.get(m[0]);
    const hub = m.map((x) => graph.ents.get(x)).find((e) => (e.type === "place" || e.type === "group") && inner.get(e.id) >= inner.get(top.id) * 0.6);
    // A group is named for the entry it gathers around ("Around Van Helsing"); a lone entry is just its name.
    const isl = { id, members: m, color: `var(--isl-${id % ISLAND_COLORS})`, hub: Boolean(hub), top: top.name,
      label: m.length > 1 ? `Around ${top.name}` : top.name };
    list.push(isl);
    for (const x of m) of.set(x, isl);
  }
  const dup = new Map();
  for (const i of list) dup.set(i.label, (dup.get(i.label) || 0) + 1);
  for (const i of list) if (dup.get(i.label) > 1) i.label = `${i.label} (${i.members.length})`;
  for (const [x, i] of of) A.lastIsland.set(x, i.id);
  return { list, of };
}

/**
 * Two-level layout: ForceAtlas2 inside each island, then islands packed as circles (linked islands
 * nearer each other). Returns Map id → {x, y}; keeps per-island positions and centres in A.
 */
function layoutIslands(A, graph, isl, links, radius) {
  const FA2 = graphologyLibrary.layoutForceAtlas2;
  const inside = new Map(isl.list.map((i) => [i.id, []]));
  const cross = new Map();
  for (const e of links) {
    const a = isl.of.get(e.a), b = isl.of.get(e.b);
    if (a === b) { inside.get(a.id).push(e); continue; }
    const k = a.id < b.id ? `${a.id}|${b.id}` : `${b.id}|${a.id}`;
    const r = cross.get(k) || { source: a.id, target: b.id, w: 0 };
    r.w += weightOf(e);
    cross.set(k, r);
  }
  for (const i of isl.list) {
    const sub = new graphology.Graph({ type: "undirected" });
    let fresh = false;
    for (const id of i.members) {
      let p = A.local.get(id);
      if (!p) {
        fresh = true;
        const nb = [...inside.get(i.id)].filter((e) => e.a === id || e.b === id).map((e) => A.local.get(e.a === id ? e.b : e.a)).filter(Boolean);
        p = nb.length ? { x: nb.reduce((t, q) => t + q.x, 0) / nb.length + jitter(id, 1), y: nb.reduce((t, q) => t + q.y, 0) / nb.length + jitter(id, 2) }
          : { x: jitter(id, 3) * 10, y: jitter(id, 4) * 10 };
      }
      sub.addNode(id, { x: p.x, y: p.y });
    }
    for (const e of inside.get(i.id)) sub.mergeEdge(e.a, e.b, { weight: weightOf(e) });
    if (sub.order > 2) FA2.assign(sub, { iterations: fresh ? 200 : 40, getEdgeWeight: "weight",
      settings: { ...FA2.inferSettings(sub), gravity: 1, scalingRatio: 3, barnesHutOptimize: sub.order > 200 } });
    // Centre it and size it by headcount, so an island's area grows with the people on it.
    const pts = sub.mapNodes((id, a) => ({ id, x: a.x, y: a.y }));
    const cx = pts.reduce((t, q) => t + q.x, 0) / pts.length, cy = pts.reduce((t, q) => t + q.y, 0) / pts.length;
    const far = Math.max(1e-6, ...pts.map((q) => Math.hypot(q.x - cx, q.y - cy)));
    i.r = 34 * Math.sqrt(i.members.length) + 28;   // loose, with room between entries
    const room = Math.max(0, i.r - 26);
    for (const q of pts) A.local.set(q.id, { x: ((q.x - cx) / far) * room, y: ((q.y - cy) / far) * room });
  }
  const mlinks = [...cross.values()];
  const meta = isl.list.map((i) => {
    let c = A.centres.get(i.id);
    if (!c) {
      const near = mlinks.filter((m) => (m.source === i.id || m.target === i.id) && A.centres.has(m.source === i.id ? m.target : m.source)).sort((x, y) => y.w - x.w)[0];
      const o = near && A.centres.get(near.source === i.id ? near.target : near.source);
      c = o ? { x: o.x + jitter(String(i.id), 5) * 160, y: o.y + jitter(String(i.id), 6) * 160 } : { x: jitter(String(i.id), 7) * 600, y: jitter(String(i.id), 8) * 600 };
    }
    return { id: i.id, r: i.r, x: c.x, y: c.y };
  });
  const settled = isl.list.every((i) => A.centres.has(i.id));
  const sim = d3.forceSimulation(meta)
    .force("link", d3.forceLink(mlinks).id((d) => d.id).distance((d) => d.source.r + d.target.r + 150).strength((d) => Math.min(0.6, 0.02 + Math.log1p(d.w) / 25)))
    .force("charge", d3.forceManyBody().strength((d) => -6 * d.r))
    .force("collide", d3.forceCollide((d) => d.r + 110).strength(1).iterations(3))
    .force("x", d3.forceX(0).strength(0.03)).force("y", d3.forceY(0).strength(0.03)).stop();
  sim.alpha(settled ? 0.3 : 1);
  for (let k = 0; k < (settled ? 120 : 400); k++) sim.tick();
  for (const m of meta) A.centres.set(m.id, { x: m.x, y: m.y });
  // Each entry at island centre + its spot on the island, then overlaps nudged apart.
  const nodes = [...isl.of.keys()].map((id) => {
    const c = A.centres.get(isl.of.get(id).id), p = A.local.get(id);
    return { id, tx: c.x + p.x, ty: c.y + p.y, x: c.x + p.x, y: c.y + p.y, r: radius(id) };
  });
  const nudge = d3.forceSimulation(nodes).force("x", d3.forceX((d) => d.tx).strength(0.6)).force("y", d3.forceY((d) => d.ty).strength(0.6))
    .force("collide", d3.forceCollide((d) => d.r + 4)).stop();
  for (let k = 0; k < 40; k++) nudge.tick();
  return new Map(nodes.map((d) => [d.id, { x: d.x, y: d.y }]));
}

// ---- the map -------------------------------------------------------------------------------

/**
 * The map element (persistent across repaints): loose groups of entries, each gathered around the
 * entry with the most ties in it, hairline ties, dashed ties between groups, no island shapes.
 * ctx: { gs, graph, laid (Set of ids with any connection), typeOn(id), sel, radius(id),
 *        revealed(id), select(id), minCo }
 */
export function archipelago(ctx) {
  const { gs, graph } = ctx;
  const A = (gs.arch ||= { local: new Map(), centres: new Map(), lastIsland: new Map(), nextIsland: 0, unfolded: new Set(), sig: null });
  if (!A.wrap) build(A);
  A.ctx = ctx;
  // What the map is: the cast (people and creatures), tied by stated relationships. Characters who
  // keep appearing together also pull toward each other, so the supporting cast settles near the
  // people they're with, but those ties are only drawn on request. Places and things join only with
  // "Places & things" on, and only through stated relationships: a bank everyone visits isn't a tie.
  // Re-layout only when this changes; type filters and selection never move anything.
  const cast = (id) => CAST.has(graph.ents.get(id).type);
  const mapped = (id) => ctx.laid.has(id) && (gs.things || cast(id));
  const links = graph.edges.filter((e) => mapped(e.a) && mapped(e.b)
    && (e.rels.length || e.old.length || (cast(e.a) && cast(e.b) && sharedEvents(e) > 0)));
  const ids = [...new Set(links.flatMap((e) => [e.a, e.b]))];
  if (!ids.length) {
    return h("div.empty", h("div.art", icon("link")), h("h3", "No ties between characters yet"),
      h("p", gs.things ? "Stated relationships show up here as the book reveals them."
        : "Stated relationships between characters show up here as the book reveals them. Turn on “Places & things” to include those too."));
  }
  const sig = `${ids.length}:${links.map((e) => `${e.key}=${weightOf(e)}`).join(",")}`;
  if (sig !== A.sig) {
    A.sig = sig;
    A.links = links;
    A.adj = new Map(ids.map((id) => [id, []]));
    for (const e of links) { A.adj.get(e.a).push(e.b); A.adj.get(e.b).push(e.a); }
    A.isl = findIslands(A, graph, ids, links);
    const target = layoutIslands(A, graph, A.isl, links, ctx.radius);
    A.home = target;
    const from = new Map();
    for (const id of target.keys()) {
      let p = A.shown?.get(id);
      if (!p) { const nb = A.adj.get(id).map((x) => A.shown?.get(x)).filter(Boolean); p = nb.length ? { x: nb.reduce((t, q) => t + q.x, 0) / nb.length, y: nb.reduce((t, q) => t + q.y, 0) / nb.length } : target.get(id); }
      from.set(id, p);
    }
    A.sim?.stop(); A.sim = null;
    A.shown = from;
    const first = !A.fitted;
    render(A);
    animate(A, from, target);
    if (first) { A.fitted = true; requestAnimationFrame(() => fit(A, false)); }
  } else render(A);
  return A.wrap;
}

function build(A) {
  A.svg = d3.create("svg").attr("class", "g-svg g-arch").attr("role", "group").attr("aria-label", "Connections map");
  A.root = A.svg.append("g");
  A.layers = Object.fromEntries(["hulls", "routes", "edges", "nodes", "labels", "names"].map((k) => [k, A.root.append("g")]));
  A.tip = d3.create("div").attr("class", "g-island-tip").attr("hidden", true);
  A.wrap = d3.create("div").attr("class", "g-arch-wrap").node();
  A.wrap.append(A.svg.node(), A.tip.node());
  A.t = d3.zoomIdentity;
  A.level = -1;
  A.zoom = d3.zoom().scaleExtent([0.05, 5])
    .filter((ev) => (ev.type === "wheel" ? ev.ctrlKey || ev.metaKey : !ev.button))   // plain scrolling scrolls the page
    .on("zoom", (ev) => { A.t = ev.transform; A.root.attr("transform", A.t); if (levelOf(A.t.k) !== A.level) render(A); })
    .on("end", () => render(A));
  A.svg.call(A.zoom).on("dblclick.zoom", null);
  A.svg.on("click", (ev) => { if (ev.defaultPrevented) return; if (!ev.target.closest(".g-node") && !ev.target.closest(".g-isl-name")) A.ctx.select(null); });
  // The stage changes size (a phone turned, a window resized, the side panel folding under): fit again.
  let lastW = 0, lastH = 0;
  A.ro = new ResizeObserver((entries) => {
    const { width, height } = entries[0].contentRect;
    if (!A.shown || !width || !height) return;
    if (Math.abs(width - lastW) < 40 && Math.abs(height - lastH) < 40) return;
    lastW = width; lastH = height;
    clearTimeout(A.refit);
    A.refit = setTimeout(() => fit(A, false), 120);
  });
  A.ro.observe(A.svg.node());
}

const levelOf = (k) => (k < 0.42 ? 0 : k < 1.05 ? 1 : 2);

/** Zoom to fit everything (or the given ids). */
export function fit(A, animated = true, only = null) {
  const pts = [...(only || [...A.shown.keys()])].map((id) => A.shown.get(id)).filter(Boolean);
  const node = A.svg.node();
  const W = node.clientWidth || 800, H = node.clientHeight || 560;
  if (!pts.length) return;
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y), pad = 120;   // room for the names beside the outer dots
  const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
  const k = Math.min(only ? 1.6 : 1.4, W / (x1 - x0), H / (y1 - y0));
  const t = d3.zoomIdentity.translate(W / 2 - k * (x0 + x1) / 2, H / 2 - k * (y0 + y1) / 2).scale(k);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  (animated && !reduce ? A.svg.transition().duration(600) : A.svg).call(A.zoom.transform, t);
}
export const zoomBy = (A, f) => A.svg.transition().duration(250).call(A.zoom.scaleBy, f);

function animate(A, from, target) {
  cancelAnimationFrame(A.raf);
  const t0 = performance.now(), dur = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 700;
  const step = (now) => {
    const e = dur ? d3.easeCubicInOut(Math.min(1, (now - t0) / dur)) : 1;
    const cur = new Map();
    for (const [id, p] of target) { const f = from.get(id) || p; cur.set(id, { x: f.x + (p.x - f.x) * e, y: f.y + (p.y - f.y) * e }); }
    A.shown = cur;
    place(A);
    if (e < 1) A.raf = requestAnimationFrame(step); else render(A);
  };
  A.raf = requestAnimationFrame(step);
}

/** Decide what's drawn at this zoom and draw it. */
function render(A) {
  const { graph, gs, sel, typeOn, radius } = A.ctx;
  const k = A.t.k, lvl = levelOf(k);
  A.level = lvl;
  const isl = A.isl;
  const near = sel ? new Set([sel, ...(A.adj.get(sel) || [])]) : null;
  // Nothing folds: every entry the filters allow is drawn.
  const nodes = [...isl.of.keys()].filter(typeOn);
  const drawn = new Set(nodes);
  for (const i of isl.list) i.folded = 0;
  // Lines: stated ties always (dashed when they cross from one group to another), earlier ties with
  // history on, "appear together" only when asked and only within a group or touching the selection.
  const touches = (e) => sel && (e.a === sel || e.b === sel);
  const together = (e) => gs.together && !e.rels.length && e.weight >= A.ctx.minCo;
  const edges = A.links.filter((e) => drawn.has(e.a) && drawn.has(e.b) && (
    e.rels.length || (gs.history && e.old.length) || (together(e) && (touches(e) || isl.of.get(e.a) === isl.of.get(e.b)))));
  A.drawn = drawn;

  A.layers.hulls.selectAll("path").data([]).join("path");
  A.layers.routes.selectAll("path").data([]).join("path");
  A.layers.edges.selectAll("line").data(edges, (d) => d.key).join("line")
    .attr("class", (d) => `g-edge ${d.rels.length ? "rel" : d.old.length && gs.history ? "old" : "co"}${isl.of.get(d.a) !== isl.of.get(d.b) ? " cross" : ""}${sel ? (touches(d) ? " lit" : " dim") : ""}`);
  const ns = A.layers.nodes.selectAll("g.g-node").data(nodes, (d) => d).join((en) => {
    const g = en.append("g").attr("tabindex", 0).attr("role", "button");
    g.on("click", (ev, id) => { ev.stopPropagation(); A.ctx.select(id === A.ctx.sel ? null : id); })
      .on("keydown", (ev, id) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); A.ctx.select(id === A.ctx.sel ? null : id); } })
      .call(drag(A));
    return g;
  });
  ns.each(function (id) {
    const e = graph.ents.get(id), r = radius(id), g = this;
    const isNew = A.ctx.revealed(id)?.isNew;
    const want = `${e.type}|${r.toFixed(1)}|${isNew}`;
    g.setAttribute("class", `g-node g-dot t-${e.type}${id === sel ? " sel" : ""}${near && !near.has(id) ? " dim" : ""}${isNew ? " new" : ""}`);
    g.setAttribute("aria-label", `${e.name}, ${TYPE_LABEL[e.type]}${isNew ? ", new" : ""}`);
    g.setAttribute("aria-pressed", String(id === sel));
    if (g.dataset.look === want) return;
    g.dataset.look = want;
    g.replaceChildren();
    if (isNew) g.append(s("circle", { class: "g-halo", r: r + 9 }));
    g.append(s("circle", { class: "g-ring", r: r + 6 }));
    g.append(s("circle", { class: "g-glow", r: r * 2.2 }));
    g.append(s("circle", { class: "g-shape", r }));
  });

  place(A);
  const node = A.svg.node(), W = node.clientWidth, H = node.clientHeight;
  // Entry names: beside each dot, most prominent first (selection and its neighbours before anything), where they fit.
  const labels = [], boxes = [];
  A.side = new Map();   // which side of its dot a name sits: right by default, left or below when the right is taken
  const order = nodes.slice().sort((a, b) => (near ? near.has(b) - near.has(a) : 0) || graph.prom.get(b) - graph.prom.get(a));
  const fs = lvl === 0 ? 11 : 12;
  // Dots themselves are kept clear of names.
  for (const id of nodes) { const p = A.shown.get(id), sx = A.t.applyX(p.x), sy = A.t.applyY(p.y), r = radius(id) * k; boxes.push([sx - r, sy - r, sx + r, sy + r]); }
  for (const id of order) {
    const p = A.shown.get(id), sx = A.t.applyX(p.x), sy = A.t.applyY(p.y), r = radius(id) * k;
    const name = graph.ents.get(id).name, w = name.length * fs * 0.56;
    if (lvl === 0 && graph.prom.get(id) < 2 && !(near && near.has(id))) continue;   // from far away, only the prominent entries are named
    const tries = [["right", [sx + r + 4, sy - fs * 0.7, sx + r + 6 + w, sy + fs * 0.7]],
      ["left", [sx - r - 6 - w, sy - fs * 0.7, sx - r - 4, sy + fs * 0.7]],
      ["below", [sx - w / 2, sy + r + 3, sx + w / 2, sy + r + 3 + fs * 1.4]]];
    for (const [side, b] of tries) {
      if (b[0] < 0 || b[2] > W || b[1] < 0 || b[3] > H) continue;   // a name cut by the edge moves to another side
      if (boxes.some((o) => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1])) continue;
      boxes.push(b); labels.push(id); A.side.set(id, side);
      break;
    }
  }
  A.layers.labels.selectAll("text").data(labels, (d) => d).join("text")
    .attr("class", (id) => `g-label on${near && !near.has(id) ? " dim" : ""}${graph.prom.get(id) >= 4 ? " bright" : ""}`)
    .attr("text-anchor", (id) => ({ right: "start", left: "end", below: "middle" })[A.side.get(id)])
    .style("font-size", `${fs / k}px`).style("stroke-width", `${3 / k}px`).text((id) => graph.ents.get(id).name);
  // Group names: "Around X" beneath each group of two or more, where they don't collide.
  const named = [], taken = boxes.slice();
  for (const d of isl.list.slice().sort((a, b) => b.members.length - a.members.length)) {
    if (d.members.length < 2 || !Number.isFinite(d.cx)) continue;
    d.fs = 13;
    const sx = A.t.applyX(d.cx), sy = A.t.applyY(d.by) + 22;
    const w = d.label.length * 13 * 0.56;
    const box = [sx - w / 2, sy - 13, sx + w / 2, sy + 4];
    if (taken.some((o) => box[0] < o[2] && box[2] > o[0] && box[1] < o[3] && box[3] > o[1])) continue;
    taken.push(box); named.push(d);
  }
  const showTip = (d) => { clearTimeout(A.tipTimer); A.tip.attr("hidden", null).text(`${d.label}, ${d.members.length} ${d.members.length === 1 ? "entry" : "entries"}`); };
  const nm = A.layers.names.selectAll("g.g-isl-name").data(named, (d) => d.id).join((en) => {
    const g = en.append("g").attr("class", "g-isl-name");
    g.append("text").attr("class", "g-isl-label").attr("text-anchor", "middle").attr("tabindex", 0).attr("role", "button")
      .on("mouseenter focus", (ev, d) => showTip(d))
      .on("mouseleave blur", () => { clearTimeout(A.tipTimer); A.tip.attr("hidden", true); })
      .on("click", (ev, d) => { ev.stopPropagation(); fit(A, true, d.members); })
      .on("keydown", (ev, d) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); fit(A, true, d.members); } });
    g.append("text").attr("class", "g-isl-sub").attr("display", "none");
    return g;
  });
  nm.select(".g-isl-label").attr("aria-label", (d) => `${d.label}, ${d.members.length} entries, press to zoom in`)
    .attr("font-size", (d) => d.fs / k).attr("stroke-width", 3 / k).text((d) => d.label);
  place(A);
}

/** Move everything to A.shown (cheap: runs every animation frame and every drag tick). */
function place(A) {
  const P = A.shown, k = A.t.k, { radius } = A.ctx;
  const at = (id) => P.get(id) || { x: 0, y: 0 };
  A.layers.nodes.selectAll("g.g-node").attr("transform", (id) => { const p = at(id); return `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`; });
  A.layers.edges.selectAll("line").attr("x1", (d) => at(d.a).x).attr("y1", (d) => at(d.a).y).attr("x2", (d) => at(d.b).x).attr("y2", (d) => at(d.b).y);
  for (const i of A.isl.list) {
    const ms = i.members.filter((id) => A.drawn?.has(id));
    const use = ms.length ? ms : i.members;
    i.cx = use.reduce((t, id) => t + at(id).x, 0) / use.length;
    i.cy = use.reduce((t, id) => t + at(id).y, 0) / use.length;
    i.by = Math.max(...use.map((id) => at(id).y + (radius(id) || 0)));   // the group's lowest entry, where its name goes
  }
  A.layers.names.selectAll("g.g-isl-name").select(".g-isl-label").attr("x", (d) => d.cx).attr("y", (d) => d.by + 22 / k);
  // Labels of entries the view just dropped (moving back in the book) are still bound until the next join.
  A.layers.labels.selectAll("text")
    .attr("x", (id) => { const side = A.side?.get(id) || "right"; return at(id).x + (side === "right" ? (radius(id) || 0) + 5 / k : side === "left" ? -((radius(id) || 0) + 5 / k) : 0); })
    .attr("y", (id) => at(id).y + ((A.side?.get(id) || "right") === "below" ? (radius(id) || 0) + 14 / k : 4 / k));
}

/** Dragging runs a live simulation: links act as springs (strong within an island, weak across),
 *  so neighbours follow and the island reshapes. A dropped entry stays on its island where it was put. */
function drag(A) {
  const simNode = (id) => liveSim(A).nodes().find((x) => x.id === id);
  return d3.drag()
    .subject((ev, id) => { const p = A.shown.get(id); return { x: p.x, y: p.y }; })
    .on("start", (ev, id) => { cancelAnimationFrame(A.raf); const n = simNode(id); n.fx = n.x; n.fy = n.y; A.sim.alphaTarget(0.25).restart(); })
    .on("drag", (ev, id) => { const n = simNode(id); n.fx = ev.x; n.fy = ev.y; })
    .on("end", (ev, id) => {
      const n = simNode(id);
      n.hx = n.fx; n.hy = n.fy; n.fx = n.fy = null;
      A.sim.alphaTarget(0);
      const c = A.centres.get(A.isl.of.get(id).id);
      A.local.set(id, { x: n.hx - c.x, y: n.hy - c.y });   // remembered for the next position
    });
}

function liveSim(A) {
  if (A.sim) return A.sim;
  cancelAnimationFrame(A.raf);
  const nodes = [...A.shown.keys()].map((id) => { const p = A.shown.get(id), h = A.home.get(id) || p; return { id, x: p.x, y: p.y, hx: h.x, hy: h.y, r: A.ctx.radius(id) }; });
  const at = new Map(nodes.map((d) => [d.id, d]));
  const same = (e) => A.isl.of.get(e.a) === A.isl.of.get(e.b);
  A.sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(A.links.map((e) => ({ source: e.a, target: e.b, e, rest: Math.hypot(at.get(e.a).x - at.get(e.b).x, at.get(e.a).y - at.get(e.b).y) })))
      .id((d) => d.id).distance((d) => Math.min(d.rest, same(d.e) ? 220 : 600)).strength((d) => (same(d.e) ? 0.35 : 0.02)))
    .force("hx", d3.forceX((d) => d.hx).strength(0.03)).force("hy", d3.forceY((d) => d.hy).strength(0.03))
    .force("collide", d3.forceCollide((d) => d.r + 4))
    .alphaDecay(0.04)
    .on("tick", () => { A.shown = new Map(nodes.map((d) => [d.id, { x: d.x, y: d.y }])); place(A); })
    .stop();
  return A.sim;
}

export { LEVELS };
