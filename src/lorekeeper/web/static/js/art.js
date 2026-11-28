// Pictures: the gating helper, the manifest cache, the picture elements, the lightbox, the "Draw a picture" sheet and the
// Pictures group of the build form. An art set lives beside the index (never in it); the server sends its manifest whole,
// as it sends the index, and the Reader gates every picture by its `from` anchor exactly as it gates names and events.
// Captions, prompts and notes are untrusted text: they only ever go through h(). Picture files are fetched only from
// /api/art/... URLs built here from ids the server sent, never from a path or URL found in the manifest.
import { api } from "./api.js";
import { choiceSentence, composePrompt, promptParts, restyle, squash } from "./artprompt.js";
import { cleanPanels, panelCaptions } from "./artgate.js";
import { confirmDialog } from "./build.js";
import { clear, foldList, h, icon, loading, sheet, showModalLocked, closeLocked, toast } from "./dom.js";
import { galleryName, gallerySentence, loadStyles, savedStyle, styleRow } from "./styles.js";
import { cleanUserRefs, describeCost, describeModel, earlyProblem, leftOutLine, MAX_REFS, pathLine, REF_ACCEPT, REF_NOTE_CHARS,
  refPath, refsBody, roomFor, sameRefs, sniff, takesReferences, uploadError, withRefsPhrase,
  appliesLine, appliesOptions, fallbackDoneLine, resolveApplies } from "./artrefs.js";

export { loadStyles, stylePicker, styleRow, viewStyle } from "./styles.js";

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
const SHAPE_OF = { plate: "landscape", event: "landscape", comic: "portrait", portrait: "portrait", place: "landscape", thing: "landscape" };
export const ENTITY_KINDS = new Set(["portrait", "place", "thing"]);
/** The image providers' names, for "on Gemini" (the record holds the id). */
const PROVIDER_LABEL = { gemini: "Gemini", openai: "OpenAI" };
/** "Nano Banana Pro on Gemini": the model a picture was drawn with, by name when the server has listed it. */
const modelFact = (r) => [modelLabel(r.provider, r.model), r.provider ? `on ${PROVIDER_LABEL[r.provider] || r.provider}` : null].filter(Boolean).join(" ");

/** A style slug as words: the known ones by name, a custom one as the person wrote it (the manifest's `style`). */
export function styleLabel(slug, text = null) {
  if (STYLE_LABEL[slug]) return STYLE_LABEL[slug];
  const saved = galleryName(slug);   // a custom style saved on this server, by the name it was saved under
  if (saved) return saved;
  const t = typeof text === "string" && text.trim() ? text.trim() : String(slug || "").replace(/-/g, " ");
  return t ? t[0].toUpperCase() + t.slice(1) : "Custom";
}
/** The picture sheet's mark for a style given as loose words (an art set's own unsaved custom style, an older default
 *  typed as words): it can never be a slug, so a saved style whose slug is "custom" is still a saved style. */
const LOOSE = "*words";
/** The folder slug the server files a style's words under (art.style_slug), for telling one custom style from another. */
const slugOf = (text) => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "custom";
export { estimateText, money } from "./artestimate.js";
import { estimateText, money } from "./artestimate.js";
import { autoFixed, beforeSrc, cleanFaults, fixBody, fixLines, MAX_FAULTS, NOTE_CHARS, panelBody, panelTextsOf, panelWish, PANEL_TEXT_CHARS, severityOf, verdictLine, WHAT_CHARS } from "./artfix.js";

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
    portraitModel: typeof d.portraitModel === "string" && d.portraitModel ? d.portraitModel : null,
    // Check every new picture for drawing faults and fix it once when one is found (off unless chosen).
    check: d.check === true,
    // After a book's first build, draw its own style previews (on unless turned off).
    previews: d.previews !== false };
}
const textPick = (v) => (v && typeof v === "object" && typeof v.provider === "string" && v.provider && typeof v.model === "string" && v.model
  ? { provider: v.provider, model: v.model } : null);
export const setArtDefaults = (d) => write("lk-art-defaults", JSON.stringify({ ...artDefaults(), ...d }));

// ---- the gating helper: the one rule (artgate.js, dependency-free so Node can test it) --------------

export { lightboxHides, refParts, sheetWorld, unfinishedSources, visibleArt, visibleAt, visibleWorld } from "./artgate.js";
import { refParts } from "./artgate.js";

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
// The answer's own top-level `describe` (the model that describes reference pictures, or null), kept beside the rows;
// undefined when the server's answer does not carry one (an older server).
let describeTop;
function allProviders({ fresh = false } = {}) {
  if (fresh || !provCache || Date.now() - provCache.at > 60000) {
    const promise = api.providers().then((r) => {
      const all = Array.isArray(r?.providers) ? r.providers : [];
      describeTop = r && typeof r === "object" && "describe" in r ? r.describe : undefined;
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
/** Both lists from one request: { image, text }, plus `broken`, the providers with a key set whose check failed or has
 *  not passed yet (Settings says why they are not offered). */
export function modelProviders(opts = {}) {
  return allProviders(opts).then((all) => ({ image: all.filter((p) => canDraw(p)), text: all.filter((p) => p && p.ok && textModels(p).length),
    broken: all.filter((p) => p && p.configured && !p.ok),
    // the model that would describe reference pictures for an image model that takes none (null: none can)
    describer: describeModel(all, describeTop) }));
}
/** Text model names by "provider\nid", and provider names by id (the lightbox names the model that wrote a prompt). */
const TEXT_LABEL = new Map();
const PROVIDER_NAME = new Map();
/** A provider's text models: presets, then live ones the presets lack (the server lists only text models under
 *  `liveModels`, music and image ones under `otherModels`, never read here; an entry marked `text: false` is skipped
 *  as well); a custom server's configured model when it lists none. [{ id, label, price: [in, out] per million tokens | null, tier }] */
export function textModels(p) {
  if (!p) return [];
  const seen = new Set(), out = [];
  for (const m of [...(Array.isArray(p.models) ? p.models : []), ...(Array.isArray(p.liveModels) ? p.liveModels : [])]) {
    if (!m || typeof m.id !== "string" || !m.id || seen.has(m.id) || m.text === false) continue;
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

const LABEL_RE = /\b[Ff]igure ([A-F])\b/g;
/** The labels a prompt text uses ("figure A"), each once, in label order: the same rule the server draws by. */
export function labelsIn(text) {
  const seen = new Set();
  for (const m of String(text || "").matchAll(LABEL_RE)) seen.add(m[1]);
  return [...seen].sort();
}
/** The figures a server reply or record carries, checked: [{ label, entity, ref }] with ref the raw reference or null. */
export function cleanFigures(list) {
  if (!Array.isArray(list)) return [];
  return list.filter((f) => f && typeof f.label === "string" && /^[A-F]$/.test(f.label) && typeof f.entity === "string" && f.entity)
    .map((f) => ({ label: f.label, entity: f.entity, ref: f.ref ?? null }))
    .sort((a, b) => (a.label < b.label ? -1 : 1));
}
/**
 * One quiet line per figure: "Figure A is Greyhand" with a 24px round thumbnail of its reference. `name(entity)` gives the
 * name from the reader's own view (null: not visible there, so just "Figure A"), `thumb(figure)` the picture's URL or null
 * (the caller decides what may show). Only labels in `text` (when given) are listed. Null when there is nothing to list.
 */
const panelList = (caps, cls, label, action = null) => (caps.length ? h(`ol.${cls}`, { "aria-label": label },
  caps.map((c) => h("li", { value: String(c.n) }, h("span.panel-n.num", `${c.n}`), h("span", c.text), action ? action(c) : null))) : null);

/** A comic's panel boxes as the record stores them ([[x0, y0, x1, y1]] as fractions of the page), accepted only when there is
 *  one per panel and each is a real box inside the page; else null (no buttons over the picture). */
export function gridBoxes(rec) {
  const g = rec?.grid, n = Array.isArray(rec?.panels) ? rec.panels.length : 0;
  if (!Array.isArray(g) || !n || g.length !== n) return null;
  const ok = g.every((b) => Array.isArray(b) && b.length === 4 && b.every((v) => typeof v === "number" && v >= 0 && v <= 1) && b[2] > b[0] && b[3] > b[1]);
  return ok ? g : null;
}

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
 * picture replaces this one; without it Redraw only draws the saved prompt again. `position()` (optional) gives the reader's
 * frontier anchor, sent with a same-prompt redraw. `onChanged()` runs after a redraw, a delete or an undo.
 */
export function openLightbox(items, index = 0, { onChanged = null } = {}) {
  if (!items.length) return null;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let i = Math.min(Math.max(0, index), items.length - 1);
  const opened = new Map(items.map((x) => [x, x.rec.id]));   // each item's id when the lightbox opened
  const stage = h("div.lb-stage");
  const caption = h("p.lb-caption");
  const panelsBox = h("div.lb-panels-box");   // a comic's captions, one per panel, from the reader's view now
  const how = h("div.lb-how");
  const acts = h("div.lb-acts");
  const count = h("span.lb-count.subtle");
  const prev = h("button.icon-btn.lb-nav.lb-prev", { type: "button", "aria-label": "Previous picture", title: "Previous picture" }, icon("chevron-left"));
  const next = h("button.icon-btn.lb-nav.lb-next", { type: "button", "aria-label": "Next picture", title: "Next picture" }, icon("chevron"));
  const closeBtn = h("button.icon-btn.lb-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const title = h("h2#lb-title.sr-only");
  const toasts = h("div.toasts.lb-toasts", { "aria-live": "polite" });
  const dlg = h("dialog.lightbox", { "aria-labelledby": "lb-title" },
    h("div.lb-panel", h("header.lb-head", title, count, closeBtn), h("div.lb-main", prev, stage, next), h("div.lb-foot", caption, panelsBox, acts, how)), toasts);
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
  let stepLock = false;   // Keep the original's undo is in flight: no stepping, so its result lands on its own picture
  function lockSteps(on) {
    stepLock = on;
    prev.disabled = on || i === 0; next.disabled = on || i === items.length - 1;
  }
  function go(n) {
    if (stepLock || n < 0 || n >= items.length || n === i) return;
    settleFix();
    i = n; paint();
  }
  function paint() {
    const it = items[i], r = it.rec;
    title.textContent = it.caption || "Picture";
    paintStage();
    caption.textContent = it.caption || "";
    paintPanels();
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
    const fixed = fixLines(r);
    clear(how, h("details.lb-details", h("summary", icon("chevron"), "How this was drawn"),
      hiddenUntil ? h("div.lb-details-body", h("p.lb-hidden", `How this was drawn is hidden until you finish ${hiddenUntil}.`)) : h("div.lb-details-body",
        typeof r.prompt === "string" && r.prompt ? h("div.lb-prompt", h("span.label", "Prompt"), h("p", r.prompt)) : null,
        fixed.length ? h("ul.lb-fixes", { "aria-label": "Fixes" }, fixed.map((x) => h("li", h("span", x.line), x.created && when(x.created) ? h("span.subtle", ` ${when(x.created)}`) : null))) : null,
        h("div.lb-figs", lbFigures(it)),
        userRefsBox(it),
        // The style's own reference picture went with this one (the record holds only its sha, never the picture).
        typeof r.styleRef === "string" && r.styleRef ? h("p.lb-styleref", "Style from your reference picture.") : null,
        h("dl.lb-facts", facts.map(([k, v]) => [h("dt", k), h(k === "Model" ? "dd.lb-model" : k === "Prompt written by" ? "dd.lb-writer" : "dd", v)])))));
    paintActs();
  }
  /** The picture, and over a comic page with known panel boxes a "Fix panel N" button per panel (shown on hover or focus of
   *  the picture, and only with a hover pointer: on a touch screen the list below is the way there). */
  function paintStage() {
    const it = items[i];
    const img = artImg(it.src, it.caption, "lb-img", { eager: true });
    const boxes = it.rec.kind === "comic" && fixable(it) ? gridBoxes(it.rec) : null;
    if (!img || !boxes) { clear(stage, img); return; }
    clear(stage, h("div.lb-pagebox", img, boxes.map(([x0, y0, x1, y1], n) => h("button.lb-panel-hover", {
      type: "button", style: { right: `${((1 - x1) * 100).toFixed(2)}%`, top: `${(y0 * 100).toFixed(2)}%` },
      "aria-label": `Fix panel ${n + 1}`, title: `Fix panel ${n + 1}`, on: { click: () => panelForm(it, n + 1) } }, icon("sparkle"), `Fix panel ${n + 1}`))));
  }
  /** The actions under the picture: Fix, Redraw and Delete. Fix needs the image provider that drew this picture set up
   *  here (the list arrives after the lightbox opens: `drawers`), since the fix is drawn by that same model. */
  let drawers = null;   // the image provider rows that can draw now, once known
  // Not while "How this was drawn" is hidden: the faults are read against that prompt and could carry an unfinished book.
  const fixable = (it) => Boolean(it.canEdit && it.canRedraw !== false && !(it.promptHiddenUntil && it.promptHiddenUntil(it.rec)) && drawers && typeof it.rec.provider === "string" && drawers.some((p) => p.id === it.rec.provider));
  function paintActs() {
    const it = items[i];
    clear(acts, it.canEdit ? [
      fixable(it) ? h("button.btn.quiet.lb-fix-btn", { type: "button", on: { click: () => fix(it) } }, icon("sparkle"), "Fix") : null,
      it.canRedraw === false ? null : h("button.btn.quiet", { type: "button", on: { click: () => redraw(it) } }, icon("refresh"), "Redraw"),
      h("button.btn.quiet.danger", { type: "button", on: { click: () => remove(it) } }, icon("trash"), "Delete"),
    ] : null);
  }
  /** Where the reader is, for the check and the fix: { p, finished } (p only for a picture of the book being read). */
  const atNow = (it) => {
    const p = it.position ? it.position() : null;
    const fin = it.finished ? it.finished() : null;
    return { ...(typeof p === "string" && p ? { p } : {}), ...(Array.isArray(fin) ? { finished: fin } : {}) };
  };
  /** Fix: a model looks at the picture for drawing faults, the person reads, changes or adds to the list, and the model
   *  that drew it edits the picture to put them right. The picture takes the old one's place, with Undo. */
  async function fix(it) {
    const rec = it.rec;
    const ctl = new AbortController();
    dlg.addEventListener("close", () => ctl.abort(), { once: true });   // closed while looking: the check is not waited for
    let gone = false;
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => { gone = true; ctl.abort(); paintActs(); } } }, "Cancel");
    clear(acts, h("div.lb-fix", loading("Looking for drawing faults…"), h("div.btns", cancel)));
    cancel.focus();
    let found = [], said = "";
    try {
      // The server's checker (its strong model that can see pictures), not the Settings picker: the providers list is
      // only asked for so the model can be named.
      await textProviders().catch(() => null);
      const r = await api.artCheck(it.key, rec.id, atNow(it), null, ctl.signal);
      found = cleanFaults(r?.faults);
      const m = r?.model && typeof r.model.model === "string" && r.model.model ? textModelLabel(r.model.provider, r.model.model) : "";
      const who = m || "The check";
      said = found.length ? (found.length === 1 ? `${who} found one drawing fault. Change it, remove it or add your own.` : `${who} found ${found.length} drawing faults. Change or remove any, or add your own.`)
        : `${m ? `${m} found no` : "No"} drawing faults. If something is wrong, say what and it is fixed.`;
    } catch (e) {
      if (gone || e.name === "AbortError") return;
      said = e.code === "no-vision" ? "No model set up here can look at pictures. Say what is wrong and it is fixed."
        : `Couldn't look at the picture: ${e.message}. Say what is wrong and it is fixed.`;
    }
    if (gone || !dlg.open || items[i] !== it || it.rec !== rec) return;
    fixForm(it, found, said);
  }
  function fixForm(it, faults, said, { note: noteText = "", error = "" } = {}) {
    const rows = faults.length ? faults.map((f) => ({ ...f })) : [{ what: "", where: "" }];
    const list = h("ul.lb-faults", { "aria-label": "Drawing faults to fix" });
    const add = h("button.btn.quiet.lb-add", { type: "button" }, icon("plus"), "Add something to fix");
    const note = h("textarea.input.lb-note#lb-fix-note", { rows: "2", maxlength: String(NOTE_CHARS), placeholder: "For example: keep the candle lit" });
    note.value = noteText;
    const err = h("p.hint.field-error", { role: "alert" }, error);
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => paintActs() } }, "Cancel");
    const go_ = h("button.btn.primary", { type: "button" }, icon("sparkle"), "Fix it");
    const price = h("p.hint.lb-fix-price");
    const read = () => rows.map((r) => ({ what: r.input.value, where: r.where, severity: severityOf(r) }));
    const ready = () => { go_.disabled = !fixBody(read(), note.value); };
    function row(r, n) {
      r.input = h("textarea.input.lb-fault", { rows: "2", maxlength: String(WHAT_CHARS), "aria-label": `What to fix, ${n + 1}`,
        placeholder: "What is wrong, for example: the left hand has six fingers", on: { input: ready } });
      r.input.value = r.what;
      const del = h("button.icon-btn.lb-fault-del", { type: "button", "aria-label": `Remove ${n + 1}`, title: "Remove", on: { click: () => {
        const k = rows.indexOf(r);
        rows.splice(k, 1);
        paintRows();
        (rows[Math.min(k, rows.length - 1)]?.input || add).focus();
        ready();
      } } }, icon("x"));
      // "minor": the check's own grade, a quiet tag (noticed on a second look); a line the person adds is fixed in full.
      const minor = severityOf(r) === "minor" ? h("span.lb-minor", "minor") : null;
      const meta = r.where || minor ? h("span.subtle.lb-where", r.where ? h("span", r.where) : null, minor) : null;
      return h("li.lb-fault-row", h("div.lb-fault-text", r.input, meta), del);
    }
    function paintRows() {
      // the typed text survives a repaint: it is read back into the rows first
      for (const r of rows) if (r.input) r.what = r.input.value;
      clear(list, rows.map(row));
      add.hidden = rows.length >= MAX_FAULTS;
    }
    add.addEventListener("click", () => { const r = { what: "", where: "", severity: "major" }; rows.push(r); paintRows(); r.input.focus(); ready(); });
    note.addEventListener("input", ready);
    paintRows();
    const p = drawers?.find((x) => x.id === it.rec.provider);
    const one = p ? imagePrice(p, it.rec.quality || "standard", it.rec.shape || "landscape", it.rec.model || null) : undefined;
    price.textContent = `${one != null ? `${money(one)[0].toUpperCase()}${money(one).slice(1)}` : "Price unknown"} for one picture with ${modelLabel(it.rec.provider, it.rec.model) || "the model that drew it"}.`;
    go_.addEventListener("click", async () => {
      const body = fixBody(read(), note.value);
      if (!body) return;
      const keep = { faults: read().filter((f) => f.what.trim() || f.where), note: note.value };
      const old = { rec: it.rec, src: it.src };
      const at = atNow(it);
      clear(acts, h("div.lb-fix", loading("Fixing the picture…")));
      try {
        const r = await api.artFix(it.key, it.rec.id, { ...body, ...at });
        forgetArt(it.key);
        const rec = r?.record || r;
        if (rec && ID_RE.test(rec.id || "")) { it.rec = rec; it.src = it.srcFor ? it.srcFor(rec) : it.src; }
        onChanged?.();
        // The fix is filed even when the lightbox closed meanwhile: the toast with Undo then goes where it was opened
        // from (undoable picks the host), and the page fetches the art set again either way.
        if (!dlg.open || items[i] !== it) { undoable(it, r, "Picture fixed"); return; }
        paint();
        fixResult(it, r, { sent: body.faults, old, before: beforeSrc(r?.beforeUrl, at) });
      } catch (e) {
        const why = e.code === "unchanged" ? "The model sent the picture back without the fix, so your picture is kept. Try again, or say it differently."
          : e.code === "refused" ? `The provider would not draw it: ${e.message}` : e.message;
        if (!dlg.open) { toast(e.code === "unchanged" || e.code === "refused" ? why : `Couldn't fix it: ${e.message}`, "error", { host: modalToasts() }); return; }
        if (items[i] !== it) return;
        fixForm(it, keep.faults, said, { note: keep.note, error: why });
      }
    });
    clear(acts, h("div.lb-fix", h("p.hint", said), list, add,
      h("label.label", { for: "lb-fix-note" }, "Anything else, optional"), note, err, price, h("div.btns", cancel, go_)));
    ready();
    (error ? go_ : rows[0].input).focus();
  }
  /** Fix one panel of a comic page: its text as drawn (editable) and a note; the model edits the page and only that panel is
   *  taken from the result. The result view is the picture Fix's. */
  function panelForm(it, k, { value = null, note: noteText = "", error = "" } = {}) {
    const rec = it.rec;
    const shown = !(it.promptHiddenUntil && it.promptHiddenUntil(rec));
    const was = shown ? (panelTextsOf(rec) || [])[k - 1] || "" : "";
    const text = h("textarea.input.lb-note#lb-panel-text", { rows: "4", maxlength: String(PANEL_TEXT_CHARS), "aria-label": `What panel ${k} shows`, placeholder: "What this panel should show" });
    text.value = value ?? was;
    const note = h("textarea.input.lb-note#lb-panel-note", { rows: "2", maxlength: String(NOTE_CHARS), placeholder: "For example: keep the candle lit" });
    note.value = noteText;
    const err = h("p.hint.field-error", { role: "alert" }, error);
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => { paintActs(); } } }, "Cancel");
    const go_ = h("button.btn.primary", { type: "button" }, icon("sparkle"), "Fix it");
    const ready = () => { go_.disabled = !panelBody(k, text.value, was, note.value); };
    text.addEventListener("input", ready); note.addEventListener("input", ready);
    const p = drawers?.find((x) => x.id === rec.provider);
    const one = p ? imagePrice(p, rec.quality || "standard", rec.shape || "landscape", rec.model || null) : undefined;
    const price = h("p.hint.lb-fix-price", `${one != null ? `${money(one)[0].toUpperCase()}${money(one).slice(1)}` : "Price unknown"} for one picture with ${modelLabel(rec.provider, rec.model) || "the model that drew it"}. Only panel ${k} changes; the others stay as they are.`);
    go_.addEventListener("click", async () => {
      const body = panelBody(k, text.value, was, note.value);
      if (!body) return;
      const keep = { value: text.value, note: note.value };
      const old = { rec: it.rec, src: it.src };
      const at = atNow(it);
      clear(acts, h("div.lb-fix", loading(`Fixing panel ${k}…`)));
      try {
        const r = await api.artPanel(it.key, it.rec.id, { ...body, ...at });
        forgetArt(it.key);
        const nr = r?.record || r;
        if (nr && ID_RE.test(nr.id || "")) { it.rec = nr; it.src = it.srcFor ? it.srcFor(nr) : it.src; }
        onChanged?.();
        if (!dlg.open || items[i] !== it) { undoable(it, r, "Panel fixed"); return; }
        paint();
        fixResult(it, r, { sent: [panelWish(k, body)], old, before: beforeSrc(r?.beforeUrl, at) });
      } catch (e) {
        const why = e.code === "unchanged" ? "The model sent the page back without the change, so your picture is kept. Try again, or say it differently."
          : e.code === "refused" ? `The provider would not draw it: ${e.message}` : e.message;
        if (!dlg.open) { toast(e.code === "unchanged" || e.code === "refused" ? why : `Couldn't fix it: ${e.message}`, "error", { host: modalToasts() }); return; }
        if (items[i] !== it) return;
        panelForm(it, k, { ...keep, error: why });
      }
    });
    clear(acts, h("div.lb-fix", h("h3.lb-fix-title", `Panel ${k}`),
      h("label.label", { for: "lb-panel-text" }, `What panel ${k} shows`), text,
      h("label.label", { for: "lb-panel-note" }, "Anything else, optional"), note, err, price, h("div.btns", cancel, go_)));
    ready();
    (error ? go_ : text).focus();
  }
  /** "Your reference pictures": the person's own pictures this one was drawn with, each opening its file in a new tab,
   *  with its note and what it was described as. Remove changes only what a redraw sends, never the picture drawn. */
  function userRefsBox(it) {
    const list = cleanUserRefs(it.rec.userRefs);
    if (!list.length) return null;
    const removable = it.canEdit && it.canRedraw !== false;
    return h("div.lb-refs", h("span.label", "Your reference pictures"),
      h("ul.lb-refs-list", list.map((r, n) => h("li.lb-ref",
        r.url ? h("a.lb-ref-thumb", { href: r.url, target: "_blank", rel: "noopener noreferrer", "aria-label": `Open your reference picture ${n + 1}` },
          artImg(r.url, "", "lb-ref-img")) : h("span.lb-ref-thumb"),
        h("div.lb-ref-text",
          r.note ? h("p.lb-ref-note", r.note) : h("p.lb-ref-note.subtle", "No note."),
          // which figure (or the subject) it was for, named from the reader's view now; nothing for the whole scene
          h("p.lb-ref-for.subtle", { dataset: { sha: r.sha } }, appliesLine(r.appliesTo, it.rec, it.figureName || undefined)),
          r.description ? h("p.lb-ref-desc.subtle", `Described as: ${r.description}`) : null),
        removable ? h("button.icon-btn.lb-ref-del", { type: "button", "aria-label": `Remove reference picture ${n + 1}`, title: "Remove",
          on: { click: (e) => removeRef(it, r.sha, e.currentTarget) } }, icon("x")) : null))),
      removable ? h("p.hint.lb-refs-hint", "Removing one changes only what a redraw uses.") : null);
  }
  async function removeRef(it, sha, btn) {
    const rec = it.rec;
    const was = cleanUserRefs(rec.userRefs);
    const keep = was.filter((r) => r.sha !== sha);
    if (btn) btn.disabled = true;
    try {
      const r = await api.artRefsPut(it.key, rec.id, refsBody(keep));
      forgetArt(it.key);
      const got = r?.record || r;
      // The reply's record, else the list as sent (the picture and its id stay the same).
      it.rec = got && got.id === rec.id ? got : { ...rec, userRefs: keep };
      if (dlg.open && items[i] === it) repaintRefs(it);
      const id = r?.undo ?? r?.journal ?? null;
      toast("Reference picture removed", "ok", { host: dlg.open ? toasts : modalToasts(), action: id ? { label: "Undo", run: async () => {
        try {
          await api.undo(it.key, id);
          forgetArt(it.key);
          it.rec = { ...it.rec, userRefs: rec.userRefs };
          if (dlg.open && items[i] === it) repaintRefs(it);
          toast("Undone", "ok", { host: dlg.open ? toasts : modalToasts() });
          onChanged?.();
        } catch (e) { toast(e.message, "error", { host: dlg.open ? toasts : modalToasts() }); }
      } } : null });
      onChanged?.();
    } catch (e) {
      if (btn?.isConnected) btn.disabled = false;
      toast(`Couldn't remove it: ${e.message}`, "error", { host: dlg.open ? toasts : modalToasts() });
    }
  }
  /** The reference list again in place ("How this was drawn" stays open), focus to the next Remove or the summary. */
  function repaintRefs(it) {
    const old = how.querySelector(".lb-refs");
    const box = userRefsBox(it);
    const body = how.querySelector(".lb-details-body");
    if (old && box) old.replaceWith(box);
    else if (old) old.remove();
    else if (box && body) body.querySelector(".lb-figs")?.after(box);
    (how.querySelector(".lb-ref-del") || how.querySelector("summary"))?.focus({ preventScroll: true });
  }
  /** After a fix: what it put right in one sentence, Before / After over the picture, Keep the fix and Keep the original
   *  (the journal's Undo, then the old picture on screen again). Left undecided (closed, or another picture stepped to),
   *  the fix stands and the usual toast with Undo is offered. */
  let pending = null;   // { it, r } while the result is on screen and not decided
  function settleFix() {
    if (!pending) return;
    const { it, r } = pending;
    pending = null;
    undoable(it, r, "Picture fixed");
  }
  dlg.addEventListener("close", settleFix);
  // The button pressed is gone with the panel: focus stays in the dialog (so the arrow keys still step), on its first action.
  const refocus = () => (acts.querySelector("button") || closeBtn).focus({ preventScroll: true });
  function fixResult(it, r, { sent, old, before }) {
    const id = r?.undo ?? r?.journal ?? null;
    pending = { it, r };
    const after = it.src;
    const err = h("p.hint.field-error", { role: "alert" });
    const bBefore = h("button", { type: "button", "aria-pressed": "false", on: { click: () => show("before") } }, "Before");
    const bAfter = h("button", { type: "button", "aria-pressed": "true", on: { click: () => show("after") } }, "After");
    const seg = before ? h("div.seg.lb-ba", { role: "group", "aria-label": "Show the picture" }, bBefore, bAfter) : null;
    function show(which) {
      for (const b of [bBefore, bAfter]) b.setAttribute("aria-pressed", String(b === (which === "before" ? bBefore : bAfter)));
      const img = artImg(which === "before" ? before : after, `${it.caption || "Picture"}${which === "before" ? ", before the fix" : ""}`, "lb-img", { eager: true });
      // a "before" that cannot be fetched (undone elsewhere, or the server has none): the toggle goes, the fix stays shown
      if (which === "before") img?.addEventListener("error", () => { seg?.remove(); show("after"); }, { once: true });
      clear(stage, img);
    }
    const keepFix = h("button.btn.primary", { type: "button", on: { click: () => { pending = null; paintStage(); paintActs(); refocus(); } } }, "Keep the fix");
    const keepOld = id ? h("button.btn.quiet", { type: "button" }, "Keep the original") : null;
    keepOld?.addEventListener("click", async () => {
      keepOld.disabled = keepFix.disabled = true;
      err.textContent = "";
      lockSteps(true);
      // Decided the moment it is pressed: closing or stepping away while the undo runs must not settle the fix (and
      // toast "Picture fixed" over an original that is coming back).
      pending = null;
      try {
        await api.undo(it.key, id);
        lockSteps(false);
        forgetArt(it.key);
        it.rec = old.rec; it.src = old.src;
        onChanged?.();
        if (!dlg.open || items[i] !== it) { toast("The original is back.", "ok", { host: dlg.open ? toasts : modalToasts() }); return; }
        paint();
        refocus();
        toast("The original is back.", "ok", { host: toasts });
      } catch (e) {
        lockSteps(false);
        // The fix stands: back to undecided where the result is still on screen, else the usual toast with Undo.
        if (dlg.open && items[i] === it && keepOld.isConnected) {
          pending = { it, r };
          err.textContent = `Couldn't put the original back: ${e.message}`;
          keepOld.disabled = keepFix.disabled = false;
        } else {
          toast(`Couldn't put the original back: ${e.message}`, "error", { host: dlg.open ? toasts : modalToasts() });
          undoable(it, r, "Picture fixed");
        }
      }
    });
    clear(acts, h("div.lb-fix.lb-fixed", { role: "group", "aria-label": "The fix" },
      h("p.lb-verdict", { role: "status" }, verdictLine(r?.verified, sent)), seg, err, h("div.btns", keepOld, keepFix)));
    keepFix.focus();
  }
  const lbFigures = (it) => figureLines(it.rec.figures, { name: it.figureName || undefined, thumb: (f) => (it.figureThumb ? it.figureThumb(it.rec, f) : null) });
  /** Names and thumbnails again from the reader's view now (a position that moved back hides what it no longer reveals). */
  function refresh() {
    const box = how.querySelector(".lb-figs");
    if (box) clear(box, lbFigures(items[i]));
    // "For figure A, Mina": the name comes and goes with the view, as the figure lines do
    const refsNow = new Map(cleanUserRefs(items[i]?.rec?.userRefs).map((r) => [r.sha, r]));
    for (const p of how.querySelectorAll(".lb-ref-for")) p.textContent = appliesLine(refsNow.get(p.dataset.sha)?.appliesTo, items[i].rec, items[i].figureName || undefined);
    paintPanels();
    // the hover buttons follow `fixable` like the list; not while a result (Before / After) is on the stage
    // A form open (not a result) when the picture stops being fixable: the form goes with its buttons.
    if (pending) return;
    if (!acts.querySelector(".lb-fix")) paintStage();
    else if (!fixable(items[i])) { paintActs(); paintStage(); }
  }
  /** A comic's captions under the picture: never drawn into it, so they come from the view at the position now. */
  function paintPanels() {
    const it = items[i];
    const act = it && it.rec.kind === "comic" && fixable(it)
      ? (c) => h("button.btn.quiet.lb-panel-fix", { type: "button", "aria-label": `Fix panel ${c.n}`, on: { click: () => panelForm(it, c.n) } }, icon("sparkle"), "Fix panel")
      : null;
    clear(panelsBox, it?.rec?.kind === "comic" ? panelList(panelCaptions(it.rec.panels, it.panelText), "lb-panels", "Panels", act) : null);
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
        // the reader's position: a figure's reference from after the scene is sent again only when it is reached
        const r = await api.artRedraw(it.key, it.rec.id, note.value.trim(), it.position ? it.position() : null);
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
    // the record's own reference pictures go again as stored (the route reuses them)
    const withRefs = withRefsPhrase(cleanUserRefs(it.rec.userRefs).length);
    clear(acts, h("div.lb-redraw", h("label.label", "Redraw with a note, optional"), note,
      withRefs ? h("p.hint.lb-redraw-refs", `Drawn again ${withRefs}.`) : null, err, h("div.btns", cancel, go_)));
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
  imageProviders().then((list) => {
    drawers = list;
    // Fix appears once the providers are known, unless a flow is already on screen there.
    if (dlg.isConnected && !acts.querySelector(".lb-redraw, .lb-fix")) { paintActs(); paintStage(); paintPanels(); }
    const r = dlg.isConnected ? items[i]?.rec : null;
    if (!r) return;
    const dd = how.querySelector(".lb-model"), wd = how.querySelector(".lb-writer");
    if (dd) dd.textContent = modelFact(r);
    if (wd) wd.textContent = writerFact(r.writer) || wd.textContent;
  });
  /** Per picture the lightbox can step to, the ids that stand for it: the one on screen now and the one it opened with
   *  (a fix or a redraw files the picture under a new id, which the page's art set learns only after it is fetched
   *  again; both share the same anchor). The Reader closes the lightbox only when neither is visible. */
  const ids = () => items.map((x) => [x.rec.id, opened.get(x)]);
  return { close, refresh, ids };
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

// ---- the person's own reference pictures (artrefs.js has the rules) ------------------------------------

/** A chosen file's kind, size and, when those pass, its size in pixels (null when it could not be decoded). */
async function inspectRef(file) {
  let kind = null;
  try { kind = sniff(new Uint8Array(await file.slice(0, 16).arrayBuffer())); } catch {}
  if (earlyProblem({ kind, size: file.size })) return { kind, size: file.size };
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
 * The picture sheet's "Reference pictures" row: up to MAX_REFS of the person's own pictures, each uploaded as soon as it
 * is chosen (`POST .../art/refs`) and shown as a thumbnail with a one-line note ("What to take from it: this dress") and Remove.
 * `initial` is a record's `userRefs` (a redraw starts from them). The element is made once and kept across the sheet's
 * repaints, so a change of style, shape, quality, model or moment keeps it as it is. Returns
 * { el, refs() [{sha, note?, appliesTo?}], count(), busy(), dirty(), drop(sentence), setPath(text), setFigures(rows),
 * dispose() }.
 * Each picture has an "Applies to" choice: the whole scene, one labelled figure the prompt uses now (`setFigures`, rows
 * [{ label, name }] from the figure lines, or null while no prompt is written yet), or for a portrait, place or thing
 * (`kind`) its subject (`subject()`, the name from the reader's view, null when not visible). A chosen figure that leaves
 * the text falls back to the whole scene with one quiet line, and comes back if the label does.
 */
/** The note names the thing to take, never who wears it: "Applies to" says who (a note naming the other sex changes the
 *  figure). Shorter on a phone, where the field is about 170px wide at 16px and the longer one would be cut off. */
const notePlaceholder = () => (globalThis.matchMedia?.("(max-width: 640px)").matches ? "Take from it: this dress" : "What to take from it: this dress");

function refsRow({ key, kind = null, subject = () => null, initial = [], onChange = null }) {
  let seq = 0;
  // { id, sha, ext, note, description, url, local (object URL while uploading), uploading, ctrl, done (Promise),
  //   wanted (what the person chose), appliesTo (what is sent: `wanted`, or "scene" after a fallback), sel, fall }
  const items = cleanUserRefs(initial).map((r) => ({ ...r, wanted: r.appliesTo, id: ++seq, uploading: false }));
  const forSubject = ENTITY_KINDS.has(kind);
  let figureRows = null;   // the labelled figures in the prompt now; null: no prompt yet (a chosen figure waits for it)
  const choices = () => (forSubject ? appliesOptions({ kind, subject: subject() }) : figureRows ? appliesOptions({ kind, figures: figureRows }) : null);
  let start = items.map((r) => ({ ...r }));
  const fileIn = h("input.sr-only.art-refs-file", { type: "file", accept: REF_ACCEPT, multiple: true, tabindex: "-1", "aria-hidden": "true" });
  const addBtn = h("button.btn.art-refs-add-btn", { type: "button", "aria-describedby": "art-refs-err", on: { click: () => fileIn.click() } },
    icon("upload"), "Add reference pictures");
  const drop = h("div.art-refs-drop", addBtn,
    h("span.art-refs-drop-hint.subtle", `or drop them here. PNG, JPEG or WebP, at most 8 MB, up to ${MAX_REFS}.`));
  const list = h("ul.art-refs-list", { "aria-label": "Your reference pictures" });
  const err = h("p.hint.field-error#art-refs-err", { role: "alert" });
  const path = h("p.hint.art-refs-path", { role: "status" });
  const el = h("div.art-field.art-refs", h("span.label#art-refs-label", "Reference pictures"),
    h("p.hint.art-refs-what", "Your own pictures to help with this one: a dress, a house, a face. Say what to take from each."),
    list, fileIn, drop, err, path);
  const changed = () => onChange?.();

  function row(it, n) {
    const thumb = h("span.art-ref-thumb", it.uploading ? h("span.spinner", { role: "img", "aria-label": "Uploading" }) : null);
    const src = it.local || it.url;
    if (src) {
      const img = h("img", { src, alt: `Your reference picture ${n + 1}`, decoding: "async", draggable: "false" });
      img.addEventListener("error", () => img.remove());
      thumb.prepend(img);
    }
    const note = h("input.input.art-ref-note", { type: "text", maxlength: String(REF_NOTE_CHARS), placeholder: notePlaceholder(),
      "aria-label": `What to take from picture ${n + 1}`, dataset: { ref: String(it.id) },
      on: { input: (e) => { it.note = e.target.value; changed(); } } });
    note.value = it.note || "";
    const del = h("button.icon-btn.art-ref-del", { type: "button", "aria-label": `Remove picture ${n + 1}`, title: "Remove", dataset: { del: String(it.id) },
      on: { click: () => remove(it) } }, icon("x"));
    const selId = `art-ref-applies-${it.id}`;
    it.sel = h("select.select.art-ref-applies", { id: selId, "aria-label": `Applies to, reference picture ${n + 1}`, dataset: { sel: String(it.id) },
      on: { change: (e) => { it.wanted = e.target.value; paintApplies(); changed(); } } });
    it.fall = h("p.hint.art-ref-fallback");
    it.wrap = h("div.art-ref-applies-row", h("label.art-ref-applies-label.subtle", { for: selId }, "Applies to"), it.sel);
    it.sel.__opts = "";
    return h(`li.art-ref${it.uploading ? ".uploading" : ""}`, thumb, h("div.art-ref-fields", note, it.wrap, it.fall), del);
  }
  /** Each picture's choice against the figures now: the select's options rebuilt only when they change (focus and an open
   *  select survive typing in the prompt), the quiet line when a chosen figure left the text. True when a sent value moved. */
  function paintApplies() {
    const opts = choices();
    let moved = false;
    for (const it of items) {
      const r = resolveApplies(it.wanted, opts);
      if (r.value !== it.appliesTo) moved = true;
      it.appliesTo = r.value;
      if (!it.sel) continue;
      const sig = JSON.stringify(r.options);
      if (it.sel.__opts !== sig) {
        it.sel.__opts = sig;
        clear(it.sel, r.options.map((o) => h("option", { value: o.value }, o.label)));
      }
      it.sel.value = r.value;
      it.fall.textContent = r.fallback;
      // one choice only (a scene with no labelled figure): nothing to choose, so no select
      it.wrap.hidden = r.options.length < 2;
    }
    return moved;
  }
  function paint() {
    // Typing and focus survive: the notes are read from the items, and focus goes back to the same item's field.
    const act = document.activeElement;
    const focusRef = list.contains(act) ? act.dataset.ref || (act.dataset.del ? `del:${act.dataset.del}` : act.dataset.sel ? `sel:${act.dataset.sel}` : null) : null;
    clear(list, items.map(row));
    paintApplies();
    list.hidden = !items.length;
    drop.hidden = items.length >= MAX_REFS;
    if (focusRef) {
      const t = focusRef.startsWith("del:") ? list.querySelector(`[data-del="${focusRef.slice(4)}"]`)
        : focusRef.startsWith("sel:") ? list.querySelector(`[data-sel="${focusRef.slice(4)}"]`) : list.querySelector(`[data-ref="${focusRef}"]`);
      t?.focus({ preventScroll: true });
    }
  }
  function remove(it) {
    const k = items.indexOf(it);
    if (k < 0) return;
    it.ctrl?.abort();
    if (it.local) URL.revokeObjectURL(it.local);
    items.splice(k, 1);
    err.textContent = "";
    paint();
    (list.querySelectorAll(".art-ref-note")[Math.min(k, items.length - 1)] || addBtn).focus({ preventScroll: true });
    changed();
  }
  async function take(files) {
    err.textContent = "";
    const chosen = [...(files || [])];
    if (!chosen.length) return;
    const room = roomFor(items, chosen.length);
    if (room < chosen.length) err.textContent = `Up to ${MAX_REFS} pictures: ${chosen.length - room === 1 ? "one was" : `${chosen.length - room} were`} left out.`;
    for (const file of chosen.slice(0, room)) {
      const it = { id: ++seq, sha: null, ext: null, note: "", description: null, url: null, wanted: "scene", appliesTo: "scene",
        local: URL.createObjectURL(file), uploading: true, ctrl: new AbortController() };
      items.push(it);
      upload(it, file);
    }
    paint();
    changed();
  }
  async function upload(it, file) {
    const gone = () => !items.includes(it);
    try {
      const problem = earlyProblem(await inspectRef(file));
      if (gone()) return;
      if (problem) throw Object.assign(new Error(problem), { code: "bad-image" });
      const r = await api.artRefUpload(key, file, it.ctrl.signal);
      if (gone()) return;
      const got = cleanUserRefs([r])[0];
      if (!got) throw new Error("the server did not say where it went");
      // the same picture twice: one item, the first one's note kept
      if (items.some((x) => x !== it && x.sha === got.sha)) { forget(it); err.textContent = "That picture is already here."; return; }
      Object.assign(it, { sha: got.sha, ext: got.ext, url: got.url, uploading: false });
      if (it.local) { URL.revokeObjectURL(it.local); it.local = null; }
      if (!it.url) it.local = URL.createObjectURL(file);   // an older reply without the address: the chosen file shows
    } catch (e) {
      if (gone() || e?.name === "AbortError") return;
      forget(it);
      err.textContent = uploadError(e);
      return;
    }
    paint();
    changed();
  }
  function forget(it) {
    const k = items.indexOf(it);
    if (k >= 0) items.splice(k, 1);
    if (it.local) URL.revokeObjectURL(it.local);
    paint();
    changed();
  }
  fileIn.addEventListener("change", () => { const f = [...fileIn.files]; fileIn.value = ""; take(f); });
  // Dropping files on the row (wide screens; the drop area is hidden on a phone, where the button is the way in).
  for (const t of ["dragenter", "dragover"]) el.addEventListener(t, (e) => { if (e.dataTransfer?.types?.includes("Files") && items.length < MAX_REFS) { e.preventDefault(); el.classList.add("over"); } });
  el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("over"); });
  el.addEventListener("drop", (e) => { if (!e.dataTransfer?.files?.length) return; e.preventDefault(); el.classList.remove("over"); take(e.dataTransfer.files); });
  paint();
  return {
    el,
    refs: () => refsBody(items),
    count: () => items.filter((x) => !x.uploading).length,
    busy: () => items.some((x) => x.uploading),
    dirty: () => items.some((x) => x.uploading) || !sameRefs(items, start),
    /** Take every picture out (the server could not use them) and say why under the row. */
    drop: (sentence) => { for (const it of [...items]) { it.ctrl?.abort(); if (it.local) URL.revokeObjectURL(it.local); } items.length = 0; paint(); err.textContent = sentence || ""; changed(); },
    setPath: (text) => { path.textContent = text || ""; },
    /** The labelled figures the prompt uses now ([{ label, name }], names from the reader's view), or null: no prompt yet. */
    setFigures: (rows) => { figureRows = Array.isArray(rows) ? rows : null; if (paintApplies()) changed(); },
    /** Drawn with these: nothing left to ask about on close. */
    settle: () => { start = items.map((r) => ({ ...r })); },
    dispose: () => { for (const it of items) { it.ctrl?.abort(); if (it.local) URL.revokeObjectURL(it.local); } },
  };
}

// ---- the picture sheet: on-demand drawing --------------------------------------------------------

/**
 * "Draw a picture" for one item the reader can see. `opts`:
 *   key          the index's library key
 *   kind         plate | event | comic | portrait | place | thing
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
 *   panelText(event)   a comic panel's caption: the event's summary from the reader's view (null: not visible there)
 *   figureName(entity)   a figure's name from the reader's view (null: not visible there, so only \"Figure A\")
 *   replaces     a picture id of the art set: Redraw with a new prompt. The sheet is titled "Redraw", Draw sends it and the
 *                new picture takes that one's place (Undo puts it back; `onUndone()` runs after)
 *   replacesStyle  the slug of that picture's set: drawn in another style, the picture is a new one in that style's set and
 *                the old one stays (no `replaces` is sent)
 *   refs         with `replaces`: the old picture's `userRefs`, the person's own reference pictures, which start in the
 *                "Reference pictures" row (sent with Draw as the row holds them then)
 *   through      a person or creature portrait: the sheet also offers "Through the book so far", the portraits a run would
 *                plan for this one entry (first look, then each lasting look change) up to the position, already drawn ones
 *                skipped, counted by the estimate and started as a pictures run on the queue (never with `replaces`)
 * Returns the sheet, with `current()` → { kind, target } (the moment selected now) and `listed()` → every item named on it.
 */
export async function openPictureSheet(opts) {
  const [{ image: provs, text: writers, describer }] = await Promise.all([modelProviders({ fresh: true }), loadStyles()]);
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
    busy: false, closed: false, figures: [], panels: [],   // panels: a comic's event ids, in panel order, as the server chose them   // figures: the labelled figures of the prompt on screen
    mode: "now",   // "now": one portrait at the position; "through": the entry's portraits through the book so far, as a run
    est: null,     // through: { ask, r, error } the estimate for the choices and position in `ask` (a JSON key)
    writer: null,   // the text model that writes the prompt ({ provider, model }); null: the server's default, the strong tier
  };
  // A style saved on this server (built-in or custom) is chosen by its slug; anything else is an art set's own custom
  // style (or an older default typed as words), drawn from its words as before.
  if (!STYLE_LABEL[st.style] && !savedStyle(st.style)) {
    st.custom = (opts.styles || []).find((x) => x.slug === st.style)?.text || st.style.replace(/-/g, " ");
    st.slug = STYLE_LABEL[st.style] ? null : opts.style;   // the art set's own slug for its custom style
    st.style = LOOSE;
  }
  // The Settings model when it is the Settings provider's, else the provider's default (null: an older server picks).
  st.model = modelFor(st.provider, st.provider.id === d.provider ? d.model : null);
  // A portrait, place or thing starts from the Settings portrait image model, when one is chosen and can draw.
  const forPortraits = ENTITY_KINDS.has(st.kind) ? portraitImageChoice(provs, d.portraitProvider, d.portraitModel) : null;
  if (forPortraits) { st.provider = provs.find((p) => p.id === forPortraits.provider); st.model = forPortraits.model; }
  st.quality = fitQuality(st.provider, st.model, st.quality);
  // The style as sent to the server (a known slug, or the person's words), and the folder slug pictures are filed under:
  // a known style is its own slug, a custom one only what the server answered (never a folder made from raw text here).
  const styleText = () => (st.style === LOOSE ? st.custom.trim() : st.style);
  const folder = () => (st.style === LOOSE ? st.slug : st.style);
  /** The style sentence of the choice now: from the reply's table, else a saved style's own words (a custom style saved
   *  on this server that the prompt route did not list). */
  const sentenceNow = (parts = st.parts) => choiceSentence(parts, st.style === LOOSE ? null : st.style, st.custom, gallerySentence);
  /** The styles an art set has that are not saved here (its own custom style): still choosable, by their words. */
  const setStyles = () => {
    const out = (opts.styles || []).filter((x) => x && typeof x.slug === "string" && !STYLE_LABEL[x.slug] && !savedStyle(x.slug) && x.text)
      .map((x) => ({ slug: x.slug, name: styleLabel(x.slug, x.text), text: x.text }));
    if (st.style === LOOSE && st.custom.trim()) {
      const slug = st.slug || slugOf(st.custom);
      if (!out.some((x) => x.slug === slug)) out.push({ slug, name: styleLabel(slug, st.custom), text: st.custom });
    }
    return out;
  };
  // Where the reader is now (asked at every call, never the position the sheet was opened at), and their finished books.
  const here = () => { const at = opts.position ? opts.position() : { p: opts.p, finished: opts.finished }; return { p: at.p ?? null, finished: at.finished || [] }; };
  let ctrl = null, gen = 0;   // the request in flight and its generation: a late reply for a moment left behind is dropped
  const abort = () => { gen++; ctrl?.abort(); ctrl = null; st.busy = false; };

  const body = h("div.art-sheet-body");
  const drawBtn = h("button.btn.primary", { type: "button", disabled: true }, icon("sparkle"), "Draw");
  const through = Boolean(opts.through) && opts.kind === "portrait" && !(typeof opts.replaces === "string" && opts.replaces);
  const startBtn = h("button.btn.primary", { type: "button", disabled: true, hidden: true }, icon("sparkle"), "Start");
  const footNote = h("span.art-price.num", { role: "status" });
  const replaces = typeof opts.replaces === "string" && ID_RE.test(opts.replaces) ? opts.replaces : null;
  const sh = sheet({ title: `${replaces ? "Redraw" : opts.kind === "comic" ? "Make a comic" : "Draw a picture"}${opts.title ? `: ${opts.title}` : ""}`, className: "art-sheet", content: body,
    footer: h("div.art-foot", footNote, drawBtn, startBtn),
    // Always closable: a prompt in flight is abandoned; a drawing already sent is not (the server files it either way), and
    // when it lands its toast, with Undo for a redraw, shows where the sheet was opened from. An edited prompt is asked about first.
    beforeClose: () => {
      if (st.drawing || st.starting) return true;
      const edited = Boolean(st.prompt) && st.prompt !== st.written;
      const pictures = Boolean(refs?.dirty());   // reference pictures added, removed or noted here count as changes
      if (st.busy && !pictures) { abort(); return true; }
      if (!edited && !pictures) return true;
      confirmDialog({ title: "Discard your changes?", message: edited && pictures ? "The prompt you changed and your reference pictures are not kept."
        : edited ? "The prompt you changed is not kept." : "Your reference pictures are not kept.", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { if (st.busy) abort(); st.prompt = st.written; refs?.dispose(); refs = null; sh.close(true); } });
      return false;
    },
    onClose: () => {
      st.closed = true;
      refs?.dispose();   // the uploads in flight stop; what was uploaded is pruned by the server after a day
      if (!st.drawing) abort();
      if (undoRedraw) toast("Picture redrawn", "ok", { host: modalToasts(), action: { label: "Undo", run: () => undoRedraw?.() } });
    } });
  let undoRedraw = null;   // after a redraw with a new prompt: puts the old picture back
  // The person's own reference pictures (not for "Through the book so far", a run). A redraw starts from the old picture's.
  let refs = refsRow({ key: opts.key, kind: opts.kind, subject: () => (opts.figureName && typeof opts.target === "string" ? opts.figureName(opts.target) : null),
    initial: replaces ? opts.refs || [] : [], onChange: () => { paintPath(); paintFoot(); } });
  /** Which way the pictures go with the image model chosen now, as one quiet line under the row. */
  function refRoute() {
    const takes = takesReferences(st.provider, st.model);
    const d = takes ? null : describer;
    return { path: refPath({ takes, describer: d }), describer: d };
  }
  function paintPath() {
    if (!refs) return;
    const { path, describer: d } = refRoute();
    const n = Math.max(1, refs.count());
    refs.setPath(pathLine({ path, image: modelLabel(st.provider.id, st.model) || st.provider.label, provider: st.provider.label,
      describer: d ? d.model.label || d.model.id : null, describerProvider: d ? d.provider.label : null, price: d ? describeCost(d.model, n) : null }));
  }
  sh.current = () => ({ kind: st.kind, target: st.target });
  sh.refreshFigures = () => { paintFigures(); paintPanels(); };
  sh.listed = () => [sh.current(), ...(opts.moments || []).map((m) => ({ kind: m.kind, target: m.target }))];
  /** The anchors the text on screen was written up to: the prompt's (the picture's own, any world-note hold included) and
   *  the world note's own anchor. The Reader checks them against the position as it checks the moments. */
  sh.anchors = () => [st.anchor, opts.world?.from].filter((a) => typeof a === "string");
  /** The position moved back before that text: drop the prompt and the World line and write a new prompt here. */
  sh.reposition = ({ world = null, canEditWorld = false } = {}) => {
    opts.world = world || null;
    opts.canEditWorld = canEditWorld;   // decided again for the position now: a note hidden here is never written over
    st.prompt = st.written = "";
    st.anchor = null; st.promptFor = null; st.asked = null; st.price = null; st.parts = null; st.sentence = null; st.missing = false; st.figures = []; st.panels = [];
    if (st.mode === "through") { paintFoot(); return; }   // counted again up to the position now; the prompt waits for "now"
    write();
  };
  /** The position moved (either way): "Through the book so far" counts again up to where the reader is now. */
  sh.moved = () => { if (st.mode === "through" && !st.closed) paintFoot(); };

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
    st.anchor = null; st.promptFor = null; st.asked = null; st.price = null; st.parts = null; st.sentence = null; st.missing = false; st.figures = []; st.panels = [];
    textarea.value = "";
    if (!retried) { write({ retried: true }); return; }
    clear(body, modePick(), h("div.art-error", { role: "alert" }, h("p", "Your position moved while the prompt was being written.")),
      h("button.btn", { type: "button", on: { click: () => write() } }, "Write it again"));
    paintFoot();
  }
  async function write(how) {
    const retried = how?.retried === true;
    abort();
    const mine = ++gen;
    ctrl = new AbortController();
    st.busy = true;
    clear(body, modePick(), loading("Writing a prompt from what you have read…"));
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
        st.sentence = sentenceNow() ?? squash(st.parts.style);
        st.prompt = st.written = composePrompt(st.parts.body, st.sentence, st.parts.noText);
      } else {
        st.sentence = null;   // an older server: the style is inside its text, so a new style writes the prompt again
        st.prompt = st.written = typeof r.prompt === "string" ? r.prompt : "";
      }
      st.figures = cleanFigures(r.figures);
      st.panels = cleanPanels(r.panels);
      st.price = typeof r.price === "number" ? r.price : null;
      st.anchor = typeof r.anchor === "string" ? r.anchor : null;
      st.asked = asked;
      st.promptStyle = asked.style;
      st.promptFor = { kind: st.kind, target: st.target };
      clear(err);
      if (st.style === LOOSE && asked.style === styleText()) st.slug = typeof r.slug === "string" && /^[a-z0-9-]{1,64}$/.test(r.slug) ? r.slug : null;
      st.busy = false; ctrl = null;
      paint();
    } catch (e) {
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      const why = promptError(e);
      if (st.promptFor && e.code !== "not-visible" && e.code !== "too-few-moments") {
        // A prompt was already on screen: keep it, with the style and moment it was written for, and say why the new one failed.
        Object.assign(st, st.promptFor);
        paint();
        clear(err, h("p", why));
        return;
      }
      clear(body, modePick(), h("div.art-error", { role: "alert" }, h("p", why)),
        e.code === "not-visible" || e.code === "too-few-moments" ? null : h("button.btn", { type: "button", on: { click: write } }, "Try again"));
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
        st.sentence = sentenceNow(parts) ?? squash(parts.style);
        st.prompt = st.written = composePrompt(parts.body, st.sentence, parts.noText);
      } else if (typeof r.prompt === "string" && r.prompt) {
        st.parts = null; st.sentence = null;
        st.prompt = st.written = r.prompt;
      } else throw new Error("No prompt came back");
      st.missing = false;
      st.figures = cleanFigures(r.figures);
      if (Array.isArray(r.panels)) st.panels = cleanPanels(r.panels);
      if (typeof r.price === "number") st.price = r.price;
      if (typeof r.anchor === "string") st.anchor = r.anchor;
      st.asked = asked; st.promptStyle = asked.style;
      st.busy = false; ctrl = null;
      textarea.value = st.prompt;
      paintStyleNote();
      paintFigures();
      paintPanels();
      clear(err, h("p.art-ok", "Rewritten so it can be drawn. Read it, change anything, then Draw."));
      paintFoot();
      textarea.focus();
    } catch (e2) {
      if (mine !== gen || st.closed) return;
      st.busy = false; ctrl = null;
      clear(err, h("p", promptError(e2).replace(/^Couldn't write a prompt/, "Couldn't rewrite it")),
        e2.code === "not-visible" || e2.code === "too-few-moments" ? null : h("button.btn", { type: "button", on: { click: () => soften(text, reason) } }, "Try again"));
      paintFoot();
    }
  }
  /** A new style changes only the prompt's style sentence, here and with no call: an unedited prompt is built again from
   *  its parts, an edited one has the old sentence replaced where it still stands (else `st.missing` says so). An older
   *  server sends no parts, so there the prompt is written again. Updates the text area in place (typing keeps focus). */
  function applyStyle() {
    if (!st.prompt && !st.parts) return;
    if (!st.parts) { if (st.mode === "now" && styleText() && styleText() !== st.promptStyle) write(); return; }
    const next = sentenceNow();
    if (next == null) { st.missing = true; return; }   // a known style the server did not list: the text stays as it is
    Object.assign(st, restyle({ prompt: st.prompt, written: st.written, parts: st.parts, old: st.sentence, next }));
    st.promptStyle = styleText();
    textarea.value = st.prompt;
    paintStyleNote();
  }
  /** Under the box: which reference picture is which figure, as the text stands now (a label taken out drops its line). */
  const figuresBox = h("div.art-figures-box");
  function paintFigures() {
    // the reply's `ref` is the reference's URL (a string; `{url}` accepted too), only ever an /api/art/<hash>/<set>/<id> address
    const pic = (f) => refParts(f.ref)?.url || null;
    clear(figuresBox, figureLines(st.figures, { text: st.prompt, name: opts.figureName || undefined, thumb: pic }));
    // the reference pictures' "Applies to" choices follow the same figures: labels in the text now, names from the view
    if (refs) {
      const used = new Set(labelsIn(st.prompt));
      const rows = cleanFigures(st.figures).filter((f) => used.has(f.label)).map((f) => ({ label: f.label, name: opts.figureName ? opts.figureName(f.entity) || null : null }));
      refs.setFigures(st.written || st.prompt ? rows : null);
    }
  }
  /** A comic's panels in place of the moment list: "5 panels", then each panel's moment as the reader's view says it. */
  const panelsBox = h("div.art-panels-box");
  function paintPanels() {
    if (st.kind !== "comic") { clear(panelsBox); return; }
    const caps = panelCaptions(st.panels, opts.panelText);
    const n = st.panels.length;
    clear(panelsBox, n ? h("div.art-field", h("span.label", `${n} panel${n === 1 ? "" : "s"}`),
      panelList(caps, "art-panels", "Panels"),
      h("p.hint", "Each panel's moment is shown under the picture, never drawn into it.")) : null);
  }
  const styleNote = h("p.hint.art-style-note", { role: "status" });
  function paintStyleNote() {
    styleNote.textContent = st.missing && st.prompt ? "The style you chose is not in the prompt text, so the picture follows the text as you wrote it." : "";
  }

  const chip = (label, pressed, on) => h("button.filter-chip", { type: "button", "aria-pressed": String(pressed), on: { click: on } }, label);
  let momentsOpen = false;
  function paint() {
    textarea.value = st.prompt;
    // The chosen style as one row (its preview and name) that opens the gallery as a sheet over this one.
    const extra = setStyles();
    const styleChoice = styleRow({ id: "art-style-row", extra, book: opts.key, provider: st.provider, value: st.style === LOOSE ? (st.slug || slugOf(st.custom)) : st.style,
      onPick: (s) => {
        const legacy = extra.find((x) => x.slug === s.slug);
        if (legacy) { if (st.style === LOOSE && st.custom === legacy.text) return; st.style = LOOSE; st.custom = legacy.text; st.slug = legacy.slug; }
        else { if (st.style === s.slug) return; st.style = s.slug; st.custom = ""; st.slug = null; }
        applyStyle(); paint(); body.querySelector("#art-style-row")?.focus({ preventScroll: true });
      } }).el;
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
    const modeSeg = modePick();
    if (st.mode === "through") {
      clear(body, modeSeg,
        h("p.hint", "A portrait at their first look and one at each lasting change in how they look, up to your position. Portraits already drawn are skipped. They are drawn as a run, which you can follow in Runs."),
        h("div.art-field", h("label.label", { for: "art-style-row" }, "Style"), styleChoice, styleNote),
        h("div.art-row",
          h("div.art-field", h("span.label", "Quality"), qualitySeg),
          provSel ? h("div.art-field", h("label.label", "Draw with"), provSel) : null),
        modelPick ? h("div.art-field", models.length <= 3 ? h("span.label", "Model") : h("label.label", { for: "art-model" }, "Model"), modelPick) : null,
        // the run's portrait writer: it writes every portrait's prompt (none chosen: the server's default, the strong tier)
        writers.length ? h("div.art-field", h("label.label", { for: "art-writer" }, "Prompts written by"),
          writerSelect({ id: "art-writer", provs: writers, value: st.writer, tier: "strong", onChange: (c) => { st.writer = c; } })) : null,
        err);
      paintFoot();
      return;
    }
    clear(body,
      modeSeg,
      opts.drawnLine ? h("p.art-drawn.subtle", opts.drawnLine) : null,
      moments,
      panelsBox,
      h("div.art-field", h("label.label", { for: "art-prompt" }, "Prompt"), textarea,
        h("p.hint", "Written from what you have read so far. Change anything; the picture is drawn from this text only. ",
          h("button.linkish", { type: "button", on: { click: write } }, "Write it again")),
        figuresBox),
      refs ? refs.el : null,
      h("div.art-field", h("label.label", { for: "art-style-row" }, "Style"), styleChoice, styleNote),
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
    paintPath();
    paintStyleNote();
    paintFigures();
    paintPanels();
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
  /** The choice between one portrait now and the entry's portraits through the book so far (null when not offered); it
   *  heads every state of the sheet, a prompt being written or failed included, so either way is always open. */
  function modePick() {
    return through ? h("div.art-field", h("span.label", "What to draw"),
      h("div.seg.art-mode", { role: "group", "aria-label": "What to draw" },
        [["now", "As they are now"], ["through", "Through the book so far"]].map(([k, l]) =>
          h("button", { type: "button", "aria-pressed": String(st.mode === k), on: { click: () => setMode(k) } }, l)))) : null;
  }
  /** "As they are now" or "Through the book so far". Leaving "now" keeps a prompt already on screen (typed text included)
   *  and abandons one being written; coming back writes one only when there is none. */
  function setMode(k) {
    if (k === st.mode) return;
    if (k === "through" && st.busy) abort();
    st.mode = k;
    clear(err);
    if (k === "now" && !st.prompt) { write(); return; }
    paint();
    body.querySelector(".art-mode [aria-pressed=true]")?.focus();
  }
  /** The run's choices as the estimate and the run route take them: no scenes, portraits of this entry only, up to the
   *  reader's position now (`until`, the frontier anchor) with their finished books. */
  function throughAsk() {
    const at = here();
    // The sheet's image model is the one that draws these portraits: sent as the portrait image model (the estimate prices
    // portraits with it, the run draws them with it), and as the scenes' too, since the run draws no scene.
    return { style: styleText(), level: "none", portraits: "none", entities: [opts.target], until: at.p, finished: at.finished,
      quality: st.quality, provider: st.provider.id, ...(st.model ? { model: st.model } : {}),
      portraitProvider: st.provider.id, ...(st.model ? { portraitModel: st.model } : {}),
      // the drawing-fault check: priced by the estimate and done by the run (both read it as a boolean)
      ...(artDefaults().check ? { check: true } : {}) };
  }
  let estGen = 0, estTimer = null;
  /** Ask the estimate for the choices and position now, unless it already answered for exactly those. */
  async function estimateThrough() {
    const ask = throughAsk();
    const key = JSON.stringify(ask);
    if (!ask.style || ask.until == null) { st.est = null; paintFoot(); return; }
    if (st.est?.ask === key) return;
    const mine = ++estGen;
    st.est = { ask: key, r: null, error: null, busy: true };
    paintFoot();
    let r = null, error = null;
    try {
      r = await api.artEstimate({ key: opts.key, ...ask });
    } catch (e) { error = e; }
    if (mine !== estGen || st.closed) return;
    st.est = { ask: key, r, error, busy: false };
    paintFoot();
  }
  /** The estimate as one sentence ("3 portraits, about 10 cents."), a range while the look index is not checked yet, and
   *  whether there is anything to start. Counts only what the server counted up to the position. */
  function throughLine() {
    const e = st.est;
    if (!styleText()) return { text: "Describe the style first.", ok: false };
    if (here().p == null) return { text: "Nothing is revealed at your position yet.", ok: false };
    if (!e || e.busy) return { text: "Counting the portraits…", ok: false };
    if (e.error) return { text: runError(e.error, "Couldn't count the portraits"), ok: false };
    const r = e.r || {};
    // A server that does not know an entry's run counts the whole cast and would draw it: only an answer that says it
    // counted this entry is a count for it.
    if (!Array.isArray(r.entities) || !r.entities.includes(opts.target)) return { text: "This server can't draw one character through the book yet.", ok: false };
    const lo = Number.isFinite(+r.portraits) ? +r.portraits : null;
    if (lo == null) return { text: "Couldn't count the portraits.", ok: false };
    const hiRaw = r.portraitsMax ?? r.portraits_max;
    const hi = Number.isFinite(+hiRaw) && +hiRaw > lo ? +hiRaw : lo;
    const noun = (n) => `portrait${n === 1 ? "" : "s"}`;
    if (hi === 0) return { text: opts.title ? `Nothing new to draw for ${opts.title} up to here.` : "Nothing new to draw up to here.", ok: false };
    if (hi > lo) {
      const pmax = r.priceMax ?? r.price_max;
      const cost = moneyRange(lo ? r.price : 0, pmax ?? r.price);
      return { text: `${lo ? `${lo} to ${hi}` : `Up to ${hi}`} ${noun(hi)}, ${cost}. The exact number is known once the run has read how they look.`, ok: true };
    }
    const planned = Number.isFinite(+r.planned?.portraits) ? +r.planned.portraits : lo;
    const held = planned > lo ? ` ${planned - lo} more ${planned - lo === 1 ? "is" : "are"} already drawn.` : "";
    return { text: `${lo} ${noun(lo)}, ${money(r.price)}.${held}`, ok: true };
  }
  function paintFoot() {
    drawBtn.hidden = st.mode === "through";
    startBtn.hidden = st.mode !== "through";
    if (st.mode === "through") {
      const ask = throughAsk();
      const stale = st.est?.ask !== JSON.stringify(ask);
      // A count made for other choices or another position is never shown as this one's.
      const line = stale && ask.style && ask.until != null ? { text: "Counting the portraits…", ok: false } : throughLine();
      footNote.textContent = line.text;
      startBtn.disabled = !line.ok || st.starting || stale;
      // A choice or the position changed: count again shortly (a typed custom style asks once the typing pauses).
      if (stale && !st.closed && ask.style && ask.until != null) { clearTimeout(estTimer); estTimer = setTimeout(() => { if (st.mode === "through") estimateThrough(); }, 250); }
      return;
    }
    // An older server's prompt carries its style in the text: until it is written again for a new style, Draw waits.
    const stale = !st.parts && st.prompt && st.promptStyle != null && styleText() !== st.promptStyle;
    // While a reference picture uploads, Draw is off: nothing is drawn without a picture the person just added, and a
    // picture whose upload fails stays said under the row with nothing drawn.
    const uploading = Boolean(refs?.busy());
    const ok = st.prompt.trim().length > 0 && Boolean(styleText()) && !st.busy && !uploading && !stale;
    drawBtn.disabled = !ok;
    const pr = price();
    // Without the image table the price is the prompt route's, for the choices it was asked with; other choices say so
    // rather than show a number for the wrong quality or provider.
    const asked = st.asked && st.asked.quality === st.quality && st.asked.provider === st.provider.id && st.asked.model === st.model && st.asked.shape === st.shape;
    footNote.textContent = st.busy ? "" : uploading ? "Uploading your pictures…" : stale ? "Write the prompt again for this style first."
      : pr != null ? `${money(pr)[0].toUpperCase()}${money(pr).slice(1)} for this picture`
        : imagePrice(st.provider, st.quality, st.shape, st.model) === undefined && st.asked && !asked ? "Price unknown for these choices" : "Price unknown";
  }

  startBtn.addEventListener("click", async () => {
    if (st.starting || st.mode !== "through") return;
    // The count shown must be for the position now: a move since it was counted counts again first, and nothing starts.
    if (st.est?.ask !== JSON.stringify(throughAsk())) { estimateThrough(); return; }
    st.starting = true; startBtn.disabled = true;
    // The run takes the position as `{p}` (an anchor id here) and portraits off: the entries alone say whose portraits.
    const { until, finished, portraits: _all, ...rest } = throughAsk();
    const cap = artDefaults().cap;
    try {
      // The writer chosen in the sheet writes these portraits' prompts (the run's portrait writer); none chosen: the
      // server's default for portrait prompts, the strong tier.
      const writer = textChoice(writers, st.writer);
      const job = await api.artRun(opts.key, { ...rest, portraits: false, until: { p: until }, finished, cap,
        ...(writer ? { portraitWriter: writer } : {}) });
      st.starting = false;
      st.written = st.prompt;   // nothing to ask about on close
      sh.close(true);
      toast(opts.title ? `Drawing ${opts.title} through the book so far.` : "Drawing their portraits through the book so far.", "ok", { host: modalToasts(),
        action: job?.id ? { label: "Follow it in Runs", run: () => { location.hash = `#/studio/job/${encodeURIComponent(job.id)}`; } } : null });
      opts.onStarted?.(job);
    } catch (e) {
      st.starting = false;
      if (st.closed) { toast(`Couldn't start the run: ${e.message}`, "error", { host: modalToasts() }); return; }
      clear(err, h("p", runError(e, "Couldn't start the run")));
      paintFoot();
    }
  });
  drawBtn.addEventListener("click", async () => {
    if (st.busy || refs?.busy()) return;   // Draw is disabled meanwhile; this only guards a click already queued
    // A custom style typed but not yet swapped in (no change event yet): put it in the prompt before it is sent.
    if (st.parts && sentenceNow() !== st.sentence) applyStyle();
    abort();
    const mine = ++gen;
    st.busy = true; st.drawing = true; drawBtn.disabled = true;
    // The old picture is replaced only within its own set: another style files a new picture there and keeps the old one.
    const replacing = Boolean(replaces) && (!opts.replacesStyle || folder() === opts.replacesStyle);
    const sent = st.prompt;   // the text as the provider saw it (a rewrite after a refusal starts from this)
    // The person's pictures go unless neither the image model nor a describing model can take them (the line says so).
    const userRefs = refs && refRoute().path !== "none" ? refs.refs() : [];
    const keep = [...body.childNodes];
    clear(body, loading("Drawing the picture. This takes a few seconds…"));
    footNote.textContent = "";
    try {
      const r = await api.artDraw(opts.key, { kind: st.kind, target: st.target, style: styleText(), shape: st.shape, quality: st.quality,
        prompt: st.prompt, provider: st.provider.id, ...(st.model ? { model: st.model } : {}), ...(replacing ? { replaces } : {}),
        // a redraw sends its list even when empty, so pictures taken out here are not carried over from the old picture
        ...(userRefs.length || replacing ? { refs: userRefs } : {}),
        ...(artDefaults().check ? { check: true } : {}), ...here() });
      st.drawing = false;
      const rec = r?.record || r;
      const slug = !STYLE_LABEL[st.style] ? (typeof r?.slug === "string" && /^[a-z0-9-]{1,64}$/.test(r.slug) ? r.slug : folder()) : folder();
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
      refs?.settle();
      // The new picture shows here only if it may show at the position (its anchor is the item's own, which is visible,
      // but the server decides the anchor; the Reader checks it as it checks every picture).
      const src = slug && opts.srcFor && (!opts.canShow || opts.canShow(rec)) ? opts.srcFor(rec, slug) : null;
      const added = replacing ? "Drawn. It takes the old picture's place in the art set." : "Drawn and added to the art set.";
      clear(body, h("div.art-done", src ? artImg(src, opts.title, "art-done-img", { eager: true }) : null,
        h("p", src ? added : `${added} It shows where it belongs in the book.`),
        autoFixed(rec) ? h("p.hint.art-fixed", "Redrawn: the first try had a drawing fault.") : null,
        leftOutLine(r?.refsLeftOut ?? rec?.refsLeftOut) ? h("p.hint.art-left-out", leftOutLine(r?.refsLeftOut ?? rec?.refsLeftOut)) : null,
        // a picture tied to a figure the text drawn no longer used went to the whole scene: said once
        fallbackDoneLine(userRefs, rec?.appliesFallback ?? r?.appliesFallback, st.kind) ? h("p.hint.art-applies-fallback", fallbackDoneLine(userRefs, rec?.appliesFallback ?? r?.appliesFallback, st.kind)) : null,
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
      if (e.code === "no-vision-model" && refs) {
        // The image model takes no pictures and nothing here can describe them: they are taken out, and Draw goes without.
        refs.drop("No model that can see pictures is set up, so your pictures were taken out. Draw again to draw without them.");
        clear(err);
      } else if (e.code === "bad-refs" && refs) {
        clear(err, h("p", `Your reference pictures could not be used: ${e.message}`));
      } else if (e.code === "refused") {
        // The person stays in control: a rewrite of the same moment, told so it can be drawn, goes into the prompt box to be
        // read before Draw. A second refusal offers it again.
        const fix = h("button.btn", { type: "button", on: { click: () => soften(sent, e.message || "") } }, icon("sparkle"), "Rewrite it so it can be drawn");
        clear(err, h("p", `The provider would not draw this: ${e.message}. Change the prompt and try again, or have it rewritten.`), fix);
      } else clear(err, h("p", e.code === "not-visible" || e.code === "too-few-moments" || e.code === "bad-position" ? promptError(e) : `Couldn't draw it: ${e.message}`));
      paintFoot();
      textarea.focus();
    }
  });
  write();
  return sh;
}

/** A price range in one phrase: "about 7 to 14 cents", "about $1.20 to $2.40", "about 40 cents to $1.20", and with a zero
 *  floor "up to about 14 cents"; either end unknown is "price unknown". */
export function moneyRange(lo, hi) {
  if (lo == null || hi == null || !Number.isFinite(+lo) || !Number.isFinite(+hi)) return "price unknown";
  if (+hi <= +lo) return money(hi);
  if (+lo <= 0) return `up to ${money(hi)}`;
  const a = money(lo).replace(/^about /, ""), b = money(hi).replace(/^about /, "");
  const cents = (t) => /cents?$/.test(t);
  // Both in cents: the unit once ("7 to 14 cents"); otherwise each end keeps its own.
  return cents(a) && cents(b) ? `about ${a.replace(/ cents?$/, "")} to ${b}` : `about ${a} to ${b}`;
}

/** One sentence for an entry's run (or its count) the server refused. */
function runError(e, what) {
  if (e.code === "not-visible") return "This isn't revealed at your position yet.";
  if (e.code === "busy") return "Pictures are being drawn for this book. Try again once that run has finished.";
  if (e.code === "bad-entity") return "Only people and creatures can be drawn through the book.";
  if (e.code === "bad-position") return "Your position or finished books don't fit this book. Move the position and try again.";
  return `${what}: ${e.message}`;
}

/** One sentence for a prompt the server could not write. "dropped" is the pipeline's own check refusing what the model wrote
 *  (a name, hindsight wording...): asking again usually gives a usable one. `why` names the check and the word it caught
 *  ("a name the picture must not show: Hagrid"), when the server could say it without revealing anything. */
function promptError(e) {
  if (e.code === "not-visible") return "This isn't revealed at your position yet.";
  if (e.code === "too-few-moments") return "A comic needs at least two moments of this chapter that you have reached. Read on and try again.";
  if (e.code === "bad-position") return "Your position doesn't fit this book. Move the position and try again.";
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

export function picturesForm({ estimate, fallback = null, open = false, always = false, idPrefix = "art", chapters: listChapters = false, onChange = null, runProvider = null, book = null } = {}) {
  const d = artDefaults();
  // The style is a slug (built-in, or custom saved on this server), or an older default typed as words, sent as it is.
  const st = { on: always, style: d.style, level: "main", cast: "main",
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
  const slug = () => st.style;
  // A custom style's name and preview come with the styles list: paint again once it is here (a built-in one needs nothing).
  loadStyles().then(() => { if (provs && !STYLE_LABEL[st.style]) paint(); });
  function paint() {
    sum.textContent = st.on ? `${styleLabel(slug())}, ${LEVEL_SUMMARY[st.level]}${CAST_SUMMARY[st.cast]}` : "Off";
    if (provs && !provs.length) {
      clear(body, h("p.hint", "Pictures need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one in Settings")));
      line.textContent = "";
      return;
    }
    const radio = (name, value, label, hint) => {
      const r = h("input", { type: "radio", name: `${idPrefix}-${name}`, value, checked: st[name] === value, disabled: !st.on,
        on: { change: () => { st[name] = value; paint(); } } });
      return h(`label.prow${st[name] === value ? ".on" : ""}`, r, h("span.pr-text", h("span.pr-title", label), hint ? h("span.pr-line", hint) : null));
    };
    // An older default typed as words, not saved here: still offered, by its words.
    const extra = STYLE_LABEL[st.style] || savedStyle(st.style) ? [] : [{ slug: st.style, name: styleLabel(st.style), text: st.style }];
    // `book`: the index's library key (or a function giving it, null before a first build), for the book's own style previews.
    const styleChoice = styleRow({ id: `${idPrefix}-style`, value: st.style, extra, disabled: !st.on, book, provider: st.provider,
      onPick: (s) => { if (s.slug === st.style) return; st.style = s.slug; paint(); el.querySelector(`#${idPrefix}-style`)?.focus({ preventScroll: true }); } }).el;
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
      h("div.field", h("label.label", { for: `${idPrefix}-style` }, "Style"), styleChoice),
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
        ...(listChapters ? { chapters: asked } : {}), ...(artDefaults().check ? { check: "1" } : {}) });
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
      line.textContent = estimateText(r, st.cap);
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
      provider: st.provider.id, model: st.model || null, cap: st.cap, ...(listChapters && chapterParam() ? { chapters: chapterParam() } : {}), ...models(),
      ...(artDefaults().check ? { check: true } : {}) } : null),
    refresh,
    reason,
    ready: () => Boolean(st.provider && slug()),
  };
}
