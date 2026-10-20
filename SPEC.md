# firnpack — SPEC v0.14 (REVIEW CLOSED 2026-10-18; v0.14 amendments ratified 2026-10-20)

> **Status: REVIEW CLOSED 2026-10-18 (owner sign-off) — approved for
> implementation; v0.14 amendments owner-ratified 2026-10-20.** All §25
> questions owner-resolved; the Q20
> flatten-persistence chain was ratified by the owner 2026-10-19. No
> outstanding items. Companions: `AGENTS.md` (founding context),
> `research/` (web research backing decisions). v0.1→v0.2: self-review fixes
> (dual identity hashes, HEAD-file removal, fast-forward merge, schema
> alignment). v0.2→v0.3: external review round — de-circularized `layer_id`
> (hashes a canonical commit record, never the payload containing it), v1.0
> goes gzip-only (zstd portability), full crash-consistency protocol (fsync
> ordering + Windows/OneDrive replace retry), honest bytes-per-commit budget,
> new blocking Q17 (target machine OS/Python). v0.3→v0.4: merge-state
> blocker fix — `merge` refuses on drift; conflicted merges persist a
> pending-merge state in `worktree` (`merge_theirs`) that `commit` consumes
> into `second_parent` (so theirs-head can never silently drop out of the
> DAG); `switch`/`amend`/`restore` defined mid-merge; `merge --abort` added;
> `status` reports pending merges; regression tests pinned. v0.4→v0.5: owner
> inputs — target machine confirmed **Windows 11** (Q17a resolved); Python
> floor re-opened with corrected facts (Q17b): the v0.5/v0.6 rationale
> ("3.10/3.11 need hand-rolled extraction-safety code") was **false** — PEP 706
> data-filter is security-backported to 3.10.12+/3.11.4+ and detected via
> `hasattr(tarfile, 'data_filter')` (PEP 721, pip#14241). Real trade is only
> interpreter support status + a version guard; symlinks stay supported on
> every floor option; the hard constraint is the laptop's actual interpreter
> (owner to check `py --version`). CI matrix includes 3.15 (current stable,
> 2026-10-09); EOL facts precise; pending-merge crash ordering (DB-first);
> commit-time marker warning; Windows MAX_PATH validation.
>
> v0.7→v0.8: owner review round 2026-10-18 — unresolved-marker guard flips to
> **blocking by default** (owner decision; configurable via `[merge]
> block_unresolved_markers`, per-commit `--allow-markers` override);
`merge --abort` exclusivity ratified; DB-first crash ordering ratified;
> **Python floor = 3.12** (Q17b resolved, owner decision); config example
> corrected to gzip; marker scan pattern unified (`^<<<<<<<` / `^>>>>>>>`;
> `=======` excluded — false-positive-prone).
>
> v0.8→v0.9: tarfile security facts corrected (verified vs python.org):
> **3.12.15 (2026-09-30, source-only) fixes three tarfile filter bypasses**
> (CVE-2026-82049 / -19672 / -87910); **last 3.12.x Windows installer was
> 3.12.10** — 3.12.11+ are source-only, so `py install` cannot reach a
> patched 3.12 on Windows; `hasattr(data_filter)` cannot detect these (bugs
> in the filter, present everywhere). Consequences: structural member
> allowlist added at pack+extract time (hardlink-class bypasses impossible in
> our layers); runtime patch-level warning on extraction below known-patched
> levels; **Q17b re-opened for owner re-affirmation** (new facts post-date
> the decision); new Q19 (warn vs refuse posture); recommended laptop
> interpreter updated to 3.14.8+/3.15.
>
> v0.9→v0.10: owner decisions round 2 (2026-10-18): **floor = 3.13.16+**
> (Q17b final); Q19 = **warn**; Q1 `firn` alias **yes**; Q2 ignore file
> **named `.gitignore`** (owner amendment — full git compatibility); Q7
> keep-modified; Q11 **MIT**; Q14 `agent-skills-firnpack`; Q18 budget
> accepted → new owner requirement: **`compact` command** (§9.9 — squash
> old layers, explicit permission only, reminder at threshold).
> 3.14.8 patch status corrected to **UNCONFIRMED** (python.org 3.14.8 list
> omits CVE-2026-87910; 3.14 backport closed 2026-10-01, one day after the
> release; wolfi's patched 3.14.8 rebuild dated 2026-10-08 corroborates);
> 3.15.0 **CONFIRMED** (gh-157265 in RC3). Per-minor warning table replaces
> the single-tuple gate; Windows-safe member-name validation (backslashes,
> drive/UNC prefixes, reserved device names) as an all-or-nothing pre-pass;
> symlink-on-Windows policy (probe + refuse + explicit `--flatten-symlinks`)
> added as new Q20.
>
> v0.10→v0.11: compaction corrected as a **history rewrite**: ancestor-
> closure refusal conditions pinned (no branch head, pin, or pending
> merge may reach the collapsed set); `--keep` confirmed required with
> **no default and no config default** (permission rule); crash ordering
> + two crash-window tests; §6 "no GC ever" now names compact as the sole
> explicit exception; nested `.gitignore` (root-only, v1) + status
> visibility of ignored files (`--ignored`) after the silent-exclusion
> finding; seed set narrowed to `~$*` only; §18 clarifies the pip gate
> makes the patch warning relevant mainly to the ungated skill copy.
>
> v0.11→v0.12: §2's "no reachability GC" line now names compact as the
> exception (§6 was fixed in v0.11, §2 was missed — an implementer
> following §2 would have skipped the sweep); §9.9 dry-run now reports
> **peak disk need** (old-total + N+1 layer writes happen *before* old
> files are deleted) and compact **refuses up front** when free space is
> below the peak, with the humane message; crash-window test wording
> normalized. (Two apparent v0.11 verification failures were check-string
> artifacts — content was correct.)
>
> v0.12→v0.13: **REVIEW CLOSED (owner sign-off 2026-10-18)** — Q20 ratified
> with the agent-continuation amendment (refusal message pre-lists every
> symlink + remedies; `flattened` arrays in both output modes); full
> housekeeping batch accepted (Q3–Q15); post-close: Q16 and Q21 ratified
> by owner 2026-10-18 (benchmark honesty; keep ubuntu-latest); keystone
> 3.13.16 page fetched first-party and promoted to Tier-1 evidence;
> flattened-symlinks persistence chain (status shows pending flattens,
> commit echoes + records them, `--allow-flattened` gate — "never silent"
> now survives one command further); ci.yml floor leg pinned to exactly
> `3.13.16`; container decision ratified; research/packaging-ci.md
> matrix synced to 3.13.16–3.15.
>
> v0.13→v0.14: M2 implementation round — owner decisions 2026-10-20:
> **B ratified** — Windows exec-mode carry-forward (§5 canonical TAR
> rules: on Windows a regular file's mode is taken from the head
> layer's manifest entry, regular-file head entries only; genuinely new
> paths pack `0o644`). **B2 ratified** — amended `tree_id` formula (§5:
> per-path `sha256(kind ∥ exec ∥ path ∥ NUL ∥ data)`; the kind byte and
> NUL separator close the two collision classes of the original 1a
> formula — path/content boundary bleed and symlink-target vs
> file-content — with regression tests pinning both). **§9.9 refusal
> conditions re-specified as reference-based** (owner-ratified):
> ancestry refusals (a branch cut from inside the collapsed range and
> committed on is refused by reachability analysis, not head
> membership), **remap** for branch heads sitting at kept layers (they
> follow the rewrite; the v0.13 "loses merge base" warning case no
> longer exists), **garbage collection** of unreachable survivors that
> reference the deleted set (amend orphans, second-parent stubs of
> collapsed merges — archived, planned as `layers_garbage_collected`,
> reported in the result), and deferred FK enforcement in the swap
> transaction (consistency checked at COMMIT against the guard-proven
> final state). Changelog wording normalized (v0.2→v0.3 entry).

---

## Table of contents

1. [Summary & goals](#1-summary--goals)
2. [Users & non-goals](#2-users--non-goals)
3. [Core concept: the glacier model](#3-core-concept-the-glacier-model)
4. [On-disk repository layout](#4-on-disk-repository-layout)
5. [Layer format v1](#5-layer-format-v1)
6. [The manifest (glacier.db)](#6-the-manifest-glacierdb)
7. [Config file](#7-config-file)
8. [Command set](#8-command-set)
9. [Semantics: the operations](#9-semantics-the-operations)
10. [Merge algorithm](#10-merge-algorithm)
11. [Merge conflict surface](#11-merge-conflict-surface)
12. [Status & change detection](#12-status--change-detection)
13. [Robustness: fail-safe design](#13-robustness-fail-safe-design)
14. [Compression profiles](#14-compression-profiles)
15. [Alternative storage designs rejected](#15-alternative-storage-designs-rejected)
16. [CLI & exit codes](#16-cli--exit-codes)
17. [Python API](#17-python-api)
18. [Packaging & distribution](#18-packaging--distribution)
19. [CI/CD](#19-cicd)
20. [Repository documentation model](#20-repository-documentation-model)
21. [The agent-skill repository (repo #2)](#21-the-agent-skill-repository-repo-2)
22. [Testing strategy](#23-testing-strategy)
23. [Performance targets](#23-performance-targets)
24. [Roadmap & versioning](#24-roadmap--versioning)
25. [Open questions for the owner](#25-open-questions-for-the-owner)
26. [Appendix A: requirements traceability](#appendix-a-requirements-traceability)
27. [Appendix B: research citations](#appendix-b-research-citations)

---

## 1. Summary & goals

**firnpack** is a single-user, offline-first version control system for
non-developers on unreliable hardware. Every commit is a **layer** — one
compressed, checksummed, immutable archive file — and history is the ordered stack
of layers. A repository is a small directory holding exactly **2 files at rest +
1 file per commit**:

- `glacier.db` — the manifest: a tiny SQLite database (layers, branches, worktree
  state, config),
- `config` — human-editable INI (author name, compression profile),
- `layers/<layer_id>.firn` — one packed snapshot per commit.

**Primary goals**

- **G1 — I/O-friendly storage.** No sprawling object stores. A repo with 100
  commits holds exactly 102 files (file count is O(commits) — bounded, pinned,
  test-asserted); a commit adds one new sequential file plus one small DB
  rewrite. Honest cost: each layer stores the **whole compressed tree**, so the
  price of history is bytes-per-commit, not file count — a ~10 MB mixed-text
  tree packs to roughly 2–6 MB per layer (gzip-6), so 100 commits ≈ 0.2–0.6 GB.
  Estimate, to be measured and confirmed (Q17, Q18).
- **G2 — Correctness over cleverness.** Whole-file snapshots per layer. No delta
  chains, no patch algebra. A layer can always be verified and extracted
  standalone.
- **G3 — Non-developer UX.** Git-adjacent vocabulary (commit/branch/merge)
  without the footguns (no index, no detached HEAD warnings, no force-anything).
  Humane error messages; auto-generated commit messages when `-m` is omitted.
- **G4 — LLM-agent operability.** Every command supports `--json`; the skill file
  (repo #2) documents the full behavioral contract.
- **G5 — Zero external runtime dependencies.** Stdlib only: `sqlite3`, `tarfile`,
  `gzip`, `hashlib`, `argparse`, `json`, `pathlib`, `configparser`, `difflib`,
  `getpass`. zstd arrives via `compression.zstd` (Python 3.14+, optional stdlib
  module) with gzip fallback.
- **G6 — Published to PyPI** via trusted-publishing CI from a public GitHub repo.
- **G7 — Single-file implementation.** `firnpack.py` is one file; the wheel
  installs exactly one module; CI guards that invariant.

**Success criteria** (provisional — owner validates in review)

- A 1000-file project with 100 commits leaves ≤ 102 files under `.firn/`;
  `history`/`status`/`diff` in < 1 s on a weak laptop (status with warm stat
  cache).
- Every layer passes checksum self-verification on `verify`.
- A repo folder can be zipped, moved to another machine, unzipped, and just works.

## 2. Users & non-goals

**The one user profile** (from AGENTS.md): non-developer, VS Code + Copilot
(DeepSeek), old laptop running **Windows 11** (upgraded from Windows 10), slow
HDD, OneDrive churning I/O, smallish personal projects, no collaboration.

**Non-goals** (v1, explicit)

- No remotes, push/pull, server protocol, or hosting. "Decentralized-ish" = the
  repo is a self-contained folder — copy it and you own all history.
- No multi-user concurrency, locking, or per-user identity. Commits record a
  configurable author string; no email in v1.
- No staging area or partial commits.
- No rebase, cherry-pick, submodules, tags-as-commands (schema-reserved only),
  worktrees, or remotes.
- No large-file optimization, no cross-layer delta/dedup (v2+ question).
- No xattr/hardlink preservation. Symlinks are stored (tar-native, safety-filtered
  on extract) and the executable bit is preserved; other permission bits are
  normalized (§5).

**What the non-goals buy us**: no reachability GC (sole exception:
`compact`, §9.9 — explicit permission only), no reflogs, no packed refs,
no negotiation protocols. 5 DB tables, 1 archive format, 1 mutable file.
This is the simplicity budget.

## 3. Core concept: the glacier model

A firnpack repository is a **glacier**: commits are **layers**, stacked in commit
order. Each layer is self-contained, compressed, checksummed, immutable. Drill a
core (log / show / restore) and read history stratum by stratum.

| firnpack term | git analog | meaning |
|---|---|---|
| layer | commit | one packed snapshot of the tracked tree |
| glacier | repository | the whole history + manifest |
| head | HEAD | current branch or pinned commit |
| branch | branch | a named line of history |
| merge | merge | union two branches at a common ancestor |
| drift | dirty working copy | changes not yet packed |
| packing | committing | turning drift into a layer |

Terminology stays 1:1 with git where the concept is identical (commit, branch,
merge, history, amend) and adds glacier metaphors only where they *explain* the
storage model to a non-developer (layer, drift, packing).

## 4. On-disk repository layout

```
<project>/
├── .firn/                     ← the glacier
│   ├── glacier.db             ← SQLite manifest (the ONLY mutable file at rest)
│   ├── config                 ← human-editable INI
│   └── layers/
│       ├── <layer_id>.firn    ← one immutable file per commit
│       └── ...
└── (working files)
```

**Invariants**

- Each layer file's name **is** the `layer_id`: SHA-256 (first 20 bytes, 40 hex
  chars) of the layer's full canonical pre-compression payload (§5).
  Self-certifying: `verify` = recompute + compare.
- `glacier.db` is the only file ever rewritten. All repo state — including the
  current head — lives in it, so every command is a single atomic SQLite
  transaction. (No `HEAD` sidecar file: a second mutable file could disagree with
  the DB after a crash; git needs it, we don't.)
- SQLite settings: `journal_mode=DELETE` (no WAL sidecar files at rest),
  `synchronous=FULL`, `foreign_keys=ON`. Durability over speed — the HDD machine
  is the fragile thing we protect. A transient `-journal` file exists only mid-
  write.
- Nothing under `.firn/` is modified in place except `glacier.db`; layers are
  write-once.
- `.firn` is the fixed repo directory name (like `.git`), auto-ignored by
  tracking.
- `.gitignore` at the project root, if present, is *tracked* like any other
  file (owner decision Q2: firnpack uses the git filename itself — one
  ignore file, both tools). `.firn/` and `.git/` are always auto-ignored.

**Why SQLite for the manifest** (research/prior-art.md): Fossil stores an entire
VCS in SQLite and has survived ~19 years of real use as a single-file DB; our DB
stays tiny because content lives in layers.

**Why not SQLite for content too**: content must be independent of the DB engine
(DB b-tree splits fragment on HDD; recovery shouldn't require SQLite), and layers
keep heavy bytes in append-friendly whole files. A corrupted DB is rebuildable
from layers alone (§13).

## 5. Layer format v1

A layer is a canonical TAR stream, compressed through an explicit pipeline. It
contains, in this order:

1. `__firnpack_meta__` — first member: JSON, UTF-8, sorted keys, trailing
   newline. Fields: `layer_id`, `tree_id`, `parent`, `second_parent` (nullable),
   `message`, `author`, `created_at` (UTC ISO-8601), `profile` (§14),
   `format_version` (1), and `flattened_paths` (optional; present only on
   layers committed from a flatten — the §5 persistence chain record).
2. Every tracked file, lexicographic by POSIX path.

**Canonical TAR rules**

- Members ordered: meta first, then paths sorted lexicographically (by UTF-8
  bytes).
- mtime, uid, gid, uname, gname are zeroed; mode normalized: `0o644` for files,
  `0o755` if the source file's execute bit is set, `0o777` for symlinks (tar
  convention). Identity comes from content, not clock or owner.
- **Windows exec-mode carry-forward (owner decision 2026-10-20, item B)**:
  Windows has no user-settable execute bit, so on Windows the mode of a
  regular file is taken from the head layer's stored manifest entry
  (carry-forward) instead of the disk — and only when that entry is
  itself a regular file (a symlink head entry never donates its `0o777`).
  The disk value cannot drift there (nothing can change it), and layers
  written on POSIX must not false-drift when the repo is used from
  Windows: a repo copied between machines keeps its exec bits exactly.
  Genuinely new paths (no head entry) pack as `0o644`. Symlinks are
  unaffected (`0o777` always).
- pax headers: none (no long-path pax records for paths ≤ 100 chars; longer
  paths use ustar prefix splitting; beyond that, pax `path` record only —
  deterministic per path).

**Two hashes** (this is the load-bearing detail)

- **`tree_id`** = SHA-256 over the tree only — for each tracked path in
  sorted order: `sha256(kind byte ∥ exec byte ∥ path bytes ∥ NUL ∥
  data)`, hashed again over the concatenation. Truncated to 40 hex.
  - kind byte: `0x66` (`f`) for regular files, `0x6c` (`l`) for symlinks
    — a symlink's target bytes and a regular file's identical content
    must never collide;
  - exec byte: `0x01` if the source file's owner-exec bit is set, else
    `0x00` (symlinks use `0x00`);
  - NUL separator: paths cannot contain NUL bytes on POSIX or Windows,
    so `path ∥ NUL ∥ data` is unambiguous — `{"a": b"bc"}` and
    `{"ab": b"c"}` hashed identically without it (collision audit
    2026-10-20).
  **Collision audit 2026-10-20 — kind byte + NUL separator: RATIFIED
  (owner decision 2026-10-20, item B2).** The original 1a formula
  (`path ∥ exec ∥ bytes`) had two ambiguities: the path/content boundary
  and symlink-target vs file-content. The implementation uses the
  formula above with regression tests pinning both collisions. *Ignores*
  message/author/time → identical trees share a tree_id. Used by:
  `status` (worktree.snapshot), merge (base/ours/theirs trees), and the
  nothing-to-pack check.
  **Owner decision 2026-10-20 (1a, ratified)**: the exec bit participates in
  identity — mode-only changes (chmod ±x) are detectable drift, never
  silent divergence between disk and history. The stat cache stores the
  mode alongside size/mtime so `status` flags a chmod cheaply; the file
  is then re-packed with its new mode bit. No compatibility cost:
  firnpack has no pre-1.0 layers outside test fixtures (fixed during the
  development window).
- **`layer_id`** = SHA-256 over a **canonical commit record** — a JSON object
  with sorted keys and a trailing newline containing exactly:
  `format_version`, `tree_id`, `parent`, `second_parent` (null), `message`,
  `author`, `created_at` — truncated to 40 hex. **Excludes** `layer_id` itself
  and `profile`: a commit's identity is codec-independent (two encodings of the
  same commit legitimately map to one file), and hashing a payload that
  contains its own id would be circular. Used by: filename, `verify`, history.

The in-band meta member **stores** `layer_id` for self-description but is **not
part of the hashed record**. `verify` therefore: recompute `tree_id` from
members → rebuild the record from parsed meta fields (minus `layer_id`, minus
`profile`) → hash → compare against filename, in-band `layer_id`, and DB row.

Identity consequences: identical trees with different messages/parents/times
are distinct layers; the one collapse case — byte-identical records (same tree,
message, author, parents, same second) mapping to the same `layer_id` — is
correct dedup, not data loss.

**Compression pipeline** (explicit, no reliance on tarfile mode strings):

- gzip profile: `tarfile.open(mode='w|', fileobj=gzip.GzipFile(fileobj=…,
  mode='wb', compresslevel=6, mtime=0))` — mtime=0 keeps gzip output
  deterministic.
- zstd profile: `tarfile.open(mode='w|', fileobj=<ZstdCompressor stream wrapper>)`
  via `compression.zstd` when importable. If zstd is unavailable on this
  interpreter, the repo silently falls back to gzip for *new* layers (old layers
  always read back by their recorded profile — reading never depends on the
  current profile).
- Decompression for reads: the recorded profile selects the decoder; stream mode
  throughout (sequential I/O, bounded memory).

**Extraction safety** — defense in depth (facts verified against
python.org 2026-10-18; full evidence recorded in
`research/tarfile-patch-levels.md`; 3.14.8 status corrected in v0.10):

1. **Stdlib filter**: `tarfile.extractall(filter='data')` — rejects absolute
   paths, `..` traversal, device nodes, unsafe symlinks. Native at the
   3.13 floor (and PEP 706-backported to 3.10.12+/3.11.4+).
   A `hasattr(tarfile, 'data_filter')` check guards unusual builds.
2. **Structural member allowlist (ours; hardlink-class)**: firnpack only
   ever *writes* regular-file and symlink members (it walks a worktree), so
   at both pack and extract time we validate that a layer's members are
   exactly regular files, symlinks, and implied directories — **hardlinks
   and special members are rejected outright**. The hardlink members of the
   2026 bypass class (CVE-2026-82049, the hardlink paths of
   CVE-2026-87910) are therefore *unrepresentable* in a firnpack layer.
   **Scoped honestly**: this does NOT cover the *symlink* fallback path of
   CVE-2026-87910 (symlinks are legitimately in our format), nor future
   filter bugs — hence layers 3 and 4.
3. **Windows-safe name validation (ours; all-or-nothing pre-pass)**: before
   any file is written, the *entire* member list is validated in one pass.
   Names must be relative, `..`-free, **backslash-free** (on Windows `\` is
   a separator: `..\evil` is one POSIX segment but traverses there), free
   of drive prefixes (`C:`, `C:\`), UNC prefixes (`\\`, `//`), and
   Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9,
   CONIN$, CONOUT$ — with any extension). A layer that fails aborts with
   **zero files written**: no half-extracted junk on the HDD.
4. **Patch-level awareness (per-minor table; Q19 resolved: WARN)**: Python
   3.12.15 / 3.13.16 (2026-09-30) fixed the three tarfile filter bypasses
   (CVE-2026-82049, -19672, -87910); **3.15.0 includes the gh-157265 fix**
   (landed in RC3, 2026-10-02; final had no further changes). **3.14.8 is
   UNCONFIRMED**: the python.org 3.14.8 security list omits
   CVE-2026-87910, the 3.14 backport (PR #157307) closed 2026-10-01 — one
   day after the release — and wolfi ships a patched 3.14.8 rebuild dated
   2026-10-08, which suggests plain 3.14.8 lacked the fix. Until a 3.14.x
   release explicitly lists gh-157265, **all 3.14.x warns**.

   | Minor | Warn below | Basis |
   |---|---|---|
   | 3.12 | (3, 12, 15) | below package floor; source installs only |
   | 3.13 | (3, 13, 16) | changelog lists gh-157265, gh-157190 (CVE-2026-82049), gh-155999 |
   | 3.14 | all 3.14.x | unconfirmed through 3.14.8 |
   | 3.15 | (3, 15, 0) | gh-157265 in RC3 |

   The gate is a per-minor table, never a single tuple —
   `sys.version_info < (3, 12, 15)` would absurdly pass 3.13.0. On warn
   (stderr + JSON `"tarfile_patch_warning"`) extraction **proceeds** (Q19:
   distro-patched builds don't bump version numbers; a hard gate would
   falsely lock out safe users). `hasattr` cannot detect these bypasses —
   they are bugs *in* the filter — hence the explicit version comparison.

**Symlinks on Windows (Q20 — RESOLVED 2026-10-18, owner ratified with an
amendment)**: creating symlinks needs `SeCreateSymbolicLinkPrivilege`
(Developer Mode or admin). Without it, tarfile's link fallback **silently
copies the link-target member** — a content substitution that also walks
the CVE-2026-87910 fallback path on unpatched builds. firnpack therefore
probes symlink capability before extracting a layer that contains symlinks
and, if the probe fails, **refuses** with a humane message (enable Developer
Mode, or use `--flatten-symlinks`). `restore|export --flatten-symlinks` is
the explicit opt-in that materializes links as copies, loudly. Layers are
data-at-rest for a non-developer; hostile layer files must not be able to
write outside the destination.

**Agent-continuation contract for the refusal and the flatten path (owner
amendment, 2026-10-18 — "give good documentation/output so the agent can
keep working if it needs to flatten")**: the refusal is not a dead end.
Human-readable refusal message template:

```
This layer contains 3 symlinks, but this Windows account can't create them
(needs Developer Mode or admin). Nothing has been extracted yet.

Options:
  1. firnpack restore <id> --flatten-symlinks
     Extracts now; each link becomes a real copy of what it pointed to.
     Every flattened path is listed below, so nothing changes silently.
  2. Enable Windows Developer Mode (Settings → Privacy & security → For
     developers), then re-run the restore to recreate real links.
  3. Extract on a machine that allows symlinks.
```

`--json` refusal payload (stable contract, part of the test suite):
`{"error": "symlink_capability_probe_failed", "symlink_count": 3,
"symlinks": [{"path": "link.sh", "target": "lib/real.sh"}, …],
"remedies": {"flatten_flag": "--flatten-symlinks",
"doc_ref": "docs/usage.md#flatten-symlinks"}}` — paths and targets are
pre-listed so an agent can decide to flatten without a second exploratory
run. With `--flatten-symlinks`: the command's summary AND `--json` output
carry `"flattened": [{"path": …, "target": …}, …]` — the complete list,
every time, in both output modes. docs/usage.md §flatten-symlinks documents
the semantics (copies, not links) and the persistence chain: **flattened
paths stay visible until they are committed** — `status` reports
*"3 paths flattened from symlinks (will be stored as regular files on
next commit)"* + JSON `"flattened_pending": [...]`; `commit` echoes the
same list before writing; the layer's `__firnpack_meta__` records a
`flattened_paths` array (so the divergence is auditable in history); and
committing flattened paths still present in the worktree requires
**`--allow-flattened`** (or `[core] allow_flattened = true`) — the same
guard pattern as the marker guard, because "never silent substitution"
must survive one command past the restore that caused it.

## 6. The manifest (glacier.db)

SQLite schema (5 tables):

```sql
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE layers (
    id            TEXT PRIMARY KEY,   -- layer_id (40 hex) = filename
    tree_id       TEXT NOT NULL,      -- 40 hex; content-only identity
    parent        TEXT REFERENCES layers(id),   -- NULL for roots
    second_parent TEXT REFERENCES layers(id),   -- NULL unless merge layer
    message       TEXT NOT NULL,
    author        TEXT NOT NULL,
    created_at    TEXT NOT NULL,      -- UTC ISO-8601
    profile       TEXT NOT NULL,      -- 'zstd' | 'gzip' (this layer's codec)
    file_count    INTEGER NOT NULL,
    size          INTEGER NOT NULL    -- compressed size, bytes
);

CREATE TABLE branches (
    name       TEXT PRIMARY KEY,
    head       TEXT NOT NULL REFERENCES layers(id),
    created_at TEXT NOT NULL
);

CREATE TABLE refs (            -- reserved for v2 tags; v1 writes nothing here
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE worktree (        -- exactly one row (id=1): ALL mutable repo state
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    head_kind    TEXT NOT NULL CHECK (head_kind IN ('branch','pin')),
    head_branch  TEXT,          -- branch name when head_kind='branch'
    head_pin     TEXT,          -- layer_id when head_kind='pin'
    head_id      TEXT REFERENCES layers(id),  -- resolved current layer.
                                 -- NULL until the first commit (9.1: empty
                                 -- repo, no initial layer). Enforced by the
                                 -- trigger below: once set, NULL is rejected.
    snapshot     TEXT,          -- tree_id of worktree at last sync (NULL: unknown)
    stat_cache   TEXT,          -- JSON: {path: [size, mtime_ns, mode]} of
                                 -- tracked files (mode: 0o644/0o755 — 5)
    merge_theirs TEXT,          -- layer_id of THEIRS head while a merge is pending;
                                 -- NULL otherwise. Set on conflicted merge; consumed
                                 -- by commit into the new layer's second_parent.
    flattened_pending TEXT,     -- JSON array [{path, target}] while flatten-pending
                                 -- state exists (ratified by owner 2026-10-19 — §5); set by
                                 -- restore --flatten-symlinks, cleared by commit.
    updated_at   TEXT NOT NULL
);

-- Enforce "NULL until the first commit, then never NULL again".
-- A partial/conditional index can't do this (SQLite indexes don't
-- enforce values); a trigger can, cheaply, and stays within stdlib SQLite.
CREATE TRIGGER worktree_head_id_no_null
AFTER UPDATE OF head_id ON worktree
WHEN NEW.head_id IS NULL AND OLD.head_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'worktree.head_id cannot return to NULL');
END;
```

Notes:

- **No reflogs, no reachability GC** (non-goals) — with one explicit,
  owner-mandated exception: **`compact` (§9.9)** rewrites history and
  deletes unreachable layer files, under explicit permission only. Outside
  compact, unreachable layer rows simply accumulate — harmless at this
  scale. A standalone `gc` command remains out of scope (superseded by
  compact, §24).
- The `worktree` row is the persisted analog of jj's TreeState: `snapshot` +
  `stat_cache` let `status` skip rehashing unchanged files (§12); `head_kind` /
  `head_branch` / `head_pin` replace git's HEAD file entirely.
- All mutations (commit, branch, switch, merge, amend, restore) update this state
  in the same transaction that writes their layer rows — there is no second file
  to desynchronize.
- **Pending-merge state**: `merge_theirs` non-NULL means a conflicted merge is in
  progress (markers already in the worktree). It is part of the transactional
  state, so a crash mid-resolution leaves the pending state durable — exactly
  like git's MERGE_HEAD, but inside the single DB. Only `commit` (completes the
  merge) or `merge --abort` clears it.

## 7. Config file (`.firn/config`)

INI via stdlib `configparser` — kept out of the DB so the human can edit it with
any editor without touching the transactional store:

```ini
[user]
name = Fabrizio        ; commit author (default: OS username)

[core]
profile = gzip         ; v1.0: gzip only (zstd reserved for the v1.x opt-in)
autoname = true        ; auto commit messages like "pack 3 files (+1.2 KiB)"

[merge]
block_unresolved_markers = true   ; commit with leftover conflict markers:
                                  ; block, exit 1. false = warn and commit.
                                  ; `commit --allow-markers` overrides once.

[history]
reminder_threshold = 50           ; commit prints a one-line "consider
                                  ; compact" reminder when layer count
                                  ; crosses this (rate-limited per crossing)
```

The *actual* codec of each layer is recorded per-layer (DB + in-band), so mixed
profiles across history are always readable regardless of this setting.

## 8. Command set

A non-developer's daily loop is 5 commands; the full v1 surface is 17. An agent
uses the same 17 plus `--json`.

| Group | Command | Does | git analog |
|---|---|---|---|
| core | `init` | create `.firn/`, schema, config | `git init` |
| core | `commit [-m MSG]` | snapshot tracked tree → layer; auto-msg if omitted | `add -A && commit` |
| core | `history [-n N] [--all] [BRANCH]` | commits newest-first, tips marked | `git log` |
| core | `status [--full]` | drift vs head layer | `git status` |
| core | `diff` | unified drift diff (worktree ↔ head) | `git diff` |
| core | `show <id>` | one commit: meta + manifest (+ diff vs parent) | `git show` |
| branching | `branch <name> [FROM]` | create branch at head or commit | `git branch n [sha]` |
| branching | `switch <name\|id>` | move worktree to branch or pin | `git switch` |
| branching | `merge <branch>` | 3-way merge into current (§10) | `git merge` |
| branching | `amend [-m MSG]` | replace head layer (new id, same parents) | `commit --amend` |
| safety | `restore <id\|head> [--to DIR]` | extract layer over worktree (rescue first) or to DIR | `checkout <sha> -- .` |
| safety | `export <id> --to DIR` | extract to fresh directory (zero risk) | `archive` + extract |
| safety | `verify [--rebuild-db]` | re-hash all layers; DB consistency; optional rebuild | `git fsck` |
| safety | `compact --keep N [--archive DIR] [--yes] [--dry-run]` | squash old layers, reclaim disk (§9.9; explicit permission only) | history squash |
| mgmt | `list-branches` | branches + heads | `git branch -v` |
| mgmt | `rename-branch <old> <new>` | rename | `git branch -m` |
| mgmt | `delete-branch <name>` | delete (warns if head becomes unreachable) | `git branch -d` |

(`root <id>` — list layer DAG roots — cut from v1; `show` + `history --all`
cover the need. `tags`, per-path restore: v2, §24. `gc` as a separate
command is superseded by `compact` — §9.9.)

**CLI name**: `firnpack`, with an optional `firn` console-script alias (Q1).
Implementation order: core group first (M1), branching+safety+mgmt second (M2).

## 9. Semantics: the operations

### 9.1 init

Create `.firn/`, `layers/`, empty DB (schema above), `config` with defaults.
No initial layer (empty repo, like git). `status` on an empty repo: *"no layers
yet — pack your work with `firnpack commit`"*.

### 9.2 commit (packing)

1. **Tracked set** = everything in the worktree except `.firn/`, `.git/`,
   and `.gitignore` rules (gitignore subset: blank lines, `#` comments,
   trailing-slash dirs, `*`, `?`, `[seq]`, `!` negation, rooted `/`
   patterns — the same file git reads, Q2). No `add`; tracking is implicit.
   **Only the root `.gitignore` is read (v1)** — nested `.gitignore`
   files are NOT consulted (a documented divergence from git, kept for
   v1 simplicity); `status` warns whenever a nested `.gitignore` exists,
   so the divergence is never silent.
2. Compute `tree_id` (streaming, one read per file, sequential I/O).
3. If `tree_id == worktree.snapshot` → *"nothing to pack"*; exit 0; no layer.
4. Build meta + canonical tar → compress → write `layers/<id>.firn.tmp` →
   `os.replace` to `<id>.firn` (atomic; a `.tmp` orphan is ignorable garbage).
5. One SQLite transaction: insert layer row, move `branches.head` (or pin),
   update `worktree` (head_id, snapshot, stat_cache, updated_at, and clear
   `merge_theirs` if set).
6. **Completing a pending merge**: if `worktree.merge_theirs` is set, the new
   layer's `second_parent` = that value (parent stays ours-head). This is how a
   conflicted merge is finished: resolve markers → `commit` → two-parent merge
   layer. Without this rule theirs-head would silently vanish from the DAG —
   the exact bug this state exists to prevent. **Unresolved-marker guard
   (owner decision 2026-10-18: block by default)**: before writing, `commit`
   scans tracked text files for leftover markers — lines starting `<<<<<<<`
   or `>>>>>>>` (`=======` is deliberately excluded: too false-positive-prone
   as legitimate content) — and **refuses**: exit 1, lists the files, JSON
   `"blocked_by_markers": [...]`. Overrides: `[merge]
   block_unresolved_markers = false` (warn-and-commit) or `commit
   --allow-markers` for this commit only (§11).
   **Flattened-paths guard (same pattern)**: committing while
   flattened-but-uncommitted paths are present requires `--allow-flattened`
   (or `[core] allow_flattened = true`); `commit` echoes the pending list
   and records it in the layer's `flattened_paths` meta field (§5) —
   auditable in history, never silent.
7. Identical-record edge: same tree, message, author, parents, and timestamp
   hash to the same `layer_id` — second write is a no-op (file exists, DB row
   insert-or-ignore); correct dedup. Different message/parents/time → distinct
   layers even with identical trees.

### 9.3 amend

New layer with the **same parent(s)** as the current head layer, tree = current
worktree, message = new (or old if `-m` omitted). Branch head moves to the new
layer_id. The previous head layer is never deleted (write-once); it may become
unreachable — allowed by design. Amending a root layer creates a new root, same
way. If the worktree tree equals the head layer's tree, amend only rewrites the
message (still a new layer_id — message is part of identity). **Refuses while a
merge is pending** (exit 1): amend would re-parent a half-finished merge.
Resolve/abort the merge first.

### 9.4 branch, branch-from-commit, switch

- `branch <name>` — branch at current head; stays on current branch (like
  `git branch`); `switch` to move.
- `branch <name> <from>` — from a specific commit: `FROM` is a layer_id prefix
  (≥ 6 hex, must be unambiguous) or a branch name.
- `switch <branch>` — refuses (exit 1, no mutation) if there is drift; the user
  must `commit` (or `restore`/`export` to discard — rescue pack makes this safe).
  No auto-snapshot (Q4).
- `switch <id>` — pins the head (`head_kind='pin'`). Pinning is a first-class,
  warning-free state (no "detached HEAD" anxiety); `switch <branch>` returns to a
  branch. Committing while pinned advances the pin (new head layer, parent = old
  pin) — same as git's detached-commit, minus the shame.
- `switch` **refuses while a merge is pending** (exit 1): resolve → `commit`, or
  `merge --abort` first. Mid-merge worktree files (markers) must never be
  silently carried to another head.

### 9.5 history

Newest-first walk from the head (default), a named branch, or `--all` (every
branch head). Follow `parent` + `second_parent`; dedupe visited ids (merge
diamonds). Human output: id, date, author, message, branch tips marked `*`,
merge layers show both parents. `--json` mirrors it.

### 9.6 diff / show

- `diff` — worktree ↔ head layer; `difflib.unified_diff` on UTF-8 (decoded with
  `errors='replace'`); binaries → *"binary file differs (size a → b)"*.
- `show <id>` — meta + file manifest + optional diff vs first parent.

### 9.7 restore / export

- `restore <id|head> [--to DIR]` — if drift exists, first write a **rescue
  layer** (tree = current worktree, parent = current head layer, message
  *"auto: drift rescue before restore"*) so nothing is ever lost; then extract
  the target layer over the worktree (safe-extract, §5) and sync `worktree`
  (snapshot/stat_cache). `--to DIR` extracts elsewhere without touching the
  worktree.
- `restore` **refuses while a merge is pending** (exit 1): the marked-up
  worktree is the merge resolution state; clobbering it would destroy the user's
  conflict work. `merge --abort` is the intended exit. `--to DIR` and `export`
  remain available (read-only, don't touch the worktree).
- `export <id> --to DIR` — extract into a fresh directory; never destructive;
  the recommended exploration path for agents.

### 9.8 verify

For every `layers/*.firn`: stream-decompress via the recorded profile,
recompute `tree_id` from members, rebuild the canonical commit record from
parsed meta (excluding `layer_id`/`profile`), recompute `layer_id`, and compare
against the filename, the in-band `layer_id`, and the DB row. Then DB
consistency: branch heads exist, worktree row resolves, no dangling parent
pointers. `--rebuild-db`: reconstruct the DB from in-band `__firnpack_meta__`
of every layer file (§13).

### 9.9 compact (history management — owner requirement, from Q18)

Whole-tree layers are the accepted cost of the glacier model (Q18); when
history grows past comfort, `compact` squashes old layers and reclaims
disk. **Never automatic** — explicit permission only:

```
firnpack compact --keep N [--archive DIR] [--dry-run] [--yes]
```

- `--keep N` — **required, no default, and no config default exists or
  may be added**: an agent must state N explicitly every time; nothing
  lets compaction fire unprompted (owner rule: never without permission).
  Keep the newest N layers of the current head's **first-parent chain**;
  everything below the cut is squashed into one new **foundation layer**
  (tree = tree at the cut; message records the squashed id range, layer
  count, and bytes; new root). The kept layers are re-parented onto it —
  which **rewrites their layer_ids** (parentage is part of identity, §5).
  `--dry-run` prints the full plan (layers dropped, layers rewritten,
  layers garbage-collected, bytes reclaimed, peak disk need) and mutates
  nothing.
- **Refusal conditions (compaction is a history rewrite — check first)**.
  The guard is **reference-based**: FK constraints see every reference,
  not just branch heads, so the analysis walks ancestry (`parent` and
  `second_parent`, any depth) from every live root — branch heads, the
  worktree head or pin, and the second parents of kept merge layers
  (ratified 2026-10-20, owner decision; supersedes the head-membership
  sketch):
  1. a merge is pending (`merge_theirs` set) — compact refuses outright;
  2. any *other* branch head resolves into the collapsed set, **or any
     reachable survivor's ancestry enters the deleted set** (a branch
     cut from inside the range and committed on has its head outside
     the range but its parent inside — the refusal names the branch);
  3. the pin resolves into the collapsed set;
  4. the kept range contains a merge layer whose `second_parent` reaches
     below the cut (re-pointing it would invent history — refuse, or
     choose a higher cutoff; `--keep 1` always works and is the
     recommended deep-clean).
  **Remap instead of refuse**: a branch head sitting exactly at a KEPT
  layer is repointed to that layer's rewritten id in the same
  transaction — the branch follows the rewrite, and its merge base with
  the compacted branch is preserved (the v0.13 "warns: loses merge
  base" case no longer exists).
  **Garbage collection**: unreachable survivors that reference the
  deleted set — amend orphans (the replaced head stays a live row until
  a compact), second-parent stubs of collapsed merges with their branch
  deleted — are collected with the rewrite: counted in the dry-run plan
  (`layers_garbage_collected`), included in the `--archive` export,
  rows and files deleted, result reports `garbage_collected`. Disk
  reclaim covers them; nothing reachable is ever collected.
  The swap transaction runs under **deferred FK enforcement**:
  consistency is checked at COMMIT against the final state, which the
  guard phase has already proven consistent — every referencer of a
  deleted row was remapped, collected, or refused.
- **Peak-disk check (up front, before any write)**: compact writes the
  foundation + N rewritten kept layers **before** deleting old files, so
  peak need ≈ old-layers total + (N+1) layer writes. `--dry-run` reports
  it (*"peak disk needed: ~X MB (writes Y MB before freeing Z)"*), and
  real runs **refuse** when free space on the volume is below the peak,
  with the humane message: compact writes new layers before deleting old
  ones (crash-safety, § below); free up space, or copy the repo folder
  to a roomier drive and compact there — the glacier is self-contained.
  A mid-write ENOSPC is never the failure mode; the refusal is.
- **Crash ordering**: `--archive` export (if any — includes collected
  garbage) → write ALL new layers (foundation + rewritten kept) to disk
  → ONE DB transaction (insert new rows, swap head pointer and branch
  remaps to the new chain, delete old and garbage-collected rows,
  deferred FK enforcement) → delete old files that are
  now unreachable → orphan sweep on next command. Crash windows are both
  safe: pre-DB-swap → old history fully intact, new files are orphans
  (swept); post-swap/pre-delete → new history authoritative, old files
  are unreferenced garbage (swept). Neither window can produce a
  half-compact.
- **Merge constraint (v1 honesty)**: if the kept range contains merge
  layers whose `second_parent` reaches below the cut, compact **refuses**
  (the rewrite would orphan merge structure). `--keep 1` — squash
  everything into a single layer — always works and is the recommended
  deep-clean.
- **Reachability-safe deletion**: a layer file is deleted only when it is
  unreachable after the rewrite — from **every** branch head, pin, and
  kept merge edge. A branch still pointing at a kept layer follows the
  rewrite (remap); a branch pointing into the collapsed range blocks it
  (refusal). Compact never silently rewrites history another branch
  depends on, and never deletes anything reachable.
- `--archive DIR` — before deletion, the compacted-away range is exported
  to `DIR/firnpack-archive-<date>.zip` (outside `.firn/`, human-visible,
  consciously OneDrive-syncable). Without `--archive` the deletion is
  unrecoverable, so compact requires `--yes` (or an interactive y/n prompt
  in a TTY; non-TTY / `--json` demands `--yes`).
- **Reminder (owner requirement)**: after a commit, if total layer count
  crosses `[history] reminder_threshold` (default 50) and no reminder was
  shown since the last crossing, `commit` prints one line (human: *"glacier
  holds N layers (~X MB) — `firnpack compact` can shrink it"*; JSON:
  `"history_reminder"`), recorded in `config` — rate-limited to once per
  crossing, never nagging.

## 10. Merge algorithm

3-way merge, tree-level, stdlib only. Single-user means no concurrent-human edge
cases; the algorithm is the boring, correct one:

0. **Drift guard**: if the worktree has drift or a merge is already pending →
   refuse (exit 1, no mutation, nothing written). `merge` must never clobber
   uncommitted work with conflict markers or a fast-forward. Commit or
   `merge --abort` first. (Same guard posture as `switch` §9.4.)
1. **Base** = LCA(theirs-head, ours-head) over the parent DAG (BFS; the layers
   table is small, in-memory is fine).
2. If base == theirs-head → already up to date (exit 0, message).
3. If base == ours-head → **fast-forward**: move the current branch head to
   theirs-head, sync the worktree (clean — guaranteed by step 0), no new layer.
   (Reported as "fast-forwarded".)
4. Otherwise, per-path 3-way over union(base, ours, theirs):
   - one side changed → take that side;
   - both sides same change → take it;
   - both changed differently:
     - text files → line-level 3-way with git-compatible markers (§11);
     - binaries → conflict, no auto-merge;
     - delete/modify → keep the modified side, report as conflict (Q7);
   - v1 has no rename tracking: a rename is delete+add.
5. Merged tree (if clean) → one **merge layer**: `parent = ours-head`,
   `second_parent = theirs-head`; one transaction (§9.2 step 5).
6. Conflicts → exit 1, and a **pending merge** begins. **Write ordering
   (crash-correct)**: the DB transaction setting `worktree.merge_theirs =
   theirs-head` commits FIRST; only then are merged file contents + conflict
 markers written to the worktree. Crash between the two leaves a pending merge
   with an untouched worktree — coherent, and fully recoverable via
   `merge --abort`. (The reverse order would resurrect the original blocker
   bug inside a crash window: markers on disk, no pending state, and a
   follow-up `commit` would drop theirs-head from the DAG.) No layer is
   written. The message explains: fix the marked files, then `firnpack
   commit` — which consumes `merge_theirs` as `second_parent` (§9.2 step 6)
   and completes the merge layer. Bail-out: `firnpack merge --abort` —
   restore the head layer to the worktree (with the §9.7 rescue semantics if
   anything was touched since) and clear `merge_theirs`, all in one
   transaction. No other command clears this state.

## 11. Merge conflict surface

- Text conflicts carry git-compatible markers `<<<<<<< ours` / `=======` /
  `>>>>>>> theirs` — deliberately, so LLMs trained on git conflicts can resolve
  them natively.
- delete/modify: modified content kept; reported as a conflict (the report names
  the kept side). Rationale: non-destructive default for a non-developer (Q7).
- Binary conflicts: reported with both `tree-side` hashes; resolution workflow is
  `export`/`restore --to` + copy (per-path restore is v2, Q8).
- A conflicted merge never writes a layer, but it *does* write state: the
  pending-merge (`merge_theirs`) is durable in the DB, surfaced in `status`
  (human: *"merge in progress — resolve markers, then commit"*; JSON:
  `"merge_pending": true, "merge_theirs": "<id>"`). An agent checking `status`
  can never mistake a mid-merge worktree for ordinary drift.
- **Unresolved-marker guard at commit — owner decision 2026-10-18**:
  completing a pending merge with leftover `<<<<<<<`/`>>>>>>>` markers
  **blocks** by default (exit 1; report lists files; JSON:
  `"blocked_by_markers": [...]`). Rationale: a non-developer's most likely
  merge failure is forgetting a marker — catching it at commit is the cheap
  moment. Escape hatches for legitimate marker-shaped content: config
  `[merge] block_unresolved_markers = false` (warn-and-commit) or one-off
  `commit --allow-markers`. Scan matches lines starting `<<<<<<<` or
  `>>>>>>>` only; `=======` alone is excluded (markdown/RST false
  positives).

## 12. Status & change detection

Fast path borrowed from jj's TreeState:

1. For each tracked file: if `(size, mtime_ns)` matches `worktree.stat_cache`
   → assume unchanged.
2. Else rehash the file; changed files contribute to drift.
3. Untracked = on disk, not in cache, not ignored. Missing = in cache, not on
   disk.
4. Drift summary: human (git-status-short-shaped, friendlier words) or `--json`.
5. `status --full` bypasses the cache and rehashes everything (mitigates
   OneDrive touching files without mtime change — documented limitation).
6. On any commit/restore/switch, the cache is rewritten from the new tree.
7. Pending merge is reported whenever `merge_theirs` is set (see §11).
8. **Excluded files are visible, never silent** (silent-exclusion is the
   Q7 failure mode applied to ignores): `status` always reports
   *"N files excluded by .gitignore"*; `status --ignored` lists them;
   `status --json` carries `"ignored": [paths]`. A warning line fires
   when a nested `.gitignore` exists (not read in v1 — §9.2).
9. Pending flattens are reported whenever `flattened_paths` state is set
   (§5 — restore/flatten persistence chain).

Known edge (documented): content change with identical size+mtime is invisible
until `--full` or the next mtime change. Same tradeoff git makes; on this
machine, `--full` is cheap at small-project scale.

## 13. Robustness: fail-safe design

- **Durability protocol for every layer write** (order matters):
  1. write `layers/<id>.firn.tmp`;
  2. `flush()` + `os.fsync(fd)` on the tmp file;
  3. `os.replace(tmp, <id>.firn)`;
  4. `os.fsync()` on the `layers/` directory fd (POSIX; safe no-op on Windows);
  5. *then* the SQLite transaction (`synchronous=FULL`) recording the layer and
     moving the head.
  Crash at any point leaves nothing (pre-replace) or a fully durable orphan
  layer file (pre-DB). Orphan rows never exist; orphan files are ignored by
  `verify` and swept by the next commit. The repo is always at its previous or
  its new commit — never between.
- **Windows/OneDrive replace retries**: `os.replace` (and DB open) can raise
  `PermissionError` when OneDrive or antivirus holds a handle — the exact
  target environment. Writers retry with bounded backoff (5 attempts, 0.2 s →
  2 s), then fail with a humane message naming the likely culprit (sync/AV
  lock) and the safe next step (retry; nothing was corrupted).
- **Windows path length (MAX_PATH)**: Windows 11 still caps paths at 260
  chars unless LongPathsEnabled is set. `commit` validates tracked paths
  pre-write and warns (does not block) when a full path would exceed ~240
  chars; extraction of such paths fails unless long paths are enabled — the
  README documents the one-time registry setting. Caught at pack time, not
  discovered at restore time years later.
- **Atomic state**: every command = one SQLite transaction (single mutable
  file; `synchronous=FULL`). Crash mid-commit leaves at worst an orphan tmp
  file and no DB row — the repo is simply at its previous commit.
- **Rebuild**: `verify --rebuild-db` reconstructs layers/refs from in-band
  meta. Branch names and pins are NOT recoverable from layers (they live only
  in the DB). Rebuild creates `main` at the layer with the newest `created_at`
  (ties broken by id), prints a notice about what was lost. (Q9: acceptable
  boundary?)
- **Checksums everywhere**: filename = layer_id; in-band meta carries layer_id +
  tree_id; DB rows cross-checked against both.
- **No background daemons, timers, or file watchers. Ever.** (HDD discipline.)
- **OneDrive co-existence**: honest limitation — if the project folder lives in
  a OneDrive-synced tree, OneDrive will sync `.firn/` too. Recommendations in
  README: exclude `.firn/` from sync, or keep projects outside OneDrive paths.
  (SQLite files + sync agents are a known-bad combination.)

## 14. Compression profiles

**v1.0 decision: gzip only.** Rationale: a layer written with
`compression.zstd` (Python 3.14+) **cannot be read on 3.13.x** —
self-describing profiles don't help a reader that lacks the codec, and
"readable years later from a single core" is a founding promise (AGENTS.md).
The target machine runs **Windows 11** (Q17a resolved 2026-10-18) and the
package floor is 3.13.16 (Q17b final) — below 3.14 — so zstd cannot be a
v1.0 writer: it would need either a 3.14-only floor or a `backports.zstd`
dependency (breaks G5). Confirmed: **gzip-only v1.0**, zstd stays a v1.x
opt-in for users who know every future reader runs 3.14+.

| Profile | Library | Level | Status |
|---|---|---|---|
| `gzip` | stdlib `gzip` | 6 (mtime=0, deterministic) | **v1.0: default and only writer** |
| `zstd` | `compression.zstd` (3.14+, optional) | 3 | v1.x, opt-in via config, only when every future reader of the repo has the codec; reading old zstd layers keeps working wherever the codec exists |

`bzip2`/`lzma` rejected: too slow on a weak CPU. The profile plumbing (per-layer
recording, in-band, DB) ships in v1.0 unchanged, so the v1.x zstd opt-in needs
no format change. Speed/ratio claims deferred to measured benchmarks (§23) — no
unmeasured numbers in shipped docs.

## 15. Alternative storage designs rejected

(Detail in `research/prior-art.md`.)

| Design | Verdict | Why |
|---|---|---|
| One SQLite for everything (Fossil-style, blobs in DB) | rejected | rewrite-heavy DB fragments on HDD; content coupled to DB engine; recovery requires SQLite |
| Fossil-style delta chains | rejected | delta chains complicate verify/recover — our explicit G2 tradeoff |
| tar/zip per commit + JSON manifest file | rejected | JSON-per-repo needs careful atomic rewrite; SQLite transactions are cleaner |
| zip per commit (vs tar) | rejected | zip's central directory + per-member random access → more seeks; tar is sequential |
| append-only snapshot journal | rejected | mid-write tail is not crash-safe; recovery complexity |
| plain copies, no compression | rejected | defeats disk-space goal |
| **tar-per-commit + SQLite manifest (chosen)** | **chosen** | tiny fixed file set; content in sequential whole files; engine-independent; DB rebuildable from layers |

## 16. CLI & exit codes

- `firnpack` root (+ optional `firn` alias, Q1); `argparse` subparsers.
- Exit codes: **0** success · **1** user-facing error (drift-blocked switch,
  merge conflict, not-a-repo…) · **2** argparse usage error · **3** integrity
  failure (verify). Stable across v1; documented in `docs/agent-contract.md`.
- `--json` on every command that produces output; shapes are part of the test
  contract (§22).
- `--version`, `--help` standard. No `--quiet` (fewer flags, fewer questions).

## 17. Python API

`from firnpack import Repo` — the CLI is a thin wrapper. Methods mirror commands
1:1 (`repo.commit(msg=None) -> str` layer_id; `repo.merge(branch) ->
MergeResult`…). Typed exceptions: `FirnpackError` base, `NotARepoError`,
`DriftError`, `MergeConflict(paths…)`, `IntegrityError(path, expected, got)`.
Agents may import the module directly (the skill file documents both paths: CLI
for humans, API for agents). Full signatures in `docs/api.md`.

## 18. Packaging & distribution

- One module: `firnpack.py` at repo root. `pyproject.toml` with
  `py-modules = ["firnpack"]` (setuptools — Q10; hatchling equivalent).
- Wheel: universal `py3-none-any`, contains exactly `firnpack.py` + dist-info;
  CI guards this (§19).
- `[project.scripts]`: `firnpack = "firnpack:main"` (+ `firn` alias if Q1 lands
  yes).
- Runtime `dependencies = []` — genuinely empty.
- Python support: **3.13.16 – 3.15** (Q17b final, owner decision
  2026-10-18: "floor must be 3.13.16+" — the first release both patched
  (all three tarfile CVEs on its release page; evidence in
  `research/tarfile-patch-levels.md`) and shipped as a Windows
  installer). Support facts as of 2026-10: 3.15 current stable
  (2026-10-09); 3.14 bugfix to 2027-10, security to 2030-10; 3.13
  security to 2029-10; 3.12 security-fixes to 2028-10.
  - `requires-python = ">=3.13.16"` (pip enforces patch-level floors).
    A distro user on an older-but-distro-patched 3.13.x is locked out — a
    documented, accepted trade of this floor; relaxable in a patch
    release if it bites. **Who the §5 patch warning actually serves**: pip
    already refuses 3.12 and 3.13.0–3.13.15, so the warn-gate's remaining
    audience is the **ungated skill-repo copy of `firnpack.py`** (repo #2,
    no pip metadata) and manual/source installs. "Q19 = warn" is
    therefore *not* "we accept old interpreters" — the package gate
    already blocks them; the warning is the safety net for copies that
    travel outside pip.
  - **Recommended interpreter on the laptop**: **3.15.x** (`py install
    3.15`) — current stable, confirmed patched (gh-157265 in RC3),
    installers available; 3.13.16 equally patched. 3.14.x carries the §5
    unconfirmed-status warning through 3.14.8. zstd profile only on 3.14+
    (3.14/3.15), gzip elsewhere.
- sdist: README, LICENSE, tests, docs, research. Tests never in the wheel.
- `description` = the tagline; long_description = README (markdown).
- License: MIT recommended (Q11).
- Name availability: checked 2026-10-18 via web search — "firnpack" unclaimed on
  PyPI/GitHub (only glaciology literature uses the word literally). Re-verify
  directly on pypi.org at publish time.

## 19. CI/CD

Standard PyPA patterns (research/packaging-ci.md):

**`ci.yml`** (push + PR): matrix — `ubuntu-latest` × {3.13.16, 3.13, 3.14,
3.15}; `windows-latest` × {3.13.16, 3.13, 3.15} (**Windows 11 is the
target OS** — pinned floor and current stable, not a token job);
`macos-latest` × {3.15}.
**The floor leg is pinned to exactly `3.13.16`** on BOTH ubuntu and
windows (a moving `3.13` label relies on setup-python's manifest resolving
to the latest patch — not a guarantee; the shipped floor must be what gets
tested). Other legs may use `3.13` (latest). Filesystem-heavy tool: tar
semantics, path separators, mtime granularity, MAX_PATH behavior differ.
3.15.0 released 2026-10-09; the matrix tracks current stable — add each
new feature release as it lands, guarded by the workflow guard test.
Steps: checkout → setup-python → `pip install -e .[dev]` → `ruff check` + `ruff format --check` → `pytest`. Dev
extras (pytest, ruff) are development-only tooling, not runtime deps (Q12).

**Container/base-image decision — RESOLVED 2026-10-18 (owner ratified)**:
keep `ubuntu-latest`, no Alpine/Void. Evidence: GitHub's billing docs say
hosted runners are **free** for public repositories; the hosted-runners
reference adds "unlimited minutes" for public repos; GitHub's 2026
pricing-changes page reports no billing changes for public Actions
minutes — so image size has zero cost impact here.
Alpine rejected: musl/busybox is a third exotic platform for
a filesystem-heavy tool whose users run Windows/macOS/glibc-Linux —
test-surface risk for zero budget gain. **Verified**: job/service
containers require Linux runners (GitHub workflow-syntax docs), so the
Windows target-OS job can't be containerized regardless. **Tier 2
(search-summary only, not fetched)**: `ubuntu-slim` (official
container-based Ubuntu 24.04 runner, ~1-CPU, unprivileged, merged
2025-12) and `python:3.13-slim` as future self-hosted options —
re-verify before relying on them.

**`release.yml`** (tag push `v*`): build job (`python -m build`) → artifact →
publish job `pypa/gh-action-pypi-publish@release/v1` with **trusted publishing**
(OIDC, job-level `id-token: write`, contents: read; publisher bound on PyPI to
this repo + workflow filename; no API tokens in secrets).

Guards shipped with v1:

- **Single-file wheel guard**: CI builds the wheel, asserts its file list is
  exactly `firnpack.py` + `dist-info` (protects G7).
- **Workflow guard test**: in-repo test asserts both workflow files exist and
  reference the supported Python versions (config-drift discipline).

## 20. Repository documentation model

Docs are a first-class deliverable (G4). All markdown, no doc framework:

```
README.md              — humane front door: what/why, quickstart, command table
AGENTS.md              — agent context: brief, constraints, terminology, invariants
SPEC.md                — this document (living, versioned decision record)
docs/usage.md          — every command: examples, exit codes, JSON shapes
docs/agent-contract.md — normative LLM contract: JSON schemas, error taxonomy,
                         safe behaviors (never auto-overwrite, always rescue)
docs/storage.md        — layer format, DB schema, recovery playbook
docs/decisions.md      — ADR log, one entry per settled decision
docs/README.md         — index
```

Same documents, two lenses: usage.md carries a "for agents" callout per command;
agent-contract.md is the normative reference. All docs ship in the sdist. README
badges: CI status, Python versions, PyPI link (post-publish).

## 21. The agent-skill repository (repo #2)

Separate public repo (name Q14, rec: `agent-skills-firnpack`):

```
agent-skills-firnpack/
├── .gitmodules         ← submodule: firnpack repo pinned to a tag
├── firnpack/           ← submodule checkout (repo #1)
├── SKILL.md            ← the skill file: full VCS usage contract
└── firnpack.py         ← copy of the module, byte-match guarded
```

The skill installs as exactly two files: `SKILL.md` + `firnpack.py`. The
submodule pins the engine to a tested release; the root copy (not a symlink —
Windows, GitHub web view, and some skill loaders reject symlinks) is verified
byte-identical to the submodule's file by repo #2's CI, which also runs repo
#1's test suite against the copy (drift-guard discipline). Why submodule at all:
the engine stays authoritative and version-pinned while the skill repo stays
minimal and directly installable (owner's stated reason).

## 22. Testing strategy

pytest only. Property/fuzz-lite tests over generated trees and random
commit/branch/merge sequences asserting invariants:

- every layer verifies (layer_id, tree_id, DB cross-check);
- round-trip: extract(pack(tree)) == tree, byte-for-byte, incl. exec bits;
- history from any head visits exactly its ancestors, no dupes;
- LCA correct on generated DAGs;
- merge(A,B) ≡ merge(B,A) tree-wise on non-conflicting random edits;
- conflicted merge leaves no layer but sets pending state; the follow-up
  `commit` yields a two-parent layer with `second_parent == theirs-head` (the
  regression test for the merge-completion blocker);
- `merge` with drift or pending merge refuses with no worktree/DB mutation;
- `merge --abort` restores the pre-merge worktree and clears pending state;
- commit-with-leftover-markers: blocks by default (exit 1), warns with
  `block_unresolved_markers = false`, commits with `--allow-markers`
  (three-state guard, all legs executed);
- flatten persistence chain: `restore --flatten-symlinks` → `status`
  reports pending flattens (human + JSON) → `commit` without
  `--allow-flattened` refuses → with it, commits and records
  `flattened_paths` in meta → follow-up `verify` sees the record;
- hostile-layer hardlink members rejected by the allowlist outright
  (CVE-2026-82049 / hardlink paths of -87910 unrepresentable); the
  *symlink* fallback side of -87910 is covered by the probe/flatten tests
  below;
- patch-level warning fires per the §5 per-minor table — 3.13 below
  3.13.16 warns, 3.14.x warns through 3.14.8 (unconfirmed), 3.15.0 never;
- Windows-unsafe member names (backslash, `C:`, UNC, CON, COM1, NUL, …)
  rejected by the full pre-pass with **zero files written**;
- symlink probe: layer with symlinks + failed probe → refuse with the
  humane message; `--flatten-symlinks` materializes copies and reports
  each path; probe success → links recreated;
- compact: `--dry-run` mutates nothing; `--keep 1` squashes to one layer
  and deletes only unreachable files; merge-bearing kept ranges refuse;
  branches/pins reaching the collapsed set refuse; merge-pending refuses;
  kept-range branches keep their layers and get the merge-base warning;
  `--archive` zip contains the compacted range; reminder fires once per
  threshold crossing (rate-limited);
- compact crash windows (simulated via os._exit hooks): killed after new
  layer files are written but before the DB swap → old history intact,
  new files swept as orphans; killed after the swap but before old-file
  deletion → new history authoritative, old files swept; neither state
  fails `verify`;
- ignore visibility: excluded files counted in `status`, listed with
  `--ignored`, present in JSON; nested `.gitignore` presence warning
  fires;
- compact peak-disk: dry-run reports peak need; free space below peak →
  up-front refusal, zero writes (ENOSPC never reached mid-write);
- repo file count == N_commits + 2;
- no command ever mutates an existing layer file (hash-stable guard);
- crash simulation (os._exit mid-commit) → repo usable, no partial row;
- tar safety: traversal/device-node/hostile-symlink layers rejected;
- mtime-touch without content change → status stays clean (macOS granularity);
- CLI integration via subprocess: exit codes + exact JSON shapes (contract
  tests).

Suite must run < 10 s locally. No fixtures in wheels.

## 23. Performance targets (soft, unmeasured)

For a 500-file ~10 MB tree on weak-laptop-class hardware: `status` < 1 s warm /
< 3 s full; `commit` < 5 s; `history` < 0.1 s; `diff` < 2 s; `verify` (100
layers × 10 MB) is decompression-bound and slow by design — it's the fsck
analog, run rarely. **All numbers are targets to measure during implementation;
shipped docs carry only measured figures with machine profile.** (Q16.)

## 24. Roadmap & versioning

- **M1**: `init, commit, history, status, diff` + tests + packaging skeleton +
  ci.yml. First RC.
- **M2**: `branch, switch, merge, amend, restore, export, verify, compact`
  + branch management → **v1.0.0**: full docs (§20), PyPI publish, skill
  repo published.
- **v1.x**: tags (mutating commands on the reserved `refs` table), per-path
  restore, rename tracking, "sneakernet" pack/unpack of layer subsets.
  (`gc` as a separate command was superseded by `compact`, §9.9.)
- **v2 candidates**: cross-layer dedup/delta if measured need, LLM REPL mode.

Semver; 0.x until 1.0; releases on `v*` tags; CHANGELOG.md by hand.

## 25. Open questions for the owner

- **Q1 — CLI alias — RESOLVED 2026-10-18 (owner)**: yes — both `firnpack`
  and `firn` console scripts register from the same module.
- **Q2 — ignore file — RESOLVED 2026-10-18 (owner, amended)**: ship ignore
  support in v1, and the file is named **`.gitignore`** — direct
  compatibility with the git ecosystem (one ignore file, both tools).
  firnpack reads the root `.gitignore` only (gitignore subset, §9.2) and
  auto-ignores `.firn/` and `.git/`. **Seed set narrowed in v0.11**: only
  `~$*` (Office lock files — unambiguous junk) is seeded, at `init` time
  and only when no `.gitignore` exists; `*- Copy*` / `* conflicted*`
  (OneDrive duplicates) moved to docs as *optional* lines — they also
  match intentional copies a user made ("Informe - Copy.docx"), and
  silent exclusion of legitimate files is the exact failure mode Q7
  rejects. Excluded files stay visible in `status --ignored` (§12) either
  way.
- **Q3 — id syntax — RESOLVED 2026-10-18 (owner, accept-all)**: prefix ids
  (≥ 6 hex, unambiguous) + branch names everywhere an id is expected.
- **Q4 — switch with drift — RESOLVED 2026-10-18 (owner, accept-all)**:
  refuse — no jj-style auto-snapshot.
- **Q5 — rescue packs — RESOLVED 2026-10-18 (owner, accept-all)**: keep —
  auto rescue-layer before destructive restore.
- **Q6 — rename tracking — RESOLVED 2026-10-18 (owner, accept-all)**: none
  in v1; rename = delete+add.
- **Q7 — delete/modify conflicts — RESOLVED 2026-10-18 (owner)**: (a) keep
  the modified file, report the conflict loudly.
- **Q8 — per-path restore — RESOLVED 2026-10-18 (owner, accept-all)**: no
  in v1; `--to DIR` only.
- **Q9 — rebuild boundary — RESOLVED 2026-10-18 (owner, accept-all)**:
  accept — branches/pins lost on `--rebuild-db`; synthetic `main` at newest
  layer, documented.
- **Q10 — build backend — RESOLVED 2026-10-18 (owner, accept-all)**:
  setuptools.
- **Q11 — license — RESOLVED 2026-10-18 (owner)**: **MIT**.
- **Q12 — dev extras — RESOLVED 2026-10-18 (owner, accept-all)**: ok —
  pytest/ruff as dev-only deps.
- **Q13 — CI wheel guard — RESOLVED 2026-10-18 (owner, accept-all)**: keep.
- **Q14 — skill repo name — RESOLVED 2026-10-18 (owner)**:
  `agent-skills-firnpack`.
- **Q15 — skill repo root copy — RESOLVED 2026-10-18 (owner, accept-all)**:
  yes — root copy + byte-match guard against the submodule.
- **Q16 — benchmark honesty — RESOLVED 2026-10-18 (owner)**: shipped docs
  carry only measured numbers. *(Agent-added elaboration, not part of the
  owner's words: each measured number ships with the machine profile that
  produced it.)*
- **Q17a — target machine OS — RESOLVED 2026-10-18**: Windows 11 (upgraded
  from Windows 10). Consequence: v1.0 is gzip-only (zstd = v1.x opt-in) under
  any floor option.
- **Q17b — Python floor — RESOLVED (final) 2026-10-18 (owner)**:
  **3.13.16+** — first release both patched (all three tarfile CVEs on its
  release page) and shipped as a Windows installer. Package floor:
  `requires-python >= 3.13.16` (distro caveat in §18).
- **Q19 — extraction gate below patched levels — RESOLVED 2026-10-18
  (owner)**: **warn** and proceed (per-minor table, §5 layer 4).
- **Q20 — symlink restore posture — RESOLVED 2026-10-18 (owner, ratified
  with amendment)**: probe → refuse → `--flatten-symlinks`, plus the
  agent-continuation contract (refusal message with pre-listed symlinks and
  remedies; `flattened` list in every output mode) — see §5. Rationale:
  silent content substitution is the worst failure mode for a
  non-developer, and the fallback also walks the CVE-2026-87910 path on
  unpatched builds. **Flatten-persistence chain** (§5/§9.2/§12/§22: status
  echo, `--allow-flattened` commit gate, `flattened_paths` meta field,
  `flattened_pending` DB state, tests) — **RATIFIED by owner 2026-10-19**
  (post-close ratification).
- **Q18 — bytes budget — RESOLVED 2026-10-18 (owner)**: accepted (~2–6 MB
  per layer at a 10 MB tree; 0.2–0.6 GB per 100 commits). This directly
  produced the new owner requirement: **`compact`** (§9.9) — squash old
  history, explicit permission only, reminder at a configurable threshold
  (default 50).
- **Q21 — container/base image — RESOLVED 2026-10-18 (owner)**: keep
  `ubuntu-latest`, no Alpine/Void (rationale + tiered evidence in §19).
  Owner ratified the proposal as-is.

---

## Appendix A: requirements traceability

| Brief requirement | Spec anchor |
|---|---|
| name `firnpack` | §18, AGENTS.md §3 |
| pure Python, prefer zero deps | G5, §18 |
| PyPI + CI/CD in public repo | G6, §19 |
| humane docs, humans + LLM agents | G4, §20 |
| single-file module (or CI single-file artifact) | G7, §18, §19 |
| commit / amend / branch / branch-from-commit / merge / history | §8, §9 |
| storage design after deep web research | §4, §5, §15, `research/` |
| second repo: submodule + 2-file skill | §21 |
| "anything else fundamental missing?" | diff, status, switch/restore, verify, export — folded into §8 |

## Appendix B: research citations (load-bearing)

- Fossil: SQLite-backed VCS, artifacts/blobs/deltas, self-checks, ~19y single-file
  repo durability — fossil-scm.org (fileformat.wiki, tech_overview.wiki,
  fossil-is-not-relational.md, selfcheck.wiki).
- Jujutsu: snapshot model, TreeState stat caching, no-stash UX —
  docs.jj-vcs.dev (working-copy, technical/architecture).
- Git packfiles: exist to fix loose-object inode sprawl; packs not deltas are the
  HDD win — git-scm.com/docs/gitformat-pack; github.blog database-internals I.
- Darcs/Pijul patch theory: studied and deliberately rejected — darcs.net
  DifferencesFromGit; pijul.org/model.
- compression.zstd stdlib (PEP 784, 3.14+, optional module) —
  docs.python.org/3/library/compression.zstd; peps.python.org/pep-0784.
- tarfile `filter='data'` extraction safety (3.12+) — docs.python.org tarfile
  extraction filters (PEP 706).
- Trusted publishing (OIDC) + pypa/gh-action-pypi-publish — docs.pypi.org
  (using-a-publisher, security-model); packaging.python.org publishing guide.
- dulwich: pure-Python precedent, porcelain/low-level split, 3.10–3.14 support
  band — dulwich.io, CONTRIBUTING.rst, PyPI metadata.

*(Full notes: `research/`.)*
