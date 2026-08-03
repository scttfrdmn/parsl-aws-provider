# Parsl Ephemeral Provider Documentation

Sphinx sources for the project documentation. This file is not part of the built
site — it is excluded in `conf.py`.

## Building

```bash
uv sync --extra dev --extra docs
uv run make -C docs html
open docs/_build/html/index.html
```

The build is warning-free and CI runs it with `-W`, so a new warning fails the
`docs` job. That is deliberate: the tree previously carried 398 warnings, which
hid ~65 toctree entries pointing at pages nobody had written (#124).

## Structure

- `conf.py` — Sphinx configuration; `release` is read from the installed package
  rather than hardcoded
- `index.rst` — landing page and the only toctree
- `api_reference.rst` — autodoc over the live modules
- `*.md` — the content pages, rendered through MyST

Every page in `index.rst`'s toctree exists, and every configuration example is
checked against the real `EphemeralProvider` signature by
`tests/unit/test_docs_examples.py`. The provider rejects unknown keyword
arguments (#105), so a stale option in an example is a crash, not a nitpick — add
new options to the tests along with the docs.

## Contributing

1. Edit the relevant page.
2. Run `uv run make -C docs html` and confirm zero warnings.
3. Run `uv run pytest tests/unit/test_docs_examples.py` if you changed a
   configuration example.
4. Open a pull request.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
