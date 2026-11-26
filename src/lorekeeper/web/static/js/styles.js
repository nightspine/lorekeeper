// Picture styles: the gallery of previews every style choice goes through (the picture sheet, the Add pictures dialog,
// the build form's Pictures group, Settings), and the form that saves a custom style with its own preview.
//
// Every built-in style has a preview shipped with the app, all of one shared scene, so the styles compare like for like;
// a custom style saved on this server may draw its own preview of the same scene. Names and descriptions of custom styles
// are the person's own text: they only ever go through h(). Preview files come only from the two URL shapes checked here.
import { api } from "./api.js";
import { artDefaults, fitQuality, imagePrice, imageProviders, modalToasts, modelFor, money, STYLES } from "./art.js";
import { confirmDialog } from "./build.js";
import { clear, h, icon, loading, sheet, showModalLocked, closeLocked, toast } from "./dom.js";

const SLUG_RE = /^[a-z0-9-]{1,64}$/;
const BUILTIN_PREVIEW = /^\/static\/styles\/[a-z0-9-]{1,64}\.webp$/;
const CUSTOM_PREVIEW = /^\/api\/styles\/[a-z0-9-]{1,64}\/preview(\?v=[A-Za-z0-9._:%+-]{1,64})?$/;
export const NAME_MAX = 40, TEXT_MAX = 300;
/** The shape a preview is drawn in (every preview is square): what its price is quoted for. */
const PREVIEW_SHAPE = "square";

const squash = (t) => String(t ?? "").split(/\s+/).filter(Boolean).join(" ");

/** One style as the server lists it, checked; null when it is not one. */
function clean(x) {
  if (!x || typeof x !== "object" || typeof x.slug !== "string" || !SLUG_RE.test(x.slug)) return null;
  const builtin = x.builtin === true;
  const p = typeof x.preview === "string" ? x.preview : null;
  const preview = p && (builtin ? BUILTIN_PREVIEW : CUSTOM_PREVIEW).test(p) ? p : null;
  const name = typeof x.name === "string" && x.name.trim() ? x.name.trim().slice(0, 80) : x.slug.replace(/-/g, " ");
  return { slug: x.slug, name, text: typeof x.text === "string" ? squash(x.text) : "", builtin, preview, stale: x.stale === true };
}
/** The built-in styles alone, with their shipped previews: an older server with no /api/styles. */
const fallback = () => STYLES.map(([slug, name]) => ({ slug, name, text: "", builtin: true, preview: `/static/styles/${slug}.webp`, stale: false }));

let cache = null;   // { at, promise } of the list
const known = new Map();   // slug → style, from the last list: names and sentences for code that cannot wait
/** Every style, built-in ones first. Never fails: an older server or a failed request gives the built-in ones. */
export function loadStyles({ fresh = false } = {}) {
  if (fresh || !cache || Date.now() - cache.at > 60000) {
    const promise = api.styles().then((r) => {
      const list = (Array.isArray(r?.styles) ? r.styles : []).map(clean).filter(Boolean);
      return list.length ? list : fallback();
    }).catch(() => fallback()).then((list) => {
      // Built-ins first whatever order arrived, each slug once.
      const seen = new Set(), out = [];
      for (const s of [...list.filter((x) => x.builtin), ...list.filter((x) => !x.builtin)]) if (!seen.has(s.slug)) { seen.add(s.slug); out.push(s); }
      known.clear();
      for (const s of out) known.set(s.slug, s);
      return out;
    });
    cache = { at: Date.now(), promise };
  }
  return cache.promise;
}
/** A saved style by slug from the last list (null before it arrived, or for a style that is not saved here). */
export const savedStyle = (slug) => known.get(slug) || null;
/** A saved custom style's name ("Linocut"), else null. */
export const galleryName = (slug) => { const s = known.get(slug); return s && !s.builtin ? s.name : null; };
/** The style sentence of a saved style (a custom one's own words), else null. */
export const gallerySentence = (slug) => { const s = known.get(slug); return s && s.text ? s.text : null; };

// ---- the preview viewer ------------------------------------------------------------------------------

/** The preview at full size, with the style's name and its sentence under it. */
export function viewStyle(s) {
  if (!s?.preview) return;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const closeBtn = h("button.icon-btn.lb-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const img = h("img.lb-img", { src: s.preview, alt: `The preview scene in ${s.name}`, decoding: "async", draggable: "false" });
  img.addEventListener("error", () => img.replaceWith(h("p.style-view-gone", "The preview could not be loaded.")));
  const dlg = h("dialog.lightbox.style-view", { "aria-labelledby": "style-view-title" },
    h("div.lb-panel",
      h("header.lb-head", h("h2#style-view-title.style-view-title", s.name), closeBtn),
      h("div.lb-main", h("div.lb-stage", img)),
      h("div.lb-foot",
        s.text ? h("p.style-view-text", s.text) : null,
        s.stale ? h("p.hint", "Preview is of the old wording.") : null)));
  const close = () => closeLocked(dlg);
  dlg.addEventListener("close", () => { dlg.remove(); if (returnTo?.isConnected) returnTo.focus({ preventScroll: true }); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  closeBtn.addEventListener("click", close);
  document.body.append(dlg);
  showModalLocked(dlg);
  closeBtn.focus({ preventScroll: true });
}

// ---- the grid ----------------------------------------------------------------------------------------

/** A card's picture: the preview, or the style's sentence on --pane-2 when it has none (or the file fails to load). */
function previewOf(s, cls = "style-prev") {
  const words = () => h(`span.${cls}.no-pic`, h("span.style-words", s.text || s.name));
  if (!s.preview) return words();
  const box = h(`span.${cls}`);
  const img = h("img", { src: s.preview, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => box.replaceWith(words()));
  box.append(img);
  return box;
}

/**
 * The styles as a grid of cards: preview and name, the chosen one ringed, an expand button on each card with a preview,
 * and a last card "New style". `opts`:
 *   value          the chosen slug
 *   extra          styles not saved here that may still be chosen (an art set's own custom style): [{ slug, name, text }]
 *   onPick(style)  a card was chosen
 *   manage         Settings: custom cards get Edit and Delete
 *   pickNew        choose a style as soon as it is saved (true in the sheet and dialogs, false in Settings)
 *   onChanged()    after a style was saved, edited or deleted
 * Returns { el, set(value), refresh() }.
 */
export function stylePicker({ value = null, extra = [], onPick = null, manage = false, pickNew = true, onChanged = null } = {}) {
  const el = h("div.style-grid", { role: "group", "aria-label": "Styles" });
  let list = null, chosen = value;
  const extras = () => (extra || []).filter((x) => x && typeof x.slug === "string" && !(list || []).some((s) => s.slug === x.slug))
    .map((x) => ({ slug: x.slug, name: x.name || x.slug.replace(/-/g, " "), text: squash(x.text), builtin: false, preview: null, stale: false, legacy: true }));
  function card(s) {
    const on = s.slug === chosen;
    const pick = h("button.style-pick", { type: "button", "aria-pressed": String(on), on: { click: () => { chosen = s.slug; paint(); onPick?.(s); } } },
      previewOf(s), h("span.style-name", s.name));
    const expand = s.preview ? h("button.icon-btn.style-expand", { type: "button", "aria-label": `Show ${s.name} larger`, title: "Show larger",
      on: { click: () => viewStyle(s) } }, icon("expand")) : null;
    const acts = manage && !s.builtin && !s.legacy ? h("div.style-acts",
      h("button.linkish", { type: "button", "aria-label": `Edit ${s.name}`, on: { click: () => editStyle(s) } }, "Edit"),
      h("button.linkish.danger", { type: "button", "aria-label": `Delete ${s.name}`, on: { click: () => removeStyle(s) } }, "Delete")) : null;
    return h(`div.style-card${on ? ".on" : ""}`, { dataset: { slug: s.slug } }, pick, expand,
      s.stale ? h("p.style-stale", "Preview is of the old wording") : null, acts);
  }
  function paint() {
    if (!list) { clear(el, loading("Loading styles…")); return; }
    const focusSlug = el.contains(document.activeElement) ? document.activeElement.closest(".style-card")?.dataset.slug : null;
    const focusNew = el.contains(document.activeElement) && document.activeElement.classList.contains("style-new");
    clear(el, [...list, ...extras()].map(card),
      h("button.style-card.style-new", { type: "button", on: { click: () => newStyle() } },
        h("span.style-prev.no-pic", icon("plus")), h("span.style-name", "New style")));
    if (focusSlug) el.querySelector(`.style-card[data-slug="${CSS.escape(focusSlug)}"] .style-pick`)?.focus({ preventScroll: true });
    else if (focusNew) el.querySelector(".style-new")?.focus({ preventScroll: true });
  }
  async function refresh(fresh = true) { list = await loadStyles({ fresh }); paint(); }
  function newStyle() {
    openStyleForm({ onSaved: async (s) => { await refresh(); onChanged?.(); if (pickNew && s) { chosen = s.slug; paint(); onPick?.(savedStyle(s.slug) || s); } } });
  }
  function editStyle(s) { openStyleForm({ style: s, onSaved: async () => { await refresh(); onChanged?.(); } }); }
  async function removeStyle(s) {
    if (!(await confirmDialog({ title: `Delete the style ${s.name}?`, message: "It can no longer be chosen for new pictures. Pictures already drawn in it stay.", confirmLabel: "Delete style" }))) return;
    try {
      await api.styleDelete(s.slug);
      toast("Style deleted", "ok", { host: modalToasts() });
      await refresh();
      onChanged?.(s);
    } catch (e) { toast(e.message, "error", { host: modalToasts() }); }
  }
  paint();
  loadStyles().then((l) => { list = l; paint(); });
  return { el, set: (v) => { chosen = v; paint(); }, refresh };
}

/**
 * The compact row the sheet and the dialogs show: the chosen style's thumbnail and name, opening the grid as a bottom
 * sheet titled "Styles". Choosing a card closes it. `opts` as stylePicker, plus `disabled` and `id`.
 * Returns { el, set(value) }.
 */
export function styleRow({ value = null, extra = [], onPick = null, disabled = false, id = null } = {}) {
  let chosen = value;
  const btn = h("button.style-row", { type: "button", id, disabled, "aria-haspopup": "dialog" });
  const find = () => savedStyle(chosen) || (extra || []).map((x) => ({ slug: x.slug, name: x.name || x.slug, text: squash(x.text), preview: null })).find((x) => x.slug === chosen)
    || { slug: chosen || "", name: chosen ? chosen.replace(/-/g, " ") : "Choose a style", text: "", preview: null };
  function paint() {
    const s = find();
    clear(btn, previewOf(s, "style-row-prev"), h("span.style-row-name", h("span.sr-only", "Style: "), s.name), h("span.style-row-change", "Change", icon("chevron")));
  }
  btn.addEventListener("click", () => {
    let sh = null;
    const picker = stylePicker({ value: chosen, extra, onPick: (s) => { chosen = s.slug; paint(); onPick?.(s); sh?.close(true); } });
    sh = sheet({ title: "Styles", content: picker.el, className: "style-sheet", stack: true });
    // Focus the chosen card once the list is there (the title holds it until then).
    loadStyles().then(() => { if (sh.el.open) (sh.el.querySelector(".style-card.on .style-pick") || sh.el.querySelector(".style-pick"))?.focus({ preventScroll: true }); });
  });
  paint();
  loadStyles().then(paint);
  return { el: btn, set: (v) => { chosen = v; paint(); } };
}

// ---- the custom style form ---------------------------------------------------------------------------

/**
 * "New style" or "Edit style": a name, a description, "Draw a preview" with its price, and Save. Drawing a preview saves
 * the style first (the preview is drawn for a saved style). `onSaved(style)` runs after it was saved, once, when it closes.
 */
export function openStyleForm({ style = null, onSaved = null } = {}) {
  let saved = style ? { ...style } : null, busy = false, changed = false;
  const nameIn = h("input.input#style-name", { value: saved?.name || "", maxlength: String(NAME_MAX), autocomplete: "off", "aria-describedby": "style-name-err" });
  const textIn = h("textarea.input#style-text", { rows: "4", maxlength: String(TEXT_MAX), "aria-describedby": "style-text-hint style-text-err",
    placeholder: "For example: a linocut print in two colours, bold carved lines" });
  textIn.value = saved?.text || "";
  const nameErr = h("p.hint.field-error#style-name-err", { role: "alert" });
  const textErr = h("p.hint.field-error#style-text-err", { role: "alert" });
  const prevBox = h("div.style-form-prev");
  const prevErr = h("p.hint.field-error", { role: "alert" });
  const drawBtn = h("button.btn", { type: "button" }, icon("sparkle"), "Draw a preview");
  const price = h("span.style-form-price.subtle", { role: "status" });
  const saveBtn = h("button.btn.primary", { type: "button" }, "Save");
  const count = h("span.hint.num", { "aria-live": "off" });
  let provider = null;

  const dirty = () => squash(nameIn.value) !== (saved?.name || "") || squash(textIn.value) !== (saved?.text || "");
  const sh = sheet({ title: style ? `Edit style: ${style.name}` : "New style", className: "style-form-sheet", stack: true, focus: nameIn,
    content: h("div.style-form",
      h("div.field", h("label.label", { for: "style-name" }, "Name"), nameIn, nameErr),
      h("div.field", h("label.label", { for: "style-text" }, "Description"), textIn,
        h("p.hint#style-text-hint", "Added after every prompt drawn in this style. Describe how it looks, not an artist's name. ", count), textErr),
      h("div.field", h("span.label", "Preview"), prevBox, h("div.style-form-draw", drawBtn, price), prevErr)),
    footer: h("div.style-form-foot", saveBtn),
    beforeClose: () => {
      if (busy) return false;
      if (!dirty()) return true;
      confirmDialog({ title: "Discard your changes?", message: "What you typed for this style is not saved.", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { nameIn.value = saved?.name || ""; textIn.value = saved?.text || ""; sh.close(true); } });
      return false;
    },
    onClose: () => { if (changed && saved) onSaved?.(saved); } });

  function paintCount() { count.textContent = `${textIn.value.length} of ${TEXT_MAX}`; }
  function paintPreview(drawing = false) {
    if (drawing) { clear(prevBox, loading("Drawing a preview…")); return; }
    clear(prevBox, saved?.preview
      ? h("button.style-form-img", { type: "button", "aria-label": `Show the preview of ${saved.name} larger`, on: { click: () => viewStyle(saved) } }, previewOf(saved))
      : h("p.hint", "No preview yet. A preview draws the same scene every built-in style shows, in this style."),
    saved?.preview && (saved.stale || (saved && squash(textIn.value) !== saved.text)) ? h("p.style-stale", "Preview is of the old wording") : null);
  }
  function paintDraw() {
    drawBtn.disabled = busy || !provider;
    saveBtn.disabled = busy;
    if (!provider) { clear(price, "Previews need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one")); return; }
    clear(price, provider.line);
  }
  nameIn.addEventListener("input", () => { nameErr.textContent = ""; });
  textIn.addEventListener("input", () => { textErr.textContent = ""; paintCount(); paintPreview(); });

  // The image provider and model the Settings defaults name, at their quality: what the preview is drawn and priced with.
  imageProviders().then((provs) => {
    const d = artDefaults();
    const p = provs.find((x) => x.id === d.provider) || provs[0] || null;
    if (p) {
      const model = modelFor(p, p.id === d.provider ? d.model : null);
      const quality = fitQuality(p, model, d.quality);
      const pr = imagePrice(p, quality, PREVIEW_SHAPE, model);
      provider = { id: p.id, model, quality, line: pr === undefined ? "" : pr == null ? "Price unknown" : money(pr)[0].toUpperCase() + money(pr).slice(1) };
    }
    if (sh.el.isConnected) paintDraw();
  });

  /** Check the fields, then create the style or save its changes. True when it is saved as typed. */
  async function store() {
    const name = squash(nameIn.value), text = squash(textIn.value);
    nameErr.textContent = textErr.textContent = "";
    if (!name) { nameErr.textContent = "Give the style a name."; nameIn.focus(); return false; }
    if (!text) { textErr.textContent = "Describe the style."; textIn.focus(); return false; }
    if (saved && !dirty()) return true;
    try {
      const r = saved ? await api.styleUpdate(saved.slug, { name, text }) : await api.styleCreate({ name, text });
      const back = clean(r?.style || r);
      const list = await loadStyles({ fresh: true });
      saved = list.find((s) => s.slug === (back?.slug || saved?.slug)) || back || saved;
      if (!saved) throw new Error("The server did not say which style it saved.");
      nameIn.value = saved.name; textIn.value = saved.text || text;
      changed = true;
      sh.setTitle(`Edit style: ${saved.name}`);
      return true;
    } catch (e) {
      if (e.status === 409) { nameErr.textContent = e.message && e.message !== "Conflict" ? e.message : "A style with this name already exists."; nameIn.focus(); }
      else { textErr.textContent = e.message; textIn.focus(); }
      return false;
    }
  }
  drawBtn.addEventListener("click", async () => {
    if (busy || !provider) return;
    busy = true; prevErr.textContent = ""; paintDraw();
    try {
      if (!(await store())) return;
      paintPreview(true);
      const body = { provider: provider.id, ...(provider.model ? { model: provider.model } : {}), quality: provider.quality };
      const r = await api.stylePreview(saved.slug, body);
      const back = clean(r?.style || r);
      const list = await loadStyles({ fresh: true });
      saved = list.find((s) => s.slug === saved.slug) || (back?.slug === saved.slug ? back : saved);
      changed = true;
    } catch (e) {
      prevErr.textContent = e.code === "refused" ? `The provider would not draw it: ${e.message}` : e.message;
    } finally {
      busy = false; paintDraw(); if (sh.el.isConnected) paintPreview();
    }
  });
  saveBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true; paintDraw();
    const ok = await store();
    busy = false; paintDraw();
    if (ok) { sh.close(true); toast(style ? "Style saved" : "Style added", "ok", { host: modalToasts() }); }
  });
  paintCount(); paintPreview(); paintDraw();
  return sh;
}
