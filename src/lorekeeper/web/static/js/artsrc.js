// Picture file URLs and their responsive variants. Dependency-free (Node tests it: tests/js/run_artsrc.mjs).
// The server (GET /api/art/{hash}/{style}/{id}?w=N) serves WebP copies at these widths, never larger than the original;
// no `w` is the original, which only the lightbox's full view asks for.

export const WIDTHS = [160, 320, 640, 1280];
const BASE_RE = /^\/api\/art\/[^/?#]+\/(?!refs\/)[^/?#]+\/[A-Za-z0-9_-]{1,64}$/;

/** Is this the plain URL of a picture file (no query)? Only those get variants. */
export const isArtBase = (url) => typeof url === "string" && BASE_RE.test(url);
/** The URL of one variant; null when `url` is not a picture file's URL or `w` is not a served width. */
export function variantUrl(url, w) {
  return isArtBase(url) && WIDTHS.includes(w) ? `${url}?w=${w}` : null;
}
/** `srcset` text for a picture file's URL ("…?w=160 160w, …"); null when it has no variants. */
export function srcsetFor(url) {
  return isArtBase(url) ? WIDTHS.map((w) => `${url}?w=${w} ${w}w`).join(", ") : null;
}
/** The width to use as a plain `src` fallback for an element displayed `px` CSS pixels wide at 2x: the smallest served width
 *  covering it, the largest otherwise. */
export function fallbackWidth(px) {
  const need = Math.max(1, Number(px) || 0) * 2;
  return WIDTHS.find((w) => w >= need) || WIDTHS[WIDTHS.length - 1];
}

/** `sizes` for each placement (the CSS width the picture is displayed at). */
export const SIZES = {
  face: "24px",                                                    // the Cast row's round portrait
  tl: "(max-width: 640px) 56px, 64px",                             // a Timeline event's picture
  list: "48px",                                                    // the entry's lists
  tile: "(max-width: 640px) 76px, 72px",                           // the entry's version tiles
  who: "(max-width: 640px) 25vw, 160px",                           // the Overview's who's who
  chapter: "(max-width: 640px) 40vw, 240px",                       // the Overview's chapter thumbs
  thumbs: "(max-width: 640px) 33vw, 240px",                        // a chapter's two or three plates
  plate: "(max-width: 640px) 100vw, 720px",                         // a plate or comic at the column's width
  hero: "(max-width: 640px) 100vw, 420px",                         // the picture-first entry
  overview: "100vw",                                               // the Overview header
  side: "(max-width: 640px) 96px, 140px",                          // the entry's portrait beside the profile, the Connections panel
  map: "120px",
  mini: "24px",                                                    // figure thumbnails
};
