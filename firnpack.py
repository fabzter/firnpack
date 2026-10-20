#!/usr/bin/env python3
"""firnpack — a glacier for your files: every commit a single packed layer.

Single-user, offline-first version control for small projects on fragile
disks. Each commit ("layer") is one compressed, checksummed, immutable
archive; the repository is a tiny directory holding two files at rest plus
one file per commit. Pure Python, standard library only.

See SPEC.md for the full contract this module implements.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import difflib
import fnmatch
import getpass
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

__version__ = "0.1.0"

FIRN_DIR = ".firn"
META_MEMBER = "__firnpack_meta__"
FORMAT_VERSION = 1
GZIP_LEVEL = 6
MAX_PATH_WARN = 240
MAX_PATH_HARD = 255  # ustar prefix-splitting ceiling
ID_LEN = 40  # hex chars
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Per-minor table of the first Python patch level that fixed the 2026
# tarfile extraction-filter bypasses (CVE-2026-82049/-19672/-87910).
# 3.14 is unconfirmed through 3.14.8 and therefore always warns.
# Evidence: research/tarfile-patch-levels.md.
PATCHED_MINORS = {(3, 12): (3, 12, 15), (3, 13): (3, 13, 16), (3, 15): (3, 15, 0)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS layers (
    id            TEXT PRIMARY KEY,
    tree_id       TEXT NOT NULL,
    parent        TEXT REFERENCES layers(id),
    second_parent TEXT REFERENCES layers(id),
    message       TEXT NOT NULL,
    author        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    profile       TEXT NOT NULL,
    file_count    INTEGER NOT NULL,
    size          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    name       TEXT PRIMARY KEY,
    head       TEXT NOT NULL REFERENCES layers(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worktree (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    head_kind         TEXT NOT NULL CHECK (head_kind IN ('branch','pin')),
    head_branch       TEXT,
    head_pin          TEXT,
    head_id           TEXT REFERENCES layers(id),
    snapshot          TEXT,
    stat_cache        TEXT,
    merge_theirs      TEXT,
    flattened_pending TEXT,
    updated_at        TEXT NOT NULL
);
-- NULL until the first commit, then never NULL again (SPEC 6).
-- SQLite indexes cannot enforce values; a trigger can, cheaply.
CREATE TRIGGER IF NOT EXISTS worktree_head_id_no_null
AFTER UPDATE OF head_id ON worktree
WHEN NEW.head_id IS NULL AND OLD.head_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'worktree.head_id cannot return to NULL');
END;
"""


# ---------------------------------------------------------------- errors


class FirnpackError(Exception):
    """Base class for user-facing firnpack errors (exit code 1)."""


class NotARepoError(FirnpackError):
    pass


class DriftError(FirnpackError):
    pass


class MergeConflict(FirnpackError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__(f"merge conflict in: {', '.join(paths)}")


class IntegrityError(FirnpackError):
    """Repository integrity failure (exit code 3)."""


# ---------------------------------------------------------------- helpers


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename inside it is durable (POSIX; no-op-safe)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace with bounded backoff.

    On Windows, OneDrive or antivirus can hold a handle on the destination
    and raise PermissionError. Retrying with backoff is the humane response;
    nothing is corrupted by a failed replace.
    """
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise FirnpackError(
                    f"could not replace {dst.name}: the file is locked, most likely "
                    f"by OneDrive or antivirus. Nothing was corrupted — "
                    f"please wait a moment and try again."
                ) from None
            time.sleep(0.2 * (2**attempt))


_WARNED_PATCH = False


def _warn_patch_level_once() -> None:
    global _WARNED_PATCH
    if _WARNED_PATCH:
        return
    _WARNED_PATCH = True
    warn = _tarfile_patch_warning()
    if warn:
        print(f"warning: {warn}", file=sys.stderr)


def _tarfile_patch_warning() -> str | None:
    """Return a warning string if this interpreter predates the
    known-patched tarfile filter levels, else None (SPEC §5 layer 4)."""
    if not hasattr(tarfile, "data_filter"):
        raise FirnpackError(
            "this Python's tarfile lacks the extraction safety filter; "
            "please update Python (any 3.10.12+/3.11.4+ has it)"
        )
    vi = sys.version_info
    key = (vi[0], vi[1])
    if key == (3, 14):
        return (
            "tarfile security fixes (gh-157265) are unconfirmed on this "
            "3.14.x interpreter; firnpack's own member checks remain active."
        )
    patched = PATCHED_MINORS.get(key)
    if patched is None:
        return None
    if (vi[0], vi[1], vi[2]) < patched:
        return (
            f"this interpreter ({vi[0]}.{vi[1]}.{vi[2]}) predates the tarfile "
            f"security fixes first shipped in {patched[0]}.{patched[1]}.{patched[2]}; "
            f"firnpack's own member checks remain active."
        )
    return None


def _canonical_json(obj) -> bytes:
    return (
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _seg_regex(seg: str) -> str:
    """fnmatch translation for one path segment ('*' must not cross '/')."""
    t = fnmatch.translate(seg)
    if t.startswith("(?s:") and t.endswith("\\Z"):
        return t[4:-3]
    return t


class _IgnoreRules:
    """The gitignore subset firnpack reads (root .gitignore only, SPEC §9.2).

    Supported: blank lines, '#' comments, trailing-slash dir patterns,
    '*', '?', '[seq]', '!' negation, rooted '/' patterns. Last matching
    rule wins, like git.
    """

    def __init__(self, text: str):
        self.rules = []
        for raw in text.splitlines():
            line = raw.strip("\r")
            if not line.strip() or line.startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            anchored = line.startswith("/")
            if anchored:
                line = line[1:]
            if not line:
                continue
            segs = line.split("/")
            self.rules.append((segs, neg, dir_only, anchored))

    def _rule_matches(self, rule, path: str, is_dir: bool) -> bool:
        segs, neg, dir_only, anchored = rule
        has_sep = len(segs) > 1
        comps = path.split("/")
        if anchored or has_sep:
            full = "/".join(_seg_regex(s) for s in segs)
            rx = re.compile(full + r"\Z")
            if rx.match(path) and (not dir_only or is_dir):
                return True
            if dir_only:
                # A dir pattern matches every path beneath it.
                for i in range(1, len(comps)):
                    if rx.match("/".join(comps[:i])):
                        return True
            return False
        # Basename rule: matches the final component (files) or any
        # component (directories at any depth).
        rx = re.compile(_seg_regex(segs[0]) + r"\Z")
        if is_dir:
            return any(rx.match(c) for c in comps)
        if dir_only:
            return any(rx.match(c) for c in comps[:-1])
        return bool(rx.match(comps[-1]))

    def matches(self, path: str, is_dir: bool) -> bool:
        result = False
        for rule in self.rules:
            if self._rule_matches(rule, path, is_dir):
                result = not rule[1]
        return result


def _count_files(path: Path) -> int:
    total = 0
    for _, _, names in os.walk(path):
        total += len(names)
    return total


# ------------------------------------------------------- layer validation


def _validate_member_names(names: list[str]) -> None:
    """Windows-safe name validation, full pre-pass (SPEC §5 layer 3).

    Runs over the entire member list before any content is used, so a bad
    layer is rejected with zero side effects.
    """
    for name in names:
        if name == META_MEMBER:
            continue
        if "\\" in name:
            raise IntegrityError(f"unsafe member name (backslash): {name!r}")
        if name.startswith("/"):
            raise IntegrityError(f"absolute member name: {name!r}")
        if re.match(r"^[A-Za-z]:", name):
            raise IntegrityError(f"drive-prefixed member name: {name!r}")
        if name.startswith("//"):
            raise IntegrityError(f"UNC member name: {name!r}")
        for comp in name.split("/"):
            if not comp or comp == ".":
                continue
            if comp == "..":
                raise IntegrityError(f"traversal member name: {name!r}")
            base = comp.split(".")[0].upper()
            if base in RESERVED_NAMES or comp.upper() in RESERVED_NAMES:
                raise IntegrityError(f"reserved device name: {name!r}")


def _validate_member_types(members) -> None:
    """Structural member allowlist (SPEC §5 layer 2)."""
    allowed = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE, tarfile.DIRTYPE}
    for m in members:
        if m.name != META_MEMBER and m.type not in allowed:
            raise IntegrityError(
                f"layer contains a {m.type!r} member ({m.name!r}); "
                f"hardlinks and special members are not valid in firnpack layers"
            )


# ---------------------------------------------------------------- hashing


def _hash_tree(
    entries: dict[str, tuple[int, bytes | None, str | None, bool]],
) -> str:
    """tree_id: per-path sha256(kind ∥ exec ∥ path ∥ NUL ∥ content), then
    over the concatenation (SPEC §5). Exec-in-identity: owner decision
    1a (ratified 2026-10-20). Kind byte + NUL separator: collision
    audit 2026-10-20 — PROPOSED, pending owner ratification (B2).

    ``entries`` maps posix path -> (size, content_bytes, symlink_target,
    exec_bit). The kind byte disambiguates a symlink's target bytes from
    a regular file's content; the NUL separator disambiguates the
    path/content boundary ({"a": b"bc"} vs {"ab": b"c"} hashed identically
    without it). Paths cannot contain NUL on POSIX or Windows, so the
    separator is collision-proof. Mode-only changes (chmod ±x) still
    change tree_id — never silently invisible drift.
    """
    digest = hashlib.sha256()
    for path in sorted(entries, key=lambda p: p.encode("utf-8")):
        _, content, target, exec_bit = entries[path]
        data = target.encode("utf-8") if content is None else content
        kind = b"f" if content is not None else b"l"
        prefix = kind + (b"\x01" if exec_bit else b"\x00")
        digest.update(
            hashlib.sha256(prefix + path.encode("utf-8") + b"\x00" + data).digest()
        )
    return digest.hexdigest()[:ID_LEN]


def _disk_exec_bit(st) -> bool:
    """True when the file's execute bit is meaningfully set.

    POSIX: st_mode & 0o100 (chmod drives it). Windows: there is no chmod
    bit — the value is extension-derived and cannot be changed by the
    user, so it participates in identity (same file -> same id) but never
    drifts (exec-in-identity ratified as owner decision 1a, 2026-10-20;
    the Windows carry-forward policy is separately PROPOSED, pending
    ratification as item B).
    """
    return bool(st.st_mode & 0o100)


_WINDOWS = os.name == "nt"


def _file_mode(path, head_mode: int | None = None) -> int:
    """Layer/cache mode for a regular file: 0o755/0o644.

    PROPOSED cross-platform policy (awaiting owner ratification — not yet
    a SPEC note): on POSIX the exec bit is real (chmod) and participates
    in identity. On Windows there is no user-settable exec bit, so:
      - the disk value cannot drift (nothing can change it), and
      - a layer written on another OS must not false-drift here: the
        head's stored mode is carried forward for regular files.
    """
    if _WINDOWS and head_mode is not None:
        return head_mode
    return 0o755 if _disk_exec_bit(path.stat()) else 0o644


def _carry_mode(carry: dict | None, path: str) -> int | None:
    """Carry-forward mode for a regular file — ONLY from regular-file
    manifest entries. A head entry that was a symlink (target set) must
    not donate its 0o777: the path is a regular file now, and an
    all-executable regular file would be applied verbatim on a POSIX
    restore. Returns None when nothing is carried (POSIX, empty repo, or
    a genuinely new path)."""
    if not carry:
        return None
    entry = carry.get(path)
    if entry is None or entry.get("target") is not None:
        return None
    return entry.get("mode")


def _merge3_text(path: str, base: str, ours: str, theirs: str) -> str | None:
    """Line-level 3-way merge of text. Returns merged text, or None when
    regions conflict (the caller writes marker blocks).

    Regions where ours and theirs both changed *base* and the changes
    overlap -> conflict. Non-overlapping changes apply cleanly.
    """
    import difflib

    b_lines = base.splitlines(keepends=True)
    o_lines = ours.splitlines(keepends=True)
    t_lines = theirs.splitlines(keepends=True)

    matcher_o = difflib.SequenceMatcher(None, b_lines, o_lines)
    matcher_t = difflib.SequenceMatcher(None, b_lines, t_lines)
    o_ops = [
        (i1, i2, o_lines[j1:j2])
        for tag, i1, i2, j1, j2 in matcher_o.get_opcodes()
        if tag != "equal"
    ]
    t_ops = [
        (i1, i2, t_lines[j1:j2])
        for tag, i1, i2, j1, j2 in matcher_t.get_opcodes()
        if tag != "equal"
    ]

    def overlaps(a, b):
        return a[0] < b[1] and b[0] < a[1]

    for oo in o_ops:
        for tto in t_ops:
            if overlaps(oo, tto) and oo[2] != tto[2]:
                return None  # conflicting region
    # apply all changes onto base, in reverse index order
    out = list(b_lines)
    for i1, i2, replacement in sorted(o_ops + t_ops, key=lambda op: -op[0]):
        out[i1:i2] = replacement
    return "".join(out)


def _marker_block(base: str, ours: str, theirs: str) -> bytes:
    """Git-compatible conflict markers (SPEC §11) — deliberately the
    standard shape so any resolver already knows it."""
    o_text = "".join(ours.splitlines(keepends=True))
    t_text = "".join(theirs.splitlines(keepends=True))
    block = "<<<<<<< ours\n" + o_text + "=======\n" + t_text + ">>>>>>> theirs\n"
    return block.encode("utf-8")


def _layer_id(record: dict) -> str:
    """layer_id: sha256 over the canonical commit record (SPEC §5).

    The record excludes layer_id itself and profile — identity is
    codec-independent. flattened_paths is not part of identity either:
    two commits with identical trees/messages/parents/time collapse,
    which is correct dedup, not data loss.
    """
    payload = _canonical_json(record)
    return hashlib.sha256(payload).hexdigest()[:ID_LEN]


# ---------------------------------------------------------------- repo


class Repo:
    """The firnpack repository API. The CLI is a thin wrapper over this."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.firn = self.root / FIRN_DIR
        if not (self.firn / "glacier.db").is_file():
            raise NotARepoError(
                f"not a firnpack repository: {self.root} (no .firn/ found; "
                f"run 'firnpack init' first)"
            )
        self.layers_dir = self.firn / "layers"
        self._db = self._connect()
        self._cfg = self._load_config()

    # -- lifecycle helpers

    @staticmethod
    def discover(start: Path | None = None) -> Repo:
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / FIRN_DIR / "glacier.db").is_file():
                return Repo(candidate)
        raise NotARepoError(
            "not a firnpack repository (walked up from "
            f"{cur}; run 'firnpack init' first)"
        )

    def _connect(self) -> sqlite3.Connection:
        db_path = self.firn / "glacier.db"
        for attempt in range(3):
            try:
                conn = sqlite3.connect(db_path, timeout=10)
                break
            except sqlite3.OperationalError as exc:
                if attempt == 2:
                    raise FirnpackError(
                        f"could not open {db_path} ({exc}); the file may be "
                        f"locked by OneDrive or antivirus. Nothing was "
                        f"corrupted — try again shortly."
                    ) from None
                time.sleep(0.5)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _load_config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read_dict(
            {
                "user": {"name": getpass.getuser()},
                "core": {"profile": "gzip", "autoname": "true"},
                "merge": {"block_unresolved_markers": "true"},
                "history": {"reminder_threshold": "50"},
            }
        )
        cfg.read(self.firn / "config")
        return cfg

    @property
    def author(self) -> str:
        return self._cfg.get("user", "name", fallback=getpass.getuser())

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._db.close()

    # -- init

    @staticmethod
    def init(path: Path | None = None, author: str | None = None) -> Repo:
        root = (path or Path.cwd()).resolve()
        firn = root / FIRN_DIR
        if firn.exists():
            raise FirnpackError(f"{root} already contains a .firn/ directory")
        firn.mkdir(parents=True)
        (firn / "layers").mkdir()
        db = sqlite3.connect(firn / "glacier.db")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO worktree (id, head_kind, head_branch, head_pin, "
            "head_id, snapshot, stat_cache, merge_theirs, "
            "flattened_pending, updated_at) VALUES (1, 'branch', 'main', "
            "NULL, NULL, NULL, NULL, NULL, NULL, ?)",
            (_utcnow(),),
        )
        db.commit()
        db.close()
        cfg = configparser.ConfigParser()
        cfg["user"] = {"name": author or getpass.getuser()}
        cfg["core"] = {"profile": "gzip", "autoname": "true"}
        cfg["merge"] = {"block_unresolved_markers": "true"}
        cfg["history"] = {"reminder_threshold": "50"}
        with open(firn / "config", "w", encoding="utf-8") as fh:
            cfg.write(fh)
        # Seed the ignore file only when none exists (SPEC §9.1, Q2):
        # one unambiguous junk pattern for office lock files.
        if not (root / ".gitignore").exists():
            (root / ".gitignore").write_text("~$*\n", encoding="utf-8")
        _fsync_dir(firn)
        return Repo(root)

    # -- worktree state

    def _worktree(self) -> dict:
        row = self._db.execute(
            "SELECT head_kind, head_branch, head_pin, head_id, snapshot, "
            "stat_cache, merge_theirs, flattened_pending FROM worktree "
            "WHERE id = 1"
        ).fetchone()
        keys = (
            "head_kind",
            "head_branch",
            "head_pin",
            "head_id",
            "snapshot",
            "stat_cache",
            "merge_theirs",
            "flattened_pending",
        )
        return dict(zip(keys, row, strict=True))

    def _set_head(self, layer_id: str) -> None:
        wt = self._worktree()
        if wt["head_kind"] == "branch":
            branch = wt["head_branch"]
            existing = self._db.execute(
                "SELECT 1 FROM branches WHERE name = ?", (branch,)
            ).fetchone()
            now = _utcnow()
            if existing:
                self._db.execute(
                    "UPDATE branches SET head = ? WHERE name = ?",
                    (layer_id, branch),
                )
            else:
                self._db.execute(
                    "INSERT INTO branches (name, head, created_at) VALUES (?, ?, ?)",
                    (branch, layer_id, now),
                )
        else:
            self._db.execute(
                "UPDATE worktree SET head_pin = ?, head_id = ?, updated_at = ? "
                "WHERE id = 1",
                (layer_id, layer_id, _utcnow()),
            )

    # -- tracked-set walk

    def _walk(self) -> tuple[list[str], list[str], list[str], bool]:
        """Returns (files, symlinks, ignored_files, nested_gitignore)."""
        ignore = _IgnoreRules(
            (self.root / ".gitignore").read_text(encoding="utf-8", errors="replace")
            if (self.root / ".gitignore").is_file()
            else ""
        )
        files, symlinks, ignored = [], [], []
        nested = False
        stack = [(self.root, "")]
        while stack:
            directory, rel = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError as exc:
                raise FirnpackError(
                    f"cannot read directory {directory}: {exc}"
                ) from None
            for entry in entries:
                name = entry.name
                rel_path = f"{rel}/{name}" if rel else name
                if name == FIRN_DIR and not rel:
                    continue
                if name == ".git":
                    continue
                if entry.is_symlink():
                    if ignore.matches(rel_path, is_dir=False):
                        ignored.append(rel_path)
                    else:
                        symlinks.append(rel_path)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if ignore.matches(rel_path, is_dir=True):
                        ignored.extend(
                            f"{rel_path}/{n}" if rel_path else n
                            for n in _iter_files_below(entry.path)
                        )
                        continue
                    stack.append((entry.path, rel_path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    if name == ".gitignore" and rel:
                        nested = True
                    if ignore.matches(rel_path, is_dir=False):
                        ignored.append(rel_path)
                    else:
                        files.append(rel_path)
        return files, symlinks, ignored, nested

    def _current_tree(self, files, symlinks):
        """Hash every tracked file/symlink. Returns (entries, per_path_stat).

        entries carry the exec bit so tree_id covers mode changes (1a);
        stats store the mode so the stat cache can flag a chmod without
        rehashing (stat fast-path compares [size, mtime, mode]).

        The head manifest is loaded ONCE (Windows carry-forward needs it;
        POSIX never does — the layer read is skipped entirely there).
        """
        carry = None
        if _WINDOWS:
            wt = self._worktree()
            if wt["head_id"]:
                _, carry, _ = self._read_layer(wt["head_id"])
        entries, stats = {}, {}
        for path in files:
            fp = self.root / path
            st = fp.stat()
            data = fp.read_bytes()
            head_mode = _carry_mode(carry, path)
            mode = _file_mode(fp, head_mode)
            entries[path] = (len(data), data, None, mode == 0o755)
            stats[path] = [st.st_size, st.st_mtime_ns, mode]
        for path in symlinks:
            fp = self.root / path
            target = os.readlink(fp)
            entries[path] = (len(target.encode("utf-8")), None, target, False)
            st = fp.lstat()
            stats[path] = [st.st_size, st.st_mtime_ns, 0o777]
        return entries, stats

    # -- layer I/O

    def _layer_path(self, layer_id: str) -> Path:
        return self.layers_dir / f"{layer_id}.firn"

    def _write_layer(self, layer_id: str, meta: dict, files, symlinks) -> int:
        """Canonical tar -> gzip -> atomic durable write (SPEC §13).

        Returns the compressed size on disk.
        """
        tmp = self.layers_dir / f"{layer_id}.firn.tmp"
        meta_bytes = _canonical_json(meta)
        # Windows carry-forward: load the head manifest ONCE (the caller
        # passes paths fresh from _current_tree, whose stats already carry
        # the resolved modes; on POSIX this read never happens).
        carry = None
        if _WINDOWS:
            wt = self._worktree()
            if wt["head_id"]:
                _, carry, _ = self._read_layer(wt["head_id"])
        with (
            open(tmp, "wb") as raw,
            gzip.GzipFile(
                fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0
            ) as gz,
            tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar,
        ):
            ti = tarfile.TarInfo(META_MEMBER)
            ti.size = len(meta_bytes)
            ti.mtime = 0
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti, io.BytesIO(meta_bytes))
            for path in sorted(files, key=lambda p: p.encode("utf-8")):
                fp = self.root / path
                data = fp.read_bytes()
                ti = tarfile.TarInfo(path)
                ti.size = len(data)
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                head_mode = _carry_mode(carry, path)
                ti.mode = _file_mode(fp, head_mode)
                tar.addfile(ti, io.BytesIO(data))
            for path in sorted(symlinks, key=lambda p: p.encode("utf-8")):
                target = os.readlink(self.root / path)
                ti = tarfile.TarInfo(path)
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                ti.mode = 0o777
                tar.addfile(ti)
            raw.flush()
            os.fsync(raw.fileno())
        _replace_with_retry(tmp, self._layer_path(layer_id))
        _fsync_dir(self.layers_dir)
        return self._layer_path(layer_id).stat().st_size

    def _read_layer(self, layer_id: str):
        """Open a layer, validate it, return (meta, manifest, contents).

        manifest maps path -> {size, mode, target}; contents maps path ->
        bytes for regular files. Validation is a full pre-pass: a bad layer
        raises before any content is returned.
        """
        path = self._layer_path(layer_id)
        if not path.is_file():
            raise IntegrityError(f"missing layer file: {path.name}")
        _warn_patch_level_once()
        try:
            tar = tarfile.open(path, "r:gz")  # noqa: SIM115
        except (tarfile.TarError, OSError) as exc:
            raise IntegrityError(f"cannot open layer {layer_id}: {exc}") from None
        with tar:
            members = tar.getmembers()
            _validate_member_types(members)
            _validate_member_names([m.name for m in members])
            meta = None
            manifest, contents = {}, {}
            for m in members:
                if m.name == META_MEMBER:
                    if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                        raise IntegrityError("corrupt meta member")
                    meta = json.loads(tar.extractfile(m).read().decode("utf-8"))
                    continue
                if m.type == tarfile.DIRTYPE:
                    continue
                if m.type == tarfile.SYMTYPE:
                    manifest[m.name] = {
                        "size": 0,
                        "mode": m.mode,
                        "target": m.linkname,
                    }
                else:
                    manifest[m.name] = {
                        "size": m.size,
                        "mode": m.mode,
                        "target": None,
                    }
            if meta is None:
                raise IntegrityError(f"layer {layer_id} has no meta member")
            return meta, manifest, contents

    def _layer_contents(self, layer_id: str, wanted: set[str]) -> dict:
        """Read only the requested regular-file members from a layer."""
        path = self._layer_path(layer_id)
        _warn_patch_level_once()
        try:
            tar = tarfile.open(path, "r:gz")  # noqa: SIM115
        except (tarfile.TarError, OSError) as exc:
            raise IntegrityError(f"cannot open layer {layer_id}: {exc}") from None
        out = {}
        with tar:
            for m in tar.getmembers():
                if m.name in wanted and m.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    out[m.name] = tar.extractfile(m).read()
        return out

    # -- merge machinery (SPEC §10/§11)

    def _ancestors(self, layer_id: str) -> set[str]:
        seen = set()
        stack = [layer_id]
        while stack:
            lid = stack.pop()
            if lid is None or lid in seen:
                continue
            seen.add(lid)
            row = self._db.execute(
                "SELECT parent, second_parent FROM layers WHERE id = ?",
                (lid,),
            ).fetchone()
            if row:
                stack.extend(row)
        return seen

    def _lca(self, ours: str, theirs: str) -> str | None:
        ours_set = self._ancestors(ours)
        stack = [theirs]
        seen = set()
        # walk theirs upward; first node also in ours_set wins — BFS-ish
        # via ordered expansion, good enough for small DAGs
        while stack:
            lid = stack.pop(0)
            if lid is None or lid in seen:
                continue
            seen.add(lid)
            if lid in ours_set:
                return lid
            row = self._db.execute(
                "SELECT parent, second_parent FROM layers WHERE id = ?",
                (lid,),
            ).fetchone()
            if row:
                stack.extend(row)
        return None

    def _merge_trees(self, base, ours, theirs):
        """3-way merge at tree level. Returns (merged, conflicts) where
        merged maps path -> (data_bytes, target_str_or_None, exec_bit)
        and conflicts is a list of paths."""
        all_paths = sorted(set(base) | set(ours) | set(theirs))
        merged, conflicts = {}, []
        for path in all_paths:
            b = base.get(path)
            o = ours.get(path)
            t = theirs.get(path)
            if o == t:  # same on both sides (including both absent)
                if o is not None:
                    merged[path] = o
                continue
            if b == o:  # only theirs changed
                if t is not None:
                    merged[path] = t
                continue
            if b == t:  # only ours changed
                if o is not None:
                    merged[path] = o
                continue
            # both changed differently
            if o is None or t is None:
                # delete/modify: keep the modified side, report conflict
                # (owner decision Q7)
                survivor = o if o is not None else t
                if survivor is not None:
                    merged[path] = survivor
                conflicts.append(path)
                continue
            if o[1] is not None or t[1] is not None or b and b[1] is not None:
                # any symlink involved: no line merge, conflict
                conflicts.append(path)
                merged[path] = o
                continue
            # text merge: line-level 3-way with markers
            b_text = (b[0] if b else b"").decode("utf-8", "replace")
            o_text = o[0].decode("utf-8", "replace")
            t_text = t[0].decode("utf-8", "replace")
            if b"\x00" in o[0] or b"\x00" in t[0]:
                # binary: no auto-merge, conflict
                conflicts.append(path)
                merged[path] = o
                continue
            result = _merge3_text(path, b_text, o_text, t_text)
            if result is None:
                conflicts.append(path)
                merged[path] = (
                    _marker_block(b_text, o_text, t_text),
                    None,
                    o[2],
                )
            else:
                merged[path] = (result.encode("utf-8"), None, o[2])
        return merged, conflicts

    def merge(self, ref: str, message: str | None = None) -> dict:
        """3-way merge of a branch into the current head (SPEC §10).

        Clean merge -> one merge layer (parent = ours, second_parent =
        theirs). Conflicts -> pending-merge state (merge_theirs), markers
        in the worktree, exit path is resolve-then-commit or --abort.
        """
        wt = self._worktree()
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is already pending — resolve it first "
                "(commit or merge --abort)"
            )
        # Step 0: drift guard (SPEC §10)
        st = self.status()
        if not st["clean"]:
            raise DriftError("the working tree has drift — commit it before merging")
        theirs_id = self._resolve_id(ref)
        ours_id = wt["head_id"]
        if theirs_id == ours_id:
            return {"status": "already_up_to_date", "head": ours_id}
        base_id = self._lca(ours_id, theirs_id)
        if base_id == theirs_id:
            return {"status": "already_up_to_date", "head": ours_id}
        if base_id == ours_id:
            # fast-forward (SPEC §10 step 3): move the CURRENT branch's
            # head to theirs (or advance the pin), then sync the
            # worktree. One transaction, no switch — switch would move
            # the worktree pointer but never touch branches.head.
            with self._db:
                if wt["head_kind"] == "branch":
                    self._db.execute(
                        "UPDATE branches SET head = ? WHERE name = ?",
                        (theirs_id, wt["head_branch"]),
                    )
                self._db.execute(
                    "UPDATE worktree SET head_id = ?, head_pin = ?, "
                    "updated_at = ? WHERE id = 1",
                    (theirs_id, theirs_id, _utcnow()),
                )
            self._sync_worktree_to(theirs_id)
            return {
                "status": "fast_forwarded",
                "head": theirs_id,
                "kind": wt["head_kind"],
            }

        # load the three trees
        def tree_of(layer_id):
            _, manifest, _ = self._read_layer(layer_id)
            data = self._layer_contents(layer_id, set(manifest))
            tree = {}
            for path, v in manifest.items():
                if v["target"] is not None:
                    tree[path] = (None, v["target"], False)
                else:
                    tree[path] = (data.get(path, b""), None, v.get("mode") == 0o755)
            return tree

        base_tree = tree_of(base_id)
        ours_tree = tree_of(ours_id)
        theirs_tree = tree_of(theirs_id)
        merged, conflicts = self._merge_trees(base_tree, ours_tree, theirs_tree)

        if not conflicts:
            # DB-first (the v0.4 ratified ordering, identical to the
            # conflict path): set pending state BEFORE materializing, so
            # a crash mid-materialize leaves a coherent pending merge —
            # recoverable via --abort — instead of merged files with no
            # state and an old snapshot (where the next commit would
            # silently drop theirs from the DAG).
            with self._db:
                self._db.execute(
                    "UPDATE worktree SET merge_theirs = ? WHERE id = 1",
                    (theirs_id,),
                )
            self._materialize(merged)
            msg = message or f"merge {ref}"
            result = self.commit(msg)
            return {
                "status": "merged",
                "layer_id": result.get("layer_id"),
                "conflicts": [],
            }

        # conflict path: DB-first pending state, then markers to disk
        # (SPEC §10 step 6 — crash ordering ratified v0.4)
        with self._db:
            self._db.execute(
                "UPDATE worktree SET merge_theirs = ? WHERE id = 1",
                (theirs_id,),
            )
        self._materialize(merged)  # markers included
        return {
            "status": "conflict",
            "head": ours_id,
            "theirs": theirs_id,
            "conflicts": conflicts,
        }

    def merge_abort(self) -> dict:
        """Abort a pending merge: rescue any drift (SPEC 9.7 — including
        half-finished conflict resolutions), restore the head layer to
        the worktree, clear pending state — all without routing the
        rescue through commit() (which would consume merge_theirs as a
        second parent or block on markers)."""
        wt = self._worktree()
        if wt["merge_theirs"] is None:
            raise FirnpackError("no merge is pending")
        ours_id = wt["head_id"]

        # Rescue layer: tree = current worktree, parent = head, no
        # second parent, distinctive auto message. Written directly, NOT
        # via commit() — merge_theirs is still set.
        files, symlinks, _ignored, _nested = self._walk()
        entries, _stats = self._current_tree(files, symlinks)
        tree_id = _hash_tree(entries)
        rescue_written = False
        if tree_id != wt["snapshot"]:
            record = {
                "format_version": FORMAT_VERSION,
                "tree_id": tree_id,
                "parent": ours_id,
                "second_parent": None,
                "message": "auto: drift rescue before merge --abort",
                "author": self.author,
                "created_at": _utcnow(),
            }
            rescue_id = _layer_id(record)
            meta = dict(record)
            meta["layer_id"] = rescue_id
            meta["profile"] = "gzip"
            size = self._write_layer(rescue_id, meta, files, symlinks)
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO layers (id, tree_id, parent, "
                    "second_parent, message, author, created_at, profile,"
                    " file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        rescue_id,
                        tree_id,
                        ours_id,
                        None,
                        record["message"],
                        self.author,
                        record["created_at"],
                        "gzip",
                        len(files) + len(symlinks),
                        size,
                    ),
                )
            rescue_written = True

        self._sync_worktree_to(ours_id)
        with self._db:
            self._db.execute("UPDATE worktree SET merge_theirs = NULL WHERE id = 1")
        return {
            "status": "aborted",
            "head": ours_id,
            "rescue_layer": rescue_id if rescue_written else None,
        }

    def _materialize(self, tree: dict) -> None:
        """Write a merged tree into the worktree (markers included),
        then refresh the stat cache so status reports drift correctly."""
        cache = json.loads(self._worktree()["stat_cache"] or "{}")
        for path in cache:
            fp = self.root / path
            if fp.is_symlink() or fp.is_file():
                fp.unlink()
        stats = {}
        for path in sorted(tree):
            data, target, exec_bit = tree[path]
            fp = self.root / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            if target is not None:
                if fp.exists() or fp.is_symlink():
                    fp.unlink()
                fp.symlink_to(target)
                st = fp.lstat()
                stats[path] = [st.st_size, st.st_mtime_ns, 0o777]
            else:
                fp.write_bytes(data)
                mode = 0o755 if exec_bit else 0o644
                if exec_bit and os.name != "nt":
                    fp.chmod(0o755)
                st = fp.stat()
                stats[path] = [st.st_size, st.st_mtime_ns, mode]
        with self._db:
            self._db.execute(
                "UPDATE worktree SET stat_cache = ?, updated_at = ? WHERE id = 1",
                (json.dumps(stats), _utcnow()),
            )

    # -- commands

    def commit(
        self,
        message: str | None = None,
        *,
        allow_markers: bool = False,
        allow_flattened: bool = False,
    ) -> dict:
        wt = self._worktree()
        files, symlinks, ignored, _nested = self._walk()
        entries, stats = self._current_tree(files, symlinks)
        tree_id = _hash_tree(entries)

        pending_flatten = json.loads(wt["flattened_pending"] or "null")
        merge_theirs = wt["merge_theirs"]

        # Marker guard fires only when completing a pending merge (§9.2).
        if merge_theirs:
            marked = self._scan_markers(files)
            if marked:
                block = self._cfg.getboolean(
                    "merge", "block_unresolved_markers", fallback=True
                )
                if block and not allow_markers:
                    raise MergeConflict(marked)
                print(
                    f"warning: conflict markers remain in: {', '.join(marked)}",
                    file=sys.stderr,
                )
        if pending_flatten:
            block_flat = not allow_flattened and not self._cfg.getboolean(
                "core", "allow_flattened", fallback=False
            )
            if block_flat:
                raise DriftError(
                    "flattened symlinks are pending commit; these paths were "
                    "stored as copies and will enter history as regular "
                    f"files: {', '.join(p['path'] for p in pending_flatten)}. "
                    f"Re-run with --allow-flattened to confirm."
                )

        if tree_id == wt["snapshot"] and not pending_flatten and not merge_theirs:
            return {
                "status": "nothing_to_pack",
                "layer_id": wt["head_id"],
                "files": len(files) + len(symlinks),
            }

        # Path-length warnings (§13): warn past 240, refuse past ustar's 255.
        long_paths = [
            p
            for p in (*files, *symlinks)
            if len(str((self.root / p).resolve())) > MAX_PATH_WARN
        ]
        for p in (*files, *symlinks):
            if len(p) > MAX_PATH_HARD:
                raise FirnpackError(
                    f"path too long to store ({len(p)} chars): {p}. "
                    f"Shorten the file name or enable Windows long paths."
                )

        parent = wt["head_id"]
        now = _utcnow()
        record = {
            "format_version": FORMAT_VERSION,
            "tree_id": tree_id,
            "parent": parent,
            "second_parent": merge_theirs,
            "author": self.author,
            "created_at": now,
        }
        if message is None:
            if not self._cfg.getboolean("core", "autoname", fallback=True):
                raise FirnpackError(
                    "commit needs a message (-m) or autoname = true in config"
                )
            message = self._auto_message(parent, entries)
        record["message"] = message
        # SPEC §5: the canonical commit record — and therefore layer_id —
        # includes the message. Hash only after it is set.
        layer_id = _layer_id(record)

        meta = dict(record)
        meta["layer_id"] = layer_id
        meta["profile"] = "gzip"
        if pending_flatten:
            meta["flattened_paths"] = [p["path"] for p in pending_flatten]
            print(
                "flattened symlinks recorded as regular files: "
                + ", ".join(p["path"] for p in pending_flatten)
            )

        size = self._write_layer(layer_id, meta, files, symlinks)

        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO layers (id, tree_id, parent, "
                "second_parent, message, author, created_at, profile, "
                "file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    layer_id,
                    tree_id,
                    parent,
                    merge_theirs,
                    message,
                    self.author,
                    now,
                    "gzip",
                    len(files) + len(symlinks),
                    size,
                ),
            )
            self._set_head(layer_id)
            self._db.execute(
                "UPDATE worktree SET head_id = ?, snapshot = ?, stat_cache = ?,"
                " merge_theirs = NULL, flattened_pending = NULL, "
                "updated_at = ? WHERE id = 1",
                (layer_id, tree_id, json.dumps(stats), _utcnow()),
            )
        self._sweep_orphans()
        result = {
            "status": "packed",
            "layer_id": layer_id,
            "tree_id": tree_id,
            "files": len(files) + len(symlinks),
            "size": size,
            "message": message,
        }
        if long_paths:
            result["maxpath_warning"] = long_paths
            print(
                f"warning: {len(long_paths)} path(s) longer than "
                f"{MAX_PATH_WARN} characters; restore may need Windows long "
                f"paths enabled.",
                file=sys.stderr,
            )
        self._maybe_remind(result)
        return result

    def _auto_message(self, parent, entries) -> str:
        n = len(entries)
        if parent is None:
            return f"pack {n} files"
        total = sum(e[0] for e in entries.values())
        _, manifest, _ = self._read_layer(parent)
        old_total = sum(v["size"] for v in manifest.values())
        delta = total - old_total
        sign = "+" if delta >= 0 else "-"
        return f"pack {n} files ({sign}{_human(abs(delta))})"

    def _scan_markers(self, files) -> list[str]:
        marked = []
        for path in files:
            data = (self.root / path).read_bytes()
            if b"\0" in data:
                continue
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("<<<<<<<") or line.startswith(">>>>>>>"):
                    marked.append(path)
                    break
        return marked

    def _maybe_remind(self, result: dict) -> None:
        threshold = self._cfg.getint("history", "reminder_threshold", fallback=50)
        count = self._db.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
        row = self._db.execute(
            "SELECT value FROM config WHERE key = 'reminder_last_count'"
        ).fetchone()
        last = int(row[0]) if row else 0
        if count >= threshold > last:
            total = self._db.execute("SELECT SUM(size) FROM layers").fetchone()[0]
            result["history_reminder"] = {
                "layers": count,
                "bytes": total,
                "hint": "firnpack compact can shrink it",
            }
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO config (key, value) "
                    "VALUES ('reminder_last_count', ?)",
                    (str(count),),
                )

    def _sweep_orphans(self) -> None:
        """Remove crash leftovers: stray .tmp files and layer files with no
        DB row (SPEC §13). Referenced layers are never touched."""
        for tmp in self.layers_dir.glob("*.firn.tmp"):
            with contextlib.suppress(OSError):
                tmp.unlink()
        known = {row[0] for row in self._db.execute("SELECT id FROM layers")}
        for layer_file in self.layers_dir.glob("*.firn"):
            if layer_file.stem not in known:
                with contextlib.suppress(OSError):
                    layer_file.unlink()

    def status(self, *, full: bool = False, list_ignored: bool = False) -> dict:
        files, symlinks, ignored, nested = self._walk()
        wt = self._worktree()
        cache = json.loads(wt["stat_cache"] or "{}")

        head_manifest = {}
        if wt["head_id"]:
            _, head_manifest, _ = self._read_layer(wt["head_id"])

        # Stage 1 (fast, stat-cache): size+mtime+mode match -> assume clean
        # (mode in the cache catches chmod even when mtime is untouched).
        # Stage 2 (rehash): stat mismatch or cache miss -> compare actual
        # bytes AND mode against the head layer, so mtime-touch-without-
        # change stays clean (SPEC §22 mtime test) while chmod ±x is
        # detected as drift (owner decision 1a).
        modified, clean = [], []
        suspect = []
        for path in files:
            fp = self.root / path
            try:
                st = fp.stat()
            except OSError:
                continue
            cached = cache.get(path)
            cached_mode = cached[2] if cached and len(cached) > 2 else None
            if cached_mode not in (0o644, 0o755):
                cached_mode = None
                if path in head_manifest:
                    suspect.append(path)
                    continue
            # Stage 1 requires a regular-file cached mode: 0o777 is the
            # symlink sentinel, and legacy 2-element entries have no mode
            # at all. Either falling through to rehash is correct — a
            # same-size/same-mtime file replacing a cached symlink must
            # not pass the fast path (Windows carry-forward would return
            # the sentinel as the disk mode and report clean).
            disk_mode = _file_mode(fp, cached_mode)
            if (
                cached
                and not full
                and cached[0] == st.st_size
                and cached[1] == st.st_mtime_ns
                and (cached_mode is None or cached_mode == disk_mode)
            ):
                clean.append(path)
                continue
            if path not in head_manifest:
                # never committed before: it's "added", not "modified"
                continue
            suspect.append(path)

        if suspect and wt["head_id"]:
            wanted = {p for p in suspect if p in head_manifest}
            head_data = self._layer_contents(wt["head_id"], wanted)
            for path in suspect:
                new = (self.root / path).read_bytes()
                entry = head_manifest.get(path)
                if entry is None or entry.get("target") is not None:
                    # not in head, or head has it as a symlink: changed
                    modified.append(path)
                    continue
                head_mode = entry.get("mode", 0o644)
                disk_mode = _file_mode(self.root / path, head_mode)
                if head_data.get(path) == new and head_mode == disk_mode:
                    clean.append(path)
                else:
                    modified.append(path)

        for path in symlinks:
            if path not in head_manifest:
                # never committed before: "added", not "modified" (same
                # guard as the regular-file loop above)
                continue
            target = os.readlink(self.root / path)
            old_target = head_manifest[path].get("target")
            if old_target == target:
                clean.append(path)
            else:
                modified.append(path)

        tracked = set(files) | set(symlinks)
        # The head manifest is the committed truth; the stat cache is only
        # an acceleration structure and may lag it after an edge-case crash.
        added = sorted(tracked - set(head_manifest))
        missing = sorted(set(head_manifest) - tracked - set(ignored))

        result = {
            "head": self._head_info(),
            "clean": not (modified or added or missing),
            "modified": sorted(modified),
            "added": added,
            "missing": missing,
            "ignored_count": len(ignored),
            "ignored": sorted(ignored) if list_ignored else sorted(ignored),
            "nested_gitignore": nested,
            "merge_pending": wt["merge_theirs"] is not None,
            "flattened_pending": json.loads(wt["flattened_pending"] or "null"),
        }
        if nested:
            print(
                "note: a nested .gitignore exists; firnpack reads only the "
                "root one (v1 limitation)",
                file=sys.stderr,
            )
        if ignored:
            print(
                f"{len(ignored)} file(s) excluded by .gitignore "
                f"(status --ignored lists them)",
                file=sys.stderr,
            )
        return result

    def _head_info(self):
        wt = self._worktree()
        if wt["head_id"] is None:
            return None
        info = {"id": wt["head_id"], "kind": wt["head_kind"]}
        if wt["head_kind"] == "branch":
            info["branch"] = wt["head_branch"]
        return info

    def _resolve_id(self, ref: str) -> str:
        """Resolve a branch name or layer-id prefix to a full layer id."""
        row = self._db.execute(
            "SELECT head FROM branches WHERE name = ?", (ref,)
        ).fetchone()
        if row:
            return row[0]
        if len(ref) >= 6 and all(c in "0123456789abcdef" for c in ref):
            matches = [
                r[0]
                for r in self._db.execute(
                    "SELECT id FROM layers WHERE id LIKE ?", (ref + "%",)
                )
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise FirnpackError(
                    f"layer id prefix {ref!r} is ambiguous ({len(matches)} matches)"
                )
        raise FirnpackError(f"no branch or layer named {ref!r}")

    def branch(self, name: str, from_ref: str | None = None) -> dict:
        """Create a branch at HEAD (or at from_ref: branch name or id
        prefix). Stays on the current branch (SPEC 9.4)."""
        if not name or "/" in name or name.startswith("."):
            raise FirnpackError(f"invalid branch name {name!r}")
        existing = self._db.execute(
            "SELECT 1 FROM branches WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise FirnpackError(f"branch {name!r} already exists")
        wt = self._worktree()
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is pending — resolve and commit, or merge --abort"
            )
        if from_ref is not None:
            head_id = self._resolve_id(from_ref)
        elif wt["head_id"] is not None:
            head_id = wt["head_id"]
        else:
            raise FirnpackError("empty repository: commit something before branching")
        with self._db:
            self._db.execute(
                "INSERT INTO branches (name, head, created_at) VALUES (?, ?, ?)",
                (name, head_id, _utcnow()),
            )
        return {"name": name, "head": head_id, "created": True}

    def switch(self, ref: str) -> dict:
        """Move the worktree to a branch or a pinned layer id.

        Guards (SPEC 9.4): refuses on drift and while a merge is pending.
        Committing while pinned advances the pin (no detached-HEAD shame).
        """
        wt = self._worktree()
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is pending — resolve and commit, or merge --abort "
                "before switching"
            )
        if self.status()["clean"] is False:
            raise DriftError(
                "the working tree has drift — commit it (or restore) before switching"
            )
        row = self._db.execute(
            "SELECT head FROM branches WHERE name = ?", (ref,)
        ).fetchone()
        with self._db:
            if row:
                layer_id = row[0]
                self._db.execute(
                    "UPDATE worktree SET head_kind = 'branch', "
                    "head_branch = ?, head_pin = NULL, head_id = ?, "
                    "updated_at = ? WHERE id = 1",
                    (ref, layer_id, _utcnow()),
                )
                kind = "branch"
            else:
                layer_id = self._resolve_id(ref)
                self._db.execute(
                    "UPDATE worktree SET head_kind = 'pin', head_pin = ?, "
                    "head_branch = NULL, head_id = ?, updated_at = ? "
                    "WHERE id = 1",
                    (layer_id, layer_id, _utcnow()),
                )
                kind = "pin"
            self._sync_worktree_to(layer_id)
        return {"ref": ref, "kind": kind, "head": layer_id}

    def _sync_worktree_to(self, layer_id: str) -> None:
        """Extract the layer's tree into the worktree and refresh the
        snapshot/stat cache. M1 scope: files+symlinks replaced wholesale.

        All layer content is read in ONE pass BEFORE any worktree
        mutation (validation included): a corrupt member aborts the
        switch with the worktree untouched — never half-deleted.
        """
        meta, manifest, _ = self._read_layer(layer_id)
        _warn_patch_level_once()
        regular_paths = {p for p, v in manifest.items() if v["target"] is None}
        contents = self._layer_contents(layer_id, regular_paths)

        cache = json.loads(self._worktree()["stat_cache"] or "{}")
        for path in cache:
            fp = self.root / path
            if fp.is_symlink() or fp.is_file():
                fp.unlink()
        entries = {}
        stats = {}
        for path in sorted(manifest):
            v = manifest[path]
            fp = self.root / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            if v["target"] is not None:
                if fp.exists() or fp.is_symlink():
                    fp.unlink()
                fp.symlink_to(v["target"])
                st = fp.lstat()
                stats[path] = [st.st_size, st.st_mtime_ns, 0o777]
                entries[path] = (
                    len(v["target"].encode()),
                    None,
                    v["target"],
                    False,
                )
            else:
                data = contents[path]
                fp.write_bytes(data)
                mode = v.get("mode", 0o644)
                if mode == 0o755 and os.name != "nt":
                    fp.chmod(0o755)
                st = fp.stat()
                exec_bit = mode == 0o755
                entries[path] = (len(data), data, None, exec_bit)
                stats[path] = [st.st_size, st.st_mtime_ns, mode]
        tree_id = _hash_tree(entries)
        with self._db:
            self._db.execute(
                "UPDATE worktree SET snapshot = ?, stat_cache = ?, "
                "updated_at = ? WHERE id = 1",
                (tree_id, json.dumps(stats), _utcnow()),
            )

    def history(
        self,
        *,
        count: int | None = None,
        all_branches: bool = False,
        branch: str | None = None,
    ) -> dict:
        tips: dict[str, list[str]] = {}
        for name, head in self._db.execute("SELECT name, head FROM branches"):
            tips.setdefault(head, []).append(name)
        wt = self._worktree()
        if branch:
            row = self._db.execute(
                "SELECT head FROM branches WHERE name = ?", (branch,)
            ).fetchone()
            if not row:
                raise FirnpackError(f"no branch named {branch!r}")
            starts = [row[0]]
        elif all_branches:
            starts = [
                head for _, head in self._db.execute("SELECT name, head FROM branches")
            ]
        else:
            starts = [wt["head_id"]] if wt["head_id"] else []

        seen, commits = set(), []
        stack = list(reversed(starts))
        while stack:
            layer_id = stack.pop()
            if layer_id is None or layer_id in seen:
                continue
            seen.add(layer_id)
            row = self._db.execute(
                "SELECT id, tree_id, parent, second_parent, message, author,"
                " created_at FROM layers WHERE id = ?",
                (layer_id,),
            ).fetchone()
            if row is None:
                raise IntegrityError(f"history references missing layer {layer_id}")
            commits.append(
                {
                    "id": row[0],
                    "tree_id": row[1],
                    "parent": row[2],
                    "second_parent": row[3],
                    "message": row[4],
                    "author": row[5],
                    "created_at": row[6],
                    "tips": tips.get(row[0], []),
                }
            )
            stack.append(row[3])
            stack.append(row[2])
            if count and len(commits) >= count:
                break
        return {"commits": commits, "count": len(commits)}

    def diff(self) -> dict:
        wt = self._worktree()
        files, symlinks, _ignored, _nested = self._walk()
        manifest = {}
        if wt["head_id"]:
            _, manifest, _ = self._read_layer(wt["head_id"])

        results = []
        current = {}
        for path in files:
            data = (self.root / path).read_bytes()
            current[path] = data
        for path in symlinks:
            current[path] = os.readlink(self.root / path).encode("utf-8")

        head_paths = set(manifest)
        cur_paths = set(current)
        wanted = set(head_paths)  # deleted files need their old content too
        head_data = self._layer_contents(wt["head_id"], wanted) if wt["head_id"] else {}

        for path in sorted(cur_paths | head_paths):
            if path not in cur_paths:
                old = head_data.get(path)
                if old is None and manifest[path]["target"] is not None:
                    old = manifest[path]["target"].encode("utf-8")
                results.append(self._diff_entry(path, old, b"", "deleted"))
            elif path not in head_paths:
                results.append(self._diff_entry(path, None, current[path], "added"))
            else:
                old = head_data.get(path)
                if old is None and manifest[path]["target"] is not None:
                    old = manifest[path]["target"].encode("utf-8")
                new = current[path]
                if old == new:
                    continue
                results.append(self._diff_entry(path, old, new, "modified"))
        return {"entries": results}

    @staticmethod
    def _diff_entry(path, old, new, status_name) -> dict:
        binary = (old is not None and b"\0" in old) or (
            new is not None and b"\0" in new
        )
        entry = {"path": path, "status": status_name, "binary": binary}
        if binary:
            sizes = []
            for data in (old, new):
                sizes.append(len(data) if data is not None else 0)
            entry["size"] = f"{sizes[0]} -> {sizes[1]}"
            return entry
        old_lines = (
            (old or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        )
        new_lines = (
            (new or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        )
        diff = list(
            difflib.unified_diff(
                old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
            )
        )
        entry["hunks"] = "".join(diff)
        return entry


def _iter_files_below(directory: str):
    for base, _dirs, names in os.walk(directory):
        prefix = os.path.relpath(base, directory)
        for name in names:
            rel = name if prefix == "." else f"{prefix}/{name}"
            yield rel.replace(os.sep, "/")


# ---------------------------------------------------------------- CLI


def _cmd_init(args) -> int:
    repo = Repo.init(Path(args.path) if args.path else None)
    print(f"packed glacier ready at {repo.root / FIRN_DIR}")
    print("no layers yet — pack your work with 'firnpack commit'")
    return 0


def _cmd_commit(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.commit(
            args.message,
            allow_markers=args.allow_markers,
            allow_flattened=args.allow_flattened,
        )
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["status"] == "nothing_to_pack":
        print("nothing to pack, glacier unchanged")
        return 0
    print(f"packed {result['files']} files into layer {result['layer_id']}")
    print(f"  {result['message']}  ({_human(result['size'])})")
    if "history_reminder" in result:
        r = result["history_reminder"]
        print(
            f"glacier holds {r['layers']} layers (~{_human(r['bytes'])}) — {r['hint']}"
        )
    return 0


def _cmd_status(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.status(full=args.full, list_ignored=args.ignored)
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["head"] is None:
        print("no layers yet — pack your work with 'firnpack commit'")
        return 0
    head = result["head"]
    where = head.get("branch") or head["id"]
    if result["clean"]:
        print(f"clean on {where}")
    else:
        for path in result["modified"]:
            print(f"  modified   {path}")
        for path in result["added"]:
            print(f"  new        {path}")
        for path in result["missing"]:
            print(f"  missing    {path}")
    if result["merge_pending"]:
        print("merge in progress — resolve markers, then commit")
    if args.ignored and result["ignored"]:
        for path in result["ignored"]:
            print(f"  ignored    {path}")
    return 0


def _cmd_history(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.history(
            count=args.number, all_branches=args.all, branch=args.branch
        )
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if not result["commits"]:
        print("no layers yet — pack your work with 'firnpack commit'")
        return 0
    for c in result["commits"]:
        tips = " ".join(f"*{t}" for t in c["tips"])
        merge = ""
        if c["second_parent"]:
            merge = f" (merge {c['second_parent'][:8]})"
        print(
            f"{c['id'][:12]}  {c['created_at']}  {c['author']}"
            f"{' ' + tips if tips else ''}{merge}"
        )
        print(f"    {c['message']}")


def _cmd_diff(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.diff()
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for entry in result["entries"]:
        if entry["binary"]:
            print(
                f"Binary file {entry['path']} differs "
                f"({entry['size']}) [{entry['status']}]"
            )
        else:
            print(entry["hunks"], end="")
    return 0


def _cmd_merge(args) -> int:
    repo = Repo.discover()
    try:
        if args.abort:
            result = repo.merge_abort()
        elif args.ref is None:
            print(
                "firnpack: merge needs a branch name (or --abort)",
                file=sys.stderr,
            )
            return 1
        else:
            result = repo.merge(args.ref, args.message)
    except MergeConflict:
        # merge() reports conflicts in its result dict, not via the
        # exception — this except is defensive only
        raise
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        if result["status"] == "conflict":
            return 1
        return 0
    status = result["status"]
    if status == "already_up_to_date":
        print(f"already up to date (head {result['head'][:12]})")
        return 0
    if status == "fast_forwarded":
        print(f"fast-forwarded to {result['head'][:12]}")
        return 0
    if status == "merged":
        print(f"merged into layer {result['layer_id'][:12]}")
        return 0
    if status == "aborted":
        print(f"merge aborted — back at {result['head'][:12]}")
        return 0
    if status == "conflict":
        print(
            f"merge conflict in {len(result['conflicts'])} file(s):",
            file=sys.stderr,
        )
        for path in result["conflicts"]:
            print(f"  {path}", file=sys.stderr)
        print(
            "fix the marked files (<<<<<<< ... >>>>>>>), then commit. "
            "Or: firnpack merge --abort",
            file=sys.stderr,
        )
        return 1
    print(f"merge: {status}")
    return 0


def _cmd_branch(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.branch(args.name, args.from_ref)
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"branch {result['name']} created at {result['head'][:12]}")
    print("(still on the current branch — switch when ready)")
    return 0


def _cmd_switch(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.switch(args.ref)
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    kind = "branch" if result["kind"] == "branch" else "pinned layer"
    print(f"switched to {kind} {result['ref']} (head {result['head'][:12]})")
    return 0


def _cmd_list_branches(args) -> int:
    repo = Repo.discover()
    try:
        rows = repo._db.execute(
            "SELECT name, head, created_at FROM branches ORDER BY name"
        ).fetchall()
        wt = repo._worktree()
    finally:
        repo.close()
    if args.json:
        payload = {
            "branches": [
                {
                    "name": n,
                    "head": h,
                    "created_at": c,
                    "current": n == wt["head_branch"],
                }
                for n, h, c in rows
            ]
        }
        print(json.dumps(payload, indent=2))
        return 0
    for name, head, _created in rows:
        marker = "* " if name == wt["head_branch"] else "  "
        print(f"{marker}{name:20s} {head[:12]}")
    return 0


def _cmd_rename_branch(args) -> int:
    repo = Repo.discover()
    try:
        row = repo._db.execute(
            "SELECT 1 FROM branches WHERE name = ?", (args.new,)
        ).fetchone()
        if row:
            repo.close()
            print(f"firnpack: branch {args.new!r} already exists", file=sys.stderr)
            return 1
        with repo._db:
            repo._db.execute(
                "UPDATE branches SET name = ? WHERE name = ?",
                (args.new, args.old),
            )
            wt = repo._worktree()
            if wt["head_branch"] == args.old:
                repo._db.execute(
                    "UPDATE worktree SET head_branch = ? WHERE id = 1",
                    (args.new,),
                )
    finally:
        repo.close()
    print(f"renamed {args.old} -> {args.new}")
    return 0


def _cmd_delete_branch(args) -> int:
    repo = Repo.discover()
    try:
        wt = repo._worktree()
        if wt["head_kind"] == "branch" and wt["head_branch"] == args.name:
            repo.close()
            print(
                f"firnpack: cannot delete the branch you are on ({args.name!r})",
                file=sys.stderr,
            )
            return 1
        row = repo._db.execute(
            "SELECT head FROM branches WHERE name = ?", (args.name,)
        ).fetchone()
        if not row:
            repo.close()
            print(f"firnpack: no branch named {args.name!r}", file=sys.stderr)
            return 1
        with repo._db:
            repo._db.execute("DELETE FROM branches WHERE name = ?", (args.name,))
    finally:
        repo.close()
    print(f"deleted branch {args.name} (layers remain on disk)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firnpack",
        description="a glacier for your files — every commit a single "
        "packed layer of history.",
    )
    parser.add_argument(
        "--version", action="version", version=f"firnpack {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a repository here")
    p_init.add_argument("path", nargs="?", default=None)
    p_init.set_defaults(func=_cmd_init)

    p_commit = sub.add_parser("commit", help="pack the working tree")
    p_commit.add_argument("-m", "--message", default=None)
    p_commit.add_argument(
        "--allow-markers",
        action="store_true",
        help="commit despite leftover conflict markers",
    )
    p_commit.add_argument(
        "--allow-flattened",
        action="store_true",
        help="confirm storing flattened symlinks as files",
    )
    p_commit.add_argument("--json", action="store_true")
    p_commit.set_defaults(func=_cmd_commit)

    p_status = sub.add_parser("status", help="show drift vs the head layer")
    p_status.add_argument(
        "--full", action="store_true", help="rehash every file (skip the stat cache)"
    )
    p_status.add_argument(
        "--ignored", action="store_true", help="list files excluded by .gitignore"
    )
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_status)

    p_history = sub.add_parser("history", help="show layers, newest first")
    p_history.add_argument("-n", "--number", type=int, default=None)
    p_history.add_argument("--all", action="store_true", help="walk every branch head")
    p_history.add_argument("branch", nargs="?", default=None)
    p_history.add_argument("--json", action="store_true")
    p_history.set_defaults(func=_cmd_history)

    p_diff = sub.add_parser("diff", help="working tree vs head layer")
    p_diff.add_argument("--json", action="store_true")
    p_diff.set_defaults(func=_cmd_diff)

    p_merge = sub.add_parser("merge", help="merge a branch into HEAD")
    p_merge.add_argument("ref", nargs="?", default=None)
    p_merge.add_argument("-m", "--message", default=None)
    p_merge.add_argument("--abort", action="store_true", help="abort a pending merge")
    p_merge.add_argument("--json", action="store_true")
    p_merge.set_defaults(func=_cmd_merge)

    p_branch = sub.add_parser("branch", help="create a branch")
    p_branch.add_argument("name")
    p_branch.add_argument("from_ref", nargs="?", default=None)
    p_branch.add_argument("--json", action="store_true")
    p_branch.set_defaults(func=_cmd_branch)

    p_switch = sub.add_parser("switch", help="move to a branch or layer id")
    p_switch.add_argument("ref")
    p_switch.add_argument("--json", action="store_true")
    p_switch.set_defaults(func=_cmd_switch)

    p_list = sub.add_parser("list-branches", help="branches and their heads")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list_branches)

    p_rename = sub.add_parser("rename-branch", help="rename a branch")
    p_rename.add_argument("old")
    p_rename.add_argument("new")
    p_rename.set_defaults(func=_cmd_rename_branch)

    p_delete = sub.add_parser(
        "delete-branch", help="delete a branch (warns if heads orphan)"
    )
    p_delete.add_argument("name")
    p_delete.set_defaults(func=_cmd_delete_branch)

    return parser


def main(argv=None) -> int:
    # Windows pipes default to a legacy codepage (cp1252 etc.); a commit
    # message like "añadido ✓" would crash the CLI *after* a successful
    # pack. UTF-8 everywhere, best-effort (non-tty streams may not have
    # reconfigure on exotic builds).
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MergeConflict as exc:
        print(
            f"firnpack: merge conflict — resolve these files first: "
            f"{', '.join(exc.paths)}",
            file=sys.stderr,
        )
        return 1
    except DriftError as exc:
        print(f"firnpack: {exc}", file=sys.stderr)
        return 1
    except IntegrityError as exc:
        print(f"firnpack: integrity failure: {exc}", file=sys.stderr)
        return 3
    except FirnpackError as exc:
        print(f"firnpack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
