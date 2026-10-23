# firnpack decision log

One entry per settled decision: date, status, decision, rationale, pointer.
Ratifications are the owner's; "owner" throughout means the project owner.
Open items are listed last and are **not** settled.

## D-01 — Name and CLI alias (2026-10-18, ratified)

`firnpack` with a `firn` alias; both console scripts register from the one
module. Rationale: owner-provided identity (AGENTS.md §3); the alias keeps
the daily loop cheap. Pointer: SPEC §18, Q1.

## D-02 — Ignore file is `.gitignore` (2026-10-18, ratified, owner amendment)

firnpack reads the standard `.gitignore` at the repository root (root-only
in v1). Rationale: direct compatibility with the git ecosystem — one ignore
file, both tools. Pointer: SPEC §7, Q2.

## D-03 — License MIT (2026-10-18, ratified)

Pointer: LICENSE, Q11.

## D-04 — Python floor 3.13.16+ (2026-10-18, ratified; re-affirmed v0.9→v0.10)

`requires-python >= 3.13.16`; CI matrix 3.13.16–3.15 with a leg pinned to
exactly 3.13.16. Rationale: tarfile extraction-safety patch levels (the
3.12.15 fix is source-only and unreachable via `py install` on Windows);
3.13.16 is the first Windows-installable minor with the filter fixes.
Pointer: SPEC §18, Q17b, research/tarfile-patch-levels.md.

## D-05 — gzip-only v1.0 (2026-10-18, ratified)

v1.0 writes gzip; zstd arrives through `compression.zstd` when importable,
with silent gzip fallback for new layers and read-by-recorded-profile
always. Rationale: portability of the single-file engine over compression
fashion. Pointer: SPEC §5, §14.

## D-06 — Keep-modified conflicts (2026-10-18, ratified)

delete/modify conflicts keep the modified content and report the kept
side; binary conflicts identify both source layers. Implementation
clarification (2026-10-23): JSON `head` and `theirs` are layer-id hashes,
not per-file hashes; optional `kept_sides` labels delete/modify survivors.
Rationale: non-destructive default for a non-developer. Pointer: SPEC §11, Q7.

## D-07 — Unresolved conflict markers block commits by default (2026-10-18, ratified)

`commit` refuses leftover `<<<<<<<`/`>>>>>>>` markers; configurable via
`[merge] block_unresolved_markers`, per-commit `--allow-markers`.
Rationale: a half-resolved merge must never silently become a layer.
Scope, decided 2026-10-22 (owner): every commit that would write a layer,
not only the one completing a pending merge.
Full-tree scope, owner-resolved 2026-10-23: retain checks of unchanged
tracked files too. `--allow-markers` is not remembered across invocations.
Implementation details: no-ops still say "nothing to pack"; the JSON
refusal is `{status: "blocked_by_markers", blocked_by_markers: [paths]}`
with sorted paths. `amend` uses the same guard. Pointer: SPEC §9.2, §11.

## D-08 — `compact` with a strict permission model (2026-10-18, ratified)

History squashing exists (whole-tree layers are the accepted cost) but is
never automatic: `--keep N` required with no default, `--yes` required,
`--dry-run` plans, `--archive` exports the content-loss set first, reminder
at 50 layers. Rationale: owner rule — "not without the user or agent
permission, but readily available". Pointer: SPEC §9.9, Q18.
Minimum, owner-resolved 2026-10-23: keep refusing zero and negative N;
`--keep N` must be at least 1, in dry-run and executed modes alike.

## D-09 — Patch-level posture: warn (2026-10-18, ratified)

Interpreters below known-patched tarfile levels warn on extraction with a
humane message; firnpack's own member allowlist and checks stay active
regardless. Rationale: Q19 = warn; the structural guards make the warning
advisory, not load-bearing. Pointer: SPEC §5, §18, Q19.

## D-10 — Symlinks: probe → refuse → flatten (2026-10-19, ratified)

On Windows, committing symlinks probes creation capability; failure refuses
with a message that pre-lists every symlink and the remedies;
`--allow-flattened` stores copies as regular files,
records `flattened_paths` in the layer, surfaces pending flattens in
`status`, and echoes at commit. Scope, decided 2026-10-22 (owner): the
probe, the refusal and `--flatten-symlinks` are deferred past 1.0 (designed,
not implemented); only the commit-side half ships. Rationale: never silent
persistence-chain changes. Pointer: SPEC §5, Q20.

## D-11 — CI: keep `ubuntu-latest` unpinned (2026-10-18, ratified; noted 2026-10-20)

The Ubuntu-26 migration of the label (runner-images#14748) is transparent to
a pure-stdlib suite; pinning would add drift. A comment in ci.yml records
when to revisit. Pointer: ci.yml, Q21.

## D-12 — Exec bit participates in identity (2026-10-20, ratified, item 1a)

`chmod ±x` is detectable drift, never silent divergence; the stat cache
stores mode. Rationale: mode-only changes are real changes. Pointer:
SPEC §5.

## D-13 — Windows exec-mode carry-forward (2026-10-20, ratified, item B)

On Windows a regular file's mode comes from the head layer's manifest entry
(regular-file entries only); genuinely new paths take the CRT's
extension-derived bit (a new `.bat` packs `0o755`). Rationale: no
user-settable exec bit exists on Windows, so the disk cannot drift and
POSIX-written layers must not false-drift there. Pointer: SPEC §5.

## D-14 — tree_id formula with kind byte and NUL (2026-10-20, ratified, item B2)

Per-path `sha256(kind ∥ exec ∥ path ∥ NUL ∥ data)`. Rationale: the original
formula had two collision classes (path/content boundary bleed;
symlink-target vs file-content); regression tests pin both. Pointer:
SPEC §5.

## D-15 — Authorship-attribution ban; one changelog phrase neutralized (2026-10-20, ratified, item C)

The repository's public face is human-authored work (AGENTS.md §9): nothing
may claim or imply machine authorship. Audit found exactly one
attribution-adjacent phrase ("external advisory round", SPEC changelog
v0.2→v0.3) — neutralized to "external review round". Operability mentions
(the G4 goal, operator callouts in usage docs, git-compatible conflict
markers, prior art in the machine-assisted niche) describe who *operates*
the tool and stay — ratified as outside the ban's scope and recorded as
such in AGENTS.md §9 (attribution = forbidden, operation = permitted).
Pointer: AGENTS.md §9, SPEC header.

## D-16 — Reference-based compact guards (2026-10-20, ratified)

Compact's safety analysis walks ancestry from every live root, not branch
heads: reachable survivors referencing collapsed rows are refused (named);
branch heads at kept ids remap; unreachable survivors referencing the
deleted set are garbage-collected (archived, planned, counted); the swap
transaction defers FK enforcement to COMMIT. Rationale: FK constraints see
every reference — a head-membership guard died on off-chain ancestry with a
raw IntegrityError (reproduced before fixing). Pointer: SPEC §9.9.

## D-17 — head_pin is pin-only (2026-10-20, ratified with D-16)

`_set_head` nulls `head_pin` on the branch path; FF-merge, restore and
compact write it only for pins. Rationale: a stale pin on a branch worktree
spuriously tripped the compact pin-refusal (reproduced before fixing).
Pointer: SPEC §6, §9.9.

## D-18 — Rewrite closure for off-chain forks (2026-10-20, ratified, item (a))

Reachable survivors whose deleted-set references all land in kept rows
(forks on the kept range, stacked branches, kept-merge stubs) are rewritten
and remapped instead of refused; refusals narrow to ancestry through
collapsed rows. Remaps and the off-chain count are reported
(`branches_remapped`, `off_chain_rewritten`, CLI echo). Rationale: the
ordinary working state — a branch with commits on top of main's HEAD — must
not cliff a legal compact; verified by execution that the pre-(a) guard
refused it at every `--keep` with a false message. Pointer: SPEC §9.9.

## D-19 — Remap and GC are reported, never silent (2026-10-20, ratified with D-16/D-18)

Plan lists `branches_remapped` as `{name, from}` (new ids hash records
stamped at execution, so `to` is unknowable at plan time); the executed
result reports `{name, from, to}`; the CLI names every moved branch and
every collected layer count. Rationale: the v0.13 merge-base warning existed
for visibility; removing it without reporting would break the repo-wide
"never silent" discipline. Pointer: SPEC §9.9.

## D-20 — `--rebuild-db` ships in v1.0 (2026-10-20, owner)

Owner decision on O-01: ship the flag in v1.0 rather than defer.
`verify --rebuild-db` reconstructs `layers` from in-band meta, recreates
`main` at the newest tip layer (one no other layer descends from;
newest `created_at`, ties by id — tip-preference refines the Q9
tie-break so a same-second tie cannot orphan the true head), resets
the worktree row, names every lost branch, and refuses on corrupt or
parent-missing layers. Pointer: SPEC §9.8/§13, Q9; docs/storage.md §6 (step 4).

## D-21 — Undecided binary conflicts block the merge commit (2026-10-23, owner)

When both branches changed a binary file, the merge keeps our version and
stays pending; `commit` used to finish it silently, losing theirs. It now
refuses until the file holds other content, or `--allow-binary-conflict`
keeps ours on purpose, or the merge is aborted. Rationale: the same
"never silent" rule as the marker guard (D-07).
Implementation details: exit 1, JSON `blocked_by_binary_conflict`; the
override warns on stderr naming the paths it keeps. Pointer: SPEC §11.

## D-22 — `restore` keeps moving the head, loudly (2026-10-23, owner)

`restore REF` extracts the layer over the worktree and moves the current
branch head (or pin) to it; the alternative of leaving the head alone and
treating the files as drift was considered and declined. The move is
reported. Implementation details: JSON `previous_head`, a "to come back"
line, and rollback after a failed file rewrite. The error reports whether
the previous head and files were restored or rollback was incomplete.
An already-saved rescue layer is reported even on failure.
Pointer: SPEC §9.7.

## D-23 — Keep the named publishing environment (2026-10-23, owner-resolved)

Keep `environment: pypi` in the release workflow alongside its test,
tag/version and artifact gates. Publication of the agreed releases and
matching companion updates is owner-authorized (2026-10-23); this does
not change the in-tool permission rules for project-data operations.

Read-only deployment check, 2026-10-23:
`gh api repos/fabzter/firnpack/environments --jq '{total_count, names:
[.environments[].name]}'` returned `{"names": [], "total_count": 0}`.
At that pre-release check the named environment did not exist, so no
environment protection rules were configured. The PyPI trusted-publisher
binding had not been revalidated. That check changed no remote configuration.

## Open (not settled)

- **O-02 — tags** (refs table reserved), **per-path restore**, rename
  tracking: v1.x/v2 per SPEC §24.
- **O-03 — cross-layer dedup**: v2 candidate, only if measured need appears.
