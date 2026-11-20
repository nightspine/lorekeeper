// api.js: how a mentions request is built and remembered (order and fin must always travel, "" included).
//   node tests/js/run_mentions_query.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { mentionsQuery, mentionsSig, generation } = await import(join(here, "../../src/lorekeeper/web/static/js/api.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };
const parse = (q) => Object.fromEntries(new URLSearchParams(q));

test("empty finished set is sent as fin=", () => {
  const q = mentionsQuery({ entity: "watson", seq: 244, order: "publication", fin: "" });
  assert.ok(q.endsWith("&fin="), q);
  assert.deepEqual(parse(q), { entity: "watson", seq: "244", order: "publication", fin: "" });
});
test("two finished books give both keys", () => {
  const q = parse(mentionsQuery({ entity: "e1", seq: 5, order: "reading", fin: "a,b" }));
  assert.equal(q.fin, "a,b");
  assert.equal(q.order, "reading");
});
test("no fin in the Reader's URL means no fin", () => {
  const q = parse(mentionsQuery({ entity: "e1", seq: 5, order: "publication", fin: null }));
  assert.ok(!("fin" in q));
  assert.equal(q.order, "publication");
});
test("a single book sends neither", () => {
  assert.deepEqual(parse(mentionsQuery({ entity: "x y", seq: 0 })), { entity: "x y", seq: "0" });
});
test("the memo key tells every difference apart", () => {
  const k = (...a) => mentionsSig("k", "e", 10, ...a);
  const sigs = [k(null, null), k("p", null), k("p", ""), k("p", "a"), k("p", "a,b"), k("q", "a")];
  assert.equal(new Set(sigs).size, sigs.length);
  assert.notEqual(mentionsSig("k", "e", 10), mentionsSig("k", "e", 11));
});

test("the memo key changes when only provenance.updated changes", () => {
  const a = mentionsSig("k", "e", 10, "p", null, "2026-11-20T10:00:00Z");
  const b = mentionsSig("k", "e", 10, "p", null, "2026-11-20T10:05:00Z");
  assert.notEqual(a, b);
  assert.equal(a, mentionsSig("k", "e", 10, "p", null, "2026-11-20T10:00:00Z"));
});

test("an answer that was in flight when the index changed is stale", () => {
  const g = generation();
  const mine = g.next();
  assert.ok(g.current(mine));
  g.invalidate();               // forgetMentions(): a merge or edit reloaded the index
  assert.ok(!g.current(mine));  // the old answer is not painted or remembered
  const again = g.next();
  assert.ok(g.current(again) && !g.current(mine));
});
test("a newer section makes the older one stale", () => {
  const g = generation();
  const a = g.next(), b = g.next();
  assert.ok(!g.current(a) && g.current(b));
});

process.exit(failed ? 1 : 0);
