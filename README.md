# firnpack

> a glacier for your files — every commit a single packed layer of history.

[![ci](https://github.com/fabzter/firnpack/actions/workflows/ci.yml/badge.svg)](https://github.com/fabzter/firnpack/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/firnpack)](https://pypi.org/project/firnpack/)
![python](https://img.shields.io/badge/python-3.13.16%20%E2%80%93%203.15-blue)

**firnpack** is version control for people who don't want version control
machinery. If you work on small projects, on one computer, and you've ever
wished you could just *save* a version and get back to it later — without
staging areas, remotes, or a `.git` folder that grows thousands of tiny files
on a tired hard drive — firnpack is for you.

Every commit is one file: a compressed, checksummed, immutable archive of your
whole project, stacked on the ones before it. A repository with 100 commits
holds 102 files. That's the whole design.

## Install

Requires Python 3.13.16 or newer.

```
pip install firnpack
```

which registers both `firnpack` and the shorter `firn` command. (From a
clone, `pip install .` does the same; or run the single module directly:
`python firnpack.py …` — the whole engine is one file.)

## Quickstart

```
cd my-project
firnpack init
firnpack commit              # packs everything — that's it
# ...work...
firnpack commit              # a new layer
firnpack status              # what changed since the last pack
firnpack history             # every saved version, newest first
firnpack diff                # what exactly changed, line by line
```

That's the whole daily loop: five commands, no staging, no jargon you have
to look up. Misspelled a commit message? `firnpack amend -m "fixed"` replaces
the head layer without adding a new one.

## Commands

| Command | What it does |
|---|---|
| `firnpack init [PATH]` | create a repository in this folder (or PATH) |
| `firnpack commit [-m MSG]` | pack the working tree into a new layer |
| `firnpack status [--full] [--ignored]` | what changed since the last pack |
| `firnpack history [-n N] [--all] [BRANCH]` | saved versions, newest first |
| `firnpack diff` | line-by-line changes vs the last pack |
| `firnpack branch NAME [FROM]` | create a branch (at HEAD, or at FROM) |
| `firnpack switch REF` | move to a branch, or pin to a layer id |
| `firnpack list-branches` | branches and their heads |
| `firnpack rename-branch OLD NEW` | rename a branch |
| `firnpack delete-branch NAME` | delete a branch (warns if heads orphan) |
| `firnpack merge BRANCH [--abort]` | merge a branch in; abort a pending one |
| `firnpack amend [-m MSG]` | replace the head layer (same parents) |
| `firnpack restore REF [--to DIR]` | bring a layer back over the worktree |
| `firnpack export REF --to DIR` | extract a layer to a fresh directory |
| `firnpack verify` | re-hash every layer; check database consistency |
| `firnpack compact --keep N [--dry-run] [--yes] [--archive DIR]` | squash old layers, reclaim disk |

Every command except `init`, `rename-branch` and `delete-branch` accepts
`--json` for machine-readable output — firnpack is
designed to be operated by people *and* by agents; see
[`docs/agent-contract.md`](docs/agent-contract.md) for the normative
contract.

## Branching and merging

Branches are names for heads; nothing else. `branch` creates one (staying
where you are), `switch` moves you — to a branch, or to a pinned layer id
when you hand it one. `merge` is a plain 3-way merge: clean trees merge into
one layer that remembers both parents; conflicting trees leave git-style
markers in the files and a durable "merge in progress" state that `status`
reports and `merge --abort` clears. A merge never clobbers uncommitted work:
with drift in the worktree it refuses instead.

## Undo, rescue, explore

- **`amend`** replaces the head layer — fix a message or a file without
  stacking another version on top. The replaced layer stays on disk until a
  `compact` collects it.
- **`restore REF`** brings any layer back over the worktree. If you have
  uncommitted work, firnpack **rescues it first** as its own layer and tells
  you — restore never destroys drift.
- **`export REF --to DIR`** extracts a layer into a fresh directory and
  touches nothing else: the zero-risk way to look at an old version.

## Checking the glacier

`firnpack verify` re-hashes every layer against its own filename, cross-checks
the database, and reports dangling references or orphan layer files. It is the
answer to "is my history still sound?" after a power cut, a disk scare, or a
copy between machines. If the database itself is lost or corrupt,
`verify --rebuild-db` reconstructs it from the layers themselves — and says
plainly which branch names could not be recovered.

## History management: compact

Whole-tree layers are the honest price of a glacier you can verify one file
at a time. When history grows past comfort, `compact --keep N` squashes
everything below the newest N layers into one foundation layer and reclaims
the disk.

It is **never automatic and never silent**:

- `--keep N` is required every time, with N at least 1 — no default;
- `--dry-run` prints the full plan (what drops, what rewrites, what gets
  collected, peak disk needed) and changes nothing;
- real runs require `--yes`;
- `--archive DIR` exports every layer whose content is about to disappear
  into a zip before anything is deleted;
- branches that point into the squashed range refuse the compact by name;
  branches that sit on rewritten layers follow the rewrite, and the plan and
  the output say exactly which branch moved where.

A reminder line after `commit` tells you when the glacier crosses 50 layers.
Nothing is ever compacted behind your back.

## Ignoring junk

firnpack reads the standard `.gitignore` file at your project root, so the
ignore rules you already have keep working. If you have none, `init` seeds
one line for you: `~$*` — the hidden lock files office tools leave behind.
Excluded files are never silently hidden: `status` counts them, and
`status --ignored` lists every one.

## Safety, in plain words

- A commit either happens completely or not at all. A power cut mid-pack
  leaves your repository exactly as it was.
- Disk trouble — a full drive, a file held open by OneDrive or antivirus —
  ends in one plain sentence that names the problem and the way back
  (`firnpack verify` confirms your history is intact), never a wall of
  technical text.
- A half-resolved merge can't slip into your history by accident: `commit`
  and `amend` refuse files that still contain conflict markers (`<<<<<<<`),
  merge or no merge, and a merge that left a binary file undecided won't
  finish until you choose a version. Each refusal names the one flag that
  overrides it.
- Every layer carries its own checksum, and the layer's *name* is that
  checksum — tampering or disk rot is detectable by re-reading.
- Extraction of a layer is guarded: a corrupt or hostile archive can never
  write outside your project folder, no matter what paths it claims to
  contain.
- No command destroys uncommitted work: merge, switch and restore all refuse
  on drift, or rescue it first and say so.
- History rewrites (compact) require explicit permission, report everything
  they move, and archive what they delete.

## Documentation

- [`docs/usage.md`](docs/usage.md) — every command: examples, exit codes,
  JSON shapes
- [`docs/agent-contract.md`](docs/agent-contract.md) — the normative contract
  for automated operators
- [`docs/storage.md`](docs/storage.md) — layer format, database schema,
  recovery playbook
- [`docs/decisions.md`](docs/decisions.md) — the decision log
- [`SPEC.md`](SPEC.md) — the full specification; [`research/`](research/) —
  the research behind it

## Status

Released: `firnpack 1.0.0` is on PyPI (wheel + sdist), tagged `v1.0.0`.
The full command surface above is implemented and tested (unit, property,
seeded-fuzz and end-to-end CLI suites; CI across Linux, Windows and macOS;
the published artifact re-verified by installing from PyPI and running the
quickstart). Development continues on `main`; see
[`CHANGELOG.md`](CHANGELOG.md) and [`docs/decisions.md`](docs/decisions.md).

## License

MIT — see [LICENSE](LICENSE).
