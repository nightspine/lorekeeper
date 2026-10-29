# Security policy

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** (a private security advisory). Do not open a public issue or discussion. You will get an answer within a week. This is a spare-time project, so fixes follow as fast as the maintainer can verify them.

Only the latest release and `main` are supported.

## What is in scope

- The web app and the read-only `/v0` protocol: injection, path traversal, request forgery, bypassing the sign-in, the Host check or the CSRF header, reading files outside the data folder.
- Handling of untrusted input: EPUB files, index files, and responses from model providers or Book Orbit.
- Secrets: anything that leaks provider keys or the Book Orbit refresh token stored in `secrets.json`.
- Spoilers: a way to make a view, protocol response or graph reveal content past the reader's position (SPEC section 7.4).

## What is by design

- **The web app has one admin account.** Sign-in is on by default: the first start prints a generated password (or uses `LOREKEEPER_ADMIN_PASSWORD`) and requires a new one at first sign-in. Sessions are cookies, and failed logins are throttled. `lorekeeper reset-password` needs access to the data folder, by design. With `--no-auth` anyone who can reach the port can use the whole app, so use it only on a trusted machine. Reports that only say "no authentication" with `--no-auth` set are not vulnerabilities. Even with sign-in, keep the port on a trusted network or behind a VPN or reverse proxy.
- **The `/v0` bearer token** (`LOREKEEPER_API_TOKEN`) protects the read-only protocol that reader apps use. It is separate from the web app sign-in.
- **`lorekeeper-data/secrets.json`** holds provider keys in plain text, readable only by the owner (mode 0600). Backups of the data folder contain it.
- Book text is sent to the cloud provider you choose when you build with one. That is stated in the build form.

## Out of scope

Denial of service by an authorised user, vulnerabilities in third-party services, and problems that need a modified local installation.
