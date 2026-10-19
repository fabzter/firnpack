# Research: single-file / low-file-count VCS storage — prior art

> Compiled 2026-10-18 via web research, to back firnpack SPEC.md §4/§5/§11b.
> Each entry: mechanism → what to copy → what to avoid → relevance.

## Fossil SCM (fossil-scm.org)

**Mechanism.** Entire VCS (files, manifests, wiki, tickets) lives in one SQLite
database. All SCM data is stored as immutable **artifacts** in a central `blob`
table keyed by hash (SHA1/SHA3-256); a blob's content may be stored raw (zlib) or as
a **delta** against a parent blob; deltas can chain. Structural artifacts (manifests
= check-ins, tags, etc.) are strict-format text with cards. Transactions are atomic;
self-checks run automatically to protect integrity. Single self-contained executable
+ single-file repository is the core identity.

- **Copy:** single-file-repository *identity*; atomic transactions; automatic
  integrity self-checks; immutable, hash-addressed artifacts; the "repo file can be
  copied/moved/emailed" property. Proves a SQLite-backed VCS survives 15+ years of
  real use (SQLite/Tcl projects self-host on it).
- **Avoid:** storing file *content* inside the DB. Fossil's blob table couples
  content lifetime to DB health, and delta chains make standalone recovery and
  verification more complex. For an HDD-failure-oriented tool we want content in
  append-friendly whole files, decoupled from the DB engine.
- **Relevance:** high — the strongest existence proof for "VCS in one file";
  blueprint for the *manifest*, not the content store.

Sources: fossil-scm.org (fileformat.wiki, tech_overview.wiki,
fossil-is-not-relational.md, selfcheck.wiki, index.wiki).

## Jujutsu / jj (docs.jj-vcs.dev)

**Mechanism.** Snapshot model: the working copy **is** a real commit, auto-amended on
every command that observes changes; no staging area, no stash. TreeState caches
per-file `mtime`+`size` to detect changes cheaply and only rehashes files whose stat
changed. Operation log gives undo; storage-agnostic backends.

- **Copy:** stat-cache change detection (mtime/size) — cheap `status` on slow disks;
  working-copy-as-a-commit idea informs our `worktree` single-row table; the
  auto-message/no-staging UX direction (we adopt no-staging, but *not* auto-snapshots
  — hidden writes are wrong for a non-developer on failing hardware).
- **Avoid:** auto-commit-on-every-command (silent disk writes — directly against the
  OneDrive/HDD constraint), op-log machinery (complexity budget).
- **Relevance:** high for UX and status detection patterns.

Sources: docs.jj-vcs.dev (working-copy, technical/architecture, git-comparison).

## Git packfiles (git-scm.com, github.blog)

**Mechanism.** Loose objects (one small zlib file per object, 2-level dirs) are the
default store; `git gc` consolidates them into `.pack` + `.idx` pairs. Packs exist
precisely because loose objects cause **inode exhaustion and fragmentation** at
scale — the exact failure mode of the user's HDD. Pack entries may be full objects or
deltas; .idx is a fanout + sorted offset table; cruft packs handle unreachable
objects; .rev reverse index speeds lookups.

- **Copy:** the core lesson — *pack, don't scatter*. One big sequential file beats
  thousands of tiny random-access files on mechanical disks. Content-addressed names.
- **Avoid:** delta chains (verification/recovery complexity), multi-file pack+idx+rev
  structure (we get random-access from SQLite manifest instead), reachability GC.
- **Relevance:** high — this is the "why" of the whole project, and the etymology of
  "-pack".

Sources: git-scm.com/docs/gitformat-pack; github.blog "Git's database internals I:
packed object store"; Git 2.41 cruft-pack notes.

## Darcs / Pijul (patch theory)

**Mechanism.** State is defined by a *set of patches*, not snapshots; merge =
commutation (darcs) or categorical pushout (Pijul); conflicts are first-class
 algebraic objects. Pijul represents files as graphs of line-vertices with stable
 identities.

- **Copy:** nothing for v1. (The insight that "conflicts should be explicit, not
  emergent" informs our conflict reporting design.)
- **Avoid:** all of it. Patch algebra is the wrong tool for a single-user, small-
  project VCS; complexity is enormous, implementations historically fragile, and the
  problem it solves (concurrent collaborative commutation) doesn't exist here.
- **Relevance:** moderate — studied mainly to *reject it deliberately* in the spec
  (SPEC §9).

Sources: darcs.net (DifferencesFromGit, Using/Model); pijul.org/model; refpath
research note on patch theory.

## SQLite-as-VCS precedents

- **git-remote-sqlite** (chrislloyd): git objects in one SQLite DB via a remote
  helper — proof that object stores map to SQLite, but still git-shaped.
- **graft** (eidos-space): version-controls a SQLite *database file* itself —
  inverse problem, not applicable.
- **kuro** (greedypanda0): local-first Go VCS, SQLite-backed refs/snapshots/objects
  — same family as our design; validates the shape.
- **WebDVCS** (ricardodeazambuja): distributed VCS in a single SQLite file with
  git-like commands — closest published cousin; corroborates feasibility.
- **fslite** (danmestas): single-file content-addressed FS on a Fossil repo for
  autonomous agents — corroborates the "one compressed repo file" pattern for agent
  use.

## Simple/personal VCS precedents on PyPI & GitHub

- **simple-vcs** (PyPI): Python from-scratch VCS, `.svcs/objects/` plain files +
  JSON — object-sprawl store, i.e. the thing we're avoiding; useful as the
  "naive baseline".
- **jakal-lit** (PyPI): local-first, no remotes, `.lit/` dir with snapshots +
  branches/merge/rebase — validates the no-remote single-user market niche.
- **snapback** (etheuer/GitHub): single-file *Python* VCS, dependency-free, full
  snapshot copies per save — proves the single-file implementation is practical;
  weak storage (full uncompressed copies), no branches.
- **simple-rcs** (PyPI): single-file, dependency-free, reverse-delta, *single
  document* VCS — shows the reverse-delta alternative and its single-file scope
  limit.
- **silo** (divyanshudhruv): "local-first, AI-first" VCS for single users — confirms
  the AI-agent-assisted single-user niche exists and is unserved by git.

**Synthesis.** No existing tool combines: (a) zero-dependency pure-Python single
file, (b) archive-per-commit storage (minimal file count), (c) real branching +
3-way merge. firnpack's slot is genuinely open.
