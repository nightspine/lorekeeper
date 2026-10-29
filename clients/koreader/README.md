# Lorekeeper for KOReader

A spoiler-safe companion for the EPUB you are reading. Select a name to see who it is, or open
"Story so far". Everything shown is limited to what you have read. There is no AI in the plugin: it
asks your own Lorekeeper server, which holds pre-built indexes (see [SPEC.md](../../SPEC.md)).

This is a **thin client**: the server works out where you are and what you may see.

## Install

1. Copy the folder `lorekeeper.koplugin` into KOReader's `plugins/` directory.
2. Restart KOReader.
3. Open the top menu, then the tools menu (the "more tools" entry), then **Lorekeeper**.
4. Set the **server address** (for example `http://192.168.1.10:8765`) and, if the server asks for one,
   the **API token**. Both are saved in KOReader's settings folder as `lorekeeper.lua`.

The server must hold an index for your book and the same EPUB file (or one with the same text).

## Using it

On opening an EPUB the plugin works out which index belongs to it, once, when the device is already
online. If the server has none, you get one message: "No Lorekeeper index for this book on ...".

**Who is this?** Select a word or name, then tap **Who is this?** in the selection menu. You see the
entry's name and type, what is known so far, and its connections as sentences ("Gabriel de Leon killed
Forever King"). Names the story has not revealed yet return "Nothing known about X yet at this point in
the book."

**Story so far** (Lorekeeper menu) shows the recap up to your current page and how many characters you
know so far. The **Cast** button lists the entries visible at that point, one line each.

## What is sent

All requests are `GET`. A token, when set, goes in an `Authorization: Bearer` header.

| When | Request |
|---|---|
| Once per server | `/.well-known/lorekeeper` (public, no token) |
| Opening a book, or "Look up this book again" | `/v0/lookup?fileHash=<sha256 of the EPUB>`, then `?isbn=<ISBN from the metadata>` if the file hash matches nothing |
| Who is this? | `/v0/indexes/{id}/entities?name=<selected text>&xpointer=<start of the selection>&fileHash=<sha256>` |
| Who is this? (only if the entry has connections) | `/v0/indexes/{id}/view?xpointer=<same>&fileHash=<sha256>&inclusive=true`, to turn ids in connections into names |
| Story so far | `/v0/indexes/{id}/view?xpointer=<start of the current page>&fileHash=<sha256>` |

## Spoiler safety

- Nothing is shown that the server did not return for the position sent with that request.
- Answers are not cached. Each action asks again, so turning pages never reveals old answers early.
- Positions are CREngine XPointers only, never percentages or page numbers (SPEC section 3.7).
- "Story so far" uses the start of the page, so a paragraph you are still in counts as unfinished.

## Privacy

Your reading position, the selected text and the book's file hash are sent to the server you
configure, and only to it. Use a server you run yourself. The plugin stores no reading history.

## Limits of v1

- **The server must hold your edition.** Otherwise it answers `409` and the plugin says so. There is
  no text model on the device, so it cannot look a book up by text hash or map positions itself.
- No offline mode. Each action needs the server to be reachable.
- EPUB only (KOReader's reflowable engine). PDFs and other formats are ignored.
- Hashing a large EPUB the first time takes a moment on slow devices. The result is remembered.
- Written without a device to test on: expect rough edges in the dialogs until it has been tried.

## Roadmap

A "fat" client: fetch the index once, resolve anchors against the local book and gate in Lua
(SPEC section 11). Reading position then never leaves the device and the plugin works offline. It will
need to pass the conformance suites in `conformance/`.

## Development

`uv run pytest tests/test_koreader_plugin.py` checks Lua syntax (when `lua` or `luac` is installed),
the plugin metadata, and that every route and query parameter the plugin uses exists in
`src/lorekeeper/web/protocol.py`. `lorekeeper_format.lua` is plain Lua and is run by the tests.
