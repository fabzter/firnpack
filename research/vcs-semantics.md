# Research: minimal VCS command semantics for a single-user system

> Compiled 2026-10-18, backing SPEC.md §7/§8. Question: beyond the brief's list
> (commit, amend, branch, branch-from-commit, merge, history), what is
> *fundamental*?

## Load-bearing commands implied by the brief's own features

You cannot implement the required list correctly without these as internal (and
frankly, user-facing) operations:

1. **status** — required to make `commit`/`switch` safe (drift detection). jj's
   TreeState model (mtime/size stat-cache → rehash on change) is the cheap
   mechanism. Without status, "switch refuses when dirty" (git-safety 101) is
   impossible.
2. **diff** — merge, amend, and history-review are all diff-shaped; a VCS whose
   merge produces conflicts but can't show *what* changed is unusable. Also the
   primary tool for LLM agents reviewing work.
3. **checkout/switch** — branching without the ability to move the worktree is
   decorative. `restore` (checkout old commit, read-only-ish) + `switch` (move
   between branches) are the minimum pair. git split these (checkout → switch/
   restore) for good UX reasons; we follow the split naming.
4. **init** — trivial but required.
5. **verify (fsck analog)** — on failing hardware this is *more* important than in
   git: whole-file content-addressed layers make it cheap (re-hash, no deltas).
   Fossil's automatic self-checks are the precedent.

## Evaluated, and the verdict

| Command | Verdict | Reasoning |
|---|---|---|
| revert/restore file(s) | **restore in v1** | undo is the safety net for a non-developer; `export --to DIR` is the zero-risk variant |
| tag | **defer v2** | not in brief; named refs table reserved in schema for forward compat |
| stash | **no** | no-staging model + "refuse to switch when dirty" makes stash unnecessary; jj eliminates it the same way |
| rebase | **no** | single-user, merge suffices; history rewriting = amend only |
| cherry-pick | **no** | out of scope, complexity > value here |
| worktrees (parallel) | **no** | single-user, single machine |
| gc/prune | **v2** | layers accumulate harmlessly at this scale; unreachable ≠ dangerous without reflogs |
| ignore file | **yes, named `.gitignore`** (owner decision 2026-10-18) | full git compatibility — one ignore file, both tools; office/OneDrive junk (`~$*`, `* conflicted*`) makes it necessary on the target machine |
| clone/remote/push/pull | **no (v1)** | non-goal; "decentralized-ish" = self-contained folder, copyable by hand |

## Merge semantics — why parents matter

3-way merge needs the common ancestor: with only parent chains you can find the LCA
by BFS over `parent`/`second_parent` — this is why merge commits MUST record
`second_parent` (SPEC §6 `layers` table). One parent is not enough: fast-forward
merges would lose which side was merged, and future merges would mis-pick the base.
Confirmed mechanism in git (OstensibleAncestor/merge-base) and jj; darcs/Pijul need
no base but pay with patch theory — rejected.

- Merge markers stay git-compatible (`<<<<<<<`/`=======`/`>>>>>>>`) — every LLM has
  been trained on resolving these; custom markers would *reduce* agent capability.
- Binary conflicts: no marker possible; report hashes, let user pick. (v1: no
  per-path restore; extract-and-copy workflow.)
- Rename tracking: v1 none (delete+add); git needed years to get it right; defer.

## Recommended minimal command table (→ SPEC §7)

```
core:        init  commit  history  status  diff
branching:   branch  switch  merge  amend
safety:      restore  export  verify
management:  list-branches  rename-branch  delete-branch  show  root
deferred v2: tag  gc  per-path restore  remotes/sneakernet
```

That's 17 user-facing commands (v1.0 — `compact` added 2026-10-18, owner
requirement from the accepted bytes-per-commit budget); a non-developer's
daily loop is 5 of them (commit/status/diff/history/switch).
