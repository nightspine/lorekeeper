// Runs every conformance case through the browser gating module. Exit code = number of failures.
//   node tests/js/run_conformance.mjs conformance/
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const { view } = await import(join(here, "../../src/lorekeeper/web/static/js/gating.js"));

const canon = (v) => JSON.stringify(v, (k, x) =>
  x && typeof x === "object" && !Array.isArray(x) ? Object.fromEntries(Object.entries(x).sort()) : x);

const dir = resolve(process.argv[2] || join(here, "../../conformance"));
let failures = 0;
for (const name of readdirSync(dir).filter((f) => f.endsWith(".json")).sort()) {
  const path = join(dir, name);
  const c = JSON.parse(readFileSync(path, "utf8"));
  const load = (p) => JSON.parse(readFileSync(resolve(dirname(path), p), "utf8"));
  const actual = view(c.indexes.map(load), c.position, c.series ? load(c.series) : null,
    c.order ?? null, c.finished ?? null);
  const ok = canon(actual) === canon(c.expect);
  if (!ok) failures++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}`);
}
process.exit(failures);
