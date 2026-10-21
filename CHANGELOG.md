# Changelog

## v0.1.1 (2026-10-22)

- Send X-Frame-Options and CSP frame-ancestors headers so the page cannot be framed.
- Follow redirects only to http and https.
- README: setup for Linux, macOS, and Windows.
- ruff clean; add a CI workflow.

## v0.1.0 (2026-10-21)

First tagged release. Self-hosted uptime monitor for a few URLs.

- Add nabd, a self-hosted uptime monitor for a few URLs.
- Document the nabd status page and one real check row.
- Reject non-local Host headers, cap intervals, prune old checks
