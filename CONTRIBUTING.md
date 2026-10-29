# Contributing

Thanks for looking at Lorekeeper. The specification is the product, and the Python code exists to make it testable. Read the README and [CLAUDE.md](CLAUDE.md) (which describes the architecture) before a larger change.

## Setup

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/) and, for the JavaScript checks, Node 22.

```sh
uv sync --all-extras --dev
uv run pytest -q
uv run lorekeeper conformance
node tests/js/run_conformance.mjs
```

Tests that need a book skip when `corpus/` is empty. The README lists the commands that download the Project Gutenberg and Standard Ebooks files. Run them, because a green run without the corpus does not cover anchor resolution or quote checks.

## Changing the specification

The spec, the schemas and the code move together, in the same pull request.

1. Edit `SPEC.md` and the matching files in `schema/`.
2. Change the reference code (`src/lorekeeper/`) and the JavaScript port in `src/lorekeeper/web/static/js/` so they agree.
3. If gating output changes, regenerate the expected views with `uv run lorekeeper conformance --update`, read the diff, and keep the spoiler properties asserted in `tests/test_gating.py`.
4. Update the docs pages in `src/lorekeeper/web/static/docs/` and the examples, and run `uv run lorekeeper validate` on them.
5. A change that makes a valid index invalid, or the reverse, needs a SPEC version bump.

A client that implements a part of the spec must pass the conformance suite for that part.

## Commits and pull requests

- Pull requests are squash-merged and the **pull request title becomes the commit message**. It must follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `perf:`, `docs:`, `refactor:`, `test:`, `build:`, `ci:`, `chore:`. A check enforces it. The release process reads these to decide the next version, see [RELEASING.md](RELEASING.md).
- Keep a pull request to one change. Say what changed and why.
- Do not add `Co-authored-by`, "Generated with" or any other attribution trailer, to commits or to the description. Authorship is the person who opened the pull request and answers for it.

## AI-assisted contributions

Lorekeeper is itself built with AI, and AI assistance is welcome. Unreviewed AI output is not. The rules are the same for everyone.

- You are responsible for every line you submit. You must be able to explain it and defend it in review.
- Read the whole diff before you open the pull request. Remove generated comments, dead code, speculative abstractions and changes you did not intend.
- Run the tests and conformance suites yourself. For anything that touches generation, gating, anchors, the Reader or the protocol, also run it on a real book and say which one.
- The pull request description is written by you. It must say what was tested, how, and on which book. A description that restates the diff, or claims verification that did not happen, will be closed.
- Do not paste model output into issues or reviews as if it were your finding. Reproduce the problem first.
- No fabricated references: every function, flag, spec section and URL you mention must exist.

Pull requests and issues that look generated and unverified are closed without a detailed review. This keeps the maintainer's limited free time for work that has been checked.

## Issues

Search first. For a bug, include the version or image tag, how you run Lorekeeper, the book (or a public-domain one that reproduces it), and the output. For an idea, describe the problem before the solution. Questions belong in Discussions.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your code is licensed under Apache-2.0 and your changes to the specification text under CC BY 4.0, as described in the README.
