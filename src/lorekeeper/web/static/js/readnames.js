// The name-lighting rule of the reading view (read.js), dependency-free so Node can test it against mentions.py
// (tests/js/run_read.mjs, tests/test_js_read.py). Nothing here reads the page or the index: the caller hands in the
// gated view and, per visible name, the position its own `from` anchor is revealed at.

/** Lit without the toggle: people and creatures. With "Light places and groups": places, groups and things too. */
export const ALWAYS = new Set(["person", "creature"]);
export const WITH_TOGGLE = new Set(["place", "group", "object"]);

// mentions.py's name rules, ported: titles are stripped and a bare given name or surname becomes a form of its own only
// when no other visible person or creature uses that word (merge.HONORIFICS and merge.PARTICLES, kept in step by hand).
export const HONORIFICS = new Set(["mr", "mrs", "miss", "ms", "dr", "doctor", "sir", "lady", "lord", "captain", "major",
  "colonel", "sergeant", "inspector", "professor", "young", "old", "the",
  "brother", "sister", "father", "mother", "frere", "pere", "abbot", "abbess", "saint", "st",
  "prior", "king", "queen", "prince", "princess", "dame", "master", "mistress", "madame",
  "monsieur", "mademoiselle", "uncle", "aunt", "lieutenant", "general", "commander", "chevalier",
  "capitaine", "captaine", "marquis", "marquise", "viscount", "viscontessa", "baron", "baroness",
  "duke", "duchess", "count", "countess", "emperor", "empress", "prioress", "seraph", "forgemaster"]);
export const PARTICLES = new Set(["de", "del", "della", "di", "da", "van", "von", "der", "den", "la", "le", "du", "des", "of", "the",
  "al", "el", "bin", "ibn", "ben", "st", "á", "y", "e", "ter", "ten", "zu", "af"]);
const NOTE = /\([^)]*\)/g;
const plain = (w) => w.normalize("NFKD").replace(/\p{M}/gu, "");
const tokens = (name) => name.replace(NOTE, " ").split(/\s+/).filter(Boolean);
const trimEnd = (t) => t.replace(/[.,]+$/, "");
const isTitle = (t) => HONORIFICS.has(plain(trimEnd(t).toLowerCase()));
const isUpper = (c) => Boolean(c) && c !== c.toLowerCase() && c === c.toUpperCase();

/**
 * Extra forms for the visible people and creatures, as mentions.py `derive` makes them. `ents` are {id, type, names};
 * the answer maps an id to [[form, a name it came from]], one pair per source name (mentions.py lists each form once).
 */
export function derive(ents) {
  const people = new Set(ents.filter((e) => ALWAYS.has(e.type)).map((e) => e.id));
  const wordsOf = new Map();
  const spelled = new Set();
  for (const e of ents) {
    const set = wordsOf.get(e.id) || new Set();
    for (const n of e.names) { spelled.add(tokens(n).join(" ")); for (const t of tokens(n)) set.add(trimEnd(t)); }
    wordsOf.set(e.id, set);
  }
  const out = new Map();
  for (const e of ents) {
    if (!ALWAYS.has(e.type)) continue;
    const others = new Set();
    for (const [k, w] of wordsOf) if (k !== e.id && people.has(k)) for (const x of w) others.add(x);
    const forms = [];
    for (const n of e.names) {
      let t = tokens(n);
      while (t.length > 1 && isTitle(t[0])) t = t.slice(1);
      if (!t.length || !isUpper(t[0][0]) || (t.length === 1 && isTitle(t[0]))) continue;
      const cands = [t.join(" "), ...(t.length > 1 ? [t[0], t[t.length - 1]] : [])];
      for (const c of cands) {
        const w = trimEnd(c);
        const ok = c.includes(" ") || t.length === 1 ? true
          : w.length >= 3 && isUpper(w[0]) && !isTitle(w) && !PARTICLES.has(w.toLowerCase());
        // Every name a form comes from is kept (as [form, name] pairs), so the form is lit from the earliest one's reveal.
        if (ok && !spelled.has(c) && !forms.some((f) => f[0] === c && f[1] === n) && !c.split(" ").some((x) => others.has(x))) forms.push([c, n]);
      }
    }
    if (forms.length) out.set(e.id, forms);
  }
  return out;
}

/**
 * What can be lit: every form of every visible name, with the entities that own it and, per owner, the position its
 * name is revealed at (`thr`, a fraction of the book; lit in a block only when `thr` is at or before the block's end).
 * `nameAt(id, value)` gives that position for a visible name. Forms are sorted longest first for one alternation.
 */
export function litForms(view, types, nameAt) {
  const ents = view.entities.filter((e) => types.has(e.type)).map((e) => ({ id: e.id, type: e.type, names: [e.name, ...e.aliases] }));
  const all = view.entities.map((e) => ({ id: e.id, type: e.type, names: [e.name, ...e.aliases] }));
  const extra = derive(all);
  const forms = new Map();   // form → Map(id → thr)
  const add = (form, id, thr) => {
    const f = form.replace(NOTE, " ").split(/\s+/).filter(Boolean).join(" ");
    if (!f || !/[\p{L}\p{N}]/u.test(f) || !Number.isFinite(thr) && thr !== -Infinity) return;
    const owners = forms.get(f) || new Map();
    owners.set(id, Math.min(owners.get(id) ?? Infinity, thr));
    forms.set(f, owners);
  };
  for (const e of ents) {
    const thrs = new Map(e.names.map((n) => [n, nameAt(e.id, n)]));
    for (const n of e.names) add(n, e.id, thrs.get(n));
    for (const [form, from] of extra.get(e.id) || []) add(form, e.id, thrs.get(from));
  }
  return [...forms.entries()].sort((a, b) => b[0].length - a[0].length || (a[0] < b[0] ? -1 : 1));
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** One alternation over the forms, whole words and case-sensitive as mentions.py matches them. */
export function matcher(forms) {
  if (!forms.length) return null;
  return new RegExp(`(?<![\\p{L}\\p{N}_])(?<![\\p{L}\\p{N}_]['’])(?:${forms.map(([f]) => escape(f)).join("|")})(?![\\p{L}\\p{N}_])`, "gu");
}

/** The lit spans of one block: [{start, end, ids}] for the owners whose name is revealed at or before the block's end. */
export function litSpans(text, re, owners, blockEnd) {
  if (!re) return [];
  const out = [];
  re.lastIndex = 0;
  for (const m of text.matchAll(re)) {
    const ids = [...(owners.get(m[0]) || [])].filter(([, thr]) => thr <= blockEnd + 1e-12).map(([id]) => id);
    // A longer form not revealed here is left unlit rather than searched for a shorter one: it can only under-light.
    if (ids.length) out.push({ start: m.index, end: m.index + m[0].length, ids });
  }
  return out;
}

