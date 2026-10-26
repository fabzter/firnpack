# Real-world verification campaign

How firnpack was exercised like a real user's day-to-day tool: whole
journeys of chained CLI subprocesses against one synthetic project at a
time, on the released artifact and across platforms — checked by
independent filesystem, archive and metadata oracles (the engine's own
`verify` is a self-check and is cross-referenced, never trusted alone).

**Status: verified and published as 1.0.2.** The source, local wheels,
fresh PyPI installations and companion copy passed the checks below.
The symlink journey runs without skips on every campaign target;
native Windows CI also passes. The
[machine-readable results](real-world-results.json) preserve measured
counts and evidence digests, separating historical candidates from the
published engine.

## 1. What is exercised

Five journey modules under `tests/`, driven entirely through real CLI
subprocesses (one process per command, `cwd` pinned to the project):

| Module | Scenario group |
|---|---|
| `tests/test_real_world_daily.py` | Daily lifecycle: multi-session commit/edit/rename/delete loops on a Unicode-named project holding Office lock files, explicit empty / binary / CRLF / BOM content; `init` on a project that already has `.gitignore`; no-op commit vs amend; historical branch, drift-refused switch, pinned commits, pin-to-branch promotion, rename/delete protection, deleted-tip recovery by layer id; config failure modes (wrong-typed values block only writers; malformed INI or invalid UTF-8 blocks everything); the history-size reminder; documented zstd→gzip profile fallback; stored-path boundaries (255-char refusal, >240-char warning, unsplittable ustar name). |
| `tests/test_real_world_merges.py` | Merge journeys: fast-forward then a divergent clean two-parent merge; marker-guard refusal then hand-resolution into a merge commit; `merge --abort` rescue of a half-finished resolution with ignored files preserved; the binary both-changed guard decided by exporting theirs or by an explicit keep-ours override; delete/modify conflicts both directions; marker-shaped example text (packable once, then blocking); a clean merge blocked by marker content; the pending-merge fences (mutators refuse, inspection/export/`restore --to` keep working). |
| `tests/test_real_world_recovery.py` | Recovery and portability per `docs/storage.md §6` and `docs/usage.md`: whole-project copies are self-contained repositories; `restore` rescues dirty work into a real layer parented on the previous head; `export`/`restore --to` write only into absent or empty directories; ignored files are never packed and never deleted; missing/empty/unreadable `glacier.db` rebuilt from in-band layer meta (`verify --rebuild-db`) losing only branch names and pins; corrupt/truncated/missing layers refuse verify and rebuild until the bytes come back; stray `*.tmp`/orphan `*.firn` crash leftovers are reported then swept by the next successful writer; file↔directory transitions across restore and switch. |
| `tests/test_real_world_compact.py` | Compaction and archive journeys: dry-run plans vs executed results; kept-chain rewrite and branch remaps reported; off-chain survivors and garbage collection; refusals (pending merge, branch/pin into the collapsed range, kept merge edges below the cut); `--archive` export contents, naming, and placement; post-compact `verify`, history topology, and layer-file inventory; quarantined-archive recovery rebuilding a parent-closed chain. |
| `tests/test_real_world_platform_journeys.py` | Platform and usage journeys: nested-directory discovery; pinned-head mutations; native file and directory symlinks through both switches and export; exact POSIX permission changes plus independent TAR-mode assertions on every target; a valid imported POSIX-mode layer proves that editing an executable `.sh` on Windows preserves its stored executable bit. No platform skip is used for these journeys. |

Journeys are deliberately chained: a single test may run dozens of CLI
invocations against the same project, the way a user accumulates history —
not a fresh repo per assertion.

Dedicated regression modules now live alongside the journeys —
worktree file↔directory transition regressions
(`tests/test_real_world_worktree_regressions.py`), storage edge cases
(`tests/test_real_world_storage_regressions.py`: archive
exclusivity/containment, canonical tar naming, the frozen-clock
archive-collision unit) and the Python-version ignore-pattern compat
checks (`tests/test_ignore_translate_compat.py`), exact-byte leaf writes
(`tests/test_write_file_binary_mode.py`) and directory-link inference
(`tests/test_symlink_directory_hints.py`). Their results are reported
separately from the 48 CLI journeys.

## 2. Independent oracles

The CLI journeys cross-check results without importing the engine:

- **Filesystem**: `tree_files()` walks the fixture byte-for-byte without
  following symlinks or applying ignore rules; exported trees are compared
  path→bytes against expected maps.
- **SQLite**: `glacier.db` is cross-checked read-only
  (`sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)`) against the
  documented schema — layer rows, parent/second_parent edges, branch heads.
- **Layer archives**: `.firn` files are compared byte-for-byte against
  pre-mutation snapshots and their contents checked through `export`
  trees. Platform journeys additionally inspect TAR member modes and
  link targets independently; the imported-POSIX fixture constructs a
  valid layer without engine imports, then requires rebuild/verify to
  accept its hashes. More detailed header/fault checks live in the
  dedicated storage-regression module.
- **Compact archives**: `--archive` zips are opened with `zipfile` and
  their member list compared against the set of layers whose content was
  deleted.
- **CLI contract**: every invocation checks its exit code and rejects a
  Python traceback on stderr. JSON-mode calls also parse the response;
  human-mode calls check their documented diagnostics.
- **Clock independence**: the repeated CLI archive journey has no
  deliberate sleeps — it detects an archive-name collision only
  probabilistically. The frozen-clock storage unit regression
  (`test_real_world_storage_regressions.py`) is the deterministic
  authority for that failure mode, rather than relying on two commands
  racing a second boundary.
- **Windows path oracles** (cross-hosted runs): a Windows result path
  reported by the engine is converted host-side via
  `World.host_path()` — `PureWindowsPath` semantics, Z:-mapped or
  rooted/relative only, fail-closed on unmapped drives — then resolved
  against the fixture, never compared as a raw string.
- **Native link oracles**: file/directory link creation, `lstat`,
  `readlink`, and reading through each link run through the explicitly
  selected target Python. Wine reparse links are not host POSIX links.
  A missing cross-platform interpreter or wrong target platform is a
  loud error, not a host fallback. These stdlib-only fixture/oracle
  processes are recorded separately from engine CLI calls.

## 3. Targets and identity proof

Captured campaign evidence pins the intended engine bytes before and
after each run: the exact command argv, `sys.version`, `platform`,
`os.name`, the module path and its `sha256`. Version output is recorded;
installed-module versions and, on Windows, 64-bit `nt` execution are
checked as well. The later installed-package runner also checks
distribution metadata. The v1.0.1 baseline engine SHA256 is
`e459b3f0fd7dc08c920d5565cad5bc0a3bcb1fa5cc124d6af0d49e5a7ba43d2e`.

Initial v1.0.1 campaign targets:

| Target | Engine under test | Journeys | CLI calls |
|---|---|---|---|
| Source checkout | `firnpack.py` @ 1.0.1, CPython 3.13.16 (macOS) | 44 | 610 |
| Installed PyPI 1.0.1 | venv `pip install firnpack==1.0.1`, CPython 3.13.16 | 44 | 610 |
| Installed PyPI 1.0.1 | same, CPython 3.14.7 | 44 | 610 |
| Windows under Wine | `firnpack.py` @ 1.0.1, CPython 3.13.16 AMD64 (win32/nt/64-bit) via CrossOver wineloader | 44 | 582 |

Call counts can differ when a failure ends a journey early or when
fixture-level skips trigger on filesystems that cannot create the
long-path fixtures. Skips are reported separately from passes — the
Journeys column counts collected cases and the per-test outcome list
disambiguates. These four rows are historical: the final platform
module brings the journey total to 48. The earlier Windows blanket
symlink skip was removed after a no-skip review; current platform
journeys execute on every target. Counts are not performance measurements.

## 4. Initial results: oracle mistakes vs confirmed defects

Keep these categories separate; the published numbers distinguish them
permanently.

**Confirmed defects on v1.0.1 (native targets — 40 pass / 4 fail each):**

- `rename-branch` reports success when the source branch does not exist.
- An unsplittable ustar name below the 255-char stored limit fails to
  pack, although `docs/storage.md §3` promises a pax `path` record for
  names beyond the ustar split.
- `compact --archive` silently truncates a pre-existing file (or a
  same-second earlier archive) at the target name.
- `compact --archive` accepts a directory inside `.firn` instead of
  refusing before any mutation.

**Wine leg (35 pass / 9 fail):** three product failures (the rename,
unsplittable-name and `.firn`-archive defects above) plus six host-oracle
path-format mistakes — reported Windows paths were interpreted as POSIX
paths or compared literally. Those were harness bugs, not product bugs;
the corrected `World.host_path()` oracle resolves them semantically. The
Wine run did not hit the time-dependent archive-name collision.

**Corrected-oracle re-run (published 1.0.1, 21 cases / 308 calls, 19
pass / 2 fail):** stricter recovery assertions expose a clean
directory-to-file restore that fails instead of succeeding, and a
protected-child refusal that does not name the child path. Separate
synthetic reproductions also proved that a failed switch can delete
previously tracked children, and that switching to a historical tracked
file can silently overwrite an ignored local leaf. The new regressions
must enforce the documented contract, not bless those observations as
acceptable behaviour.

**Corrected Windows installed-package re-run (v1.0.1):** all 44
journeys completed through Windows CPython under Wine after pip
`--target` installation: **39 pass / 5 fail, 603 CLI calls**. Module and
distribution versions both report 1.0.1; the loaded engine matches the
baseline SHA256 above. All six original host-path oracle failures are
gone. The remaining failures cover the rename, unsplittable-name,
administrative archive-placement and two stricter recovery checks.

**Corrections verified before publication.** The work covered
oracle repairs first (host path mapping, read-only SQLite URIs, strict
file↔directory transition contract, deterministic archive-collision and
injected write-failure regressions); canonical long-path writing (ustar
first, pax `path` record only when a name genuinely cannot split);
archive safety (`.firn` containment refusal before any mutation,
exclusive create with collision suffixes, fsync-then-parent ordering,
no overwrite of anything pre-existing, incomplete-archive cleanup);
worktree transition preparation (preflight before deleting tracked
leaves, never recursive deletion near ignored data, honest partial
rollback reporting); and `rename-branch` source existence. Completion
requires the full matrix to pass against the final artifact.

Integration testing also rejected defects in unpublished candidates.
The Python 3.14 full suite exposed slashed ignore rules losing their
matches because `fnmatch.translate` changed its end anchor from `\Z`
to `\z`; a seven-case compatibility module now covers both forms.
Later, an intermediate engine passed all 47 journeys then collected on
native runtimes (before the campaign grew to 48) and 647 full-suite
tests, but failed 18 Windows journeys: a new exclusive write helper used
CRT text mode and expanded LF bytes to CRLF. A direct
Windows payload proved `b"LF\nCRLF\r\n\0\x1a\xff\n"` was changed.
The final helper requests binary mode; six byte/flag regression cases
and the repeated Windows matrix cover that correction. Neither
candidate was published.

The no-skip review also corrected a platform-fixture mistake. The
original POSIX symlink/mode journey already passed on both native
runtimes, but was blanket-skipped for Windows. Forcing that body through
Wine failed before the mode assertions: a host-created POSIX link is
not a Windows reparse point (`os.readlink` in Windows Python reports
WinError 4390). Conversely, a Windows-created link appears host-side as
a specially encoded regular filename. Fixtures and link/mode oracles
now run in the actual target interpreter. The revised journey covers
relative file and forward directory links without skipping; exact
POSIX permissions remain asserted, Windows stored modes are checked
against their documented policy, and an additional imported-POSIX-mode
journey distinguishes carry-forward from re-stat on Windows. The four
platform journeys passed natively and under Windows/Wine without a
product-code change.

## 5. Results: candidates and published release

These are retained results for the earlier 1.0.2 candidate, engine sha256
`90d43459f57f74c3b5ccc25c5f0bfcfb7861e6729f91fc95ed27efca47148268`,
not proof for the corrected engine below. The journey rows use all five
modules; full-suite totals include them
and must not be added to the journey counts.

| Target | Evidence | Passed | Failed | Skipped | CLI calls |
|---|---|---|---|---|---|
| Source, CPython 3.13.16 / macOS | `no-skip-checkout313.json` | 48 | 0 | 0 | 689 |
| Source, CPython 3.14.7 / macOS | `no-skip-checkout314.json` | 48 | 0 | 0 | 689 |
| Source, Windows CPython 3.13.16 / Wine | `no-skip-wine313.json` | 48 | 0 | 0 | 689 |

The complete suite also passed on both native runtimes: **663 passed,
3 skipped** each. The companion copy passed the same full suite with
its engine preloaded and every CLI binding redirected to that copy.
The three skips are two Windows-only executable-bit cases and one
non-UTF-8 filename fixture that APFS cannot create.

A final oracle refinement added directory enumeration through the link,
not merely opening a file through it. All four platform journeys were
rerun on each target: **4 passed, 0 failed, 0 skipped, 51 CLI calls**
each. The source hashes/digests for this later two-line assertion are
recorded separately in the machine-readable summary; the engine bytes
did not change.

The four API regression modules present before the directory-link fix
also passed against each fresh local wheel installation: **79 passed,
1 skipped** per runtime.
Both `firn` and `firnpack` aliases work; installed module and
distribution versions match, and wheel/sdist engine bytes equal the
source hash above.

Fresh PyPI checks had not been run for that earlier candidate.
Earlier `final-*` evidence used the intermediate engine described in §4.
`verified-*` identifies
the earlier candidate before the no-skip harness review; the
`no-skip-*` journey evidence includes the symlink and imported-POSIX-mode
journeys on every local target. These green results did not predict the
native Windows failure below: Wine is not native Windows.

### Native Windows CI found an additional defect

[CI run 40246365170](https://github.com/fabzter/firnpack/actions/runs/40246365170)
tested commit `9bd2bf8`. All five POSIX jobs and the wheel guard passed.
All three native Windows jobs failed the **unskipped** symlink journey:
an exported forward directory link was created before its target
directory existed, so Windows gave it an immutable file-link type.
Reading through it as a directory failed with WinError 5. This is a real
product defect, not a reason to skip or weaken the journey. The official
[`os.symlink` contract](https://docs.python.org/3/library/os.html#os.symlink)
explains why an explicit `target_is_directory` is required for a missing
directory target.

The correction infers directory targets from the complete snapshot,
including chains and directory aliases, and passes the type through
export, worktree synchronization and merge materialization. It neither
follows host links to make that decision nor changes the archive format,
stored targets, write order or rollback policy. Portable call-site
regressions first failed on the earlier engine (**6 failed, 3 passed**);
the corrected engine passes all **41** new cases, including alias
resolution, cycles, case folding and both separator conventions.

Directory inference is also bounded by Windows' documented maximum of
[63 reparse points per path](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points).
An acyclic graph can repeat aliases exponentially even without a cycle;
two call-count regressions failed before adding that bound. They now
pass for both path models without a timing-based test.

The same CI run exposed unrelated fixture assumptions: a text-mode
write produced CRLF while its oracle expected literal LF, and a test
intended to create a POSIX backslash filename instead created a normal
Windows subpath. The fixture now writes exact bytes; only those
POSIX-only filename cases are platform-gated. The symlink journey still
has **no skip or xfail**.

The corrected engine is sha256
`3f7e16565a24030b2f6f5dd74303b78c169924514c6b9b9c2083da8d2d62a782`.
The corrected commit `2f586cd` passed all nine jobs in
[CI run 40248551709](https://github.com/fabzter/firnpack/actions/runs/40248551709),
including all three native Windows jobs. The Windows floor-runtime
suite reports **645 passed, 62 platform-specific skips**; the symlink
journey is not one of those skips.

### Corrected candidate results

| Target | Journeys passed | Failed | Skipped | CLI calls |
|---|---:|---:|---:|---:|
| Native checkout, Python 3.13.16 | 48 | 0 | 0 | 689 |
| Native checkout, Python 3.14.7 | 48 | 0 | 0 | 689 |
| Windows CPython 3.13.16 under Wine | 48 | 0 | 0 | 689 |
| Fresh local wheel, Python 3.13.16 | 48 | 0 | 0 | 689 |
| Fresh local wheel, Python 3.14.7 | 48 | 0 | 0 | 689 |

Every row also executes seven target-interpreter fixture/oracle probes.
All use the same final journey sources, including directory enumeration.
Both native full suites and the independently preloaded companion copy
pass **704 tests, 3 skips** each. All five dedicated API regression
modules pass against both local-wheel installations: **120 passed,
1 skipped** each. The skips have the same platform reasons described
above; none is a symlink-journey skip. Wheel/sdist engine bytes match
the corrected source and both native CLI aliases work.

### Published artifacts and fresh installs

The annotated `v1.0.2` tag points to that exact tested commit.
The [release workflow](https://github.com/fabzter/firnpack/actions/runs/40249186617)
passed all three test jobs, artifact gates and trusted publication.
[1.0.2 is on PyPI](https://pypi.org/project/firnpack/1.0.2/).

New installations downloaded directly from PyPI, independently of the
local wheels, passed the complete journey set:

| Fresh PyPI target | Journeys passed | Failed | Skipped | CLI calls |
|---|---:|---:|---:|---:|
| Native Python 3.13.16 | 48 | 0 | 0 | 689 |
| Native Python 3.14.7 | 48 | 0 | 0 | 689 |
| Windows CPython 3.13.16 under Wine | 48 | 0 | 0 | 689 |

Each run also made seven target-interpreter fixture/oracle probes.
All five dedicated API modules passed against both native PyPI installs:
**120 passed, 1 filesystem-specific skip** each. Both native CLI aliases
work. Module and distribution versions are 1.0.2, and the installed
engine hash matches the tag. The embedded Windows installation uses a
pip `--target` directory and an explicit installed-module shim, not a
venv or a claim that its generated executable wrappers are on PATH.

The actual PyPI wheel and sdist were downloaded separately. Their
archive digests match PyPI's published metadata, and each archive's
engine bytes equal the tag, source and companion copy. Exact digests
are in the machine-readable results.

The companion update is commit `71dae951`, pinned to `v1.0.2`.
Its [CI](https://github.com/fabzter/agent-skills-firnpack/actions/runs/40249829347)
passed the byte-match guard, all **49** skill-contract checks and the
engine suite (**705 passed, 2 platform-specific skips** on Linux).
The independent native companion-copy full-suite result above remains
separate: **704 passed, 3 skips**. These overlapping suites must not be
added to the journey totals.

## 6. Running the harness

The journeys run under pytest with the engine selected by environment
variables — an explicit `FIRNPACK_E2E_COMMAND` replaces the engine
verbatim; plain pytest exercises the checkout's own `firnpack.py`, and
the campaign runner independently pins the intended engine (argv +
sha256 proof) rather than trusting whichever binary resolved:

```sh
# default: the checkout's own firnpack.py under the current interpreter
python -m pytest tests/test_real_world_daily.py tests/test_real_world_merges.py \
    tests/test_real_world_recovery.py tests/test_real_world_compact.py \
    tests/test_real_world_platform_journeys.py

# installed artifact (any interpreter): the command is a JSON argv array
FIRNPACK_E2E_COMMAND='["/path/to/venv/bin/python", "-I", "-m", "firnpack"]' \
FIRNPACK_E2E_PYTHON='["/path/to/venv/bin/python", "-I"]' \
    python -m pytest tests/test_real_world_daily.py tests/test_real_world_merges.py \
        tests/test_real_world_recovery.py tests/test_real_world_compact.py \
        tests/test_real_world_platform_journeys.py

# Windows runtime under Wine: the argv is a full Windows-side command line
# (wineloader → python.exe → module shim), and the platform tells oracles
# to expect Windows paths and .gitignore line endings
FIRNPACK_E2E_PLATFORM=win32 \
FIRNPACK_E2E_COMMAND='["<wineloader>", "Z:\\<path-to>\\python.exe", "-I", "-c", "<module-shim>"]' \
FIRNPACK_E2E_PYTHON='["<wineloader>", "Z:\\<path-to>\\python.exe", "-I"]' \
    python -m pytest tests/test_real_world_daily.py tests/test_real_world_merges.py \
        tests/test_real_world_recovery.py tests/test_real_world_compact.py \
        tests/test_real_world_platform_journeys.py
```

- `FIRNPACK_E2E_COMMAND` must be a JSON string array; it replaces the
  default `[sys.executable, <checkout>/firnpack.py]` verbatim. For an
  embedded or `--target`-installed Windows runtime, the shim inserts the
  site-packages directory into `sys.path` and runs the module via
  `runpy.run_module("firnpack", run_name="__main__")`.
- `FIRNPACK_E2E_PLATFORM` (default `sys.platform`) describes the *target*
  so fixtures pick the right seed `.gitignore` bytes and
  `World.host_path()` converts reported paths correctly.
- `FIRNPACK_E2E_PYTHON` is a JSON interpreter-prefix array for
  stdlib-only fixture/oracle code. It defaults to the pytest interpreter
  only when host and target platforms match. A cross-platform run must
  set it explicitly; the subprocess asserts the target platform before
  executing fixture code.
- The captured campaign runs used a separate runner to save identity
  proof, per-test outcomes, the full CLI call log (cwd, arguments, exit
  code, stdout, stderr and elapsed time), test-source hashes and fixture
  locations. Ordinary pytest invocation does **not** emit that evidence
  JSON or independently verify an installed artifact's hash; verify the
  intended interpreter, module and version before running it.

## 7. Honest limits

- **One writer only.** The format assumes a single writer per
  repository (docs/storage.md §3); the harness does not test concurrent
  commands, and no concurrency guarantee is claimed.
- **Filesystems.** Local fixtures live on the host's filesystem (and
  Wine's Z: mapping); native Windows CI also exercises ordinary Windows
  filesystem operations. Neither establishes NTFS ACL guarantees, real
  HDD rotational behaviour, OneDrive lock-holder timing or every sync
  race. Real antivirus/OneDrive handle contention is not proven here.
- **Power loss.** Injected I/O failures and damaged-layer fixtures are
  not physical power-loss tests. Actual power-loss/UNC durability and
  fsync effectiveness on consumer drives remain out of scope.
- **No benchmark claims.** CLI call counts and wall time are recorded
  per run for audit; no performance number is asserted.
- **Wine is not Windows.** The Wine leg proves the code paths under a
  win32 API, not a native Windows install; registry, real Explorer
  integration and native antivirus interplay are untested.
