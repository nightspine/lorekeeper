# Roadmap

What comes next, roughly in order. Each item gets a GitHub issue when work starts; the spec's own open questions are in SPEC.md §11. Dates are deliberately absent: this is maintained in one person's free time.

## 1. Manual index editor (human in the loop)

Indexes today are built by a model and corrected after the fact through merges. The editor makes a person a first-class author:

- **Create an index from scratch** for a book in the library, with no model involved: pick the book, walk the chapters, add entities, facts, relationships, events and recaps while reading, each anchored to the paragraph it comes from (the editor picks the anchor from the text; the author never types an id).
- **Edit anything the model produced**: rename an entity, rewrite a description or fact, move its reveal point, change its type, add or remove a relationship or an event, mark a fact as inferred.
- **Delete entities** (and single facets, relations or events), with the same undo journal the merges use.
- **Overrides survive regeneration**: an edit is recorded as an override on the entity or item id, so re-running the generator on the same book keeps the human's version.
- **Spoiler rules enforced in the UI**: a description can only cite text up to its reveal point; the editor shows the passage and refuses hindsight wording the same way the generator does.
- A beautiful, calm flow in the existing "lit and unlit" design: the Reader's entry panel grows an Edit mode rather than a separate admin area.

Tracked in issue #6.

## 2. Reader apps

- Test the KOReader thin client on a device, then a fat client (local gating in Lua) so an e-reader works offline.
- A second reference integration for an iOS reader.

## 3. Generation quality

- Verify the provider registry's model ids against each provider's live list, and measure speed for the estimate.
- Mention anchors per entity ("appears in chapters") computed on device from names.
- Cross-book identity for translations and alternate editions.

## 4. Sharing

- A community registry of indexes with signed files and maintainer keys (SPEC §11.6).
- Index embedding in EPUB (SPEC §9) end to end.
