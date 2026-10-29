# Releasing

Releases are automated with [release-please](https://github.com/googleapis/release-please). Nobody edits the version or the changelog by hand.

## How it works

1. Every change reaches `main` as a squash-merged pull request. The pull request title is the commit message, and it must follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat: ...`, `fix: ...`, `docs: ...`). The PR title check enforces this.
2. After each push to `main`, release-please opens or updates a single release pull request. It lists the changes since the last release, bumps the version in `pyproject.toml` and adds them to `CHANGELOG.md`.
3. Merging that pull request creates the `vX.Y.Z` tag and the GitHub Release. The tag starts the Docker workflow, which publishes the image.

`feat` raises the minor version, `fix` and `perf` raise the patch version. While the project is 0.x, a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) also raises only the minor version. `docs`, `test`, `ci`, `build`, `refactor` and `chore` do not appear in the changelog and do not trigger a release on their own.

## Cadence

The release pull request accumulates on `main`. Merge it whenever something user-visible is ready, and at least once a month while the project is 0.x and anything has changed. If nothing user-visible changed, skip the month.

## Image tags

Images are published to `ghcr.io/nightspine/lorekeeper` for linux/amd64 and linux/arm64.

| Tag | Meaning |
|---|---|
| `edge` | The latest build of `main`. May contain unreleased changes. |
| `latest` | The newest stable release. Never a pre-release. |
| `1.2.3` | That exact release. |
| `1.2` | The newest patch of that minor release. |
| `1` | The newest release of that major version. Not published for 0.x. |
| `sha-abc1234` | The build of one commit. |

Pin a full version for anything you depend on. Use `edge` only to try what is coming.

## Hotfixes

Fix the problem on `main` first, with a `fix:` pull request, so the fix is never lost. Then merge the release pull request, which will be a patch release if nothing else user-visible is waiting. If `main` holds changes that are not ready to ship, create a branch from the last tag, cherry-pick the fix, tag `vX.Y.Z+1` by hand from that branch and publish a GitHub Release. The tag still starts the Docker workflow.

## Pre-releases

To publish a release candidate, add a commit to the release pull request with a `Release-As: 0.2.0-rc.1` footer, or tag by hand (`v0.2.0-rc.1`). Tags with a hyphen publish `0.2.0-rc.1` and the `sha-` tag, but not `latest`.

## The index format

The index format is versioned by SPEC.md (`"lorekeeper": "0.1"`), separately from the package. While the package is 0.x, a minor bump may break the index format, but only together with a bump of the SPEC version, schemas and conformance cases in the same release. A package release without a SPEC bump never changes what a valid index looks like.

## Notes

- release-please updates `pyproject.toml` but not `uv.lock`, which records the project version. Run `uv lock` after merging a release pull request, in a `chore:` pull request, if you use `uv sync --locked`. The Docker build uses `--frozen` and is not affected.
- Events created with the default `GITHUB_TOKEN` do not start other workflows. For that reason the release workflow builds and pushes the image itself, in the same run, by calling the Docker workflow with the new tag. A tag pushed by hand still starts the Docker workflow normally, and "Run workflow" on a tag rebuilds an image that is missing.
- For the same reason, the release pull request gets no CI run by default. Two ways to handle it:
  - Add a fine-grained personal access token as the repository secret `RELEASE_TOKEN` (contents: write, pull requests: write). release-please then uses it, and CI runs on the release pull request.
  - Without the token, the maintainer merges the release pull request with the admin bypass. This is safe because that pull request only changes `CHANGELOG.md`, the manifest and the version in `pyproject.toml`.
