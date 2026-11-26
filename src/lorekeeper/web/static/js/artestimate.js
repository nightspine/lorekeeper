// The pictures estimate in words, with no imports so it can be tested in Node (tests/js/run_artestimate.mjs).

/** "about 3 cents", "about $1.75", "price unknown" (never a made-up number). */
export function money(usd) {
  if (usd == null || !Number.isFinite(+usd)) return "price unknown";
  const v = +usd;
  if (v < 0.995) { const c = Math.max(1, Math.round(v * 100)); return `about ${c} cent${c === 1 ? "" : "s"}`; }
  return `about $${v.toFixed(2)}`;
}

/** The estimate line: "About 8 pictures, about 27 cents." or, while some counted characters' looks are not checked yet (the
 *  run may draw more than one portrait of them), "8 to 24 pictures, up to about 81 cents. Characters whose look changes get
 *  more than one portrait." A range only when the answer's `picturesMax` is above `pictures`; the cap note follows the
 *  higher price. */
export function estimateText(r, cap) {
  const n = +r.pictures;
  const max = Number.isFinite(+r.picturesMax) && r.picturesMax != null ? +r.picturesMax : n;
  const range = max > n;
  const price = range ? r.priceMax : r.price;
  const known = price != null && Number.isFinite(+price);
  const stops = known && cap != null && +price > cap ? `. The run stops at $${Number.isInteger(cap) ? cap : (+cap).toFixed(2)}` : "";
  if (!range) return `About ${n} picture${n === 1 ? "" : "s"}, ${money(price)}${stops}.`;
  return `${n} to ${max} pictures, ${known ? `up to ${money(price)}` : "price unknown"}${stops}. Characters whose look changes get more than one portrait.`;
}
