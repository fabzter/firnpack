# firnpack usage

Every command, with examples, exit codes and JSON shapes. Companion documents:
[`agent-contract.md`](agent-contract.md) (normative contract for automated
operators), [`storage.md`](storage.md) (on-disk format and recovery),
[`decisions.md`](decisions.md) (why things are the way they are).

**Exit codes (all commands)**

| Code | Meaning |
|---|---|
| 0 | success (including "nothing to do" outcomes, which say so) |
| 1 | refused or failed with a humane message (`firnpack: …` on stderr) — includes merge conflicts, drift guards, a full disk or a file held by OneDrive/antivirus, a busy database, an unreadable config |
| 2 | command-line usage error (argparse) |
| 3 | integrity failure (`verify`, or any command that hits a corrupt glacier) |

**JSON mode**: every command except `init`, `rename-branch` and `delete-branch`
accepts `--json` and prints exactly one JSON object on stdout (human notes,
when any, go to stderr). Shapes below are the `--json` payloads; optional
keys carry a `// [optional]` comment.

---

## init

```
firnpack init [PATH]
```

Creates `.firn/` in PATH (default: the current directory), seeds a
`.gitignore` with `~$*` when none exists, and writes the config. Never
touches existing files.

```
$ firnpack init
packed glacier ready at /home/me/proj/.firn
no layers yet — pack your work with 'firnpack commit'
```

Exit 1 if the directory is already a repository. Seeds a root `.gitignore`
(one line, `~$*`) only when none exists; afterwards it is an ordinary
tracked file.

## commit

```
firnpack commit [-m MSG] [--allow-markers] [--allow-flattened] [--json]
```

Packs the tracked working tree into a new layer on the current head (branch
or pin). With no `-m`, an honest auto-message is generated from the change
set. Refuses (exit 1) when it would complete a pending merge while
unresolved conflict markers remain, unless `--allow-markers`; consumes a
pending merge by recording `merge_theirs` as the new layer's second parent.

```
$ firnpack commit -m "chapters 1-3 draft"
packed 42 files into layer 9f3c1ab07e2d
  chapters 1-3 draft  (1.2 MB)
```

JSON:

```json
{
  "status": "packed",
  "layer_id": "<40 hex>",
  "tree_id": "<40 hex>",
  "files": 42,
  "size": 1258291,
  "message": "chapters 1-3 draft",
  "maxpath_warning": ["very/long/path.txt"],      // [optional]
  "history_reminder": {"layers": 51, "bytes": 61000000,
                       "hint": "firnpack compact can shrink it"}  // [optional]
}
```

`status: "nothing_to_pack"` (exit 0, no layer written) when the tree is
identical to the head. The `history_reminder` object appears once per
crossing of the configured threshold (default 50 layers).

> **For agents**: a clean `status` is the precondition for `merge` and
> `switch`; `commit` is the only command that consumes a pending merge.

## status

```
firnpack status [--full] [--ignored] [--json]
```

Drift vs the head layer. Fast path uses the stat cache (size+mtime+mode);
`--full` re-hashes everything. While a merge is pending, `status` always
re-hashes (the files the merge wrote are measured against the head layer, so
a marker-filled file is reported as `modified`, never as clean). Ignored files
are counted always and listed with `--ignored`.

JSON:

```json
{
  "head": {"id": "<40 hex>", "kind": "branch", "branch": "main"},
  "clean": false,
  "modified": ["notes.txt"],
  "added": ["new.txt"],
  "missing": ["gone.txt"],
  "ignored_count": 3,
  "ignored": ["~$draft.docx"],
  "nested_gitignore": false,
  "merge_pending": false,
  "flattened_pending": null
}
```

`head` is `null` in an empty repository; `kind` is `"branch"` or `"pin"`
(`branch` key absent when pinned). `flattened_pending` lists symlink paths
awaiting a flatten-confirming commit, or `null`. A present nested
`.gitignore` prints a stderr warning (root-only ignore rules in v1).

## history

```
firnpack history [-n N] [--all] [BRANCH] [--json]
```

Layers newest-first from the current head; `--all` walks every branch head;
a BRANCH argument walks that branch.

JSON:

```json
{
  "commits": [
    {"id": "<40 hex>", "tree_id": "<40 hex>", "parent": "<40 hex>|null",
     "second_parent": "<40 hex>|null", "message": "…", "author": "…",
     "created_at": "2026-10-20T12:00:00+00:00", "tips": ["main"]}
  ],
  "count": 12
}
```

`tips` names the branches whose head is that layer. Merge layers carry both
parents; `history` follows both, visiting each layer once.

## diff

```
firnpack diff [--json]
```

Working tree vs head layer, per path.

JSON:

```json
{
  "entries": [
    {"path": "notes.txt", "status": "modified", "binary": false,
     "hunks": "--- a/notes.txt\n+++ b/notes.txt\n@@ …"},
    {"path": "photo.png", "status": "modified", "binary": true,
     "size": "1024 -> 2048"},
    {"path": "new.txt", "status": "added", "binary": false, "hunks": "…"},
    {"path": "gone.txt", "status": "deleted", "binary": false, "hunks": "…"}
  ]
}
```

Binary entries carry sizes instead of hunks. Empty `entries` = clean.

## branch / switch / list-branches / rename-branch / delete-branch

```
firnpack branch NAME [FROM] [--json]      # create at HEAD or FROM
firnpack switch REF [--json]              # branch name, or layer-id prefix (>= 6 hex)
firnpack list-branches [--json]
firnpack rename-branch OLD NEW
firnpack delete-branch NAME
```

`branch` stays on the current branch. `switch` refuses on drift or pending
merge (exit 1); switching to a layer-id prefix (at least 6 hex characters)
pins the worktree (commits while pinned advance the pin). `delete-branch`
refuses for the branch you are on, and warns on stderr when the deleted
branch's tip is no longer reachable from any branch or from where you are.

JSON — `branch`: `{"name": …, "head": "<40 hex>", "created": true}`.
`switch`: `{"ref": …, "kind": "branch"|"pin", "head": "<40 hex>"}`.
`list-branches`:

```json
{"branches": [{"name": "main", "head": "<40 hex>",
               "created_at": "…", "current": true}]}
```

`rename-branch` and `delete-branch` are human-only (no `--json`).

## merge

```
firnpack merge BRANCH [-m MSG] [--json]
firnpack merge --abort
```

3-way merge of BRANCH into the current head. Refuses (exit 1, nothing
written) on drift or when a merge is already pending. Clean merge writes one
layer with two parents; conflicts leave git-style markers in the files and a
durable pending state (`status.merge_pending`, `merge_theirs` in the DB) —
resolve the markers and `commit`, or `merge --abort` (which rescues any
half-finished resolution as a layer first).

JSON — outcomes:

```json
{"status": "merged", "layer_id": "<40 hex>", "conflicts": []}
{"status": "fast_forwarded", "head": "<40 hex>", "kind": "branch"}
{"status": "already_up_to_date", "head": "<40 hex>"}
{"status": "conflict", "head": "<40 hex>", "theirs": "<40 hex>",
 "conflicts": ["shared.txt"]}
```

`conflict` exits 1 (the state is durable and recoverable, not an error in
the repository). `--abort`: `{"status": "aborted", "head": "<40 hex>",
"rescue_layer": "<id>|null"}`.

## amend

```
firnpack amend [-m MSG] [--json]
```

Replaces the head layer with the same parent(s): new tree from the worktree,
message = `-m` or the old message. The replaced layer stays on disk
(write-once) until a `compact` collects it. Refuses mid-merge and on an
empty repository; identical tree + no `-m` reports `nothing_to_amend`.

JSON: `{"status": "amended", "layer_id": "<40 hex>", "replaced": "<40 hex>"}`
or `{"status": "nothing_to_amend", "layer_id": "<40 hex>"}`.

## restore

```
firnpack restore REF [--to DIR] [--json]
```

Extracts REF over the worktree and moves the head (branch head or pin) to
it. **Rescue-first**: with uncommitted drift, the drift is packed as a
rescue layer before anything is overwritten, and the output names it.
`--to DIR` extracts without touching the worktree or the head (same
semantics as `export`). Refuses mid-merge.

JSON: `{"status": "restored", "layer_id": "<40 hex>",
"rescue_layer": "<40 hex>|null"}` or, with `--to`,
`{"status": "exported", "layer_id": "<40 hex>", "to": "<dir>"}`.

## export

```
firnpack export REF --to DIR [--json]
```

Extracts REF into DIR, which must not exist (fresh-directory-only: the
zero-risk exploration path). Exec bits are restored on POSIX.

JSON: `{"status": "exported", "layer_id": "<40 hex>", "to": "<dir>"}`.

## verify

```
firnpack verify [--rebuild-db] [--json]
```

Re-hashes every layer against its filename, cross-checks in-band meta vs the
database, and checks for dangling parents/second-parents, dangling branch
heads and worktree head, and orphan layer files. Any problem raises an
integrity failure (exit 3) listing every problem.

`--rebuild-db` first reconstructs the database from the in-band meta of
every layer file (SPEC §9.8/§13): branch names and pins live only in the
database and are **not** recoverable — the rebuild creates `main` at the
newest tip layer (one no other layer descends from; newest `created_at`,
ties broken by id) and prints exactly which
branch names were lost. It refuses (exit 3) when a layer file fails its
self-hash or a referenced parent layer file is missing: restore those
files first. An empty or table-less database file is re-schematized in
place; if the file is missing entirely or unopenable, replace it with an
empty file first (`storage.md` §6).

JSON (verify alone, success only): `{"status": "ok", "layers": 12}`.
JSON (`--rebuild-db`): `{"status": "rebuilt", "layers": N,
"main_head": "<40 hex>|null", "lost_branches": ["side"]|null, "verify":
{"status": "ok", "layers": N}}`. `lost_branches` is `null` when the database
was missing, empty or unreadable (the names are unknowable); `[]` only when a
readable database held no other branches.

## compact

```
firnpack compact --keep N [--archive DIR] [--dry-run] [--yes] [--json]
```

Squashes everything below the newest N layers of the current head's
first-parent chain into one foundation layer and reclaims disk. `--keep` is
required (no default exists or may be added) and must be at least 1 — `0`
or a negative number is refused (exit 1, nothing touched), in every mode;
a non-number is a usage error (exit 2). `--yes` is required for real
runs; `--dry-run` plans without mutating.

Rewrites, reports and guards (all visible, never silent):

- kept layers and every reachable off-chain survivor (forks on the kept
  range, stacked branches, kept-merge stubs) are rewritten with new ids and
  re-parented; branch heads at rewritten ids are remapped and listed in
  `branches_remapped`;
- unreachable layers referencing the rewritten/deleted set (amend orphans,
  stubs of collapsed merges) are garbage-collected: archived with the
  dropped range when `--archive` is given, counted in
  `layers_garbage_collected` / `garbage_collected`;
- refusals (exit 1, nothing mutated, `.firn/layers/` byte-identical —
  stray leftovers included): pending merge; another branch head or the pin
  inside the collapsed range; any reachable layer whose ancestry passes
  through collapsed layers (the message names the branch when one points
  there; a merge edge is cleared only by a higher `--keep`); a kept merge
  whose second parent falls below the cut;
- peak-disk need is checked up front and reported in the plan.

JSON — plan:

```json
{"status": "plan", "keep": 2, "layers_total": 60, "layers_dropped": 58,
 "layers_rewritten": 3, "off_chain_rewritten": 1,
 "layers_garbage_collected": 1,
 "branches_remapped": [{"name": "feature", "from": "<40 hex>"}],
 "bytes_reclaimed": 184000000, "peak_disk_needed": 210000000}
```

JSON — executed:

```json
{"status": "compacted", "kept": 3, "dropped": 58, "off_chain_rewritten": 1,
 "garbage_collected": 1,
 "branches_remapped": [{"name": "feature", "from": "<40 hex>",
                        "to": "<40 hex>"}],
 "new_head": "<40 hex>", "archive": "<path>|null"}
```

JSON — nothing to do: `{"status": "nothing_to_compact", "layers": 2}`.
(`--keep N` with N ≥ chain length.)

> **For agents**: never run compact without a human-stated `--keep` and
> `--yes`. Present the `--dry-run` plan first; `branches_remapped` and
> `garbage_collected` are the audit trail.
