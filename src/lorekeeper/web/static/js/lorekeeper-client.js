// Lorekeeper reference client (SPEC.md §3 and §7): everything a reader app needs to turn the
// reader's own EPUB plus a published index into spoiler-safe answers. No dependencies; runs as a
// browser module and in Node 22+. Gating itself lives in gating.js, which this file imports.
//
// Four calls, from "I have an EPUB" to "what is this name?":
//
//   import { buildEdition, LorekeeperIndex } from "/static/js/lorekeeper-client.js";
//
//   // 1. The EPUB's spine items in order (every itemref, so spineChild == array index), each parsed
//   //    with DOMParser (or give {blocks: [{words}]} if you already hold the text model).
//   const docs = spine.map((item, i) => ({
//     spineChild: i, linear: item.linear !== "no",
//     document: new DOMParser().parseFromString(xhtmlOf(item), "application/xhtml+xml") }));
//   const edition = buildEdition(docs);                          // 2. block numbering (§3.1)
//   const index = new LorekeeperIndex(indexJson, edition);       // 3. resolves anchors once (§3.6)
//   const hits = index.lookup("Holmes", { cfi: "epubcfi(/6/22!/4/108/1:0)" });   // 4. ask (§3.8, §8.6)
//
// `hits` is [{entity, facets, relations, events}], limited to what the reader has already read.
// `index.view(locator)` gives the whole spoiler-safe view, `index.at(locator)` just the position.
// Locators (§3.8): {block}, {cfi}, {xpointer}, {spine, word}, {spine, progress}.

import { view as gate } from "./gating.js";

export class LocatorError extends Error {
  constructor(message) { super(message); this.name = "LocatorError"; }
}

// ---------------------------------------------------------------------------------------------
// §3.2 Normalization

const JOINER_RE = /[-‐‑'’­‍⁠﻿]/g;
const WORD_RE = /[\p{L}\p{M}\p{N}]+/gu;

// Where Unicode full case folding differs from String.prototype.toLowerCase, as "cp>folded" pairs
// in hex (generated from Python's str.casefold(); Unicode 17). Folding runs per code point, so the
// context-sensitive final-sigma rule of toLowerCase never applies.
const FOLD_TABLE = `
b5>3bc df>73+73 149>2bc+6e 17f>73 1f0>6a+30c 345>3b9 390>3b9+308+301 3b0>3c5+308+301 3c2>3c3 3d0>3b2
3d1>3b8 3d5>3c6 3d6>3c0 3f0>3ba 3f1>3c1 3f5>3b5 587>565+582 13a0>13a0 13a1>13a1 13a2>13a2 13a3>13a3
13a4>13a4 13a5>13a5 13a6>13a6 13a7>13a7 13a8>13a8 13a9>13a9 13aa>13aa 13ab>13ab 13ac>13ac 13ad>13ad
13ae>13ae 13af>13af 13b0>13b0 13b1>13b1 13b2>13b2 13b3>13b3 13b4>13b4 13b5>13b5 13b6>13b6 13b7>13b7
13b8>13b8 13b9>13b9 13ba>13ba 13bb>13bb 13bc>13bc 13bd>13bd 13be>13be 13bf>13bf 13c0>13c0 13c1>13c1
13c2>13c2 13c3>13c3 13c4>13c4 13c5>13c5 13c6>13c6 13c7>13c7 13c8>13c8 13c9>13c9 13ca>13ca 13cb>13cb
13cc>13cc 13cd>13cd 13ce>13ce 13cf>13cf 13d0>13d0 13d1>13d1 13d2>13d2 13d3>13d3 13d4>13d4 13d5>13d5
13d6>13d6 13d7>13d7 13d8>13d8 13d9>13d9 13da>13da 13db>13db 13dc>13dc 13dd>13dd 13de>13de 13df>13df
13e0>13e0 13e1>13e1 13e2>13e2 13e3>13e3 13e4>13e4 13e5>13e5 13e6>13e6 13e7>13e7 13e8>13e8 13e9>13e9
13ea>13ea 13eb>13eb 13ec>13ec 13ed>13ed 13ee>13ee 13ef>13ef 13f0>13f0 13f1>13f1 13f2>13f2 13f3>13f3
13f4>13f4 13f5>13f5 13f8>13f0 13f9>13f1 13fa>13f2 13fb>13f3 13fc>13f4 13fd>13f5 1c80>432 1c81>434
1c82>43e 1c83>441 1c84>442 1c85>442 1c86>44a 1c87>463 1c88>a64b 1e96>68+331 1e97>74+308 1e98>77+30a
1e99>79+30a 1e9a>61+2be 1e9b>1e61 1e9e>73+73 1f50>3c5+313 1f52>3c5+313+300 1f54>3c5+313+301
1f56>3c5+313+342 1f80>1f00+3b9 1f81>1f01+3b9 1f82>1f02+3b9 1f83>1f03+3b9 1f84>1f04+3b9 1f85>1f05+3b9
1f86>1f06+3b9 1f87>1f07+3b9 1f88>1f00+3b9 1f89>1f01+3b9 1f8a>1f02+3b9 1f8b>1f03+3b9 1f8c>1f04+3b9
1f8d>1f05+3b9 1f8e>1f06+3b9 1f8f>1f07+3b9 1f90>1f20+3b9 1f91>1f21+3b9 1f92>1f22+3b9 1f93>1f23+3b9
1f94>1f24+3b9 1f95>1f25+3b9 1f96>1f26+3b9 1f97>1f27+3b9 1f98>1f20+3b9 1f99>1f21+3b9 1f9a>1f22+3b9
1f9b>1f23+3b9 1f9c>1f24+3b9 1f9d>1f25+3b9 1f9e>1f26+3b9 1f9f>1f27+3b9 1fa0>1f60+3b9 1fa1>1f61+3b9
1fa2>1f62+3b9 1fa3>1f63+3b9 1fa4>1f64+3b9 1fa5>1f65+3b9 1fa6>1f66+3b9 1fa7>1f67+3b9 1fa8>1f60+3b9
1fa9>1f61+3b9 1faa>1f62+3b9 1fab>1f63+3b9 1fac>1f64+3b9 1fad>1f65+3b9 1fae>1f66+3b9 1faf>1f67+3b9
1fb2>1f70+3b9 1fb3>3b1+3b9 1fb4>3ac+3b9 1fb6>3b1+342 1fb7>3b1+342+3b9 1fbc>3b1+3b9 1fbe>3b9
1fc2>1f74+3b9 1fc3>3b7+3b9 1fc4>3ae+3b9 1fc6>3b7+342 1fc7>3b7+342+3b9 1fcc>3b7+3b9 1fd2>3b9+308+300
1fd3>3b9+308+301 1fd6>3b9+342 1fd7>3b9+308+342 1fe2>3c5+308+300 1fe3>3c5+308+301 1fe4>3c1+313
1fe6>3c5+342 1fe7>3c5+308+342 1ff2>1f7c+3b9 1ff3>3c9+3b9 1ff4>3ce+3b9 1ff6>3c9+342 1ff7>3c9+342+3b9
1ffc>3c9+3b9 ab70>13a0 ab71>13a1 ab72>13a2 ab73>13a3 ab74>13a4 ab75>13a5 ab76>13a6 ab77>13a7
ab78>13a8 ab79>13a9 ab7a>13aa ab7b>13ab ab7c>13ac ab7d>13ad ab7e>13ae ab7f>13af ab80>13b0 ab81>13b1
ab82>13b2 ab83>13b3 ab84>13b4 ab85>13b5 ab86>13b6 ab87>13b7 ab88>13b8 ab89>13b9 ab8a>13ba ab8b>13bb
ab8c>13bc ab8d>13bd ab8e>13be ab8f>13bf ab90>13c0 ab91>13c1 ab92>13c2 ab93>13c3 ab94>13c4 ab95>13c5
ab96>13c6 ab97>13c7 ab98>13c8 ab99>13c9 ab9a>13ca ab9b>13cb ab9c>13cc ab9d>13cd ab9e>13ce ab9f>13cf
aba0>13d0 aba1>13d1 aba2>13d2 aba3>13d3 aba4>13d4 aba5>13d5 aba6>13d6 aba7>13d7 aba8>13d8 aba9>13d9
abaa>13da abab>13db abac>13dc abad>13dd abae>13de abaf>13df abb0>13e0 abb1>13e1 abb2>13e2 abb3>13e3
abb4>13e4 abb5>13e5 abb6>13e6 abb7>13e7 abb8>13e8 abb9>13e9 abba>13ea abbb>13eb abbc>13ec abbd>13ed
abbe>13ee abbf>13ef fb00>66+66 fb01>66+69 fb02>66+6c fb03>66+66+69 fb04>66+66+6c fb05>73+74
fb06>73+74 fb13>574+576 fb14>574+565 fb15>574+56b fb16>57e+576 fb17>574+56d
`;
const FOLD = new Map(FOLD_TABLE.trim().split(/\s+/).map((pair) => {
  const [cp, to] = pair.split(">");
  return [parseInt(cp, 16), String.fromCodePoint(...to.split("+").map((h) => parseInt(h, 16)))];
}));
const NEEDS_SLOW_FOLD = new RegExp(
  `[\\u03a3${[...FOLD.keys()].map((cp) => `\\u{${cp.toString(16)}}`).join("")}]`, "u");

function casefold(s) {
  if (!NEEDS_SLOW_FOLD.test(s)) return s.toLowerCase();
  let out = "";
  for (const ch of s) out += FOLD.get(ch.codePointAt(0)) ?? ch.toLowerCase();
  return out;
}

/** Normalized words of a text (byte-identical to text.py words()). */
export function words(text) {
  const s = casefold(String(text).normalize("NFKC")).replace(JOINER_RE, "");
  return s.match(WORD_RE) ?? [];
}

// ---------------------------------------------------------------------------------------------
// §3.3, §3.4 Fingerprints and edition identity: SHA-256, synchronous, pure JS.

const K = new Uint32Array(64);
const H0 = new Uint32Array(8);
(() => {
  const primes = [];
  for (let n = 2; primes.length < 64; n++) if (primes.every((p) => n % p)) primes.push(n);
  const frac = (x) => Math.floor((x - Math.floor(x)) * 4294967296);
  primes.forEach((p, i) => { K[i] = frac(Math.cbrt(p)); if (i < 8) H0[i] = frac(Math.sqrt(p)); });
})();

class Sha256 {
  constructor() {
    this.h = Uint32Array.from(H0);
    this.buf = new Uint8Array(64);
    this.fill = 0;
    this.bytes = 0;
    this.w = new Uint32Array(64);
  }

  update(data) {
    this.bytes += data.length;
    let i = 0;
    while (i < data.length) {
      const n = Math.min(64 - this.fill, data.length - i);
      this.buf.set(data.subarray(i, i + n), this.fill);
      this.fill += n; i += n;
      if (this.fill === 64) { this.block(); this.fill = 0; }
    }
    return this;
  }

  block() {
    const w = this.w, b = this.buf, h = this.h;
    for (let i = 0; i < 16; i++) w[i] = (b[4 * i] << 24) | (b[4 * i + 1] << 16) | (b[4 * i + 2] << 8) | b[4 * i + 3];
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], c = w[i - 2];
      const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
      const s1 = ((c >>> 17) | (c << 15)) ^ ((c >>> 19) | (c << 13)) ^ (c >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let [a, bb, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (hh + S1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (S0 + ((a & bb) ^ (a & c) ^ (bb & c))) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    h[0] += a; h[1] += bb; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
  }

  hex() {
    const bits = this.bytes * 8;
    const pad = new Uint8Array(((this.fill < 56 ? 56 : 120) - this.fill) + 8);
    pad[0] = 0x80;
    const view = new DataView(pad.buffer);
    view.setUint32(pad.length - 8, Math.floor(bits / 4294967296));
    view.setUint32(pad.length - 4, bits >>> 0);
    const bytes = this.bytes;
    this.update(pad);
    this.bytes = bytes;
    return [...this.h].map((x) => (x >>> 0).toString(16).padStart(8, "0")).join("");
  }
}

const utf8 = new TextEncoder();
const sha256Hex = (s) => new Sha256().update(utf8.encode(s)).hex();

/** Fingerprint of a block: SHA-256 of its first 12 words, first 16 hex chars (§3.3). */
export function fingerprint(blockWords) {
  return sha256Hex(blockWords.slice(0, 12).join(" ")).slice(0, 16);
}

/** Edition identity (§3.4): SHA-256 over every word of every block, space-joined. */
export function textHash(blocksWords) {
  const h = new Sha256();
  let first = true;
  for (const block of blocksWords) {
    for (const w of block) {
      h.update(utf8.encode(first ? w : " " + w));
      first = false;
    }
  }
  return h.hex();
}

// ---------------------------------------------------------------------------------------------
// §3.1 Text model: blocks from a DOM document

const BLOCK_TAGS = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th",
  "pre", "figcaption", "caption"]);
const FALLBACK_BLOCK_TAGS = new Set([...BLOCK_TAGS, "div", "blockquote", "section", "article", "aside"]);
const SKIP_TAGS = new Set(["script", "style", "head", "title"]);
const FALLBACK_MIN_WORDS = 20;

const lname = (n) => (n.localName || n.nodeName || "").toLowerCase();
const isText = (n) => n.nodeType === 3 || n.nodeType === 4;
const elementChildren = (n) => Array.from(n.childNodes, (c) => c).filter((c) => c.nodeType === 1);

/** One pass over the document: blocks in start-tag order, which block owns each text node, and a
 *  document-order number for every node (so "the block before this point" can be found). */
function scan(root, tags) {
  const blocks = [];
  const owner = new Map();
  const order = new Map();
  let n = 0;
  const stack = [];
  const number = (node) => { order.set(node, n++); for (const c of node.childNodes) number(c); };
  const visit = (node) => {
    if (isText(node)) {
      order.set(node, n++);
      if (stack.length) {
        const top = stack[stack.length - 1];
        top.parts.push(node.data ?? node.nodeValue ?? "");
        owner.set(node, top);
      }
      return;
    }
    if (node.nodeType !== 1) { order.set(node, n++); return; }
    const name = lname(node);
    if (SKIP_TAGS.has(name)) { number(node); return; }
    order.set(node, n++);
    let block = null;
    if (tags.has(name)) {
      block = { element: node, parts: [], start: order.get(node) };
      blocks.push(block);
      stack.push(block);
    } else if (name === "br" && stack.length) {
      stack[stack.length - 1].parts.push(" ");
    }
    for (const c of node.childNodes) visit(c);
    if (block) stack.pop();
  };
  visit(root);
  for (const b of blocks) { b.raw = b.parts.join(""); b.words = words(b.raw); delete b.parts; }
  return { blocks, owner, order };
}

function parseDocument(doc, fallback) {
  const root = doc.documentElement ?? doc;
  const standard = scan(root, BLOCK_TAGS);
  if (!fallback || standard.blocks.some((b) => b.words.length)) return standard;
  const probe = scan(root, FALLBACK_BLOCK_TAGS);
  const total = probe.blocks.reduce((sum, b) => sum + b.words.length, 0);
  return total >= FALLBACK_MIN_WORDS ? probe : standard;
}

function elementPathOf(el) {
  const path = [];
  for (let n = el; n && n.parentNode && n.parentNode.nodeType === 1; n = n.parentNode) {
    path.unshift(elementChildren(n.parentNode).indexOf(n) + 1);
  }
  return path;
}

/**
 * Blocks of one XHTML document (§3.1 rules 2 to 4 and 6), words-empty blocks dropped.
 * @param {Document} doc  a DOM document (DOMParser output, or any DOM with childNodes/nodeType)
 * @returns {{words: string[], raw: string, element: Element, elementPath: number[]}[]}
 *   `elementPath` is the 1-based element-child index at each level below the root element.
 */
export function blocksFromDocument(doc, { fallback = true } = {}) {
  return parseDocument(doc, fallback).blocks.filter((b) => b.words.length).map((b) => ({
    words: b.words,
    raw: b.raw.split(/\s+/).filter(Boolean).join(" "),
    element: b.element,
    get elementPath() { return elementPathOf(b.element); },
  }));
}

// ---------------------------------------------------------------------------------------------
// Edition: seq numbering over the linear documents

/**
 * @param {{spineChild?: number, linear?: boolean, document?: Document, blocks?: {words: string[]}[]}[]} docs
 *   every `itemref` of the spine in order, so `spineChild` is the array index. Non-linear items get
 *   no blocks (§3.1 rule 1) but keep their place for CFI steps.
 * @returns {{blocks: {seq, spine, words, fp}[], docs: {spineChild, spine, first, count, document?}[]}}
 */
export function buildEdition(docs) {
  const blocks = [];
  const out = [];
  let spine = 0;
  docs.forEach((d, i) => {
    const entry = { spineChild: d.spineChild ?? i, spine: null, first: null, count: 0, document: d.document };
    if (d.linear !== false) {
      entry.spine = spine++;
      entry.first = blocks.length;
      const src = d.blocks ?? (d.document ? blocksFromDocument(d.document) : []);
      for (const b of src) {
        if (!b.words.length) continue;
        blocks.push({ seq: blocks.length, spine: entry.spine, words: b.words, fp: fingerprint(b.words) });
      }
      entry.count = blocks.length - entry.first;
    }
    out.push(entry);
  });
  return {
    blocks,
    docs: out,
    get textHash() { return textHash(blocks.map((b) => b.words)); },
  };
}

// ---------------------------------------------------------------------------------------------
// §3.6 Anchor resolution

/** Anchor id → block seq in this edition, or null when it cannot be found. */
export function resolveAnchors(anchors, edition) {
  const byFp = new Map();
  for (const b of edition.blocks) {
    if (!byFp.has(b.fp)) byFp.set(b.fp, []);
    byFp.get(b.fp).push(b.seq);
  }
  const out = new Map();
  let prev = -1;
  for (const a of anchors) {
    const seqs = byFp.get(a.fp) ?? [];
    let lo = 0, hi = seqs.length;           // first seq strictly greater than prev
    while (lo < hi) { const mid = (lo + hi) >> 1; if (seqs[mid] <= prev) lo = mid + 1; else hi = mid; }
    if (lo < seqs.length) { prev = seqs[lo]; out.set(a.id, prev); } else out.set(a.id, null);
  }
  return out;
}

/** Location used for gating: an unresolved anchor takes the next resolved anchor's location. */
export function effectiveLocations(anchors, resolved) {
  const eff = new Array(anchors.length).fill(Infinity);
  let next = Infinity;
  for (let i = anchors.length - 1; i >= 0; i--) {
    const loc = resolved.get(anchors[i].id);
    if (loc !== null && loc !== undefined) next = loc;
    eff[i] = next;
  }
  return eff;
}

/** Last anchor revealed once the reader has finished block `completedSeq`, or null. */
export function frontier(anchors, resolved, completedSeq) {
  const eff = effectiveLocations(anchors, resolved);
  let lo = 0, hi = eff.length;              // count of eff <= completedSeq (eff never decreases)
  while (lo < hi) { const mid = (lo + hi) >> 1; if (eff[mid] <= completedSeq) lo = mid + 1; else hi = mid; }
  return lo ? anchors[lo - 1].id : null;
}

/** Anchor id for a block seq (§3.7): `finished` counts blocks before seq, `inclusive` seq itself. */
export function position(anchors, resolved, seq, { inclusive = false } = {}) {
  return frontier(anchors, resolved, inclusive ? seq : seq - 1);
}

// ---------------------------------------------------------------------------------------------
// §3.8 Locators

const wordsCache = new WeakMap();
function cumulativeWords(edition, entry) {
  let cum = wordsCache.get(entry);
  if (!cum) {
    cum = [];
    let total = 0;
    for (let s = entry.first; s < entry.first + entry.count; s++) { total += edition.blocks[s].words.length; cum.push(total); }
    wordsCache.set(entry, cum);
  }
  return cum;
}

function linearDocument(edition, spine) {
  if (!Number.isInteger(spine) || spine < 0) throw new LocatorError(`bad spine index ${spine}`);
  const entry = edition.docs.find((d) => d.spine === spine);
  if (!entry) throw new LocatorError(`no linear spine document ${spine}`);
  return entry;
}

function blockAtWord(edition, entry, word) {
  if (!entry.count) return entry.first - 1;
  const cum = cumulativeWords(edition, entry);
  let lo = 0, hi = cum.length - 1;          // first block whose cumulative count exceeds `word`
  if (word >= cum[hi]) return entry.first + hi;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (cum[mid] > word) hi = mid; else lo = mid + 1; }
  return entry.first + lo;
}

const FORMS = [
  ["block", ["block"]], ["cfi", ["cfi"]], ["xpointer", ["xpointer"]],
  ["word", ["spine", "word"]], ["progress", ["spine", "progress"]],
];

function formOf(locator) {
  if (!locator || typeof locator !== "object" || Array.isArray(locator)) throw new LocatorError("locator must be an object");
  const keys = Object.keys(locator).sort().join(",");
  for (const [form, members] of FORMS) if (keys === [...members].sort().join(",")) return form;
  throw new LocatorError(`locator members {${keys}} match no form`);
}

/** DOM facts about one linear document, computed on first use. */
const domCache = new WeakMap();
function domInfo(entry) {
  if (!entry.document) return null;
  if (domCache.has(entry)) return domCache.get(entry);
  const parsed = parseDocument(entry.document, true);
  const kept = parsed.blocks.filter((b) => b.words.length);
  let info = null;
  if (kept.length === entry.count) {         // the DOM must be the document the edition was numbered from
    const byId = new Map();
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType !== 1) continue;
        const id = c.getAttribute ? c.getAttribute("id") : null;
        if (id && !byId.has(id)) byId.set(id, c);
        walk(c);
      }
    };
    const root = entry.document.documentElement ?? entry.document;
    walk(root);
    info = { ...parsed, kept, byId, root, blockOfElement: new Map(kept.map((b, i) => [b.element, i])) };
  }
  domCache.set(entry, info);
  return info;
}

/** Index in `kept` of the last block that starts before document-order number `at`, or -1. */
function precedingBlock(info, at) {
  let lo = 0, hi = info.kept.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (info.kept[mid].start < at) lo = mid + 1; else hi = mid; }
  return lo - 1;
}

function lastDescendantOrder(info, node) {
  let n = node;
  while (n.childNodes && n.childNodes.length) n = n.childNodes[n.childNodes.length - 1];
  return info.order.get(n) ?? info.order.get(node);
}

/** The innermost block with words that is `el` or contains it, or null. */
function owningBlock(info, el) {
  for (let n = el; n && n.nodeType === 1; n = n.parentNode) {
    if (info.blockOfElement.has(n)) return info.blockOfElement.get(n);
  }
  return null;
}

/** Rules 1 and 2 for an element: its block (or the block holding it), else the first block that
 *  starts inside it, else the block before it. Index into the document's blocks, −1 for none. */
function elementBlock(info, el) {
  const own = owningBlock(info, el);
  if (own !== null) return own;
  const at = info.order.get(el);
  const next = precedingBlock(info, at) + 1;
  if (next < info.kept.length && info.kept[next].start <= lastDescendantOrder(info, el)) return next;
  return next - 1;
}

/** Rules 1 and 2 for text gap `k` of `el` (0: before its first child element, j: after child j). */
function gapBlock(info, el, k) {
  const own = owningBlock(info, el);
  if (own !== null) return own;
  if (k === 0) return precedingBlock(info, info.order.get(el));
  return precedingBlock(info, lastDescendantOrder(info, elementChildren(el)[k - 1]) + 0.5);
}

/** Gaps of `el` that hold text, as gap numbers in document order. */
function textGaps(el) {
  const gaps = [];
  let seen = 0;
  for (const c of el.childNodes) {
    if (c.nodeType === 1) { seen++; continue; }
    if (isText(c) && (c.data ?? c.nodeValue ?? "").length && gaps[gaps.length - 1] !== seen) gaps.push(seen);
  }
  return gaps;
}

function spineChildDocument(edition, spineChild) {
  const entry = edition.docs[spineChild];
  if (!entry) throw new LocatorError(`no spine item ${spineChild + 1}`);
  if (entry.spine === null) throw new LocatorError(`spine item ${spineChild + 1} is not linear`);
  return entry;
}

// Document found but the path inside it does not resolve (rule 6): the block before the document.
const UNRESOLVED = (entry, basis) => ({ seq: entry.first - 1, basis, exact: false });

const CFI_ID = String.raw`\[(?:[^\]^]|\^.)*\]`;

function parseCfi(raw) {
  const m = typeof raw === "string" ? /^\s*epubcfi\(([\s\S]*)\)\s*$/.exec(raw) : null;
  if (!m) throw new LocatorError("not an epubcfi(...) string");
  const parts = m[1].split(/(?<!\^),/);      // a range "parent,start,end": locate where it starts
  if (parts.length > 3) throw new LocatorError("too many commas in CFI range");
  const body = parts.length >= 2 ? parts[0] + parts[1] : parts[0];
  const bang = body.indexOf("!");
  const pkg = bang < 0 ? body : body.slice(0, bang);
  let inner = bang < 0 ? "" : body.slice(bang + 1);
  const pm = /^\/6(?:\[[^\]]*\])?\/(\d+)(?:\[[^\]]*\])?$/.exec(pkg.trim());
  if (!pm) throw new LocatorError("CFI does not start with a spine step (/6/n)");
  const step = Number(pm[1]);
  if (step < 2 || step % 2) throw new LocatorError(`spine step ${step} is not an itemref (must be even)`);
  inner = inner.split("!")[0].split(/[~@]/)[0];       // deeper indirection and spatial/temporal parts
  inner = inner.replace(new RegExp(String.raw`:\d+(?:${CFI_ID})?\s*$`), "");   // the character offset
  if (!new RegExp(String.raw`^(?:\/\d+(?:${CFI_ID})?)*$`).test(inner)) throw new LocatorError("malformed CFI path");
  const steps = [...inner.matchAll(new RegExp(String.raw`\/(\d+)(?:\[((?:[^\]^]|\^.)*)\])?`, "g"))].map((x) => ({
    n: Number(x[1]),
    id: x[2] ? x[2].replace(/\^(.)/g, "$1").split(";")[0] || null : null,
  }));
  return { spineChild: step / 2 - 1, steps };
}

function cfiTarget(info, steps) {
  let el = info.root, gap = null;
  for (let i = 0; i < steps.length; i++) {
    const { n } = steps[i];
    const kids = elementChildren(el);
    if (n % 2) {                              // a text step can only be the last one
      if (i !== steps.length - 1 || (n - 1) / 2 > kids.length) return null;
      gap = (n - 1) / 2;
      break;
    }
    if (!(n / 2 >= 1 && n / 2 <= kids.length)) { el = null; break; }
    el = kids[n / 2 - 1];
  }
  const lastId = [...steps].reverse().find((s) => s.n % 2 === 0 && s.id)?.id;
  const named = lastId ? info.byId.get(lastId) : null;
  if (named && named !== el) return elementBlock(info, named);   // the asserted id names the element
  if (!el) return null;
  return gap !== null ? gapBlock(info, el, gap) : elementBlock(info, el);
}

function locateCfi(edition, locator) {
  const { spineChild, steps } = parseCfi(locator.cfi);
  const entry = spineChildDocument(edition, spineChild);
  const info = domInfo(entry);
  const idx = info ? cfiTarget(info, steps) : null;
  return idx === null ? UNRESOLVED(entry, "cfi") : { seq: entry.first + idx, basis: "cfi", exact: true };
}

function parseXPointer(raw) {
  const m = typeof raw === "string" ? /^\/body\/DocFragment\[(\d+)\](\/.*)?$/s.exec(raw.trim()) : null;
  if (!m) throw new LocatorError("not a /body/DocFragment[n]/... XPointer");
  const n = Number(m[1]);
  if (n < 1) throw new LocatorError("DocFragment index is 1-based");
  const segs = (m[2] ?? "").split("/").filter(Boolean);
  let textK = null;
  if (segs.length && segs[segs.length - 1].startsWith("text()")) {
    const tm = /^text\(\)(?:\[(\d+)\])?(?:\.\d+)?$/.exec(segs.pop());
    if (!tm) throw new LocatorError("malformed text() step");
    textK = tm[1] === undefined ? 1 : Number(tm[1]);
  } else if (segs.length) {
    segs[segs.length - 1] = segs[segs.length - 1].replace(/\.\d+$/, "");   // a bare character offset
  }
  const path = segs.map((s) => {
    const sm = /^([A-Za-z][A-Za-z0-9_:.-]*)(?:\[(\d+)\])?$/.exec(s);
    if (!sm) throw new LocatorError(`malformed XPointer step ${s}`);
    return { name: sm[1].toLowerCase(), index: sm[2] === undefined ? 1 : Number(sm[2]) };
  });
  return { spineChild: n - 1, path, textK };
}

function locateXPointer(edition, locator) {
  const { spineChild, path, textK } = parseXPointer(locator.xpointer);
  const entry = spineChildDocument(edition, spineChild);
  const info = domInfo(entry);
  let idx = null;
  if (info) {
    let el = info.root;
    for (const { name, index } of path) {
      const same = elementChildren(el).filter((c) => lname(c) === name);
      el = same[index - 1] ?? null;
      if (!(index >= 1) || !el) { el = null; break; }
    }
    if (el) {
      const gaps = textK === null ? [] : textGaps(el);
      idx = textK !== null && textK >= 1 && textK <= gaps.length ? gapBlock(info, el, gaps[textK - 1]) : elementBlock(info, el);
    }
  }
  return idx === null ? UNRESOLVED(entry, "xpointer") : { seq: entry.first + idx, basis: "xpointer", exact: true };
}

/**
 * Map a locator to the seq of the block it points into, in the reader's own edition (§3.8).
 * @returns {{seq: number, basis: string, exact: boolean}}  seq −1 means before the first block.
 * @throws {LocatorError} malformed locators and documents that do not exist or are not linear.
 */
export function locate(edition, locator) {
  const form = formOf(locator);
  if (form === "block") {
    const b = locator.block;
    if (!Number.isInteger(b) || b < -1 || b >= edition.blocks.length) throw new LocatorError(`block ${b} is not in this edition`);
    return { seq: b, basis: "block", exact: true };
  }
  if (form === "cfi") return locateCfi(edition, locator);
  if (form === "xpointer") return locateXPointer(edition, locator);
  const entry = linearDocument(edition, locator.spine);
  const total = entry.count ? cumulativeWords(edition, entry)[entry.count - 1] : 0;
  if (form === "word") {
    if (!Number.isInteger(locator.word) || locator.word < 0) throw new LocatorError(`bad word offset ${locator.word}`);
    // Past the end clamps to the last block (rule 4) and is approximate; so is an empty document.
    return { seq: blockAtWord(edition, entry, locator.word), basis: "word", exact: locator.word < total };
  }
  const p = locator.progress;
  if (typeof p !== "number" || !(p >= 0 && p <= 1)) throw new LocatorError(`progress ${p} is not in 0..1`);
  return { seq: blockAtWord(edition, entry, Math.min(Math.floor(p * total), total - 1)), basis: "progress", exact: false };
}

// ---------------------------------------------------------------------------------------------
// The four-call integration

const fold = (s) => casefold(String(s).normalize("NFKC")).split(/\s+/).filter(Boolean).join(" ");

export class LorekeeperIndex {
  /**
   * @param {object} indexDoc  the index of the book being read
   * @param {object} edition   from buildEdition (or any {blocks: [{seq, fp}], docs})
   * @param {{series?: object|null, order?: string|null, finished?: string[]|null, others?: object[]}} options
   *   `others`: indexes of earlier books of the series, for views that carry knowledge across books.
   */
  constructor(indexDoc, edition, { series = null, order = null, finished = null, others = [] } = {}) {
    this.index = indexDoc;
    this.edition = edition;
    this.series = series;
    this.order = order;
    this.finished = finished;
    this.others = others;
    this.anchors = indexDoc.anchors;
    this.resolved = resolveAnchors(this.anchors, edition);
    this.effective = effectiveLocations(this.anchors, this.resolved);
  }

  /** Anchor id the locator puts the reader at, or null before the first anchor. */
  at(locator, { inclusive = false } = {}) {
    return position(this.anchors, this.resolved, locate(this.edition, locator).seq, { inclusive });
  }

  /** The spoiler-safe view (§7) at the locator. */
  view(locator, { inclusive = false } = {}) {
    const { seq } = locate(this.edition, locator);
    const anchor = position(this.anchors, this.resolved, seq, { inclusive });
    const book = this.series ? this.index.series?.book ?? null : null;
    return gate([...this.others, this.index], { book, anchor }, this.series, this.order, this.finished);
  }

  /** Visible entities whose name or alias is `name` (case-insensitive), or whose unique surname is. */
  lookup(name, locator, { inclusive = true } = {}) {
    const view = this.view(locator, { inclusive });
    const q = fold(name), qw = words(name).join(" ");
    const names = (e) => [e.name, ...e.aliases];
    let hits = view.entities.filter((e) => names(e).some((n) => fold(n) === q || (qw && words(n).join(" ") === qw)));
    if (!hits.length && qw && !qw.includes(" ")) {
      const bySurname = view.entities.filter((e) => {
        const w = words(e.name);
        return w.length > 1 && w[w.length - 1] === qw;
      });
      if (bySurname.length === 1) hits = bySurname;
    }
    return hits.map((entity) => ({
      entity,
      facets: entity.facets,
      relations: view.relations.filter((r) => r.src === entity.id || r.dst === entity.id),
      events: view.events.filter((ev) => ev.entities.includes(entity.id)),
    }));
  }
}
