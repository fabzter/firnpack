# Research: CI/CD & packaging for a zero-dependency single-file Python package

> Compiled 2026-10-18, backing SPEC.md §15/§16.

## Publishing: trusted publishing (OIDC) is the 2025+ default

- Use `pypa/gh-action-pypi-publish@release/v1` with **job-level
  `permissions: id-token: write`**; no username/password, no API tokens in secrets.
  PyPI exchanges the GitHub OIDC token for a short-lived (≤ 15 min) API token.
- Bind the trusted publisher to the exact repo + workflow filename
  (`release.yml`) on PyPI (docs.pypi.org → "Adding a Trusted Publisher"); scope
  tightly — broad trust = credential compromise surface.
- Hardening per PyPI security-model docs: dedicated environment (e.g. `pypi`) with
  protection rules, tag protection on `v*`, lean two-step publish job, job-level
  (not workflow-level) permissions.
- Trigger: push of tags `v*`; build job → artifact upload → publish job
  (packaging.python.org guide pattern).

## Single-module packaging

- `pyproject.toml` only (no setup.py). Build backend: **setuptools** (ubiquitous,
  zero config for `py-modules = ["firnpack"]`) or **hatchling** (cleaner, no
  plugins needed). Either ships a wheel containing exactly one top-level
  `firnpack.py` via `py-modules`.
- `[project.scripts] firnpack = "firnpack:main"` gives the CLI; a second alias
  entry (`firn`) costs one line if wanted.
- Wheel is `py3-none-any` universal — pure Python, no extensions, no data files.
- Runtime dependencies: `dependencies = []` — genuinely empty (stdlib only:
  sqlite3, tarfile, gzip, hashlib, argparse, json, pathlib, configparser,
  difflib, getpass). `compression.zstd` (PEP 784, Python 3.14+) is *optional*:
  import-guarded, gzip fallback — the optional-module caveat is documented in PEP
  784 and CPython docs (distribution-dependent availability).
- Dev tooling (pytest, ruff) lives in the `dev` extra / dependency-group —
  development-only, does not violate zero-runtime-deps.
- sdist: README, LICENSE, tests, docs, research notes. Tests stay out of the wheel
  (dulwich issue #1024 precedent: don't ship test modules in wheels).
- Single-file precedent sanity check: stdlib-era single-file libs (docopt-era
  style) and current PyPI entries (snapback, simple-rcs) prove one-file modules
  remain installable/idiomatic. pyproject-level `py-modules` is the supported
  mechanism.

## Testing matrix

- GitHub Actions matrix: Python **3.13.16–3.15** (synced to the owner's floor
  decision, SPEC §18/§19 — supersedes the earlier 3.10–3.14 draft range),
  `ubuntu-latest` primary; one `macos-latest` + `windows-latest` legs (tar
  extraction semantics, path separators, mtime granularity differ — this is a
  filesystem-heavy tool; Windows is the target OS).
- `pip install -e .[dev]` → `ruff check` + `ruff format --check` → `pytest`.
- Keep the suite fast (< 10 s) so it runs on the target laptop class too.

## Single-file discipline guard

- CI step: build wheel, unzip, assert the file list is exactly
  `['firnpack.py', 'firnpack-<version>.dist-info/...']` — protects the "one file"
  promise (SPEC G7) against accidental package sprawl over time.
- Static guard test in-repo asserting `.github/workflows/ci.yml` and
  `release.yml` exist and reference the supported Python versions (config-drift
  discipline).

## Release checklist (per release)

1. Bump `__version__` (single source: `firnpack.__version__`).
2. Update CHANGELOG.md.
3. Tag `vX.Y.Z` → release.yml builds + publishes via trusted publishing.
4. GitHub Release with notes; skill repo submodule bump follows (repo #2).
