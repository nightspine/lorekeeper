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

## 2. Non-fiction and self-help books

Today's index describes a story. Other books need their own shape:

- **A book kind** on every index (`fiction`, `non-fiction`, `self-help`, with room for more), inferred by default from the EPUB and overridable in the build form, recorded in the index and the series manifest.
- **One schema per kind**, versioned with the spec. A non-fiction companion gates concepts and terms with definitions as the book gives them, people and works cited, the argument so far and chapter takeaways; a self-help companion gates practices, principles and exercises as they are introduced and the author's framework. The rule stays the same: nothing appears before the book has said it.
- **Generation, Reader and protocol per kind**: separate extraction and fact-check prompts, tabs and a concept map in the Reader, the kind in `/v0` responses, new conformance cases.

This is a large change across SPEC §4 and §7, the schemas and the conformance suites. It lands as a spec minor version with the fiction schema unchanged, so existing indexes stay valid. Tracked in issue #13.

## 3. Reader apps

- Test the KOReader thin client on a device, then a fat client (local gating in Lua) so an e-reader works offline.
- A second reference integration for an iOS reader.

## 4. Generation quality

- Verify the provider registry's model ids against each provider's live list, and measure speed for the estimate.
- Mention anchors per entity ("appears in chapters") computed on device from names.
- Cross-book identity for translations and alternate editions.

## 5. Sharing

- A community registry of indexes with signed files and maintainer keys (SPEC §11.6).
- Index embedding in EPUB (SPEC §9) end to end.
