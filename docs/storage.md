# firnpack storage

The on-disk format, the database schema, and the recovery playbook.
Normative source: SPEC §4–§6, §13; this document is the operator-facing
view. Everything here is stdlib-readable: `sqlite3`, `tarfile`, `gzip`,
`hashlib`, `json`.

## 1. Repository layout

```
project/
├── .firn/
│   ├── glacier.db        ← one SQLite file: all metadata, all refs
│   ├── layers/
│   │   └── <layer_id>.firn   ← one compressed archive per commit
│   └── config            ← INI: [user] [core] [history] [merge]
└── …worktree…
```

File count: **N_commits + 2** for a history of plain commits (one layer file
per layer row, plus the database and the config), and transient `.tmp`
files during writes. `amend` and the rescue layer of `restore` add layer
rows that stay until `compact` collects them. There is no object
store, no reflog, no packed refs.

`config` keys (all optional with defaults): `user.name` (author string;
defaults to the OS user), `core.profile` (`gzip`), `core.autoname`,
`history.reminder_threshold` (50), `merge.block_unresolved_markers` (true).
Values are literal (no `%` interpolation). `commit`, `amend` and `merge`
validate integer/boolean settings before changing anything; inspection and
export do not depend on those types, so a typo does not block `status` or
`verify`. Invalid INI syntax or invalid UTF-8 still prevents loading the
config: fix the file, or remove it to use defaults. An existing config
that cannot be opened produces a warning and defaults are used.

## 2. Database schema

SQLite with `journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`.
Five tables:

```sql
config  (key TEXT PRIMARY KEY, value TEXT NOT NULL)
layers  (id TEXT PRIMARY KEY, tree_id TEXT NOT NULL,
         parent TEXT REFERENCES layers(id),
         second_parent TEXT REFERENCES layers(id),
         message TEXT NOT NULL, author TEXT NOT NULL,
         created_at TEXT NOT NULL, profile TEXT NOT NULL,
         file_count INTEGER NOT NULL, size INTEGER NOT NULL)
branches(name TEXT PRIMARY KEY,
         head TEXT NOT NULL REFERENCES layers(id), created_at TEXT NOT NULL)
refs    (name TEXT PRIMARY KEY, value TEXT NOT NULL)   -- reserved (tags, v1.x)
worktree(id INTEGER PRIMARY KEY CHECK (id = 1),
         head_kind TEXT NOT NULL CHECK (head_kind IN ('branch','pin')),
         head_branch TEXT, head_pin TEXT,
         head_id TEXT REFERENCES layers(id),
         snapshot TEXT,          -- head layer's tree_id
         stat_cache TEXT,        -- JSON: path -> [size, mtime_ns, mode|null]
         merge_theirs TEXT,      -- pending-merge state
         flattened_pending TEXT, -- JSON list or NULL
         updated_at TEXT NOT NULL)
```

A trigger forbids `worktree.head_id` returning to NULL after the first
commit. `head_pin` is pin-only: on a branch worktree it is always NULL.
Branch names and pins live **only** here — layer files do not carry them.

## 3. Layer format

A layer file is a canonical TAR stream compressed through an explicit
pipeline (`gzip` in v1.0; `zstd` via `compression.zstd` when importable,
with gzip fallback for new layers — old layers always read by their
recorded profile).

Members, in order:

1. `__firnpack_meta__` — JSON, UTF-8, sorted keys, trailing newline:
   `layer_id`, `tree_id`, `parent`, `second_parent`, `message`, `author`,
   `created_at` (UTC ISO-8601), `profile`, `format_version` (1), and
   `flattened_paths` only when present.
2. Every tracked path, lexicographic by POSIX path. Symlinks as tar symlinks
   (mode `0o777`); regular files `0o644`, or `0o755` when the exec bit is
   set (on Windows: the CRT's extension-derived bit; existing entries
   carry their stored mode forward — SPEC §5 item B).

Canonical rules: mtime/uid/gid/uname/gname zeroed; no pax headers for
paths ≤ 100 chars (ustar prefix splitting, then a pax `path` record
beyond); identity comes from content, never from clock or owner.

Write path: `layers/<id>.firn.tmp` → fsync → atomic `os.replace`. A crash
leaves an orphan `.tmp`/`.firn`, which the next `commit`, `amend` or
`compact` sweeps (any `*.tmp` in `layers/`, and any `.firn` with no database
row); orphans are ignorable garbage by construction.
This protocol assumes **one writer per repository**. In particular, the
sweep cannot distinguish another process's in-progress layer from an
orphan: never run concurrent commands against the same `.firn/`.

## 4. The two hashes

- **`tree_id`** — per tracked path in sorted order:
  `sha256(kind_byte ∥ exec_byte ∥ path ∥ NUL ∥ data)`; SHA-256 over the
  concatenation of those digests; truncated to 40 hex.
  `kind_byte` = `0x66` regular / `0x6c` symlink; `exec_byte` = `0x01`/`0x00`.
  The kind byte and the NUL separator close the two collision classes of the
  original formula (path/content boundary; symlink-target vs file-content).
  Ignores message/author/time: identical trees share a tree_id.
- **`layer_id`** — SHA-256 over the canonical commit record: JSON with sorted
  keys and trailing newline containing exactly `format_version`, `tree_id`,
  `parent`, `second_parent`, `message`, `author`, `created_at`; truncated to
  40 hex. Excludes `layer_id` itself (no circular hashing) and `profile`
  (codec-independent identity). The filename **is** this hash — that is what
  makes `verify` possible.

Byte-identical records (same tree, message, author, parents, same second)
mapping to one id is correct dedup, not data loss.

## 5. What the stat cache is (and is not)

`worktree.stat_cache` accelerates `status`: `[size, mtime_ns, mode]` per
path; a match skips re-hashing. It is a cache only — the head layer's
manifest is the committed truth, `--full` bypasses the cache entirely, and
mode is cached so `chmod ±x` shows as drift without a re-hash.

## 6. Recovery playbook

1. **Diagnose**: `firnpack verify` (exit 3 lists every problem: missing
   layer file, corrupt member, id mismatch, tree_id mismatch, dangling
   parent/second_parent/branch head/worktree head, orphan files).
2. **Missing layer file**: history below that layer is unreadable until the
   file returns. Layer files are self-naming and self-verifying — a copy
   from any backup, archive zip (`compact --archive`), or another machine
   can be dropped into `layers/` under its id; `verify` confirms it.
3. **Orphan files**: swept automatically by the next `commit`/`amend`/
   `compact` (any stray `*.tmp`, and any `.firn` with no database row);
   `verify` lists orphan `.firn` files first so nothing vanishes
   unobserved. A refused command never sweeps, and a leftover that cannot be
   removed is reported on stderr and retried next time.
4. **Lost or corrupt database**: layers are self-describing.
   `firnpack verify --rebuild-db` reconstructs the `layers` table from
   each file's in-band `__firnpack_meta__` (id = filename; `file_count`
   = tracked members; `size` = file size), recreates `main` at the
   newest tip layer (one no other layer descends from; newest
   `created_at`, ties broken by id), resets the
   worktree row to that head, and prints exactly which branch names
   were lost. **Branch names and pins are not recoverable from
   layers** — they exist only in the database; re-create them by hand.
   The rebuild refuses (exit 3) when a layer file fails its self-hash
   or a referenced parent file is missing: restore those files from an
   archive or backup first. A missing database file is created
   automatically; an empty or table-less database is re-schematized in
   place. If the file cannot be read as SQLite, back it up and replace it
   with an empty file before rebuilding. If access is denied or it is
   locked, resolve that access problem first. The SQLite `config` and
   reserved `refs` tables survive a readable database's rebuild untouched;
   they cannot be recovered from layers if the old database was lost.
5. **Moving machines**: copy the project folder including `.firn/` — the
   repository is fully self-contained. `verify` on arrival.
