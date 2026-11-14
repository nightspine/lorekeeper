// Kinship from relation labels, and family trees from a gated view.
//
// Dependency-free ES module, reusable by reader apps. `src/lorekeeper/kinship.py` holds the same
// tables; the two must agree label for label. Index relations carry free-text labels ("daughter of",
// "half-sister of"), so kinship is read from the label, with `kind: "family"` as a second signal.
// Uncertain labels are not kinship: a tie we cannot read is not drawn as one.
//
// The input of `families` is a gated view, so nothing past the reader's position exists in it
// (SPEC.md §7.4). Ties nobody stated (a cousin link) are derived in layout only, never written.

// ---- vocabulary -------------------------------------------------------------------------------

/** kin word → [role, reverse role] */
export const KIN = {
  mother: ["parent", "child"], father: ["parent", "child"], parent: ["parent", "child"],
  mum: ["parent", "child"], mom: ["parent", "child"], dad: ["parent", "child"], papa: ["parent", "child"], mama: ["parent", "child"],
  son: ["child", "parent"], daughter: ["child", "parent"], child: ["child", "parent"],
  husband: ["spouse", "spouse"], wife: ["spouse", "spouse"], spouse: ["spouse", "spouse"], consort: ["spouse", "spouse"],
  widow: ["spouse", "spouse"], widower: ["spouse", "spouse"],
  brother: ["sibling", "sibling"], sister: ["sibling", "sibling"], sibling: ["sibling", "sibling"], twin: ["sibling", "sibling"],
  grandmother: ["grandparent", "grandchild"], grandfather: ["grandparent", "grandchild"], grandparent: ["grandparent", "grandchild"],
  grandma: ["grandparent", "grandchild"], grandpa: ["grandparent", "grandchild"], granny: ["grandparent", "grandchild"],
  grandson: ["grandchild", "grandparent"], granddaughter: ["grandchild", "grandparent"], grandchild: ["grandchild", "grandparent"],
  aunt: ["pibling", "nibling"], uncle: ["pibling", "nibling"],
  niece: ["nibling", "pibling"], nephew: ["nibling", "pibling"],
  cousin: ["cousin", "cousin"],
  guardian: ["guardian", "ward"], godfather: ["guardian", "ward"], godmother: ["guardian", "ward"],
  ward: ["ward", "guardian"], godson: ["ward", "guardian"], goddaughter: ["ward", "guardian"],
};
/** Spelling variants and short forms, written one way by `normalize`. */
export const CANON = { mum: "mother", mom: "mother", mama: "mother", dad: "father", papa: "father", grandma: "grandmother", granny: "grandmother", grandpa: "grandfather" };
export const MODIFIERS = ["younger", "older", "elder", "eldest", "youngest", "twin", "half", "step", "adoptive", "adopted", "foster", "late", "ex", "former",
  "blood", "first", "second", "great", "identical"];
const LEAD = new Set(["is", "the", "a", "an", "her", "his", "their"]);
const MOD = new Set(MODIFIERS);
const KIN_KEYS = Object.keys(KIN);

/** Split a joined token ("stepson", "greatgrandmother") into modifiers and a kin word, or null. */
function joined(tok) {
  if (KIN[tok]) return { mods: [], word: tok };
  for (const m of MODIFIERS) {
    if (tok.length > m.length && tok.startsWith(m)) {
      const rest = joined(tok.slice(m.length));
      if (rest) return { mods: [m, ...rest.mods], word: rest.word };
    }
  }
  return null;
}

/**
 * Read a relation label as kinship: `{role, modifiers, word}` or null. `src` has `role` toward `dst`
 * ("daughter of": src is the child). `word` is the kin word as written, modifiers in order; an
 * "-in-law" suffix is the modifier "in-law", "widow"/"widower" carry "late".
 */
export function parse(label) {
  if (typeof label !== "string") return null;
  let t = label.toLowerCase().replace(/[‐-―]/g, "-").replace(/\s+/g, " ").trim();
  if (!t || /[,;:()/&]/.test(t)) return null;
  let toks = t.split(" ");
  while (toks.length && LEAD.has(toks[0])) toks.shift();
  if (!toks.length) return null;
  if (toks.join(" ") === "married to") return { role: "spouse", modifiers: [], word: "married to" };
  if (toks[toks.length - 1] === "of") toks.pop();
  if (!toks.length || toks.includes("of")) return null;      // anything after "of", a second "of"
  t = toks.join(" ");
  let inlaw = false;
  if (/[ -]in[ -]law$/.test(t)) { inlaw = true; t = t.replace(/[ -]in[ -]law$/, ""); }
  // Hyphens join modifiers to the word ("half-sister", "ex-husband"); any other suffix ("-in-arms") ends up a stray token.
  toks = t.split(/[ -]/).filter(Boolean);
  if (!toks.length) return null;
  const mods = [];
  let word = null;
  for (let i = 0; i < toks.length; i++) {
    const tok = toks[i], last = i === toks.length - 1;
    if (last) {
      const j = joined(tok);
      if (!j) return null;
      mods.push(...j.mods);
      word = j.word;
    } else if (MOD.has(tok)) mods.push(tok);
    else return null;                                       // "killed", "barracks", "possible", a second kin word...
  }
  if ((word === "widow" || word === "widower") && !mods.includes("late")) mods.unshift("late");
  if (inlaw) mods.push("in-law");
  return { role: KIN[word][0], modifiers: mods, word };
}

/** The reverse role: "parent" → "child". */
export const reverse = (role) => ({ parent: "child", child: "parent", spouse: "spouse", sibling: "sibling", grandparent: "grandchild", grandchild: "grandparent",
  pibling: "nibling", nibling: "pibling", cousin: "cousin", guardian: "ward", ward: "guardian", related: "related" })[role] || role;

/** Canonical label: lowercase `<modifiers> <word> of` ("is niece of" → "niece of", "Mom of" → "mother of"); null when not kinship. */
export function normalize(label) {
  const p = parse(label);
  if (!p) return null;
  if (p.word === "married to") return "married to";
  const implicit = p.word === "widow" || p.word === "widower" ? "late" : null;
  const inlaw = p.modifiers.includes("in-law");
  const mods = p.modifiers.filter((m) => m !== "in-law" && m !== implicit);
  const word = (CANON[p.word] || p.word) + (inlaw ? "-in-law" : "");
  return `${[...mods, word].join(" ")} of`;
}

/**
 * The kinship a relation expresses, or null: its label if it parses, else `{role: "related"}` when
 * its kind is "family" (an unparseable family tie is drawn as a plain, dashed "related" line).
 */
export function tie(rel) {
  const p = parse(rel?.label);
  if (p) return p;
  return rel?.kind === "family" ? { role: "related", modifiers: [], word: "related" } : null;
}

// ---- family names ------------------------------------------------------------------------------

const TITLES = new Set(["mr", "mrs", "ms", "miss", "mister", "madam", "madame", "dr", "doctor", "prof", "professor", "sir", "lady", "lord", "dame", "captain", "capt",
  "colonel", "major", "general", "lieutenant", "sergeant", "reverend", "rev", "father", "mother", "sister", "brother", "uncle", "aunt", "king", "queen", "prince",
  "princess", "count", "countess", "baron", "baroness", "duke", "duchess", "master", "mistress", "saint", "st"]);
const PARTICLES = new Set(["van", "von", "de", "del", "della", "di", "du", "la", "le", "der", "den", "ter", "ten", "da", "dos", "al", "el", "bin", "ibn"]);

/** The family name in a person's name ("Mr. Dursley" → "Dursley", "Abraham Van Helsing" → "Van Helsing"), or null. */
export function surname(name) {
  const clean = String(name || "").replace(/\([^)]*\)/g, " ").replace(/,.*$/, " ").replace(/['’]s\b/g, "");
  const toks = clean.split(/\s+/).map((x) => x.replace(/^[^\p{L}]+|[^\p{L}.]+$/gu, "")).filter(Boolean);
  let titled = false;
  while (toks.length && TITLES.has(toks[0].toLowerCase().replace(/\.$/, ""))) { toks.shift(); titled = true; }
  if (!toks.length || (toks.length === 1 && !titled)) return null;
  let i = toks.length - 1;
  const last = toks[i].replace(/\.$/, "");
  if (!/^\p{Lu}/u.test(last)) return null;
  while (i > 0 && PARTICLES.has(toks[i - 1].toLowerCase())) i--;
  return toks.slice(i).join(" ").replace(/\.$/, "");
}

// ---- families ----------------------------------------------------------------------------------

// How a tie constrains generations: level(src) - level(dst), and how firmly it says so.
const DY = { parent: [-1, 3], child: [1, 3], grandparent: [-2, 2], grandchild: [2, 2], pibling: [-1, 2], nibling: [1, 2], guardian: [-1, 2], ward: [1, 2],
  spouse: [0, 2], sibling: [0, 2], cousin: [0, 2], related: [0, 1] };
const KINDRED = new Set(["person", "creature"]);
const DASHED = new Set(["in-law", "step", "foster"]);

/** The family trees of a gated view. `opts.history` also draws relationHistory ties (marked old). Self-help books have none. */
export function families(view, opts = {}) {
  if (!view || view.bookKind === "self-help") return [];
  const order = new Map(view.entities.map((e, i) => [e.id, i]));
  const ents = new Map(view.entities.map((e) => [e.id, e]));
  const nameOf = (id) => ents.get(id)?.name || id;
  // 1. kinship ties between entities in the view, one link per pair and kind of tie.
  const links = [], seen = new Map();
  const take = (list, old) => {
    for (const r of list || []) {
      if (!ents.has(r.src) || !ents.has(r.dst) || r.src === r.dst) continue;
      if (!KINDRED.has(ents.get(r.src).type) || !KINDRED.has(ents.get(r.dst).type)) continue;   // objects, places, groups and terms never join a family
      const t = tie(r);
      if (!t) continue;
      const key = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);
      // a parent link is the same fact from either end: normalise to (parent, child)
      let a = r.src, b = r.dst, role = t.role;
      if (role === "child") { a = r.dst; b = r.src; role = "parent"; }
      if (role === "grandchild") { a = r.dst; b = r.src; role = "grandparent"; }
      if (role === "nibling") { a = r.dst; b = r.src; role = "pibling"; }
      if (role === "ward") { a = r.dst; b = r.src; role = "guardian"; }
      const k = `${role}|${role === "parent" || role === "grandparent" || role === "pibling" || role === "guardian" ? `${a}>${b}` : key(a, b)}`;
      const text = `${nameOf(r.src)}, ${r.label} ${nameOf(r.dst)}`;
      const prior = seen.get(k);
      if (prior) { if (!prior.texts.includes(text)) prior.texts.push(text); prior.old &&= old; continue; }
      const link = { a, b, role, modifiers: t.modifiers, old, texts: [text], dashed: role === "related" || t.modifiers.some((m) => DASHED.has(m)) };
      seen.set(k, link);
      links.push(link);
    }
  };
  take(view.relations, false);
  if (opts.history) take(view.relationHistory, true);
  if (!links.length) return [];

  // 2. families: connected components.
  const adj = new Map();
  const nbr = (id) => { if (!adj.has(id)) adj.set(id, []); return adj.get(id); };
  for (const l of links) { nbr(l.a).push(l); nbr(l.b).push(l); }
  const comps = [], visited = new Set();
  for (const id of [...adj.keys()].sort((x, y) => order.get(x) - order.get(y))) {
    if (visited.has(id)) continue;
    const members = [], stack = [id];
    visited.add(id);
    while (stack.length) {
      const x = stack.pop();
      members.push(x);
      for (const l of adj.get(x)) { const o = l.a === x ? l.b : l.a; if (!visited.has(o)) { visited.add(o); stack.push(o); } }
    }
    comps.push(members.sort((x, y) => order.get(x) - order.get(y)));
  }
  const out = comps.map((members) => layoutFamily(members, links.filter((l) => members.includes(l.a)), ents, order, nameOf));
  out.sort((p, q) => q.members.length - p.members.length || order.get(p.members[0]) - order.get(q.members[0]));
  return out;
}

/** A person's name for the family, shared by a third or more of the members; else after the member with the most ties. */
function familyName(members, links, ents, order) {
  const count = new Map(), firstSeen = new Map();
  for (const id of members) {
    const sn = surname(ents.get(id).name);
    if (!sn) continue;
    const k = sn.toLowerCase();
    count.set(k, (count.get(k) || 0) + 1);
    if (!firstSeen.has(k)) firstSeen.set(k, sn);
  }
  const best = [...count].sort((p, q) => q[1] - p[1] || [...firstSeen.keys()].indexOf(p[0]) - [...firstSeen.keys()].indexOf(q[0]))[0];
  if (best && best[1] >= 2 && best[1] * 3 >= members.length) return `The ${firstSeen.get(best[0])} family`;
  const deg = new Map(members.map((id) => [id, 0]));
  for (const l of links) { deg.set(l.a, deg.get(l.a) + 1); deg.set(l.b, deg.get(l.b) + 1); }
  const hub = [...members].sort((p, q) => deg.get(q) - deg.get(p) || order.get(p) - order.get(q))[0];
  return `The family of ${ents.get(hub).name}`;
}

/**
 * One family laid out: `layer` (generation, 0 at the top) and `x` (column, in node widths) per person, and the
 * links to draw: `{kind: "child"|"spouse"|"sibling"|"other", a, b, ...}` (child: a is the parent).
 */
function layoutFamily(members, links, ents, order, nameOf) {
  const mset = new Set(members);
  const deg = new Map(members.map((id) => [id, 0]));
  for (const l of links) { deg.set(l.a, deg.get(l.a) + (DY[l.role][1])); deg.set(l.b, deg.get(l.b) + (DY[l.role][1])); }
  // Generations: breadth-first from the best-tied person; a node takes the level most of its placed links propose.
  // d(l) = level(l.a) - level(l.b); for symmetric roles a/b order is irrelevant (0).
  const level = new Map();
  const start = [...members].sort((p, q) => deg.get(q) - deg.get(p) || order.get(p) - order.get(q))[0];
  level.set(start, 0);
  const byId = new Map(members.map((id) => [id, links.filter((l) => l.a === id || l.b === id)]));
  while (level.size < members.length) {
    let pick = null, pickW = -1;
    for (const id of members) {
      if (level.has(id)) continue;
      const votes = new Map();
      for (const l of byId.get(id)) {
        const o = l.a === id ? l.b : l.a;
        if (!level.has(o)) continue;
        const [d, w] = DY[l.role];
        // level(a) - level(b) = d (parent: a is above b, d = -1)
        const lv = l.a === id ? level.get(o) + d : level.get(o) - d;
        votes.set(lv, (votes.get(lv) || 0) + w);
      }
      if (!votes.size) continue;
      const [lv] = [...votes].sort((p, q) => q[1] - p[1] || Math.abs(p[0]) - Math.abs(q[0]))[0];
      const total = [...votes.values()].reduce((t, x) => t + x, 0);
      if (total > pickW || (total === pickW && order.get(id) < order.get(pick.id))) { pick = { id, lv }; pickW = total; }
    }
    level.set(pick.id, pick.lv);
  }
  const min = Math.min(...level.values());
  for (const id of members) level.set(id, level.get(id) - min);

  // Units: spouses on one layer travel together.
  const unitOf = new Map(members.map((id) => [id, id]));
  const find = (x) => { while (unitOf.get(x) !== x) { unitOf.set(x, unitOf.get(unitOf.get(x))); x = unitOf.get(x); } return x; };
  for (const l of links) if (l.role === "spouse" && level.get(l.a) === level.get(l.b)) unitOf.set(find(l.a), find(l.b));
  const units = new Map();
  for (const id of members) {
    const u = find(id);
    if (!units.has(u)) units.set(u, { id: u, members: [], level: level.get(id), kids: [], parent: null });
    units.get(u).members.push(id);
  }
  const U = [...units.values()].sort((p, q) => Math.min(...p.members.map((x) => order.get(x))) - Math.min(...q.members.map((x) => order.get(x))));
  for (const u of U) {
    u.members.sort((p, q) => order.get(p) - order.get(q));
    // three or more spouses: the one with most spouse links sits in the middle
    if (u.members.length > 2) {
      const sp = (id) => links.filter((l) => l.role === "spouse" && (l.a === id || l.b === id)).length;
      const mid = [...u.members].sort((p, q) => sp(q) - sp(p))[0];
      const rest = u.members.filter((x) => x !== mid);
      u.members = [...rest.slice(0, Math.floor(rest.length / 2)), mid, ...rest.slice(Math.floor(rest.length / 2))];
    }
    u.first = Math.min(...u.members.map((x) => order.get(x)));
  }
  // Each unit hangs under the parent unit most of its members' parents belong to (one layer or more above).
  for (const u of U) {
    const votes = new Map();
    for (const l of links) {
      if (l.role !== "parent" || !u.members.includes(l.b)) continue;
      const pu = units.get(find(l.a));
      if (pu === u || pu.level >= u.level) continue;
      votes.set(pu, (votes.get(pu) || 0) + 1);
    }
    const best = [...votes].sort((p, q) => q[1] - p[1] || p[0].first - q[0].first)[0];
    if (best) { u.parent = best[0]; best[0].kids.push(u); }
  }
  // Roots in the order links join them, so siblings and cousins without known parents stand together.
  const roots = U.filter((u) => !u.parent);
  const linked = (p, q) => links.some((l) => (p.members.includes(l.a) && q.members.includes(l.b)) || (p.members.includes(l.b) && q.members.includes(l.a)));
  const orderedRoots = [];
  const left = [...roots];
  while (left.length) {
    let next = left[0];
    const prev = orderedRoots[orderedRoots.length - 1];
    if (prev) next = left.find((u) => linked(prev, u)) || left[0];
    left.splice(left.indexOf(next), 1);
    orderedRoots.push(next);
  }
  // A member linked to the neighbour on the right stands at that end of its unit.
  for (let i = 0; i + 1 < orderedRoots.length; i++) {
    const p = orderedRoots[i], q = orderedRoots[i + 1];
    const touch = (u, other) => u.members.filter((m) => links.some((l) => (l.a === m && other.members.includes(l.b)) || (l.b === m && other.members.includes(l.a))));
    const tp = touch(p, q), tq = touch(q, p);
    if (tp.length && p.members.length > 1) p.members = [...p.members.filter((m) => !tp.includes(m)), ...tp];
    if (tq.length && q.members.length > 1) q.members = [...tq, ...q.members.filter((m) => !tq.includes(m))];
  }

  const GAP = 0.35;
  const x = new Map();
  const wid = new Map();
  const widthOf = (u) => {
    const own = u.members.length;
    const kids = u.kids.reduce((t, k) => t + widthOf(k), 0) + Math.max(0, u.kids.length - 1) * GAP;
    wid.set(u, Math.max(own, kids));
    return wid.get(u);
  };
  const place = (u, x0) => {
    const w = wid.get(u), centre = x0 + w / 2;
    u.members.forEach((m, i) => x.set(m, centre - u.members.length / 2 + i + 0.5));
    const kidsW = u.kids.reduce((t, k) => t + wid.get(k), 0) + Math.max(0, u.kids.length - 1) * GAP;
    let cx = x0 + (w - kidsW) / 2;
    for (const k of u.kids) { place(k, cx); cx += wid.get(k) + GAP; }
  };
  const run = () => {
    let cx = 0;
    for (const r of orderedRoots) widthOf(r);
    for (const r of orderedRoots) { place(r, cx); cx += wid.get(r) + GAP; }
  };
  for (const u of U) u.kids.sort((p, q) => p.first - q.first);
  run();
  // One barycentre pass: children also tied to someone outside their parent unit lean toward that person.
  for (const u of U) {
    if (u.kids.length < 2) continue;
    const bary = (k) => {
      const xs = [];
      for (const l of links) for (const m of k.members) {
        const o = l.a === m ? l.b : l.b === m ? l.a : null;
        if (o && !k.members.includes(o) && !u.members.includes(o) && !(l.role === "parent" && l.b === m)) xs.push(x.get(o));
      }
      return xs.length ? xs.reduce((t, v) => t + v, 0) / xs.length : null;
    };
    const keyed = u.kids.map((k, i) => ({ k, b: bary(k), i }));
    if (keyed.every((e) => e.b === null)) continue;
    const mean = (e) => (e.b === null ? x.get(e.k.members[0]) : e.b);
    keyed.sort((p, q) => mean(p) - mean(q) || p.i - q.i);
    u.kids = keyed.map((e) => e.k);
  }
  run();
  const shift = Math.min(...x.values()) - 0.5;
  for (const [k, v] of x) x.set(k, v - shift);

  // Links to draw, with what the reader sees on hover.
  const draw = links.map((l) => {
    const pa = units.get(find(l.a)), pb = units.get(find(l.b));
    let kind = "other";
    if (l.role === "parent") kind = "child";
    else if (l.role === "spouse" && pa === pb) kind = "spouse";
    else if (l.role === "sibling") kind = "sibling";
    return { ...l, kind, title: l.texts.join("\n"), tree: l.role === "parent" && pb.parent === pa };
  });
  const ties = new Map(members.map((id) => [id, []]));
  for (const l of draw) for (const id of [l.a, l.b]) for (const t of l.texts) if (!ties.get(id).includes(t)) ties.get(id).push(t);
  const layers = Math.max(...level.values()) + 1;
  return {
    id: members[0], name: familyName(members, links, ents, order), members,
    layer: level, x, layers, width: Math.max(...x.values()) + 0.5, links: draw,
    units: U.map((u) => ({ members: u.members, parent: u.parent ? u.parent.members[0] : null })), ties,
    hub: [...members].sort((p, q) => deg.get(q) - deg.get(p) || order.get(p) - order.get(q))[0],
  };
}

/** Is this entity in any family of the view? (Drives the "See the family tree" link.) */
export const inFamily = (view, id, opts = {}) => families(view, opts).some((f) => f.members.includes(id));
