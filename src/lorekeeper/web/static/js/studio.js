import { api } from "./api.js";
import { onOllama, parseHash, state as appState } from "./app.js";
import { announce, clear, fmtDuration, fmtInt, h, icon, relTime, STATUS_LABEL, toast, typeTag } from "./dom.js";
import { confirmDialog, cover } from "./build.js";

const PHASES = [
  { key: "extract", label: "Reading", icon: "book-open" },
  { key: "rephrase", label: "Rephrasing", icon: "pen" },
  { key: "verify", label: "Fact-checking", icon: "shield" },
  { key: "recap", label: "Writing recap", icon: "feather" },
  { key: "portraits", label: "Writing descriptions", icon: "person" },
];
const DROP_COLORS = ["var(--t-person)", "var(--t-group)", "var(--t-concept)", "var(--t-object)", "var(--t-term)",
  "var(--t-place)", "var(--rose)", "var(--text-3)"];
const statusBadge = (s) => h(`span.badge.${s}`, STATUS_LABEL[s] || s);
// Provider id → label, fetched once, for the run's chip ("gemini-2.5-flash on Gemini").
let providerLabels = null;
const loadProviderLabels = () => (providerLabels ||= api.providers().then((r) => Object.fromEntries(r.providers.map((p) => [p.id, p.label]))).catch(() => ({})));
export const libraryKey = (job) => "generated--" + job.output_name.replace(/\.json$/, "");

// ---- the build form ---------------------------------------------------------------------------
// A short form on its own page: #/build/<localBookId>?catalog=<catalogKey>&from=<jobId>.

export function mountBuild(root) {
  const { parts, params } = parseHash();
  const ctx = { catalog: params.get("catalog"), cleanup: [] };
  const page = h("div.build");
  root.append(h("div.build-page", page));
  let gone = false;
  (async () => {
    let books;
    try { books = await api.books(); } catch (e) { return toast(`Could not reach the Lorekeeper server: ${e.message}`, "error"); }
    const from = params.get("from") ? await api.job(params.get("from")).catch(() => null) : null;
    if (gone) return;
    const book = books.find((b) => b.id === parts[0]);
    if (!book) {
      return clear(page, h("p.muted", "That book is no longer in your uploads. Find it in the ", h("a", { href: "#/library" }, "Library"), "."));
    }
    configure(page, book, ctx, from && from.book_id === book.id ? from : null);
  })();
  return () => { gone = true; ctx.cleanup.forEach((fn) => fn()); };
}

function configure(target, book, ctx, fromJob = null) {
  const t = fromJob?.config || {};
  const picked = new Set(Array.isArray(t.chapters) && t.chapters.length ? t.chapters
    : book.chapters.filter((c) => c.story).map((c) => c.n));
  const cfg = { provider: "ollama", model: null, check_model: null, chunk_words: 2500, depth: "standard", events: true, verify: true,
    series_id: "", book_key: "", license: "CC0-1.0", num_ctx: 16384 };
  for (const k of Object.keys(cfg)) if (t[k] !== undefined && t[k] !== null) cfg[k] = t[k];
  // A run from before the fact-check model and smaller chunks re-runs with today's defaults.
  const legacy = Boolean(fromJob) && !("check_model" in t);
  if (cfg.chunk_words < 1000 || (legacy && cfg.chunk_words > 4000)) cfg.chunk_words = 2500;
  let replaceOld = Boolean(fromJob);
  const maxWords = Math.max(1, ...book.chapters.map((c) => c.words));
  const catalogKey = ctx.catalog || fromJob?.config.catalog_key || null;

  const summary = h("span.sec-summary");
  const optHint = h("span.sum-hint");
  const storySet = new Set(book.chapters.filter((c) => c.story).map((c) => c.n));
  const rows = book.chapters.map((c) => {
    const box = h("input", { type: "checkbox", checked: picked.has(c.n), "aria-label": `Include ${c.title}` });
    const row = h("label.chapter-row", { class: picked.has(c.n) ? "" : "off" },
      box, h("span.ch-title", { title: c.title }, h("span.subtle.num", `${String(c.n).padStart(2, "0")}  `), c.title),
      h("span.wordbar", h("i", h("b", { style: { width: `${Math.max(2, (c.words / maxWords) * 100)}%` } })), fmtInt(c.words)));
    box.addEventListener("change", () => { box.checked ? picked.add(c.n) : picked.delete(c.n); row.classList.toggle("off", !box.checked); update(); });
    return { row, box, c };
  });
  const setAll = (fn) => () => { rows.forEach((r) => { r.box.checked = fn(r.c); r.box.dispatchEvent(new Event("change")); }); };

  const modelSel = h("select.select", { id: "cfg-model" });
  const checkSel = h("select.select", { id: "cfg-check" });
  checkSel.addEventListener("change", () => { cfg.check_model = checkSel.value || null; update(); });
  const checkField = h("div.field", { hidden: true }, h("label", { for: "cfg-check" }, "Fact-check with"), checkSel,
    h("p.hint", "Accuracy depends most on the fact-check. A stronger model here keeps more true facts and catches more errors."));
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
  // One row of the "where should it run" list: a real radio, a bold label, one sentence under it.
  const prow = (pv, title) => {
    const radio = h("input", { type: "radio", name: "cfg-provider", value: pv, checked: cfg.provider === pv });
    const body = h("span.pr-line");
    const card = h("label.prow", { dataset: { pv } }, radio, h("span.pr-text", h("span.pr-title", title), body));
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      cfg.provider = pv; cfg.model = null; cfg.check_model = null;
      ctxField.hidden = pv !== "ollama";
      fillModels(); update();
    });
    return { pv, radio, body, card };
  };
  const cardOllama = prow("ollama", "On this computer");
  const cloudRows = {};   // provider id → row, rebuilt when the provider list arrives
  const noCloud = h("p.hint.prow-add", "With a cloud model: ", h("a", { href: "#/settings" }, "add a key in Settings"));
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
  chunk.addEventListener("input", () => { cfg.chunk_words = +chunk.value; update(); });

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

  function update() {
    const chosen = book.chapters.filter((c) => picked.has(c.n));
    const words = chosen.reduce((a, c) => a + c.words, 0);
    const whole = chosen.length === storySet.size && chosen.every((c) => storySet.has(c.n));
    summary.textContent = `Sections: ${whole ? "the whole story, " : ""}${chosen.length} of ${book.chapters.length} (${fmtInt(words)} words)`;
    optHint.textContent = [cfg.model, `${cfg.depth} depth`, `${fmtInt(cfg.chunk_words)}-word chunks`].filter(Boolean).join(", ");
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
      ? `Free and private. Uses Ollama with ${olName || "a local model"}. ${words ? `About ${fmtDuration(olEst.secs)}.` : "Select at least one section."}`
      : "Ollama isn't running. Start it with: brew services start ollama");
    for (const [pv, row] of Object.entries(cloudRows)) {
      const c = clouds[pv], e = estimate(pv, selName(pv)), ok = Boolean(c?.ok);
      row.radio.disabled = !ok;
      row.card.classList.toggle("off", !ok);
      clear(row.body, ok
        ? (pv === "custom"
          ? `Your own server. Price unknown. Book text is sent to ${c.base_url || "that server"}.`
          : `Much faster. ${e.cost != null && words ? `About ${money(e.cost)}` : e.cost == null && words ? "Price unknown" : "Costs a little"}, billed to your ${c.label} account. Book text is sent to ${c.label}.`)
        : c?.configured ? (c.error || `${c.label} isn't working.`)
          : [`${c?.label || pv} isn't set up yet. `, h("a", { href: "#/settings" }, "Add a key in Settings")]);
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
    const reason = !words ? "Select at least one section to index." : unavailable || (!cfg.model ? "No model available." : "");
    startBtn.disabled = Boolean(reason);
    est.classList.toggle("why", Boolean(reason));
    clear(est, reason
      ? [reason, cloudNow && clouds && !cs?.ok ? [" ", h("a", { href: "#/settings" }, "Open Settings")] : null]
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

  const storyCount = storySet.size;
  const sep = () => h("span.sep", { "aria-hidden": "true" }, "/");
  clear(target,
    h("nav.crumbs.build-crumbs", { "aria-label": "Breadcrumb" }, h("a", { href: "#/library" }, "Library"), sep(),
      catalogKey ? h("a", { href: `#/book/${encodeURIComponent(catalogKey)}` }, book.title) : h("span", book.title)),
    h("header.build-head",
      h("div.build-id",
        cover({ title: book.title, authors: book.authors, cover: book.cover ? `/api/catalog/cover/up:${book.id}` : null }, "sm"),
        h("div", h("h1", book.title), h("p.by", book.authors.length ? `by ${book.authors.join(", ")}` : "Unknown author"))),
      h("p.build-facts", `${fmtInt(book.words)} words, ${storyCount} story ${storyCount === 1 ? "section" : "sections"}.`)),
    banner,
    h("section.build-run", h("h2#build-q.build-q", "Where should it run?"), providerCards),
    h("div.build-go", est, startBtn),
    h("div.build-more",
      h("details.advanced.opts",
        h("summary", icon("chevron"), "Options", optHint),
        h("div.advanced-body",
          h("div.field", h("label", { for: "cfg-model" }, "Model"), modelSel, modelHint),
          h("div.field", h("div.field-split",
            h("label", { for: "cfg-chunk" }, "Chunk size"), chunkVal), chunk,
            h("p.hint#chunk-hint", "How much text the model reads per call. Smaller is more accurate; larger is faster and a little cheaper.")),
          h("div.field", h("span.label", "Depth"), h("div.seg", { role: "group", "aria-label": "Depth" }, depthBtns), depthHint),
          h("label.switch", { for: "cfg-events" }, events,
            h("span", h("span.label.label-block", "Timeline of key events"),
              h("span.hint", "A few key moments per chapter for the Reader's timeline. Turn off for a little more speed."))),
          h("label.switch", { for: "cfg-verify" }, verify,
            h("span", h("span.label.label-block", "Fact-check every claim"),
              h("span.hint", "A second pass over each passage rejects unsupported claims and moves early ones later. Recommended; about a third slower."))),
          checkField,
          h("div.field", h("span.label", "Series and licence"),
            h("p.hint", "Only needed when this book is part of a series you are indexing by hand.")),
          text("cfg-series", "Series id", "Shared by every book in a series, e.g. sherlock-holmes.", "series_id", "optional"),
          text("cfg-key", "Book key", "This book's key within the series, e.g. study-in-scarlet.", "book_key", "optional"),
          h("div.field", h("label", { for: "cfg-license" }, "Licence for the index"), license),
          ctxField)),
      h("details.sections",
        h("summary", icon("chevron"), summary),
        h("div.sections-body",
          h("p.hint.sections-hint", "Front and back matter (contents, acknowledgements, about the author, licences) is unchecked for you."),
          h("div.toolbar",
            h("button.btn.ghost", { type: "button", on: { click: setAll((c) => c.story) } }, "Story only"),
            h("button.btn.ghost", { type: "button", on: { click: setAll(() => true) } }, "All"),
            h("button.btn.ghost", { type: "button", on: { click: setAll(() => false) } }, "None")),
          h("div.chapter-list", { role: "group", "aria-label": "Sections to index" }, rows.map((r) => r.row))))),
    del);

  ctx.cleanup.push(onOllama(() => { if (cfg.provider === "ollama") fillModels(); }));
  update();
}

// ---- the Runs page ------------------------------------------------------------------------------

export function mountStudio(root) {
  const { parts } = parseHash();
  const ctx = { jobs: [], view: parts[0] || null, id: parts[1] || null, cleanup: [] };
  const side = h("aside.studio-side", { "aria-label": "Runs" });
  const mainCol = h("section.studio-main", { "aria-live": "off" });
  root.append(h("div.studio", side, mainCol));

  async function refresh() {
    try {
      ctx.jobs = await api.jobs();
    } catch (e) {
      toast(`Could not reach the Lorekeeper server: ${e.message}`, "error");
    }
    renderSide();
  }

  function renderSide() {
    clear(side, runsCard());
  }

  function runsCard() {
    const items = ctx.jobs.slice(0, 12).map((j) => h("li", h("button.job-item", {
      type: "button", "aria-current": ctx.view === "job" && ctx.id === j.id ? "true" : "false",
      on: { click: () => { location.hash = `#/studio/job/${j.id}`; } },
    }, statusBadge(j.status),
    h("span", h("span.item-title.job-title", j.book_title),
      h("span.item-meta", `${(j.config.model || "").split(":")[0]}, ${relTime(j.created)}`)),
    icon("chevron"))));
    return h("div.card.card-pad",
      h("div.section-title", h("h2", "Runs"), h("span.eyebrow", `${ctx.jobs.length}`)),
      items.length ? h("ul.list", items) : h("p.muted", "Runs you start appear here with live progress."));
  }

  function renderMain() {
    if (ctx.view === "job" && ctx.id) return monitor(ctx.id);
    clear(mainCol, h("p.muted.runs-empty", "Runs appear here while an index builds. Start one from a book in ",
      h("a", { href: "#/library" }, "the Library"), "."));
  }

  // ---- monitor ---------------------------------------------------------------------------

  async function monitor(jobId) {
    let job;
    try { job = await api.job(jobId); } catch { return clear(mainCol, h("div.card.empty", h("h3", "Run not found"))); }

    const m = { status: job.status, chunks: 0, wordsTotal: 0, wordsDone: 0, current: null, phase: null, stats: {},
      chunkTimes: [], feed: [], recap: null, summary: null, logs: [], runStart: null, runWords: 0, runSeconds: 0, usage: null,
      lastDelta: {}, error: job.error, firstT: null };

    const el = {
      badge: h("span"), pct: h("div.progress-pct", "0", h("small", "%")), fill: h("div.fill"),
      bar: null, where: h("div.meta-row"), phases: h("ol.phases"), stats: h("div.stats", { role: "group", "aria-label": "Totals" }), drops: h("div.drops"),
      feed: h("ul.feed", { "aria-label": "Latest discoveries" }), recap: h("p.recap-quote"), summary: h("div"), chart: h("div.chunks-chart", { "aria-hidden": "true" }),
      log: h("pre", { tabindex: "0", "aria-label": "Generation log" }), actions: h("div.actions"), error: h("div"),
    };
    // What the model found is ahead of wherever the reader is, so it stays hidden until they say
    // they've read the book (remembered per book). Counts, timings and the log carry no content.
    const revealKey = `lk-reveal-${job.book_id}`;
    let revealed = false;
    try { revealed = localStorage.getItem(revealKey) === "1"; } catch {}
    el.foundHead = h("div.section-title", h("h2", "Discoveries"), h("span.eyebrow", "Newest first"));
    el.recapCard = h("div.card.card-pad.recap-card", h("div.section-title", h("h2", "Story so far")), el.recap, el.summary);
    el.shield = h("div.spoiler-shield",
      icon("shield"),
      h("h3", "Hidden to avoid spoilers"),
      h("p.muted", "The model reads ahead of you, so the names, facts and story it finds stay hidden here. "
        + "The Reader shows them only up to where you are."),
      h("button.btn", { type: "button", on: { click: () => setRevealed(true) } }, icon("eye"), "I've read this book, show them"));
    function setRevealed(on) {
      revealed = on;
      try { on ? localStorage.setItem(revealKey, "1") : localStorage.removeItem(revealKey); } catch {}
      el.shield.style.display = on ? "none" : "";
      el.feed.style.display = el.recapCard.style.display = on ? "" : "none";
      clear(el.foundHead, h("h2", "Discoveries"), on
        ? h("button.linkish", { type: "button", on: { click: () => setRevealed(false) } }, "Hide")
        : h("span.eyebrow", "Hidden"));
    }
    setRevealed(revealed);
    el.bar = h("div.bar", { role: "progressbar", "aria-label": "Generation progress", "aria-valuemin": "0", "aria-valuemax": "100" }, el.fill);

    let follow = true;
    const filter = h("input.input", { placeholder: "Filter log", "aria-label": "Filter log" });
    const followBtn = h("button.btn", { type: "button", "aria-pressed": "true" }, "Follow");
    followBtn.addEventListener("click", () => { follow = !follow; followBtn.setAttribute("aria-pressed", String(follow)); if (follow) el.log.scrollTop = el.log.scrollHeight; });
    const copyBtn = h("button.btn", { type: "button", "aria-label": "Copy log" }, icon("copy"));
    copyBtn.addEventListener("click", async () => { try { await navigator.clipboard.writeText(m.logs.map((l) => l.line).join("\n")); toast("Log copied"); } catch { toast("Copy failed", "error"); } });
    filter.addEventListener("input", () => paintLog(true));
    el.log.addEventListener("scroll", () => {
      const atBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 24;
      if (!atBottom && follow) { follow = false; followBtn.setAttribute("aria-pressed", "false"); }
    });

    clear(mainCol,
      h("div.card.card-pad",
        h("div.monitor-head",
          h("div", h("span.eyebrow", "Run"), h("h1", job.book_title),
            h("p.muted.run-meta",
              el.badge, el.model = h("span.chip", icon((job.config.provider || "ollama") === "ollama" ? "cpu" : "sparkle"), job.config.model || "default model"),
              h("span.chip", `${fmtInt(job.config.chunk_words || 1200)}-word chunks`),
              h("span.chip", icon("shield"), job.config.verify === false ? "No fact-check" : "Fact-checked"))),
          el.actions),
        el.error,
        h("div.progress-hero.progress-hero-wrap",
          h("div.progress-top", el.pct, el.where), el.bar, el.phases)),
      el.stats,
      h("div.split",
        h("div.card.card-pad", el.foundHead, el.shield, el.feed),
        h("div.monitor-right",
          el.recapCard,
          h("div.card.card-pad", h("div.section-title", h("h2", "Quality gate"), h("span.eyebrow", "Dropped claims")), el.drops),
          h("div.card.card-pad", h("div.section-title", h("h2", "Chunks"), h("span.eyebrow", "Seconds each")), el.chart))),
      h("div.console",
        h("div.console-bar", h("span.title", icon("terminal"), "Log"), h("div.ctrls", filter, followBtn, copyBtn)),
        el.log));
    if ((job.config.provider || "ollama") !== "ollama") {
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

    function paintActions() {
      const a = [];
      if (["running", "queued"].includes(m.status)) a.push(h("button.btn.danger", { type: "button", on: { click: async () => {
        try { await api.stopJob(jobId); toast(m.status === "queued" ? "Run cancelled" : "Stopping. Resume picks up from the last finished chunk."); } catch (e) { toast(e.message, "error"); }
      } } }, icon("stop"), m.status === "queued" ? "Cancel" : "Stop"));
      if (m.status === "stopping") a.push(h("button.btn", { type: "button", disabled: true }, icon("clock"), "Stopping…"));
      if (["stopped", "interrupted", "failed"].includes(m.status)) a.push(h("button.btn.primary", { type: "button", on: { click: async () => {
        try { await api.resumeJob(jobId); toast("Resuming"); } catch (e) { toast(e.message, "error"); }
      } } }, icon("refresh"), "Resume"));
      if (m.wordsDone > 0 || m.status === "done") {
        a.push(h("a.btn", { "aria-label": `Read ${job.book_title}`, href: `#/reader?i=${encodeURIComponent(libraryKey(job))}` }, icon("book-open"), "Read"));
        a.push(h("a.btn", { href: `/api/jobs/${jobId}/download`, download: "" }, icon("download"), "Download index"));
      }
      if (["done", "stopped", "interrupted", "failed"].includes(m.status)) {
        a.push(h("a.btn", { href: `#/build/${job.book_id}?from=${jobId}` }, icon("pen"), "Edit and re-run"));
        a.push(h("button.btn.danger", { type: "button", on: { click: async () => {
          if (!await confirmDialog({ title: "Delete this run?",
            message: "Its generated index is deleted too. This can't be undone.", confirmLabel: "Delete run" })) return;
          try { await api.deleteJob(jobId); toast("Run deleted"); location.hash = "#/studio"; } catch (e) { toast(e.message, "error"); }
        } } }, icon("trash"), "Delete run"));
      }
      clear(el.actions, a);
    }

    function paint() {
      clear(el.badge, statusBadge(m.status));
      const pct = m.wordsTotal ? (m.wordsDone / m.wordsTotal) * 100 : m.status === "done" ? 100 : 0;
      clear(el.pct, pct >= 10 || pct === 0 ? Math.round(pct) : pct.toFixed(1), h("small", "%"));
      el.fill.style.width = `${pct}%`;
      el.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
      el.bar.classList.toggle("live", m.status === "running");
      const rate = m.runSeconds ? m.runWords / m.runSeconds : (appState.ollama?.throughput || {})[job.config.model] || 0;
      const remaining = rate ? (m.wordsTotal - m.wordsDone) / rate : NaN;
      const elapsed = m.runStart ? (m.status === "running" ? Date.now() / 1000 - m.runStart : m.runSeconds) : 0;
      const cur = m.current;
      const describing = m.status === "running" && m.phase === "portraits" && m.portraits;
      clear(el.where,
        h("span", "Chunk ", h("b.num", m.chunks ? `${Math.min(m.chunkTimes.length + (m.status === "running" ? 1 : 0), m.chunks)} / ${m.chunks}` : "—")),
        describing && h("span", "Describing the cast ", h("b.num", `${m.portraits[0]} / ${m.portraits[1]}`), " chapters"),
        cur && m.status === "running" && !describing && h("span", "Reading ", h("b", cur.chapter)),
        h("span", "Elapsed ", h("b.num", fmtDuration(elapsed))),
        m.status === "running" && !describing && h("span", "Remaining ", h("b.num", isFinite(remaining) ? `≈ ${fmtDuration(remaining)}` : (job.config.provider || "ollama") === "ollama" ? "estimating…" : "a few minutes")),
        rate ? h("span", h("b.num", `${Math.round(rate * 60)}`), " words/min") : null,
        m.usage && m.usage.seconds ? h("span", h("b.num", `${(m.usage.output_tokens / m.usage.seconds).toFixed(1)}`), " tokens/s") : null,
        m.usage && m.usage.cost_usd ? h("span", h("b.num", `$${m.usage.cost_usd.toFixed(m.usage.cost_usd < 1 ? 3 : 2)}`), " spent") : null,
        m.usage && m.usage.refusals ? h("span", h("b.num", String(m.usage.refusals)), " passages declined") : null);
      const at = m.status === "running" ? PHASES.findIndex((x) => x.key === m.phase) : -1;
      clear(el.phases, PHASES.map((p, i) => {
        const active = i === at, done = m.status === "done" || (at > 0 && i < at);
        return h(`li.phase${active ? ".active" : done ? ".done" : ""}`, { "aria-current": active ? "step" : null },
          icon(p.icon), p.label, done ? h("span.sr-only", " (done)") : null);
      }));

      const s = m.stats, d = m.lastDelta;
      const dropped = Object.entries(s).filter(([k]) => k.startsWith("dropped")).reduce((a, [, v]) => a + v, 0);
      const droppedDelta = Object.entries(d).filter(([k]) => k.startsWith("dropped")).reduce((a, [, v]) => a + v, 0);
      const tile = (k, ic, v, dv, warn) => h(`div.stat${warn ? ".warn" : ""}`, h("span.k", icon(ic), k), h("span.v", fmtInt(v)),
        h("span.d", dv ? `+${dv} last chunk` : ""));
      clear(el.stats,
        tile("Entities", "person", s.entities, d.entities), tile("Facts", "scroll", s.facets, d.facets),
        tile("Relations", "link", s.relations, d.relations), tile("Events", "timeline", s.events, d.events),
        tile("Recaps", "feather", s.recaps, d.recaps), tile("Dropped", "scissors", dropped, droppedDelta, true));

      const reasons = Object.entries(s).filter(([k, v]) => k.startsWith("dropped") && v).sort((a, b) => b[1] - a[1]);
      clear(el.drops, reasons.length
        ? [h("div.stackbar", { role: "img", "aria-label": `Dropped claims: ${reasons.map(([k, v]) => `${v} ${k.slice(9)}`).join(", ")}` },
            reasons.map(([, v], i) => h("span", { style: { width: `${(v / dropped) * 100}%`, background: DROP_COLORS[i % DROP_COLORS.length] } }))),
          h("ul.legend", reasons.map(([k, v], i) => h("li", h("span.sw", { style: { background: DROP_COLORS[i % DROP_COLORS.length] } }),
            k.slice(9).replace(/^./, (c) => c.toUpperCase()), h("span.num", fmtInt(v)))))]
        : h("p.muted", "Nothing dropped yet. Rejected claims and the reason appear here."));

      const maxT = Math.max(1, ...m.chunkTimes);
      const bars = m.chunkTimes.map((t, i) => h("span", { style: { height: `${Math.max(6, (t / maxT) * 100)}%` }, title: `Chunk ${i + 1}: ${fmtDuration(t)}` }));
      for (let i = m.chunkTimes.length; i < m.chunks; i++) bars.push(h(`span.${i === m.chunkTimes.length && m.status === "running" ? "current" : "pending"}`, { style: { height: "12%" } }));
      clear(el.chart, bars.length ? bars : h("p.muted", "Timings appear as chunks finish."));

      if (!m.feed.length) clear(el.feed, h("li", h("span.what", "New characters, places and facts will stream in here as the model reads.")));
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
          if (live) announce(`Chunk ${e.index + 1} of ${e.chunks} done: ${e.delta?.entities || 0} entities and ${e.delta?.facets || 0} facts added.`);
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

  refresh().then(renderMain);
  const poll = setInterval(async () => {
    try { const jobs = await api.jobs(); if (JSON.stringify(jobs.map((j) => [j.id, j.status])) !== JSON.stringify(ctx.jobs.map((j) => [j.id, j.status]))) { ctx.jobs = jobs; renderSide(); } } catch {}
  }, 5000);
  return () => { clearInterval(poll); ctx.cleanup.forEach((fn) => fn()); };
}
