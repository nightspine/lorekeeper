# Roadmap

What comes next, roughly in order. Each item gets a GitHub issue when work starts; the spec's own open questions are in SPEC.md §11. Dates are deliberately absent: this is maintained in one person's free time.

## 1. Manual index editor (human in the loop)

Shipped (issue #6): the Reader's edit mode adds, changes and deletes entries, names, details, connections, events and recaps, with the reveal point chosen from the book's paragraphs; "Write it myself" on the build form starts an index with no model; every change is one undoable step in the same journal as merges; hindsight and long quotes are refused as the generator refuses them; and edits are recorded per edition and applied again after a rebuild. Still open: editing indexes that were not built here, and more than one editor at a time.

## 1b. Connections map: what counts as a tie, and a layout that reads as a social network

Two things about the Connections tab are to be re-evaluated, in this order.

**What earns a line.** Today a line is any relation the model proposed and the fact-check accepted, plus "appear together" edges from shared events when asked for. Lines appear between entities that have no real tie. The plan:

- Measure first: on the bundled Dracula index and one series, list every relation with its cited paragraph and mark it stated, inferred-but-fair, or spurious; count how many pairs have several near-synonymous ties. That number is the baseline the rest is judged against.
- Tighten the generator: a relation must be stated by the passage in words (the way `TIE_WORDS` already guards descriptions), name both sides in the cited paragraph, and be checked as a sentence ("Harker *is the guest of* Dracula") rather than a bare label; one tie per pair per passage unless it changed; the companion's existing ties are already shown so the model repeats fewer.
- Give relations a `kind` (family, allegiance, service, hostility, acquaintance, place) the map can colour and filter by, the way events now carry a `kind`.
- Make "appear together" opt-in only and require two or more shared events; show the sentence behind a line on hover and in the panel, so a wrong line is one click from the edit mode's bin.

**How the map is drawn.** The target look is a night sky: entries as small glowing stars in their type colour on a dark starfield, thin constellation lines for stated ties, each group gathered loosely around the entry with the most ties in it and named for it ("Around Van Helsing"), dashed lines between the hubs of groups that are tied, every star labelled when there is room, and no island shapes or sea between groups. The plan:

- Keep the grouping and the two-level layout (they already find the hubs and keep positions stable from one position to the next), but drop the hulls, fold nothing, space groups more loosely, and draw cross-group ties as dashed lines between the entries themselves.
- Restyle: starfield ground, glow on the stars, hairline ties, muted "Around X" labels under each group, names beside the stars.
- Compare on Dracula at 30%, 60% and 100% and on a series; keep the zoom levels only if they still earn their place once nothing is folded.

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
