// Chapter numbers as a book states them: a faithful port of src/lorekeeper/chapters.py (keep the two in step; both run
// tests/fixtures/chapter_numbering.json, this one through tests/js/run_chapters.mjs). Dependency-free.
//
// One pure rule over a book's whole ordered list of chapter titles, never over the rows a view happens to list, so a
// prologue, a "Part I" divider or a chapter nobody reached never shifts the numbers after it.
// - classify(title): "chapter" | "divider" | "named" | "other"
// - ownNumber(title): the number a chapter title states, else null
// - numbering(entries): [{ kind, number }] per entry; number null means no "Chapter N" label

const UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];
const TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"];
const TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];
const ORD_UNITS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"];
const ORD_TEENS = ["tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
  "eighteenth", "nineteenth"];
const ORD_TENS = ["twentieth", "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth"];

const WORDS = new Map();
UNITS.forEach((w, i) => WORDS.set(w, i + 1));
TEENS.forEach((w, i) => WORDS.set(w, i + 10));
TENS.forEach((w, i) => WORDS.set(w, (i + 2) * 10));
const ORDINALS = new Map();
ORD_UNITS.forEach((w, i) => ORDINALS.set(w, i + 1));
ORD_TEENS.forEach((w, i) => ORDINALS.set(w, i + 10));
ORD_TENS.forEach((w, i) => ORDINALS.set(w, (i + 2) * 10));

const byLen = (xs) => [...xs].sort((a, b) => b.length - a.length);   // stable, like Python's sorted(key=len, reverse=True)
const TENS_RE = TENS.join("|");
const WORD_RE = `(?:(?:${TENS_RE})[\\s\\-‐–]+(?:${UNITS.join("|")})|${byLen(WORDS.keys()).join("|")})`;
const ORD_RE = `(?:(?:${TENS_RE})[\\s\\-‐–]+(?:${ORD_UNITS.join("|")})|${byLen(ORDINALS.keys()).join("|")})`;
const ROMAN_RE = "[ivxlc]+";
// Standing alone: end of the title, punctuation, or a dash (an en or em dash anywhere, a hyphen only after a space).
// Python's \s and $ (no MULTILINE) differ from JS only on titles with line breaks, which _tidy collapses first.
const SEP = "(?=\\s*(?:$|[.:,;)\\]–—])|\\s+[-‐])";
// After "Chapter" (or "Part", "Scene"...): never inside a word or before "-letter".
const END = "(?=$|[\\s.:,;)\\]–—]|[-‐](?![A-Za-z]))";
const ANY = `(?<t>\\d{1,4}|${ROMAN_RE}|${ORD_RE}|${WORD_RE})${END}`;
const CHAPTER = new RegExp(`^chapter\\s+(?:the\\s+)?${ANY}`, "i");
const CHAPTER_BARE = /^chapter\b/i;
const DIVIDER = new RegExp(`^(?:part|book|volume|vol\\.|section|act)\\s+(?:the\\s+)?${ANY}`, "i");
const SCENE = new RegExp(`^scene\\s+${ANY}`, "i");
const ALONE_DIGITS = new RegExp(`^(?<t>\\d{1,3})${SEP}`);
const ALONE_ROMAN = new RegExp(`^(?<t>[IVXLC]+)${SEP}`);
const ALONE_WORD = new RegExp(`^(?<t>${WORD_RE})${SEP}`, "i");
const NAMED = new RegExp(
  "^(?:the\\s+|an?\\s+)?(?:prologue|epilogue|interlude|intermission|introduction|preface|foreword|afterword|"
  + "prelude|postlude|postscript|coda|dramatis\\s+person(?:ae|æ)|cast\\s+of\\s+characters|list\\s+of\\s+characters|"
  + "characters$|principal\\s+characters|appendix|appendices|glossary|author[’']?s\\s+note|note\\s+on|"
  + "translator[’']?s\\s+note|acknowledge?ments?|dedication|epigraph|contents|table\\s+of\\s+contents|copyright|"
  + "colophon|title\\s+page|half\\s+title|cover$|about\\s+the\\s+(?:author|publisher)|also\\s+by|maps?$|index$|notes$|"
  + "endnotes|bibliography|further\\s+reading|reading\\s+group|discussion\\s+questions)"
  + "(?![A-Za-z0-9])", "i");

const ROMAN_VALUES = { i: 1, v: 5, x: 10, l: 50, c: 100, d: 500, m: 1000 };

function tidy(title) {
  const t = String(title || "").replace(/\s+/g, " ").trim();
  return t.replace(/^["'“‘«*_ ]+/, "").trim();
}

function toRoman(n) {
  let out = "";
  for (const [v, s] of [[1000, "m"], [900, "cm"], [500, "d"], [400, "cd"], [100, "c"], [90, "xc"], [50, "l"], [40, "xl"],
    [10, "x"], [9, "ix"], [5, "v"], [4, "iv"], [1, "i"]]) {
    while (n >= v) { out += s; n -= v; }
  }
  return out;
}

/** The value of a well-formed roman numeral ("XIV" 14), else null ("IIII", "VX", "IC"). */
export function roman(token) {
  const t = String(token).toLowerCase();
  if (!t || [...t].some((ch) => !(ch in ROMAN_VALUES))) return null;
  let total = 0;
  for (let i = 0; i < t.length; i++) {
    const v = ROMAN_VALUES[t[i]];
    total += i + 1 < t.length && ROMAN_VALUES[t[i + 1]] > v ? -v : v;
  }
  return total > 0 && total < 4000 && toRoman(total) === t ? total : null;
}

function value(token) {
  const t = String(token).trim().toLowerCase();
  if (/^\d+$/.test(t)) { const n = parseInt(t, 10); return n > 0 ? n : null; }
  const parts = t.split(/[\s\-‐–]+/).filter(Boolean);
  if (parts.length === 2 && TENS.includes(parts[0])) {
    const unit = WORDS.get(parts[1]) || ORDINALS.get(parts[1]);
    return unit && unit < 10 ? WORDS.get(parts[0]) + unit : null;
  }
  if (parts.length === 1) {
    if (WORDS.has(parts[0])) return WORDS.get(parts[0]);
    if (ORDINALS.has(parts[0])) return ORDINALS.get(parts[0]);
    return roman(parts[0]);
  }
  return null;
}

function isDivider(t) {
  const m = DIVIDER.exec(t);
  return Boolean(m) && value(m.groups.t) != null;   // "Part Mic" is no divider
}

/** The chapter number a title states: "Chapter III" 3, "Twenty-One" 21, "II" 2, "3 - Title" 3; null for "Prologue",
 *  "I went home", "Part Two" or "The Company". */
export function ownNumber(title) {
  const t = tidy(title);
  if (isDivider(t) || NAMED.test(t) || SCENE.test(t)) return null;
  const m = CHAPTER.exec(t);
  if (m) return value(m.groups.t);
  for (const rx of [ALONE_DIGITS, ALONE_ROMAN, ALONE_WORD]) {
    const a = rx.exec(t);
    if (a) return value(a.groups.t);
  }
  return null;
}

/** "chapter", "divider", "named" or "other". */
export function classify(title) {
  const t = tidy(title);
  if (isDivider(t)) return "divider";
  if (NAMED.test(t) || SCENE.test(t)) return "named";
  if (CHAPTER_BARE.test(t) || ownNumber(t) != null) return "chapter";
  return "other";
}

/**
 * { kind, number } for each entry of a book's table of contents, in order. An entry is a title or { title, story }
 * (story false for front and back matter; absent or null counts as story). Pass the whole list, never only the rows a
 * view lists: in a book whose chapters are only titled, the count depends on every entry before.
 */
export function numbering(entries) {
  const rows = (entries || []).map((e) => (e && typeof e === "object" ? e : { title: e }));
  const kinds = rows.map((r) => classify(r.title || ""));
  const own = rows.map((r) => ownNumber(r.title || ""));
  const counted = [];
  rows.forEach((r, i) => { if (r.story !== false && (kinds[i] === "chapter" || kinds[i] === "other")) counted.push(i); });
  const stated = counted.filter((i) => own[i] != null).length;
  const byOwn = stated * 2 > counted.length;
  const out = kinds.map((kind) => ({ kind, number: null }));
  counted.forEach((i, k) => { out[i].number = own[i] != null ? own[i] : byOwn ? null : k + 1; });
  return out;
}

/** "Chapter 3", or "" for no number. */
export const label = (number) => (number != null ? `Chapter ${number}` : "");

/**
 * The Reader's chapter map numbered by chapter start: the server's `number` on each chapter (null for none), else, from
 * an older server, `numbering` over the whole map. Map(start → number | null).
 * @param chapters [{ title, start, story, number? }], the whole map in book order
 */
export function numbersByStart(chapters) {
  chapters = chapters || [];
  const worked = chapters.some((c) => c.number === undefined) ? numbering(chapters.map((c) => ({ title: c.title || "", story: c.story }))) : null;
  return new Map(chapters.map((c, i) => [c.start, worked ? worked[i].number : Number.isInteger(c.number) && c.number > 0 ? c.number : null]));
}

/**
 * A book's chapter numbers from its index's chapter recaps, for when there is no chapter map (an earlier series book, or
 * no book file): every chapter recap of the index in book order (by its `from` anchor's ordinal), consecutive recaps
 * with one title joined as parts of one chapter, numbered as a table of contents. Never only the rows a view lists.
 * Map(recap id → number | null).
 * @param doc an index document ({ anchors, recaps })
 */
export function recapNumbers(doc) {
  const ord = new Map((doc?.anchors || []).map((a, i) => [a.id, i]));
  const recaps = (doc?.recaps || []).filter((r) => r && r.scope === "chapter")
    .map((r, i) => ({ r, i, o: ord.get(r.from) ?? Infinity })).sort((a, b) => a.o - b.o || a.i - b.i).map((x) => x.r);
  const groups = [];
  for (const r of recaps) {
    const last = groups.at(-1);
    const title = typeof r.title === "string" ? r.title : "";
    if (last && last.title === title) last.ids.push(r.id); else groups.push({ title, ids: [r.id] });
  }
  const nums = numbering(groups.map((g) => g.title));
  const out = new Map();
  groups.forEach((g, i) => g.ids.forEach((id) => out.set(id, nums[i].number)));
  return out;
}

// ---- not part of the chapters.py port: where a chapter recap sits in the Reader's chapter map ----------------------

const ANCHOR_WORDS = 6;   // SPEC §3.5: a block of fewer words never carries an anchor
const sameTitle = (t) => String(t || "").replace(/\s+/g, " ").trim().toLowerCase();

/** The anchor's chapter `i`, the real chapter before it `j` (-1 for none) and whether the anchor was snapped past `j`'s
 *  end (recapChapter's rules 1 and 2), or null when the anchor is before the first chapter or unknown. */
function recapWindow(chapters, seq, blockWords) {
  if (!chapters?.length || !Number.isInteger(seq) || seq < 0) return null;
  let i = -1;
  for (let k = 0; k < chapters.length && chapters[k].start <= seq; k++) i = k;
  if (i < 0) return null;
  const words = blockWords || null;
  const eligible = (a, b) => { for (let k = a; k < b; k++) if ((words[k] || 0) >= ANCHOR_WORDS) return true; return false; };
  let j = i - 1;
  if (words) while (j >= 0 && !eligible(chapters[j].start, chapters[j + 1].start)) j--;
  // Snapped: nothing in the anchor's chapter before the anchor could have carried it (with a known chapter to go back to).
  const snapped = Boolean(words) && j >= 0 && !eligible(chapters[i].start, seq);
  return { i, j, snapped };
}

/**
 * The map chapter a chapter recap of the book being read summarises. A recap's `from` is the first anchor-eligible block
 * after its chapter's last paragraph, and a chapter usually opens with a short heading, so the anchor often lands in the
 * next chapter's second block; the chapter it is in is then the wrong row.
 * 1. A chapter whose title is the recap's, at the anchor's chapter or back to the chapter before it (the last one
 *    before it holding a block that can carry an anchor; chapters made only of short blocks in between, a "Part Two"
 *    divider, are passed over as the snap would pass over them). Never later than the anchor's chapter.
 * 2. Else (a hand-written or retitled recap), or when both match: the chapter before when no block between the
 *    anchor's chapter's start and the anchor could carry an anchor (the anchor was snapped past a chapter end),
 *    else the anchor's chapter.
 * Only for recaps: an event in a chapter's first long paragraph belongs to that chapter.
 * @param chapters the whole map in book order, [{ title, start }]
 * @param seq the block the recap's anchor is in (its effective location)
 * @param title the recap's title
 * @param blockWords words per block of the edition (the map's), or null when unknown (then rule 2 never goes back)
 * @param tidy the title tidier the Reader shows titles with
 * @returns a chapter of `chapters`, or null when the anchor is before the first one or unknown
 */
export function recapChapter(chapters, seq, title, blockWords, tidy = (t) => t) {
  const w = recapWindow(chapters, seq, blockWords);
  if (!w) return null;
  const { i, j, snapped } = w;
  const key = sameTitle(tidy(title));
  const matches = [];
  if (key) for (let k = i; k >= Math.max(j, 0); k--) if (sameTitle(tidy(chapters[k].title)) === key) matches.push(k);
  if (matches.length === 1) return chapters[matches[0]];
  if (matches.length > 1) return chapters[matches[0] === i && !snapped ? i : matches.find((k) => k !== i)];
  return chapters[snapped ? j : i];
}

/**
 * The chapter keys the picture pipeline files plates under (art.py `_chapters`): a map chapter is listed when an event's
 * anchor is in its blocks or one of its own chapter recaps (by recapChapter) is; a listed title is its key the first
 * time and "<title> (2)", "(3)" for the next listed chapters with the same title.
 * @param chapters the whole map in book order, [{ title, start }]
 * @param eventSeqs the blocks the events' anchors are in
 * @param recaps the chapter recaps, [{ seq, title }] (seq the block of the recap's anchor)
 * @param blockWords words per block, or null when unknown
 * @returns Map(chapter start → key)
 */
export function artChapterKeys(chapters, eventSeqs, recaps, blockWords) {
  const keys = new Map();
  if (!chapters?.length) return keys;
  const index = (seq) => { let i = -1; for (let k = 0; k < chapters.length && chapters[k].start <= seq; k++) i = k; return i; };
  const used = new Set();
  for (const q of eventSeqs || []) if (Number.isInteger(q) && q >= 0) { const i = index(q); if (i >= 0) used.add(i); }
  for (const r of recaps || []) {
    const ch = recapChapter(chapters, r.seq, r.title, blockWords);
    if (ch) used.add(chapters.indexOf(ch));
  }
  const seen = new Map();
  chapters.forEach((c, i) => {
    if (!used.has(i)) return;
    const n = (seen.get(c.title) || 0) + 1;
    seen.set(c.title, n);
    keys.set(c.start, n === 1 ? c.title : `${c.title} (${n})`);
  });
  return keys;
}

/**
 * The map chapter a chapter plate belongs to. A plate is anchored at its chapter's last anchor, which is its own recap's
 * when that comes last, and a recap's anchor is often the next chapter's first long paragraph; so a plate is placed as a
 * recap is (recapChapter), by its `target`, the chapter key the pipeline filed it under.
 * 1. A chapter whose key (`keys`, from artChapterKeys) is the target, at the anchor's chapter or back to the chapter
 *    before it: this tells two chapters of one title apart ("Interlude (2)").
 * 2. Else recapChapter with the target as the title, a " (N)" suffix taken off.
 * Never later than the anchor's chapter, never before the chapter before it.
 * @returns a chapter of `chapters`, or null when the anchor is before the first one or unknown
 */
export function plateChapter(chapters, seq, target, blockWords, keys = null) {
  const w = recapWindow(chapters, seq, blockWords);
  if (!w) return null;
  const t = String(target ?? "");
  if (keys && t) for (let k = w.i; k >= Math.max(w.j, 0); k--) if (keys.get(chapters[k].start) === t) return chapters[k];
  return recapChapter(chapters, seq, t.replace(/\s+\(\d+\)$/, ""), blockWords);
}
