// The Ask tab's rule for when a thread must go: revealsLess(snapshot, view, position). Run by tests/test_js_ask.py.
import assert from "node:assert/strict";
import { anyRevealsLess, draftAfterInput, revealsLess, snapshot } from "../../src/lorekeeper/web/static/js/ask.js";

const ent = (id, name, aliases = [], extra = {}) => ({ id, name, aliases, profile: null, facets: [], history: [], ...extra });
const view = (o = {}) => ({ entities: [ent("E1", "Holmes")], relations: [], relationHistory: [], events: [{ id: "V1" }], chapters: [{ id: "C1" }], recap: { id: "R1" }, ...o });
const pos = (o = {}) => ({ key: "k", book: "b1", rank: 1, ord: 5, order: "pub", finished: ["b0"], ...o });
const at = snapshot(view(), pos());
const t = (name, fn) => { fn(); console.log("ok", name); };

t("same view, same place keeps", () => assert.equal(revealsLess(at, view(), pos()), false));
t("forward with the story recap replaced keeps", () => assert.equal(revealsLess(at,
  view({ recap: { id: "R2" }, chapters: [{ id: "C1" }, { id: "C2" }], events: [{ id: "V1" }, { id: "V2" }] }), pos({ ord: 9 })), false));
t("a replaced connection (moved to relationHistory) keeps", () => {
  const was = snapshot(view({ relations: [{ id: "L1" }] }), pos());
  assert.equal(revealsLess(was, view({ relations: [{ id: "L2" }], relationHistory: [{ id: "L1" }] }), pos({ ord: 8 })), false);
});
t("a replaced detail (moved to history) keeps", () => {
  const was = snapshot(view({ entities: [ent("E1", "Holmes", [], { facets: [{ id: "F1" }] })] }), pos());
  assert.equal(revealsLess(was, view({ entities: [ent("E1", "Holmes", [], { facets: [{ id: "F2" }], history: [{ id: "F1" }] })] }), pos({ ord: 8 })), false);
});
t("a name gained going forward keeps", () => {
  assert.equal(revealsLess(at, view({ entities: [ent("E1", "Holmes", ["Sherlock"])] }), pos({ ord: 8 })), false);
});
t("back before an alias clears though every id is the same", () => {
  const was = snapshot(view({ entities: [ent("E1", "Holmes", ["Sherlock"])] }), pos());
  assert.equal(revealsLess(was, view(), pos({ ord: 4 })), true);
  // and even with the anchor not lower (a view alone that lost the alias)
  assert.equal(revealsLess(was, view(), pos()), true);
});
t("a display name that changed back clears", () => {
  const was = snapshot(view({ entities: [ent("E1", "Sherlock Holmes")] }), pos());
  assert.equal(revealsLess(was, view(), pos()), true);
});
t("back one anchor with no id change clears", () => assert.equal(revealsLess(at, view(), pos({ ord: 4 })), true));
t("an item gone clears", () => assert.equal(revealsLess(at, view({ events: [] }), pos()), true));
t("a finished box unticked clears", () => assert.equal(revealsLess(at, view(), pos({ finished: [] })), true));
t("a finished box ticked keeps", () => assert.equal(revealsLess(at, view(), pos({ finished: ["b0", "bx"] })), false));
t("another index clears", () => assert.equal(revealsLess(at, view(), pos({ key: "other" })), true));
t("another reading order clears", () => assert.equal(revealsLess(at, view(), pos({ order: "chron" })), true));
t("an earlier book clears, a later one keeps", () => {
  assert.equal(revealsLess(at, view(), pos({ rank: 0, book: "b0", ord: 99 })), true);
  assert.equal(revealsLess(at, view(), pos({ rank: 2, book: "b2", ord: 0 })), false);
});
t("nothing to compare keeps", () => assert.equal(revealsLess(null, view(), pos()), false));

// The draft: text typed by hand gets a snapshot when the field goes from empty to non-empty.
const take = (view_, pos_) => () => snapshot(view_, pos_);
t("typed, then moved back, is cleared", () => {
  const withAlias = view({ entities: [ent("E1", "Holmes", ["Sherlock"])] });
  const draft = draftAfterInput(null, "Who is Sherlock?", take(withAlias, pos()));
  assert.ok(draft);
  assert.equal(anyRevealsLess([draft], view(), pos({ ord: 4 })), true);
});
t("typed, then moved forward, is kept", () => {
  const draft = draftAfterInput(null, "Who is Holmes?", take(view(), pos()));
  assert.equal(anyRevealsLess([draft], view({ recap: { id: "R2" } }), pos({ ord: 9 })), false);
});
t("the snapshot is taken once, at the first character", () => {
  const first = draftAfterInput(null, "W", take(view(), pos()));
  assert.equal(draftAfterInput(first, "Wh", () => { throw new Error("taken again"); }), first);
});
t("emptied by hand leaves nothing to clear", () => {
  const draft = draftAfterInput(null, "Who", take(view(), pos()));
  assert.equal(draftAfterInput(draft, "  ", take(view(), pos())), null);
  assert.equal(anyRevealsLess([null], view(), pos({ ord: 0 })), false);
});
