// Shared build flow for the Library, Book and Series pages: series preflight, the "earlier book
// has no index" choice, and a compact settings dialog for queuing several books in order.
import { api } from "./api.js";
import { state as appState } from "./app.js";
import { clear, fmtDuration, fmtInt, h, icon, toast } from "./dom.js";

/** A modal dialog on a native <dialog> (focus trap, Esc, inert page and scroll lock come from the
 *  browser). Returns { close, body, dialog }. Esc and the backdrop close it; focus returns afterwards.
 *  Callers may override the returned `close`: every dismissal path goes through it. */
export function modal(title, { width = 600 } = {}) {
  const prev = document.activeElement;
  const body = h("div", { style: { display: "grid", gap: "16px" } });
  const closeBtn = h("button.icon-btn.small", { type: "button", "aria-label": "Close" }, icon("x"));
  const dlg = h("dialog.modal", { "aria-labelledby": "modal-title", style: { maxWidth: `${width}px` } },
    h("div", { style: { display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "flex-start" } },
      h("h2#modal-title", title), closeBtn), body);
  let done = false;
  const api = {
    dialog: dlg, body,
    close() {
      if (done) return;
      done = true;
      window.removeEventListener("hashchange", api.close0);
      if (dlg.open) dlg.close();
      dlg.remove();
      if (prev && document.contains(prev)) prev.focus();
    },
  };
  api.close0 = () => api.close();   // route change: close without going through an override
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); api.close(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) api.close(); });
  closeBtn.addEventListener("click", () => api.close());
  window.addEventListener("hashchange", api.close0);
  document.body.append(dlg);
  dlg.showModal();
  (dlg.querySelector("input, select, button:not(.icon-btn)") || closeBtn).focus();
  return api;
}

/** Ask before a destructive step. Resolves true on confirm, false on cancel, close or Esc. */
export function confirmDialog({ title, message, confirmLabel = "Delete", cancelLabel = "Cancel", danger = true }) {
  return new Promise((resolve) => {
    const m = modal(title, { width: 460 });
    const real = m.close;
    m.close = () => { real(); resolve(false); };
    const cancel = h("button.btn", { type: "button" }, cancelLabel);
    const ok = h(`button.btn.${danger ? "danger" : "primary"}`, { type: "button" }, confirmLabel);
    cancel.addEventListener("click", () => m.close());
    ok.addEventListener("click", () => { resolve(true); real(); });
    m.body.append(h("p", { style: { margin: 0 } }, message),
      h("div.confirm-actions", cancel, ok));
    (danger ? cancel : ok).focus();
  });
}

/** Start building one catalog book: checks the series first, then opens the configure screen. */
export async function startBuild(entry) {
  let pre;
  try { pre = await api.preflight(entry.key); } catch (e) { return toast(e.message, "error"); }
  const missing = pre.missingEarlier || [];
  const absent = pre.notInLibrary || [];
  if (!missing.length && !absent.length) return openConfigure(entry);

  const buildable = missing.filter((m) => m.canBuild);
  const m = modal(missing.length === 1 && !absent.length ? `Book ${missing[0].index} doesn't have an index yet`
    : !missing.length && absent.length === 1 ? `Book ${absent[0]} isn't in your library` : "Earlier books are missing");
  let mode = buildable.length ? "order" : "alone";
  const option = (k, title, text, disabled) => {
    const input = h("input", { type: "radio", name: "mode", value: k, checked: mode === k, disabled });
    const label = h("label.choice", { "aria-disabled": disabled ? "true" : null }, input,
      h("span", h("span.choice-title", title), h("span.hint", text)));
    input.addEventListener("change", () => { mode = k; paint(); });
    return label;
  };
  const go = h("button.btn.primary", { type: "button" });
  const paint = () => {
    go.replaceChildren(icon("play"), mode === "order" ? `Queue ${buildable.length + 1} books in order` : "Build this book only");
    m.body.querySelectorAll(".choice").forEach((c) => c.classList.toggle("on", c.querySelector("input").checked));
  };
  const list = h("ul.list", [
    ...missing.map((x) => h("li.series-row", h("span.subtle.num", `${x.index}.`), h("span", x.title),
      x.canBuild ? h("span.status-chip.missing", "No index") : h("span.chip", "No EPUB to index"))),
    ...absent.map((n) => h("li.series-row", h("span.subtle.num", `${n}.`), h("span.muted", "Not in your library"),
      h("a", { href: "#/settings" }, "Add it"))),
  ]);
  clear(m.body,
    h("p.muted", `You're building `, h("b", entry.title), `, ${bookNo(entry.series) || "a later book"} of ${entry.series.name}. These earlier books can't be built on yet:`),
    list,
    absent.length ? h("p.hint", "Add missing books as EPUBs (with the series set on their book page) or connect Book Orbit, then build them first.") : null,
    h("fieldset.choices", h("legend.sr-only", "How to build"),
      option("order", "Build the earlier books first (recommended)",
        "They're queued in reading order, then this one. Returning characters keep one entry, and anything a later book overturns is marked as an earlier impression.",
        !buildable.length),
      option("alone", (pre.indexedEarlier || []).length ? "Build this book with what's there" : "Build this book on its own",
        (pre.indexedEarlier || []).length
          ? `It builds on book ${pre.indexedEarlier.join(", ")}. Anything introduced in the missing book${missing.length + absent.length === 1 ? "" : "s"} appears as if new. You can rebuild later.`
          : "Faster, but characters from earlier books appear as if new and nothing links back. You can rebuild once the earlier books have indexes.")),
    h("div.toolbar", { style: { justifyContent: "flex-end" } }, h("button.btn", { type: "button", on: { click: m.close } }, "Cancel"), go));
  paint();
  go.addEventListener("click", () => {
    m.close();
    if (mode === "alone") openConfigure(entry);
    else buildDialog([...buildable.map((b) => ({ key: b.key, title: b.title })), { key: entry.key, title: entry.title }]);
  });
}

/** Single book: make sure the file is local, then hand off to the full configure screen. */
export async function openConfigure(entry) {
  if (!entry.canBuild) return toast("There's no EPUB for this book, so there's no text to index.", "error");
  const done = entry.source === "bookorbit" && !entry.localId
    ? toast(`Fetching the text of “${entry.title}” from Book Orbit. Large books with audio take up to a minute.`, "busy")
    : () => {};
  try {
    const { localId } = await api.prepare(entry.key);
    done();
    location.hash = `#/build/${localId}?catalog=${encodeURIComponent(entry.key)}`;
  } catch (e) {
    done();
    toast(e.message, "error");
  }
}

/** Queue several books, in the order given, with one set of settings. */
export async function buildDialog(books, { title } = {}) {
  const m = modal(title || `Build ${books.length} indexes in order`, { width: 560 });
  const cfg = { provider: "ollama", model: null, depth: "standard", verify: true };
  // Configured cloud providers (Claude, OpenAI, Gemini, ...) from /api/providers, shaped like Ollama's model list.
  const clouds = {};
  let throughput = {};
  try {
    const r = await api.providers();
    throughput = r.throughput || {};
    for (const p of r.providers) {
      if (!p.configured) continue;
      const models = [...p.models, ...p.liveModels].map((m) => ({ name: m.id, label: m.label }));
      if (p.default && !models.some((m) => m.name === p.default)) models.unshift({ name: p.default, label: p.default });
      clouds[p.id] = { ...p, models, throughput };
    }
  } catch {}
  const modelSel = h("select.select", { id: "bd-model" });
  const est = h("p.hint", { role: "status" });
  const go = h("button.btn.primary", { type: "button" }, icon("play"), "Queue builds");
  const words = books.reduce((a, b) => a + (b.words || 0), 0);
  const fill = () => {
    const cloud = cfg.provider !== "ollama";
    const src = cloud ? clouds[cfg.provider] : appState.ollama;
    const models = src?.models || [];
    clear(modelSel, models.length ? models.map((x) => h("option", { value: x.name, selected: x.name === (cfg.model || src.default) },
      cloud ? x.label : x.name)) : h("option", { value: "" }, "No models"));
    cfg.model = modelSel.value || null;
    const unavailable = cloud ? (src && !src.ok ? src.error : "") : (!appState.ollama?.ok ? "Ollama isn't running." : "");
    const tp = (src?.throughput || {})[cfg.model];
    est.textContent = unavailable || (words && tp ? `About ${fmtDuration(words / tp)} for ${fmtInt(words)} words.`
      : cloud ? "Runs one after another, in this order. A few minutes a book." : "Runs one after another, in this order.");
    go.disabled = Boolean(unavailable) || !cfg.model;
  };
  const seg = (items, key) => h("div.seg", { role: "group" }, items.map(([v, label]) => {
    const b = h("button", { type: "button", "aria-pressed": String(cfg[key] === v) }, label);
    b.addEventListener("click", () => {
      cfg[key] = v; if (key === "provider") cfg.model = null;
      b.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      fill();
    });
    return b;
  }));
  modelSel.addEventListener("change", () => { cfg.model = modelSel.value; fill(); });
  const verify = h("input", { type: "checkbox", checked: true, id: "bd-verify", role: "switch" });
  verify.addEventListener("change", () => { cfg.verify = verify.checked; });
  clear(m.body,
    h("ol.list.ordered", books.map((b, i) => h("li.series-row", h("span.subtle.num", `${i + 1}.`), h("span", b.title)))),
    h("div.field", h("span.label", "Runs on"), seg([["ollama", "Ollama (local)"], ...Object.values(clouds).map((c) => [c.id, c.label])], "provider"),
      Object.keys(clouds).length ? null : h("p.hint", "For a cloud model, ", h("a", { href: "#/settings" }, "add a key in Settings"), ".")),
    h("div.field", h("label", { for: "bd-model" }, "Model"), modelSel),
    h("div.field", h("span.label", "Depth"), seg([["light", "Light"], ["standard", "Standard"], ["deep", "Deep"]], "depth")),
    h("label.switch", { for: "bd-verify" }, verify, h("span", h("span.label", { style: { display: "block" } }, "Fact-check every claim"),
      h("span.hint", "Recommended. Rejects unsupported claims and moves early ones later."))),
    est,
    h("div.toolbar", { style: { justifyContent: "flex-end" } }, h("button.btn", { type: "button", on: { click: m.close } }, "Cancel"), go));
  fill();
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const { jobs } = await api.buildMany(books.map((b) => b.key), cfg);
      m.close();
      toast(`Queued ${jobs.length} build${jobs.length === 1 ? "" : "s"}`);
      location.hash = `#/studio/job/${jobs[0].id}`;
    } catch (e) {
      toast(e.message, "error");
      go.disabled = false;
    }
  });
}

/** A deterministic book-cloth colour for books without cover art. */
const CLOTH = ["#5B1414", "#2E2A3B", "#7A3A12", "#8C1D18", "#1F3A4D", "#33402A", "#4A3B2A", "#1E1B1B", "#5E6B4E", "#3B2F4F", "#6B2E3E", "#24434A"];
export function clothColor(title) {
  let n = 0;
  for (const ch of title || "") n = (n * 31 + ch.charCodeAt(0)) >>> 0;
  return CLOTH[n % CLOTH.length];
}

/** Cover art: Book Orbit's thumbnail when there is one, else a cloth-bound placeholder with the title. */
export function cover(entry, size = "md") {
  const c = clothColor(entry.title);
  const el = h(`span.cover.cover-${size}`, { style: { background: `linear-gradient(160deg, ${c}, color-mix(in srgb, ${c} 45%, black))` }, "aria-hidden": "true" });
  if (entry.cover) {
    const img = h("img", { src: entry.cover, alt: "", loading: "lazy" });
    img.addEventListener("load", () => el.classList.add("has-img"));
    img.addEventListener("error", () => img.remove());
    el.append(img);
  }
  if (size === "md" || size === "lg") el.append(h("span.cover-title", entry.title), h("span.cover-author", (entry.authors || []).join(", ")));
  return el;
}

export const STATUS = {
  missing: { label: "No index", cls: "missing" },
  partial: { label: "Partial", cls: "partial" },
  building: { label: "Building", cls: "building" },
  complete: { label: "Complete", cls: "complete" },
};
export const statusChip = (e) => {
  const partial = e.status === "partial" && e.coverage != null;
  return h(`span.status-chip.${STATUS[e.status].cls}`, partial ? { style: { "--pct": `${Math.max(0, Math.min(100, e.coverage))}%` } } : null,
    e.status === "building" && e.job?.progress?.chunks ? `Building, ${e.job.progress.index ?? 0} of ${e.job.progress.chunks}`
      : partial ? `Partial, ${e.coverage}%` : STATUS[e.status].label);
};

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];
/** Small whole numbers as words ("two"), anything else as written ("2.5"). */
export const numWord = (n) => (Number.isInteger(Number(n)) && WORDS[Number(n)] && String(n).trim() !== "" ? WORDS[Number(n)] : String(n));
/** "book two" for a series entry, or "" when its number is unknown. */
export const bookNo = (series) => (series && series.index != null && series.index !== "" ? `book ${numWord(series.index)}` : "");
