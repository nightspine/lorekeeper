// Picture styles: the gallery of previews every style choice goes through (the picture sheet, the Add pictures dialog,
// the build form's Pictures group, Settings), and the form that saves a custom style with its own preview.
//
// Every built-in style has a preview shipped with the app, all of one shared scene, so the styles compare like for like;
// a custom style saved on this server may draw its own preview of the same scene. Names and descriptions of custom styles
// are the person's own text: they only ever go through h(). Preview files come only from the two URL shapes checked here.
import { api } from "./api.js";
import { artDefaults, fitQuality, imagePrice, imageProviders, modalToasts, modelFor, modelProviders, money, STYLES, writerFact } from "./art.js";
import { confirmDialog } from "./build.js";
import { clear, h, icon, loading, sheet, showModalLocked, closeLocked, toast } from "./dom.js";
import { describedLine, REF_ACCEPT, refProblem, sniff } from "./styleref.js";

const SLUG_RE = /^[a-z0-9-]{1,64}$/;
const BUILTIN_PREVIEW = /^\/static\/styles\/[a-z0-9-]{1,64}\.webp$/;
const CUSTOM_PREVIEW = /^\/api\/styles\/[a-z0-9-]{1,64}\/preview(\?v=[A-Za-z0-9._:%+-]{1,64})?$/;
/** A custom style's reference picture: the only URL shape a reference is shown from. */
const REFERENCE = /^\/api\/styles\/[a-z0-9-]{1,64}\/reference(\?v=[0-9a-f]{1,64})?$/;
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
  // A reference picture only on a custom style, and only from its own reference URL.
  const reference = !builtin && typeof x.reference === "string" && REFERENCE.test(x.reference)
    && x.reference.startsWith(`/api/styles/${x.slug}/reference`) ? x.reference : null;
  return { slug: x.slug, name, text: typeof x.text === "string" ? squash(x.text) : "", builtin, preview, stale: x.stale === true,
    reference, useReference: Boolean(reference) && x.useReference === true };
}
/** The built-in styles alone, with their shipped previews: an older server with no /api/styles. */
const fallback = () => STYLES.map(([slug, name]) => ({ slug, name, text: "", builtin: true, preview: `/static/styles/${slug}.webp`, stale: false, reference: null, useReference: false }));

let cache = null;   // { at, promise } of the list
let lastGood = null;   // the last list the server answered: what a failed refresh keeps showing
const known = new Map();   // slug → style, from the last list: names and sentences for code that cannot wait
/** Every style, built-in ones first. Never fails: an older server or a failed request gives the built-in ones. */
export function loadStyles({ fresh = false } = {}) {
  if (fresh || !cache || Date.now() - cache.at > 60000) {
    // A failed request (or an empty answer) gives the built-in styles for now and is not kept: the next open asks again.
    let ok = true;
    const promise = api.styles().then((r) => {
      const list = (Array.isArray(r?.styles) ? r.styles : []).map(clean).filter(Boolean);
      if (!list.length) ok = false;
      return list.length ? list : lastGood || fallback();
    }).catch(() => { ok = false; return lastGood || fallback(); }).then((list) => {
      // Built-ins first whatever order arrived, each slug once.
      const seen = new Set(), out = [];
      for (const s of [...list.filter((x) => x.builtin), ...list.filter((x) => !x.builtin)]) if (!seen.has(s.slug)) { seen.add(s.slug); out.push(s); }
      if (ok) { known.clear(); lastGood = out; }
      for (const s of out) known.set(s.slug, s);
      if (!ok && cache?.promise === promise) cache = null;
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
  if (!s?.preview && !s?.reference) return;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const closeBtn = h("button.icon-btn.lb-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  // The stage shows the preview; a style with only a reference picture shows that instead.
  const main = s.preview || s.reference;
  const img = h("img.lb-img", { src: main, alt: s.preview ? `The preview scene in ${s.name}` : `Your reference picture for ${s.name}`, decoding: "async", draggable: "false" });
  img.addEventListener("error", () => img.replaceWith(h("p.style-view-gone", s.preview ? "The preview could not be loaded." : "The picture could not be loaded.")));
  let refFig = null;
  if (s.preview && s.reference) {
    const ri = h("img", { src: s.reference, alt: `Your reference picture for ${s.name}`, loading: "lazy", decoding: "async", draggable: "false" });
    refFig = h("figure.style-view-ref", h("span.style-view-ref-img", ri), h("figcaption", "Your reference picture"));
    ri.addEventListener("error", () => refFig.remove());
  }
  const refLine = s.reference ? h("p.hint", s.useReference
    ? (s.preview ? "Your picture is sent with every drawing in this style." : "Your reference picture. It is sent with every drawing in this style.")
    : (s.preview ? "Your picture is kept with this style but not sent with drawings." : "Your reference picture. It is kept with this style but not sent with drawings.")) : null;
  const dlg = h("dialog.lightbox.style-view", { "aria-labelledby": "style-view-title" },
    h("div.lb-panel",
      h("header.lb-head", h("h2#style-view-title.style-view-title", s.name), closeBtn),
      h("div.lb-main", h("div.lb-stage", img)),
      h("div.lb-foot",
        s.text ? h("p.style-view-text", s.text) : null,
        s.preview && s.stale ? h("p.hint", "Preview is of the old wording.") : null,
        refFig, refLine)));
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
function previewOf(s, cls = "style-prev", { marks = false } = {}) {
  const words = () => h(`span.${cls}.no-pic`, h("span.style-words", s.text || s.name));
  // Without a preview a style's own reference picture stands in, marked as the person's (on the cards).
  const mine = () => {
    if (!s.reference) return words();
    const box = h(`span.${cls}.ref-pic`);
    const img = h("img", { src: s.reference, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
    img.addEventListener("error", () => box.replaceWith(words()));
    box.append(img, marks ? h("span.style-mine", "Your picture") : null);
    return box;
  };
  if (!s.preview) return mine();
  const box = h(`span.${cls}`);
  const img = h("img", { src: s.preview, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => box.replaceWith(mine()));
  box.append(img);
  if (marks && s.reference) {
    // The preview, with the person's reference picture small in a corner.
    const corner = h("span.style-ref-corner", { title: "Your picture" });
    const ci = h("img", { src: s.reference, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
    ci.addEventListener("error", () => corner.remove());
    corner.append(ci, h("span.sr-only", "Has your reference picture"));
    box.append(corner);
  }
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
    .map((x) => ({ slug: x.slug, name: x.name || x.slug.replace(/-/g, " "), text: squash(x.text), builtin: false, preview: null, stale: false, reference: null, useReference: false, legacy: true }));
  function card(s) {
    const on = s.slug === chosen;
    const pick = h("button.style-pick", { type: "button", "aria-pressed": String(on), on: { click: (e) => { e.currentTarget.focus({ preventScroll: true }); chosen = s.slug; paint(); onPick?.(s); } } },
      previewOf(s, "style-prev", { marks: true }), h("span.style-name", s.name));
    const expand = s.preview || s.reference ? h("button.icon-btn.style-expand", { type: "button", "aria-label": `Show ${s.name} larger`, title: "Show larger",
      // Focused first: Safari does not focus a button on click, and the viewer gives focus back to it.
      on: { click: (e) => { e.currentTarget.focus({ preventScroll: true }); viewStyle(s); } } }, icon("expand")) : null;
    const acts = manage && !s.builtin && !s.legacy ? h("div.style-acts",
      h("button.linkish", { type: "button", "aria-label": `Edit ${s.name}`, on: { click: (e) => { e.currentTarget.focus({ preventScroll: true }); editStyle(s); } } }, "Edit"),
      h("button.linkish.danger", { type: "button", "aria-label": `Delete ${s.name}`, on: { click: () => removeStyle(s) } }, "Delete")) : null;
    return h(`div.style-card${on ? ".on" : ""}`, { dataset: { slug: s.slug } }, pick, expand,
      s.stale ? h("p.style-stale", "Preview is of the old wording") : null, acts);
  }
  function paint() {
    if (!list) { clear(el, loading("Loading styles…")); return; }
    const focusSlug = el.contains(document.activeElement) ? document.activeElement.closest(".style-card")?.dataset.slug : null;
    const focusNew = el.contains(document.activeElement) && document.activeElement.classList.contains("style-new");
    clear(el, [...list, ...extras()].map(card),
      h("button.style-card.style-new", { type: "button", on: { click: (e) => { e.currentTarget.focus({ preventScroll: true }); newStyle(); } } },
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
    btn.focus({ preventScroll: true });   // Safari does not focus a button on click; the sheet gives focus back to it
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

/** Read a chosen file far enough to check it: its kind from the first bytes, then its size in pixels once decoded
 *  (null when it could not be decoded). Never rejects. */
async function inspect(file) {
  let kind = null;
  try { kind = sniff(new Uint8Array(await file.slice(0, 16).arrayBuffer())); } catch {}
  if (!kind) return { kind, size: file.size };
  const dims = await new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve({ width: img.naturalWidth || null, height: img.naturalHeight || null }); };
    img.onerror = () => { URL.revokeObjectURL(url); resolve({ width: null, height: null }); };
    img.src = url;
  });
  return { kind, size: file.size, ...dims };
}

/**
 * "New style" or "Edit style": a name, an optional reference picture (with "Describe it for me" and "Send this picture
 * with every drawing in this style"), a description, "Draw a preview" with its price, and Save. Save stores the style,
 * then sends a chosen picture; drawing a preview does the same first (the preview is drawn for a saved style).
 * `onSaved(style)` runs after it was saved, once, when it closes.
 */
export function openStyleForm({ style = null, onSaved = null } = {}) {
  let saved = style ? { ...style } : null, busy = false, changed = false;
  // A picture chosen here and not sent yet: { file, url } (url an object URL for the thumbnail, revoked when it goes).
  let pending = null;
  let describing = null;   // the AbortController of a describe call in flight
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

  // The reference picture.
  const fileIn = h("input.sr-only#style-ref-file", { type: "file", accept: REF_ACCEPT, tabindex: "-1", "aria-hidden": "true" });
  const refBox = h("div.style-ref");
  const refErr = h("p.hint.field-error#style-ref-err", { role: "alert" });
  const useIn = h("input#style-ref-use", { type: "checkbox", checked: saved?.reference ? saved.useReference : true, "aria-describedby": "style-ref-use-hint" });
  const useRow = h("div.style-ref-use",
    h("label.style-ref-check", { for: "style-ref-use" }, useIn, h("span", "Send this picture with every drawing in this style")),
    h("p.hint#style-ref-use-hint", "It goes to the image provider with each picture."));
  const describeBtn = h("button.linkish.style-describe", { type: "button" }, icon("sparkle"), "Describe it for me");
  const describeLine = h("p.hint.style-describe-line", { role: "status" });

  const hasPicture = () => Boolean(pending || saved?.reference);
  const savedUse = () => (saved?.reference ? saved.useReference : true);
  const dirty = () => squash(nameIn.value) !== (saved?.name || "") || squash(textIn.value) !== (saved?.text || "")
    || Boolean(pending) || (hasPicture() && useIn.checked !== savedUse());
  const dropPending = () => { if (pending) URL.revokeObjectURL(pending.url); pending = null; };

  const sh = sheet({ title: style ? `Edit style: ${style.name}` : "New style", className: "style-form-sheet", stack: true, focus: nameIn,
    content: h("div.style-form",
      h("div.field", h("label.label", { for: "style-name" }, "Name"), nameIn, nameErr),
      h("div.field", h("span.label#style-ref-label", "Reference picture"), fileIn, refBox, refErr, useRow),
      h("div.field",
        h("div.style-form-labelrow", h("label.label", { for: "style-text" }, "Description"), describeBtn),
        textIn,
        h("p.hint#style-text-hint", "Added after every prompt drawn in this style. Describe how it looks, not an artist's name. ", count),
        describeLine, textErr),
      h("div.field", h("span.label", "Preview"), prevBox, h("div.style-form-draw", drawBtn, price), prevErr)),
    footer: h("div.style-form-foot", saveBtn),
    beforeClose: () => {
      if (busy) return false;
      if (!dirty()) return true;
      confirmDialog({ title: "Discard your changes?", message: pending ? "What you typed and the picture you chose for this style are not saved." : "What you typed for this style is not saved.",
        confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { nameIn.value = saved?.name || ""; textIn.value = saved?.text || ""; dropPending(); useIn.checked = savedUse(); sh.close(true); } });
      return false;
    },
    onClose: () => { describing?.abort(); dropPending(); if (changed && saved) onSaved?.(saved); } });

  function paintCount() { count.textContent = `${textIn.value.length} of ${TEXT_MAX}`; }
  function paintPreview(drawing = false) {
    if (drawing) { clear(prevBox, loading("Drawing a preview…")); return; }
    clear(prevBox, saved?.preview
      ? h("button.style-form-img", { type: "button", "aria-label": `Show the preview of ${saved.name} larger`, on: { click: () => viewStyle(saved) } }, previewOf({ ...saved, reference: null }))
      : h("p.hint", hasPicture()
        ? "No preview yet. A preview draws the same scene every built-in style shows, in this style, with your picture if it is sent."
        : "No preview yet. A preview draws the same scene every built-in style shows, in this style."),
    saved?.preview && (saved.stale || (saved && squash(textIn.value) !== saved.text)) ? h("p.style-stale", "Preview is of the old wording") : null);
  }
  function paintDraw() {
    drawBtn.disabled = busy || Boolean(describing) || !provider;
    saveBtn.disabled = busy || Boolean(describing);
    if (!provider) { clear(price, "Previews need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one")); return; }
    clear(price, provider.line);
  }
  function paintRef() {
    const focusIn = refBox.contains(document.activeElement) ? document.activeElement.dataset.act : null;
    const choose = (label, act) => h("button.btn", { type: "button", dataset: { act }, disabled: busy, "aria-describedby": "style-ref-err",
      on: { click: () => fileIn.click() } }, act === "choose" ? icon("upload") : null, label);
    const src = pending?.url || saved?.reference || null;
    if (!src) {
      clear(refBox, h("div.style-ref-drop", { "aria-labelledby": "style-ref-label" },
        h("p.style-ref-hint", "Drop a picture in a style you like here, or choose one. PNG, JPEG or WebP, at most 8 MB."),
        choose("Choose a picture", "choose")));
    } else {
      const img = h("img", { src, alt: pending ? "The picture you chose" : `Your reference picture for ${saved.name}`, decoding: "async", draggable: "false" });
      const thumb = h("span.style-ref-thumb", img);
      img.addEventListener("error", () => clear(thumb, h("span.style-words", "The picture could not be shown.")));
      clear(refBox, h("div.style-ref-chosen",
        thumb,
        h("div.style-ref-side",
          pending ? h("p.hint", "Not saved yet. Save sends it.") : null,
          h("div.style-ref-acts",
            choose("Replace", "replace"),
            h("button.btn.quiet", { type: "button", dataset: { act: "remove" }, disabled: busy, on: { click: removeRef } }, "Remove")))));
    }
    useRow.hidden = !src;
    describeBtn.hidden = !src;
    describeBtn.disabled = Boolean(describing) || busy;
    if (focusIn) (refBox.querySelector(`[data-act="${focusIn}"]`) || refBox.querySelector("button"))?.focus({ preventScroll: true });
  }

  /** Check a chosen or dropped file and keep it as the pending picture, or say what is wrong under the field. */
  async function take(file) {
    if (!file) return;
    refErr.textContent = "";
    const problem = refProblem(await inspect(file));
    if (problem) { refErr.textContent = problem; return; }
    const first = !hasPicture();
    dropPending();
    pending = { file, url: URL.createObjectURL(file) };
    if (first) useIn.checked = true;   // on by default once a picture is added
    describeLine.textContent = "";
    paintRef(); paintPreview();
  }
  async function removeRef() {
    refErr.textContent = "";
    if (pending && !saved?.reference) { dropPending(); paintRef(); paintPreview(); refBox.querySelector("button")?.focus({ preventScroll: true }); return; }
    if (!(await confirmDialog({ title: "Remove the reference picture?", message: "Pictures drawn in this style from now on get only its description. Pictures already drawn stay.", confirmLabel: "Remove picture" }))) return;
    dropPending();
    busy = true; paintDraw(); paintRef();
    try {
      await api.styleReferenceDelete(saved.slug);
      await reload();
      changed = true;
      useIn.checked = true;
      toast("Reference picture removed", "ok", { host: sh.toasts });
    } catch (e) { refErr.textContent = e.message; }
    finally { busy = false; paintDraw(); if (sh.el.isConnected) { paintRef(); paintPreview(); refBox.querySelector("button")?.focus({ preventScroll: true }); } }
  }
  fileIn.addEventListener("change", () => { const f = fileIn.files?.[0]; fileIn.value = ""; take(f); });
  refBox.addEventListener("dragover", (e) => {
    if (busy || ![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy"; refBox.classList.add("over");
  });
  refBox.addEventListener("dragleave", (e) => { if (!refBox.contains(e.relatedTarget)) refBox.classList.remove("over"); });
  refBox.addEventListener("drop", (e) => {
    refBox.classList.remove("over");
    if (busy) return;
    e.preventDefault();
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length > 1) { refErr.textContent = "Drop one picture: a style has one reference picture."; return; }
    take(files[0]);
  });
  useIn.addEventListener("change", () => paintPreview());

  /** Fill the Description from the picture: one call to a model that sees pictures. Asks first over typed text. */
  describeBtn.addEventListener("click", async () => {
    if (describing || busy || !hasPicture()) return;
    if (squash(textIn.value) && !(await confirmDialog({ title: "Replace your description?", message: "The description you have now is replaced by one written from the picture.", confirmLabel: "Replace", danger: false }))) return;
    textErr.textContent = ""; describeLine.textContent = "";
    const ctrl = new AbortController();
    describing = ctrl; paintRef(); paintDraw();
    clear(describeLine, loading("Looking at the picture…"));
    try {
      const blob = pending ? pending.file : await fetch(saved.reference, { signal: ctrl.signal }).then((res) => {
        if (!res.ok) throw new Error("Your saved picture could not be read. Choose it again.");
        return res.blob();
      });
      const r = await api.styleDescribe(blob, { signal: ctrl.signal });
      if (ctrl.signal.aborted || !sh.el.isConnected) return;
      const text = squash(r?.text).slice(0, TEXT_MAX);
      if (!text) throw new Error("No description came back. Try again, or write it yourself.");
      textIn.value = text; paintCount(); paintPreview();
      await modelProviders().catch(() => null);   // the model's name, from the provider list
      describeLine.textContent = describedLine(r.model ? writerFact({ provider: r.provider, model: r.model }) : "", r.cost);
    } catch (e) {
      if (ctrl.signal.aborted || e?.name === "AbortError") { describeLine.textContent = ""; return; }
      if (e.code === "no-vision-model") clear(describeLine, "No model that can see pictures is set up. ", h("a", { href: "#/settings/cloud" }, "Add a key in Settings, Cloud models"), ".");
      else if (e.code === "bad-image" || e.status === 413) { describeLine.textContent = ""; refErr.textContent = e.message; }
      else describeLine.textContent = e.code === "model-failed" ? `The model could not describe it: ${e.message}` : e.message;
    } finally {
      if (describing === ctrl) describing = null;
      if (sh.el.isConnected) { paintRef(); paintDraw(); }
    }
  });
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

  /** The saved row again, from a fresh list (else the row a call answered). */
  async function reload(back = null) {
    const slug = back?.slug || saved?.slug;
    const list = await loadStyles({ fresh: true });
    saved = list.find((s) => s.slug === slug) || back || saved;
  }
  /** Check the fields, then create the style or save its changes, then send a chosen picture and the "Send this picture"
   *  choice. True when it is all saved as it stands. */
  async function store() {
    const name = squash(nameIn.value), text = squash(textIn.value);
    nameErr.textContent = textErr.textContent = refErr.textContent = "";
    if (!name) { nameErr.textContent = "Give the style a name."; nameIn.focus(); return false; }
    if (!text) { textErr.textContent = "Describe the style."; textIn.focus(); return false; }
    if (saved && !dirty()) return true;
    const use = useIn.checked;
    try {
      if (!saved || name !== saved.name || text !== (saved.text || "") || (saved.reference && !pending && use !== saved.useReference)) {
        const body = { name, text };
        if (saved?.reference && !pending && use !== saved.useReference) body.useReference = use;
        const r = saved ? await api.styleUpdate(saved.slug, body) : await api.styleCreate(body);
        await reload(clean(r?.style || r));
        if (!saved) throw new Error("The server did not say which style it saved.");
        changed = true;
        nameIn.value = saved.name; textIn.value = saved.text || text;
        sh.setTitle(`Edit style: ${saved.name}`);
      }
    } catch (e) {
      if (e.status === 409) { nameErr.textContent = e.message && e.message !== "Conflict" ? e.message : "A style with this name already exists."; nameIn.focus(); }
      else { textErr.textContent = e.message; textIn.focus(); }
      return false;
    }
    if (pending) {
      try {
        const r = await api.styleReference(saved.slug, pending.file);
        dropPending();
        changed = true;
        await reload(clean(r?.style || r));
        if (saved.reference && saved.useReference !== use) {
          await api.styleUpdate(saved.slug, { useReference: use });
          await reload();
        }
      } catch (e) {
        refErr.textContent = e.message;
        paintRef();
        refBox.querySelector('[data-act="replace"]')?.focus({ preventScroll: true });
        return false;
      }
    }
    useIn.checked = savedUse();
    paintRef();
    return true;
  }
  drawBtn.addEventListener("click", async () => {
    if (busy || !provider) return;
    busy = true; prevErr.textContent = ""; paintDraw(); paintRef();
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
      prevErr.textContent = e.code === "refused" ? `The provider would not draw it: ${String(e.message).replace(/^refused:\s*/i, "")}` : e.message;
    } finally {
      busy = false; paintDraw(); if (sh.el.isConnected) { paintPreview(); paintRef(); }
    }
  });
  saveBtn.addEventListener("click", async () => {
    if (busy) return;
    busy = true; paintDraw(); paintRef();
    const ok = await store();
    busy = false; paintDraw(); paintRef();
    if (ok) { sh.close(true); toast(style ? "Style saved" : "Style added", "ok", { host: modalToasts() }); }
  });
  paintCount(); paintRef(); paintPreview(); paintDraw();
  return sh;
}
