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

delete/modify conflicts keep the modified content and report; binary
conflicts report both tree-side hashes. Rationale: non-destructive default
for a non-developer. Pointer: SPEC §11, Q7.

## D-07 — Unresolved conflict markers block commits by default (2026-10-18, ratified)

`commit` refuses leftover `<<<<<<<`/`>>>>>>>` markers; configurable via
`[merge] block_unresolved_markers`, per-commit `--allow-markers`.
Rationale: a half-resolved merge must never silently become a layer.
Pointer: SPEC §10, v0.7→v0.8.

## D-08 — `compact` with a strict permission model (2026-10-18, ratified)

History squashing exists (whole-tree layers are the accepted cost) but is
never automatic: `--keep N` required with no default, `--yes` required,
`--dry-run` plans, `--archive` exports the content-loss set first, reminder
at 50 layers. Rationale: owner rule — "not without the user or agent
permission, but readily available". Pointer: SPEC §9.9, Q18.

## D-09 — Patch-level posture: warn (2026-10-18, ratified)

Interpreters below known-patched tarfile levels warn on extraction with a
humane message; firnpack's own member allowlist and checks stay active
regardless. Rationale: Q19 = warn; the structural guards make the warning
advisory, not load-bearing. Pointer: SPEC §5, §18, Q19.

## D-10 — Symlinks: probe → refuse → flatten (2026-10-19, ratified)

On Windows, committing symlinks probes creation capability; failure refuses
with a message that pre-lists every symlink and the remedies;
`--allow-flattened`/`--flatten-symlinks` stores copies as regular files,
records `flattened_paths` in the layer, surfaces pending flattens in
`status`, and echoes at commit. Rationale: never silent persistence-chain
changes. Pointer: SPEC §5, Q20.

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

## Open (not settled)

- **O-01 — `--rebuild-db` (SPEC §9.8) specified, not shipped in v1.0.**
  v1.0 recovery is `verify` + the manual re-import recipe in
  `docs/storage.md` §6.4. Target: v1.x. Owner awareness recorded 2026-10-20.
- **O-02 — tags** (refs table reserved), **per-path restore**, rename
  tracking: v1.x/v2 per SPEC §24.
- **O-03 — cross-layer dedup**: v2 candidate, only if measured need appears.
