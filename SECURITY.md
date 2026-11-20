# Security policy

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** (a private security advisory). Do not open a public issue or discussion. You will get an answer within a week. This is a spare-time project, so fixes follow as fast as the maintainer can verify them.

Only the latest release and `main` are supported.

## What is in scope

- The web app and the read-only `/v0` protocol: injection, path traversal, request forgery, bypassing the sign-in, the Host check or the CSRF header, reading files outside the data folder.
- Handling of untrusted input: EPUB files, index files, and responses from model providers, Book Orbit or a community registry.
- Secrets: anything that leaks provider keys or the Book Orbit refresh token stored in `secrets.json`.
- Spoilers: a way to make a view, protocol response or graph reveal content past the reader's position (SPEC section 7.4).

## What is by design

- **The web app has one admin account.** Sign-in is on by default: the first start prints a generated password (or uses `LOREKEEPER_ADMIN_PASSWORD`) and requires a new one at first sign-in. Sessions are cookies, and failed logins are throttled. `lorekeeper reset-password` needs access to the data folder, by design. With `--no-auth` anyone who can reach the port can use the whole app, so use it only on a trusted machine. Reports that only say "no authentication" with `--no-auth` set are not vulnerabilities. Even with sign-in, keep the port on a trusted network or behind a VPN or reverse proxy.
- **The `/v0` bearer token** (`LOREKEEPER_API_TOKEN`) protects the read-only protocol that reader apps use. It is separate from the web app sign-in.
- **`lorekeeper-data/secrets.json`** holds provider keys in plain text, readable only by the owner (mode 0600). Backups of the data folder contain it.
- **Community indexes are off until you turn them on.** With the feature on, the server makes an outbound request to the community registry (default `https://nightspine.github.io/lorekeeper-registry`) when you open a book, at most once every 10 minutes per book (the answer is cached), carrying a SHA-256 of the book's text, and one more to fetch an index you preview. The browser never contacts the registry. The registry address must be HTTPS; plain HTTP is accepted only for localhost. Everything the registry returns is treated as hostile: 10 second timeout, 6 MB read cap, no redirects to another origin, file links must stay under the registry's own address, JSON size and nesting limits, the downloaded bytes must match the published digest, and the index must pass the schema and validator and name your book's text before it is stored. A community index is read-only in the app and its text is rendered as text only. A builder's attestations are self-reported and are shown as such. Reports about the registry's own content or workflows belong in the registry repository's tracker.
- Book text is sent to the cloud provider you choose when you build with one. That is stated in the build form.
- The Reader's Ask tab sends your question and a selection of index text up to your position (never the book's text) to the cloud provider whose cheap model answers, or to Ollama on your own machine. The route needs a session, keeps no record of questions or answers, and the Reader says where each answer came from.

## Out of scope

Denial of service by an authorised user, vulnerabilities in third-party services, and problems that need a modified local installation.
