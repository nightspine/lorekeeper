# Lorekeeper Specification — v0.1 (draft)

Status: working draft, 2026-10-21. Everything here may change before 1.0.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

## 1. Purpose

Lorekeeper is an open format and protocol for **spoiler-safe book companions**:
characters, places, terms, relationships, events and "story so far" recaps that
an ebook reader shows *only up to the reader's current position*.

The design separates three concerns:

| Layer | What it is | Who implements it |
|---|---|---|
| **Format** (§3–§6) | A JSON index document for one book, plus a series manifest | Generators, editors, validators |
| **Protocol** (§8) | A small read-only HTTP API for finding and fetching indexes | Self-hosted servers, community registries |
| **Client gating** (§7) | A deterministic algorithm that hides everything past the reader's position | Ebook reader apps |

Reader apps never need AI. Indexes are built once (by AI, by people, or both),
then shared.

### 1.1 Goals

- One file serves **every** reading position; clients filter locally and offline.
- Positions survive **different editions** of the same book.
- Indexes are **checkable**: every claim cites where in the text it comes from.
- Works on constrained devices (e-ink readers running Lua).
- Series-aware.

### 1.2 Non-goals (v0)

- Generation pipelines. The format is agnostic to how an index is produced (§6 sets content rules only).
- Rendering or UI. §7.6 gives presentation guidance, not requirements.
- Write APIs, accounts, ratings, payments. A registry MAY add these outside this spec.

## 2. Concepts

- **Work** — a book as written (e.g. *A Study in Scarlet*), independent of edition.
- **Edition** — one concrete text of a work (a particular EPUB). Editions differ in front matter, spelling, hyphenation and paragraphing.
- **Index** — a Lorekeeper document describing one work (§4).
- **Series** — an ordered set of works with shared entities (§5).
- **Block** — one paragraph-level unit of book text (§3.1).
- **Anchor** — a named point in the text, identified by a fingerprint of a block (§3.5). Every revealed item names the anchor at which it becomes known.
- **Position** — the last anchor the reader has passed, in a given book.
- **View** — what a reader at a position may see (§7).

## 3. Text model and anchors

### 3.1 Text model

An edition's text is a sequence of **blocks**:

1. Take the EPUB spine in order, skipping items with `linear="no"`.
2. In each XHTML document, each of these elements starts a block: `p`, `h1`–`h6`, `li`, `dt`, `dd`, `td`, `th`, `pre`, `figcaption`, `caption`.
3. A block's text is the character data inside the element, **excluding** text inside nested block elements (which form their own blocks). Text in `script`, `style`, `head` and `title` is ignored. `<br>` is a space. Text outside any block element is ignored.
4. Blocks are ordered by the position of their start tag.
5. Blocks with zero words (§3.2) are dropped. The remaining blocks are numbered from 0; this number is the block's **seq** in that edition.
6. If a document yields no block with at least one word under rule 2 but still contains text, that document alone is parsed again with `div`, `blockquote`, `section`, `article` and `aside` added to the block elements of rule 2; the result is kept when it holds at least 20 words in total, otherwise the document contributes nothing. This covers producers that set every paragraph in a `div`. Documents that already produce blocks are never re-parsed, so the fallback cannot change the seq of a block in any other document.

### 3.2 Normalization

`words(text)` produces the normalized word list:

1. Apply Unicode NFKC, then full case folding. Implementations MUST use the Unicode Character Database for both and for the categories in step 3; the reference implementation uses the data of the Python it runs on (Unicode 15.1 at the time of writing). Text containing characters added to Unicode after the version an implementation ships may fingerprint differently across implementations; see §11.
2. Delete every **joiner**: `U+002D - U+2010 U+2011 U+0027 ' U+2019 ’ U+00AD U+200D U+2060 U+FEFF`.
   (This makes "wine-glass"/"wineglass", "Good-bye"/"Goodbye" and "don't"/"don’t" equal.)
3. A **word** is a maximal run of characters whose Unicode general category is L\*, M\* or N\*. Everything else separates words.

### 3.3 Fingerprints

`fp(block) = hex(SHA-256(join(" ", first 12 words of block)))[0:16]`, UTF-8 encoded, lowercase hex.

Only a 12-word prefix is hashed, so later edits in the paragraph don't change
the fingerprint, and no book text is distributed.

### 3.4 Edition identity

- `textHash = hex(SHA-256(join(" ", every word of every block in seq order)))`. Identifies an edition's *text*, regardless of metadata, images or styling.
- `fileHash = hex(SHA-256(EPUB file bytes))`. Identifies one exact file.

### 3.5 Anchors

An index's `anchors` array is **ordered**: array order is reading order, and an
anchor's position in the array is its **ordinal**. Each anchor has:

- `id` — opaque string, unique within the index (`[A-Za-z0-9._-]+`). Human-readable ids such as `ch2.para14` (chapter 2, paragraph 14) are RECOMMENDED but carry no meaning. Ids MUST NOT encode pages, screens or any other layout-dependent unit. An anchor is located only by its fingerprint, so it is the same on every device at any font size.
- `fp` — the fingerprint of its block (§3.3).
- `seq` — OPTIONAL block number in the edition marked `builtFrom: true`.

Generators MUST place anchors only on blocks with at least 6 words, and SHOULD
choose blocks whose fingerprint is unique within the edition. An anchor marks
the **end** of its block: anything revealed at that anchor becomes visible
once the reader has finished that block.

### 3.6 Resolving anchors in a client

Given the reader's own edition:

1. Compute `fp` for every block.
2. Walk the anchors in order, keeping `prev` (initially −1). An anchor resolves to the **smallest** block seq greater than `prev` whose fingerprint matches; it then becomes `prev`. If there is none, the anchor is **unresolved**.
3. The **effective location** of an unresolved anchor is the location of the next resolved anchor after it, or +∞ if there is none.

Effective locations never decrease, so the revealed anchors always form a
prefix of the list. A missing or mismatched paragraph can only make the client
reveal *later*, never earlier.

### 3.7 Reader position

A client MUST determine the block the reader is at (§3.8 describes how to get
it from the locator the reading system holds). What the position is depends on
the **mode**:

- `finished` (default): the block the reader is at is **not yet finished**, so the position is the last anchor whose effective location is ≤ that block's seq − 1, or *none* if the reader has not passed the first anchor. Use it for views (§7): "what do I know so far".
- `inclusive`: the block counts, so the position is the last anchor whose effective location is ≤ that block's seq. Use it to look up something the reader is looking at, such as a name tapped on the current page (§8.6).

A client that cannot tell where the current block ends SHOULD use `finished`.

Clients MUST NOT derive positions from percentages of the whole book or from
page numbers. The `progress` locator (§3.8) is a fraction of one document's
words, used only to pick a block inside that document, and is allowed.

### 3.8 Locators

A **locator** is how a reading system says where the reader is. It maps to one
block seq in the reader's own edition (§3.1), which §3.7 turns into a position.
A locator is a JSON object with exactly the members of one form:

| Form | Example | Produced by | Precision |
|---|---|---|---|
| `block` | `{"block": 123}` | clients that already hold the text model | exact |
| `cfi` | `{"cfi": "epubcfi(/6/22!/4/108/1:0)"}` | EPUB readers (Readium, Apple Books, Kobo) | exact |
| `xpointer` | `{"xpointer": "/body/DocFragment[11]/body/div[3]/p[7]/text().0"}` | CREngine (KOReader) | exact |
| `spine` + `word` | `{"spine": 10, "word": 240}` | readers that count words | exact |
| `spine` + `progress` | `{"spine": 10, "progress": 0.42}` | readers that only know a fraction of the document | approximate |

- **block**: the block seq in the reader's own edition.
- **cfi**: an EPUB CFI. The spine step counts every `itemref` of the package spine, including `linear="no"` ones, so the document is the spine item at step `n` divided by 2 (1-based), if its `itemref` is linear. The path after `!` walks element children of the document root: an even step selects the element child at that 1-based even index, an odd step a text node. `:n` is a character offset. `[id]` assertions do not take part in walking the path, but a client MAY use one to find its element directly: when the element named by the last id assertion on the path differs from the element the steps reach, the asserted element wins, because an id survives re-flow and edits that renumber children. The reference implementation and the conformance cases follow this rule.
- **xpointer**: CREngine syntax. `DocFragment[n]` is the 1-based index among all spine items, linear or not. The inner path uses element names with 1-based indices among same-named siblings, and `text().n` is a character offset.
- **spine** and **word**: `spine` is the 0-based index of a linear spine document, in the §3.1 order. `word` is a 0-based index among the §3.2 words of that document's blocks, in order.
- **spine** and **progress**: the same document, and `progress` is a number from 0 to 1, the fraction of that document's words. The result is the block whose cumulative word range contains the fraction, rounding down. It is always approximate.

**Mapping rules.** The result is the seq of the block the locator points into.

1. A CFI or XPointer resolves to an element or text node. The block is the innermost block element (§3.1 rule 2, or rule 6 for a re-parsed document) that contains the first text at or after that point inside it.
2. A target that lies between blocks, or before the first block of its document, maps to the nearest **preceding** block, never the next. A wrong guess MUST under-reveal.
3. A target before the first block of the edition maps to *none* (−1).
4. A word offset past the end of a document clamps to that document's last block. A character offset past the end of its text maps to the same block.
5. A malformed locator, a locator with members of more than one form, or a document that does not exist (or is not linear where the form requires it) is an **error**; clients MUST NOT guess.
6. If the document is found but the inner path does not resolve, the result is the block before that document's first block, and the result is marked **approximate**. A server reports it as `"exact": false` in the `locator` object of its response (§8.4).

**Worked example.** Document 10 holds blocks 200 to 230, so its first block is
seq 200. `{"spine": 10, "word": 240}` falls in the word range of block 207, so
the result is 207. In `finished` mode the position is the last anchor at or
before seq 206. In `inclusive` mode it is the last anchor at or before seq
207. If block 207 carries an anchor, a name lookup there sees what that block
reveals, and the view does not.

## 4. Index document

Media type `application/vnd.lorekeeper.index+json`; file extension `.lore.json`.
Normative schema: [`schema/index.schema.json`](schema/index.schema.json).

```jsonc
{
  "lorekeeper": "0.1",
  "kind": "index",
  "id": "sherlock-holmes.study-in-scarlet",      // registry-unique slug
  "work": { "title": "...", "authors": ["..."], "language": "en",
            "ids": { "openlibrary": "...", "wikidata": "Q...", "isbn13": ["..."], "gutenberg": "244" } },
  "series": { "id": "sherlock-holmes", "book": "study-in-scarlet" },   // optional
  "editions": [{ "textHash": "...", "fileHash": "...", "label": "...", "builtFrom": true }],
  "coverage": { "complete": false, "through": "ch2.para51" },
  "provenance": { ... },
  "anchors": [ { "id": "ch1.para01", "fp": "ab12...", "seq": 31 }, ... ],
  "entities": [ ... ], "relations": [ ... ], "events": [ ... ], "recaps": [ ... ],
  "carried": [ ... ]                                                    // optional, §5.4
}
```

### 4.1 Gated items

Every name, facet, relation, event and recap has a `from` anchor: the point at
which it becomes visible. Facets, relations, events and recaps also carry:

- `evidence` — one or more anchors whose blocks support the item. Every evidence anchor MUST have an ordinal ≤ that of `from`.
- `basis` (facets and relations) — `stated` if the text says it outright, `inferred` if a reader would reasonably conclude it from the cited text.

Item `id`s (facets, relations, events, recaps) MUST be unique across the whole
series, or within the index if it has no series. A `<book>.<entity>.<n>`
pattern is RECOMMENDED.

### 4.2 Entities

```jsonc
{ "id": "holmes", "type": "person", "from": "ch1.para14",
  "names":  [{ "value": "Sherlock Holmes", "kind": "primary", "from": "ch1.para14" },
             { "value": "Holmes", "kind": "alias", "from": "ch1.para27" }],
  "facets": [{ "id": "sis.holmes.8", "from": "ch2.para05", "basis": "stated", "evidence": ["ch2.para05"],
               "text": "His profession is a mystery to Watson..." }] }
```

- `id` — a slug, stable for the entity across the whole series.
- `type` — `person`, `creature`, `place`, `group`, `object`, `concept` or `term`. A `creature` is an animal, monster or kind of being (a pet owl, a dragon, trolls as a kind); a non-human who speaks and acts as a character (a ghost, a centaur, a talking cat) is a `person`. Clients map these to their own labels (e.g. "Cast", "World", "Lexicon").
- `from` — the entity's first appearance in this book. It MUST equal its earliest name's `from`, unless the entity was introduced in an earlier series book.
- `names[].kind` — `primary`, `alias`, `title`, `nickname`, or `descriptive` (a generator-supplied label for something unnamed, e.g. "the retired Marine sergeant"). The display name is the **latest visible `primary`** name (so "Baker Street" can become "221B Baker Street", or "Strider" become "Aragorn").
- `facets` — short descriptions of what the entity **is**: who or what it is, its role, appearance, nature, abilities, allegiances, and changes in its status. Each is true *as the reader understands things at its `from`* (§6.2). Happenings belong in `events`, not facets.
- `facets[].kind` — optional. `"profile"` marks a self-contained description of the entity as the reader knows it at `from` (who they are, what they look like, what they are to the story), written to be read on its own. Each new profile SHOULD list the previous current profile in `replaces`, including one from an earlier series book. Facets without `kind` are details.

### 4.3 Relations

`{ id, src, dst, label, from, basis, evidence, replaces? }`. `label` reads
src → dst ("Murray" — *orderly who saved the life of* → "Watson"). Both
entities MUST be visible at or before the relation's `from`.

### 4.4 Events

`{ id, from, summary, entities[], evidence }`: key happenings in reading order.

### 4.5 Recaps

`{ id, from, text, evidence, scope?, title? }`. `scope` is one of:

- `"story"` (the default): the story so far, as of `from`. Clients show the
  latest visible story recap.
- `"chapter"`: what happens in the text since the previous chapter recap,
  revealed at the end of that text. `title` (at most 120 characters) names the
  chapter. A long chapter MAY be summarized in several consecutive parts that
  share a `title`; clients join them into one chapter summary. Clients list
  every visible chapter recap in reading order.

Story recaps are at most 1200 characters, chapter recaps at most 2000.

### 4.6 Replacement

A facet or relation MAY list item ids in `replaces`. Once the replacing item is
visible, the replaced items become **history**: still true as statements about
what the reader once knew, but no longer current. This is how indexes handle:

- misdirection (the reader suspects Snape → the reader learns it was Quirrell),
- refinement ("works for Mrs. Forrester" → "governess to Mrs. Forrester"),
- change over time ("agreed to share rooms" → "shares rooms at 221B").

An item MUST only replace items of the same kind (facet ↔ facet, relation ↔
relation) that are revealed earlier, either in the same book or in an earlier
series book. Because items only ever replace *earlier* items, indexes can be
generated forward through a series without editing earlier books' files.

### 4.7 Coverage

`coverage.through` is the last anchor the index covers. Partial indexes
(`complete: false`) are valid and expected: community work in progress, or
indexes built incrementally. A complete index MUST cover through its last
anchor. Past `through`, a client SHOULD tell the reader the index ends there.

### 4.8 Provenance

`provenance` records `created`/`updated` timestamps, a `license` (SPDX id;
CC0-1.0 or CC-BY-4.0 RECOMMENDED), optional `generator` (`name`, `version`,
`model`, `promptSet`), `contributors` and `review` records
(`unreviewed`/`spot-checked`/`reviewed`, optionally `through` an anchor).

## 5. Series

Media type `application/vnd.lorekeeper.series+json`. Normative schema:
[`schema/series.schema.json`](schema/series.schema.json).

```json
{ "lorekeeper": "0.1", "kind": "series", "id": "sherlock-holmes", "title": "Sherlock Holmes",
  "books": [{ "key": "study-in-scarlet", "title": "A Study in Scarlet" }, ...],
  "orders": { "publication": ["study-in-scarlet", "sign-of-four"] },
  "defaultOrder": "publication" }
```

### 5.1 Reading orders

A series MAY declare several orders (e.g. `publication` and `chronological`).
The reader chooses one, and gating follows it.

### 5.2 Shared entities

Entity ids are scoped to the series. Each book's index lists the entities that
appear in that book, with only that book's names and facets. A client merges
entries with the same id across the books visible to the reader. A book's entry
SHOULD repeat the names used for the entity in that book, so it still reads
well for a reader who has not finished the earlier books.

### 5.3 Dependency rule

An item in book *B* MAY reference (`replaces`, relation endpoints, event
entities) only items and entities from *B* itself or from books that precede
*B* in **every** declared order.

### 5.4 Carried entities (optional)

An index MAY include `carried`: entities as they stood at the end of earlier
books, with names and facets but no anchors, and an `origin` book key. This
lets a single file stand alone. A client MUST use carried entries only when it
does not have the origin book's index, and only when that book counts as
finished (§7.1).

## 6. Content rules

### 6.1 Text only

Every item MUST be supported by the book's own text, cited through `evidence`.
Knowledge from outside the book is out of scope: author interviews, wikis,
adaptations, later books, or a model's prior knowledge. In a series, earlier
books in the dependency order (§5.3) count as "the text".

### 6.2 Belief at position

An item describes what a careful reader **knows or believes at its `from`
anchor**, including beliefs the book later overturns. It MUST NOT be worded
with hindsight: no foreshadowing, no "seemingly", no "at first", no hint of
importance that only later text reveals. Overturned beliefs are handled with
`replaces` (§4.6).

### 6.3 Original wording

- Facets, events, recaps and relation labels MUST NOT contain more than **8 consecutive words** from the book (after normalization, §3.2). Names are exempt.
- Length limits: facet and event 280 characters (a profile facet 800), name 120, relation label 80, story recap 1200, chapter recap 2000.

These limits keep an index a factual reference work, not a reproduction of the
text.

### 6.4 Validation

A conforming validator checks, at minimum:

| Check | Needs book text |
|---|---|
| Schema conformance | no |
| Anchor ids unique; every reference resolves; `seq` hints increasing | no |
| Evidence ordinal ≤ `from` ordinal | no |
| Entity `from` = earliest name; nothing revealed before its entity | no |
| Relation/event entities visible at the item's `from` | no |
| `replaces` targets exist, are the same kind, and are revealed earlier | no (series: needs earlier books) |
| Nothing revealed after `coverage.through` | no |
| Anchors resolve in the edition; anchor blocks have ≥ 6 words; `seq` matches for the `builtFrom` edition | yes |
| No run of more than 8 consecutive words from the book | yes |

Whether the cited text actually *supports* a claim, and whether wording leaks
hindsight, needs a person or an AI reviewer. Because claims carry evidence,
that reviewer reads only the cited blocks.

## 7. Gating algorithm (normative)

Reference implementation: `lorekeeper.gating.view`. Output schema:
[`schema/view.schema.json`](schema/view.schema.json).

### 7.1 Inputs

- The indexes available to the client, and the series manifest if any.
- A reading order (default: `defaultOrder`).
- The position: current book `B` and anchor `A` (or *none*).
- The set of **finished** books. Default: every book before `B` in the chosen order. A client MAY let the reader change this (e.g. a reader who skipped book 2).

### 7.2 Visibility

An item with anchor `from` in book `X` is **visible** if and only if:

- `X` is finished, **or**
- `X` = `B`, `A` is not *none*, and ordinal(`from`) ≤ ordinal(`A`).

Items in any other book are invisible, whatever order they appear in the
manifest. Carried entries (§5.4) are visible when their origin is finished and
that book's index is absent.

### 7.3 Merging

Visible items are ordered by (book's rank in the reading order, anchor
ordinal, file order). Then:

1. **Entities**: visible if any of their per-book entries is visible. Ordered by first visible appearance, then id.
2. **Display name**: the last visible `primary` name; otherwise the first visible name; otherwise the id. **Aliases**: all other visible name values, deduplicated, in order.
3. **Replacement**: `R` = the union of `replaces` over *visible* items. Facets and relations in `R` go to `history` / `relationHistory`; the others are current.
4. **Profile**: the entity's last visible facet with `kind: "profile"`, as `{ id, text, basis, book }`, or null. Across a series this is inherited: a profile from a finished earlier book stays the entity's profile until a later one is visible. Profile facets are not listed in `facets`; earlier visible profiles go to `history`.
5. **Relations** appear only when both endpoints are visible entities.
6. **Recap**: the last visible story recap, or null.
7. **Chapters**: every visible chapter recap, in reading order, as `{ id, title, text, book }`.
8. **Coverage**: `exhausted` is true when the index is partial and ordinal(`A`) ≥ ordinal(`coverage.through`).

### 7.4 Safety requirements

A client MUST NOT:

- display, prefetch into the UI, or search over invisible items;
- reveal an item because of a partial or failed anchor match (§3.6 guarantees this when followed);
- show counts, placeholders or ordering that disclose invisible items (e.g. "3 more facts about Snape").

### 7.5 Server-side gating

A server MAY compute views (§8.4). Its output MUST equal the client algorithm's output.

### 7.6 Presentation guidance (informative)

- Mark `inferred` facets subtly; show `history` collapsed under "Earlier impressions".
- Label merged series content with its book ("From *A Study in Scarlet*").
- Offer a lookup that searches visible names and aliases only.

## 8. Protocol

A read-only HTTP API. JSON bodies, UTF-8. Errors use RFC 9457
`application/problem+json`. Servers SHOULD send CORS headers so browser-based
readers can call them. Response schemas: [`schema/api.schema.json`](schema/api.schema.json).

### 8.1 Discovery — `GET /.well-known/lorekeeper`

```json
{ "lorekeeper": "0.1", "name": "Home library", "api": "/v0",
  "capabilities": ["lookup", "index", "series", "view", "entities"], "auth": { "type": "bearer" } }
```

`auth.type` is `none` or `bearer`. Self-hosted servers SHOULD support bearer
tokens.

### 8.2 Lookup — `GET /v0/lookup`

Query parameters (at least one): `fileHash`, `textHash`, `isbn`, `openlibrary`,
`wikidata`, `gutenberg`. Results are ordered best first. `match` says how the
result matched: `file` (same file), `text` (same text), or `identifier`
(different edition, so anchors resolve by fingerprint only).

```json
{ "results": [{ "id": "sherlock-holmes.study-in-scarlet", "title": "A Study in Scarlet",
                "match": "text", "series": { "id": "sherlock-holmes", "book": "study-in-scarlet" },
                "complete": false, "digest": "sha256:…", "href": "/v0/indexes/sherlock-holmes.study-in-scarlet" }] }
```

### 8.3 Fetch — `GET /v0/indexes/{id}` and `GET /v0/series/{id}`

They return the index and series documents. `ETag` MUST be the `digest` from
lookup, and servers MUST honour `If-None-Match`. Clients SHOULD cache indexes
and gate locally (§7). This keeps reading position on the device and works
offline. `GET /v0/indexes/{id}` and the view endpoint (§8.4) MUST send
`Access-Control-Allow-Origin`, so browser-based readers can call them.

### 8.4 View — `GET /v0/indexes/{id}/view` (capability `view`)

Position parameters: exactly one of

- `anchor` — an anchor id of the index; or
- one locator form (§3.8), as query parameters: `block`, `cfi`, `xpointer`, `spine` with `word`, or `spine` with `progress`.

A locator MUST come with `textHash` or `fileHash`, naming the reader's
edition. The server resolves a locator only against an edition it holds (the
index's `builtFrom` edition, or another edition it stores); for any other
edition it answers `409` with a problem body, and the client falls back to
fingerprinting and local gating (§3.6, §7). Sending both `anchor` and a
locator, or more than one locator form, is `400`.

When a locator was used, the view carries one extra top-level member, `locator`
(absent when `anchor` was given):

```json
"locator": { "position": "ch2.para14", "seq": 207, "basis": "word", "exact": true }
```

`position` is the anchor id the view was gated at, or `null` for *none*. `seq`
is the block the locator mapped to (−1 for none). `basis` names the form used:
`block`, `cfi`, `xpointer`, `word` or `progress`. `exact` is false for
`progress` and when §3.8 rule 6 applied, otherwise true.

Other parameters: `inclusive` (`true` or `false`; default `false`, so the
position mode is `finished`, §3.7); `order` and `finished` (comma-separated
book keys) for series. Returns a view (§7).

This exists for thin clients. It sends the reading position to the server,
which is acceptable for self-hosted servers and a privacy cost elsewhere.

**Exact-edition shortcut.** A self-hosted server that generated an index from
the very file a reader app displays (same `fileHash`) can map EPUB CFIs, or
spine index plus offset, straight to anchors. The client then needs no
fingerprinting at all. A server that offers locators advertises `view-cfi`
when it accepts `cfi` and `xpointer`.

### 8.5 Requests (capability `requests`, optional)

`POST /v0/requests` with `{ "fileHash": "…" }` asks a server to build an index
for a book it holds. The response and job lifecycle are server-defined in v0.

### 8.6 Entity lookup — `GET /v0/indexes/{id}/entities` (capability `entities`)

Answers "I tapped this name; who is it?" without fetching the whole view.

Parameters:

- `name` (required): matches an entity by an exact, case-insensitive comparison with one of its visible names or aliases, or by a unique surname among the visible entities. Names that are not visible at the position never match (§7.4).
- The position parameters of §8.4: `anchor`, or one locator form with `textHash` or `fileHash`; plus `order` and `finished`.
- `inclusive`: default **`true`** here, because the reader is looking at the name (§3.7).

```json
{ "position": "ch2.para14",
  "locator": { "position": "ch2.para14", "seq": 207, "basis": "word", "exact": true },
  "entities": [ { "id": "sherlock-holmes", "type": "person", "name": "Sherlock Holmes",
                  "aliases": [], "facets": [], "history": [], "profile": null,
                  "relations": [], "events": [] } ] }
```

`position` is the anchor id the lookup was gated at, or `null` for *none*.
`entities` holds every match, as the view (§7) would render them at that
position: each element has the members of a view entity, plus `relations` and
`events` listing the visible relations and events that involve it, rendered as
in a view. The array is empty when nothing matches. When a locator was used,
the response also carries the `locator` object of §8.4, absent when `anchor`
was given.
The result MUST equal what filtering the view for the same position gives.

## 9. Embedding in EPUB

An index MAY be shipped inside the EPUB it describes, at
`META-INF/lorekeeper/index.lore.json`, with an optional
`META-INF/lorekeeper/series.json`. Reading systems ignore unknown `META-INF`
files, so this is invisible to readers that don't support Lorekeeper.
Embedding changes `fileHash` but not `textHash`.

## 10. Conformance

- **Index documents** conform if they pass every check in §6.4.
- **Clients** conform if, for every case in [`conformance/`](conformance/), their view equals the expected view (JSON equality, array order significant).
- **Servers** conform if their responses match `schema/api.schema.json`, and their views (if offered) pass the client cases.
- **Locators.** [`conformance/locators/`](conformance/locators/) holds locator cases: an edition, an index, a locator, and the expected seq, whether it is approximate, and the expected `finished` and `inclusive` anchors. A client MUST pass every case for each locator form it supports. A server that accepts a locator form MUST pass the cases for that form.

## 11. Open questions

1. **Hash availability on devices.** Confirm that KOReader's Lua runtime offers SHA-256 cheaply (`ffi/sha2`?) and that Unicode category tests are practical in Lua. If not, consider a simpler word definition.
2. **Block-level drift.** Editions that split or merge paragraphs (e.g. a list printed as one paragraph vs. many) leave anchors unresolved. That is safe but coarse. Should anchors carry a second fingerprint (e.g. the *last* 12 words)?
3. **Mentions.** KOAssistant-style "appearances by chapter" would need per-entity mention anchors. Should they be in the index, or computed on device from names and aliases?
4. **Chapter structure.** Should indexes carry a gated table of contents so clients can label positions ("as of Chapter 7")?
5. **Translations.** A translation is a different text; should it be a different work with linked entity ids?
6. **Signatures.** Registries will need signed indexes and maintainer keys; out of scope for v0.
7. **Non-EPUB formats** (PDF, MOBI/KF8, CBZ): text models for these are undefined.
