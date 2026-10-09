# Repository instructions

- This repository packages a macOS workflow for official ChatGPT login plus an existing API gateway. Preserve the user's model, provider, history, and unrelated settings.
- Never commit real credentials, OAuth files, browser data, phone numbers, verification codes, personal configuration, or runtime backups.
- Runtime state belongs outside this repository in a private directory. Public examples use placeholders only.
- Use the existing installed OpenCodex management API. Do not patch application binaries or bypass authentication checks.
- Run `python3 -m unittest discover -s tests -v` and `python3 scripts/hybrid_remote.py --help` before publishing changes.
- `verify` makes two small billable model requests. Unit tests use synthetic credentials and local mocks only.
- Mobile pairing is performed in the official desktop app; completion requires a real mobile-originated task. Do not describe CLI smoke tests as mobile verification.
