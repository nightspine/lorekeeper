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
    model: typeof d.model === "string" ? d.model : null,
    // The text models of each responsibility ({ provider, model } or null: the server's default) and the portraits' image
    // model (null: the same as scenes). A choice whose provider or model is no longer offered is ignored where it is used.
    writer: textPick(d.writer), portraitWriter: textPick(d.portraitWriter), picker: textPick(d.picker),
    portraitProvider: typeof d.portraitProvider === "string" && d.portraitProvider ? d.portraitProvider : null,
    portraitModel: typeof d.portraitModel === "string" && d.portraitModel ? d.portraitModel : null };
}
const textPick = (v) => (v && typeof v === "object" && typeof v.provider === "string" && v.provider && typeof v.model === "string" && v.model
  ? { provider: v.provider, model: v.model } : null);
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

/** Every provider row of /api/providers, cached for a minute ([] when the call fails). */
let provCache = null;
function allProviders({ fresh = false } = {}) {
  if (fresh || !provCache || Date.now() - provCache.at > 60000) {
    const promise = api.providers().then((r) => {
      const all = Array.isArray(r?.providers) ? r.providers : [];
      for (const p of all) {
        if (typeof p?.label === "string") PROVIDER_NAME.set(p.id, p.label);
        for (const m of imageModels(p)) MODEL_LABEL.set(`${p.id}\n${m.id}`, m.label || m.id);
        for (const m of textModels(p)) TEXT_LABEL.set(`${p.id}\n${m.id}`, m.label || m.id);
      }
      return all;
    }).catch(() => []);
    provCache = { at: Date.now(), promise };
  }
  return provCache.promise;
}
/** The image providers that can draw now: configured cloud providers that have an image table. */
export function imageProviders(opts = {}) {
  return allProviders(opts).then((all) => all.filter((p) => canDraw(p)));
}
/** The providers that can write prompts now: every cloud provider whose key works and that lists a text model (Ollama is
 *  not one of them: /api/providers lists cloud providers only). */
export function textProviders(opts = {}) {
  return allProviders(opts).then((all) => all.filter((p) => p && p.ok && textModels(p).length));
}
/** Both lists from one request: { image, text }. */
export function modelProviders(opts = {}) {
  return allProviders(opts).then((all) => ({ image: all.filter((p) => canDraw(p)), text: all.filter((p) => p && p.ok && textModels(p).length) }));
}
/** Text model names by "provider\nid", and provider names by id (the lightbox names the model that wrote a prompt). */
const TEXT_LABEL = new Map();
const PROVIDER_NAME = new Map();
/** A provider's text models as Settings lists them: presets, then live ones the presets lack; a custom server's
 *  configured model when it lists none. [{ id, label, price: [in, out] per million tokens | null, tier }] */
export function textModels(p) {
  if (!p) return [];
  const seen = new Set(), out = [];
  for (const m of [...(Array.isArray(p.models) ? p.models : []), ...(Array.isArray(p.liveModels) ? p.liveModels : [])]) {
    if (!m || typeof m.id !== "string" || !m.id || seen.has(m.id)) continue;
    seen.add(m.id); out.push(m);
  }
  if (!out.length && typeof p.default === "string" && p.default) out.push({ id: p.default, label: p.default, price: null, tier: null });
  return out;
}
const dollars = (v) => `$${v < 1 ? v.toFixed(2) : v.toFixed(v % 1 ? 2 : 0)}`;
/** "Claude Haiku 5.5, $0.10 in and $0.50 out per million tokens", or "..., price unknown". */
export function textModelLine(m) {
  const pr = Array.isArray(m?.price) && m.price.length === 2 && m.price.every((x) => Number.isFinite(+x)) ? m.price : null;
  return `${m.label || m.id}, ${pr ? `${dollars(+pr[0])} in and ${dollars(+pr[1])} out per million tokens` : "price unknown"}`;
}
/** A text model's name ("Claude Sonnet 5.5"), else its id. */
export const textModelLabel = (provider, id) => (id ? TEXT_LABEL.get(`${provider}\n${id}`) || id : "");
/** "Claude Sonnet 5.5 on Claude": who wrote a picture's prompt, from a record's `writer`; "" when it does not say. */
export function writerFact(w) {
  if (!w || typeof w !== "object" || typeof w.model !== "string" || !w.model) return "";
  const prov = typeof w.provider === "string" ? PROVIDER_NAME.get(w.provider) || PROVIDER_LABEL[w.provider] || w.provider : "";
  const name = textModelLabel(w.provider, w.model);
  return prov && !name.toLowerCase().includes(prov.toLowerCase()) ? `${name} on ${prov}` : name;
}
/** A saved or chosen text model when it can still be used ({ provider, model } of a working provider that lists it),
 *  else null: the server's default then. */
export function textChoice(provs, want) {
  if (!want || typeof want.provider !== "string" || typeof want.model !== "string") return null;
  const p = provs.find((x) => x.id === want.provider);
  return p && textModels(p).some((m) => m.id === want.model) ? { provider: p.id, model: want.model } : null;
}
/** The text model the server uses when none is chosen: the `tier` model of `prefer` (a run's own cloud provider) when it
 *  works, else of the first working provider. */
function tierDefault(provs, tier, prefer = null) {
  const first = provs.find((p) => p.id === prefer);
  for (const p of first ? [first, ...provs.filter((x) => x !== first)] : provs) {
    const id = typeof p[tier] === "string" && p[tier] ? p[tier] : null;
    const m = id ? textModels(p).find((x) => x.id === id) : textModels(p)[0];
    if (m) return { p, m };
  }
  return null;
}
/** The first option of a writer select: "Default: Claude Haiku 5.5, $0.10 in and ...". */
const defaultLine = (provs, tier, prefer = null) => { const def = tierDefault(provs, tier, prefer); return def ? `Default: ${textModelLine(def.m)}` : "Default"; };
/** Say again which model Default stands for in a writer select made by `writerSelect` (the run's provider changed). */
export function setWriterDefault(sel, provs, tier, prefer = null) {
  const opt = sel?.querySelector("option[value='']");
  if (opt) opt.textContent = defaultLine(provs, tier, prefer);
}
const SEP = "|";   // provider ids are slugs, so "provider|model" splits at its first bar whatever the model id holds
const splitChoice = (v) => { const i = String(v || "").indexOf(SEP); return i > 0 ? { provider: v.slice(0, i), model: v.slice(i + 1) } : null; };
/**
 * A select of every text model of the working providers, grouped by provider, each with its price per million tokens;
 * the first option is the server's default for `tier` ("Default: Claude Haiku 5.5"). `value` is { provider, model } or
 * null; `onChange(choice)` gets the same.
 */
export function writerSelect({ id, provs, value = null, tier = "cheap", prefer = null, disabled = false, onChange, label = null }) {
  const cur = textChoice(provs, value);
  return h(`select.select.art-writer#${id}`, { disabled, ...(label ? { "aria-label": label } : {}), on: { change: (e) => onChange?.(splitChoice(e.target.value)) } },
    h("option", { value: "", selected: !cur }, defaultLine(provs, tier, prefer)),
    provs.map((p) => h("optgroup", { label: p.label }, textModels(p).map((m) =>
      h("option", { value: `${p.id}${SEP}${m.id}`, selected: Boolean(cur && cur.provider === p.id && cur.model === m.id) }, textModelLine(m))))));
}
/** The portrait image model chosen ({ provider, model } of a provider that draws and lists it), else null: same as scenes.
 *  A saved model the provider no longer lists is never swapped for its default: it falls back to the same as scenes. */
export function portraitImageChoice(provs, provider, model) {
  const p = typeof provider === "string" ? provs.find((x) => x.id === provider) : null;
  if (!p) return null;
  const ms = imageModels(p);
  if (!ms.length) return { provider: p.id, model: null };   // an older server with no image table: it picks the model itself
  return typeof model === "string" && ms.some((m) => m.id === model) ? { provider: p.id, model } : null;
}
/** "Portraits drawn with": "Same as scenes" first, then every image model grouped by provider, priced at `quality`. */
export function portraitImageSelect({ id, provs, provider = null, model = null, quality = "standard", disabled = false, onChange }) {
  const cur = portraitImageChoice(provs, provider, model);
  return h(`select.select.art-pimage#${id}`, { disabled, on: { change: (e) => onChange?.(splitChoice(e.target.value)) } },
    h("option", { value: "", selected: !cur }, "Same as scenes"),
    provs.map((p) => {
      const ms = imageModels(p);
      // An older server lists no image table: the provider alone, drawn with its default.
      if (!ms.length) return h("option", { value: `${p.id}${SEP}`, selected: Boolean(cur && cur.provider === p.id) }, p.label);
      return h("optgroup", { label: p.label }, ms.map((m) =>
        h("option", { value: `${p.id}${SEP}${m.id}`, selected: Boolean(cur && cur.provider === p.id && cur.model === m.id) }, imageLine(p, quality, m.id))));
    }));
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

// ---- figure labels: which picture is which figure in a scene prompt -------------------------------

const LABEL_RE = /\b[Ff]igure ([A-D])\b/g;
/** The labels a prompt text uses ("figure A"), each once, in label order: the same rule the server draws by. */
export function labelsIn(text) {
  const seen = new Set();
  for (const m of String(text || "").matchAll(LABEL_RE)) seen.add(m[1]);
  return [...seen].sort();
}
/** The figures a server reply or record carries, checked: [{ label, entity, ref }] with ref the raw reference or null. */
export function cleanFigures(list) {
  if (!Array.isArray(list)) return [];
  return list.filter((f) => f && typeof f.label === "string" && /^[A-D]$/.test(f.label) && typeof f.entity === "string" && f.entity)
    .map((f) => ({ label: f.label, entity: f.entity, ref: f.ref ?? null }))
    .sort((a, b) => (a.label < b.label ? -1 : 1));
}
/** The id and set of a reference as stored (a plain id, { set, id }) or as the sheet's reply gives it ({ id, set?, url }). */
export function refParts(ref) {
  if (typeof ref === "string") return ID_RE.test(ref) ? { id: ref, set: null } : null;
  if (ref && typeof ref === "object" && typeof ref.id === "string" && ID_RE.test(ref.id)) return { id: ref.id, set: typeof ref.set === "string" ? ref.set : null };
  return null;
}
/**
 * One quiet line per figure: "Figure A is Greyhand" with a 24px round thumbnail of its reference. `name(entity)` gives the
 * name from the reader's own view (null: not visible there, so just "Figure A"), `thumb(figure)` the picture's URL or null
 * (the caller decides what may show). Only labels in `text` (when given) are listed. Null when there is nothing to list.
 */
export function figureLines(figures, { text = null, name = () => null, thumb = () => null } = {}) {
  const used = text == null ? null : new Set(labelsIn(text));
  const rows = cleanFigures(figures).filter((f) => !used || used.has(f.label));
  if (!rows.length) return null;
  return h("ul.art-figures", { "aria-label": "Which picture is which figure" }, rows.map((f) => {
    const nm = name(f.entity), src = thumb(f);
    const img = src ? artImg(src, "", "art-fig-img") : null;
    return h("li.art-figure", img ? h("span.art-fig-face", img) : null, h("span", nm ? `Figure ${f.label} is ${nm}` : `Figure ${f.label}`));
  }));
}

// ---- the lightbox ----------------------------------------------------------------------------------

const when = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : ""; };

/**
 * One picture at full size, with its caption and a closed "How this was drawn". `items` are
 * [{ rec, src, caption, key, canEdit, canRedraw, newPrompt, figureName(entity), figureThumb(rec, figure) }] in the order the arrows step through (only visible pictures
 * are ever passed). `newPrompt(rec)` opens the picture sheet to write a new prompt for the same kind and target, whose
 * picture replaces this one; without it Redraw only draws the saved prompt again. `onChanged()` runs after a redraw, a
 * delete or an undo.
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
      ["Prompt written by", writerFact(r.writer)],
      ["Quality", r.quality ? `${r.quality[0].toUpperCase()}${r.quality.slice(1)}` : null],
      ["Cost", typeof r.cost === "number" ? (r.cost > 0 ? money(r.cost).replace(/^about /, "") : "Price unknown") : null],
      ["Drawn", when(r.created)],
    ].filter(([, v]) => v);
    // The prompt may carry an earlier book's world note or looks: it shows only once those books are finished.
    const hiddenUntil = it.promptHiddenUntil ? it.promptHiddenUntil(r) : null;
    clear(how, h("details.lb-details", h("summary", icon("chevron"), "How this was drawn"),
      hiddenUntil ? h("div.lb-details-body", h("p.lb-hidden", `How this was drawn is hidden until you finish ${hiddenUntil}.`)) : h("div.lb-details-body",
        typeof r.prompt === "string" && r.prompt ? h("div.lb-prompt", h("span.label", "Prompt"), h("p", r.prompt)) : null,
        h("div.lb-figs", lbFigures(it)),
        h("dl.lb-facts", facts.map(([k, v]) => [h("dt", k), h(k === "Model" ? "dd.lb-model" : k === "Prompt written by" ? "dd.lb-writer" : "dd", v)])))));
    clear(acts, it.canEdit ? [
      it.canRedraw === false ? null : h("button.btn.quiet", { type: "button", on: { click: () => redraw(it) } }, icon("refresh"), "Redraw"),
      h("button.btn.quiet.danger", { type: "button", on: { click: () => remove(it) } }, icon("trash"), "Delete"),
    ] : null);
  }
  const lbFigures = (it) => figureLines(it.rec.figures, { name: it.figureName || undefined, thumb: (f) => (it.figureThumb ? it.figureThumb(it.rec, f) : null) });
  /** Names and thumbnails again from the reader's view now (a position that moved back hides what it no longer reveals). */
  function refresh() { const box = how.querySelector(".lb-figs"); if (box) clear(box, lbFigures(items[i])); }
  function undoable(it, r, done) {
    const id = r?.undo ?? r?.journal ?? null;
    // While the lightbox is open its own stack; after it closed, the modal it was opened from (the entry drawer, a sheet),
    // since a modal makes the page's stack unreachable.
    toast(done, "ok", { host: dlg.open ? toasts : modalToasts(), action: id ? { label: "Undo", run: async () => {
      if (dlg.open) close();   // the picture on screen is the one being undone
      try { await api.undo(it.key, id); forgetArt(it.key); toast("Undone", "ok", { host: modalToasts() }); onChanged?.(); } catch (e) { toast(e.message, "error", { host: modalToasts() }); }
    } } : null });
  }
  /** Redraw: write a new prompt (the picture sheet, at the reader's position) or draw the saved one again. The default,
   *  first and focused, is a new prompt for a picture drawn before 0.18.0 (no `notes`, or under 3: an older kind of prompt). */
  function redraw(it) {
    if (!it.newPrompt) { sameNote(it); return; }
    const older = !(typeof it.rec.notes === "number" && it.rec.notes >= 3);
    const fresh = () => h(`button.btn${older ? ".primary" : ""}`, { type: "button", on: { click: () => { const rec = it.rec; close(); it.newPrompt(rec); } } },
      icon("sparkle"), "Write a new prompt");
    const same = () => h(`button.btn${older ? "" : ".primary"}`, { type: "button", on: { click: () => sameNote(it) } }, icon("refresh"), "Same prompt");
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => paint() } }, "Cancel");
    const choices = older ? [fresh(), same()] : [same(), fresh()];
    clear(acts, h("div.lb-redraw.lb-choice", { role: "group", "aria-label": "Redraw" },
      h("p.hint", older
        ? "This picture was drawn from an older kind of prompt. A new prompt is written from what you have read so far."
        : "A new prompt is written from what you have read so far. The same prompt draws the saved text again, with a note if you like."),
      h("div.btns", choices, cancel)));
    choices[0].focus();
  }
  function sameNote(it) {
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
    const r = dlg.isConnected ? items[i]?.rec : null;
    if (!r) return;
    const dd = how.querySelector(".lb-model"), wd = how.querySelector(".lb-writer");
    if (dd) dd.textContent = modelFact(r);
    if (wd) wd.textContent = writerFact(r.writer) || wd.textContent;
  });
  return { close, refresh };
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
 *   reached(anchor)                   whether an anchor is at or before the position now: a prompt or rewrite that comes
 *                                     back written further on (the reader moved back meanwhile) is dropped and written again
 *   onDrawn(rec, slug)                after a picture was filed (slug is the server's, null when it did not say)
 *   figureName(entity)   a figure's name from the reader's view (null: not visible there, so only \"Figure A\")
 *   replaces     a picture id of the art set: Redraw with a new prompt. The sheet is titled "Redraw", Draw sends it and the
 *                new picture takes that one's place (Undo puts it back; `onUndone()` runs after)
 *   replacesStyle  the slug of that picture's set: drawn in another style, the picture is a new one in that style's set and
 *                the old one stays (no `replaces` is sent)
 * Returns the sheet, with `current()` → { kind, target } (the moment selected now) and `listed()` → every item named on it.
 */
export async function openPictureSheet(opts) {
  const { image: provs, text: writers } = await modelProviders({ fresh: true });
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
    busy: false, closed: false, figures: [],   // figures: the labelled figures of the prompt on screen
    writer: null,   // the text model that writes the prompt ({ provider, model }); null: the server's default, the strong tier
  };
  if (!STYLE_LABEL[st.style]) {
    st.custom = (opts.styles || []).find((x) => x.slug === st.style)?.text || st.style.replace(/-/g, " ");
    st.slug = STYLE_LABEL[st.style] ? null : opts.style;   // the art set's own slug for its custom style
    st.style = "custom";
  }
  // The Settings model when it is the Settings provider's, else the provider's default (null: an older server picks).
  st.model = modelFor(st.provider, st.provider.id === d.provider ? d.model : null);
  // A portrait, place or thing starts from the Settings portrait image model, when one is chosen and can draw.
  const forPortraits = ENTITY_KINDS.has(st.kind) ? portraitImageChoice(provs, d.portraitProvider, d.portraitModel) : null;
  if (forPortraits) { st.provider = provs.find((p) => p.id === forPortraits.provider); st.model = forPortraits.model; }
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
  const replaces = typeof opts.replaces === "string" && ID_RE.test(opts.replaces) ? opts.replaces : null;
  const sh = sheet({ title: `${replaces ? "Redraw" : "Draw a picture"}${opts.title ? `: ${opts.title}` : ""}`, className: "art-sheet", content: body,
    footer: h("div.art-foot", footNote, drawBtn),
    // Always closable: a prompt in flight is abandoned; a drawing already sent is not (the server files it either way), and
    // when it lands its toast, with Undo for a redraw, shows where the sheet was opened from. An edited prompt is asked about first.
    beforeClose: () => {
      if (st.drawing) return true;
      if (st.busy) { abort(); return true; }
      if (!st.prompt || st.prompt === st.written) return true;
      confirmDialog({ title: "Discard your prompt?", message: "The prompt you changed is not kept.", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { st.prompt = st.written; sh.close(true); } });
      return false;
    },
    onClose: () => {
      st.closed = true;
      if (!st.drawing) abort();
      if (undoRedraw) toast("Picture redrawn", "ok", { host: modalToasts(), action: { label: "Undo", run: () => undoRedraw?.() } });
    } });
  let undoRedraw = null;   // after a redraw with a new prompt: puts the old picture back
  sh.current = () => ({ kind: st.kind, target: st.target });
  sh.refreshFigures = () => paintFigures();
  sh.listed = () => [sh.current(), ...(opts.moments || []).map((m) => ({ kind: m.kind, target: m.target }))];
  /** The anchors the text on screen was written up to: the prompt's (the picture's own, any world-note hold included) and
   *  the world note's own anchor. The Reader checks them against the position as it checks the moments. */
  sh.anchors = () => [st.anchor, opts.world?.from].filter((a) => typeof a === "string");
  /** The position moved back before that text: drop the prompt and the World line and write a new prompt here. */
  sh.reposition = ({ world = null, canEditWorld = false } = {}) => {
    opts.world = world || null;
    opts.canEditWorld = canEditWorld;   // decided again for the position now: a note hidden here is never written over
    st.prompt = st.written = "";
    st.anchor = null; st.promptFor = null; st.asked = null; st.price = null; st.parts = null; st.sentence = null; st.missing = false; st.figures = [];
    write();
  };

  const textarea = h("textarea.input.art-prompt", { rows: "7", "aria-label": "Prompt", spellcheck: "true" });
  textarea.addEventListener("input", () => { st.prompt = textarea.value; paintFoot(); paintFigures(); });
  const err = h("div.art-error", { role: "alert" });

  /** Is a reply's anchor still reached at the position now? A reply written further on than the reader is now (they moved
   *  back while it was being written) is never put on screen. `opts.reached` comes from the Reader; without it, yes. */
  const reachedNow = (r) => !(opts.reached && typeof r?.anchor === "string" && !opts.reached(r.anchor));
  /** A reply came back for a position the reader has left: drop it and what it replaced, and write the prompt again here,
   *  once; a second miss says so and waits for the person. */
  function moved(retried) {
    st.prompt = st.written = "";
    st.anchor = null; st.promptFor = null; st.asked = null; st.price = null; st.parts = null; st.sentence = null; st.missing = false; st.figures = [];
    textarea.value = "";
    if (!retried) { write({ retried: true }); return; }
    clear(body, h("div.art-error", { role: "alert" }, h("p", "Your position moved while the prompt was being written.")),
      h("button.btn", { type: "button", on: { click: () => write() } }, "Write it again"));
    paintFoot();
  }
  async function write(how) {
    const retried = how?.retried === true;
    abort();
    const mine = ++gen;
    ctrl = new AbortController();
    st.busy = true;
    clear(body, loading("Writing a prompt from what you have read…"));
    paintFoot();
    const asked = { style: styleText(), quality: st.quality, provider: st.provider.id, model: st.model, shape: st.shape };
    const writer = textChoice(writers, st.writer);
    try {
      const r = await api.artPrompt(opts.key, { kind: st.kind, target: st.target, style: asked.style, shape: asked.shape,
        quality: asked.quality, provider: asked.provider, ...(asked.model ? { model: asked.model } : {}), ...(writer ? { writer } : {}), ...here() }, ctrl.signal);
      if (mine !== gen || st.closed) return;
      if (!reachedNow(r)) { st.busy = false; ctrl = null; moved(retried); return; }
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
      st.figures = cleanFigures(r.figures);
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
  /** Rewrite a refused prompt: one writer call for the same moment at the position now, told so it can be drawn. The new text
   *  replaces the prompt in the box, in the style chosen now; nothing is drawn until the person presses Draw. */
  async function soften(text, reason) {
    abort();
    const mine = ++gen;
    ctrl = new AbortController();
    st.busy = true;
    clear(err, loading("Rewriting the prompt so it can be drawn…"));
    paintFoot();
    const asked = { style: styleText(), quality: st.quality, provider: st.provider.id, model: st.model, shape: st.shape };
    const writer = textChoice(writers, st.writer);
    try {
      const r = await api.artSoften(opts.key, { kind: st.kind, target: st.target, style: asked.style, shape: asked.shape,
        quality: asked.quality, provider: asked.provider, ...(asked.model ? { model: asked.model } : {}), ...(writer ? { writer } : {}), text, reason, ...here() }, ctrl.signal);
      if (mine !== gen || st.closed) return;
      if (!reachedNow(r)) { st.busy = false; ctrl = null; moved(false); return; }
      const parts = promptParts(r);
      if (parts) {
        st.parts = parts;
        st.sentence = sentenceFor(parts, st.style, st.custom) ?? squash(parts.style);
        st.prompt = st.written = composePrompt(parts.body, st.sentence, parts.noText);
      } else if (typeof r.prompt === "string" && r.prompt) {
        st.parts = null; st.sentence = null;
        st.prompt = st.written = r.prompt;
      } else throw new Error("No prompt came back");
      st.missing = false;
      st.figures = cleanFigures(r.figures);
      if (typeof r.price === "number") st.price = r.price;
      if (typeof r.anchor === "string") st.anchor = r.anchor;
      st.asked = asked; st.promptStyle = asked.style;
      st.busy = false; ctrl = null;
      textarea.value = st.prompt;
      paintStyleNote();
      paintFigures();
      clear(err, h("p.art-ok", "Rewritten so it can be drawn. Read it, change anything, then Draw."));
      paintFoot();
      textarea.focus();
    } catch (e2) {
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      clear(err, h("p", promptError(e2).replace(/^Couldn't write a prompt/, "Couldn't rewrite it")),
        e2.code === "not-visible" ? null : h("button.btn", { type: "button", on: { click: () => soften(text, reason) } }, "Try again"));
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
  /** Under the box: which reference picture is which figure, as the text stands now (a label taken out drops its line). */
  const figuresBox = h("div.art-figures-box");
  function paintFigures() {
    const pic = (f) => { const u = f.ref && typeof f.ref === "object" ? f.ref.url : null; return typeof u === "string" && /^\/api\/art\/[^/]+\/[^/]+\/[A-Za-z0-9_-]{1,64}$/.test(u) ? u : null; };
    clear(figuresBox, figureLines(st.figures, { text: st.prompt, name: opts.figureName || undefined, thumb: pic }));
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
          h("button.linkish", { type: "button", on: { click: write } }, "Write it again")),
        figuresBox),
      h("div.art-field", h("span.label", "Style"), styleRow, st.style === "custom" ? customField : null, styleNote),
      h("div.art-row",
        h("div.art-field", h("span.label", "Shape"), seg("Shape", SHAPES, st.shape, (k) => { st.shape = k; })),
        h("div.art-field", h("span.label", "Quality"), qualitySeg),
        provSel ? h("div.art-field", h("label.label", "Draw with"), provSel) : null),
      modelPick ? h("div.art-field", models.length <= 3 ? h("span.label", "Model") : h("label.label", { for: "art-model" }, "Model"), modelPick) : null,
      // Who writes the prompt: kept for "Write it again" (and a rewrite after a refusal); choosing one never writes it again.
      writers.length ? h("div.art-field", h("label.label", { for: "art-writer" }, "Prompt written by"),
        writerSelect({ id: "art-writer", provs: writers, value: st.writer, tier: "strong", onChange: (c) => { st.writer = c; } })) : null,
      worldLine(),
      err);
    textarea.id = "art-prompt";
    paintStyleNote();
    paintFigures();
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
    st.busy = true; st.drawing = true; drawBtn.disabled = true;
    // The old picture is replaced only within its own set: another style files a new picture there and keeps the old one.
    const replacing = Boolean(replaces) && (!opts.replacesStyle || folder() === opts.replacesStyle);
    const sent = st.prompt;   // the text as the provider saw it (a rewrite after a refusal starts from this)
    const keep = [...body.childNodes];
    clear(body, loading("Drawing the picture. This takes a few seconds…"));
    footNote.textContent = "";
    try {
      const r = await api.artDraw(opts.key, { kind: st.kind, target: st.target, style: styleText(), shape: st.shape, quality: st.quality,
        prompt: st.prompt, provider: st.provider.id, ...(st.model ? { model: st.model } : {}), ...(replacing ? { replaces } : {}), ...here() });
      st.drawing = false;
      const rec = r?.record || r;
      const slug = st.style === "custom" ? (typeof r?.slug === "string" && /^[a-z0-9-]{1,64}$/.test(r.slug) ? r.slug : folder()) : folder();
      forgetArt(opts.key);
      opts.onDrawn?.(rec, slug || null);
      const undoId = replacing ? (r?.undo ?? r?.journal ?? null) : null;
      if (undoId) undoRedraw = async () => {
        undoRedraw = null;
        try {
          await api.undo(opts.key, undoId);
          forgetArt(opts.key);
          if (!st.closed) sh.close(true);
          toast("Undone", "ok", { host: modalToasts() });
          opts.onUndone?.();
        } catch (e) { toast(e.message, "error", { host: st.closed ? modalToasts() : sh.toasts }); }
      };
      if (st.closed) {
        // Closed while it was drawing: say so where the sheet was opened from, with Undo for a redraw.
        toast(replacing ? "Picture redrawn" : "Picture drawn", "ok", { host: modalToasts(), action: undoRedraw ? { label: "Undo", run: () => undoRedraw?.() } : null });
        return;
      }
      if (mine !== gen) {
        // The sheet moved on meanwhile (the position moved back and it writes a new prompt): the picture is filed all the same.
        toast(replacing ? "Picture redrawn" : "Picture drawn", "ok", { host: sh.toasts, action: undoRedraw ? { label: "Undo", run: () => undoRedraw?.() } : null });
        return;
      }
      st.busy = false;
      st.written = st.prompt;   // drawn: nothing to ask about on close
      // The new picture shows here only if it may show at the position (its anchor is the item's own, which is visible,
      // but the server decides the anchor; the Reader checks it as it checks every picture).
      const src = slug && opts.srcFor && (!opts.canShow || opts.canShow(rec)) ? opts.srcFor(rec, slug) : null;
      const added = replacing ? "Drawn. It takes the old picture's place in the art set." : "Drawn and added to the art set.";
      clear(body, h("div.art-done", src ? artImg(src, opts.title, "art-done-img", { eager: true }) : null,
        h("p", src ? added : `${added} It shows where it belongs in the book.`),
        h("div.btns", h("button.btn.primary", { type: "button", on: { click: () => sh.close(true) } }, "Done"))));
      drawBtn.hidden = true; footNote.textContent = typeof rec?.cost === "number" && rec.cost > 0 ? `Cost ${money(rec.cost).replace(/^about /, "")}` : "";
      // A redraw took the old picture out of the set: Undo puts it back (and takes the new one out).
      // The toast is offered again where the sheet was opened from once it closes, until it is used.
      if (undoRedraw) toast("Picture redrawn", "ok", { host: sh.toasts, action: { label: "Undo", run: () => undoRedraw?.() } });
    } catch (e) {
      st.drawing = false;
      if (st.closed) { toast(e.code === "refused" ? `The provider would not draw it: ${e.message}` : `Couldn't draw it: ${e.message}`, "error", { host: modalToasts() }); return; }
      if (mine !== gen) return;
      st.busy = false;
      clear(body, keep);
      if (e.code === "refused") {
        // The person stays in control: a rewrite of the same moment, told so it can be drawn, goes into the prompt box to be
        // read before Draw. A second refusal offers it again.
        const fix = h("button.btn", { type: "button", on: { click: () => soften(sent, e.message || "") } }, icon("sparkle"), "Rewrite it so it can be drawn");
        clear(err, h("p", `The provider would not draw this: ${e.message}. Change the prompt and try again, or have it rewritten.`), fix);
      } else clear(err, h("p", `Couldn't draw it: ${e.message}`));
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
 * { el, config() → the job's `art` object or null when "Draw pictures" is off, refresh(), reason() → why nothing would be
 * drawn ("" when something would) }. `estimate(params)` is asked for the line "About 52 pictures, about $1.75";
 * `fallback()` gives a rough count when it cannot answer; `onChange()` is told after every choice.
 * With `chapters` on (the book page, where the index exists) the form lists the book's chapters from the estimate's
 * answer, each with a checkbox and its count at the chosen level; the config then carries `chapters` (plan numbers `n`)
 * when not every chapter that can get a picture is chosen.
 */
// The portrait choice: `st.cast` is "main", "all" or "none"; the job config sends "main", "all" or false, the estimate the word.
const CAST_SUMMARY = { main: ", main characters' portraits", all: ", everyone's portraits", none: "" };
const LEVEL_SUMMARY = { main: "one per chapter", all: "every moment", none: "no chapter pictures" };
export const NOTHING_CHOSEN = "Choose at least one chapter, or portraits.";
export const ALL_DRAWN = "Everything you chose is already drawn.";

/** Whether a chapter row can be ticked: it would get a picture at the chosen level (`pictures` > 0; a chapter whose events
 *  are all minor still gets its recap plate). An answer without `pictures` falls back on the chapter having events. */
export const chapterChoosable = (c) => (c?.pictures != null && Number.isFinite(+c.pictures) ? +c.pictures > 0 : +c?.events > 0);

/** What a chapter row says beside its box: "2 pictures", "2 pictures, 1 already drawn", "already drawn", "nothing to draw". */
export function chapterCount(c) {
  const n = c?.pictures != null && Number.isFinite(+c.pictures) ? +c.pictures : null, done = Number.isFinite(+c?.drawn) ? +c.drawn : 0;
  if (!chapterChoosable(c)) return "nothing to draw";
  if (n == null) return "";
  if (done >= n) return "already drawn";
  return `${n} picture${n === 1 ? "" : "s"}${done ? `, ${done} already drawn` : ""}`;
}

export function picturesForm({ estimate, fallback = null, open = false, always = false, idPrefix = "art", chapters: listChapters = false, onChange = null, runProvider = null } = {}) {
  const d = artDefaults();
  const st = { on: always, style: STYLE_LABEL[d.style] ? d.style : "custom", custom: STYLE_LABEL[d.style] ? "" : d.style, level: "main", cast: "main",
    quality: d.quality, provider: null, model: null, cap: d.cap,
    // The Models group: each responsibility's text model (null: the server's default) and the portraits' image model
    // (null provider: the same as scenes), from the Settings defaults.
    writer: d.writer, portraitWriter: d.portraitWriter, picker: d.picker, pProvider: d.portraitProvider, pModel: d.portraitModel, modelsOpen: false };
  let provs = null, writers = [];
  // The chapter list: `chs` is the estimate's last list (null before it, or from a server that does not number chapters),
  // `picked` the chosen plan numbers, every chapter that can get a picture at first. What can be ticked follows the level
  // (`chapterChoosable`); a row that stops being choosable keeps its tick for when it is again, and is never counted or sent
  // meanwhile. A chapter choosable for the first time starts on.
  let chs = null;
  const picked = new Set(), seen = new Set();
  const choosable = () => (chs || []).filter(chapterChoosable);
  const allPicked = () => choosable().every((c) => picked.has(c.n));
  const chosenNs = () => choosable().filter((c) => picked.has(c.n)).map((c) => c.n);
  const noScenes = () => st.level === "none" || (chs != null && !chosenNs().length);
  // Why nothing would be drawn. `choiceReason` is known before asking (no call is made then); `allDrawn` comes from the
  // last estimate (it counts only pictures still to draw) and applies only to the book page dialog, where the run draws
  // pictures and nothing else: on the build form the index is written again and the pictures follow the new one.
  let allDrawn = false;
  const choiceReason = () => (st.on && noScenes() && st.cast === "none" ? NOTHING_CHOSEN : "");
  const reason = () => choiceReason() || (st.on && allDrawn ? ALL_DRAWN : "");
  const chapterParam = () => (st.level === "none" || !chs || allPicked() ? undefined : chosenNs());
  const chList = h("div.art-chapters", { role: "group", "aria-labelledby": `${idPrefix}-ch-h` });
  const chCount = h("span.art-ch-count");
  const chField = listChapters ? h("div.field.art-ch-field", { hidden: true },
    h("div.art-ch-head", h("span.label", { id: `${idPrefix}-ch-h` }, "Chapters"), chCount,
      h("button.linkish", { type: "button", on: { click: () => setPicked(true) } }, "All"),
      h("button.linkish", { type: "button", on: { click: () => setPicked(false) } }, "None")),
    chList) : null;
  const rowsByN = new Map();
  function setPicked(on) {
    for (const c of choosable()) on ? picked.add(c.n) : picked.delete(c.n);
    for (const [n, r] of rowsByN) r.box.checked = picked.has(n) && !r.box.disabled;
    chosen();
  }
  function chosen() {
    paintChapterHead();
    onChange?.();
    refresh();
  }
  function paintChapterHead() {
    if (!chField) return;
    const all = choosable().length, n = chosenNs().length;
    chCount.textContent = all ? `${n} of ${all} chosen` : "";
    for (const r of rowsByN.values()) r.row.classList.toggle("off", !r.box.checked);
  }
  // Rows are built once per list of chapters and kept, so a new estimate updates the counts in place and never moves focus.
  function paintChapters() {
    if (!chField) return;
    chField.hidden = !st.on || st.level === "none" || !chs || !chs.length;
    if (!chs) return;
    const sig = chs.map((c) => `${c.n}:${c.title}:${chapterChoosable(c)}`).join("|");
    if (chList.dataset.sig !== sig) {
      chList.dataset.sig = sig;
      rowsByN.clear();
      clear(chList, chs.map((c) => {
        const can = chapterChoosable(c);
        const box = h("input", { type: "checkbox", checked: can && picked.has(c.n), disabled: !can });
        const count = h("span.art-ch-n");
        const row = h(`label.art-ch-row${can ? "" : ".none"}`, box, h("span.art-ch-title", { title: c.title }, c.title), count);
        box.addEventListener("change", () => { box.checked ? picked.add(c.n) : picked.delete(c.n); chosen(); });
        rowsByN.set(c.n, { row, box, count });
        return row;
      }));
    }
    for (const c of chs) {
      const r = rowsByN.get(c.n);
      if (r) r.count.textContent = chapterCount(c);
    }
    paintChapterHead();
  }
  function takeChapters(list) {
    if (!listChapters || !Array.isArray(list) || !list.length || !list.every((c) => Number.isInteger(c?.n))) { chs = null; return; }
    for (const c of list) if (chapterChoosable(c) && !seen.has(c.n)) { seen.add(c.n); picked.add(c.n); }
    chs = list;
  }
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
  modelProviders().then(({ image: list, text }) => {
    provs = list;
    writers = text;
    const p = list.find((x) => x.id === d.provider) || list[0] || null;
    pickProvider(p, p && p.id === d.provider ? d.model : null);
    paint();
  });
  const slug = () => (st.style === "custom" ? st.custom.trim() : st.style);
  function paint() {
    sum.textContent = st.on ? `${styleLabel(slug())}, ${LEVEL_SUMMARY[st.level]}${CAST_SUMMARY[st.cast]}` : "Off";
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
        radio("level", "all", "Every moment worth drawing"),
        radio("level", "none", "No chapter pictures"))),
      chField,
      h("div.field", h("span.label", "Portraits"), h("div.prows", { role: "radiogroup", "aria-label": "Portraits" },
        radio("cast", "main", "Main characters", "The characters the story is most about, up to 12 a book."),
        radio("cast", "all", "Everyone with a part"),
        radio("cast", "none", "No portraits"))),
      h("div.field", h("span.label", "Quality"), h("div.seg", { role: "group", "aria-label": "Quality" },
        QUALITIES.map(([k, l]) => h("button", { type: "button", "aria-pressed": String(st.quality === k), disabled: !st.on || !levels.includes(k),
          title: levels.includes(k) ? null : `This model has no ${l} quality`, on: { click: () => { st.quality = k; paint(); } } }, l)))),
      modelsGroup(modelSel),
      h("div.field.art-cap", h("label", { for: `${idPrefix}-cap` }, "Stop at"), h("span.art-cap-row", "$", cap),
        h("p.hint", "The run stops drawing there and keeps what it drew.")));
    paintChapters();
    onChange?.();
    refresh();
  }
  /** The five model choices, closed: who writes scene prompts, portrait prompts and the choosing calls, and which image
   *  models draw scenes and portraits. Its open state survives the repaint every choice makes. */
  function modelsGroup(modelSel) {
    const field = (id, label, control) => h("div.field", h("label", { for: id }, label), control);
    // Default is what the server would use: the cheap or strong model of the run's own cloud provider (the build form's
    // "Where should it run?"), else of the first working provider.
    const w = (key, id, tier) => writerSelect({ id, provs: writers, value: st[key], tier, prefer: runProvider?.() || null, disabled: !st.on, onChange: (c) => { st[key] = c; onChange?.(); } });
    const sceneModel = st.provider ? (imageModels(st.provider).find((m) => m.id === st.model)?.label || st.provider.label) : "";
    const pc = provs ? portraitImageChoice(provs, st.pProvider, st.pModel) : null;
    const pp = pc ? provs.find((p) => p.id === pc.provider) : null;
    const portraitModel = pc ? (imageModels(pp).find((m) => m.id === pc.model)?.label || pp.label) : "";
    const det = h("details.advanced.art-models-group", { open: st.modelsOpen || null },
      h("summary", icon("chevron"), "Models", h("span.sum-hint", [sceneModel, portraitModel ? `${portraitModel} for portraits` : ""].filter(Boolean).join(", "))),
      h("div.advanced-body",
        writers.length ? [
          field(`${idPrefix}-writer`, "Scene prompts written by", w("writer", `${idPrefix}-writer`, "cheap")),
          field(`${idPrefix}-pwriter`, "Portrait prompts written by", w("portraitWriter", `${idPrefix}-pwriter`, "strong")),
          field(`${idPrefix}-picker`, "Choosing calls by", w("picker", `${idPrefix}-picker`, "cheap")),
        ] : null,
        provs && provs.length > 1 ? field(`${idPrefix}-prov`, "Draw with",
          h(`select.select#${idPrefix}-prov`, { disabled: !st.on, on: { change: (e) => { pickProvider(provs.find((p) => p.id === e.target.value)); paint(); } } },
            provs.map((p) => h("option", { value: p.id, selected: p.id === st.provider?.id }, p.label)))) : null,
        modelSel ? field(`${idPrefix}-model`, "Image model", modelSel)
          : provs && provs.length === 1 ? h("p.hint", `Drawn with ${provs[0].label}: ${imageLine(provs[0], st.quality, st.model)}.`) : null,
        provs && provs.length ? field(`${idPrefix}-pimage`, "Portraits drawn with", portraitImageSelect({ id: `${idPrefix}-pimage`, provs,
          provider: st.pProvider, model: st.pModel, quality: st.quality, disabled: !st.on,
          onChange: (c) => { st.pProvider = c?.provider || null; st.pModel = c?.model || null; paint(); } })) : null,
        h("p.hint", "Choosing calls pick the moments worth drawing and the details that say how someone looks.")));
    det.addEventListener("toggle", () => { st.modelsOpen = det.open; });
    return det;
  }
  let seq = 0;
  async function refresh() {
    if (!st.on || !st.provider) { seq++; line.textContent = ""; return; }
    const mine = ++seq;
    line.classList.remove("why");
    // Nothing would be drawn: no call, the one sentence saying why stands in for the estimate.
    if (choiceReason()) { line.classList.add("why"); line.textContent = choiceReason(); return; }
    // `allDrawn` stands until the new answer, so Start stays off while a choice that was all drawn is being counted again.
    line.textContent = "Counting the pictures…";
    let r = null;
    const asked = listChapters ? chapterParam() : undefined;
    try {
      const pc = portraitImageChoice(provs || [], st.pProvider, st.pModel);
      r = await estimate?.({ style: slug(), level: st.level, portraits: st.cast, quality: st.quality, provider: st.provider.id, model: st.model,
        ...(pc ? { portraitProvider: pc.provider, ...(pc.model ? { portraitModel: pc.model } : {}) } : {}),
        ...(listChapters ? { chapters: asked } : {}) });
    } catch { r = null; }
    if (mine !== seq) return;
    // A level "none" answer has no chapter counts worth keeping; the list is hidden then and asked again on the way back.
    if (r && st.level !== "none") {
      const was = JSON.stringify([chs?.map((c) => [c.n, chapterChoosable(c)]), reason()]);
      takeChapters(r.chapters);
      paintChapters();
      // A new list can change which rows can be ticked (`pictures` follows the level), so what was asked for may no longer
      // be the choice: ask again with the choice as it is now. Otherwise tell the page when the list or the reason changed.
      if (JSON.stringify(chapterParam()) !== JSON.stringify(asked)) { onChange?.(); refresh(); return; }
      if (JSON.stringify([chs?.map((c) => [c.n, chapterChoosable(c)]), reason()]) !== was) onChange?.();
      if (choiceReason()) { refresh(); return; }
    }
    const drawnBefore = allDrawn;
    allDrawn = Boolean(listChapters && r && Number.isFinite(+r.pictures) && +r.pictures === 0);
    if (allDrawn !== drawnBefore) onChange?.();
    if (allDrawn) { line.classList.add("why"); line.textContent = ALL_DRAWN; return; }
    if (r && Number.isFinite(+r.pictures)) {
      const n = +r.pictures;
      line.textContent = `About ${n} picture${n === 1 ? "" : "s"}, ${money(r.price)}${r.price != null && +r.price > st.cap ? `. The run stops at $${Number.isInteger(st.cap) ? st.cap : st.cap.toFixed(2)}` : ""}.`;
    } else {
      const rough = fallback?.(st);
      line.textContent = rough || "The count is worked out when the index is written.";
    }
  }
  /** The model choices for the job's `art`: only what was chosen (absent means the server's default). */
  function models() {
    const out = {};
    for (const k of ["writer", "portraitWriter", "picker"]) { const c = textChoice(writers, st[k]); if (c) out[k] = c; }
    const pc = portraitImageChoice(provs || [], st.pProvider, st.pModel);
    if (pc) { out.portraitProvider = pc.provider; if (pc.model) out.portraitModel = pc.model; }
    return out;
  }
  /** The run's provider changed: the Default options name that provider's models now (nothing else is repainted). */
  function runProviderChanged() {
    const prefer = runProvider?.() || null;
    for (const [suffix, tier] of [["writer", "cheap"], ["pwriter", "strong"], ["picker", "cheap"]])
      setWriterDefault(el.querySelector(`#${idPrefix}-${suffix}`), writers, tier, prefer);
  }
  paint();
  return {
    el,
    runProviderChanged,
    config: () => (st.on && st.provider && slug() ? { style: slug(), level: st.level, portraits: st.cast === "none" ? false : st.cast, quality: st.quality,
      provider: st.provider.id, model: st.model || null, cap: st.cap, ...(listChapters && chapterParam() ? { chapters: chapterParam() } : {}), ...models() } : null),
    refresh,
    reason,
    ready: () => Boolean(st.provider && slug()),
  };
}
