# Changelog

Hand-maintained (SPEC §24). Semver; releases on `v*` tags.

## 1.0.3 — 2026-11-11

### Fixed

- `merge` of two disconnected histories (for example an amended root layer
  beside the original root) now refuses with a plain exit-1 message naming
  the missing common ancestor, instead of a false exit-3
  "integrity failure: missing layer file: None.firn" on a repository that
  verifies clean. Exit 3 stays reserved for corruption.
- The history reminder re-fires after a `compact` once history regrows past
  `[history] reminder_threshold`: a successful compact resets the crossing
  baseline instead of silencing the reminder for the repository's life.
- `verify` now performs the two SPEC §9.8 comparisons it documented but
  skipped: the in-band `layer_id` field against the layer filename, and the
  stored `profile` column against the in-band profile. A forged in-band
  `layer_id` no longer passes a green verify.
- `merge`'s own pending-merge refusal now carries the normative recognition
  string "merge is pending", matching every other fence refusal.
- `history` tips, `compact`'s `branches_remapped` and `maxpath_warning` are
  lexicographically sorted, as the agent contract promises for arrays.
- Extraction commands (`export`, `restore`, `switch`, `merge`) carry the
  tarfile patch-level warning in their `--json` payload as
  `tarfile_patch_warning`, not only on stderr.

### Changed — documentation truthfulness

- README Status names the shipped 1.0.2 release; the file-count invariant is
  restated in layers (amend and rescue layers add layers until `compact`
  reclaims them); the power-cut sentence is a design claim verified under
  injected I/O failures, not a physical power-loss guarantee.
- storage.md §3 and decisions D-05 state the ratified gzip-only v1.0 codec
  truth; SPEC §5 layer 1 describes the extraction mechanism that actually
  runs (firnpack never calls `tarfile.extractall`) and records the 1.0.2
  directory-link typing rule with its 63-follow bound (decisions D-24).
- SPEC §9.9 drops the never-implemented interactive TTY prompt and documents
  the compact reminder reset; SPEC §10 specifies the disconnected-roots
  refusal as step 4.
- usage.md and agent-contract.md document `flattened_pending` as
  objects-or-null, add the disconnected-histories refusal class, and document
  the conditional `tarfile_patch_warning` key.
- The sdist now ships `docs/real-world-results.json`; the release sdist guard
  and the MANIFEST guard both name it.
- research/packaging-ci.md states that the version lives in two
  gate-enforced places rather than a single source.

### Verification

- Nine new regressions (disconnected merge, reminder re-fire, in-band
  `layer_id`/`profile` tampering, machine-readable patch warning, sorted
  tips/remapped/maxpath) failed on 1.0.2 before these fixes; seven previously
  unpinned JSON arms are now pinned in the contract suite.

## 1.0.2 — 2026-10-25

### Fixed

- Clean tracked file↔directory transitions now work through `restore` and
  `switch`. Worktree writes preflight every target and ancestor before
  deleting anything; ignored paths and symlink obstructions are refused
  with a path-specific diagnostic instead of silently overwritten.
- Deletion decisions use the previous layer's manifest and current
  visibility, not a possibly missing or stale stat cache. Restore can
  replace a new visible obstruction only after saving it in a rescue
  layer; unrelated new files and ignored content remain untouched.
- Failed head-moving writes attempt to restore both selection and files,
  including cleaning up newly created directories. Errors distinguish
  successful rollback from partial recovery. An exclusive-create collision
  preserves the occupying entry rather than deleting it during rollback.
  A failed merge abort keeps the merge pending and reports its saved
  rescue layer for recovery.
- Worktree materialization uses binary-mode exclusive writes on Windows,
  preserving LF, CRLF and arbitrary binary bytes without CRT translation.
- Forward directory symlinks receive an explicit directory type on
  Windows, inferred from the complete snapshot before materialization.
  Export, worktree restore/switch and merge share the correction, including
  link chains and directory aliases; stored targets and layer format stay
  unchanged.
- Supported names that cannot fit a USTAR name/prefix split use a
  deterministic PAX `path` record. Short, Unicode and splittable names
  retain their existing USTAR bytes. Unsupported symbolic-link targets
  and non-UTF-8 filesystem names receive humane refusals, not tracebacks.
- Both layer writers finish TAR/gzip trailers before syncing the file,
  and clean up their temporary file after a caught write failure.
- `compact --archive` exclusively creates its output and selects a
  collision suffix instead of truncating an earlier archive, file or
  symlink target. Failed archive writes clean up only their owned output;
  the completed ZIP is closed and synced before history is rewritten.
  If a later rewrite fails, the error also names the completed archive.
- Archive destinations inside `.firn`, including symlink and filesystem
  aliases, are refused before any mutation, even for a preview or no-op.
  Space checks include archive bytes and the archive destination.
- `amend` enforces commit's existing 255-character stored-path limit and
  emits the same optional `maxpath_warning` for long full paths.
- `rename-branch` refuses a missing source and applies branch creation's
  existing name constraints before changing any branch.
- Slashed ignore rules keep working on Python 3.14, whose public
  `fnmatch.translate` output uses a different end-of-string anchor.

### Verification

- Added 48 chained CLI journeys for daily editing, branches and pins,
  merge resolution/abort, copy/rebuild/corruption recovery, explicit
  history compaction, nested-directory commands, and POSIX symlinks and
  executable modes, with independent file, archive and metadata checks.
- Added worktree and storage regressions for ignored obstructions,
  rollback failures, deterministic archive collisions, encoding and
  path boundaries, and injected I/O failures. See
  [the verification report](docs/real-world-verification.md) for target
  identities, initial failures, corrected oracles and test limits.

## 1.0.1 — 2026-10-23

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
