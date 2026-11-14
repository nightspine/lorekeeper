// Reads a JSON list of labels on stdin; prints [{role, modifiers, normalize}|null...] from kinship.js.
//   node tests/js/kinship_dump.mjs < labels.json
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const { parse, normalize } = await import(join(here, "../../src/lorekeeper/web/static/js/kinship.js"));
let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const out = JSON.parse(raw).map((label) => {
  const p = parse(label);
  return { role: p ? p.role : null, modifiers: p ? [...p.modifiers].sort() : null, normalize: normalize(label) };
});
console.log(JSON.stringify(out));
