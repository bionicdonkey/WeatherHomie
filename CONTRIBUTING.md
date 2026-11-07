# Contributing

Thanks for looking. This is a small self-hosted project, so keep changes focused.

* **Run the tests:** `uv venv --python 3.12 && uv pip install -e '.[dev]' && .venv/bin/pytest` (or `pip install -e '.[dev]'` and
  `pytest`). pySTEPS compiles from source on install, so you need a C compiler.
* **Architecture:** read the README first. Data sources and alert destinations are plugins (one folder each, found
  automatically); settings live in the database and are edited in the UI; secrets are encrypted and write-only. New API routes
  need the right permission (`admin` or `viewer`), and `tests/access/test_roles_tokens.py` fails if one is missing.
* **No external runtime dependencies in the web UI** (no CDN scripts, fonts or images); `tests/core/test_web_assets.py` checks.
* **No personal data in the repo or tests:** use invented names and places and the neutral test location. Real BOM downloads
  go in the git-ignored `tests/fixtures/*/real/` folders (BOM's copyright applies).
* **New data sources:** state the terms of use in the plugin's `licence`, and say so in your pull request if they are unclear.
  A feed that forbids third-party access (Blitzortung, for example) must not be queried directly.
* **Releases:** bump the version in `pyproject.toml` and `src/stormtracker/__init__.py`, then push a tag `vX.Y.Z`. The release
  workflow refuses a tag that doesn't match.

By contributing you agree your work is licensed under the AGPL-3.0, like the rest of the project.
