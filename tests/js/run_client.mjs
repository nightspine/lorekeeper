// Checks the JS reference client (src/lorekeeper/web/static/js/lorekeeper-client.js) against the
// Python reference. Exit code = number of failures.
//   node tests/js/run_client.mjs [fixtures-dir]       (fixtures: tests/js/make_fixtures.py)
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseXhtml } from "./dom_shim.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "../..");
const C = await import(join(root, "src/lorekeeper/web/static/js/lorekeeper-client.js"));
const { words, fingerprint, textHash, blocksFromDocument, buildEdition, resolveAnchors, effectiveLocations,
  frontier, locate, position, LorekeeperIndex, LocatorError } = C;
const fx = (n) => JSON.parse(readFileSync(join(resolve(process.argv[2] || join(here, "fixtures")), n), "utf8"));
const canon = (v) => JSON.stringify(v, (k, x) =>
  x && typeof x === "object" && !Array.isArray(x) ? Object.fromEntries(Object.entries(x).sort()) : x);

let failures = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failures++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? "\n       " + detail : ""}`);
};
const same = (a, b) => canon(a) === canon(b);
const throwsLocator = (fn) => { try { fn(); return false; } catch (e) { return e instanceof LocatorError; } };

// (a) words, fingerprint, text hash --------------------------------------------------------------
{
  const s = fx("strings.json");
  check("sha256 of abc", fingerprint(["abc"]) === s.sha256abc.slice(0, 16));
  const bad = s.strings.filter((x) => !same(words(x.text), x.words) || fingerprint(x.words) !== x.fp);
  check(`words/fingerprint parity on ${s.strings.length} strings`, !bad.length,
    bad.slice(0, 3).map((x) => `${JSON.stringify(x.text)} -> ${JSON.stringify(words(x.text))} expected ${JSON.stringify(x.words)}`).join("\n       "));
  const mismatched = [];
  s.codepointHashes.forEach((expected, i) => {
    const h = [];
    for (const [lo, hi] of s.assignedRanges) {
      for (let cp = Math.max(lo, i * 4096); cp <= Math.min(hi, (i + 1) * 4096 - 1); cp++) {
        h.push(words(String.fromCodePoint(cp)).join(" ") + "\n");
      }
    }
    // Python hashed these same lines with hashlib; textHash of a single "word" is SHA-256 of its bytes.
    if (textHash([[h.join("")]]) !== expected) mismatched.push(`0x${(i * 4096).toString(16)}`);
  });
  check(`words() agrees with Python for every code point assigned in its Unicode ${s.unicode}`, !mismatched.length, `chunks differ: ${mismatched.join(" ")}`);
}

// (e) text model from DOM, edition, text hash ------------------------------------------------------
const ed = fx("edition.json");
const docsFx = fx("documents.json");
const parsed = docsFx.map((d) => ({ ...d, document: parseXhtml(d.html) }));
const edition = buildEdition(parsed.map((d) => ({ spineChild: d.spineChild, linear: d.linear, document: d.document })));
{
  const same_blocks = edition.blocks.length === ed.blocks.length
    && edition.blocks.every((b, i) => same(b, ed.blocks[i]));
  check(`blocks of pg244 (${edition.blocks.length}) match epub.read: seq, spine, words, fp`, same_blocks);
  check("textHash matches Python", edition.textHash === ed.textHash);
  check("doc table (first/spine)", edition.docs.every((d, i) => d.spine === ed.docs[i].spine && (d.first === ed.docs[i].first)));
  for (const [i, c] of fx("htmlcases.json").entries()) {
    const got = blocksFromDocument(parseXhtml(c.html)).map((b) => b.words);
    check(`html case ${i} blocks (incl. rule 6 fallback)`, same(got, c.blocks), JSON.stringify(got));
  }
  const noFallback = blocksFromDocument(parseXhtml(`<html><body><div>${"word ".repeat(25)}</div></body></html>`), { fallback: false });
  check("fallback: false skips rule 6", noFallback.length === 0);
}

// (b) anchors ---------------------------------------------------------------------------------------
for (const c of fx("anchors.json")) {
  const index = JSON.parse(readFileSync(join(root, "examples/sherlock-holmes", `${c.index}.json`), "utf8"));
  const fps = c.edition === "pg244" ? ed.blocks.map((b) => b.fp) : c.fps;
  const e = { blocks: fps.map((fp, seq) => ({ seq, fp })) };
  const resolved = resolveAnchors(index.anchors, e);
  const eff = effectiveLocations(index.anchors, resolved).map((x) => (x === Infinity ? null : x));
  const front = Object.fromEntries(Object.keys(c.frontier).map((s) => [s, frontier(index.anchors, resolved, Number(s))]));
  const unresolved = [...resolved.values()].filter((v) => v === null).length;
  check(`anchors ${c.index} vs ${c.edition} (${unresolved} unresolved)`,
    same(Object.fromEntries(resolved), c.resolved) && same(eff, c.effective) && same(front, c.frontier));
}

// (c) conformance cases through LorekeeperIndex.view -----------------------------------------------------
{
  const dir = join(root, "conformance");
  for (const name of readdirSync(dir).filter((f) => f.endsWith(".json")).sort()) {
    const path = join(dir, name);
    const c = JSON.parse(readFileSync(path, "utf8"));
    const load = (p) => JSON.parse(readFileSync(resolve(dirname(path), p), "utf8"));
    const indexes = c.indexes.map(load);
    const series = c.series ? load(c.series) : null;
    const cur = series ? indexes.find((ix) => ix.series.book === c.position.book) : indexes[0];
    // A stand-in edition with one block per anchor, so each anchor resolves to its own ordinal.
    const e = { blocks: cur.anchors.map((a, i) => ({ seq: i, fp: a.fp })), docs: [] };
    const lk = new LorekeeperIndex(cur, e, { series, order: c.order ?? null, finished: c.finished ?? null,
      others: indexes.filter((ix) => ix !== cur) });
    const ord = c.position.anchor === null ? -1 : cur.anchors.findIndex((a) => a.id === c.position.anchor);
    const view = lk.view({ block: ord }, { inclusive: true });
    let ok = same(view, c.expect) && lk.at({ block: ord }, { inclusive: true }) === c.position.anchor;
    if (ord + 1 < e.blocks.length) ok = ok && lk.at({ block: ord + 1 }) === c.position.anchor;   // finished mode
    check(`conformance ${name}`, ok);
  }
}

// (d) conformance/locators vectors (written by the Python reference; a case whose EPUB is absent is skipped) ----
{
  const dir = join(root, "conformance/locators");
  const editions = existsSync(dir) ? fx("editions.json") : {};
  const built = new Map();
  const editionOf = (name) => {
    if (!built.has(name)) {
      built.set(name, buildEdition(editions[name].map((d) => ({ spineChild: d.spineChild, linear: d.linear, document: parseXhtml(d.html) }))));
    }
    return built.get(name);
  };
  let ran = 0, skipped = 0;
  for (const name of existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith(".json")).sort() : []) {
    const c = JSON.parse(readFileSync(join(dir, name), "utf8"));
    if (!editions[c.edition]) { skipped++; continue; }
    const index = JSON.parse(readFileSync(join(root, c.index), "utf8"));
    const e = editionOf(c.edition);
    const lk = new LorekeeperIndex(index, e);
    let actual;
    try {
      const found = locate(e, c.locator);
      actual = { seq: found.seq, exact: found.exact, finished: lk.at(c.locator), inclusive: lk.at(c.locator, { inclusive: true }) };
    } catch (err) { actual = { error: `${err.name}: ${err.message}` }; }
    ran++;
    check(`locator vector ${name}`, same(actual, c.expect), `${JSON.stringify(actual)} != ${JSON.stringify(c.expect)}`);
  }
  console.log(`locator vectors: ${ran} run, ${skipped} skipped (edition not in this checkout)`);
}

// locate() on the real book: every block by CFI, XPointer, word offset and progress ----------------------------
{
  let cfiBad = 0, xpBad = 0, wordBad = 0, firstBad = null;
  const stepsName = (el) => el.localName.toLowerCase();
  edition.docs.forEach((entry) => {
    if (entry.spine === null) return;
    const kept = blocksFromDocument(entry.document);
    kept.forEach((b, k) => {
      const seq = entry.first + k;
      const cfi = `epubcfi(/6/${(entry.spineChild + 1) * 2}!/${b.elementPath.map((i) => i * 2).join("/")})`;
      let xp = `/body/DocFragment[${entry.spineChild + 1}]`, node = entry.document.documentElement;
      const names = [];
      for (const idx of b.elementPath) {
        const el = node.children[idx - 1];
        const sameName = node.children.filter((c) => stepsName(c) === stepsName(el));
        names.push(`${stepsName(el)}[${sameName.indexOf(el) + 1}]`);
        node = el;
      }
      xp += "/" + names.join("/") + "/text().0";
      const r1 = locate(edition, { cfi }).seq, r2 = locate(edition, { xpointer: xp }).seq;
      let w = 0;
      for (let s = entry.first; s < seq; s++) w += edition.blocks[s].words.length;
      const r3 = locate(edition, { spine: entry.spine, word: w }).seq;
      const r4 = locate(edition, { spine: entry.spine, word: w + b.words.length - 1 }).seq;
      if (r1 !== seq) { cfiBad++; firstBad ??= `cfi ${cfi} -> ${r1}, expected ${seq}`; }
      if (r2 !== seq) { xpBad++; firstBad ??= `xpointer ${xp} -> ${r2}, expected ${seq}`; }
      if (r3 !== seq || r4 !== seq) wordBad++;
    });
  });
  check(`pg244 every block: element CFI (${cfiBad} bad)`, cfiBad === 0, firstBad ?? "");
  check(`pg244 every block: XPointer (${xpBad} bad)`, xpBad === 0, firstBad ?? "");
  check(`pg244 every block: first and last word offset (${wordBad} bad)`, wordBad === 0);
  const docEntry = edition.docs[5];
  const total = edition.blocks.slice(docEntry.first, docEntry.first + docEntry.count).reduce((n, b) => n + b.words.length, 0);
  const prog = locate(edition, { spine: docEntry.spine, progress: 0.5 });
  let acc = 0, want = docEntry.first;
  for (let s = docEntry.first; s < docEntry.first + docEntry.count; s++) { acc += edition.blocks[s].words.length; if (acc > Math.floor(total / 2)) { want = s; break; } }
  check("pg244 progress 0.5 lands in the block holding the middle word", prog.seq === want && prog.exact === false);
}

// locate() edge cases on a hand-made edition --------------------------------------------------------
{
  const page = (head, body) => parseXhtml(`<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>${head}</title></head><body>${body}</body></html>`);
  const d0 = page("t t", `<h1 id="h">Title words here</h1><p id="a">Alpha beta <em>gamma</em> delta</p>`
    + `<div id="d"><p id="b">Epsilon zeta</p>loose text<p id="c">Eta theta</p></div>`
    + `<p id="e"> </p><p id="f"><img src="x.png"/></p><p id="g">Iota kappa</p>`);
  const d1 = page("n", `<p>Nonlinear text</p>`);
  const d2 = page("t t", `<p>Doc two para one</p><p>Doc two para two</p>`);
  const d3 = page("t", `<p> </p>`);
  const e = buildEdition([{ document: d0 }, { document: d1, linear: false }, { document: d2 }, { document: d3 }]);
  check("hand-made edition numbering", e.blocks.length === 7 && e.docs[1].spine === null && e.docs[2].first === 5 && e.docs[3].first === 7);
  const L = (loc) => { const r = locate(e, loc); return `${r.seq}${r.exact ? "" : "~"}`; };
  const cases = [
    [{ block: -1 }, "-1"], [{ block: 6 }, "6"],
    // CFI
    [{ cfi: "epubcfi(/6/2!/4/2/1:0)" }, "0"],
    [{ cfi: "epubcfi(/6/2!/4/4/1:3)" }, "1"],
    [{ cfi: "epubcfi(/6/2!/4/4/2/1:2)" }, "1"],
    [{ cfi: "epubcfi(/6/2!/4/4/3:1)" }, "1"],
    [{ cfi: "epubcfi(/6/2!/4/4/1:999)" }, "1"],
    [{ cfi: "epubcfi(/6/2[chap]!/4[body01]/4[a]/1:3[xyz;s=b])" }, "1"],
    [{ cfi: "epubcfi(/6/2!/4/6)" }, "2"],
    [{ cfi: "epubcfi(/6/2!/4/6/3:2)" }, "2"],
    [{ cfi: "epubcfi(/6/2!/4/6/4/1:0)" }, "3"],
    [{ cfi: "epubcfi(/6/2!/4/8)" }, "3"],
    [{ cfi: "epubcfi(/6/2!/4/10)" }, "3"],
    [{ cfi: "epubcfi(/6/2!/4/12/1:0)" }, "4"],
    [{ cfi: "epubcfi(/6/2!/4/98[g])" }, "4"],
    [{ cfi: "epubcfi(/6/2!/4/2!/4)" }, "0"],
    [{ cfi: "epubcfi(/6/2!/4,/4/1:0,/4/1:3)" }, "1"],
    [{ cfi: "epubcfi(/6/2!/4/40/1:0)" }, "-1~"],
    [{ cfi: "epubcfi(/6/6!/4/40)" }, "4~"],
    [{ cfi: "epubcfi(/6/2!/2/2/1:0)" }, "-1"],
    [{ cfi: "epubcfi(/6/6!/2/2/1:0)" }, "4"],
    [{ cfi: "epubcfi(/6/6!/4/4/1:0)" }, "6"],
    [{ cfi: "epubcfi(/6/8!/4/2/1:0)" }, "6"],
    // XPointer
    [{ xpointer: "/body/DocFragment[1]/body/p[1]/text().0" }, "1"],
    [{ xpointer: "/body/DocFragment[1]/body/h1/text().3" }, "0"],
    [{ xpointer: "/body/DocFragment[1]/body/div/p[2]/text().0" }, "3"],
    [{ xpointer: "/body/DocFragment[1]/body/div/text().0" }, "2"],
    [{ xpointer: "/body/DocFragment[1]/body/p[2]/text().0" }, "3"],
    [{ xpointer: "/body/DocFragment[1]/body/p[4]/text().0" }, "4"],
    [{ xpointer: "/body/DocFragment[1]/body/p[2]/em/text().0" }, "-1~"],
    [{ xpointer: "/body/DocFragment[1]/body/div" }, "2"],
    [{ xpointer: "/body/DocFragment[1]/body/p[9]/text().0" }, "-1~"],
    [{ xpointer: "/body/DocFragment[1]/body/p[0]" }, "-1~"],
    [{ xpointer: "/body/DocFragment[3]/body/p[2]/text().0" }, "6"],
    // spine + word / progress
    [{ spine: 0, word: 0 }, "0"], [{ spine: 0, word: 2 }, "0"], [{ spine: 0, word: 3 }, "1"],
    [{ spine: 0, word: 12 }, "4"], [{ spine: 0, word: 9999 }, "4~"],
    [{ spine: 1, word: 0 }, "5"], [{ spine: 1, word: 4 }, "6"],
    [{ spine: 2, word: 0 }, "6~"],
    [{ spine: 0, progress: 0 }, "0~"], [{ spine: 0, progress: 0.5 }, "1~"], [{ spine: 0, progress: 0.99 }, "4~"],
    [{ spine: 0, progress: 1 }, "4~"],
  ];
  for (const [loc, want] of cases) {
    let got;
    try { got = L(loc); } catch (err) { got = `${err.name}: ${err.message}`; }
    check(`locate ${JSON.stringify(loc)} -> ${want}`, got === want, `got ${got}`);
  }
  const errors = [
    { cfi: "epubcfi(/6/4!/4/2)" }, { cfi: "epubcfi(/6/100!/4/2)" }, { cfi: "epubcfi(/6/3!/4/2)" },
    { cfi: "epubcfi(/7/2!/4)" }, { cfi: "epubcfi(/6/2!/4/x)" }, { cfi: "not a cfi" },
    { xpointer: "/body/DocFragment[2]/body/p/text().0" }, { xpointer: "/body/DocFragment[9]/body/p/text().0" },
    { xpointer: "/body/DocFragment[0]/body" }, { xpointer: "/html/body" }, { xpointer: "/body/DocFragment/body/p" },
    { block: 99 }, { block: 1.5 }, { block: "3" }, { block: -2 },
    { spine: 5, word: 0 }, { spine: -1, word: 0 }, { spine: 0, word: -1 }, { spine: 0, word: 1.5 },
    { spine: 0, progress: 1.5 }, { spine: 0, progress: -0.1 }, { spine: 0, progress: "0.5" },
    { block: 1, spine: 0 }, { cfi: "epubcfi(/6/2!/4)", block: 1 }, {}, { spine: 0 }, { word: 3 }, null, "x", [],
  ];
  for (const loc of errors) check(`locate ${JSON.stringify(loc)} throws LocatorError`, throwsLocator(() => locate(e, loc)));
  const noDom = buildEdition([{ blocks: [{ words: ["a"] }] }, { blocks: [{ words: ["b", "c"] }] }]);
  const r = locate(noDom, { cfi: "epubcfi(/6/4!/4/2/1:0)" });
  check("CFI without a loaded document is approximate and under-reveals", r.seq === 0 && r.exact === false);
  check("position modes", position([{ id: "x", fp: "" }], new Map([["x", 5]]), 5) === null
    && position([{ id: "x", fp: "" }], new Map([["x", 5]]), 6) === "x"
    && position([{ id: "x", fp: "" }], new Map([["x", 5]]), 5, { inclusive: true }) === "x");
}

// lookup ----------------------------------------------------------------------------------------------
{
  const index = JSON.parse(readFileSync(join(root, "examples/sherlock-holmes/study-in-scarlet.json"), "utf8"));
  const lk = new LorekeeperIndex(index, edition);
  const end = { block: edition.blocks.length - 1 };
  const before = lk.lookup("Holmes", { block: -1 });
  const hits = lk.lookup("holmes", end);
  const byFull = lk.lookup("Sherlock Holmes", end);
  check("lookup hides everything before the first anchor", before.length === 0);
  check("lookup by unique surname, case-insensitive, with facets", hits.length === 1 && hits[0].entity.name.includes("Holmes")
    && Array.isArray(hits[0].facets) && Array.isArray(hits[0].relations) && Array.isArray(hits[0].events), JSON.stringify(hits.map((h) => h.entity.name)));
  check("lookup by full name agrees", byFull.length === 1 && byFull[0].entity.id === hits[0]?.entity.id);
  check("lookup of an unknown name is empty", lk.lookup("Nobody Atall", end).length === 0);
}

process.exit(failures);
