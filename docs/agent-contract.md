# firnpack agent contract (normative)

This document is the contract firnpack guarantees to automated operators.
`usage.md` lists the same shapes per command; where they disagree, this
document wins. Every shape here is pinned by the e2e suite — a change to any
of them is a versioned contract change, not a refactor.

## 1. Process contract

- One JSON object on **stdout** with `--json`, nothing else; human notes and
  warnings go to **stderr**. Never parse stderr.
- Exit codes: `0` success (including explicit no-op outcomes),
  `1` refused/failed with a humane message, `2` CLI usage error,
  `3` integrity failure. A refusal (1) means **nothing was mutated** unless
  the command's contract says otherwise (only `restore` and `merge --abort`
  may add a rescue layer while refusing to destroy work — and they report
  it).
- `firnpack: ` prefixes error text on stderr. Match on exit code first,
  message text second.
- Discovery: commands operate on the repository containing the working
  directory (walk-up). There is no global state, no daemon, no lock server.
- Single-writer: firnpack assumes one process at a time per repository. Do
  not parallelize commands against one `.firn/`.

## 2. Read-only set

These never mutate the repository or worktree, in any state:
`status`, `history`, `diff`, `verify`, `export`, `list-branches`,
`compact --dry-run`. Agents may run them freely, including in loops.

## 3. Error taxonomy

| Class | Exit | Recognize | Operator response |
|---|---|---|---|
| drift guard | 1 | message names drift / "uncommitted" | commit, restore, or export the drift first; never discard on a user's behalf |
| pending-merge guard | 1 | "merge is pending" | resolve markers + `commit`, or `merge --abort` |
| merge conflict | 1 | JSON `status: "conflict"` with `conflicts: [paths]` | edit markers, then `commit` (records both parents); or `--abort` |
| compact refusal | 1 | message names a branch (merge/delete it, or raise `--keep`), or "still referenced" with no branch (a merge edge: raise `--keep` only; deleting a branch cannot clear it), or the kept-merge second-parent condition | raise `--keep`; delete the named branch only when the message names one; re-plan |
| permission refusal | 1 | "pass --yes", "--keep must be" | stop; obtain explicit human parameters |
| integrity failure | 3 | `firnpack: integrity failure:` + problem list | stop writes; run `verify`; follow `storage.md` recovery |
| usage error | 2 | argparse text | fix the invocation |

Refusals are **atomic**: the repository and worktree are byte-identical
after them (rescue-layer additions excepted, and reported).

## 4. Safe behaviors (MUST)

1. **Never auto-overwrite.** Check `status.clean` before `merge`, `switch`,
   or `restore` without `--to`. If not clean, surface the drift to the human
   or rescue it via a commit; never delete drift silently.
2. **Always rescue.** `restore` and `merge --abort` pack uncommitted drift
   as a rescue layer *before* overwriting and report `rescue_layer`. After
   either, if `rescue_layer` is non-null, tell the human where their work
   went (`export <id> --to DIR` retrieves it).
3. **Compact only with human parameters.** `--keep N` and `--yes` must come
   from the human, per invocation. Always show the `--dry-run` plan first.
   After execution, report `branches_remapped` and `garbage_collected` —
   they are the audit trail of what moved and what was collected.
4. **Never silent.** Reporting fields agents must surface when present:
   `branches_remapped`, `off_chain_rewritten`, `garbage_collected`,
   `rescue_layer`, `history_reminder`, `maxpath_warning`,
   `flattened_pending`, `status.nested_gitignore`, `status.ignored_count`.
5. **Verify after external events.** After copying a repository, after a
   power loss, or before trusting an old clone: run `verify`. Exit 3 lists
   every problem; do not write to a repository that fails verify.
6. **Treat ids as opaque.** Layer ids are 40-hex content hashes; prefixes
   resolve uniquely or error ("ambiguous"). Never construct or truncate ids
   yourself.
7. **Pins are heads.** `switch <layer-id>` pins; commits while pinned advance
   the pin (no detached-HEAD failure mode). `status.head.kind` tells you
   which world you are in.
8. **Recovery is explicit.** `verify --rebuild-db` is the only command
   that reconstructs the database; it names every branch it drops and
   refuses on corrupt or parent-missing layers. Report `lost_branches`
   to the human; never present a rebuild as lossless.

## 5. JSON schemas (normative top level)

```
commit    {status: packed|nothing_to_pack, layer_id?, tree_id?, files?,
           size?, message?, maxpath_warning?, history_reminder?}
status    {head: {id, kind, branch?}|null, clean, modified[], added[],
           missing[], ignored_count, ignored[], nested_gitignore,
           merge_pending, flattened_pending}
history   {commits: [{id, tree_id, parent, second_parent, message, author,
           created_at, tips[]}], count}
diff      {entries: [{path, status: added|modified|deleted, binary,
           hunks?|size?}]}
merge     {status: merged|fast_forwarded|already_up_to_date|conflict, …}
          --abort → {status: aborted, head, rescue_layer}
amend     {status: amended|nothing_to_amend, layer_id, replaced?}
restore   {status: restored|exported, layer_id, rescue_layer?, to?}
export    {status: exported, layer_id, to}
verify    {status: ok, layers}            (failures exit 3, no JSON)
          --rebuild-db → {status: rebuilt, layers, main_head,
          lost_branches[], verify: {status, layers}}
compact   plan      {status: plan, keep, layers_total, layers_dropped,
                     layers_rewritten, off_chain_rewritten,
                     layers_garbage_collected, branches_remapped:
                     [{name, from}], bytes_reclaimed, peak_disk_needed}
          compacted {status: compacted, kept, dropped, off_chain_rewritten,
                     garbage_collected, branches_remapped:
                     [{name, from, to}], new_head, archive}
          idle      {status: nothing_to_compact, layers}
branch    {name, head, created}
switch    {ref, kind: branch|pin, head}
list-br.  {branches: [{name, head, created_at, current}]}
```

Arrays are sorted (paths lexicographically); `null` is used for absent
parents/second parents; timestamps are UTC ISO-8601 with offset.

## 6. Merge protocol for agents

1. `status` → must be clean and `merge_pending: false`, else stop.
2. `merge BRANCH --json`:
   - `merged` / `fast_forwarded` / `already_up_to_date` → done; report head.
   - `conflict` → for each path in `conflicts`: read the file, resolve
     `<<<<<<<`/`=======`/`>>>>>>>` blocks (git-compatible by design), write
     the resolution, then `commit -m "merge BRANCH"` — the pending state
     supplies the second parent automatically.
   - Giving up: `merge --abort` (rescues partial resolutions; report
     `rescue_layer`).
3. Never commit conflict markers: `commit` refuses them by default
   (`--allow-markers` exists for the human who insists).

## 7. Things firnpack will never do

- No network, no remotes, no background daemons, no auto-compaction, no
  automatic garbage collection outside an explicit `compact`, no silent
  history rewrite, no staging area, no force flags.
