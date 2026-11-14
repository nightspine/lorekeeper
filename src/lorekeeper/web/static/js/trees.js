// Family trees: the gated kinship ties drawn as generations (see kinship.js for how they are read and laid out).
//
// One SVG, every family stacked with a heading each, in a d3 zoom/pan container like the map. Only text
// from the view reaches the page, and only through textContent; the view holds nothing past the position
// (SPEC.md §7.4), so neither does this drawing. Light budget: flat dots and hairlines, no glow.
import { h } from "./dom.js";
import { loadLibs } from "./islands.js";

const NS = "http://www.w3.org/2000/svg";
const COL = 150;      // one node width, in map units
const ROW = 128;      // one generation
const HEAD = 62;      // room for a family's heading
const FOOT = 70;      // room for names under the last generation
const GAP = 64;       // between families
const R = 9;          // dot radius
const WRAP = 15;      // characters per line of a name

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v === true ? "" : String(v));
  for (const c of children.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

/** A name over at most two lines. */
function lines(name) {
  const words = name.split(/\s+/), out = [""];
  for (const w of words) {
    const cur = out[out.length - 1];
    if (cur && (cur + " " + w).length > WRAP && out.length < 2) out.push(w);
    else out[out.length - 1] = cur ? `${cur} ${w}` : w;
  }
  return out.map((l) => (l.length > WRAP + 3 ? `${l.slice(0, WRAP + 2)}…` : l));
}

/** Where each family sits in the one big drawing. */
export function arrange(fams) {
  let y = 0;
  return fams.map((f) => {
    const box = { f, x: 0, y, w: f.width * COL, h: HEAD + (f.layers - 1) * ROW + FOOT };
    y += box.h + GAP;
    return box;
  });
}

/**
 * The trees element. ctx: { gs (graph state: gs.tt remembers the zoom), fams, ents (Map id → entity), sel, select(id),
 * revealed(id) }. Returns a div to put in the stage; call `fitTo(wrap, id?)` to frame everything or one family.
 */
export function treesStage(ctx) {
  const { fams, ents, sel, gs } = ctx;
  const boxes = arrange(fams);
  const svg = s("svg", { class: "g-svg g-trees", role: "group", "aria-label": "Family trees" });
  const root = s("g");
  svg.append(root);
  const wrap = h("div.g-arch-wrap.g-trees-wrap", svg);
  const tied = sel ? new Set(fams.flatMap((f) => (f.members.includes(sel) ? f.links.filter((l) => l.a === sel || l.b === sel).flatMap((l) => [l.a, l.b]) : []))) : null;

  for (const { f, y } of boxes) {
    const px = (id) => f.x.get(id) * COL;
    const py = (id) => y + HEAD + f.layer.get(id) * ROW;
    const unit = new Map();
    for (const u of f.units) for (const m of u.members) unit.set(m, u);
    const mid = (u) => u.members.reduce((t, m) => t + px(m), 0) / u.members.length;
    const g = s("g", { class: "t-family", role: "group", "aria-label": `${f.name}: ${f.members.length} people` });
    g.append(s("text", { class: "t-name", x: Math.min(f.width * COL / 2, COL * 1.5), y: y + 22, "text-anchor": "middle" }, f.name), s("text", { class: "t-count", x: Math.min(f.width * COL / 2, COL * 1.5), y: y + 42, "text-anchor": "middle" }, `${f.members.length} people`));
    const linkLayer = s("g", { class: "t-links" }), nodeLayer = s("g", { class: "t-nodes" });
    g.append(linkLayer, nodeLayer);
    for (const l of f.links) {
      const a = l.a, b = l.b;
      const lit = sel && (a === sel || b === sel);
      let d, hit;
      const cls = `t-link ${l.kind}${l.dashed ? " dashed" : ""}${l.old ? " old" : ""}${lit ? " lit" : ""}`;
      if (l.kind === "spouse") {
        const [p, q] = px(a) < px(b) ? [a, b] : [b, a];
        d = `M${px(p) + R + 3},${py(p)} H${px(q) - R - 3}`;
      } else if (l.kind === "child" && l.tree) {
        const u = unit.get(a);
        const x0 = u.members.length > 1 ? mid(u) : px(a);
        const y0 = u.members.length > 1 ? py(a) : py(a) + R + 3;
        const by = py(a) + (py(b) - py(a)) * 0.5;
        d = `M${x0},${y0} V${by} H${px(b)} V${py(b) - R - 4}`;
      } else if (l.kind === "sibling") {
        const su = unit.get(a), sv = unit.get(b);
        if (su && sv && su.parent && su.parent === sv.parent) continue;   // brothers and sisters under one parent say so already
        const top = Math.min(py(a), py(b)) - R - 22;
        d = `M${px(a)},${py(a) - R - 4} V${top} H${px(b)} V${py(b) - R - 4}`;
      } else {
        const same = py(a) === py(b);
        const [x1, y1, x2, y2] = same ? [px(a), py(a) + R + 4, px(b), py(b) + R + 4] : [px(a), py(a) + R + 4, px(b), py(b) - R - 4];
        const bow = same ? Math.min(46, 14 + Math.abs(x2 - x1) * 0.12) : 0;
        d = same ? `M${x1},${y1} Q${(x1 + x2) / 2},${y1 + bow * 2} ${x2},${y2}` : `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`;
      }
      const grp = s("g", { class: cls });
      grp.append(s("path", { class: "t-line", d }), s("path", { class: "t-hit", d }, s("title", {}, l.title)));
      grp.lastChild.addEventListener("click", (ev) => { ev.stopPropagation(); ctx.select(a); });
      linkLayer.append(grp);
    }
    for (const id of f.members) {
      const e = ents.get(id);
      const isSel = id === sel;
      const ties = f.ties.get(id) || [];
      const node = s("g", { class: `g-node g-dot t-node t-${e.type}${isSel ? " sel" : ""}${tied && !tied.has(id) ? " dim" : ""}`, transform: `translate(${px(id)},${py(id)})`, "data-id": id,
        tabindex: 0, role: "button", "aria-pressed": String(isSel),
        "aria-label": `${e.name}${ties.length ? `. ${ties.join(". ")}` : ""}` });
      node.append(s("circle", { class: "g-ring", r: R + 6 }), s("circle", { class: "g-shape", r: R }));
      const label = s("text", { class: "t-label", "text-anchor": "middle", y: R + 18 });
      lines(e.name).forEach((ln, i) => label.append(s("tspan", { x: 0, dy: i ? 15 : 0 }, ln)));
      node.append(label);
      node.addEventListener("click", (ev) => { ev.stopPropagation(); ctx.select(isSel ? null : id); });
      node.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ctx.select(isSel ? null : id); } });
      nodeLayer.append(node);
    }
    root.append(g);
  }

  const total = { w: Math.max(1, ...boxes.map((b) => b.w)), h: boxes.length ? boxes[boxes.length - 1].y + boxes[boxes.length - 1].h : 1 };
  wrap._trees = { svg, root, boxes, total };
  svg.addEventListener("click", () => ctx.select(null));
  // d3 draws nothing itself; it only moves the root group (pan, pinch, ctrl-wheel), like the map.
  const t0 = gs.tt;
  if (t0 && !ctx.focusId) root.setAttribute("transform", `translate(${t0.x},${t0.y}) scale(${t0.k})`);
  const start = () => {
    if (!svg.isConnected) return;     // a stale frame must not write gs.tt from a detached stage
    const zoom = d3.zoom().scaleExtent([0.15, 4])
      .filter((ev) => (ev.type === "wheel" ? ev.ctrlKey || ev.metaKey : !ev.button))
      .on("zoom", (ev) => { root.setAttribute("transform", ev.transform); gs.tt = { x: ev.transform.x, y: ev.transform.y, k: ev.transform.k, user: performance.now() > (wrap._trees.autoUntil || 0) }; });
    wrap._trees.zoom = zoom;
    d3.select(svg).call(zoom).on("dblclick.zoom", null);
    if (ctx.focusId) fitTo(wrap, ctx.focusId, false);
    else if (t0) d3.select(svg).call(zoom.transform, d3.zoomIdentity.translate(t0.x, t0.y).scale(t0.k));
    else fitTo(wrap, null, false);
    let lw = 0;
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver((es) => {
        const w = es[0].contentRect.width;
        if (!w || Math.abs(w - lw) < 40) return;
        const first = !lw; lw = w;
        if (!first && !(gs.tt && gs.tt.user)) fitTo(wrap, null, false);
      }).observe(svg);
    }
  };
  requestAnimationFrame(start);
  return wrap;
}

/** Frame everything, or the family holding `id`. */
export function fitTo(wrap, id = null, animated = true) {
  const T = wrap._trees;
  if (!T?.zoom) return;
  const W = T.svg.clientWidth || 800, H = T.svg.clientHeight || 560;
  let x0 = 0, y0 = 0, w = T.total.w, hh = T.total.h;
  const frame = (b) => { x0 = b.x; y0 = b.y; w = b.w; hh = b.h; };
  if (id) { const b = T.boxes.find((bx) => bx.f.members.includes(id)); if (b) frame(b); }
  T.autoUntil = performance.now() + 800;
  const pad = 34, minK = W < 520 ? 0.7 : 0.55;
  const whole = x0 === 0 && y0 === 0 && w === T.total.w;
  let k = Math.min(1.15, (W - 2 * pad) / (w + COL), id || T.boxes.length === 1 ? (H - 2 * pad) / hh : Infinity);
  // Many families side by side would shrink the names past reading: start on the largest and let the reader pan.
  if (whole && k < minK && T.boxes.length > 1) { frame(T.boxes[0]); k = Math.min(1.15, Math.max(minK, (W - 2 * pad) / (w + COL))); }
  k = Math.max(0.15, k);
  const tx = w * k + 2 * pad > W ? pad - k * x0 : W / 2 - k * (x0 + w / 2);
  const ty = hh * k + 2 * pad <= H ? (H - hh * k) / 2 - k * y0 : pad - k * y0;
  const t = d3.zoomIdentity.translate(tx, ty).scale(k);
  const reduce = typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  (animated && !reduce ? d3.select(T.svg).transition().duration(500) : d3.select(T.svg)).call(T.zoom.transform, t);
}

export const zoomTrees = (wrap, f) => wrap._trees?.zoom && d3.select(wrap._trees.svg).transition().duration(220).call(wrap._trees.zoom.scaleBy, f);
export { loadLibs };
