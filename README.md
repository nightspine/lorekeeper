# Lorekeeper

[![CI](https://img.shields.io/github/actions/workflow/status/nightspine/lorekeeper/ci.yml?branch=main&label=CI)](https://github.com/nightspine/lorekeeper/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nightspine/lorekeeper?include_prereleases)](https://github.com/nightspine/lorekeeper/releases)
[![Image](https://img.shields.io/badge/ghcr.io-nightspine%2Florekeeper-blue)](https://github.com/nightspine/lorekeeper/pkgs/container/lorekeeper)

An open format and protocol for **spoiler-safe book companions**. Lorekeeper
lists characters, places, terms, relationships, events and recaps, and an ebook
reader shows them only up to where you are in the book.

Indexes are built once (by AI, people, or both) and shared. Reader apps just
fetch and filter them: no AI, no tokens, works offline.

**Status:** v0.1 draft. Read [SPEC.md](SPEC.md).

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
| [`conformance/`](conformance/) | Positions plus the exact view every client must produce |
| Locators | How a reader's EPUB CFI, KOReader XPointer or spine offset becomes a block and a position: SPEC §3.8, `lorekeeper locate` |

## Quick start

The documentation is hosted at https://nightspine.github.io/lorekeeper/ (docs at `/docs/`). Using a coding agent? Point your coding agent at [AGENTS.md](AGENTS.md).

With Docker, no checkout needed. Add this service to your own compose file, or save it as `compose.yaml` and run `docker compose up -d`, then open http://localhost:8765. Use the tag `edge` instead of `latest` for the newest build of `main`.

```yaml
services:
  lorekeeper:
    image: ghcr.io/nightspine/lorekeeper:latest
    ports:
      - "8765:8765"
    volumes:
      - lorekeeper-data:/data
    environment:
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
mkdir -p corpus/pg244 corpus/pg2097 corpus/se-scarlet
curl -sL -o corpus/pg244/pg244.epub   https://www.gutenberg.org/cache/epub/244/pg244.epub
curl -sL -o corpus/pg2097/pg2097.epub https://www.gutenberg.org/cache/epub/2097/pg2097.epub
curl -sL -o corpus/se-scarlet/scarlet.epub \
  "https://standardebooks.org/ebooks/arthur-conan-doyle/a-study-in-scarlet/downloads/arthur-conan-doyle_a-study-in-scarlet.epub?source=download"

uv run pytest
uv run lorekeeper validate examples/sherlock-holmes/*-*.json --series examples/sherlock-holmes/series.json
uv run lorekeeper view examples/sherlock-holmes/study-in-scarlet.json \
  --series examples/sherlock-holmes/series.json --book study-in-scarlet --anchor ch2.para21
```

## Web UI

With Docker, the server, docs and example library come as one image, and your data stays in a volume:

```sh
docker compose up -d        # http://localhost:8765
```

Set `LOREKEEPER_API_TOKEN` and any cloud model keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`) in the environment (or an `.env` file) if you want them, or enter keys under Settings; the compose file points at an Ollama on the host. See the self-hosting guide for the options. Without Docker:

```sh
uv sync --extra web --extra claude
uv run --extra web --extra claude lorekeeper web        # http://127.0.0.1:8765
```

- **Library:** every book from your Book Orbit server (sign in under Settings) and your uploads, by series, with each book's index status, versions and a default version. Warns before you build a book whose earlier series books have no index, and can queue a whole series in order, each book building on the ones before it.
- **Runs:** upload EPUBs, pick sections, model, chunk size and fact-checking, then follow the run live: progress, phase, discoveries, recaps, rejected claims and a filterable log. Runs can be stopped between chunks and resumed.
- **Reader:** open any index (generated, examples, `bench/runs`) and drag through the book. Only what has been revealed at that point is shown. It supports series, chapter jumps, step-by-reveal and deep links.

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
