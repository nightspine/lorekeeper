// Duplicate entities in the Reader: review suggested pairs, or merge two entries by hand.
//
// Everything shown comes from the gated view at the reader's position: a pair is only offered once
// both entries are revealed, and only facts revealed so far are shown to help decide.
import { api } from "./api.js";
import { modal } from "./build.js";
import { clear, describe, h, icon, TYPE_LABEL, toast, typeTag } from "./dom.js";

const REASON = { "same name": "Same name", "shared alias": "Shared name", "fuller name": "Fuller name",
  "described alike": "Described alike" };

/** The pairs whose two entries are both visible in `view`. */
export function visiblePairs(dupes, view) {
  const ids = new Set(view.entities.map((e) => e.id));
  return (dupes?.pairs || []).filter((p) => ids.has(p.keep) && ids.has(p.drop));
}

/** Pairs worth raising: everything except what a model judged to be two different things. */
export const openPairs = (pairs) => pairs.filter((p) => p.verdict !== "different");

function entitySummary(e) {
  const facts = e.facets.slice(-2).filter((f) => f.text !== describe(e));
  return h(`div.dup-side.t-${e.type}`,
    typeTag(e.type),
    h("div.dup-name", e.name),
    e.aliases.length ? h("div.hint", `also ${e.aliases.slice(0, 4).join(" · ")}`) : null,
    e.profile ? h("p.dup-profile", e.profile.text) : null,
    facts.length ? h("ul.dup-facts", facts.map((f) => h("li", f.text))) : h("p.hint", "Nothing known beyond the name yet."));
}

/**
 * Choose which name the merged entry shows. Offers the best-ranked names (server ranking: clean,
 * plain, most mentioned, fullest) among those the reader has already seen, best first.
 * Returns { el, name() }.
 */
function nameChoice(keep, drop, group, ranked = []) {
  const seen = [keep.name, ...keep.aliases, drop.name, ...drop.aliases];
  const lower = new Set(seen.map((n) => n.toLowerCase()));
  const clean = (n) => !/[()]/.test(n);
  const fallback = [keep.name, drop.name].sort((a, b) => clean(b) - clean(a));
  const options = [...new Set([...ranked.filter((n) => lower.has(n.toLowerCase())), ...fallback])].slice(0, 3);
  let choice = options[0];
  const opt = (n, i) => {
    const input = h("input", { type: "radio", name: group, value: n, checked: i === 0 });
    input.addEventListener("change", () => { choice = n; });
    return h("label.dup-opt", input, h("span", "Show as ", h("b", n)));
  };
  return { el: h("div.dup-choose", { role: "radiogroup", "aria-label": "Name to show" }, options.map(opt)), name: () => choice };
}

/**
 * Review dialog for suggested pairs. ctx: { key, view, dupes, onChanged() }.
 * On open, pairs no model has judged yet are checked in the background (Claude Haiku or Ollama);
 * verdicts sort pairs into likely, to check, and ones the text could not settle; pairs judged different are not listed. Merges apply to every index of
 * the series at once; the latest one can be undone from here.
 */
export function reviewDuplicates(ctx) {
  const byId = new Map(ctx.view.entities.map((e) => [e.id, e]));
  let dupes = ctx.dupes;
  let pairs = visiblePairs(dupes, ctx.view);
  let changed = false;
  let poll = null;
  const m = modal(`Possible duplicates (${openPairs(pairs).length})`, { width: 760 });
  const close = m.close;
  m.close = () => { clearTimeout(poll); close(); ctx.onChanged(changed); };
  const checking = h("div", { role: "status" });
  const status = h("p.hint", { role: "status" });
  const sections = h("div.dup-sections");
  const bulk = h("button.btn.primary", { type: "button" }, icon("layers"));

  async function refresh() {
    try { dupes = await api.duplicates(ctx.key); } catch { return; }
    pairs = visiblePairs(dupes, ctx.view);
    paint();
    if (dupes.checking) poll = setTimeout(refresh, 2000);
  }

  async function startCheck() {
    try { await api.checkDuplicates(ctx.key); } catch (e) { toast(e.message, "error"); return; }
    refresh();
  }

  async function run(batch, label) {
    try {
      const r = await api.merge(ctx.key, batch.map((p) => ({ keep: p.keep, drop: p.drop, name: p.name || null })));
      changed = true;
      const gone = new Set(batch.map((b) => b.drop));
      pairs = pairs.filter((p) => !gone.has(p.keep) && !gone.has(p.drop));
      const undo = h("button.linkish", { type: "button", on: { click: async () => {
        try { await api.undoMerge(ctx.key, r.undo); toast("Merge undone"); m.close(); }
        catch (e) { toast(e.message, "error"); }
      } } }, "Undo");
      clear(status, `${label} `, undo);
      paint();
    } catch (e) { toast(e.message, "error"); }
  }

  function row(p, i) {
    const keep = byId.get(p.keep), drop = byId.get(p.drop);
    if (!keep || !drop) return null;
    const choose = nameChoice(keep, drop, `dup-${p.keep}-${p.drop}`, p.ranked);
    const label = p.verdict === "same" || p.sure ? ["dup-sure", "Likely"]
      : p.verdict === "unsure" ? ["dup-check", "Couldn't tell"] : ["dup-check", "Check"];
    return h("li.dup-row",
      h("div.dup-head", h("b", keep.name), h("span.dup-arrow", { "aria-hidden": "true" }, "↔"), h("b", drop.name),
        h("span.chip", REASON[p.reason] || p.reason), h(`span.chip.${label[0]}`, label[1])),
      h("div.dup-sides", entitySummary(keep), entitySummary(drop)),
      h("div.dup-actions", choose.el,
        h("span.g-spacer"),
        h("button.btn.ghost", { type: "button", on: { click: async () => {
          try {
            await api.markDistinct(ctx.key, p.keep, p.drop);
            pairs = pairs.filter((x) => x !== p);
            clear(status, `Kept ${keep.name} and ${drop.name} apart. They won't be suggested again.`);
            paint();
          } catch (e) { toast(e.message, "error"); }
        } } }, "Not the same"),
        h("button.btn.primary", { type: "button", on: { click: () => run([{ ...p, name: choose.name() }], `Merged ${drop.name} into ${keep.name}.`) } },
          "Merge")));
  }

  function paint() {
    const likely = pairs.filter((p) => p.sure);
    const unsure = pairs.filter((p) => !p.sure && p.verdict !== "different");
    const unknown = unsure.filter((p) => p.verdict === "unsure");   // checked against the text, still undecided
    const toCheck = unsure.filter((p) => p.verdict !== "unsure");
    document.getElementById("modal-title").textContent = `Possible duplicates (${likely.length + unsure.length})`;
    bulk.style.display = likely.length ? "" : "none";   // [hidden] loses to .btn's display
    bulk.replaceChildren(icon("layers"), likely.length === 1 ? "Merge the likely one" : `Merge the ${likely.length} likely ones`);
    const unjudged = pairs.filter((p) => !p.verdict).length;
    clear(checking,
      dupes.checking ? h("div.notice.info.dup-checking", h("span.spinner", { "aria-hidden": "true" }),
        h("div", `Checking ${unjudged} pair${unjudged === 1 ? "" : "s"} with ${dupes.checking}, using what both entries say…`))
        : dupes.checkError ? h("div.notice.warn", icon("alert"), h("div", h("b", "The check didn't finish. "), dupes.checkError, " ",
          h("button.linkish", { type: "button", on: { click: startCheck } }, "Try again")))
          : unjudged && !dupes.checker ? h("p.hint", "Add a Claude key in Settings, or start Ollama, to have each pair checked against what both entries say.")
            : null);
    const sec = (title, list, hint) => (list.length ? h("section.dup-section", h("h3", title, h("span.n", String(list.length))),
      hint ? h("p.hint", hint) : null, h("ol.dup-list", list.map(row))) : null);
    clear(sections,
      !likely.length && !unsure.length ? h("p.dup-done", icon("check-circle"), h("span", "Nothing left to review.")) : null,
      sec("Likely the same", likely, null),
      sec("To check", toCheck, "The names fit, but the entries alone don't settle it."),
      sec("Couldn't tell from the text", unknown, "A model read both entries and the book's own text and still couldn't say. You decide."));
  }

  bulk.addEventListener("click", () => {
    const likely = pairs.filter((p) => p.sure);
    run(likely.map((p) => ({ ...p, name: null })), `Merged ${likely.length} pair${likely.length === 1 ? "" : "s"}.`);   // names chosen by ranking
  });
  clear(m.body,
    h("p.muted", "These entries look like the same person or thing. Merging combines their names and facts across the whole series. Each fact still appears only from where the book reveals it."),
    checking,
    h("div.toolbar", bulk, status),
    sections,
    h("div.toolbar", { style: { justifyContent: "flex-end" } }, h("button.btn", { type: "button", on: { click: m.close } }, "Done")));
  paint();
  if (dupes.checking) refresh();
  else if (dupes.unjudged && dupes.checker) startCheck();
}

/**
 * Merge one entry into another the reader picks: for pairs the suggestions miss ("Bob" and "Robert").
 * ctx: { key, view, id, onChanged() }.
 */
export function mergeWith(ctx) {
  const me = ctx.view.entities.find((e) => e.id === ctx.id);
  const m = modal(`Merge “${me.name}” with…`, { width: 640 });
  const search = h("input.input", { type: "search", placeholder: "Find the other entry", "aria-label": "Find the other entry" });
  const results = h("ul.dup-pick");
  const others = ctx.view.entities.filter((e) => e.id !== me.id);
  function list() {
    const q = search.value.trim().toLowerCase();
    const found = others.filter((e) => !q || [e.name, ...e.aliases].some((n) => n.toLowerCase().includes(q)))
      .sort((a, b) => (b.type === me.type) - (a.type === me.type)).slice(0, 12);
    clear(results, found.length ? found.map((e) => h("li", h("button", { type: "button", on: { click: () => confirm(e) } },
      h(`span.g-swatch.t-${e.type}`), h("span", e.name), h("span.hint", TYPE_LABEL[e.type])))) : h("li.hint", "No match among what you've read so far."));
  }
  function confirm(other) {
    // The entry introduced first keeps its id; the view lists entities in order of first appearance.
    const order = ctx.view.entities.map((e) => e.id);
    const [keep, drop] = order.indexOf(me.id) < order.indexOf(other.id) ? [me, other] : [other, me];
    const choose = nameChoice(keep, drop, "manual");
    clear(m.body,
      h("div.dup-sides", entitySummary(keep), entitySummary(drop)),
      h("div.notice.warn", icon("alert"), h("div", h("b", "Only merge if the book has already made clear these are the same. "),
        "If it's a twist that comes later, merging would show one's facts under the other's name before the reveal.")),
      h("div.dup-actions", choose.el, h("span.g-spacer"),
        h("button.btn", { type: "button", on: { click: () => { clear(m.body, search, results); list(); search.focus(); } } }, "Back"),
        h("button.btn.primary", { type: "button", on: { click: async () => {
          try {
            await api.merge(ctx.key, [{ keep: keep.id, drop: drop.id, name: choose.name() }]);
            m.close();
            toast(`Merged ${drop.name} into ${keep.name}`);
            ctx.onChanged();
          } catch (e) { toast(e.message, "error"); }
        } } }, "Merge")));
  }
  search.addEventListener("input", list);
  clear(m.body, search, results);
  list();
}
