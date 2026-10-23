# Changelog

Hand-maintained (SPEC §24). Semver; releases on `v*` tags.

## Unreleased

Planned as 1.0.1 (owner decision 2026-10-22). The version is not bumped and
nothing is tagged or published yet.

### Changed behavior

- **Leftover conflict markers block every commit that would write a layer**,
  and `amend` too, not only the commit that completes a merge (owner
  decision 2026-10-22). Override with `--allow-markers` (commit and amend)
  or `[merge] block_unresolved_markers = false`. A commit that would write
  nothing still says "nothing to pack". With `--json` a refusal prints
  `{"status": "blocked_by_markers", "blocked_by_markers": [paths]}`.
  The full-tree scan deliberately rechecks unchanged marker-shaped text:
  the override must be repeated on later commits/amends (owner-confirmed
  2026-10-23).
- **A merge that leaves a binary file undecided no longer finishes
  silently** (owner decision 2026-10-23). `commit` refuses while a binary
  file both branches changed still holds your version; put other content in
  it, pass `--allow-binary-conflict` to keep yours on purpose, or
  `merge --abort`. The override warns on stderr naming the paths kept as
  yours. JSON refusal: `blocked_by_binary_conflict`.
- `restore` reports the head it moved away from (`previous_head` in JSON, a
  "to come back" line in the output). A failed file rewrite attempts to put
  the previous head and files back (also for fast-forward `merge`) and
  reports whether rollback succeeded or was incomplete. A rescue layer
  already saved is named with its export command even on failure.
  Rollback also removes files newly created before the failure, without
  deleting ignored files that the failed rewrite never reached.
- `restore --to` now shares `export`'s missing-or-empty destination guard:
  it can no longer overwrite existing destination files.
- `status` re-hashes while a merge is pending, so refreshed stat-cache
  entries cannot hide files that differ from the head during resolution.
- `compact --keep 0` and negative values remain invalid in every mode;
  N must be at least 1 (owner-confirmed 2026-10-23).
- `verify --rebuild-db` reports `lost_branches: null` (not `[]`) when the old
  database was missing, empty or unreadable.

### Fixed

- A refused `compact` no longer touches `.firn/layers/`: stray leftovers
  stay exactly as they were until the next command that is allowed to
  sweep (`commit`, `amend`, or an executed `compact`).
- The orphan sweep removes any stray `*.tmp` in `layers/`, not only
  `<id>.firn.tmp`, and says so on stderr when a leftover cannot be removed
  (OneDrive or antivirus holding it) instead of silently keeping it.
- A full disk, a read-only folder, a file held by OneDrive or antivirus, a
  busy or damaged database, and an invalid config now produce a humane
  error with the documented exit code (1, or 3 for a damaged database),
  never a traceback. Disk errors point to `verify`; unreadable database
  errors explain the backup-and-rebuild steps; config errors name the file
  or key to fix.
- A history-reminder bookkeeping error can no longer turn a commit that
  landed into a reported failure.
- A refused symbolic link (Windows without Developer Mode) is reported as
  such, not as a disk or OneDrive problem. The designed probe and
  `--flatten-symlinks` are deferred past 1.0 (owner decision 2026-10-22).
- Wrong-typed config values (`reminder_threshold = abc`, a bad boolean)
  stop `commit`, `amend` and `merge` before they change anything, with one
  line naming the key. Inspection, export and verification remain usable.
  Invalid INI syntax or UTF-8 still needs fixing before any command can
  load the config. Values are literal (`%` in an author name is allowed).
  An existing config that cannot be opened is announced before defaults
  are used; `.firn/config` is read as UTF-8.
- The marker scan treats only `\n`, `\r\n` and `\r` as line ends (form feeds,
  NEL and Unicode separators no longer cause false refusals) and sees a
  marker after a UTF-8 byte-order mark.
- A clean merge blocked by marker-shaped text now emits the documented
  refusal object with `--json`; its files and pending merge remain
  available for resolution or abort.
- `firnpack history | head -1` and other closed pipes end quietly.
- Delete/modify conflict reports now identify the kept side in text and
  optional JSON `kept_sides`. Conflict text names both source layer IDs
  and explains binary resolution instead of asking for nonexistent markers.
- `delete-branch` warns when the deleted branch's tip is no longer reachable
  from any branch or from where you are; layer-id prefixes shorter than 6
  hex characters say so instead of "no branch named".
- `commit` refuses a file literally named `__firnpack_meta__`.
- The published 1.0.0 source distribution shipped none of the documents it
  promised; `MANIFEST.in` now adds SPEC, CHANGELOG, `docs/`, `research/`,
  the tests and the workflows. The wheel is unchanged.

### Release automation

- `release.yml` runs the suite before building, checks that the pushed tag
  equals the package version, checks the wheel and the source distribution
  contents, and names the publish environment. `pyproject.toml` gains
  project URLs.

### Documentation

- SPEC, usage, storage, agent-contract and README now match shipped
  behavior: orphan and sweep wording, `--json` scope, the 16-command
  surface (`show` is deferred), the 255-character path refusal, `restore`
  moving the head, the new refusals and flags, and the symlink probe and
  `--flatten-symlinks` marked as deferred past 1.0.

### Tests

- Windows-only exec-bit facts run on the windows-latest CI legs; the
  carry-forward policy is also pinned on POSIX with a discriminating case.
- New suites: marker guard, binary-conflict guard, humane errors, review
  fixes, an independent golden `tree_id`, rescue-path refusals, a 96-cell
  compact matrix checked against an independent oracle, read-side probes
  through the CLI, a stronger compact fuzz, and a real cp1252 run.

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
