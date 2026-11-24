# Lorekeeper

[![CI](https://img.shields.io/github/actions/workflow/status/nightspine/lorekeeper/ci.yml?branch=main&label=CI)](https://github.com/nightspine/lorekeeper/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nightspine/lorekeeper?include_prereleases)](https://github.com/nightspine/lorekeeper/releases)
[![Image](https://img.shields.io/badge/ghcr.io-nightspine%2Florekeeper-blue)](https://github.com/nightspine/lorekeeper/pkgs/container/lorekeeper)

An open format and protocol for **spoiler-safe book companions**. Lorekeeper
lists characters, places, terms, relationships, events and recaps, and an ebook
reader shows them only up to where you are in the book.

Indexes are built once (by AI, people, or both) and shared. Reader apps just
fetch and filter them: no AI, no tokens, works offline.

**Status:** v0.1 draft. Read [SPEC.md](SPEC.md). What comes next is in [ROADMAP.md](ROADMAP.md); first up is a manual index editor so a person can write, correct and delete what the model produced.

**Releases:** a release is cut whenever something user-visible is ready, at least monthly while the project is 0.x. Docker images are published to `ghcr.io/nightspine/lorekeeper` for amd64 and arm64: `latest` is the newest release, `1.2.3` and `1.2` pin one, and `edge` is the latest build of `main`. See [RELEASING.md](RELEASING.md) and the [changelog](CHANGELOG.md).

## How this project is made

Lorekeeper is a fully AI-made project. The maintainer is a full-time software engineer who works on it in his free time, and the code, the spec text and the docs are written with AI assistance. AI slop is not permitted. Proper AI usage and hygiene is enforced in the development process: every change is reviewed, tested, and verified on real books before it lands, and contributions that look generated and unverified are closed. The rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

**Documentation:** a developer guide for reader apps, the protocol reference and the spec are served by the web UI at `/docs/` (`lorekeeper web`), and `lorekeeper docs export DIR` writes the same site for any static host.

**Website:** the landing page lives in [`site/`](site/) and is exported with the docs (`lorekeeper docs export DIR` puts `index.html` at the root), so one static folder is the whole public site.

## What's here

| Path | What |
|---|---|
| [`SPEC.md`](SPEC.md) | The specification: text model and anchors, index format, series, content rules, gating algorithm, HTTP protocol |
| [`schema/`](schema/) | JSON Schemas: index, series, view, API responses |
| [`src/lorekeeper/`](src/lorekeeper/) | Python reference implementation: EPUB text model, fingerprints, anchor resolution, gating, validator, CLI |
| [`examples/sherlock-holmes/`](examples/sherlock-holmes/) | Hand-written example indexes for the first two chapters of *A Study in Scarlet* and *The Sign of the Four* |
| [`examples/dracula/`](examples/dracula/) | A complete index of *Dracula* (Project Gutenberg #345) built by Lorekeeper with Claude Haiku and spot-checked by hand: the book the hosted demo and a fresh install open with |
| [`conformance/`](conformance/) | Positions plus the exact view every client must produce |
| Locators | How a reader's EPUB CFI, KOReader XPointer or spine offset becomes a block and a position: SPEC §3.8, `lorekeeper locate` |

## Quick start

The documentation is hosted at https://nightspine.github.io/lorekeeper/ (docs at `/docs/`). Using a coding agent? Point your coding agent at [AGENTS.md](AGENTS.md).

With Docker, no checkout needed. Add this service to your own compose file, or save it as `compose.yaml` and run `docker compose up -d`, then open http://localhost:8765. Sign in as `admin` with the password from `docker compose logs lorekeeper` (or set `LOREKEEPER_ADMIN_PASSWORD` first), and choose a new one. Use the tag `edge` instead of `latest` for the newest build of `main`.

```yaml
services:
  lorekeeper:
    image: ghcr.io/nightspine/lorekeeper:latest
    ports:
      - "8765:8765"
    volumes:
      - lorekeeper-data:/data
    environment:
      LOREKEEPER_ADMIN_PASSWORD: ${LOREKEEPER_ADMIN_PASSWORD:-}
      LOREKEEPER_API_TOKEN: ${LOREKEEPER_API_TOKEN:-}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      LOREKEEPER_OLLAMA_URL: http://host.docker.internal:11434
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

volumes:
  lorekeeper-data:
```

To work on the code and the spec:

```sh
uv sync

# The examples are anchored to Project Gutenberg EPUBs; tests that need them skip if missing.
mkdir -p corpus/pg244 corpus/pg2097 corpus/pg345 corpus/se-scarlet
curl -sL -o corpus/pg244/pg244.epub   https://www.gutenberg.org/cache/epub/244/pg244.epub
curl -sL -o corpus/pg2097/pg2097.epub https://www.gutenberg.org/cache/epub/2097/pg2097.epub
curl -sL -o corpus/pg345/pg345.epub   https://www.gutenberg.org/cache/epub/345/pg345.epub
curl -sL -o corpus/se-scarlet/scarlet.epub \
  "https://standardebooks.org/ebooks/arthur-conan-doyle/a-study-in-scarlet/downloads/arthur-conan-doyle_a-study-in-scarlet.epub?source=download"

uv run pytest
uv run lorekeeper validate examples/sherlock-holmes/*-*.json --series examples/sherlock-holmes/series.json
uv run lorekeeper view examples/sherlock-holmes/study-in-scarlet.json \
  --series examples/sherlock-holmes/series.json --book study-in-scarlet --anchor ch2.para21

# Share an index: one bundle file, filed in a registry, built into static lookup files.
uv run lorekeeper share examples/dracula/dracula.json -o dracula.share.json
uv run lorekeeper registry add dracula.share.json --publisher mina-m --source registry/
uv run lorekeeper registry build registry/ -o build/registry/ --base-path /lorekeeper-registry
```

The [share guide](https://nightspine.github.io/lorekeeper/docs/share.html) covers the bundle, the checks a registry runs and the files `registry build` writes.

## Web UI

With Docker, the server, docs and example library come as one image, and your data stays in a volume:

```sh
docker compose up -d        # http://localhost:8765
```

The web app asks for a sign-in: the first start creates `admin` and prints its password once (`docker compose logs lorekeeper` in Docker), and you choose your own at first sign-in. `lorekeeper reset-password` sets a new one, and `lorekeeper web --no-auth` skips sign-in for trusted local use only. Set `LOREKEEPER_API_TOKEN` (for the `/v0` protocol) and any cloud model keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`) in the environment (or an `.env` file) if you want them, or enter keys under Settings; the compose file points at an Ollama on the host. See the self-hosting guide for the options. Without Docker:

```sh
uv sync --extra web --extra claude
uv run --extra web --extra claude lorekeeper web        # http://127.0.0.1:8765
```

- **Library:** every book from your Book Orbit server (sign in under Settings) and your uploads, by series, with each book's index status, versions and a default version. Warns before you build a book whose earlier series books have no index, and can queue a whole series in order, each book building on the ones before it.
- **Runs:** upload EPUBs, pick sections, model, part size (or whole chapters) and fact-checking, then follow the run live: progress, phase, discoveries, recaps, rejected claims and a filterable log. Runs can be stopped between chunks and resumed.
- **Reader:** open any index (generated, examples, `bench/runs`) and drag through the book. Only what has been revealed at that point is shown. It supports series, chapter jumps, step-by-reveal and deep links. In a series, an entry first met in an earlier book says so, and entries that haven't appeared in the current book yet stay out of the way behind one "From earlier books" chip (search still finds them). The Chapters and Timeline tabs read as a book-by-book timeline: a banner with the cover for each book, then a rail with a dot per chapter you have reached and the five nearest shown before "Show all". The Timeline groups events by chapter, marks the turning points that change the course of the story, keeps small moments behind a chip, and, where the book states it, heads each run of events with the setting they happen in ("A carriage", "The Westenra drawing room"). The position panel names the last known setting in the current chapter. In a series, an entry shows one card per book, and each card opens with a few sentences on what happened to the character in that book so far (a "book arc", written from the index's events at the end of a web build with a cloud key, or on any index with `lorekeeper arcs`; `lorekeeper generate` writes none).
- **Ask:** the Reader's Ask tab answers a question such as "What happened to Aaron in the second book?" from the index at your position, across the finished books of a series. Most answers list the entries, events or recaps they came from. No book text is sent. On one large series a question cost 0.1 to 0.2 cents on Claude Haiku (a small index sends far less), and it is free on Ollama. Nothing is saved. See [Ask about a book](src/lorekeeper/web/static/docs/ask.html).
- **Where it comes up:** an entry's panel charts its mentions chapter by chapter, solid up to your position and counted only through the paragraph you have reached, with the passages themselves one click away. The counts come from the book's text and use only the names you can already see, so a name revealed later is not counted early.
- **Family trees:** when the book states family ties, a chip in the Connections tab draws each family met so far as a tree, using only the ties visible at your position. There is no chip for a book without kinship ties. `lorekeeper ties` tags the ties of older indexes without a rebuild.
- **Editing:** every index built here can be corrected by hand from the Reader's edit mode: rename or retype an entry, rewrite or delete a description, detail, connection, event or recap, move its reveal point by pointing at the paragraph, or write a whole index yourself with no model ("Write it myself" on the build form). Each change can be undone, hindsight and long quotes are refused the way the generator refuses them, and your edits are applied again when the book is rebuilt.
- **Community indexes:** opt in, and a build page offers a ready-made index shared for your exact copy of the book. Try it in the real Reader held to the first chapters, see who checked it and how, and add it with one click. Share your own the other way round: the app checks it, saves a bundle and opens the submission form. Off by default; see the [guide](https://nightspine.github.io/lorekeeper/docs/community.html).

The Reader's spoiler filter is a JavaScript port of the reference algorithm (`src/lorekeeper/web/static/js/gating.js`), checked against the same conformance suite.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, in particular the rules for AI-assisted changes. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

The code is licensed under the [Apache License 2.0](LICENSE). The specification text ([SPEC.md](SPEC.md), the schemas and the conformance cases) is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Prior art

Lorekeeper borrows its ideas about what a reader wants from an X-Ray from the
KOReader plugin [KOAssistant](https://github.com/zeeyado/koassistant.koplugin),
which builds them per device with AI. Its anchoring approach follows W3C Web
Annotation text-quote selectors. Its sharing model is inspired by community
registries such as OpenSubtitles and SponsorBlock.
