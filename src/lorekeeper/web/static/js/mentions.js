// "Where it comes up": the entry drawer's chart of mentions per chapter, gated at the reader's position.
// The server counts (names visible at the position only, chapters up to the current one, the current one
// through the position); this file only draws what comes back. Text goes through textContent (h()).
import { api } from "./api.js";
import { clear, h, icon, tidyTitle } from "./dom.js";

const CHART_H = 96;   // px; keep in step with .mchart in reader.css
let latest = 0;       // the newest section; an answer for an older one is dropped
let pending = null;   // timers of the section being replaced
let memo = null;      // { sig, data } so a repaint at the same position doesn't ask again

const remembered = () => { try { return localStorage.getItem("lk-passages") === "1"; } catch { return false; } };
const remember = (on) => { try { localStorage.setItem("lk-passages", on ? "1" : "0"); } catch { /* private mode */ } };

const plural = (n, one, many) => (n === 1 ? one : many);
/** "Chapter 5, Letters, Lucy and Mina": the book's own heading when it already says what it is. */
function chapterName(c) {
  const t = tidyTitle(c.title || "");
  if (/^(chapter|part|book|prologue|epilogue|preface|introduction)\b/i.test(t)) return t;
  return t ? `Chapter ${c.n}, ${t}` : `Chapter ${c.n}`;
}

/** Axis numbers: the first chapter, a few evenly spaced, the last. */
export function axisNumbers(total) {
  if (total <= 6) return Array.from({ length: total }, (_, i) => i + 1);
  const out = new Set([1, total]);
  const steps = total >= 20 ? 3 : 2;
  for (let i = 1; i < steps; i++) out.add(Math.round(1 + ((total - 1) * i) / steps));
  return [...out].sort((a, b) => a - b);
}

function chart(data) {
  const total = Math.max(data.total || 0, data.chapters.length);
  const placed = Boolean(data.current);   // null: the position is in a non-story section; only the listed chapters are drawn
  const here = placed ? data.current.n : Infinity;
  const counts = new Map(data.chapters.map((c) => [c.n, c.count]));
  if (placed) counts.set(here, data.current.count ?? 0);
  const max = Math.max(1, ...counts.values());
  const barH = (n) => `${Math.max(2, Math.round((n / max) * (CHART_H - 12)))}px`;
  const cols = [];
  for (let c = 1; c <= total; c++) {
    let inner;
    if (c < here && (placed || counts.has(c))) inner = counts.get(c) ? h("div.m-bar", { style: { height: barH(counts.get(c)) } }) : h("div.m-bar.m-none");
    else if (!placed) inner = h("div.m-dot");
    else if (c === here) inner = h("div.m-track", h("div.m-bar", { style: { height: counts.get(c) ? barH(counts.get(c)) : "0px" } }));
    else inner = h("div.m-dot");
    cols.push(h("div.m-col", c === here ? { "aria-current": "true" } : {}, inner));
  }
  const at = (c) => ((c - 0.5) / total) * 100;
  const nums = axisNumbers(total);
  const axis = h("div.maxis", nums.map((c) => h(`span${c === 1 ? ".first" : c === total ? ".last" : ""}`,
    { style: { left: `${c === 1 ? 0 : c === total ? 100 : at(c)}%` } }, String(c))));
  const x = placed ? at(here) : 0;
  const you = !placed ? null : h("div.mhere", h(`span${x < 9 ? ".start" : x > 91 ? ".end" : ""}`, { style: { left: `${x}%` } }, "You're here"));
  const label = placed ? `Mentions per chapter. ${counts.get(here)} so far in chapter ${here}.` : "Mentions per chapter.";
  const mchart = h("div.mchart", cols);
  mchart.style.setProperty("--cols", String(total));
  return h("div", { role: "img", "aria-label": label }, mchart, axis, you);
}

function passages(list) {
  if (!list?.length) return null;
  const items = list.map((sn) => {
    const t = sn.text || "";
    const a = Math.max(0, Math.min(t.length, sn.start ?? 0)), b = Math.max(a, Math.min(t.length, sn.end ?? a));
    // start/end are UTF-16 offsets (what String.slice uses); para counts within the chapter.
    return h("li", h("span", t.slice(0, a), b > a ? h("b", t.slice(a, b)) : null, t.slice(b)), h("span.para", `Paragraph ${sn.para ?? sn.seq}`));
  });
  const det = h("details.passages", h("summary", icon("chevron"), "Passages"), h("ul.snips", items));
  det.open = remembered();
  det.addEventListener("toggle", () => remember(det.open));
  return det;
}

function rows(data) {
  const row = (label, c, extra) => h("div", h("dt", label), h("dd", chapterName(c), extra ? h("small", extra) : null));
  return h("dl.m-kv",
    data.first ? row("First seen", data.first) : null,
    data.busiest ? row("Busiest", data.busiest, `${data.busiest.count} ${plural(data.busiest.count, "mention", "mentions")}`) : null,
    data.latest ? row("Latest", data.latest) : null);
}

/** True when there is something to show: at least one mention up to the position. */
export const hasMentions = (data) => Boolean(data && data.chapters?.length && (data.first || data.current?.count));

/**
 * The section for one entry at block `seq` (the last finished block). It starts hidden and appears only when the
 * server has counts (a 404 without the EPUB, or no mention yet, leaves nothing, not an empty chart).
 * `immediate` skips the 300 ms debounce used when the position is moving.
 */
export function mentionsSection(key, id, seq, { immediate = false, order = null, fin = null } = {}) {
  const box = h("div.d-sec.mentions", { hidden: true, dataset: { jump: "mentions" } });
  if (pending) pending.forEach(clearTimeout);
  pending = [];
  const mine = ++latest;
  const sig = `${key}\n${id}\n${seq}\n${order ?? ""}\n${fin ?? "-"}`;
  const paint = (data) => {
    if (mine !== latest || !hasMentions(data)) { if (mine === latest) box.hidden = true; return; }
    const n = data.current?.count ?? 0;
    clear(box, h("span.eyebrow", "Where it comes up"), chart(data),
      !data.current ? null : h("p.msent", n === 0 ? "Not mentioned in this chapter yet." : n === 1 ? "Mentioned once in this chapter so far." : `Mentioned ${n} times in this chapter so far.`),
      passages(data.current?.snippets), rows(data));
    box.hidden = false;
  };
  if (memo?.sig === sig) { paint(memo.data); return box; }
  if (seq == null || seq < 0) return box;   // nothing has been read yet: nothing to count
  const run = async () => {
    pending.push(setTimeout(() => {   // a slow answer shows one quiet line
      if (mine === latest && box.hidden) { clear(box, h("span.eyebrow", "Where it comes up"), h("p.msent.muted", "Counting mentions…")); box.hidden = false; }
    }, 300));
    let data = null;
    try { data = await api.mentions(key, id, seq, { order, fin }); } catch { data = null; }
    if (mine !== latest) return;
    memo = { sig, data };
    paint(data);
  };
  if (immediate) run(); else pending.push(setTimeout(run, 300));
  return box;
}
