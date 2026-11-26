import { api } from "./api.js";
import { onOllama, parseHash, replaceHash, state as appState } from "./app.js";
import { announce, clear, crumbs, fmtDuration, fmtInt, foldList, h, icon, loadError, loading, relTime, STATUS_LABEL, toast, typeTag } from "./dom.js";
import { confirmDialog, cover, partLine } from "./build.js";
import { openShare, readyMade } from "./community.js";
import { registerProvider } from "./palette.js";
import { picturesForm, styleLabel } from "./art.js";

const PHASES = [
  { key: "extract", label: "Reading", icon: "book-open" },
  { key: "rephrase", label: "Rephrasing", icon: "pen" },
  { key: "verify", label: "Fact-checking", icon: "shield" },
  { key: "recap", label: "Writing recap", icon: "feather" },
  { key: "portraits", label: "Writing descriptions", icon: "person" },
  { key: "arcs", label: "Writing book arcs", icon: "layers" },
];
// The pictures stage shows only for a run that draws them; an "Add pictures" run (kind "art") has no other stage.
const ART_PHASE = { key: "art", label: "Drawing pictures", icon: "sparkle" };
/** An "Add pictures" run: no reading, only the pictures stage, on an index that already exists (`index` is its library key). */
const isArtJob = (job) => job.kind === "art" || job.config?.kind === "art";
const PICTURE_PROVIDER = { gemini: "Gemini", openai: "OpenAI" };
/** "Ink, draft, drawn by Gemini" from a run's `art` config. */
const artLine = (job) => {
  const a = job.config?.art || {};
  const prov = PICTURE_PROVIDER[a.provider || job.config?.image_provider] || a.provider || job.config?.image_provider;
  return [styleLabel(a.style || ""), a.quality, prov ? `drawn by ${prov}` : null].filter(Boolean).join(", ");
};
const phasesOf = (job) => (isArtJob(job) ? [ART_PHASE] : job.config?.art ? [...PHASES, ART_PHASE] : PHASES);
const DROP_COLORS = ["var(--t-person)", "var(--t-group)", "var(--t-concept)", "var(--t-object)", "var(--t-term)",
  "var(--t-place)", "var(--rose)", "var(--text-3)"];
const statusBadge = (s) => h(`span.badge.${s}`, STATUS_LABEL[s] || s);
// Provider id → label, fetched once, for the run's chip ("gemini-2.5-flash on Gemini").
let providerLabels = null;
const loadProviderLabels = () => (providerLabels ||= api.providers().then((r) => Object.fromEntries(r.providers.map((p) => [p.id, p.label]))).catch(() => ({})));
export const libraryKey = (job) => (isArtJob(job) && (job.index || job.config?.index_key) ? job.index || job.config.index_key
  : "generated--" + String(job.output_name || "").replace(/\.json$/, ""));

// ---- the build form ---------------------------------------------------------------------------
// A short form on its own page: #/build/<localBookId>?catalog=<catalogKey>&from=<jobId>.

export function mountBuild(root) {
  const { parts, params } = parseHash();
  const ctx = { catalog: params.get("catalog"), cleanup: [] };
  const page = h("div.build");
  root.append(h("div.build-page", page));
  let gone = false;
  const retry = () => { clear(page, loading("Loading the build form…")); start(); };
  clear(page, loading("Loading the build form…"));
  start();
  async function start() {
    let books;
    try { books = await api.books(); } catch (e) { if (!gone) clear(page, loadError("load the build form", e, retry)); return; }
    const from = params.get("from") ? await api.job(params.get("from")).catch(() => null) : null;
    if (gone) return;
    const book = books.find((b) => b.id === parts[0]);
    if (!book) {
      return clear(page, h("p.muted", "That book is no longer in your uploads. Find it in the ", h("a", { href: "#/library" }, "Library"), "."));
    }
    configure(page, book, ctx, from && from.book_id === book.id ? from : null);
  }
  return () => { gone = true; ctx.cleanup.forEach((fn) => fn()); };
}

function configure(target, book, ctx, fromJob = null) {
  const t = fromJob?.config || {};
  const picked = new Set(Array.isArray(t.chapters) && t.chapters.length ? t.chapters
    : book.chapters.filter((c) => c.story).map((c) => c.n));
  // A cloud model reads about a chapter per call; a local model keeps a chunk its context allows.
  const defaultChunk = (pv) => (pv === "ollama" ? 2500 : 8000);
  const cfg = { provider: "ollama", model: null, check_model: null, chunk_words: null, chunk_mode: "words", depth: "standard", events: true, verify: true,
    series_id: "", book_key: "", license: "CC0-1.0", num_ctx: 16384, book_kind: "auto" };
  for (const k of Object.keys(cfg)) if (t[k] !== undefined && t[k] !== null) cfg[k] = t[k];
  // A run from before the fact-check model and smaller chunks re-runs with today's defaults.
  const legacy = Boolean(fromJob) && !("check_model" in t);
  let chunkChosen = Boolean(fromJob) && cfg.chunk_words >= 1000 && !legacy;   // a size the user set stays
  if (!chunkChosen) cfg.chunk_words = defaultChunk(cfg.provider);
  // Whole chapters are the default for a cloud model, off for Ollama; a rerun keeps what its run had.
  const defaultMode = (pv) => (pv === "ollama" ? "words" : "chapter");
  let modeChosen = Boolean(fromJob);
  if (!modeChosen) cfg.chunk_mode = defaultMode(cfg.provider);
  let replaceOld = Boolean(fromJob);
  const maxWords = Math.max(1, ...book.chapters.map((c) => c.words));
  const catalogKey = ctx.catalog || fromJob?.config.catalog_key || null;

  let pics = null;   // the Pictures choices (below); the estimate follows the chosen sections
  const summary = h("span.sec-summary");
  const optHint = h("span.sum-hint");
  const storySet = new Set(book.chapters.filter((c) => c.story).map((c) => c.n));
  const rows = book.chapters.map((c) => {
    const box = h("input", { type: "checkbox", checked: picked.has(c.n), "aria-label": `Include ${c.title}` });
    const row = h("label.chapter-row", { class: picked.has(c.n) ? "" : "off" },
      box, h("span.ch-title", { title: c.title }, h("span.subtle.num", `${String(c.n).padStart(2, "0")}  `), c.title),
      h("span.wordbar", h("i", h("b", { style: { width: `${Math.max(2, (c.words / maxWords) * 100)}%` } })), fmtInt(c.words)));
    box.addEventListener("change", () => { box.checked ? picked.add(c.n) : picked.delete(c.n); row.classList.toggle("off", !box.checked); update(); pics?.refresh(); });
    return { row, box, c };
  });
  const setAll = (fn) => () => { rows.forEach((r) => { r.box.checked = fn(r.c); r.box.dispatchEvent(new Event("change")); }); };

  const modelSel = h("select.select", { id: "cfg-model" });
  const checkSel = h("select.select", { id: "cfg-check" });
  checkSel.addEventListener("change", () => { cfg.check_model = checkSel.value || null; update(); });
  const checkField = h("div.field", { hidden: true }, h("label", { for: "cfg-check" }, "Fact-check with"), checkSel);
  const modelHint = h("p.hint");
  // Cloud providers the user has set up, from /api/providers; each one looks like a model source:
  // { id, label, ok, error, models: [{ name, label, price_in, price_out, tier }], default, strong, throughput }.
  let clouds = null;   // null until loaded, then { id: source } in registry order
  const isCloud = (pv) => pv !== "ollama";
  const asSource = (p, throughput) => {
    const list = [...p.models, ...p.liveModels].map((m) => ({ name: m.id, label: m.label, tier: m.tier,
      price_in: m.price ? m.price[0] : null, price_out: m.price ? m.price[1] : null }));
    // A custom server has no presets: its configured model is the only entry.
    if (p.default && !list.some((m) => m.name === p.default)) list.unshift({ name: p.default, label: p.default, price_in: null, price_out: null });
    return { ...p, models: list, throughput };
  };
  const source = () => (isCloud(cfg.provider) ? clouds?.[cfg.provider] : appState.ollama);
  const priceText = (m) => (m.price_in != null ? `$${m.price_in} / $${m.price_out} per M tokens` : "price unknown");
  const fillModels = () => {
    const o = source();
    if (!o) return;
    const names = o.models.map((m) => m.name);
    const chosen = cfg.model && names.includes(cfg.model) ? cfg.model : names.includes(o.default) ? o.default : names[0];
    clear(modelSel, names.length ? o.models.map((m) => h("option", { value: m.name },
      isCloud(cfg.provider)
        ? `${m.label}, ${priceText(m)}${m.name === o.default ? ", recommended" : ""}`
        : `${m.name}${m.parameters ? `, ${m.parameters}` : ""}${m.name === o.default ? ", recommended" : ""}`))
      : h("option", { value: "" }, "No models found"));
    if (chosen) modelSel.value = chosen;
    cfg.model = modelSel.value || null;
    // The fact-check decides what's kept, so it's worth a stronger model than the extraction.
    checkField.hidden = !isCloud(cfg.provider) || !cfg.verify;
    if (isCloud(cfg.provider)) {
      clear(checkSel, h("option", { value: "" }, "The same model"),
        o.models.filter((m) => m.name !== cfg.model).map((m) => h("option", { value: m.name },
          `${m.label}, ${priceText(m)}${m.name === o.strong ? ", recommended" : ""}`)));
      // Measured on a test book with Claude: Haiku as a checker threw out mostly true facts; Sonnet kept them
      // and caught real errors. So a lighter Claude reading model is checked by Sonnet unless the reader says otherwise.
      const sonnet = cfg.provider === "claude" ? o.models.find((m) => /sonnet/i.test(m.name) && m.name !== cfg.model) : null;
      const light = /haiku/i.test(cfg.model || "");
      if (cfg.check_model === null && light && sonnet && (!fromJob || legacy)) cfg.check_model = sonnet.name;
      checkSel.value = cfg.check_model && o.models.some((m) => m.name === cfg.check_model) ? cfg.check_model : "";
      cfg.check_model = checkSel.value || null;
    } else cfg.check_model = null;
    update();
  };
  modelSel.addEventListener("change", () => { cfg.model = modelSel.value; fillModels(); });
  // One row of the "where should it run" list: a real radio, the name with one quiet line under it, and the
  // time or cost at the right edge.
  const prow = (pv, title) => {
    const radio = h("input", { type: "radio", name: "cfg-provider", value: pv, checked: cfg.provider === pv });
    const body = h("span.pr-line");
    const meta = h("span.pr-meta.num");
    const card = h("label.prow", { dataset: { pv } }, radio, h("span.pr-text", h("span.pr-title", title), body), meta);
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      cfg.provider = pv; cfg.model = null; cfg.check_model = null;
      if (!chunkChosen) { cfg.chunk_words = defaultChunk(pv); chunk.value = String(cfg.chunk_words); }
      if (!modeChosen) { cfg.chunk_mode = defaultMode(pv); wholeBox.checked = cfg.chunk_mode === "chapter"; }
      ctxField.hidden = pv !== "ollama";
      fillModels(); update();
      pics?.runProviderChanged?.();   // the picture models' Default follows the run's provider
    });
    return { pv, radio, body, meta, card };
  };
  const cardOllama = prow("ollama", "On this computer");
  const cloudRows = {};   // provider id → row, rebuilt when the provider list arrives
  const noCloud = h("p.hint.prow-add", "With a cloud model: ", h("a", { href: "#/settings/cloud" }, "add a key in Settings"));
  const providerCards = h("div.prows", { role: "radiogroup", "aria-labelledby": "build-q" }, cardOllama.card);
  const paintRows = () => {
    for (const r of Object.values(cloudRows)) r.card.remove();
    noCloud.remove();
    for (const k of Object.keys(cloudRows)) delete cloudRows[k];
    const shown = Object.values(clouds || {}).filter((c) => c.configured || c.id === cfg.provider);
    for (const c of shown) {
      cloudRows[c.id] = prow(c.id, `With ${c.label}`);
      providerCards.append(cloudRows[c.id].card);
    }
    if (clouds && !shown.length) providerCards.append(noCloud);
  };
  api.providers().then((r) => {
    clouds = Object.fromEntries(r.providers.map((p) => [p.id, asSource(p, r.throughput)]));
    paintRows();
    if (isCloud(cfg.provider)) fillModels(); else update();
  }).catch(() => { clouds = {}; paintRows(); update(); });

  const chunk = h("input.range", { type: "range", min: "1000", max: "8000", step: "500", value: String(cfg.chunk_words), id: "cfg-chunk",
    "aria-describedby": "chunk-hint" });
  const chunkVal = h("output.num", { for: "cfg-chunk" }, `${fmtInt(cfg.chunk_words)} words`);
  const wholeBox = h("input", { type: "checkbox", role: "switch", id: "cfg-whole", checked: cfg.chunk_mode === "chapter", "aria-describedby": "whole-hint" });
  wholeBox.addEventListener("change", () => { modeChosen = true; cfg.chunk_mode = wholeBox.checked ? "chapter" : "words"; update(); });
  const sizeField = h("div.field", { id: "size-field" }, h("div.field-split",
    h("label", { for: "cfg-chunk" }, "Part size"), chunkVal), chunk,
    h("p.hint#chunk-hint", "Words the model reads per call."));
  const DEPTH_HINT = {
    light: "Main characters and places only, one short update each. Fastest.",
    standard: "The cast and anything worth looking up, a few updates each per chapter.",
    deep: "Everything named, including minor characters and terms. Slowest.",
  };
  const depthHint = h("p.hint", DEPTH_HINT[cfg.depth] || DEPTH_HINT.standard);
  const depthBtns = ["light", "standard", "deep"].map((d) => h("button", { type: "button", "aria-pressed": String(d === cfg.depth),
    dataset: { d }, on: { click: () => {
      cfg.depth = d; depthHint.textContent = DEPTH_HINT[d];
      depthBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.d === d)));
      update();
    } } }, d[0].toUpperCase() + d.slice(1)));
  const events = h("input", { type: "checkbox", checked: cfg.events, id: "cfg-events", role: "switch" });
  events.addEventListener("change", () => { cfg.events = events.checked; update(); });
  chunk.addEventListener("input", () => { cfg.chunk_words = +chunk.value; chunkChosen = true; update(); });

  const verify = h("input", { type: "checkbox", checked: cfg.verify, id: "cfg-verify", role: "switch" });
  verify.addEventListener("change", () => { cfg.verify = verify.checked; checkField.hidden = !isCloud(cfg.provider) || !cfg.verify; update(); });

  const text = (id, label, hint, key, ph) => {
    const input = h("input.input", { id, placeholder: ph || "", value: cfg[key] || "" });
    input.addEventListener("input", () => { cfg[key] = input.value.trim(); });
    return h("div.field", h("label", { for: id }, label), input, hint && h("p.hint", hint));
  };
  const license = h("select.select", { id: "cfg-license" }, ["CC0-1.0", "CC-BY-4.0"].map((l) => h("option", { value: l, selected: l === cfg.license }, l)));
  license.addEventListener("change", () => { cfg.license = license.value; });
  const ctxSel = h("select.select", { id: "cfg-ctx" }, [8192, 16384, 32768].map((n) => h("option", { value: n, selected: n === cfg.num_ctx }, `${fmtInt(n)} tokens`)));
  ctxSel.addEventListener("change", () => { cfg.num_ctx = +ctxSel.value; });
  const ctxField = h("div.field", { hidden: cfg.provider !== "ollama" }, h("label", { for: "cfg-ctx" }, "Context window (Ollama)"), ctxSel);

  // The estimate and the reason the button is off share one line; this is the only place the estimate is shown.
  const est = h("p.build-est", { role: "status" });
  const startBtn = h("button.btn.primary", { type: "button" }, "Build index");
  // A ready-made index offered above: building is the second choice, so it is not the page's one primary button.
  const ready = catalogKey ? readyMade(catalogKey) : null;
  const providerHead = h("h2#build-q.build-q", "Where should it run?");
  const buildOwn = h("h2.build-q.build-own", { hidden: true }, "Or build your own");
  ready?.onOffer((offering) => {
    startBtn.classList.toggle("primary", !offering);
    buildOwn.hidden = !offering;
  });
  ctx.cleanup.push(() => ready?.stop());
  if (ready) {
    // Opens the block (and its opt-in question, if it has not been answered) for this book.
    const off = registerProvider("build-community", () => [{ id: "find-community", label: "Find a community index for this book",
      group: "This book", keywords: "ready-made shared registry browse", run: () => {
        ready.refresh();
        setTimeout(() => { ready.el.scrollIntoView({ block: "center" }); ready.el.querySelector("button, a")?.focus(); }, 200);
      } }]);
    ctx.cleanup.push(off);
  }

  function update() {
    const chosen = book.chapters.filter((c) => picked.has(c.n));
    const words = chosen.reduce((a, c) => a + c.words, 0);
    const whole = chosen.length === storySet.size && chosen.every((c) => storySet.has(c.n));
    summary.textContent = `Sections: ${whole ? "the whole story, " : ""}${chosen.length} of ${book.chapters.length} (${fmtInt(words)} words)`;
    const whole_ = cfg.chunk_mode === "chapter";
    sizeField.hidden = whole_;
    optHint.textContent = [cfg.model, `${cfg.depth} depth`, whole_ ? "whole chapters" : `${fmtInt(cfg.chunk_words)}-word parts`].filter(Boolean).join(", ");
    chunk.style.setProperty("--fill", `${((cfg.chunk_words - 1000) / 7000) * 100}%`);
    chunkVal.textContent = `${fmtInt(cfg.chunk_words)} words`;
    const cloudNow = isCloud(cfg.provider);
    const factor = (cfg.verify ? 1 : 0.75) * { light: 0.7, standard: 1, deep: 1.5 }[cfg.depth] * (cfg.events ? 1 : 0.9);
    const sourceOf = (pv) => (isCloud(pv) ? clouds?.[pv] : appState.ollama);
    // Estimate for one provider and model: seconds (null when unmeasured) and dollars (null when the price is unknown).
    const estimate = (pv, name) => {
      const src = sourceOf(pv);
      const measured = (src?.throughput || {})[name];
      // Local models without a measurement get a rough guess; cloud models without one say "a few minutes".
      const tp = measured || (isCloud(pv) ? null : 3);
      const m = isCloud(pv) ? src?.models.find((x) => x.name === name) : null;
      // Per word of book text, measured on a fact-checked Claude Haiku run: about 1.2 input tokens at full
      // price (most of the passage is a cheap cache read on the second call) and 0.58 output tokens.
      const cost = m && m.price_in != null ? (words * factor * (1.2 * m.price_in + 0.58 * m.price_out)) / 1e6 : null;
      return { tp, secs: tp ? (words / tp) * factor : null, cost, m };
    };
    const money = (c) => `$${c < 0.1 ? c.toFixed(3) : c.toFixed(2)}`;
    const when = (secs) => (secs ? `about ${fmtDuration(secs)}` : "a few minutes");
    const selName = (pv) => (cfg.provider === pv && cfg.model) || sourceOf(pv)?.default || null;
    const cur = estimate(cfg.provider, cfg.model);

    // The rows: this computer, then every cloud provider with a key.
    const ol = appState.ollama, olName = selName("ollama"), olEst = estimate("ollama", olName);
    cardOllama.radio.disabled = !ol?.ok;
    cardOllama.card.classList.toggle("off", !ol?.ok);
    clear(cardOllama.body, ol?.ok
      ? `Free and private, with ${olName || "a local model"}.`
      : "Ollama isn't running. Start it with: brew services start ollama");
    cardOllama.meta.textContent = ol?.ok && words ? `About ${fmtDuration(olEst.secs)}` : "";
    for (const [pv, row] of Object.entries(cloudRows)) {
      const c = clouds[pv], e = estimate(pv, selName(pv)), ok = Boolean(c?.ok);
      row.radio.disabled = !ok;
      row.card.classList.toggle("off", !ok);
      clear(row.body, ok
        ? `Book text is sent to ${pv === "custom" ? c.base_url || "that server" : c.label}.`
        : c?.configured ? (c.error || `${c.label} isn't working.`)
          : [`${c?.label || pv} isn't set up yet. `, h("a", { href: "#/settings/cloud" }, "Add a key in Settings")]);
      row.meta.textContent = !ok || !words ? "" : e.cost != null ? `About ${money(e.cost)}` : "Price unknown";
    }
    cardOllama.radio.checked = !cloudNow;
    cardOllama.card.classList.toggle("on", !cloudNow);
    for (const [pv, row] of Object.entries(cloudRows)) {
      row.radio.checked = cfg.provider === pv;
      row.card.classList.toggle("on", cfg.provider === pv);
    }

    const cs = cloudNow ? clouds?.[cfg.provider] : null;
    modelHint.textContent = !cfg.model ? "" : cloudNow
      ? `Sends the selected text to ${cs?.label || "the provider"}. Much faster than local models, and billed per book.`
      : `About ${Math.round(cur.tp * 60)} words per minute with this model, measured locally when available.`;
    const unavailable = cloudNow ? (clouds ? (cs?.ok ? "" : cs?.error || `${cs?.label || "That provider"} isn't set up. Add a key in Settings.`) : "Checking your cloud models…")
      : (!appState.ollama?.ok ? "Ollama is not running. Start it with: brew services start ollama" : "");
    const reason = !words ? "Select at least one section to index." : unavailable || (!cfg.model ? "No model available." : "") || pics?.reason() || "";
    startBtn.disabled = Boolean(reason);
    est.classList.toggle("why", Boolean(reason));
    clear(est, reason
      ? [reason, cloudNow && clouds && !cs?.ok ? [" ", h("a", { href: "#/settings/cloud" }, "Open Settings")] : null]
      : cloudNow
        ? `${cur.cost != null ? `About ${money(cur.cost)}` : "Price unknown"} with ${cs.label}, ${when(cur.secs)}`
        : `About ${fmtDuration(cur.secs)} on this computer`);
  }

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try {
      const job = await api.createJob(book.id, {
        ...cfg, chapters: [...picked].sort((a, b) => a - b),
        series_id: cfg.series_id || null, book_key: cfg.book_key || null,
        ...(pics.config() ? { art: pics.config() } : {}),
        // From the Library: the server fills in the series and builds on earlier books' indexes.
        ...(catalogKey ? { catalog_key: catalogKey } : {}),
      });
      toast(`Queued “${book.title}”`);
      if (fromJob && replaceOld) {
        try { await api.deleteJob(fromJob.id); } catch (e) { toast(`The earlier build was kept: ${e.message}`, "error"); }
      }
      location.hash = `#/studio/job/${job.id}`;
    } catch (e) {
      toast(e.message, "error");
      startBtn.disabled = false;
    }
  });

  const del = h("button.linkish.build-remove", { type: "button", on: { click: async () => {
    if (!await confirmDialog({ title: "Remove this book?",
      message: `“${book.title}” leaves the uploaded books list. Indexes already built from it are kept.`,
      confirmLabel: "Remove" })) return;
    try { await api.deleteBook(book.id); toast("Book removed"); location.hash = "#/library"; }
    catch (e) { toast(e.message, "error"); }
  } } }, "Remove this book from uploads");

  const replaceBox = h("input", { type: "checkbox", checked: replaceOld, id: "cfg-replace" });
  replaceBox.addEventListener("change", () => { replaceOld = replaceBox.checked; });
  const banner = fromJob && h("div.notice.info.build-note",
    h("span", "Settings copied from the ", h("b", (fromJob.config.model || "").split(":")[0] || "earlier"),
      ` run started ${relTime(fromJob.created)}.`),
    h("label.replace-label", { for: "cfg-replace" }, replaceBox, "Delete the old run"));

  // Book kind: the detected one is the first option and means "auto"; the rest are explicit choices.
  const KINDS = [["fiction", "Fiction"], ["non-fiction", "Non-fiction"], ["self-help", "Self-help"]];
  const kindName = (k) => (KINDS.find(([v]) => v === k) || [k, k])[1];
  const BASIS = { subjects: "from the book's subjects", structure: "from its chapter titles", default: "the usual guess: fiction", model: "from reading the book" };
  const kindRows = h("div.prows", { role: "radiogroup", "aria-labelledby": "kind-q" });
  const kindRow = (value, title, line) => {
    const radio = h("input", { type: "radio", name: "cfg-kind", value, checked: cfg.book_kind === value });
    const card = h("label.prow", { class: cfg.book_kind === value ? "on" : "" }, radio,
      h("span.pr-text", h("span.pr-title", title), line ? h("span.pr-line", line) : null));
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      cfg.book_kind = value;
      kindRows.querySelectorAll(".prow").forEach((r) => r.classList.toggle("on", r.contains(radio)));
    });
    return card;
  };
  const paintKinds = (det) => {
    const first = det
      ? kindRow("auto", `Detected: ${kindName(det.detected)}`, BASIS[det.basis] ? `${cap1(BASIS[det.basis])}.` : null)
      : kindRow("auto", "Detected", "Lorekeeper decides from the book's subjects and chapter titles.");
    clear(kindRows, first, ...KINDS.map(([v, l]) => kindRow(v, l)));
  };
  const cap1 = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  if (book.bookKind?.detected) paintKinds(book.bookKind);
  else paintKinds(null);
  if (!book.bookKind?.detected && catalogKey) {
    api.book(catalogKey).then((b) => { if (b?.bookKind?.detected) paintKinds(b.bookKind); }).catch(() => {});
  }

  // Pictures, closed and off by default: drawn after the index is written, as the run's last stage. The estimate asks the
  // server (which counts from the book's current index when it has one); before a first build it is a rough count.
  let indexKey = null;
  if (catalogKey) api.book(catalogKey).then((b) => { indexKey = (b?.versions || []).find((v) => v.isDefault)?.key || null; pics.refresh(); }).catch(() => {});
  pics = picturesForm({
    // No chapter list here (the index the pictures follow is not written yet), so no `chapters`: every chapter it ends up with.
    estimate: (q) => api.artEstimate({ key: indexKey || "", catalog: catalogKey || "", ...q }),
    onChange: () => { if (pics) update(); },   // the form paints once while it is made, before `pics` is set
    runProvider: () => cfg.provider,   // "ollama" matches no picture writer, so Default is then the first working provider's
    fallback: (st) => {
      const n = [...picked].filter((n) => storySet.has(n)).length;
      if (st.level === "none") return `${st.cast === "main" ? "Portraits of the main characters" : "Portraits of everyone with a part"}, no chapter pictures; the count is set once the index is written.`;
      return `About ${n} chapter picture${n === 1 ? "" : "s"}${st.level === "all" ? " or more" : ""}${st.cast === "main" ? ", plus portraits of the main characters" : st.cast === "all" ? ", plus portraits" : ""}; the count is set once the index is written.`;
    },
  });

  const storyCount = storySet.size;
  // The book is a link when its page exists; the page you are on (Build) is plain text.
  const trail = crumbs([{ label: "Library", href: "#/library" },
    { label: book.title, href: catalogKey ? `#/book/${encodeURIComponent(catalogKey)}` : null }, { label: "Build" }]);
  clear(target,
    trail,
    h("header.build-head",
      h("div.build-id",
        cover({ title: book.title, authors: book.authors, cover: book.cover ? `/api/catalog/cover/up:${book.id}` : null }, "sm"),
        h("div", h("h1", book.title), h("p.by", book.authors.join(", ") || "Unknown author"))),
      h("p.build-facts", `${fmtInt(book.words)} words, ${storyCount} story ${storyCount === 1 ? "section" : "sections"}.`)),
    ready?.el,
    banner,
    buildOwn,
    h("section.build-run", h("h2#kind-q.build-q", "Book kind"), kindRows),
    h("section.build-run", providerHead, providerCards),
    h("div.build-go", est, startBtn),
    catalogKey ? h("p.hint.build-hand", "Prefer to write it yourself? Start an empty index and add entries as you read. ",
      h("button.linkish", { type: "button", on: { click: async (ev) => {
        ev.currentTarget.disabled = true;
        try {
          const { key } = await api.handmade(catalogKey, cfg.license);
          location.hash = `#/reader?i=${encodeURIComponent(key)}&edit=1`;
        } catch (e) { toast(e.message, "error"); ev.currentTarget.disabled = false; }
      } } }, "Write it myself")) : null,
    h("div.build-more",
      h("details.advanced.opts",
        h("summary", icon("chevron"), "Options", optHint),
        h("div.advanced-body",
          h("div.field", h("label", { for: "cfg-model" }, "Model"), modelSel, modelHint),
          h("div.field",
            h("label.switch", { for: "cfg-whole" }, wholeBox, h("span", h("span.label.label-block", "Read whole chapters"),
              h("span.hint#whole-hint", "One call per chapter, however long; a chapter over the model's limit is cut at a scene break. Best for books with long chapters."))),
            sizeField),
          h("div.field", h("span.label", "Depth"), h("div.seg", { role: "group", "aria-label": "Depth" }, depthBtns), depthHint),
          h("label.switch", { for: "cfg-events" }, events,
            h("span", h("span.label.label-block", "Timeline of key events"))),
          h("label.switch", { for: "cfg-verify" }, verify,
            h("span", h("span.label.label-block", "Fact-check every claim"), h("span.hint", "Recommended."))),
          checkField,
          text("cfg-series", "Series id, for a series indexed by hand", null, "series_id", "e.g. sherlock-holmes"),
          text("cfg-key", "Book key within that series", null, "book_key", "e.g. study-in-scarlet"),
          h("div.field", h("label", { for: "cfg-license" }, "Licence for the index"), license),
          ctxField)),
      pics.el,
      h("details.sections",
        h("summary", icon("chevron"), summary),
        h("div.sections-body",
          h("div.toolbar",
            h("button.btn.ghost", { type: "button", on: { click: setAll((c) => c.story) } }, "Story only"),
            h("button.btn.ghost", { type: "button", on: { click: setAll(() => true) } }, "All"),
            h("button.btn.ghost", { type: "button", on: { click: setAll(() => false) } }, "None")),
          h("div.chapter-list", { role: "group", "aria-label": "Sections to index" }, rows.map((r) => r.row)))),
      // Everything the form used to explain in place, said once and closed by default.
      h("details.how",
        h("summary", icon("chevron"), "How this works"),
        h("div.how-body",
          h("p", "On this computer, Ollama reads the book: free and private, and slow. A cloud model is much faster and costs a little per book, billed to your own account with that provider; book text is sent to it only when you build."),
          h("p", "Part size is how much text the model reads per call. A cloud model reads about a chapter at a time, so events and summaries come from one reading of it; a local model keeps to what its context window allows. \"Read whole chapters\" makes it one call per chapter however long, cutting a chapter over the model's limit at a scene break."),
          h("p", "The timeline gives the Reader a few key moments per chapter; turning it off saves a little time. The fact-check is a second pass over each passage that rejects unsupported claims and moves early ones later; it makes a build about a third slower. Accuracy depends most on it, so a stronger model there keeps more true facts and catches more errors."),
          h("p", "Pictures are drawn after the index is written. The text model sees the passage each picture comes from, as it did while reading; the image provider sees only the written prompt, never the book text. The run stops drawing at the amount you set and keeps what it drew."),
          h("p", "Front and back matter (contents, acknowledgements, about the author, licences) is unchecked in Sections for you."),
          h("p", "Series id and book key are only needed when this book is part of a series you are indexing by hand. Books built from the Library get their series from it.")))),
    del);

  ctx.cleanup.push(onOllama(() => { if (cfg.provider === "ollama") fillModels(); }));
  update();
}

// ---- the Runs page ------------------------------------------------------------------------------

// Whether the runs list is expanded survives a click on a run, which re-mounts the page.
let runsOpen = false;

export function mountStudio(root) {
  const { parts } = parseHash();
  const ctx = { jobs: [], view: parts[0] || null, id: parts[1] || null, cleanup: [] };
  let disposed = false;   // set on unmount: a late answer must not rewrite the address or open a stream for a page that is gone
  // A head row, then the runs as a table and the monitor of the open run beside it (under it when narrow).
  const count = h("p.runs-count");
  const side = h("section.studio-side", { "aria-label": "Runs" });
  const mainCol = h("section.studio-main", { "aria-live": "off" });
  root.append(h("div.studio", h("header.page-head", h("h1", "Runs"), count), h("div.runs-split", side, mainCol)));

  /** Re-read the runs. The first read throws so the page can say it failed; later ones only toast. */
  async function refresh({ first = false } = {}) {
    try {
      ctx.jobs = await api.jobs();
    } catch (e) {
      if (first) throw e;
      toast(`Could not reach the Lorekeeper server: ${e.message}`, "error");
    }
    renderSide();
  }

  /** "part 3 of 12" while it runs, else what it covered when it stopped. */
  const partText = (j) => partLine(j.progress, j.status);
  // "15 min ago" within a day, then a short date: the column stays narrow.
  const when = (t) => (Date.now() / 1000 - t < 86400 ? relTime(t) : new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }));
  const rowCells = (j) => [isArtJob(j) ? h("span.subtle", artLine(j))
    : h("span.subtle", { title: j.config.model || "default model" }, (j.config.model || "default model").split(":")[0]), statusBadge(j.status), partText(j), when(j.created)];
  let tableSig = "";
  /** The runs table. A poll that only changes a run's state or progress updates those cells in place, so focus and
   *  the open fold survive; a new or removed run rebuilds the rows. */
  function renderSide() {
    const active = ctx.jobs.filter((j) => ["running", "queued", "stopping"].includes(j.status)).length;
    count.textContent = ctx.jobs.length ? `${ctx.jobs.length} run${ctx.jobs.length === 1 ? "" : "s"}${active ? `, ${active} building` : ""}.` : "";
    const sig = `${ctx.jobs.map((j) => j.id).join(",")}|${ctx.view === "job" ? ctx.id : ""}`;   // the open run is part of it
    if (sig === tableSig && side.querySelector("tbody")) {
      for (const j of ctx.jobs) {
        const tr = side.querySelector(`tr[data-id="${CSS.escape(j.id)}"]`);
        if (!tr) continue;
        rowCells(j).forEach((c, i) => {
          const td = tr.children[i + 1];
          const next = h("td", c);
          if (td && td.textContent !== next.textContent) clear(td, c);
        });
      }
      return;
    }
    tableSig = sig;
    if (!ctx.jobs.length) return clear(side);
    const rows = ctx.jobs.map((j) => {
      const here = ctx.view === "job" && ctx.id === j.id;
      return h(`tr${here ? ".selected" : ""}`, { dataset: { id: j.id }, on: { click: (ev) => { if (!ev.target.closest("a")) location.hash = `#/studio/job/${j.id}`; } } },
        h("td.c-book", h("a", { href: `#/studio/job/${j.id}`, title: j.book_title, "aria-current": here ? "true" : null }, j.book_title)),
        ...rowCells(j).map((c, i) => h(`td${["", ".c-state", ".c-part.num", ".c-when.subtle"][i]}`, c)));
    });
    const table = (trs) => h("div.table-wrap", h("table.data.runs",
      h("caption.sr-only", "Runs, newest first"),
      h("thead", h("tr", ["Book", "Model", "State", "Progress", "Started"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", trs)));
    clear(side, foldList(rows, table, { show: matchMedia("(max-width: 640px)").matches ? 5 : 12, noun: "runs", open: runsOpen,
      onToggle: (on) => { runsOpen = on; }, keep: ctx.jobs.flatMap((j, i) => (ctx.view === "job" && ctx.id === j.id ? [i] : [])) }));
  }

  function renderMain() {
    if (ctx.view === "job" && ctx.id) return monitor(ctx.id);
    clear(mainCol, h("p.muted.runs-empty", "No runs yet. Start one from a book in ",
      h("a", { href: "#/library" }, "the Library"), "."));
  }

  // ---- monitor ---------------------------------------------------------------------------

  async function monitor(jobId) {
    let job;
    clear(mainCol, loading("Opening the run…"));
    try { job = await api.job(jobId); } catch (e) {
      if (disposed) return;
      if (e.status === 404) return clear(mainCol, h("div.empty", h("h3", "Run not found")));
      return clear(mainCol, loadError("open the run", e, () => monitor(jobId)));
    }
    if (disposed) return;

    const m = { status: job.status, chunks: 0, wordsTotal: 0, wordsDone: 0, current: null, phase: null, stats: {},
      chunkTimes: [], feed: [], recap: null, summary: null, logs: [], runStart: null, runWords: 0, runSeconds: 0, usage: null,
      lastDelta: {}, error: job.error, firstT: null };

    const el = {
      badge: h("span"), pct: h("div.progress-pct", "0", h("small", "%")), fill: h("div.fill"),
      bar: null, where: h("div.meta-row"), phases: h("ol.phases"), stats: h("dl.stats", { "aria-label": "Totals" }), drops: h("div.drops"),
      feed: h("ul.feed", { "aria-label": "Latest discoveries" }), recap: h("p.recap-quote"), summary: h("div"), chart: h("div.chunks-chart", { "aria-hidden": "true" }),
      log: h("pre", { tabindex: "0", "aria-label": "Generation log" }), actions: h("div.actions"), error: h("div"),
    };
    // What the model found is ahead of wherever the reader is, so it stays hidden until they say
    // they've read the book (remembered per book). Counts, timings and the log carry no content.
    const revealKey = `lk-reveal-${job.book_id}`;
    let revealed = false;
    try { revealed = localStorage.getItem(revealKey) === "1"; } catch {}
    el.foundHead = h("div.section-title", h("h3", "Discoveries"), h("span.eyebrow", "Newest first"));
    el.recapCard = h("section.mon-sec.recap-card", h("div.section-title", h("h3", "Story so far")), el.recap, el.summary);
    el.shield = h("div.spoiler-shield",
      icon("shield"),
      h("h3", "Hidden to avoid spoilers"),
      h("p.muted", "The model reads ahead of you, so the names, details and story it finds stay hidden here. "
        + "The Reader shows them only up to where you are."),
      h("button.btn", { type: "button", on: { click: () => setRevealed(true) } }, icon("eye"), "I've read this book, show them"));
    function setRevealed(on) {
      revealed = on;
      try { on ? localStorage.setItem(revealKey, "1") : localStorage.removeItem(revealKey); } catch {}
      el.shield.style.display = on ? "none" : "";
      el.feed.style.display = el.recapCard.style.display = on ? "" : "none";
      clear(el.foundHead, h("h3", "Discoveries"), on
        ? h("button.linkish", { type: "button", on: { click: () => setRevealed(false) } }, "Hide")
        : h("span.eyebrow", "Hidden"));
    }
    setRevealed(revealed);
    el.bar = h("div.bar", { role: "progressbar", "aria-label": "Generation progress", "aria-valuemin": "0", "aria-valuemax": "100" }, el.fill);

    let follow = true;
    const filter = h("input.input", { placeholder: "Filter log", "aria-label": "Filter log" });
    const followBtn = h("button.btn", { type: "button", "aria-pressed": "true" }, "Follow");
    followBtn.addEventListener("click", () => { follow = !follow; followBtn.setAttribute("aria-pressed", String(follow)); if (follow) el.log.scrollTop = el.log.scrollHeight; });
    const copyBtn = h("button.btn", { type: "button", "aria-label": "Copy log", title: "Copy log" }, icon("copy"));
    copyBtn.addEventListener("click", async () => { try { await navigator.clipboard.writeText(m.logs.map((l) => l.line).join("\n")); toast("Log copied"); } catch { toast("Copy failed", "error"); } });
    filter.addEventListener("input", () => paintLog(true));
    el.log.addEventListener("scroll", () => {
      const atBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 24;
      if (!atBottom && follow) { follow = false; followBtn.setAttribute("aria-pressed", "false"); }
    });

    // Flat regions on hairlines: the head with its actions, the progress line, the totals as one row, then the
    // discoveries beside the story, the quality gate and the part timings, and the log under everything.
    clear(mainCol,
      h("section.mon-sec.monitor-top",
        h("div.monitor-head",
          h("div.monitor-id", h("h2", job.book_title),
            h("p.run-meta",
              el.badge,
              // A pictures run has no reading model of its own: its style, quality and the provider that draws.
              isArtJob(job) ? [el.model = h("span.run-model", artLine(job)), h("span", "pictures for the index as it is")]
                : [el.model = h("span.run-model", job.config.model || "default model"),
                  h("span", job.config.chunk_mode === "chapter" ? "whole chapters" : `${fmtInt(job.config.chunk_words || 1200)}-word parts`),
                  h("span", job.config.verify === false ? "no fact-check" : "fact-checked"),
                  job.config.art ? h("span", `with pictures, ${artLine(job)}`) : null])),
          el.actions),
        el.error,
        h("div.progress-hero",
          h("div.progress-top", el.pct, el.where), el.bar, el.phases)),
      isArtJob(job) ? null : el.stats,
      // A pictures run reads nothing, so it has no discoveries, recap, quality gate or parts: only its progress and log.
      isArtJob(job) ? null : h("div.split",
        h("section.mon-sec", el.foundHead, el.shield, el.feed),
        h("div.monitor-right",
          el.recapCard,
          h("section.mon-sec", h("div.section-title", h("h3", "Quality gate"), h("span.eyebrow", "Dropped claims")), el.drops),
          h("section.mon-sec", h("div.section-title", h("h3", "Parts"), h("span.eyebrow", "Seconds each")), el.chart))),
      h("div.console",
        h("div.console-bar", h("span.title", icon("terminal"), "Log"), h("div.ctrls", filter, followBtn, copyBtn)),
        el.log));
    if ((job.config.provider || "ollama") !== "ollama" && !isArtJob(job)) {
      loadProviderLabels().then((labels) => {
        const label = labels[job.config.provider];
        if (label && el.model.isConnected) el.model.append(` on ${label}`);
      });
    }

    const tFmt = (t) => {
      if (!m.firstT) return "";
      const s = Math.max(0, Math.round(t - m.firstT));
      return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    };
    let logPainted = 0;
    function paintLog(full = false) {
      const q = filter.value.trim().toLowerCase();
      if (full) { el.log.replaceChildren(); logPainted = 0; }
      const frag = document.createDocumentFragment();
      for (const l of m.logs.slice(logPainted)) {
        if (q && !l.line.toLowerCase().includes(q)) continue;
        const cls = /^\[\d+\/\d+\]/.test(l.line) ? "chunk" : /^\s+blocks/.test(l.line) ? "result"
          : /stop|fail|error/i.test(l.line) ? "warn" : /^(queued|running|resum)/i.test(l.line) ? "dim" : "";
        frag.append(h(`span.ln${cls ? "." + cls : ""}`, h("span.ts", tFmt(l.t)), l.line));
      }
      logPainted = m.logs.length;
      el.log.append(frag);
      if (follow) el.log.scrollTop = el.log.scrollHeight;
    }

    // The actions repaint only when what they offer changes (the 1 s tick would otherwise take focus off Stop).
    let actionsKey = "";
    function paintActions() {
      const key = `${m.status}|${m.wordsDone > 0}`;
      if (key === actionsKey) return;
      actionsKey = key;
      const keep = el.actions.contains(document.activeElement) ? document.activeElement.textContent : null;
      const a = [];
      if (["running", "queued"].includes(m.status)) a.push(h("button.btn.danger", { type: "button", on: { click: async () => {
        try { await api.stopJob(jobId); toast(m.status === "queued" ? "Run cancelled" : "Stopping. Resume picks up from the last finished part."); } catch (e) { toast(e.message, "error"); }
      } } }, icon("stop"), m.status === "queued" ? "Cancel" : "Stop"));
      if (m.status === "stopping") a.push(h("button.btn", { type: "button", disabled: true }, icon("clock"), "Stopping…"));
      if (["stopped", "interrupted", "failed"].includes(m.status)) a.push(h("button.btn.primary", { type: "button", on: { click: async () => {
        try { await api.resumeJob(jobId); toast("Resuming"); } catch (e) { toast(e.message, "error"); }
      } } }, icon("refresh"), "Resume"));
      if (m.wordsDone > 0 || m.status === "done") {
        a.push(h(`a.btn${m.status === "done" ? ".primary" : ".quiet"}`, { "aria-label": `Read ${job.book_title}`, href: `#/reader?i=${encodeURIComponent(libraryKey(job))}` }, icon("book-open"), "Read"));
        a.push(h("a.btn.quiet", { href: `/api/jobs/${jobId}/download`, download: "" }, icon("download"), "Download index"));
      }
      if (["done", "stopped", "interrupted", "failed"].includes(m.status)) {
        // A pictures run drew for an index that was already there: re-running it is "Add pictures" on the book page, not a build.
        if (!isArtJob(job)) a.push(h("a.btn.quiet", { href: `#/build/${job.book_id}?from=${jobId}` }, icon("pen"), "Edit and re-run"));
        a.push(h("button.btn.quiet.danger", { type: "button", on: { click: async () => {
          if (!await confirmDialog({ title: "Delete this run?",
            // The server deletes a build's index with its run, but never the index a pictures run drew for, nor its pictures.
            message: isArtJob(job) ? "Only this run's record and log go. The index and the pictures it drew stay; remove pictures in Settings, Pictures."
              : "Its generated index is deleted too. This can't be undone.", confirmLabel: "Delete run" })) return;
          try { await api.deleteJob(jobId); toast("Run deleted"); location.hash = "#/studio"; } catch (e) { toast(e.message, "error"); }
        } } }, icon("trash"), "Delete run"));
      }
      if (m.status === "done" && job.config?.whole_story) {
        a.push(h("button.linkish.share-line", { type: "button", on: { click: () => openShare(libraryKey(job), job.book_title) } },
          "Share this index with the community"));
      }
      clear(el.actions, a);
      if (keep) [...el.actions.children].find((x) => x.textContent === keep)?.focus();
    }

    function paint() {
      clear(el.badge, statusBadge(m.status));
      const pct = isArtJob(job) && m.art?.[1] ? (m.art[0] / m.art[1]) * 100
        : m.wordsTotal ? (m.wordsDone / m.wordsTotal) * 100 : m.status === "done" ? 100 : 0;
      clear(el.pct, pct >= 10 || pct === 0 ? Math.round(pct) : pct.toFixed(1), h("small", "%"));
      el.fill.style.width = `${pct}%`;
      el.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
      el.bar.classList.toggle("live", m.status === "running");
      const rate = m.runSeconds ? m.runWords / m.runSeconds : (appState.ollama?.throughput || {})[job.config.model] || 0;
      const remaining = rate ? (m.wordsTotal - m.wordsDone) / rate : NaN;
      const elapsed = m.runStart ? (m.status === "running" ? Date.now() / 1000 - m.runStart : m.runSeconds) : 0;
      const cur = m.current;
      const describing = m.status === "running" && m.phase === "portraits" && m.portraits;
      const arcing = m.status === "running" && m.phase === "arcs" && m.arcs;
      // Drawing pictures: a count and the money spent on pictures, never what they show (that would reveal the book).
      const drawing = m.status === "running" && m.phase === "art" && m.art;
      clear(el.where,
        isArtJob(job) ? null : h("span", "Part ", h("b.num", m.chunks ? `${Math.min(m.chunkTimes.length + (m.status === "running" ? 1 : 0), m.chunks)} / ${m.chunks}` : "—")),
        m.art && (drawing || isArtJob(job) || m.status !== "running") ? h("span", drawing ? "Drawing pictures " : "Pictures ", h("b.num", `${m.art[0]} of ${m.art[1]}`),
          m.art[2] ? [", ", h("b.num", `$${m.art[2].toFixed(m.art[2] < 1 ? 3 : 2)}`), " on pictures"] : null) : null,
        describing && h("span", "Describing the cast ", h("b.num", `${m.portraits[0]} / ${m.portraits[1]}`), " chapters"),
        arcing && h("span", "Writing book arcs ", h("b.num", `${m.arcs[0]} / ${m.arcs[1]}`), " chapters"),
        cur && m.status === "running" && !describing && !arcing && !drawing && h("span", "Reading ", h("b", cur.chapter)),
        h("span", "Elapsed ", h("b.num", fmtDuration(elapsed))),
        m.status === "running" && !describing && !arcing && !drawing && !isArtJob(job) && h("span", "Remaining ", h("b.num", isFinite(remaining) ? `≈ ${fmtDuration(remaining)}` : (job.config.provider || "ollama") === "ollama" ? "estimating…" : "a few minutes")),
        rate ? h("span", h("b.num", `${Math.round(rate * 60)}`), " words/min") : null,
        m.usage && m.usage.seconds ? h("span", h("b.num", `${(m.usage.output_tokens / m.usage.seconds).toFixed(1)}`), " tokens/s") : null,
        m.usage && m.usage.cost_usd ? h("span", h("b.num", `$${m.usage.cost_usd.toFixed(m.usage.cost_usd < 1 ? 3 : 2)}`), " spent") : null,
        m.usage && m.usage.refusals ? h("span", h("b.num", String(m.usage.refusals)), " passages declined") : null);
      const phases = phasesOf(job);
      const at = m.status === "running" ? phases.findIndex((x) => x.key === m.phase) : -1;
      clear(el.phases, phases.map((p, i) => {
        const active = i === at, done = m.status === "done" || (at > 0 && i < at);
        return h(`li.phase${active ? ".active" : done ? ".done" : ""}`, { "aria-current": active ? "step" : null },
          icon(p.icon), p.label, done ? h("span.sr-only", " (done)") : null);
      }));

      const s = m.stats, d = m.lastDelta;
      const dropped = Object.entries(s).filter(([k]) => k.startsWith("dropped")).reduce((a, [, v]) => a + v, 0);
      const droppedDelta = Object.entries(d).filter(([k]) => k.startsWith("dropped")).reduce((a, [, v]) => a + v, 0);
      // One row of plain label / number pairs; "+N" is what the last part added.
      const pair = (k, v, dv, warn) => h(`div.stat${warn ? ".warn" : ""}`, h("dt", k), h("dd", h("span.v", fmtInt(v)), dv ? h("span.d", ` +${dv}`) : null));
      clear(el.stats,
        pair("Entries", s.entities, d.entities), pair("Details", s.facets, d.facets),
        pair("Connections", s.relations, d.relations), pair("Events", s.events, d.events),
        pair("Recaps", s.recaps, d.recaps), pair("Dropped", dropped, droppedDelta, true));

      const reasons = Object.entries(s).filter(([k, v]) => k.startsWith("dropped") && v).sort((a, b) => b[1] - a[1]);
      clear(el.drops, reasons.length
        ? [h("div.stackbar", { role: "img", "aria-label": `Dropped claims: ${reasons.map(([k, v]) => `${v} ${k.slice(9)}`).join(", ")}` },
            reasons.map(([, v], i) => h("span", { style: { width: `${(v / dropped) * 100}%`, background: DROP_COLORS[i % DROP_COLORS.length] } }))),
          h("ul.legend", reasons.map(([k, v], i) => h("li", h("span.sw", { style: { background: DROP_COLORS[i % DROP_COLORS.length] } }),
            k.slice(9).replace(/^./, (c) => c.toUpperCase()), h("span.num", fmtInt(v)))))]
        : h("p.muted", "Nothing dropped yet. Rejected claims and the reason appear here."));

      const maxT = Math.max(1, ...m.chunkTimes);
      const bars = m.chunkTimes.map((t, i) => h("span", { style: { height: `${Math.max(6, (t / maxT) * 100)}%` }, title: `Part ${i + 1}: ${fmtDuration(t)}` }));
      for (let i = m.chunkTimes.length; i < m.chunks; i++) bars.push(h(`span.${i === m.chunkTimes.length && m.status === "running" ? "current" : "pending"}`, { style: { height: "12%" } }));
      clear(el.chart, bars.length ? bars : h("p.muted", "Timings appear as parts finish."));

      if (!m.feed.length) clear(el.feed, h("li", h("span.what", "New characters, places and details will stream in here as the model reads.")));
      if (m.recap) el.recap.textContent = m.recap; else el.recap.textContent = "The first recap is written when the model finishes a chapter.";
      clear(el.summary, m.summary && h("div.last-chapter", h("span.eyebrow", `Latest chapter: ${m.summary.chapter}`), h("p", m.summary.text)));
      clear(el.error, m.dupes ? h("div.notice.info.notice-gap", icon("layers"),
        h("div", h("b", `${m.dupes} ${m.dupes === 1 ? "pair of entries" : "pairs of entries"} may be the same person or thing. `),
          "The Reader offers to merge them once you've reached both, so this note doesn't name them.")) : null,
      m.error && h("div.notice.warn.notice-gap", { role: "alert" }, icon("alert"), h("div", h("b", "This run failed. "), m.error)));
      paintActions();
    }

    function feedItem(a) {
      if (a.kind === "entity") return h("li", h("span.who", typeTag(a.type), a.name), h("span.what", "First appears."));
      if (a.kind === "facet") return h("li", h("span.who", icon("scroll"), a.name), h("span.what", a.text));
      if (a.kind === "relation") return h("li", h("span.who", icon("link"), `${a.src} → ${a.dst}`), h("span.what", a.text));
      return h("li", h("span.who", icon("timeline"), "Event"), h("span.what", a.text));
    }

    let painted = false;
    const schedule = () => { if (!painted) { painted = true; requestAnimationFrame(() => { painted = false; paint(); paintLog(); }); } };

    function handle(e) {
      m.firstT = m.firstT || e.t;
      switch (e.type) {
        case "queued": m.status = "queued"; m.logs.push({ t: e.t, line: e.resume ? "queued to resume" : "queued" }); break;
        case "running": m.status = "running"; m.runStart = e.t; m.runWords = 0; m.runSeconds = 0; m.error = null;
          m.logs.push({ t: e.t, line: e.resume ? "resuming from last checkpoint" : "running" }); break;
        case "start": m.chunks = e.chunks; m.wordsTotal = e.words; m.wordsDone = e.words_done;
          m.chunkTimes = m.chunkTimes.slice(0, e.done); break;
        case "chunk": m.current = e; m.phase = "extract"; break;
        case "phase": m.phase = e.phase; break;
        case "portraits": m.phase = "portraits"; m.portraits = [e.done, e.total]; break;
        case "arcs": m.phase = "arcs"; m.arcs = [e.done, e.total]; break;
        case "art": m.phase = "art"; m.art = [Number(e.done) || 0, Number(e.total) || 0, Number(e.cost) || 0]; break;
        case "chunk_done":
          m.wordsDone += e.words; m.runWords += e.words; m.runSeconds += e.seconds; m.chunkTimes.push(e.seconds);
          m.stats = e.stats; m.lastDelta = e.delta || {}; m.usage = e.usage;
          if (e.added?.length) {
            const items = e.added.slice().reverse().map(feedItem);
            if (!m.feed.length) el.feed.replaceChildren();
            el.feed.prepend(...items.slice(0, 40));
            while (el.feed.children.length > 120) el.feed.lastChild.remove();
            m.feed.push(...e.added);
          }
          if (live) announce(`Part ${e.index + 1} of ${e.chunks} done: ${e.delta?.entities || 0} entries and ${e.delta?.facets || 0} details added.`);
          break;
        case "recap": m.recap = e.text; if (e.summary) m.summary = { chapter: e.chapter, text: e.summary }; break;
        case "log": m.logs.push({ t: e.t, line: e.line }); break;
        case "done": m.status = "done"; m.stats = e.report?.stats || m.stats;
          if (live) { toast(`Finished “${job.book_title}”`); announce("Generation finished."); }
          // Names stay out of this note: the run covers the whole book, the reader may not have.
          api.duplicates(libraryKey(job)).then((d) => { m.dupes = d.pairs.length; paint(); }).catch(() => {});
          refresh(); break;
        case "stopped": m.status = "stopped"; if (live) toast("Run stopped. Resume any time."); refresh(); break;
        case "failed": m.status = "failed"; m.error = e.error; m.logs.push({ t: e.t, line: `failed: ${e.error}` });
          if (live) toast(`Run failed: ${e.error}`, "error"); refresh(); break;
      }
      schedule();
    }

    // Replay history silently, then go live. EventSource reconnects on its own with Last-Event-ID.
    let live = false;
    const source = new EventSource(`/api/jobs/${jobId}/events`);
    source.onmessage = (msg) => handle(JSON.parse(msg.data));
    // The event log can't express "the server restarted mid-run", so the job record is authoritative.
    const sync = async () => {
      try {
        const j = await api.job(jobId);
        if (j.status !== m.status) { m.status = j.status; m.error = j.error || m.error; schedule(); }
      } catch {}
    };
    setTimeout(() => { live = true; sync(); }, 1200);
    let ticks = 0;
    const ticker = setInterval(() => {
      if (m.status === "running" || m.status === "stopping") paint();
      if (++ticks % 5 === 0) sync();
    }, 1000);
    ctx.cleanup.push(() => { source.close(); clearInterval(ticker); });
    m.status = job.status;
    paint();
  }

  // Open on a run: the one that is running, else the first queued, else the newest. The address is
  // rewritten, not pushed, so Back leaves the page.
  const pickRun = () => {
    if (ctx.view === "job" && ctx.id) return;
    const jobs = ctx.jobs;
    const run = jobs.find((j) => j.status === "running") || jobs.filter((j) => j.status === "queued").sort((a, b) => a.created - b.created)[0]
      || [...jobs].sort((a, b) => b.created - a.created)[0];
    if (!run) return;
    ctx.view = "job"; ctx.id = run.id;
    replaceHash(`#/studio/job/${run.id}`);
  };
  const first = () => {
    clear(mainCol, loading("Loading your runs…"));
    refresh({ first: true }).then(() => { if (disposed) return; pickRun(); renderSide(); renderMain(); },
      (e) => { if (!disposed) clear(mainCol, loadError("load your runs", e, first)); });
  };
  first();
  const poll = setInterval(async () => {
    try { const jobs = await api.jobs(); const sig = (js) => JSON.stringify(js.map((j) => [j.id, j.status, partText(j)]));   // progress counts too
      if (sig(jobs) !== sig(ctx.jobs)) { ctx.jobs = jobs; renderSide(); } } catch {}
  }, 5000);
  return () => { disposed = true; clearInterval(poll); ctx.cleanup.forEach((fn) => fn()); };
}
