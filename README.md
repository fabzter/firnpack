# firnpack

> a glacier for your files — every commit a single packed layer of history.

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
to look up. Misspelled a commit message? `firnpack commit` again with
`-m "fixed"` — the auto-message keeps you honest, `-m` makes it yours.

## Why it's shaped like this

- **One file per commit.** Traditional version control sprays thousands of
  small files across your disk; on old, slow, failing drives that's the
  failure mode that hurts. firnpack writes exactly one new file per commit,
  plus a small manifest that tracks where everything is.
- **Self-contained history.** Copy the project folder — `.firn/` and all —
  anywhere: to another machine, a USB stick, a backup drive. It just works.
  There is no server, no login, nothing to configure.
- **Layers, not objects.** Firn is old snow that has survived a summer,
  compacting under its own weight into dense layers. Drill a core through
  it and you can read history, stratum by stratum. Every commit here is
  one of those layers: whole, compressed, and verifiable on its own.

## Commands

| Command | What it does |
|---|---|
| `firnpack init` | create a repository in this folder |
| `firnpack commit [-m MSG]` | pack the working tree into a new layer |
| `firnpack status` | what changed since the last pack (see `--ignored`) |
| `firnpack diff` | line-by-line changes vs the last pack |
| `firnpack history [-n N]` | saved versions, newest first |

*(branching, merging, restoring and more arrive with v1.0 — see the roadmap)*

## Ignoring junk

firnpack reads the standard `.gitignore` file at your project root, so the
ignore rules you already have keep working. If you have none, `init` seeds
one line for you: `~$*` — the hidden lock files office tools leave behind.
Excluded files are never silently hidden: `status` counts them, and
`status --ignored` lists every one.

## Safety, in plain words

- A commit either happens completely or not at all. A power cut mid-pack
  leaves your repository exactly as it was.
- Every layer carries its own checksum, and the layer's *name* is that
  checksum — tampering or disk rot is detectable by re-reading.
- Extraction of a layer is guarded: a corrupt or hostile archive can never
  write outside your project folder, no matter what paths it claims to
  contain.

## Status

Early development (M1: init, commit, status, history, diff). The full
specification lives in [`SPEC.md`](SPEC.md); the design rationale and the
research behind it in [`research/`](research/).

## License

MIT — see [LICENSE](LICENSE).
