# Roadmap

What comes next, roughly in order. Each item gets a GitHub issue when work starts; the spec's own open questions are in SPEC.md §11. Dates are deliberately absent: this is maintained in one person's free time.

## 1. Manual index editor (human in the loop)

Shipped (issue #6): the Reader's edit mode adds, changes and deletes entries, names, details, connections, events and recaps, with the reveal point chosen from the book's paragraphs; "Write it myself" on the build form starts an index with no model; every change is one undoable step in the same journal as merges; hindsight and long quotes are refused as the generator refuses them; and edits are recorded per edition and applied again after a rebuild. Still open: editing indexes that were not built here, and more than one editor at a time.

## 1b. Connections map: what counts as a tie, and a layout that reads as a social network

Shipped in 0.6 and 0.7.

- **Measured first.** The bundled Dracula index had 56 ties over 25 pairs, and 10 pairs carried 41 of them (seven near-synonyms between Dracula and Harker alone). Several labels were actions ("drove the stake through the heart of") and one was a sentence ("Un-Dead, calls Arthur her husband"). Requiring both sides to be named in the cited paragraph was tried and dropped: the novel is first-person diaries, so "I" and "the Count" fail it on correct ties.
- **Generator.** Ties are lasting states with a `kind` (family, romance, friendship, alliance, service, hostility, acquaintance, place); a label over 8 words is dropped; a passage keeps one tie per pair and kind, the one shown latest; the fact-check drops ties that are one action; a newer tie of the same kind between the same two replaces the older one, which becomes history.
- **Map.** Glowing dots and hairline ties in loose groups named "Around X", no island shapes; lines tinted by kind and filterable; hovering a line shows its tie and clicking it selects the entry; "appear together" needs two or more shared events. The zoom levels stay only to thin the names when zoomed far out.

Next, if the results call for it: a kind per tie in the duplicate judge's context, and ties carried across series books with the same replacement rule.

## 2. Non-fiction and self-help books

Today's index describes a story. Other books need their own shape:

- **A book kind** on every index (`fiction`, `non-fiction`, `self-help`, with room for more), inferred by default from the EPUB and overridable in the build form, recorded in the index and the series manifest. *Landed (phase 1):* the `bookKind` field, the inference from subjects and chapter titles (with one optional model call), the build form row, `generate --kind`, `lorekeeper kind`, and the kind in views and `/v0/lookup`. Every kind still builds the story index. What remains is below.
- **One schema per kind**, versioned with the spec. A non-fiction companion gates concepts and terms with definitions as the book gives them, people and works cited, the argument so far and chapter takeaways; a self-help companion gates practices, principles and exercises as they are introduced and the author's framework. The rule stays the same: nothing appears before the book has said it.
- **Generation, Reader and protocol per kind**: separate extraction and fact-check prompts, tabs and a concept map in the Reader, new conformance cases. (The kind in `/v0` lookup results has landed.)

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
