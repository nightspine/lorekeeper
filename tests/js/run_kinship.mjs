// kinship.js: label parsing (good and noise labels from real indexes), normalisation, and family trees.
//   node tests/js/run_kinship.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { parse, normalize, tie, families, surname, reverse } = await import(join(here, "../../src/lorekeeper/web/static/js/kinship.js"));

let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

// label → [role, modifiers, normalized]
const GOOD = {
  "daughter of": ["child", [], "daughter of"], "sister of": ["sibling", [], "sister of"], "son of": ["child", [], "son of"],
  "father of": ["parent", [], "father of"], "brother of": ["sibling", [], "brother of"], "husband of": ["spouse", [], "husband of"],
  "wife of": ["spouse", [], "wife of"], "cousin of": ["cousin", [], "cousin of"], "mother of": ["parent", [], "mother of"],
  "half-sister of": ["sibling", ["half"], "half sister of"], "married to": ["spouse", [], "married to"], "aunt of": ["pibling", [], "aunt of"],
  "guardian of": ["guardian", [], "guardian of"], "nephew of": ["nibling", [], "nephew of"], "widow of": ["spouse", ["late"], "widow of"],
  "ex-husband of": ["spouse", ["ex"], "ex husband of"], "niece of": ["nibling", [], "niece of"], "younger brother of": ["sibling", ["younger"], "younger brother of"],
  "ward of": ["ward", [], "ward of"], "sibling of": ["sibling", [], "sibling of"], "granddaughter of": ["grandchild", [], "granddaughter of"],
  "grandson of": ["grandchild", [], "grandson of"], "grandfather of": ["grandparent", [], "grandfather of"], "grandchild of": ["grandchild", [], "grandchild of"],
  "stepson of": ["child", ["step"], "step son of"], "uncle of": ["pibling", [], "uncle of"], "is niece of": ["nibling", [], "niece of"],
  "blood-niece of": ["nibling", ["blood"], "blood niece of"],
  // beyond the data
  "Mom of": ["parent", [], "mother of"], "his wife": ["spouse", [], "wife of"], "the late husband of": ["spouse", ["late"], "late husband of"],
  "mother-in-law of": ["parent", ["in-law"], "mother-in-law of"], "brother-in-law of": ["sibling", ["in-law"], "brother-in-law of"],
  "step-son of": ["child", ["step"], "step son of"], "greatgrandmother of": ["grandparent", ["great"], "great grandmother of"],
  "great-grandmother of": ["grandparent", ["great"], "great grandmother of"], "adoptive mother of": ["parent", ["adoptive"], "adoptive mother of"],
  "twin of": ["sibling", [], "twin of"], "twin brother of": ["sibling", ["twin"], "twin brother of"], "second cousin of": ["cousin", ["second"], "second cousin of"],
  "godfather of": ["guardian", [], "godfather of"], "godson of": ["ward", [], "godson of"], "widower of": ["spouse", ["late"], "widower of"],
  "Her sister": ["sibling", [], "sister of"], "foster-daughter of": ["child", ["foster"], "foster daughter of"], "dad of": ["parent", [], "father of"],
};
const NOISE = ["killed mother of", "barracks brother of", "brother-in-arms of", "guardian and friend of", "possible father of", "sibling of, fights",
  "calls cousin, rebuffed by", "grandson of the cast-out daughter of", "remembers late wife", "keeps secret from", "friend of", "", "servant of",
  "suspected brother of", "alleged son of", "rumoured wife of", "rumored husband of", "mother of the bride", "brother of Jonathan", "knows", "wife-beater of"];

test("parse: every good label", () => {
  for (const [label, [role, mods, norm]] of Object.entries(GOOD)) {
    const p = parse(label);
    assert.ok(p, `parse(${JSON.stringify(label)}) is null`);
    assert.equal(p.role, role, label);
    assert.deepEqual([...p.modifiers].sort(), [...mods].sort(), label);
    assert.equal(normalize(label), norm, label);
  }
});
test("parse: every noise label is not kinship", () => {
  for (const label of NOISE) { assert.equal(parse(label), null, label); assert.equal(normalize(label), null, label); }
});
test("normalize is idempotent", () => {
  for (const label of Object.keys(GOOD)) { const n = normalize(label); assert.equal(normalize(n), n, label); }
});
test("tie: kind family makes an unreadable label 'related'; other kinds do not", () => {
  assert.equal(tie({ label: "keeps secret from", kind: "family" }).role, "related");
  assert.equal(tie({ label: "keeps secret from", kind: "hostility" }), null);
  assert.equal(tie({ label: "wife of", kind: "romance" }).role, "spouse");
});
test("reverse", () => { assert.equal(reverse("parent"), "child"); assert.equal(reverse("pibling"), "nibling"); assert.equal(reverse("cousin"), "cousin"); });
test("surname", () => {
  assert.equal(surname("Mr. Dursley"), "Dursley");
  assert.equal(surname("Vernon Dursley"), "Dursley");
  assert.equal(surname("Abraham Van Helsing"), "Van Helsing");
  assert.equal(surname("Petunia"), null);
  assert.equal(surname("Lucy Westenra (the elder)"), "Westenra");
});

// ---- the Dursleys: Harry is Dudley's cousin and Petunia's nephew, neither stated ---------------------------------
const ent = (id, name) => ({ id, name, type: "person", aliases: [], profile: null, facets: [], history: [] });
const rel = (id, src, label, dst, kind) => ({ id, src, dst, label, ...(kind ? { kind } : {}), book: null });
const view = {
  bookKind: "fiction",
  entities: [ent("vernon", "Vernon Dursley"), ent("petunia", "Petunia Dursley"), ent("dudley", "Dudley Dursley"), ent("lily", "Lily Potter"), ent("harry", "Harry Potter"), ent("hagrid", "Rubeus Hagrid")],
  relations: [rel("r1", "vernon", "married to", "petunia", "family"), rel("r2", "dudley", "son of", "vernon"), rel("r3", "dudley", "son of", "petunia"),
    rel("r4", "petunia", "sister of", "lily"), rel("r5", "harry", "son of", "lily"), rel("r6", "hagrid", "friend of", "harry", "friendship")],
  relationHistory: [rel("h1", "hagrid", "guardian of", "harry")],
  events: [], chapters: [],
};
test("families: the Dursleys", () => {
  const fams = families(view);
  assert.equal(fams.length, 1);
  const f = fams[0];
  assert.equal(f.name, "The Dursley family");
  assert.deepEqual([...f.members].sort(), ["dudley", "harry", "lily", "petunia", "vernon"]);
  const L = (id) => f.layer.get(id), X = (id) => f.x.get(id);
  assert.equal(L("vernon"), L("petunia"));
  assert.equal(L("lily"), L("petunia"));
  assert.equal(L("dudley"), L("vernon") + 1);
  assert.equal(L("harry"), L("dudley"));
  assert.equal(Math.abs(X("vernon") - X("petunia")), 1, "spouses adjacent");
  assert.ok(Math.abs(X("petunia") - X("lily")) < 1.5, "Lily beside Petunia");
  assert.ok(Math.abs(X("dudley") - (X("vernon") + X("petunia")) / 2) < 1e-9, "Dudley centred under his parents");
  assert.ok(Math.abs(X("harry") - X("lily")) < 1e-9, "Harry under Lily");
  assert.ok(f.ties.get("harry").length >= 1);
});
test("families: Hagrid's friendship is not kinship; history only on request", () => {
  assert.ok(!families(view)[0].members.includes("hagrid"));
  const h = families(view, { history: true });
  assert.ok(h[0].members.includes("hagrid"));
  assert.ok(h[0].links.some((l) => l.old));
});
test("families: self-help never has any; no kinship, no families", () => {
  assert.deepEqual(families({ ...view, bookKind: "self-help" }), []);
  assert.deepEqual(families({ ...view, relations: [view.relations[5]] }), []);
});
test("families: a tie to something not in the view is skipped", () => {
  const v = { ...view, entities: view.entities.filter((e) => e.id !== "lily") };
  const f = families(v)[0];
  assert.ok(!f.members.includes("lily") && !f.members.includes("harry"));
});
test("families: a single tie is a family of two; name after the best-tied member", () => {
  const v = { entities: [ent("a", "Jonathan Harker"), ent("b", "Mina Murray")], relations: [rel("x", "b", "wife of", "a")], relationHistory: [] };
  const f = families(v);
  assert.equal(f.length, 1);
  assert.equal(f[0].members.length, 2);
  assert.equal(f[0].name, "The family of Jonathan Harker");
  assert.equal(f[0].layer.get("a"), f[0].layer.get("b"));
});
test("families: grandparents above, cousins level, largest family first", () => {
  const v = { entities: [ent("g", "Old Tom"), ent("p", "Ann Hale"), ent("c", "Bo Hale"), ent("d", "Cy Fox"), ent("x", "Ed Lo"), ent("y", "Flo Lo")],
    relations: [rel("1", "g", "grandfather of", "c"), rel("2", "p", "mother of", "c"), rel("3", "d", "cousin of", "c"), rel("4", "x", "husband of", "y", "family")], relationHistory: [] };
  const f = families(v);
  assert.equal(f.length, 2);
  assert.equal(f[0].members.length, 4);
  assert.equal(f[0].layer.get("g") + 2, f[0].layer.get("c"));
  assert.equal(f[0].layer.get("p") + 1, f[0].layer.get("c"));
  assert.equal(f[0].layer.get("d"), f[0].layer.get("c"));
});
process.exit(failed);
