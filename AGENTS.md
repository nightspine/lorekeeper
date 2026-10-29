# AGENTS.md

Setup and working rules for coding agents and new contributors. The architecture reference is [CLAUDE.md](CLAUDE.md). Despite the name it is plain documentation for any agent.

## What this is

Lorekeeper is a draft open standard for spoiler-gated book indexes (SPEC.md) with a Python reference implementation, a self-hosted web app and a KOReader plugin. The spec is the product. The invariant that matters most: **nothing past the reader's position may ever be revealed**.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.11 or later for you)
- Node 22, only for the JavaScript checks
- Optional: [Ollama](https://ollama.com) for local index generation, or a cloud key such as `ANTHROPIC_API_KEY`. Neither is needed to run the app, the tests or the example library.

Or open the repo in a dev container or Codespace; `.devcontainer/` does the setup below.

## Clone to running app

```sh
uv sync --all-extras --dev
uv run --extra web --extra claude lorekeeper web      # then open http://localhost:8765
```

With no books, the Library opens on the bundled example index; press "Try it now" to open it in the Reader.

## Checks

```sh
uv run pytest -q                       # all tests; none call a model
uv run lorekeeper conformance          # Python gating against conformance/
node tests/js/run_conformance.mjs      # JavaScript gating against the same cases
uv run lorekeeper docs export build    # builds the static docs site into build/ (use --force to overwrite)
```

Tests that need a book skip when `corpus/` is empty. The README lists the commands that download three public-domain EPUBs; run them before touching anchors, quotes, the EPUB parser or the generator.

## Layout

- `SPEC.md`: the normative specification.
- `schema/`: JSON Schemas for indexes, series, views and API responses.
- `conformance/`: positions and the exact views (and locator results) every client must produce.
- `src/lorekeeper/`: reference implementation (EPUB text model, fingerprints, anchors, locators, gating, validation, generation, CLI).
- `src/lorekeeper/web/`: FastAPI app, job queue, read-only `/v0` protocol.
- `src/lorekeeper/web/static/`: no-build vanilla JS frontend, including `gating.js` and `lorekeeper-client.js`, plus the docs site in `docs/`.
- `clients/koreader/`: KOReader plugin (Lua) that talks to `/v0`.
- `examples/`: hand-written Sherlock Holmes indexes anchored to Project Gutenberg.
- `site/`: the public landing page, exported together with the docs.
- `tests/`, `bench/`: tests and benchmarking scripts.

## Rules that matter

- **Spec, schemas and code change together** in one pull request. If a valid index changes, bump the SPEC version. The JavaScript gating must stay a faithful port of `gating.py`.
- **Spoiler safety is the invariant.** Unresolved anchors may only under-reveal. No count, marker or hint past the position may appear in any view, graph or protocol response.
- **Index content is untrusted in the frontend.** Render it with `textContent` (the `h()` helper), never `innerHTML`.
- **Verify generator and checker changes on a real Gutenberg book**, not only with the scripted fake model, and say which book in the pull request.
- **Conformance outputs come from the reference implementation.** After an intended gating change, run `uv run lorekeeper conformance --update`, review the diff, and keep the spoiler properties asserted in `tests/test_gating.py`.
- **Never restart `lorekeeper web` or kill a generate process while a run is active.** Check `/api/jobs` first and abort if anything is running, queued or stopping.
- **Commits:** the pull request title is the commit message, in Conventional Commits form (`feat:`, `fix:`, `docs:`...). Never add `Co-authored-by`, "Generated with" or any other AI attribution trailer anywhere in git history.
- **AI slop is not accepted.** Read your whole diff, run the checks, and write the pull request description yourself. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Where to read more

- [CLAUDE.md](CLAUDE.md): the full architecture, pipeline and conventions.
- [SPEC.md](SPEC.md): the format, gating algorithm and protocol.
- Docs site: served at `http://localhost:8765/docs/` while the app runs, and published at https://nightspine.github.io/lorekeeper/docs/.
- [RELEASING.md](RELEASING.md): versions, image tags and the release flow.

## First contribution

1. Pick an open issue, or open one first for anything larger than a small fix.
2. Branch from `main`, make one focused change, and update the spec, schemas, docs and tests it touches.
3. Run the four checks above, and try the change on a real book if it touches generation, anchors, gating or the Reader.
4. Open a pull request with a conventional title, for example `fix: keep chapter recaps in order`.
5. Fill in the pull request template: what changed, what you ran, on which book, and that spec and docs agree. The description is yours, not generated.
