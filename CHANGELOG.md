# Changelog

Hand-maintained (SPEC §24). Semver; releases on `v*` tags.

## 1.0.0 — 2026-10-20

First stable release: the complete v1.0 command surface, single-module
pure-stdlib engine, tested on Linux, Windows and macOS (Python
3.13.16–3.15).

### Commands

- **Core**: `init`, `commit`, `status`, `history`, `diff`.
- **Branching**: `branch` (incl. from a specific layer), `switch`
  (branch or pinned layer), `list-branches`, `rename-branch`,
  `delete-branch`, `merge` — 3-way with durable pending state on
  conflict, fast-forward, and `--abort` with drift rescue.
- **Safety**: `amend`, `restore` (rescue-first: uncommitted work is
  packed before anything is overwritten; `--to DIR` extracts without
  touching the worktree), `export` (fresh-directory extraction),
  `verify` (self-hash every layer, cross-check the database),
  `verify --rebuild-db` (reconstruct the database from in-band layer
  meta, naming what cannot be recovered).
- **History management**: `compact --keep N` — permission-gated squash
  with dry-run planning, archive-before-delete, branch remapping,
  garbage collection of unreachable layers, and named refusals.

### Storage and safety properties

- One compressed, checksummed, immutable layer file per commit;
  repository file count is N_commits + 2.
- Single SQLite metadata file; `synchronous=FULL`, one transaction per
  command; crash windows leave either the old state or a sweepable
  orphan, never a half-write.
- Layer names are their own hashes (`layer_id` over a canonical commit
  record; `tree_id` over kind∥exec∥path∥NUL∥data per entry).
- Extraction guards: structural member allowlist, path validation
  (traversal, device names, Windows-unsafe names), symlink probe →
  refuse → explicit flatten with a persistence chain.
- `.gitignore`-compatible ignore rules (root-only in v1); ignored files
  counted and listable, never silently hidden.
- Windows exec-mode carry-forward; exec bit participates in identity.

### Packaging

- One module (`firnpack.py`), zero runtime dependencies; `firnpack` and
  `firn` console scripts; universal wheel guarded by CI to contain
  exactly the module plus dist-info.
- PyPI publication via trusted publishing (OIDC) from the `v*` tag
  workflow; no API tokens.
