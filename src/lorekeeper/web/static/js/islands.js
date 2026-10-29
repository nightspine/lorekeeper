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
const TITLE = /^(mr|mrs|miss|ms|mx|dr|sir|dame|lady|lord|master|madam|madame|monsieur|mademoiselle|professor|uncle|aunt|captain|king|queen|prince|princess|duke|duchess|count|countess|baron|baroness|father|mother|brother|sister|saint|st)\.?$/i;

// ---- groups and layout ---------------------------------------------------------------------

/** A family name a third or more of the island shares ("Dursley" for Mr. and Mrs. Dursley, Dudley
 *  Dursley and friends), or null. One-word names count only with a title ("Mr. Dursley"). */
function familyName(members) {
  const people = members.filter((e) => e.type === "person");
  const count = new Map();
  for (const e of people) {
    const surnames = new Set();
    for (const n of [e.name, ...e.aliases]) {
      const words = n.replace(/\([^)]*\)/g, " ").split(/\s+/).filter(Boolean);
      const titled = words.length > 1 && TITLE.test(words[0]);
      const rest = titled ? words.slice(1) : words;
      if ((rest.length > 1 || titled) && /^[A-Z]/.test(rest[rest.length - 1])) surnames.add(rest[rest.length - 1].replace(/['’]s$/, ""));
    }
    for (const w of surnames) count.set(w, (count.get(w) || 0) + 1);
  }
  const [best, n] = [...count.entries()].sort((a, b) => b[1] - a[1])[0] || [];
  return best && n >= 2 && n * 3 >= people.length ? best : null;
}

/** What a character is called for short: a one-word name they go by ("Filch", "Harry"), else the
 *  first word of their name that isn't a title ("Professor Quirrell" → Quirrell). */
function shortName(e) {
  const one = [e.name, ...e.aliases].find((n) => /^[A-Z][^\s]*$/.test(n) && !TITLE.test(n));
  if (one) return one;
  const words = e.name.replace(/\([^)]*\)/g, " ").split(/\s+/).filter(Boolean);
  return words.find((w) => !TITLE.test(w) && !/^the$/i.test(w)) || words[0];
}

/** Louvain groups as islands; ids (and so colours) follow the members from one position to the next. */
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
    const family = familyName(m.map((x) => graph.ents.get(x)));
    const isl = { id, members: m, color: `var(--isl-${id % ISLAND_COLORS})`, hub: Boolean(hub || family), top: top.name,
      label: family ? `The ${family} family` : hub ? hub.name : m.length > 1 ? `${shortName(top)}'s circle` : top.name };
    list.push(isl);
    for (const x of m) of.set(x, isl);
  }
  const dup = new Map();
  for (const i of list) dup.set(i.label, (dup.get(i.label) || 0) + 1);
  // Two islands with the same name: circles use their leader's full name, places add the leader.
  for (const i of list) if (dup.get(i.label) > 1) i.label = i.hub ? `${i.label} · ${shortName(graph.ents.get(i.members[0]))}` : `${i.top}'s circle`;
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
    i.r = 26 * Math.sqrt(i.members.length) + 18;
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
    .force("link", d3.forceLink(mlinks).id((d) => d.id).distance((d) => d.source.r + d.target.r + 110).strength((d) => Math.min(0.6, 0.02 + Math.log1p(d.w) / 25)))
    .force("charge", d3.forceManyBody().strength((d) => -6 * d.r))
    .force("collide", d3.forceCollide((d) => d.r + 80).strength(1).iterations(3))
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
 * The archipelago map element (persistent across repaints).
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
}

const levelOf = (k) => (k < 0.42 ? 0 : k < 1.05 ? 1 : 2);

/** Zoom to fit everything (or the given ids). */
export function fit(A, animated = true, only = null) {
  const pts = [...(only || [...A.shown.keys()])].map((id) => A.shown.get(id)).filter(Boolean);
  const node = A.svg.node();
  const W = node.clientWidth || 800, H = node.clientHeight || 560;
  if (!pts.length) return;
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y), pad = 90;
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
  const minor = (id) => !A.links.some((e) => (e.a === id || e.b === id) && (e.rels.length || e.old.length)) && A.adj.get(id).length <= 2 && graph.prom.get(id) < 4;
  const nodes = [...isl.of.keys()].filter((id) => typeOn(id) && (lvl === 2 || !minor(id) || A.unfolded.has(isl.of.get(id).id) || (near && near.has(id))));
  const drawn = new Set(nodes);
  for (const i of isl.list) i.folded = i.members.filter((id) => typeOn(id) && minor(id) && !drawn.has(id)).length;
  // Lines: stated relations from the islands level; "appear together" close up within an island;
  // everything touching the selection at any level.
  const touches = (e) => sel && (e.a === sel || e.b === sel);
  // Lines: stated relationships (and earlier ones with history on); "appear together" only when asked.
  const together = (e) => gs.together && !e.rels.length && e.weight >= A.ctx.minCo;
  const edges = A.links.filter((e) => drawn.has(e.a) && drawn.has(e.b) && (
    (e.rels.length && (lvl >= 1 || touches(e)))
    || (gs.history && e.old.length && (lvl >= 1 || touches(e)))
    || (together(e) && (touches(e) || (lvl === 2 && isl.of.get(e.a) === isl.of.get(e.b))))));
  let routes = [];
  if (lvl === 0) {
    const agg = new Map();
    for (const e of A.links) {
      const a = isl.of.get(e.a), b = isl.of.get(e.b);
      if (a === b || !e.rels.length) continue;   // routes are stated ties between islands
      const kk = a.id < b.id ? `${a.id}|${b.id}` : `${b.id}|${a.id}`;
      const r = agg.get(kk) || { kk, a: a.id < b.id ? a : b, b: a.id < b.id ? b : a, n: 0 };
      r.n += weightOf(e);
      agg.set(kk, r);
    }
    routes = [...agg.values()].filter((r) => r.n >= 2);
  }
  A.drawn = drawn;

  const showTip = (d) => { clearTimeout(A.tipTimer); A.tip.attr("hidden", null).text(`${d.label} · ${d.members.length} ${d.members.length === 1 ? "entry" : "entries"}`); };
  A.layers.hulls.selectAll("path").data(isl.list, (d) => d.id).join("path").attr("class", "g-isl")
    .style("fill", (d) => `color-mix(in srgb, ${d.color} 10%, transparent)`)
    .style("stroke", (d) => `color-mix(in srgb, ${d.color} 35%, transparent)`)
    .attr("tabindex", 0).attr("role", "button")
    .attr("aria-label", (d) => `${d.label}, ${d.members.length} ${d.members.length === 1 ? "entry" : "entries"}, press to zoom in`)
    .on("mouseenter focus", (ev, d) => showTip(d))
    .on("mouseleave blur", () => { clearTimeout(A.tipTimer); A.tip.attr("hidden", true); })
    .on("click", (ev, d) => {
      ev.stopPropagation();
      showTip(d);
      if (ev.pointerType === "touch") { clearTimeout(A.tipTimer); A.tipTimer = setTimeout(() => A.tip.attr("hidden", true), 2000); }
      fit(A, true, d.members);
    })
    .on("keydown", (ev, d) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); fit(A, true, d.members); } });
  A.layers.routes.selectAll("path").data(routes, (d) => d.kk).join("path").attr("class", "g-route").attr("stroke-width", (d) => 1 + Math.log2(d.n) * 1.3);
  A.layers.edges.selectAll("line").data(edges, (d) => d.key).join("line")
    .attr("class", (d) => `g-edge ${d.rels.length ? "rel" : d.old.length && gs.history ? "old" : "co"}${sel ? (touches(d) ? " lit" : " dim") : ""}`);
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
    const want = `${e.type}|${r.toFixed(1)}|${isNew}|${lvl > 0 && r >= 13}`;
    g.setAttribute("class", `g-node t-${e.type}${id === sel ? " sel" : ""}${near && !near.has(id) ? " dim" : ""}${isNew ? " new" : ""}`);
    g.setAttribute("aria-label", `${e.name}, ${TYPE_LABEL[e.type]}${isNew ? ", new" : ""}`);
    g.setAttribute("aria-pressed", String(id === sel));
    if (g.dataset.look === want) return;
    g.dataset.look = want;
    g.replaceChildren();
    if (isNew) g.append(s("circle", { class: "g-halo", r: r + 10 }));
    g.append(s("circle", { class: "g-ring", r: r + 6 }));
    const sh = shape(e.type, r); sh.setAttribute("class", "g-shape"); g.append(sh);
    if (lvl > 0 && r >= 13) {
      const ic = icon(e.type), sz = r * 1.05;
      for (const [kk, v] of Object.entries({ x: -sz / 2, y: -sz / 2, width: sz, height: sz })) ic.setAttribute(kk, v);
      ic.setAttribute("class", "svg-icon g-icon");
      Object.assign(ic.style, { width: `${sz}px`, height: `${sz}px` });
      g.append(ic);
    }
  });

  place(A);   // hulls must exist before names are placed against them
  const node = A.svg.node(), W = node.clientWidth, H = node.clientHeight;
  // Island names: sized to the island as it appears, left off islands too small to carry one, never
  // overlapping (bigger islands first). Hovering an island always names it.
  const named = [], taken = [];
  for (const d of isl.list.slice().sort((a, b) => b.members.length - a.members.length)) {
    if (d.members.length < 3 || !d.hull) continue;
    const xs = d.hull.map((p) => p[0]), ys = d.hull.map((p) => p[1]);
    const sw = (Math.max(...xs) - Math.min(...xs)) * k, sh = (Math.max(...ys) - Math.min(...ys)) * k;
    const fs = lvl === 0 ? Math.min(18, (sw / Math.max(6, d.label.length)) * 2.1) : 16;
    if (fs < 10) continue;
    d.fs = fs;
    d.sub = lvl === 0 ? sw > 150 && sh > 70 : d.folded > 0;
    const sx = A.t.applyX(d.cx), sy = lvl === 0 ? A.t.applyY(d.cy) : A.t.applyY(Math.min(...ys)) - 18;
    const w = Math.max(d.label.length * fs * 0.5, d.sub ? 110 : 0), hh = fs * 1.25 + (d.sub ? 15 : 0);
    const box = [sx - w / 2, sy - fs, sx + w / 2, sy - fs + hh];
    if (taken.some((o) => box[0] < o[2] && box[2] > o[0] && box[1] < o[3] && box[3] > o[1])) continue;
    taken.push(box); named.push(d);
  }
  const nm = A.layers.names.selectAll("g.g-isl-name").data(named, (d) => d.id).join((en) => {
    const g = en.append("g").attr("class", "g-isl-name");
    g.append("text").attr("class", "g-isl-label").attr("text-anchor", "middle")
      .on("click", (ev, d) => { ev.stopPropagation(); fit(A, true, d.members); });
    g.append("text").attr("class", "g-isl-sub").attr("text-anchor", "middle")
      .on("click", (ev, d) => { ev.stopPropagation(); if (A.unfolded.has(d.id)) A.unfolded.delete(d.id); else A.unfolded.add(d.id); render(A); });
    return g;
  });
  nm.select(".g-isl-label").style("fill", (d) => d.color).attr("font-size", (d) => d.fs / k).attr("stroke-width", 4 / k).text((d) => d.label);
  nm.select(".g-isl-sub").attr("font-size", 12 / k).attr("stroke-width", 3 / k).attr("display", (d) => (d.sub ? null : "none"))
    .classed("unfold", (d) => lvl === 1 && (d.folded > 0 || A.unfolded.has(d.id)))
    .text((d) => (lvl === 0 ? `${d.members.length} entries` : A.unfolded.has(d.id) ? "Fold minor entries" : `+${d.folded} minor`));
  // Entry names: most prominent first (selection and its neighbours before anything), where they fit.
  const labels = [];
  if (lvl >= 1) {
    const boxes = [];
    const order = nodes.slice().sort((a, b) => (near ? near.has(b) - near.has(a) : 0) || graph.prom.get(b) - graph.prom.get(a));
    for (const id of order) {
      const p = A.shown.get(id), sx = A.t.applyX(p.x), sy = A.t.applyY(p.y), r = radius(id) * k;
      const name = graph.ents.get(id).name;
      const b = [sx - name.length * 3.4, sy + r + 2, sx + name.length * 3.4, sy + r + 17];
      if (b[2] < 0 || b[0] > W || b[3] < 0 || b[1] > H) continue;
      if (boxes.some((o) => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1])) continue;
      boxes.push(b); labels.push(id);
    }
  }
  A.layers.labels.selectAll("text").data(labels, (d) => d).join("text")
    .attr("class", (id) => `g-label on${near && !near.has(id) ? " dim" : ""}`).attr("text-anchor", "middle")
    .style("font-size", `${12 / k}px`).style("stroke-width", `${4 / k}px`).text((id) => graph.ents.get(id).name);
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
    const pts = [];
    for (const id of use) { const p = at(id), r = radius(id) + 22; for (let a = 0; a < 8; a++) pts.push([p.x + r * Math.cos(a * Math.PI / 4), p.y + r * Math.sin(a * Math.PI / 4)]); }
    i.hull = d3.polygonHull(pts);
    i.cx = use.reduce((t, id) => t + at(id).x, 0) / use.length;
    i.cy = use.reduce((t, id) => t + at(id).y, 0) / use.length;
  }
  const line = d3.line().curve(d3.curveBasisClosed);
  A.layers.hulls.selectAll("path").attr("d", (d) => (d.hull ? line(d.hull) : null));
  A.layers.routes.selectAll("path").attr("d", (d) => `M${d.a.cx},${d.a.cy}L${d.b.cx},${d.b.cy}`);
  A.layers.names.selectAll("g.g-isl-name").each(function (d) {
    const g = d3.select(this), top = Math.min(...d.hull.map((p) => p[1]));
    if (A.level === 0) { g.select(".g-isl-label").attr("x", d.cx).attr("y", d.cy); g.select(".g-isl-sub").attr("x", d.cx).attr("y", d.cy + ((d.fs || 15) * 0.7 + 10) / k); }
    else { g.select(".g-isl-label").attr("x", d.cx).attr("y", top - 20 / k); g.select(".g-isl-sub").attr("x", d.cx).attr("y", top - 6 / k); }
  });
  // Labels of entries the view just dropped (moving back in the book) are still bound until the next join.
  A.layers.labels.selectAll("text").attr("x", (id) => at(id).x).attr("y", (id) => at(id).y + (radius(id) || 0) + 14 / k);
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
