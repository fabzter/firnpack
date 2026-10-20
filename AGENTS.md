# firnpack — AGENTS.md

> Initial context document. Captures the founding brief verbatim-intent plus structured
> decisions extracted from it. This file is the source of truth for why firnpack exists
> and what it is — new agents (human or LLM) should read this before anything else.

---

## 1. Why firnpack exists (the problem)

The target user has a very old, brittle laptop with a slow, likely-failing **mechanical
hard drive**, running **Windows 11 (upgraded from Windows 10)**. A non-removable OneDrive
process makes recurrent reads/writes against that already-fragile HDD. The user is **not a developer**: all coding happens in VS Code with
Copilot (DeepSeek as the LLM), no collaboration, no multi-user workflows, no centralized
servers. Smallish personal projects. Concretely: **having tons of tiny files on disk is
what fails on this machine** — a classic loose-object store (like `.git/objects/`)
aggravates exactly the failure mode we need to avoid.

## 2. What firnpack is

A **simple, decentralized-ish, single-user version control system** written as a
**pure Python module** — in the spirit of [dulwich](https://github.com/jelmer/dulwich)
(dulwich = pure-Python Git), but designed from the start for:

- **HDD-friendly storage**: minimal file count. Leading design candidates (to be settled
  in SPEC.md): one compressed archive file per commit + metadata store, a single SQLite
  database, or another approach validated by web research. Research phase is part of the
  build.
- **Small-project scale**: no performance theater. Correctness and simplicity first.
- **Single-user reality**: no merge conflicts between humans, no network, no remotes in
  v1. "Decentralized-ish" = the repo is self-contained (repository file/folder can be
  copied, moved, or shared by hand — copy it and you have everything).

## 3. Name & identity (user-provided, fixed)

- **Project/package/software name**: `firnpack`
- **CLI entry points**: `firnpack` + `firn` alias (both register from one module)
- **Etymology** (user's own words, keep with the project):
  > Firn is the mountaineer's word for old snow — snow that has survived at least one
  > summer, compacted by its own weight into dense, granular layers on its way to
  > becoming glacial ice. Each layer is a season: self-contained, compressed, and
  > preserved in the order it fell. Drill a core through firn and you can read history
  > stratum by stratum.
  >
  > That is exactly what this software does to your work. Every commit in firnpack is a
  > single packed archive — one file, compressed, checksummed, immutable — stacked on the
> ones before it. There is no sprawling tree of objects and no thousands of tiny files
  > scattered across a fragile disk. Just layers.
  >
  > "Pack" does double duty. It names the archive itself — one packed file per snapshot,
  > in the spirit of git's packfiles — and it names the act: taking a whole working
  > directory and consolidating it into something compact enough to live on hardware that
  > cannot afford sprawl.
  >
  > Firnpack, then: history kept the way a glacier keeps it — layer upon layer,
  > compressed, whole, and readable years later from a single core.

  *(Clarification, kept outside the quote: a git packfile is a many-object
  archive; firnpack's layer is one-file-per-commit. The metaphor is the
  glacier's strata, not git's packfile mechanics.)*

- **Tagline** (PyPI description + GitHub): *"a glacier for your files — every commit a
  single packed layer of history."*

## 4. Hard requirements (user-stated, non-negotiable)

1. **Name**: `firnpack`.
2. **Pure Python**, preferably **zero external dependencies** (stdlib only) unless a
   dependency clearly simplifies things. (dulwich-style purity; unlike dulwich we are not
   implementing Git's wire formats, so this is easier.)
3. **Published to PyPI**, with a **CI/CD pipeline** in the public repo.
4. **Clearly and humanely documented in the repo, for both humans and LLM agents** —
   docs are a first-class deliverable, not an afterthought.
5. **Single-file: either the module itself is one file, or the CI produces a single-file
   artifact** — IF POSSIBLE without sacrificing validity/correctness. Zero-dep pure
   Python with a single .py in the wheel is idiomatic and achievable.
6. **Second repo (agent skill)**: uses firnpack as a **git submodule**, shipping exactly
   two files: (a) a skill file documenting full usage of the VCS, (b) the Python code
   itself. Submodule chosen so skills can be installed directly from the repo.
7. **Must support**: commit, amend, branching, branch-from-specific-commit, merging,
   history (log). Open question raised by user: *is anything else fundamental missing?*
   (Research answer: yes — diff/status/checkout/tag; see SPEC.)
8. **History management (owner, 2026-10-18)**: a `compact` command that squashes
   old commits — always available, never automatic, explicit permission only
   (`--keep N`, `--yes`), with an optional archive export before deletion and a
   reminder threshold (default 50 layers). Owner rule: "not without the user or
   agent permission, but readily available."

## 5. Research mandate (part of the build)

The user explicitly asked for **deep web research** on the single-file / archive-per-commit
storage question. Findings are recorded in `research/` and must inform SPEC.md:

- `research/prior-art.md` — Fossil (SQLite VCS), Jujutsu (snapshot model), git
  packfiles, darcs/Pijul (patch theory, what to avoid), SQLite-VCS precedents, simple
  personal VCS projects.
- `research/vcs-semantics.md` — minimal command set + merge semantics.
- `research/packaging-ci.md` — trusted publishing (OIDC), single-module packaging,
  testing matrix, wheel layout for a one-file package.
- `research/tarfile-patch-levels.md` — the 2026 tarfile extraction-filter CVEs,
  per-release patch status (incl. the 3.14.8 ambiguity), Windows installer
  availability — the evidence base for the 3.13.16+ floor (SPEC §18) and the
  §5 extraction-safety layers.

## 6. Repository / workspace layout (planned)

```
firnpack/
├── AGENTS.md          ← this file: initial context, constraints, identity
├── SPEC.md            ← product + storage format + command spec (living doc)
├── research/          ← web-research findings backing SPEC decisions
├── firnpack.py        ← (planned) the entire implementation, one file
├── tests/             ← pytest suite
├── pyproject.toml     ← minimal single-module packaging, [project.scripts] entry
├── .github/workflows/ ← ci.yml (lint + test matrix), release.yml (trusted publish)
├── README.md          ← humane docs, humans + LLM agents
├── LICENSE            ← MIT (owner decision 2026-10-18)
└── docs/              ← usage, agent-contract, storage, decisions, index
```

## 7. Constraints & principles derived from the brief

- **Target hardware shapes the design**: HDD, OneDrive churning I/O, weak CPU. Fewer,
  larger files; sequential I/O; no background daemons; keep total repo file count tiny.
  Pinned invariant: repo layout is O(commits) — **N_commits + 2 files** — with
  each layer a whole compressed tree (~2–6 MB per 10 MB tree; ~0.2–0.6 GB per
  100 commits — the accepted budget, Q18, reclaimed via `compact`).
- **Target user shapes the UX**: non-developer. Git-like jargon only where it helps
  (commit, branch, merge are fine). No staging area in v1 (mental overhead) — but amend
  must exist per requirements. Error messages must be humane and actionable.
- **LLM-agent friendliness is a requirement, not a nicety** — the skill file in repo #2
  must let a Copilot agent operate firnpack reliably. Machine-readable output (e.g.
  `--json`) should exist wherever it's cheap.
- **Simplicity over performance theater**: no delta encoding in v1 unless research
  demands it; whole-file snapshots per commit are acceptable at this scale, possibly
  with per-commit compressed archives. Dedup across commits is a v2+ question.
- **Verification before assertion**: memory lesson — verify factual claims against
  primary sources; never assert capabilities of Python stdlib/PyPI/CI without checking
  current docs.

## 8. Status & next steps

- [x] AGENTS.md — initial context saved (this file)
- [x] SPEC.md v0.14 — review closed 2026-10-18; v0.14 amendments ratified
      2026-10-20 (item B: Windows exec-mode carry-forward, §5; item B2:
      amended tree_id formula kind∥exec∥path∥NUL∥data, §5; §9.9 refusal
      conditions re-specified reference-based: ancestry refusals, kept-head
      branch remap, garbage collection of unreachable survivors, deferred FK
      enforcement). Earlier owner decisions: Python floor 3.13.16+,
      `.gitignore` compatibility, MIT, `firn` alias, keep-modified conflicts,
      gzip-only v1.0, `compact` command (Q18), blocking marker guard,
      tarfile defense-in-depth
- [x] Review session — CLOSED 2026-10-18 (owner sign-off): all §25
      questions resolved (Q21 container and Q16 benchmark honesty ratified
      post-close), including Q20 (symlink probe→refuse→flatten, with the
      agent-continuation output contract) and the housekeeping batch
      (Q3–Q15, accept-all). SPEC stamped v0.13.
- [x] firnpack.py implementation — M1 + M2 complete: all 16 v1.0 commands
      (init, commit, status, history, diff, branch, switch, list-branches,
      rename-branch, delete-branch, merge, amend, restore, export, verify,
      compact) plus `verify --rebuild-db`; 225 tests green (unit + property
      + seeded fuzz + e2e CLI + docs/guard suites), ruff clean
- [x] CI/CD — ci.yml (8-leg matrix + wheel guard) green on main;
      release.yml (trusted publishing) fired: **v1.0.0 on PyPI**
      (2026-10-20; published artifact re-verified by clean install)
- [x] README upgrade + docs/ tree (SPEC §20) — shipped with the
      JSON-contract guard suite
- [x] Skill repo — github.com/fabzter/agent-skills-firnpack (public):
      SKILL.md + byte-match-guarded firnpack.py copy, submodule pinned
      at v1.0.0, CI runs the engine suite against the copy

## 9. Working rules for agents in this repo

- **DECIDE items stay open** until the owner explicitly confirms — never close a
  review checkpoint unilaterally to satisfy a reminder.
- **No new runtime dependencies** without owner sign-off (G5: stdlib-only).
- **Spec changes go through the owner review loop** — never mark a section
  settled that the owner hasn't ratified.
- **Docs ship with behavior**: every documented JSON shape is a test contract;
  docs and code change together.
- **File edits use `read`/`edit`/`write` tools** — no scripted bulk edits via
  shell (owner directive 2026-10-18; the failure history lives in this §9
  bullet itself and the session record, not in SPEC).
- **research/ is real and verified** — four docs exist and back SPEC decisions;
- **No AI-collaboration mentions anywhere in the repo** — nothing in
  commits, logs, messages, docs, code comments, changelogs, or release
  notes may reference AI, LLMs, agents, or AI-assisted authorship. The
  repo's public face is human-authored work only (owner rule 2026-10-19).
  Exception: `AGENTS.md` and `docs/agent-contract.md`, whose *purpose* is
  agent operation — they describe how agents use the tool, but never
  attribute authorship of the work to AI.
  Scope, as ratified by owner decision C (2026-10-20): the ban targets
  *authorship attribution* — nothing may claim or imply the work was
  produced by AI. Mentions that describe who *operates* the tool (the
  G4 operability goal, operator callouts in usage docs, conflict
  markers designed for machine resolution) are product requirements
  and are permitted in any file; the two exempt files remain the
  normative operator documents. When in doubt: attribution =
  forbidden, operation = permitted.

Last updated: 2026-10-20 — drafted during the founding session; kept current
through the spec review close (v0.13), the v0.14 amendments, M2 completion,
and the v1.0.0 release (PyPI live, skill repo published).
