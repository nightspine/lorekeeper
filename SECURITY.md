# Security policy

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** (a private security advisory). Do not open a public issue or discussion. You will get an answer within a week. This is a spare-time project, so fixes follow as fast as the maintainer can verify them.

Only the latest release and `main` are supported.

## What is in scope

- The web app and the read-only `/v0` protocol: injection, path traversal, request forgery, bypassing the Host check or the CSRF header, reading files outside the data folder.
- Handling of untrusted input: EPUB files, index files, and responses from model providers or Book Orbit.
- Secrets: anything that leaks provider keys or the Book Orbit refresh token stored in `secrets.json`.
- Spoilers: a way to make a view, protocol response or graph reveal content past the reader's position (SPEC section 7.4).

## What is by design

- **The web app has no login.** When the server binds to a non-local address, as the Docker image does, anyone who can reach the port can use the whole app, including building books and changing settings. Put it behind a VPN, Tailscale or an authenticating reverse proxy. Reports that only say "no authentication" are not vulnerabilities.
- **The `/v0` bearer token** (`LOREKEEPER_API_TOKEN`) protects only the read-only protocol, not the web app.
- **`lorekeeper-data/secrets.json`** holds provider keys in plain text, readable only by the owner (mode 0600). Backups of the data folder contain it.
- Book text is sent to the cloud provider you choose when you build with one. That is stated in the build form.

## Out of scope

Denial of service by an authorised user, vulnerabilities in third-party services, and problems that need a modified local installation.
