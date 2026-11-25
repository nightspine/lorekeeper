// Pictures: the gating helper, the manifest cache, the picture elements, the lightbox, the "Draw a picture" sheet and the
// Pictures group of the build form. An art set lives beside the index (never in it); the server sends its manifest whole,
// as it sends the index, and the Reader gates every picture by its `from` anchor exactly as it gates names and events.
// Captions, prompts and notes are untrusted text: they only ever go through h(). Picture files are fetched only from
// /api/art/... URLs built here from ids the server sent, never from a path or URL found in the manifest.
import { api } from "./api.js";
import { composePrompt, promptParts, restyle, sentenceFor, squash } from "./artprompt.js";
import { confirmDialog } from "./build.js";
import { clear, foldList, h, icon, loading, sheet, showModalLocked, closeLocked, toast } from "./dom.js";

/** The styles the pipeline knows (art.STYLES), in the order the person sees them. Anything else is a custom style. */
export const STYLES = [
  ["digital-art", "Digital art"], ["comic-strip", "Comic strip"], ["anime", "Anime"], ["realism", "Realism"],
  ["hand-drawn", "Hand-drawn"], ["oil-painting", "Oil painting"], ["epic-sci-fi", "Epic sci-fi"], ["watercolour", "Watercolour"], ["ink", "Ink"],
];
const STYLE_LABEL = Object.fromEntries(STYLES);
export const QUALITIES = [["draft", "Draft"], ["standard", "Standard"], ["best", "Best"]];
export const SHAPES = [["landscape", "Landscape"], ["portrait", "Portrait"], ["square", "Square"]];
/** The picture kind an entry type is drawn as; concepts and terms are not drawn. */
export const KIND_OF_TYPE = { person: "portrait", creature: "portrait", group: "portrait", place: "place", object: "thing" };
const SHAPE_OF = { plate: "landscape", event: "landscape", portrait: "portrait", place: "landscape", thing: "landscape" };
export const ENTITY_KINDS = new Set(["portrait", "place", "thing"]);
/** The image providers' names, for "on Gemini" (the record holds the id). */
const PROVIDER_LABEL = { gemini: "Gemini", openai: "OpenAI" };
/** "Nano Banana Pro on Gemini": the model a picture was drawn with, by name when the server has listed it. */
const modelFact = (r) => [modelLabel(r.provider, r.model), r.provider ? `on ${PROVIDER_LABEL[r.provider] || r.provider}` : null].filter(Boolean).join(" ");

/** A style slug as words: the known ones by name, a custom one as the person wrote it (the manifest's `style`). */
export function styleLabel(slug, text = null) {
  if (STYLE_LABEL[slug]) return STYLE_LABEL[slug];
  const t = typeof text === "string" && text.trim() ? text.trim() : String(slug || "").replace(/-/g, " ");
  return t ? t[0].toUpperCase() + t.slice(1) : "Custom";
}
/** "about 3 cents", "about $1.75", "price unknown" (never a made-up number). */
export function money(usd) {
  if (usd == null || !Number.isFinite(+usd)) return "price unknown";
  const v = +usd;
  if (v < 0.995) { const c = Math.max(1, Math.round(v * 100)); return `about ${c} cent${c === 1 ? "" : "s"}`; }
  return `about $${v.toFixed(2)}`;
}

// ---- per-viewer preferences (localStorage) ----------------------------------------------------

const read = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const write = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} };
export const artOn = () => read("lk-art-on", "1") !== "0";
export const setArtOn = (on) => write("lk-art-on", on ? "1" : "0");
export const artStyle = () => read("lk-art-style", null);
export const setArtStyle = (slug) => write("lk-art-style", slug || null);
/** The Settings "Pictures" defaults for new pictures: style, quality, the per-run cap, the image provider and its model
 *  (null: the provider's default; a model the provider does not list is ignored where it is used, `modelFor`). */
export function artDefaults() {
  let d = {};
  try { d = JSON.parse(read("lk-art-defaults", "{}")) || {}; } catch {}
  return { style: typeof d.style === "string" ? d.style : "digital-art", quality: ["draft", "standard", "best"].includes(d.quality) ? d.quality : "standard",
    cap: Number.isFinite(+d.cap) && +d.cap > 0 ? +d.cap : 10, provider: typeof d.provider === "string" ? d.provider : null,
    model: typeof d.model === "string" ? d.model : null };
}
export const setArtDefaults = (d) => write("lk-art-defaults", JSON.stringify({ ...artDefaults(), ...d }));

// ---- the gating helper: the one rule (artgate.js, dependency-free so Node can test it) --------------

export { sheetWorld, unfinishedSources, visibleArt, visibleAt, visibleWorld } from "./artgate.js";

// ---- the manifest cache --------------------------------------------------------------------------

const cache = new Map();   // `${key}\n${style}` → { at, promise } of { manifest, styles, screened } (null when the server has none)
const FRESH_MS = 30000;   // a run may add pictures meanwhile: an answer older than this is asked for again
/** The art set of an index in one style (the server's default style when none is asked). Null without an art set, on an
 *  older server, or when the request fails: pictures are an extra, never a reason for the Reader to fail. */
export function loadArt(key, style = null) {
  const k = `${key}\n${style || ""}`;
  const hit = cache.get(k);
  if (!hit || Date.now() - hit.at > FRESH_MS) {
    const promise = api.art(key, style).then((r) => (r && typeof r === "object" ? normalise(r) : null)).catch(() => null);
    cache.set(k, { at: Date.now(), promise });
  }
  return cache.get(k).promise;
}
function normalise(r) {
  const manifest = r.manifest && typeof r.manifest === "object" ? r.manifest : r;
  const styles = (Array.isArray(r.styles) ? r.styles : []).filter((x) => typeof x === "string" || (x && typeof x.slug === "string"))
    .map((x) => (typeof x === "string" ? { slug: x, text: null, pictures: null }
      : { slug: x.slug, text: typeof x.style === "string" ? x.style : typeof x.text === "string" ? x.text : null, pictures: Number.isFinite(x.pictures) ? x.pictures : null }))
    // A style with no pictures yet (the prompt route writes a set's world note before anything is drawn) is not offered.
    .filter((x) => x.pictures !== 0);
  const screened = r.screened && typeof r.screened === "object" ? r.screened : manifest.screened && typeof manifest.screened === "object" ? manifest.screened : {};
  return { manifest: { ...manifest, pictures: Array.isArray(manifest.pictures) ? manifest.pictures : [] }, styles, screened, slug: typeof r.slug === "string" ? r.slug : null };
}
/** Forget what was fetched for an index (after a draw, a redraw, a delete or an undo). */
export function forgetArt(key = null) {
  for (const k of [...cache.keys()]) if (key == null || k.startsWith(`${key}\n`)) cache.delete(k);
}

/** The image providers that can draw now: configured cloud providers that have an image table. Cached for a minute. */
let provCache = null;
export function imageProviders({ fresh = false } = {}) {
  if (fresh || !provCache || Date.now() - provCache.at > 60000) {
    const promise = api.providers().then((r) => {
      const all = r.providers || [];
      for (const p of all) for (const m of imageModels(p)) MODEL_LABEL.set(`${p.id}\n${m.id}`, m.label || m.id);
      return all.filter((p) => canDraw(p));
    }).catch(() => []);
    provCache = { at: Date.now(), promise };
  }
  return provCache.promise;
}
/** Image model names by "provider\nid", from every /api/providers reply (the lightbox names the model a picture was drawn with). */
const MODEL_LABEL = new Map();
/** A model's name ("Nano Banana Pro"), else its id when the server has not said (or no longer lists it). */
export const modelLabel = (provider, id) => (id ? MODEL_LABEL.get(`${provider}\n${id}`) || id : "");
/** A provider row from /api/providers that draws: its image table, or (older server) Gemini or OpenAI by id. */
export const drawsPictures = (p) => Boolean(p && (p.images || p.id === "gemini" || p.id === "openai"));
const canDraw = (p) => drawsPictures(p) && p.ok;
/** The image models a provider offers ([{ id, label, price, levels }]), as the server lists them; [] without a table. */
export function imageModels(p) {
  const ms = p?.images && Array.isArray(p.images.models) ? p.images.models : [];
  return ms.filter((m) => m && typeof m.id === "string" && m.id);
}
/** The image model to use with a provider: the wanted one when the provider lists it, else its default. Null without a
 *  table (an older server: it then picks the default itself). */
export function modelFor(p, want = null) {
  const ms = imageModels(p);
  if (!ms.length) return null;
  if (want && ms.some((m) => m.id === want)) return want;
  return ms.some((m) => m.id === p.images.default) ? p.images.default : ms[0].id;
}
const modelRow = (p, model) => { const id = modelFor(p, model); return imageModels(p).find((m) => m.id === id) || null; };
/** The quality levels an image model offers (the provider's default when none is named); all three when the server does not say. */
export function levelsOf(p, model = null) {
  const m = modelRow(p, model);
  const lv = Array.isArray(m?.levels) && m.levels.length ? m.levels : ["draft", "standard", "best"];
  return QUALITIES.filter(([k]) => lv.includes(k));
}
/** The quality to draw at with a model: the wanted one when the model offers it, else Standard, else its first level. */
export function fitQuality(p, model, quality) {
  const lv = levelsOf(p, model).map(([k]) => k);
  return lv.includes(quality) ? quality : lv.includes("standard") ? "standard" : lv[0];
}
/** USD for one picture with an image model (the provider's default when none is named) at a quality and shape: a number,
 *  null when the table says the price is unknown, undefined when the server sent no image table (then the prompt route's
 *  price stands). */
export function imagePrice(p, quality = "standard", shape = "landscape", model = null) {
  const m = modelRow(p, model);
  if (!m) return undefined;
  const v = m.price && typeof m.price === "object" ? m.price[quality] : null;
  if (typeof v === "number") return v;
  if (v && typeof v === "object" && typeof v[shape] === "number") return v[shape];
  return null;
}
/** An image model's name and its price for one picture at a quality ("Nano Banana 2.1, about 3 cents a picture"). A model
 *  without that quality is priced at the one it would draw at, and says so ("..., about 3 cents a picture at Standard"). */
export function imageLine(p, quality = "standard", model = null) {
  const m = modelRow(p, model);
  if (!m) return drawsPictures(p) ? "Can draw" : "Can't draw";
  const q = fitQuality(p, m.id, quality);
  const one = imagePrice(p, q, "landscape", m.id);
  const at = q !== quality ? ` at ${QUALITIES.find(([k]) => k === q)?.[1] || q}` : "";
  return `${m.label || m.id}, ${one != null ? `${money(one)} a picture${at}` : `price unknown${at}`}`;
}

// ---- picture elements ----------------------------------------------------------------------------

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
/** The file of one picture, from the API only. `textHash` and `style` are the art set's (checked by the caller against the
 *  index's own edition); the id must look like an id. Null when anything is off: no picture rather than a guessed URL. */
export function artUrl(rec, textHash, style, { thumb = false } = {}) {
  if (!rec || !ID_RE.test(rec.id || "") || typeof textHash !== "string" || !textHash || typeof style !== "string" || !style) return null;
  return `/api/art/${encodeURIComponent(textHash)}/${encodeURIComponent(style)}/${encodeURIComponent(rec.id)}${thumb ? "?thumb=1" : ""}`;
}
/** An <img> for a picture: alt is the caption (a chapter title or an entry name, never the prompt), lazy and async.
 *  A file that fails to load removes the element, so a broken picture leaves nothing on screen. */
export function artImg(src, alt, cls = "art-img", { eager = false } = {}) {
  if (!src) return null;
  const img = h(`img.${cls}`, { src, alt: alt || "", loading: eager ? "eager" : "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => {
    img.closest(".has-face")?.classList.remove("has-face");   // a Cast row gets its type mark back
    const btn = img.closest(".art-thumb, .art-plate, .art-face-btn, .ent-face");
    (btn || img).remove();
  });
  return img;
}

// ---- the lightbox ----------------------------------------------------------------------------------

const when = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : ""; };

/**
 * One picture at full size, with its caption and a closed "How this was drawn". `items` are
 * [{ rec, src, caption, key, canEdit }] in the order the arrows step through (only visible pictures are ever passed).
 * `onChanged()` runs after a redraw, a delete or an undo; `onRedraw(item)` is "Draw again" for an item without a picture.
 */
export function openLightbox(items, index = 0, { onChanged = null } = {}) {
  if (!items.length) return null;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let i = Math.min(Math.max(0, index), items.length - 1);
  const stage = h("div.lb-stage");
  const caption = h("p.lb-caption");
  const how = h("div.lb-how");
  const acts = h("div.lb-acts");
  const count = h("span.lb-count.subtle");
  const prev = h("button.icon-btn.lb-nav.lb-prev", { type: "button", "aria-label": "Previous picture", title: "Previous picture" }, icon("chevron-left"));
  const next = h("button.icon-btn.lb-nav.lb-next", { type: "button", "aria-label": "Next picture", title: "Next picture" }, icon("chevron"));
  const closeBtn = h("button.icon-btn.lb-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const title = h("h2#lb-title.sr-only");
  const toasts = h("div.toasts.lb-toasts", { "aria-live": "polite" });
  const dlg = h("dialog.lightbox", { "aria-labelledby": "lb-title" },
    h("div.lb-panel", h("header.lb-head", title, count, closeBtn), h("div.lb-main", prev, stage, next), h("div.lb-foot", caption, acts, how)), toasts);
  const close = () => { closeLocked(dlg); };
  dlg.addEventListener("close", () => { dlg.remove(); if (returnTo?.isConnected) returnTo.focus({ preventScroll: true }); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  closeBtn.addEventListener("click", close);
  prev.addEventListener("click", () => go(i - 1));
  next.addEventListener("click", () => go(i + 1));
  dlg.addEventListener("keydown", (e) => {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); go(i - 1); }
    if (e.key === "ArrowRight") { e.preventDefault(); go(i + 1); }
  });
  function go(n) {
    if (n < 0 || n >= items.length || n === i) return;
    i = n; paint();
  }
  function paint() {
    const it = items[i], r = it.rec;
    title.textContent = it.caption || "Picture";
    clear(stage, artImg(it.src, it.caption, "lb-img", { eager: true }));
    caption.textContent = it.caption || "";
    count.textContent = items.length > 1 ? `${i + 1} of ${items.length}` : "";
    prev.hidden = next.hidden = items.length < 2;
    prev.disabled = i === 0; next.disabled = i === items.length - 1;
    const facts = [
      ["Style", styleLabel(it.style || r.style, it.styleText)],
      ["Model", modelFact(r)],
      ["Quality", r.quality ? `${r.quality[0].toUpperCase()}${r.quality.slice(1)}` : null],
      ["Cost", typeof r.cost === "number" ? (r.cost > 0 ? money(r.cost).replace(/^about /, "") : "Price unknown") : null],
      ["Drawn", when(r.created)],
    ].filter(([, v]) => v);
    // The prompt may carry an earlier book's world note or looks: it shows only once those books are finished.
    const hiddenUntil = it.promptHiddenUntil ? it.promptHiddenUntil(r) : null;
    clear(how, h("details.lb-details", h("summary", icon("chevron"), "How this was drawn"),
      hiddenUntil ? h("div.lb-details-body", h("p.lb-hidden", `How this was drawn is hidden until you finish ${hiddenUntil}.`)) : h("div.lb-details-body",
        typeof r.prompt === "string" && r.prompt ? h("div.lb-prompt", h("span.label", "Prompt"), h("p", r.prompt)) : null,
        h("dl.lb-facts", facts.map(([k, v]) => [h("dt", k), h(k === "Model" ? "dd.lb-model" : "dd", v)])))));
    clear(acts, it.canEdit ? [
      it.canRedraw === false ? null : h("button.btn.quiet", { type: "button", on: { click: () => redraw(it) } }, icon("refresh"), "Redraw"),
      h("button.btn.quiet.danger", { type: "button", on: { click: () => remove(it) } }, icon("trash"), "Delete"),
    ] : null);
  }
  function undoable(it, r, done) {
    const id = r?.undo ?? r?.journal ?? null;
    // While the lightbox is open its own stack; after it closed, the modal it was opened from (the entry drawer, a sheet),
    // since a modal makes the page's stack unreachable.
    toast(done, "ok", { host: dlg.open ? toasts : modalToasts(), action: id ? { label: "Undo", run: async () => {
      if (dlg.open) close();   // the picture on screen is the one being undone
      try { await api.undo(it.key, id); forgetArt(it.key); toast("Undone", "ok", { host: modalToasts() }); onChanged?.(); } catch (e) { toast(e.message, "error", { host: modalToasts() }); }
    } } : null });
  }
  function redraw(it) {
    const note = h("textarea.input.lb-note", { rows: "2", placeholder: "What to change, for example: in daylight, closer to the face", "aria-label": "What to change" });
    const go_ = h("button.btn.primary", { type: "button" }, "Redraw");
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => paint() } }, "Cancel");
    const err = h("p.hint.field-error", { role: "alert" });
    go_.addEventListener("click", async () => {
      go_.disabled = true; cancel.disabled = true;
      clear(acts, loading("Drawing it again…"));
      try {
        const r = await api.artRedraw(it.key, it.rec.id, note.value.trim());
        forgetArt(it.key);
        const rec = r?.record || r;
        if (rec && ID_RE.test(rec.id || "")) { it.rec = rec; it.src = it.srcFor ? it.srcFor(rec) : it.src; }
        paint();
        undoable(it, r, "Picture redrawn");
        onChanged?.();
      } catch (e) {
        clear(acts, h("div.lb-redraw", note, err, h("div.btns", cancel, go_)));
        err.textContent = e.code === "refused" ? `The provider would not draw it: ${e.message}` : e.message;
        go_.disabled = false; cancel.disabled = false;
      }
    });
    clear(acts, h("div.lb-redraw", h("label.label", "Redraw with a note, optional"), note, err, h("div.btns", cancel, go_)));
    note.focus();
  }
  async function remove(it) {
    if (!(await confirmDialog({ title: "Delete this picture?", message: "It goes from this art set. Undo brings it back.", confirmLabel: "Delete" }))) return;
    try {
      const r = await api.artDelete(it.key, it.rec.id);
      forgetArt(it.key);
      items.splice(i, 1);
      close();
      undoable(it, r, "Picture deleted");
      onChanged?.();
    } catch (e) { toast(e.message, "error", { host: toasts }); }
  }
  document.body.append(dlg);
  showModalLocked(dlg);
  paint();
  closeBtn.focus({ preventScroll: true });
  // The model's name comes with the providers list; until it has arrived the id stands, then the line is put right in
  // place (a repaint would close "How this was drawn" under the person's eyes).
  imageProviders().then(() => {
    const dd = dlg.isConnected ? how.querySelector(".lb-model") : null;
    const r = items[i]?.rec;
    if (dd && r) dd.textContent = modelFact(r);
  });
  return { close };
}

/** A toast stack inside the topmost open modal (the entry drawer or a sheet), or null for the page's own. */
export function modalToasts() {
  const open = [...document.querySelectorAll("dialog[open]")].filter((d) => { try { return d.matches(":modal") && !d.classList.contains("lightbox"); } catch { return d.matches(".drawer, .sheet"); } });
  const top = open.at(-1);
  if (!top) return null;
  let host = top.querySelector(":scope .drawer-toasts, :scope .sheet-toasts");
  if (!host) { host = h("div.drawer-toasts"); top.append(host); }
  return host;
}

// ---- the picture sheet: on-demand drawing --------------------------------------------------------

/**
 * "Draw a picture" for one item the reader can see. `opts`:
 *   key          the index's library key
 *   kind         plate | event | portrait | place | thing
 *   target       the chapter title, event id or entity id the server knows it by
 *   title        what the sheet is about ("Chapter V", "Jonathan Harker"), shown as the title
 *   p            the reader's frontier anchor (the server refuses a target not yet visible)
 *   finished     the series book keys the reader has finished (the prompt leaves out an unfinished book's world note)
 *   style        the art set's current style slug (selected first); styles: the styles it has [{ slug, text }]
 *   world        the world note as visible at the position ({ text, own } or null: not shown)
 *   moments      for a chapter: [{ kind, target, label }] the visible moments it may draw, the first being the chapter's main one
 *   drawnLine    for a chapter: "4 moments in this chapter, 1 drawn" (counted from visible content only)
 *   srcFor(rec, slug), canShow(rec)   the URL of a filed picture, and whether it may show at the position
 *   onDrawn(rec, slug)                after a picture was filed (slug is the server's, null when it did not say)
 * Returns the sheet, with `current()` → { kind, target } (the moment selected now) and `listed()` → every item named on it.
 */
export async function openPictureSheet(opts) {
  const provs = await imageProviders({ fresh: true });
  if (!provs.length) {
    const sh = sheet({ title: "Draw a picture", className: "art-sheet",
      content: h("div.art-sheet-body", h("p", "Pictures need a Gemini or OpenAI key."),
        h("p", h("a", { href: "#/settings/cloud" }, "Add one in Settings, Cloud models"))) });
    sh.current = () => ({ kind: opts.kind, target: opts.target });
    sh.listed = () => [sh.current()];
    return sh;
  }
  const d = artDefaults();
  const st = {
    kind: opts.kind, target: opts.target,
    style: opts.style || d.style, custom: "", shape: SHAPE_OF[opts.kind] || "landscape",
    quality: d.quality, provider: provs.find((p) => p.id === d.provider) || provs[0], model: null,
    prompt: "", written: "", price: null, asked: null, slug: null,
    parts: null, sentence: null, missing: false,   // the prompt's parts, the style sentence in it now, and whether that is lost
    busy: false, closed: false,
  };
  if (!STYLE_LABEL[st.style]) {
    st.custom = (opts.styles || []).find((x) => x.slug === st.style)?.text || st.style.replace(/-/g, " ");
    st.slug = STYLE_LABEL[st.style] ? null : opts.style;   // the art set's own slug for its custom style
    st.style = "custom";
  }
  // The Settings model when it is the Settings provider's, else the provider's default (null: an older server picks).
  st.model = modelFor(st.provider, st.provider.id === d.provider ? d.model : null);
  st.quality = fitQuality(st.provider, st.model, st.quality);
  // The style as sent to the server (a known slug, or the person's words), and the folder slug pictures are filed under:
  // a known style is its own slug, a custom one only what the server answered (never a folder made from raw text here).
  const styleText = () => (st.style === "custom" ? st.custom.trim() : st.style);
  const folder = () => (st.style === "custom" ? st.slug : st.style);
  // Where the reader is now (asked at every call, never the position the sheet was opened at), and their finished books.
  const here = () => { const at = opts.position ? opts.position() : { p: opts.p, finished: opts.finished }; return { p: at.p ?? null, finished: at.finished || [] }; };
  let ctrl = null, gen = 0;   // the request in flight and its generation: a late reply for a moment left behind is dropped
  const abort = () => { gen++; ctrl?.abort(); ctrl = null; st.busy = false; };

  const body = h("div.art-sheet-body");
  const drawBtn = h("button.btn.primary", { type: "button", disabled: true }, icon("sparkle"), "Draw");
  const footNote = h("span.art-price.num", { role: "status" });
  const sh = sheet({ title: `Draw a picture${opts.title ? `: ${opts.title}` : ""}`, className: "art-sheet", content: body,
    footer: h("div.art-foot", footNote, drawBtn),
    // Always closable: a prompt or a drawing in flight is abandoned (a drawing already sent may still be filed and show
    // later); an edited prompt is asked about first.
    beforeClose: () => {
      if (st.busy) { abort(); return true; }
      if (!st.prompt || st.prompt === st.written) return true;
      confirmDialog({ title: "Discard your prompt?", message: "The prompt you changed is not kept.", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { st.prompt = st.written; sh.close(true); } });
      return false;
    },
    onClose: () => { st.closed = true; abort(); } });
  sh.current = () => ({ kind: st.kind, target: st.target });
  sh.listed = () => [sh.current(), ...(opts.moments || []).map((m) => ({ kind: m.kind, target: m.target }))];
  /** The anchors the text on screen was written up to: the prompt's (the picture's own, any world-note hold included) and
   *  the world note's own anchor. The Reader checks them against the position as it checks the moments. */
  sh.anchors = () => [st.anchor, opts.world?.from].filter((a) => typeof a === "string");
  /** The position moved back before that text: drop the prompt and the World line and write a new prompt here. */
  sh.reposition = ({ world = null, canEditWorld = false } = {}) => {
    opts.world = world || null;
    opts.canEditWorld = canEditWorld;   // decided again for the position now: a note hidden here is never written over
    st.prompt = st.written = "";
    st.anchor = null; st.promptFor = null; st.asked = null; st.price = null; st.parts = null; st.sentence = null; st.missing = false;
    write();
  };

  const textarea = h("textarea.input.art-prompt", { rows: "7", "aria-label": "Prompt", spellcheck: "true" });
  textarea.addEventListener("input", () => { st.prompt = textarea.value; paintFoot(); });
  const err = h("div.art-error", { role: "alert" });

  async function write() {
    abort();
    const mine = ++gen;
    ctrl = new AbortController();
    st.busy = true;
    clear(body, loading("Writing a prompt from what you have read…"));
    paintFoot();
    const asked = { style: styleText(), quality: st.quality, provider: st.provider.id, model: st.model, shape: st.shape };
    try {
      const r = await api.artPrompt(opts.key, { kind: st.kind, target: st.target, style: asked.style, shape: asked.shape,
        quality: asked.quality, provider: asked.provider, ...(asked.model ? { model: asked.model } : {}), ...here() }, ctrl.signal);
      if (mine !== gen || st.closed) return;
      st.parts = promptParts(r);
      st.missing = false;
      if (st.parts) {
        // Built here from its parts, in the style chosen now (it may have changed while the prompt was being written).
        st.sentence = sentenceFor(st.parts, st.style, st.custom) ?? squash(st.parts.style);
        st.prompt = st.written = composePrompt(st.parts.body, st.sentence, st.parts.noText);
      } else {
        st.sentence = null;   // an older server: the style is inside its text, so a new style writes the prompt again
        st.prompt = st.written = typeof r.prompt === "string" ? r.prompt : "";
      }
      st.price = typeof r.price === "number" ? r.price : null;
      st.anchor = typeof r.anchor === "string" ? r.anchor : null;
      st.asked = asked;
      st.promptStyle = asked.style;
      st.promptFor = { kind: st.kind, target: st.target };
      clear(err);
      if (st.style === "custom" && asked.style === styleText()) st.slug = typeof r.slug === "string" && /^[a-z0-9-]{1,64}$/.test(r.slug) ? r.slug : null;
      st.busy = false; ctrl = null;
      paint();
    } catch (e) {
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      const why = promptError(e);
      if (st.promptFor && e.code !== "not-visible") {
        // A prompt was already on screen: keep it, with the style and moment it was written for, and say why the new one failed.
        Object.assign(st, st.promptFor);
        paint();
        clear(err, h("p", why));
        return;
      }
      clear(body, h("div.art-error", { role: "alert" }, h("p", why)),
        e.code === "not-visible" ? null : h("button.btn", { type: "button", on: { click: write } }, "Try again"));
      paintFoot();
    }
  }
  /** A new style changes only the prompt's style sentence, here and with no call: an unedited prompt is built again from
   *  its parts, an edited one has the old sentence replaced where it still stands (else `st.missing` says so). An older
   *  server sends no parts, so there the prompt is written again. Updates the text area in place (typing keeps focus). */
  function applyStyle() {
    if (!st.prompt && !st.parts) return;
    if (!st.parts) { if (styleText() && styleText() !== st.promptStyle) write(); return; }
    const next = sentenceFor(st.parts, st.style, st.custom);
    if (next == null) { st.missing = true; return; }   // a known style the server did not list: the text stays as it is
    Object.assign(st, restyle({ prompt: st.prompt, written: st.written, parts: st.parts, old: st.sentence, next }));
    st.promptStyle = styleText();
    textarea.value = st.prompt;
    paintStyleNote();
  }
  const styleNote = h("p.hint.art-style-note", { role: "status" });
  function paintStyleNote() {
    styleNote.textContent = st.missing && st.prompt ? "The style you chose is not in the prompt text, so the picture follows the text as you wrote it." : "";
  }

  const chip = (label, pressed, on) => h("button.filter-chip", { type: "button", "aria-pressed": String(pressed), on: { click: on } }, label);
  let momentsOpen = false;
  function paint() {
    textarea.value = st.prompt;
    const customField = h("input.input.art-custom", { value: st.custom, placeholder: "Describe the style, for example: linocut, two colours", "aria-label": "Custom style",
      // Swapped into the prompt once, on change (blur or Enter), not on every keystroke; Draw applies a pending one first.
      on: { input: (e) => { st.custom = e.target.value; st.slug = null; paintFoot(); },
        change: (e) => { st.custom = e.target.value; applyStyle(); paintFoot(); },
        keydown: (e) => { if (e.key === "Enter") { e.preventDefault(); st.custom = e.target.value; applyStyle(); paintFoot(); } } } });
    const styleRow = h("div.art-chips", { role: "group", "aria-label": "Style" },
      STYLES.map(([slug, label]) => chip(label, st.style === slug, () => { if (st.style !== slug) { st.style = slug; applyStyle(); paint(); } })),
      chip("Custom…", st.style === "custom", () => { if (st.style !== "custom") { st.style = "custom"; applyStyle(); paint(); } body.querySelector(".art-custom")?.focus(); }));
    const seg = (label, list, value, set) => h("div.seg", { role: "group", "aria-label": label },
      list.map(([k, l]) => h("button", { type: "button", "aria-pressed": String(value === k), on: { click: () => { set(k); paint(); } } }, l)));
    // Every level is shown; the ones the chosen model does not offer are disabled (Nano Banana 2 Lite has no Best).
    const levels = levelsOf(st.provider, st.model).map(([k]) => k);
    st.quality = fitQuality(st.provider, st.model, st.quality);
    const modelName = (m) => m.label || m.id;
    const qualitySeg = h("div.seg", { role: "group", "aria-label": "Quality" },
      QUALITIES.map(([k, l]) => {
        const off = !levels.includes(k);
        const name = off ? modelName(imageModels(st.provider).find((m) => m.id === st.model) || { id: st.model || "" }) : "";
        return h("button", { type: "button", "aria-pressed": String(st.quality === k), disabled: off,
          title: off && name ? `${name} has no ${l} quality` : null, on: { click: () => { st.quality = k; paint(); } } }, l);
      }));
    // The models of the chosen provider: chips for three or fewer, else a select; each with its price at the quality chosen.
    const models = imageModels(st.provider);
    const setModel = (id) => { st.model = id; st.quality = fitQuality(st.provider, id, st.quality); paint(); };
    const priceOf = (m) => { const pr = imagePrice(st.provider, fitQuality(st.provider, m.id, st.quality), st.shape, m.id); return pr != null ? money(pr) : "price unknown"; };
    const modelPick = models.length > 1 ? (models.length <= 3
      ? h("div.art-chips.art-models", { role: "group", "aria-label": "Model" }, models.map((m) =>
        chip([`${modelName(m)},`, h("span.chip-price", priceOf(m))], st.model === m.id, () => { if (st.model !== m.id) setModel(m.id); })))
      : h("select.select.art-model#art-model", { on: { change: (e) => setModel(e.target.value) } },
        models.map((m) => h("option", { value: m.id, selected: m.id === st.model }, `${modelName(m)}, ${priceOf(m)}`)))) : null;
    const provSel = provs.length > 1 ? h("select.select.art-prov", { "aria-label": "Draw with", on: { change: (e) => {
      st.provider = provs.find((p) => p.id === e.target.value) || provs[0];
      st.model = modelFor(st.provider);   // another provider starts from its own default model
      st.quality = fitQuality(st.provider, st.model, st.quality);
      paint(); } } },
      provs.map((p) => h("option", { value: p.id, selected: p.id === st.provider.id }, p.label))) : null;
    const moments = (opts.moments || []).length > 1 ? h("div.art-field",
      h("span.label", "Moment"),
      // The first few moments, the chosen one always among them; the rest behind "Show all".
      foldList(opts.moments.map((m, n) => {
        const radio = h("input", { type: "radio", name: "art-moment", checked: m.kind === st.kind && m.target === st.target,
          on: { change: () => { st.kind = m.kind; st.target = m.target; st.shape = SHAPE_OF[m.kind] || st.shape; momentsOpen = true; write(); } } });
        return h(`label.art-moment${m.kind === st.kind && m.target === st.target ? ".on" : ""}`, radio, h("span", m.label || (n ? "A moment" : "The chapter's main moment")));
      }), (nodes) => h("div.art-moments", { role: "radiogroup", "aria-label": "Moment to draw" }, nodes),
      { show: 4, noun: "moments", open: momentsOpen, onToggle: (o) => { momentsOpen = o; },
        keep: [opts.moments.findIndex((m) => m.kind === st.kind && m.target === st.target)] })) : null;
    clear(body,
      opts.drawnLine ? h("p.art-drawn.subtle", opts.drawnLine) : null,
      moments,
      h("div.art-field", h("label.label", { for: "art-prompt" }, "Prompt"), textarea,
        h("p.hint", "Written from what you have read so far. Change anything; the picture is drawn from this text only. ",
          h("button.linkish", { type: "button", on: { click: write } }, "Write it again"))),
      h("div.art-field", h("span.label", "Style"), styleRow, st.style === "custom" ? customField : null, styleNote),
      h("div.art-row",
        h("div.art-field", h("span.label", "Shape"), seg("Shape", SHAPES, st.shape, (k) => { st.shape = k; })),
        h("div.art-field", h("span.label", "Quality"), qualitySeg),
        provSel ? h("div.art-field", h("label.label", "Draw with"), provSel) : null),
      modelPick ? h("div.art-field", models.length <= 3 ? h("span.label", "Model") : h("label.label", { for: "art-model" }, "Model"), modelPick) : null,
      worldLine(),
      err);
    textarea.id = "art-prompt";
    paintStyleNote();
    paintFoot();
  }
  /** The person's own note on the book's world, which the prompt writer reads with every picture of this book (an
   *  automatic note is no longer written or used). Empty by default: one quiet "Add a note about this book's world". */
  function worldLine() {
    const has = Boolean(opts.world?.text);
    if (!has && !opts.canEditWorld) return null;
    const line = has
      ? h("p.art-world", h("span.subtle", "Your note on this book's world: "), h("span.art-world-text", opts.world.text), " ",
        opts.canEditWorld ? h("button.linkish", { type: "button", on: { click: edit } }, "Change") : null)
      : h("p.art-world", h("button.linkish", { type: "button", on: { click: edit } }, "Add a note about this book's world"));
    function edit() {
      const was = opts.world?.text || "";
      const field = h("textarea.input", { rows: "3", "aria-label": "Your note on this book's world", placeholder: "For example: a walled city in a cold kingdom, lit by oil lamps, where guilds rule" });
      field.value = was;
      const ferr = h("p.hint.field-error", { role: "alert" });
      const save = h("button.btn", { type: "button", on: { click: async () => {
        const text = field.value.trim();
        if (!text) { ferr.textContent = "Write the note first."; return; }
        save.disabled = true;
        try {
          const r = await api.artWorld(opts.key, text, opts.style || styleText());   // the note of the art set shown, by its slug
          const from = typeof r?.world?.from === "string" ? r.world.from : opts.world?.from;
          opts.world = { ...(opts.world || {}), text, own: true, edited: true, ...(from ? { from } : {}) };
          forgetArt(opts.key);
          toast("Note saved. Write the prompt again to use it.", "ok", { host: sh.toasts });
          box.replaceWith(worldLine() || h("span"));
        } catch (e) { ferr.textContent = e.message; save.disabled = false; }
      } } }, "Save");
      const cancel = h("button.btn.quiet", { type: "button", on: { click: () => box.replaceWith(worldLine() || h("span")) } }, "Cancel");
      const box = h("div.art-field.art-world-edit", h("span.label", "Your note on this book's world"), field,
        h("p.hint", "The prompt writer reads it with every picture of this book. Say what the book itself makes clear: the era, the places, how its people live."), ferr, h("div.btns", cancel, save));
      line.replaceWith(box);
      field.focus();
    }
    return line;
  }
  /** The price for this picture as chosen now: the image table's, else the prompt route's when it was asked for the same choices. */
  function price() {
    const known = imagePrice(st.provider, st.quality, st.shape, st.model);
    if (known !== undefined) return known;
    const a = st.asked;
    return a && a.quality === st.quality && a.provider === st.provider.id && a.model === st.model && a.shape === st.shape ? st.price : null;
  }
  function paintFoot() {
    // An older server's prompt carries its style in the text: until it is written again for a new style, Draw waits.
    const stale = !st.parts && st.prompt && st.promptStyle != null && styleText() !== st.promptStyle;
    const ok = st.prompt.trim().length > 0 && Boolean(styleText()) && !st.busy && !stale;
    drawBtn.disabled = !ok;
    const pr = price();
    // Without the image table the price is the prompt route's, for the choices it was asked with; other choices say so
    // rather than show a number for the wrong quality or provider.
    const asked = st.asked && st.asked.quality === st.quality && st.asked.provider === st.provider.id && st.asked.model === st.model && st.asked.shape === st.shape;
    footNote.textContent = st.busy ? "" : stale ? "Write the prompt again for this style first."
      : pr != null ? `${money(pr)[0].toUpperCase()}${money(pr).slice(1)} for this picture`
        : imagePrice(st.provider, st.quality, st.shape, st.model) === undefined && st.asked && !asked ? "Price unknown for these choices" : "Price unknown";
  }

  drawBtn.addEventListener("click", async () => {
    if (st.busy) return;
    // A custom style typed but not yet swapped in (no change event yet): put it in the prompt before it is sent.
    if (st.parts && sentenceFor(st.parts, st.style, st.custom) !== st.sentence) applyStyle();
    abort();
    const mine = ++gen;
    ctrl = new AbortController();
    st.busy = true; drawBtn.disabled = true;
    const keep = [...body.childNodes];
    clear(body, loading("Drawing the picture. This takes a few seconds…"));
    footNote.textContent = "";
    try {
      const r = await api.artDraw(opts.key, { kind: st.kind, target: st.target, style: styleText(), shape: st.shape, quality: st.quality,
        prompt: st.prompt, provider: st.provider.id, ...(st.model ? { model: st.model } : {}), ...here() }, ctrl.signal);
      const rec = r?.record || r;
      const slug = st.style === "custom" ? (typeof r?.slug === "string" && /^[a-z0-9-]{1,64}$/.test(r.slug) ? r.slug : folder()) : folder();
      forgetArt(opts.key);
      opts.onDrawn?.(rec, slug || null);
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      st.written = st.prompt;   // drawn: nothing to ask about on close
      // The new picture shows here only if it may show at the position (its anchor is the item's own, which is visible,
      // but the server decides the anchor; the Reader checks it as it checks every picture).
      const src = slug && opts.srcFor && (!opts.canShow || opts.canShow(rec)) ? opts.srcFor(rec, slug) : null;
      clear(body, h("div.art-done", src ? artImg(src, opts.title, "art-done-img", { eager: true }) : null,
        h("p", src ? "Drawn and added to the art set." : "Drawn and added to the art set. It shows where it belongs in the book."),
        h("div.btns", h("button.btn.primary", { type: "button", on: { click: () => sh.close(true) } }, "Done"))));
      drawBtn.hidden = true; footNote.textContent = typeof rec?.cost === "number" && rec.cost > 0 ? `Cost ${money(rec.cost).replace(/^about /, "")}` : "";
    } catch (e) {
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      clear(body, keep);
      clear(err, h("p", e.code === "refused" ? `The provider would not draw this: ${e.message}. Change the prompt and try again.` : `Couldn't draw it: ${e.message}`));
      paintFoot();
      textarea.focus();
    }
  });
  write();
  return sh;
}

/** One sentence for a prompt the server could not write. "dropped" is the pipeline's own check refusing what the model wrote
 *  (a name, hindsight wording...): asking again usually gives a usable one. `why` names the check and the word it caught
 *  ("a name the picture must not show: Hagrid"), when the server could say it without revealing anything. */
function promptError(e) {
  if (e.code === "not-visible") return "This isn't revealed at your position yet.";
  if (e.code === "dropped") {
    if (e.why) return `The prompt that came back had ${e.why}. Write it again for another one.`;
    const reason = String(e.message || "").replace(/^(dropped|skipped):\s*/, "");
    return `The prompt that came back didn't pass the checks (${reason}). Write it again for another one.`;
  }
  if (e.code === "busy") return "Pictures are being drawn for this book. Try again once that run has finished.";
  return `Couldn't write a prompt: ${e.message}`;
}

// ---- the Pictures group of the build form and the book page --------------------------------------

/**
 * The "Pictures" choices, as one closed disclosure (`details`) or as a plain block for a dialog. Returns
 * { el, config() → the job's `art` object or null when "Draw pictures" is off, refresh() }. `estimate(params)` is
 * asked for the line "About 52 pictures, about $1.75"; `fallback()` gives a rough count when it cannot answer.
 */
// The portrait choice: `st.cast` is "main", "all" or "none"; the job config sends "main", "all" or false, the estimate the word.
const CAST_SUMMARY = { main: ", main characters' portraits", all: ", everyone's portraits", none: "" };

export function picturesForm({ estimate, fallback = null, open = false, always = false, idPrefix = "art" } = {}) {
  const d = artDefaults();
  const st = { on: always, style: STYLE_LABEL[d.style] ? d.style : "custom", custom: STYLE_LABEL[d.style] ? "" : d.style, level: "main", cast: "main",
    quality: d.quality, provider: null, model: null, cap: d.cap };
  let provs = null;
  const line = h("p.art-est", { role: "status" });
  const body = h("div.art-form-body");
  const tick = h("input", { type: "checkbox", role: "switch", id: `${idPrefix}-on`, checked: st.on });
  tick.addEventListener("change", () => { st.on = tick.checked; paint(); });
  const sum = h("span.sum-hint");
  const el = always ? h("div.art-form", body, line)
    : h("details.advanced.art-group", { open: open || null }, h("summary", icon("chevron"), "Pictures", sum), h("div.advanced-body", body, line));
  // The Settings provider and model (the model only with its own provider), else the first provider and its default model.
  const pickProvider = (p, want = null) => {
    st.provider = p || null;
    st.model = p ? modelFor(p, want) : null;
    if (p) st.quality = fitQuality(p, st.model, st.quality);
  };
  imageProviders().then((list) => {
    provs = list;
    const p = list.find((x) => x.id === d.provider) || list[0] || null;
    pickProvider(p, p && p.id === d.provider ? d.model : null);
    paint();
  });
  const slug = () => (st.style === "custom" ? st.custom.trim() : st.style);
  function paint() {
    sum.textContent = st.on ? `${styleLabel(slug())}, ${st.level === "main" ? "one per chapter" : "every moment"}${CAST_SUMMARY[st.cast]}` : "Off";
    if (provs && !provs.length) {
      clear(body, h("p.hint", "Pictures need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one in Settings")));
      line.textContent = "";
      return;
    }
    const chip = (label, pressed, on) => h("button.filter-chip", { type: "button", "aria-pressed": String(pressed), disabled: !st.on, on: { click: on } }, label);
    const radio = (name, value, label, hint) => {
      const r = h("input", { type: "radio", name: `${idPrefix}-${name}`, value, checked: st[name] === value, disabled: !st.on,
        on: { change: () => { st[name] = value; paint(); } } });
      return h(`label.prow${st[name] === value ? ".on" : ""}`, r, h("span.pr-text", h("span.pr-title", label), hint ? h("span.pr-line", hint) : null));
    };
    const custom = h("input.input.art-custom", { value: st.custom, disabled: !st.on, placeholder: "Describe the style", "aria-label": "Custom style",
      on: { change: (e) => { st.custom = e.target.value; refresh(); } } });
    // Every level is shown; the ones the chosen model does not offer are disabled (Nano Banana 2 Lite has no Best).
    const levels = (st.provider ? levelsOf(st.provider, st.model) : QUALITIES).map(([k]) => k);
    if (st.provider) st.quality = fitQuality(st.provider, st.model, st.quality);
    const models = st.provider ? imageModels(st.provider) : [];
    const modelSel = models.length > 1 ? h(`select.select.art-model-sel#${idPrefix}-model`, { disabled: !st.on, on: { change: (e) => {
      st.model = modelFor(st.provider, e.target.value); st.quality = fitQuality(st.provider, st.model, st.quality); paint(); } } },
    models.map((m) => h("option", { value: m.id, selected: m.id === st.model }, imageLine(st.provider, st.quality, m.id)))) : null;
    // Dollars and cents: a small first run ("Stop at $0.30") is a reasonable way to try a style.
    const cap = h("input.input.w-90", { type: "number", min: "0.05", step: "0.05", id: `${idPrefix}-cap`, value: String(st.cap), disabled: !st.on, inputmode: "decimal",
      on: { change: (e) => { const v = +e.target.value; st.cap = Number.isFinite(v) && v > 0 ? Math.round(v * 100) / 100 : 10; e.target.value = String(st.cap); refresh(); } } });
    clear(body,
      always ? null : h("label.switch", { for: `${idPrefix}-on` }, tick, h("span", h("span.label.label-block", "Draw pictures"),
        h("span.hint", "Chapter pictures and portraits, drawn after the index is written."))),
      h("div.field", h("span.label", "Style"), h("div.art-chips", { role: "group", "aria-label": "Style" },
        STYLES.map(([k, l]) => chip(l, st.style === k, () => { st.style = k; paint(); })),
        chip("Custom…", st.style === "custom", () => { st.style = "custom"; paint(); el.querySelector(".art-custom")?.focus(); })),
        st.style === "custom" ? custom : null),
      h("div.field", h("span.label", "How many"), h("div.prows", { role: "radiogroup", "aria-label": "How many" },
        radio("level", "main", "The main moment of each chapter"),
        radio("level", "all", "Every moment worth drawing"))),
      h("div.field", h("span.label", "Portraits"), h("div.prows", { role: "radiogroup", "aria-label": "Portraits" },
        radio("cast", "main", "Main characters", "The characters the story is most about, up to 12 a book."),
        radio("cast", "all", "Everyone with a part"),
        radio("cast", "none", "No portraits"))),
      h("div.field", h("span.label", "Quality"), h("div.seg", { role: "group", "aria-label": "Quality" },
        QUALITIES.map(([k, l]) => h("button", { type: "button", "aria-pressed": String(st.quality === k), disabled: !st.on || !levels.includes(k),
          title: levels.includes(k) ? null : `This model has no ${l} quality`, on: { click: () => { st.quality = k; paint(); } } }, l)))),
      provs && provs.length > 1 ? h("div.field", h("label", { for: `${idPrefix}-prov` }, "Draw with"),
        h(`select.select#${idPrefix}-prov`, { disabled: !st.on, on: { change: (e) => { pickProvider(provs.find((p) => p.id === e.target.value)); paint(); } } },
          provs.map((p) => h("option", { value: p.id, selected: p.id === st.provider?.id }, p.label)))) : null,
      modelSel ? h("div.field", h("label", { for: `${idPrefix}-model` }, "Image model"), modelSel)
        : provs && provs.length === 1 ? h("p.hint", `Drawn with ${provs[0].label}: ${imageLine(provs[0], st.quality, st.model)}.`) : null,
      h("div.field.art-cap", h("label", { for: `${idPrefix}-cap` }, "Stop at"), h("span.art-cap-row", "$", cap),
        h("p.hint", "The run stops drawing there and keeps what it drew.")));
    refresh();
  }
  let seq = 0;
  async function refresh() {
    if (!st.on || !st.provider) { line.textContent = st.on && provs && !provs.length ? "" : ""; return; }
    const mine = ++seq;
    line.textContent = "Counting the pictures…";
    let r = null;
    try { r = await estimate?.({ style: slug(), level: st.level, portraits: st.cast, quality: st.quality, provider: st.provider.id, model: st.model }); } catch { r = null; }
    if (mine !== seq) return;
    if (r && Number.isFinite(+r.pictures)) {
      const n = +r.pictures;
      line.textContent = `About ${n} picture${n === 1 ? "" : "s"}, ${money(r.price)}${r.price != null && +r.price > st.cap ? `. The run stops at $${Number.isInteger(st.cap) ? st.cap : st.cap.toFixed(2)}` : ""}.`;
    } else {
      const rough = fallback?.(st);
      line.textContent = rough || "The count is worked out when the index is written.";
    }
  }
  paint();
  return {
    el,
    config: () => (st.on && st.provider && slug() ? { style: slug(), level: st.level, portraits: st.cast === "none" ? false : st.cast, quality: st.quality,
      provider: st.provider.id, model: st.model || null, cap: st.cap } : null),
    refresh,
    ready: () => Boolean(st.provider && slug()),
  };
}
