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
import copy
import difflib
import fnmatch
import getpass
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import time
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

__version__ = "1.0.2"

FIRN_DIR = ".firn"
META_MEMBER = "__firnpack_meta__"
# a conflict marker line: starts a line (after \n, \r\n or \r, or at the
# start of the file) with seven < or > characters (SPEC §11)
_MARKER_LINE = re.compile(rb"(?:\A|[\r\n])(?:<<<<<<<|>>>>>>>)")
FORMAT_VERSION = 1
GZIP_LEVEL = 6
MAX_PATH_WARN = 240
MAX_PATH_HARD = 255  # stored relative-path character limit
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
    """Leftover conflict markers block a commit. `pending_merge` says whether
    the commit was completing a conflicted merge (the original case) or an
    ordinary commit (SPEC §9.2: the guard covers every commit)."""

    def __init__(self, paths, *, pending_merge=True):
        self.paths = paths
        self.pending_merge = pending_merge
        what = "merge conflict in" if pending_merge else "conflict markers in"
        super().__init__(f"{what}: {', '.join(paths)}")


class BinaryConflict(FirnpackError):
    """A merge left a binary file undecided and the commit would keep only
    OUR version (owner decision 2026-10-23: refuse until the user acts)."""

    def __init__(self, paths):
        self.paths = paths
        super().__init__(f"undecided binary conflict in: {', '.join(paths)}")


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


def _write_file(fp: Path, data: bytes) -> None:
    """Create a leaf with exclusive create: a path that raced in after
    preflight is never overwritten silently — EEXIST propagates to the
    caller's rollback."""
    fd = os.open(
        fp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o666,
    )
    owned_identity = None
    try:
        st = os.fstat(fd)
        owned_identity = (st.st_dev, st.st_ino)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"could not finish writing {fp}")
            view = view[written:]
        os.close(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        if owned_identity is not None:
            with contextlib.suppress(OSError):
                st = os.lstat(fp)
                if (st.st_dev, st.st_ino) == owned_identity:
                    fp.unlink()
        raise


def _directory_symlinks(targets: dict[str, str | None]) -> set[str]:
    """Infer directory-link types from the complete snapshot, not disk order.

    Windows links keep their creation-time type even when a forward
    target appears later. Resolve directory aliases and chains in the
    snapshot without following host links or changing stored targets.
    """

    def key(path):
        return os.path.normcase(path).replace(os.sep, "/")

    links = {
        key(path): target for path, target in targets.items() if target is not None
    }
    if not links:
        return set()
    directories = {""}
    for path in targets:
        parts = path.split("/")
        directories.update(key("/".join(parts[:i])) for i in range(1, len(parts)))
    result = set()
    for path, target in targets.items():
        if not target or os.path.isabs(target) or os.path.splitdrive(target)[0]:
            continue
        parts = path.split("/")[:-1]
        pending = list(reversed(target.replace(os.sep, "/").split("/")))
        active = {key(path)}
        # Windows permits at most 63 reparse points in one path.
        # Bound expanding aliases too, not only recursive link cycles.
        follows_left = 63
        while pending:
            component = pending.pop()
            if isinstance(component, tuple):
                active.remove(component[0])
                continue
            if component in ("", "."):
                continue
            if component == "..":
                if not parts:
                    break
                parts.pop()
                continue
            parts.append(component)
            current = key("/".join(parts))
            if current in links:
                following = links[current]
                if (
                    not follows_left
                    or current in active
                    or os.path.isabs(following)
                    or os.path.splitdrive(following)[0]
                ):
                    break
                follows_left -= 1
                active.add(current)
                parts.pop()
                pending.append((current,))
                pending.extend(reversed(following.replace(os.sep, "/").split("/")))
            elif current not in directories:
                break
        else:
            if key("/".join(parts)) in directories:
                result.add(path)
    return result


def _symlink(fp: Path, target: str, *, target_is_directory: bool = False) -> None:
    """Create a symbolic link, or say plainly why not.

    On Windows creating a link needs Developer Mode or an administrator
    account; without it the OS refuses with "a required privilege is not
    held". That is not a disk or sync problem, so it must not be reported
    as one. (The designed probe-and-flatten flow is deferred past 1.0 —
    SPEC §5; this only makes the failure honest.)"""
    try:
        fp.symlink_to(target, target_is_directory=target_is_directory)
    except (FileExistsError, IsADirectoryError, NotADirectoryError):
        raise
    except (OSError, NotImplementedError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise FirnpackError(
            f"cannot create the symbolic link {fp.name!r} -> {target!r}: "
            f"{reason}. On Windows this needs Developer Mode (Settings → "
            f"Privacy & security → For developers) or an administrator "
            f"account; otherwise use a machine that allows symbolic links."
        ) from None


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
    if t.startswith("(?s:") and t.endswith(("\\Z", "\\z")):
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


def _stored_utf8(text: str, description: str = "path") -> bytes:
    """Refuse undecodable filesystem names before hashing or writing."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise FirnpackError(
            f"cannot store {description} {text!r}: it is not valid UTF-8. "
            "Rename the path or replace the symbolic link target and try again."
        ) from None


def _check_storable(files, symlinks) -> None:
    """Refuse, at COMMIT, any name the read side would refuse later.

    Shares _validate_member_names so commit and read cannot disagree. Used
    by both layer writers, including history rewritten by compact. A name
    rejected here used to pack fine and then wedge
    status/verify/export/restore on the committed-but-unreadable layer.
    """
    for path in (*files, *symlinks):
        _stored_utf8(path)
    try:
        if META_MEMBER in (*files, *symlinks):
            raise IntegrityError(f"reserved member name: {META_MEMBER!r}")
        _validate_member_names(sorted({*files, *symlinks}))
    except IntegrityError as exc:
        raise FirnpackError(
            f"cannot store {exc.args[0]}; rename the file and try again"
        ) from None


class _CanonicalTarInfo(tarfile.TarInfo):
    """Keep existing USTAR bytes, using PAX only for an unsplittable name."""

    def tobuf(
        self,
        format=tarfile.DEFAULT_FORMAT,
        encoding=tarfile.ENCODING,
        errors="surrogateescape",
    ):
        _stored_utf8(self.name)
        if self.issym():
            _stored_utf8(self.linkname, f"symbolic link target for {self.name!r}")
        if format != tarfile.USTAR_FORMAT:
            return super().tobuf(format, encoding, errors)
        try:
            return super().tobuf(format, encoding, errors)
        except ValueError as exc:
            probe = copy.copy(self)
            probe.name = "member"
            try:
                tarfile.TarInfo.tobuf(probe, format, encoding, errors)
            except ValueError:
                raise FirnpackError(
                    f"cannot store {self.name!r}: {exc}. Adjust the file or "
                    "shorten the symbolic link target and try again."
                ) from None
            return super().tobuf(tarfile.PAX_FORMAT, encoding, errors)


def _validate_member_types(members) -> None:
    """Structural member allowlist (SPEC §5 layer 2)."""
    allowed = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE, tarfile.DIRTYPE}
    for m in members:
        if m.name != META_MEMBER and m.type not in allowed:
            raise IntegrityError(
                f"layer contains a {m.type!r} member ({m.name!r}); "
                f"hardlinks and special members are not valid in firnpack layers"
            )


# Errors that mean "this layer's bytes are damaged or not a tar.gz at all":
# gzip/zlib faults, a truncated stream (EOFError), tar header faults, and
# OS-level read failures. All of them must surface as IntegrityError
# (exit 3), never as a raw traceback.
_LAYER_DECODE_ERRORS = (tarfile.TarError, zlib.error, EOFError, OSError, ValueError)


def _open_layer(path: Path, layer_id: str) -> tarfile.TarFile:
    """Open a layer as tar.gz, mapping open-time damage to IntegrityError."""
    try:
        return tarfile.open(path, "r:gz")  # noqa: SIM115
    except _LAYER_DECODE_ERRORS as exc:
        raise IntegrityError(f"cannot open layer {layer_id}: {exc}") from None


def _drain_layer(tar: tarfile.TarFile) -> None:
    """Read the gzip stream to its end so the CRC/ISIZE trailer is checked.

    tarfile stops at the end-of-archive marker and never reads the 8-byte
    gzip trailer, so a flipped byte in CRC32/ISIZE would otherwise pass.
    """
    fileobj = tar.fileobj
    if fileobj is None:
        return
    while fileobj.read(1 << 16):
        pass


# ---------------------------------------------------------------- hashing


def _hash_tree(
    entries: dict[str, tuple[int, bytes | None, str | None, bool]],
) -> str:
    """tree_id: per-path sha256(kind ∥ exec ∥ path ∥ NUL ∥ content), then
    over the concatenation (SPEC §5). Exec-in-identity: owner decision
    1a (ratified 2026-10-20). Kind byte + NUL separator: collision
    audit 2026-10-20 — ratified (owner decision 2026-10-20, item B2).

    ``entries`` maps posix path -> (size, content_bytes, symlink_target,
    exec_bit). The kind byte disambiguates a symlink's target bytes from
    a regular file's content; the NUL separator disambiguates the
    path/content boundary ({"a": b"bc"} vs {"ab": b"c"} hashed identically
    without it). Paths cannot contain NUL on POSIX or Windows, so the
    separator is collision-proof. Mode-only changes (chmod ±x) still
    change tree_id — never silently invisible drift.
    """
    digest = hashlib.sha256()
    for path in sorted(entries, key=_stored_utf8):
        _, content, target, exec_bit = entries[path]
        data = (
            _stored_utf8(target, f"symbolic link target for {path!r}")
            if content is None
            else content
        )
        kind = b"f" if content is not None else b"l"
        prefix = kind + (b"\x01" if exec_bit else b"\x00")
        digest.update(
            hashlib.sha256(prefix + _stored_utf8(path) + b"\x00" + data).digest()
        )
    return digest.hexdigest()[:ID_LEN]


def _disk_exec_bit(st) -> bool:
    """True when the file's execute bit is meaningfully set.

    POSIX: st_mode & 0o100 (chmod drives it). Windows: there is no chmod
    bit — the CRT reports S_IEXEC from the file extension (.exe/.bat/
    .cmd/.com), so the value is extension-derived and cannot be changed
    by the user: it participates in identity (same file -> same id) but
    never drifts (exec-in-identity ratified as owner decision 1a,
    2026-10-20; the Windows carry-forward policy is ratified as item B,
    2026-10-20 — SPEC §5).
    """
    return bool(st.st_mode & 0o100)


_WINDOWS = os.name == "nt"


def _file_mode(path, head_mode: int | None = None) -> int:
    """Layer/cache mode for a regular file: 0o755/0o644.

    Cross-platform policy (owner decision 2026-10-20, item B — SPEC
    §5): on POSIX the exec bit is real (chmod) and participates in
    identity. On Windows there is no user-settable exec bit, so:
      - the disk value cannot drift (nothing can change it), and
      - a layer written on another OS must not false-drift here: the
        head's stored mode is carried forward for regular files
        (regular-file head entries only — see _carry_mode).
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

    Conflict rule (git's): two changes conflict when their base ranges
    touch — insertions at the same index, or an insertion at the edge of
    a replacement. Zero-length (pure insertion) ranges at index i touch
    any change whose range contains i. Identical changes on both sides
    are deduplicated, not applied twice.
    """
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

    def touches(a, b):
        """Git-style adjacency: changes conflict if their base ranges
        touch, INCLUDING pure insertions at the same point or at the
        boundary of another change."""
        # half-open [a0, a1) vs [b0, b1); insertions are zero-width
        if a[0] == a[1] or b[0] == b[1]:
            # zero-width range touches when its index lies within or
            # on the boundary of the other
            return a[0] <= b[1] and b[0] <= a[1]
        return a[0] < b[1] and b[0] < a[1]

    for oo in o_ops:
        for tto in t_ops:
            if touches(oo, tto) and oo[2] != tto[2]:
                return None  # conflicting region
    # dedupe: identical (span, content) changes on both sides apply once
    seen = set()
    ops = []
    for op in o_ops + t_ops:
        key = (op[0], op[1], tuple(op[2]))
        if key in seen:
            continue
        seen.add(key)
        ops.append(op)
    # one forward sweep over base, consuming ops in index order
    out = []
    cursor = 0
    for i1, i2, replacement in sorted(ops, key=lambda op: op[0]):
        out.extend(b_lines[cursor:i1])
        out.extend(replacement)
        cursor = max(cursor, i2)
    out.extend(b_lines[cursor:])
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

    def __init__(self, root: Path, *, recovering: bool = False):
        self.root = root.resolve()
        self.firn = self.root / FIRN_DIR
        if not (self.firn / "glacier.db").is_file() and not recovering:
            # the repo exists (its .firn/ is here) but its index is gone:
            # point at the rebuild, never at 'init' (which would refuse)
            raise NotARepoError(
                f"glacier.db is missing in {self.firn}; run "
                f"'firnpack verify --rebuild-db' to rebuild the index from "
                f"the layers"
            )
        self.layers_dir = self.firn / "layers"
        self._db = self._connect()
        try:
            self._cfg = self._load_config()
        except BaseException:
            self._db.close()
            raise

    # -- lifecycle helpers

    @staticmethod
    def discover(start: Path | None = None, *, recovering: bool = False) -> Repo:
        # the nearest .firn/ directory is the repository, even when its
        # glacier.db is missing: never walk past it into an outer repo
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / FIRN_DIR).is_dir():
                return Repo(candidate, recovering=recovering)
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
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError:
            # lock contention is transient (rc 1, retry); let it propagate
            conn.close()
            raise
        except sqlite3.DatabaseError as exc:
            # the file is not a SQLite database (or is damaged): this is
            # store damage, not a usage error — rc 3, one humane line
            conn.close()
            raise IntegrityError(
                f"glacier.db is not a readable firnpack database ({exc}). "
                f"Save a copy, replace it with an empty file, then run "
                f"'firnpack verify --rebuild-db' to rebuild the index "
                f"from the layers"
            ) from None
        return conn

    def _load_config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read_dict(
            {
                "user": {"name": getpass.getuser()},
                "core": {"profile": "gzip", "autoname": "true"},
                "merge": {"block_unresolved_markers": "true"},
                "history": {"reminder_threshold": "50"},
            }
        )
        # init writes this file as UTF-8; read it the same way on every
        # platform (the locale default would garble a non-ASCII author name)
        path = self.firn / "config"
        try:
            read = cfg.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeError) as exc:
            raise FirnpackError(
                f"cannot read {path}: {exc}. Fix the file, or delete it to "
                f"fall back to the defaults."
            ) from None
        if not read and path.exists():
            # configparser skips an unreadable file without a word; a user
            # who set block_unresolved_markers = false must not silently
            # lose that choice
            print(
                f"warning: could not read {path}; using the default settings "
                f"until it is readable again",
                file=sys.stderr,
            )
        return cfg

    @staticmethod
    def _validate_config(cfg: configparser.ConfigParser, path: Path) -> None:
        """Reject wrong-typed settings before commit, amend or merge changes
        anything. Inspection and export do not consume these settings, so
        opening a repository must not validate them: a typo must not block
        status, verify or access to the saved files."""
        typed = (
            ("history", "reminder_threshold", cfg.getint, "a whole number"),
            ("merge", "block_unresolved_markers", cfg.getboolean, "true or false"),
            ("core", "autoname", cfg.getboolean, "true or false"),
            ("core", "allow_flattened", cfg.getboolean, "true or false"),
        )
        for section, key, getter, wanted in typed:
            try:
                getter(section, key, fallback=None)
            except (ValueError, configparser.Error):
                raise FirnpackError(
                    f"cannot use {path}: [{section}] {key} = "
                    f"{cfg.get(section, key, raw=True)!r} is not {wanted}. Fix "
                    f"that line, or delete the file to fall back to the defaults."
                ) from None

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
        cfg = configparser.ConfigParser(interpolation=None)
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
            # head_pin is pin-only (SPEC 6): a branch worktree must
            # never carry one, or stale pins spuriously trip the
            # compact pin-refusal
            self._db.execute("UPDATE worktree SET head_pin = NULL WHERE id = 1")
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
            _stored_utf8(path)
            fp = self.root / path
            st = fp.stat()
            data = fp.read_bytes()
            head_mode = _carry_mode(carry, path)
            mode = _file_mode(fp, head_mode)
            entries[path] = (len(data), data, None, mode == 0o755)
            stats[path] = [st.st_size, st.st_mtime_ns, mode]
        for path in symlinks:
            _stored_utf8(path)
            fp = self.root / path
            target = os.readlink(fp)
            target_bytes = _stored_utf8(target, f"symbolic link target for {path!r}")
            entries[path] = (len(target_bytes), None, target, False)
            st = fp.lstat()
            stats[path] = [st.st_size, st.st_mtime_ns, 0o777]
        return entries, stats

    # -- worktree preparation (SPEC §9.4/§9.7/§10)
    #
    # Two phases, always in this order:
    #   1. _prepare_tree_write — READ-ONLY. Every target path and every
    #      ancestor is inspected before a single byte changes. Anything
    #      that is not a currently _walk-visible leaf is protected
    #      unconditionally: ignored files, dangling symlinks, FIFOs and
    #      other special files, now-ignored formerly-tracked paths, and
    #      every entry an ignored directory hides (ignored enumeration is
    #      not exhaustive). Blocking directories are inspected recursively
    #      WITHOUT following symlinks. Refusal names the blocking path.
    #   2. _apply_tree — the only place anything is deleted or created:
    #      unlink eligible leaves, rmdir EMPTY obstructing directories
    #      (never rmtree), mkdir needed parents, exclusive-create leaves.
    #
    # Deletion baseline is the previous-state MANIFEST (old head layer for
    # switch/restore/FF, ours ∪ theirs for a pending merge), intersected
    # with current visible leaves — never stat_cache, which may be NULL
    # after a rebuild and may still name files that are ignored now.
    # `rescued_ok` (a durable rescue layer already written this
    # invocation) additionally permits removing new visible files, but
    # only where they obstruct a target leaf or needed ancestor.

    def _check_tree_shape(self, target_paths) -> None:
        """Refuse a tree that contains both a leaf and paths beneath it —
        impossible on a real filesystem. _read_layer enforces this for
        persisted layers; in-memory merged trees need the same gate."""
        seen = set(target_paths)
        for path in seen:
            parts = path.split("/")
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                if ancestor in seen:
                    raise FirnpackError(
                        f"cannot materialize: {path!r} lies beneath "
                        f"{ancestor!r}, which is a file"
                    )

    def _prepare_tree_write(
        self,
        target_paths,
        baseline: set[str],
        *,
        rescued_ok: bool,
    ) -> tuple[set[str], set[str]]:
        """Inspect every target leaf and ancestor BEFORE mutation.

        Returns (unlink, rmdirs): leaves to unlink and directories to
        remove empty (deepest-first). Raises FirnpackError naming the
        blocking path when a protected entry would have to be destroyed.
        """
        self._check_tree_shape(target_paths)
        files, symlinks, _ignored, _nested = self._walk()
        visible = set(files) | set(symlinks)

        def leaf_removable(rel: str) -> bool:
            return rel in visible and (rel in baseline or rescued_ok)

        blockers: list[str] = []
        # every previous tracked leaf is deleted wholesale (the old
        # stat-cache wipe's semantics, but driven by the MANIFEST):
        # leaves absent from the target disappear, leaves shared with the
        # target are rewritten. Only visible leaves may be unlinked.
        unlink: set[str] = set(baseline) & visible
        rmdirs: set[str] = set()

        def scan_dir(directory: Path, rel: str) -> bool:
            """True when `directory` holds only removable leaves/dirs.
            Never follows symlinks."""
            removable = True
            try:
                items = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                blockers.append(rel or str(directory))
                return False
            for item in items:
                item_rel = f"{rel}/{item.name}" if rel else item.name
                item_path = Path(item.path)
                if item.is_symlink():
                    if leaf_removable(item_rel):
                        unlink.add(item_rel)
                    else:
                        blockers.append(item_rel)
                        removable = False
                elif item.is_dir(follow_symlinks=False):
                    if scan_dir(item_path, item_rel):
                        rmdirs.add(item_rel)
                    else:
                        blockers.append(item_rel)
                        removable = False
                elif item.is_file(follow_symlinks=False):
                    if leaf_removable(item_rel):
                        unlink.add(item_rel)
                    else:
                        blockers.append(item_rel)
                        removable = False
                else:
                    blockers.append(item_rel)
                    removable = False
            return removable

        for path in sorted(set(target_paths)):
            comps = path.split("/")
            if not path or not all(comps) or comps[0] in (FIRN_DIR, ".git"):
                raise FirnpackError(
                    f"cannot write to repository metadata path: {path!r}"
                )
            fp = self.root / path
            # every ancestor must be a real directory, absent, or a
            # removable leaf — a symlinked ancestor is never followed.
            prefix = ""
            broken = False
            for comp in comps[:-1]:
                prefix = f"{prefix}/{comp}" if prefix else comp
                ap = self.root / prefix
                if not os.path.lexists(ap):
                    continue
                try:
                    st = os.lstat(ap)
                except OSError:
                    blockers.append(prefix)
                    broken = True
                    break
                if stat.S_ISDIR(st.st_mode):
                    continue
                if leaf_removable(prefix):
                    unlink.add(prefix)
                    continue
                blockers.append(prefix)
                broken = True
                break
            if broken:
                continue
            # the leaf slot itself
            if not os.path.lexists(fp):
                continue
            try:
                st = os.lstat(fp)
            except OSError:
                blockers.append(path)
                continue
            if stat.S_ISDIR(st.st_mode):
                if scan_dir(fp, path):
                    rmdirs.add(path)
                else:
                    blockers.append(path)
            elif leaf_removable(path):
                unlink.add(path)
            else:
                blockers.append(path)
        if blockers:
            raise FirnpackError(
                "cannot materialize: untracked or ignored items occupy "
                f"{', '.join(sorted(set(blockers)))} — move them aside "
                "(or commit what should be kept) and try again. Nothing "
                "was changed."
            )
        return unlink, rmdirs

    def _apply_prepared(
        self,
        prepared: tuple[set[str], set[str]],
    ) -> None:
        """Phase 2a: unlink the eligible leaves, then remove EMPTY
        obstructing directories deepest-first (never rmtree — a raced-in
        child makes rmdir fail, which propagates to the caller's rollback
        path instead of deleting user data)."""
        unlink, rmdirs = prepared
        for rel in sorted(unlink):
            fp = self.root / rel
            if fp.is_symlink() or fp.is_file():
                fp.unlink()
            else:
                fp.unlink()  # special leaf approved by preflight
        for rel in sorted(rmdirs, key=lambda p: p.count("/"), reverse=True):
            with contextlib.suppress(FileNotFoundError):
                (self.root / rel).rmdir()

    def _ensure_parents(self, fp: Path, created_dirs: set[str] | None):
        """mkdir -p the leaf's ancestors; record only directories that did
        not exist before, for rollback."""
        parent = fp.parent
        missing = []
        while parent != self.root and not os.path.lexists(parent):
            missing.append(parent)
            parent = parent.parent
        for d in reversed(missing):
            d.mkdir()
            if created_dirs is not None:
                created_dirs.add(str(d.relative_to(self.root)))

    def _write_tree_leaves(
        self, tree: dict, created_dirs: set[str] | None = None
    ) -> set[str]:
        """Write leaf entries of `tree` ({path: (bytes|None, target|None,
        exec)}); record created ancestors in `other_dirs` and return the
        set of created leaf paths."""
        directory_links = _directory_symlinks({p: v[1] for p, v in tree.items()})
        created: set[str] = set()
        for path in sorted(tree):
            data, target, exec_bit = tree[path]
            fp = self.root / path
            self._ensure_parents(fp, created_dirs)
            if target is not None:
                _symlink(fp, target, target_is_directory=path in directory_links)
                created.add(path)
            else:
                _write_file(fp, data)
                created.add(path)
                if exec_bit and os.name != "nt":
                    fp.chmod(0o755)
        return created

    def _layer_path(self, layer_id: str) -> Path:
        return self.layers_dir / f"{layer_id}.firn"

    def _write_layer(self, layer_id: str, meta: dict, files, symlinks) -> int:
        """Canonical tar -> gzip -> atomic durable write (SPEC §13).

        Returns the compressed size on disk. Names the read side would
        refuse are rejected here first (see _check_storable): nothing is
        written, so the repo stays readable.
        """
        _check_storable(files, symlinks)
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
        try:
            with open(tmp, "wb") as raw:
                with (
                    gzip.GzipFile(
                        fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0
                    ) as gz,
                    tarfile.open(
                        fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT
                    ) as tar,
                ):
                    ti = _CanonicalTarInfo(META_MEMBER)
                    ti.size = len(meta_bytes)
                    ti.mtime = 0
                    ti.mode = 0o644
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    tar.addfile(ti, io.BytesIO(meta_bytes))
                    for path in sorted(files, key=_stored_utf8):
                        fp = self.root / path
                        data = fp.read_bytes()
                        ti = _CanonicalTarInfo(path)
                        ti.size = len(data)
                        ti.mtime = 0
                        ti.uid = ti.gid = 0
                        ti.uname = ti.gname = ""
                        head_mode = _carry_mode(carry, path)
                        ti.mode = _file_mode(fp, head_mode)
                        tar.addfile(ti, io.BytesIO(data))
                    for path in sorted(symlinks, key=_stored_utf8):
                        target = os.readlink(self.root / path)
                        ti = _CanonicalTarInfo(path)
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
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
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
        with _open_layer(path, layer_id) as tar:
            try:
                members = tar.getmembers()
                _validate_member_types(members)
                _validate_member_names([m.name for m in members])
                meta = None
                manifest, contents = {}, {}
                seen = set()
                for m in members:
                    if m.name in seen:
                        raise IntegrityError(
                            f"layer {layer_id} has a duplicate member {m.name!r}; "
                            "extraction would keep only one of them"
                        )
                    seen.add(m.name)
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
                        data = tar.extractfile(m).read()
                        manifest[m.name] = {
                            "size": m.size,
                            "mode": m.mode,
                            "target": None,
                        }
                        contents[m.name] = data
                _drain_layer(tar)
                # a member may not sit beneath a non-directory member: the
                # extractor would write through a symlink (d -> ../outside)
                # or fail mid-write (f0.txt is a file, f0.txt/inner.txt
                # cannot exist)
                for name in manifest:
                    parts = name.split("/")
                    for i in range(1, len(parts)):
                        ancestor = "/".join(parts[:i])
                        if ancestor in manifest:
                            raise IntegrityError(
                                f"layer {layer_id}: {name!r} lies beneath "
                                f"{ancestor!r}, which is not a directory"
                            )
            except _LAYER_DECODE_ERRORS as exc:
                raise IntegrityError(f"layer {layer_id} is damaged: {exc}") from None
        if meta is None:
            raise IntegrityError(f"layer {layer_id} has no meta member")
        return meta, manifest, contents

    def _layer_contents(self, layer_id: str, wanted: set[str]) -> dict:
        """Read only the requested regular-file members from a layer."""
        path = self._layer_path(layer_id)
        _warn_patch_level_once()
        out = {}
        with _open_layer(path, layer_id) as tar:
            try:
                for m in tar.getmembers():
                    if m.name in wanted and m.type in (
                        tarfile.REGTYPE,
                        tarfile.AREGTYPE,
                    ):
                        out[m.name] = tar.extractfile(m).read()
                _drain_layer(tar)
            except _LAYER_DECODE_ERRORS as exc:
                raise IntegrityError(f"layer {layer_id} is damaged: {exc}") from None
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

    def _tree_of(self, layer_id: str) -> dict:
        """A layer as {path: (bytes|None, target|None, exec_bit)}."""
        _, manifest, _ = self._read_layer(layer_id)
        data = self._layer_contents(layer_id, set(manifest))
        tree = {}
        for path, v in manifest.items():
            if v["target"] is not None:
                tree[path] = (None, v["target"], False)
            else:
                tree[path] = (data.get(path, b""), None, v.get("mode") == 0o755)
        return tree

    def merge(self, ref: str, message: str | None = None) -> dict:
        """3-way merge of a branch into the current head (SPEC §10).

        Clean merge -> one merge layer (parent = ours, second_parent =
        theirs). Conflicts -> pending-merge state (merge_theirs), markers
        in the worktree, exit path is resolve-then-commit or --abort.
        """
        self._validate_config(self._cfg, self.firn / "config")
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
            self._move_head_and_sync(wt, theirs_id, wt["head_kind"])
            return {
                "status": "fast_forwarded",
                "head": theirs_id,
                "kind": wt["head_kind"],
            }

        # load the three trees
        def tree_of(layer_id):
            return self._tree_of(layer_id)

        base_tree = tree_of(base_id)
        ours_tree = tree_of(ours_id)
        theirs_tree = tree_of(theirs_id)
        merged, conflicts = self._merge_trees(base_tree, ours_tree, theirs_tree)

        # Preflight BEFORE merge_theirs is set: two valid trees can merge
        # into an impossible file+descendant result, and protected
        # worktree entries must refuse before any pointer moves. The
        # deletion baseline is the ours manifest (drift guard already ran).
        prepared = self._prepare_tree_write(
            set(merged), set(ours_tree), rescued_ok=False
        )

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
            self._materialize(merged, baseline=set(ours_tree), prepared=prepared)
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
        # markers included
        self._materialize(merged, baseline=set(ours_tree), prepared=prepared)
        result = {
            "status": "conflict",
            "head": ours_id,
            "theirs": theirs_id,
            "conflicts": conflicts,
        }
        kept_sides = {
            path: "theirs" if path not in ours_tree else "ours"
            for path in conflicts
            if path not in ours_tree or path not in theirs_tree
        }
        if kept_sides:
            result["kept_sides"] = kept_sides
        return result

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
        theirs_id = wt["merge_theirs"]

        # Deletion baseline for putting ours back: everything the pending
        # merge may have written — ours paths UNION theirs paths — never
        # the (possibly stale) stat cache. stat_cache is a hint only.
        ours_manifest = set(self._read_layer(ours_id)[1])
        theirs_manifest = set(self._read_layer(theirs_id)[1])
        abort_baseline = ours_manifest | theirs_manifest

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

        # Preflight with the rescue already durable (or none needed):
        # visible new leaves may be removed only where they obstruct.
        prepared = self._prepare_tree_write(
            ours_manifest,
            abort_baseline,
            rescued_ok=rescue_written,
        )
        created_paths: set[str] = set()
        created_dirs: set[str] = set()
        try:
            self._sync_worktree_to(
                ours_id,
                prepared=prepared,
                created_paths=created_paths,
                created_dirs=created_dirs,
            )
        except BaseException as exc:
            # best-effort: drop only what this attempt created, then say
            # plainly what state was left — pending merge stays, the
            # rescue (if any) is named, files may be partly restored.
            for path in sorted(created_paths):
                fp = self.root / path
                with contextlib.suppress(OSError):
                    if fp.is_symlink() or fp.is_file():
                        fp.unlink()
            for path in sorted(created_dirs, key=lambda p: p.count("/"), reverse=True):
                with contextlib.suppress(OSError):
                    (self.root / path).rmdir()
            note = (
                "merge --abort did not finish restoring the previous "
                "worktree: files may be partly restored, and the merge "
                "is still pending — fix the problem and run "
                "'firnpack merge --abort' again."
            )
            if rescue_written:
                note += (
                    f" Your uncommitted work was saved as layer "
                    f"{rescue_id}; retrieve it with: firnpack export "
                    f"{rescue_id} --to DIR"
                )
            if isinstance(exc, FirnpackError):
                exc.args = (f"{exc} {note}",)
            else:
                print(f"warning: {note}", file=sys.stderr)
            raise
        with self._db:
            self._db.execute("UPDATE worktree SET merge_theirs = NULL WHERE id = 1")
        return {
            "status": "aborted",
            "head": ours_id,
            "rescue_layer": rescue_id if rescue_written else None,
        }

    def _materialize(
        self,
        tree: dict,
        *,
        baseline: set[str],
        prepared: tuple[set[str], set[str]] | None = None,
    ) -> None:
        """Write a merged tree into the worktree (markers included),
        then refresh the stat cache so status reports drift correctly.

        Same two-phase contract as _sync_worktree_to: merge() prepares
        before merge_theirs is set, so a refused materialization leaves
        the worktree and pending state untouched."""
        if prepared is None:
            prepared = self._prepare_tree_write(set(tree), baseline, rescued_ok=False)
        self._apply_prepared(prepared)
        self._write_tree_leaves(tree)
        stats = {}
        for path in sorted(tree):
            data, target, exec_bit = tree[path]
            fp = self.root / path
            if target is not None:
                st = fp.lstat()
                stats[path] = [st.st_size, st.st_mtime_ns, 0o777]
            else:
                mode = 0o755 if exec_bit else 0o644
                st = fp.stat()
                stats[path] = [st.st_size, st.st_mtime_ns, mode]
        with self._db:
            self._db.execute(
                "UPDATE worktree SET stat_cache = ?, updated_at = ? WHERE id = 1",
                (json.dumps(stats), _utcnow()),
            )

    def _path_length_warnings(self, files, symlinks) -> list[str]:
        paths = (*files, *symlinks)
        for path in paths:
            if len(path) > MAX_PATH_HARD:
                raise FirnpackError(
                    f"path too long to store ({len(path)} chars): {path}. "
                    "Shorten the file name or enable Windows long paths."
                )
        return [
            path
            for path in paths
            if len(str((self.root / path).resolve())) > MAX_PATH_WARN
        ]

    @staticmethod
    def _add_path_warning(result: dict, long_paths: list[str]) -> None:
        if long_paths:
            result["maxpath_warning"] = long_paths
            print(
                f"warning: {len(long_paths)} path(s) longer than "
                f"{MAX_PATH_WARN} characters; restore may need Windows long "
                "paths enabled.",
                file=sys.stderr,
            )

    # -- commands

    def commit(
        self,
        message: str | None = None,
        *,
        allow_markers: bool = False,
        allow_binary_conflict: bool = False,
        allow_flattened: bool = False,
    ) -> dict:
        self._validate_config(self._cfg, self.firn / "config")
        wt = self._worktree()
        files, symlinks, ignored, _nested = self._walk()
        entries, stats = self._current_tree(files, symlinks)
        tree_id = _hash_tree(entries)

        pending_flatten = json.loads(wt["flattened_pending"] or "null")
        merge_theirs = wt["merge_theirs"]

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

        # Marker guard (SPEC §9.2/§11; owner decision 2026-10-22: every
        # commit that would write a layer, not only the one completing a
        # pending merge). A no-op commit never gets here, so a repository
        # that already holds marker-shaped lines can still say "nothing to
        # pack".
        self._check_markers(
            entries, allow=allow_markers, pending_merge=bool(merge_theirs)
        )
        if merge_theirs:
            undecided = self._unresolved_binary_conflicts(merge_theirs, entries)
            if undecided:
                if not allow_binary_conflict:
                    raise BinaryConflict(undecided)
                print(
                    f"warning: keeping our version of undecided binary "
                    f"conflict(s): {', '.join(undecided)}; their version is "
                    f"not included in this merge's tree.",
                    file=sys.stderr,
                )

        long_paths = self._path_length_warnings(files, symlinks)

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
        self._add_path_warning(result, long_paths)
        self._maybe_remind(result)
        return result

    def amend(self, message: str | None = None, *, allow_markers: bool = False) -> dict:
        """Replace the head layer: same parent(s) as head, tree = the
        current worktree (SPEC 9.3). The old layer stays on disk
        (write-once); the head moves. Message-only amend when the tree
        is unchanged. Refuses while a merge is pending."""
        self._validate_config(self._cfg, self.firn / "config")
        wt = self._worktree()
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is pending — resolve and commit, or "
                "merge --abort, before amending"
            )
        if wt["head_id"] is None:
            raise FirnpackError("nothing to amend — the repository is empty")
        row = self._db.execute(
            "SELECT parent, second_parent, message FROM layers WHERE id = ?",
            (wt["head_id"],),
        ).fetchone()
        old_parent, old_second, old_msg = row

        files, symlinks, _ignored, _nested = self._walk()
        entries, stats = self._current_tree(files, symlinks)
        tree_id = _hash_tree(entries)

        if tree_id == wt["snapshot"] and message is None:
            # message-only amend keeps the old message by spec ("message
            # = new (or old if -m omitted)") — but identical record
            # (same everything) dedups to the same layer: a no-op amend
            return {"status": "nothing_to_amend", "layer_id": wt["head_id"]}

        # amend writes a layer, so it gets the same marker guard as commit
        self._check_markers(entries, allow=allow_markers, pending_merge=False)
        long_paths = self._path_length_warnings(files, symlinks)

        msg = message if message is not None else old_msg
        record = {
            "format_version": FORMAT_VERSION,
            "tree_id": tree_id,
            "parent": old_parent,
            "second_parent": old_second,
            "message": msg,
            "author": self.author,
            "created_at": _utcnow(),
        }
        layer_id = _layer_id(record)
        meta = dict(record)
        meta["layer_id"] = layer_id
        meta["profile"] = "gzip"
        size = self._write_layer(layer_id, meta, files, symlinks)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO layers (id, tree_id, parent, "
                "second_parent, message, author, created_at, profile, "
                "file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    layer_id,
                    tree_id,
                    old_parent,
                    old_second,
                    msg,
                    self.author,
                    record["created_at"],
                    "gzip",
                    len(files) + len(symlinks),
                    size,
                ),
            )
            self._set_head(layer_id)
            self._db.execute(
                "UPDATE worktree SET head_id = ?, snapshot = ?, "
                "stat_cache = ?, updated_at = ? WHERE id = 1",
                (layer_id, tree_id, json.dumps(stats), _utcnow()),
            )
        self._sweep_orphans()
        result = {
            "status": "amended",
            "layer_id": layer_id,
            "replaced": wt["head_id"],
        }
        self._add_path_warning(result, long_paths)
        return result

    def _move_head_and_sync(
        self,
        wt: dict,
        layer_id: str,
        kind: str,
        *,
        prepared: tuple[set[str], set[str]] | None = None,
        rescued_ok: bool = False,
        head_change: tuple[str | None, str | None] | None = None,
    ) -> None:
        """Move the head (branch head or pin), then sync the worktree.
        DB-first ordering records the target across a crash. A caught
        write failure attempts to restore the previous head and files;
        the error reports whether that rollback succeeded or was partial.

        `head_change` = (new_head_kind, new_head_branch) lets switch move
        the branch selection atomically with the head (default: keep the
        current kind, pin iff kind == 'pin'). `wt` must be the pre-move
        worktree row: its fields are exactly what rollback restores."""
        old_head = wt["head_id"]
        target_manifest = self._read_layer(layer_id)[1]
        if prepared is None:
            baseline = set(self._read_layer(old_head)[1]) if old_head else set()
            prepared = self._prepare_tree_write(
                set(target_manifest), baseline, rescued_ok=rescued_ok
            )
        with self._db:
            if head_change is None and kind == "branch":
                self._db.execute(
                    "UPDATE branches SET head = ? WHERE name = ?",
                    (layer_id, wt["head_branch"]),
                )
            new_kind, new_branch = (
                head_change
                if head_change is not None
                else (kind, wt["head_branch"] if kind == "branch" else None)
            )
            self._db.execute(
                "UPDATE worktree SET head_kind = ?, head_branch = ?, "
                "head_pin = ?, head_id = ?, updated_at = ? WHERE id = 1",
                (
                    new_kind,
                    new_branch,
                    layer_id if new_kind == "pin" else None,
                    layer_id,
                    _utcnow(),
                ),
            )
        created_paths: set[str] = set()
        created_dirs: set[str] = set()
        try:
            self._sync_worktree_to(
                layer_id,
                prepared=prepared,
                created_paths=created_paths,
                created_dirs=created_dirs,
            )
        except BaseException as exc:
            collision = isinstance(
                exc, (FileExistsError, IsADirectoryError, NotADirectoryError)
            )
            note = (
                "The previous head could not be restored; head and files may "
                "be partly updated. Run 'firnpack status' and 'firnpack verify'."
            )
            try:
                with self._db:
                    if head_change is None and kind == "branch":
                        self._db.execute(
                            "UPDATE branches SET head = ? WHERE name = ?",
                            (old_head, wt["head_branch"]),
                        )
                    self._db.execute(
                        "UPDATE worktree SET head_kind = ?, head_branch = ?, "
                        "head_pin = ?, head_id = ?, updated_at = ? WHERE id = 1",
                        (
                            wt["head_kind"],
                            wt["head_branch"],
                            wt["head_pin"],
                            old_head,
                            _utcnow(),
                        ),
                    )
            except Exception:
                pass  # preserve the original failure, not a rollback error
            else:
                try:
                    # Remove only what the failed attempt actually
                    # created — never untouched paths that may hold
                    # ignored work. Directories it made are removed empty,
                    # deepest first.
                    for path in sorted(created_paths):
                        fp = self.root / path
                        if fp.is_symlink() or fp.is_file():
                            fp.unlink()
                    for path in sorted(
                        created_dirs, key=lambda p: p.count("/"), reverse=True
                    ):
                        with contextlib.suppress(OSError):
                            (self.root / path).rmdir()
                    # A collision belongs to another writer. Even the
                    # failed target's manifest must not authorize its
                    # deletion during rollback.
                    if not collision:
                        self._sync_worktree_to(
                            old_head,
                            baseline=set(target_manifest),
                            rescued_ok=False,
                        )
                except Exception:
                    note = (
                        "The head was put back, but files may be partly "
                        "written. Run 'firnpack status' before continuing."
                    )
                else:
                    if collision:
                        note = (
                            "The head was put back, but files may be partly "
                            "written. An entry that occupied a target path "
                            "was left untouched. Run 'firnpack status' before "
                            "continuing."
                        )
                    else:
                        note = (
                            "The head was put back and files were restored to "
                            "the previous head."
                        )
            if isinstance(exc, FirnpackError):
                exc.args = (f"{exc} {note}",)
            else:
                print(f"warning: {note}", file=sys.stderr)
            raise

    def restore(self, ref: str, *, to_dir: str | None = None) -> dict:
        """Extract a layer over the worktree (rescue first if drift),
        or to a directory when --to is given (SPEC 9.7)."""
        wt = self._worktree()
        layer_id = self._resolve_id(ref)
        if to_dir is not None:
            return self.export(layer_id, to_dir)
        # over the worktree: rescue drift first (SPEC 9.7)
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is pending — resolve it, or merge --abort, before restoring"
            )
        st = self.status()
        # Preflight BEFORE the rescue write: a restore that cannot change
        # the worktree must refuse without leaving an unneeded rescue
        # layer behind. The baseline is the CURRENT head manifest; the
        # prospective rescue permits removing only obstructing new
        # visible files once it is durably written.
        baseline = set(self._read_layer(wt["head_id"])[1])
        target_manifest = set(self._read_layer(layer_id)[1])
        prepared = self._prepare_tree_write(
            target_manifest, baseline, rescued_ok=not st["clean"]
        )
        rescue_id = None
        if not st["clean"]:
            files, symlinks, _i, _n = self._walk()
            entries, _stats = self._current_tree(files, symlinks)
            tree_id = _hash_tree(entries)
            record = {
                "format_version": FORMAT_VERSION,
                "tree_id": tree_id,
                "parent": wt["head_id"],
                "second_parent": None,
                "message": "auto: drift rescue before restore",
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
                        wt["head_id"],
                        None,
                        record["message"],
                        self.author,
                        record["created_at"],
                        "gzip",
                        len(files) + len(symlinks),
                        size,
                    ),
                )
        # Move the head and sync; failed writes attempt rollback. A rescue
        # already written above remains recoverable even if the sync fails.
        kind = wt["head_kind"]
        try:
            self._move_head_and_sync(
                wt,
                layer_id,
                kind,
                prepared=prepared,
                rescued_ok=rescue_id is not None,
            )
        except BaseException:
            if rescue_id is not None:
                print(
                    f"warning: your uncommitted work was saved as layer "
                    f"{rescue_id}; retrieve it with: firnpack export "
                    f"{rescue_id} --to DIR",
                    file=sys.stderr,
                )
            raise
        return {
            "status": "restored",
            "layer_id": layer_id,
            "rescue_layer": rescue_id,
            "previous_head": wt["head_id"],
        }

    def export(self, ref: str, to_dir: str) -> dict:
        """Extract a layer into a fresh directory — never destructive
        (SPEC 9.7). The recommended exploration path."""
        layer_id = self._resolve_id(ref)
        dest = Path(to_dir)
        if dest.exists() and any(dest.iterdir()):
            raise FirnpackError(
                f"{dest} already exists and is not empty — export needs "
                "a fresh directory"
            )
        dest.mkdir(parents=True, exist_ok=True)
        self._extract_layer_to(layer_id, dest)
        return {"status": "exported", "layer_id": layer_id, "to": str(dest)}

    def _extract_layer_to(self, layer_id: str, dest: Path) -> None:
        """Safe extraction of a layer into a destination directory:
        validate all members first, then write (SPEC 5 layers 1-3)."""
        _, manifest, _ = self._read_layer(layer_id)
        _warn_patch_level_once()
        regular = {p for p, v in manifest.items() if v["target"] is None}
        contents = self._layer_contents(layer_id, regular)
        directory_links = _directory_symlinks(
            {p: v["target"] for p, v in manifest.items()}
        )
        for path in sorted(manifest):
            v = manifest[path]
            fp = dest / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            if v["target"] is not None:
                _symlink(fp, v["target"], target_is_directory=path in directory_links)
            else:
                fp.write_bytes(contents[path])
                mode = v.get("mode", 0o644)
                if mode == 0o755 and os.name != "nt":
                    fp.chmod(0o755)

    def verify(self) -> dict:
        """Re-hash every layer file; cross-check DB and in-band meta
        (SPEC 9.8). Exit-code contract: 3 on any integrity failure."""
        problems = []
        rows = self._db.execute(
            "SELECT id, tree_id, parent, second_parent, message, author,"
            " created_at FROM layers"
        ).fetchall()
        known = {r[0] for r in rows}
        for layer_id, tree_id, parent, second, _message, _author, _created in rows:
            path = self._layer_path(layer_id)
            if not path.is_file():
                problems.append(f"missing layer file: {layer_id}")
                continue
            try:
                meta, manifest, contents = self._read_layer(layer_id)
            except IntegrityError as exc:
                problems.append(f"layer {layer_id}: {exc}")
                continue
            # rebuild the canonical record and re-hash the layer's own meta
            record = {
                "format_version": FORMAT_VERSION,
                "tree_id": meta["tree_id"],
                "parent": meta["parent"],
                "second_parent": meta["second_parent"],
                "message": meta["message"],
                "author": meta["author"],
                "created_at": meta["created_at"],
            }
            if _layer_id(record) != layer_id:
                problems.append(
                    f"layer {layer_id}: id mismatch (meta does not hash "
                    f"to the filename)"
                )
            # re-hash the file content: the tree id must equal the hash of
            # the bytes actually stored, not just the value the meta claims
            entries = {}
            for p, v in manifest.items():
                if v["target"] is not None:
                    target = v["target"]
                    entries[p] = (len(target.encode("utf-8")), None, target, False)
                else:
                    data = contents[p]
                    entries[p] = (
                        len(data),
                        data,
                        None,
                        v["mode"] == 0o755,
                    )
            if _hash_tree(entries) != meta["tree_id"]:
                problems.append(
                    f"layer {layer_id}: tree_id mismatch (stored bytes do not "
                    f"hash to the recorded tree)"
                )
            # the DB row duplicates the meta record; every column must agree
            db_row = {
                "tree_id": tree_id,
                "parent": parent,
                "second_parent": second,
                "message": _message,
                "author": _author,
                "created_at": _created,
            }
            for column, value in db_row.items():
                if value != meta[column]:
                    problems.append(f"layer {layer_id}: {column} mismatch (DB vs meta)")
            if parent is not None and parent not in known:
                problems.append(f"layer {layer_id}: dangling parent {parent}")
            if second is not None and second not in known:
                problems.append(f"layer {layer_id}: dangling second_parent {second}")
        for row in self._db.execute("SELECT name, head FROM branches"):
            if row[1] not in known:
                problems.append(f"branch {row[0]}: dangling head {row[1]}")
        wt = self._worktree()
        if wt["head_id"] is not None and wt["head_id"] not in known:
            problems.append("worktree: dangling head_id")
        # unreferenced files on disk (orphans beyond the sweep)
        for f in self.layers_dir.glob("*.firn"):
            if f.stem not in known:
                problems.append(f"orphan layer file: {f.stem}")
        if problems:
            raise IntegrityError("verify failed:\n  " + "\n  ".join(problems))
        return {"status": "ok", "layers": len(rows)}

    def rebuild_db(self) -> dict:
        """Reconstruct the database from the in-band meta of every
        layer file (SPEC 9.8/§13, Q9 owner-accept). Branch names and
        pins live only in the database and are NOT recoverable: the
        rebuild creates `main` at the newest tip layer (one no other
        layer descends from; newest created_at, ties broken by id)
        and reports what was lost.

        Refuses (IntegrityError, exit 3) when a layer file fails its
        self-hash or a referenced parent layer file is missing —
        restore those files from an archive or backup first
        (storage.md §6). An empty or table-less database file is
        re-schematized in place; a database file sqlite cannot open
        at all, or a missing one, must be replaced by an empty file
        first (storage.md §6, step 4)."""
        # snapshot the branch names BEFORE the schema heals an empty file:
        # a missing/empty database has no names to read, so unknowable
        pre_branches: list[str] | None = None
        with contextlib.suppress(sqlite3.Error):
            pre_branches = [n for (n,) in self._db.execute("SELECT name FROM branches")]
        self._db.executescript(SCHEMA)  # idempotent: heals empty/corrupt-empty files
        rows = []
        for path in sorted(self.layers_dir.glob("*.firn")):
            layer_id = path.stem
            meta, manifest, _ = self._read_layer(layer_id)
            record = {
                "format_version": FORMAT_VERSION,
                "tree_id": meta["tree_id"],
                "parent": meta["parent"],
                "second_parent": meta["second_parent"],
                "message": meta["message"],
                "author": meta["author"],
                "created_at": meta["created_at"],
            }
            if _layer_id(record) != layer_id:
                raise IntegrityError(
                    f"layer {layer_id[:12]} fails its self-hash — restore "
                    f"it from an archive or backup before rebuilding"
                )
            rows.append(
                (
                    layer_id,
                    meta["tree_id"],
                    meta["parent"],
                    meta["second_parent"],
                    meta["message"],
                    meta["author"],
                    meta["created_at"],
                    meta.get("profile", "gzip"),
                    len(manifest),
                    path.stat().st_size,
                )
            )
        ids = {r[0] for r in rows}
        missing = sorted(
            {p for r in rows for p in (r[2], r[3]) if p is not None and p not in ids}
        )
        if missing:
            raise IntegrityError(
                "cannot rebuild: parent layer file(s) missing: "
                + ", ".join(m[:12] for m in missing)
                + " — restore them first (storage.md §6)"
            )
        # None = unknowable (missing/empty/unreadable database: no branch
        # names to read) — distinct from [] (a readable table, none lost)
        lost_branches: list[str] | None = pre_branches
        head_id = head_tree = None
        if rows:
            # newest TIP layer: one no other layer descends from.
            # created_at is second-granular, so "newest created_at,
            # ties by id" alone can pick a parent over its same-second
            # child and orphan the true head; tip-preference recovers
            # the latest work in that case and matches the ratified
            # rule whenever timestamps differ (SPEC 13, Q9)
            parents = {p for r in rows for p in (r[2], r[3]) if p is not None}
            tips = [r for r in rows if r[0] not in parents]
            newest = max(tips or rows, key=lambda r: (r[6], r[0]))
            head_id, head_tree = newest[0], newest[1]
        with self._db:
            self._db.execute("PRAGMA defer_foreign_keys = ON")
            self._db.execute("DELETE FROM layers")
            self._db.execute("DELETE FROM branches")
            for r in rows:
                self._db.execute(
                    "INSERT INTO layers (id, tree_id, parent, "
                    "second_parent, message, author, created_at, profile, "
                    "file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    r,
                )
            self._db.execute("DELETE FROM worktree")
            if head_id is not None:
                self._db.execute(
                    "INSERT INTO branches (name, head, created_at) "
                    "VALUES ('main', ?, ?)",
                    (head_id, _utcnow()),
                )
                if lost_branches is not None:
                    lost_branches = [b for b in lost_branches if b != "main"]
                self._db.execute(
                    "INSERT INTO worktree (id, head_kind, head_branch, "
                    "head_pin, head_id, snapshot, stat_cache, "
                    "merge_theirs, flattened_pending, updated_at) "
                    "VALUES (1, 'branch', 'main', NULL, ?, ?, NULL, NULL, "
                    "NULL, ?)",
                    (head_id, head_tree, _utcnow()),
                )
            else:
                self._db.execute(
                    "INSERT INTO worktree (id, head_kind, head_branch, "
                    "head_pin, head_id, snapshot, stat_cache, "
                    "merge_theirs, flattened_pending, updated_at) "
                    "VALUES (1, 'branch', 'main', NULL, NULL, NULL, NULL, "
                    "NULL, NULL, ?)",
                    (_utcnow(),),
                )
        return {
            "status": "rebuilt",
            "layers": len(rows),
            "main_head": head_id,
            "lost_branches": (
                sorted(lost_branches) if lost_branches is not None else None
            ),
        }

    def _archive_location(self, directory: str) -> tuple[Path, Path]:
        """Validate placement without creating even an empty directory."""
        try:
            arc = Path(directory).resolve()
            admin = self.firn.resolve()
        except (OSError, RuntimeError) as exc:
            raise FirnpackError(
                f"cannot resolve archive directory {directory!r}: {exc}"
            ) from None
        if arc == admin or admin in arc.parents:
            raise FirnpackError("the archive directory must be outside .firn")
        for ancestor in (arc, *arc.parents):
            with contextlib.suppress(OSError):
                if ancestor.samefile(self.firn):
                    raise FirnpackError("the archive directory must be outside .firn")
        ancestor = arc
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise FirnpackError(
                f"archive directory has a non-directory ancestor: {ancestor}"
            )
        return arc, ancestor

    def _write_compact_archive(self, arc: Path, layer_ids: set[str]) -> Path:
        """Own one exclusive output; finish and sync it before any rewrite."""
        missing = []
        parent = arc
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        arc.mkdir(parents=True, exist_ok=True)
        stem = f"firnpack-archive-{_utcnow().replace(':', '')}"
        for suffix in range(1000):
            name = stem + (f"-{suffix}" if suffix else "") + ".zip"
            candidate = arc / name
            try:
                # Explicit close preserves the first failure during cleanup.
                raw = open(candidate, "xb")  # noqa: SIM115
            except OSError:
                if os.path.lexists(candidate):
                    continue
                raise
            break
        else:
            raise FirnpackError(
                "too many existing archive names; choose another archive directory"
            )
        zf = None
        owned_identity = None
        try:
            st = os.fstat(raw.fileno())
            owned_identity = (st.st_dev, st.st_ino)
            zf = zipfile.ZipFile(raw, "w", zipfile.ZIP_STORED)
            for lid in sorted(layer_ids):
                zf.write(self._layer_path(lid), f"{lid}.firn")
            zf.close()
            zf = None
            raw.flush()
            os.fsync(raw.fileno())
            raw.close()
            _fsync_dir(arc)
            for directory in missing:
                _fsync_dir(directory.parent)
        except BaseException:
            if zf is not None:
                with contextlib.suppress(BaseException):
                    zf.close()
            with contextlib.suppress(BaseException):
                raw.close()
            with contextlib.suppress(OSError):
                st = candidate.lstat()
                if owned_identity is None or (st.st_dev, st.st_ino) == owned_identity:
                    candidate.unlink()
            raise
        return candidate

    def compact(
        self,
        keep: int,
        *,
        archive_dir: str | None = None,
        dry_run: bool = False,
        yes: bool = False,
    ) -> dict:
        """Squash old layers, reclaim disk (SPEC 9.9). See _compact.

        Wrapper guarantee: new layer files are written BEFORE the swap
        transaction, so a failure anywhere after the first write must
        not strand them — they have no database row, and an orphan
        file fails `verify` while nothing else would ever sweep it."""
        self._compact_wrote = False
        self._compact_archive_path = None
        try:
            return self._compact(
                keep, archive_dir=archive_dir, dry_run=dry_run, yes=yes
            )
        except BaseException:
            if self._compact_wrote:
                with contextlib.suppress(Exception):
                    self._sweep_orphans()
            if self._compact_archive_path is not None:
                print(
                    f"warning: archive saved to {self._compact_archive_path} "
                    "before the later failure.",
                    file=sys.stderr,
                )
            raise

    def _compact(
        self,
        keep: int,
        *,
        archive_dir: str | None = None,
        dry_run: bool = False,
        yes: bool = False,
    ) -> dict:
        """Squash old layers, reclaim disk (SPEC 9.9 — owner-mandated,
        never automatic, explicit permission only).

        --keep N (required, no default — an agent must state it every
        time): keep the newest N layers of the head's first-parent
        chain; everything below the cut becomes one foundation layer.
        Kept layers are re-parented onto it (layer_ids change —
        parentage is identity, SPEC 5).

        Refusals (a history rewrite checks first): pending merge; any
        other branch head or pin reaching into the collapsed set; a
        kept merge layer whose second_parent reaches below the cut.
        A kept merge whose second parent is collapsed is refused, so
        --keep 1 is the deep clean only when no kept merge reaches below
        the cut; otherwise raise --keep until that second parent is kept.

        Crash ordering: archive first, then write ALL new layers, then
        ONE DB transaction, then delete old files (both crash windows
        safe: pre-swap -> old history intact; post-swap -> new
        authoritative, old swept).

        Peak-disk checks include the archive and run before directory or
        history writes. Concurrent space depletion can still cause ENOSPC;
        archive failures leave the old history intact.
        """
        arc = arc_ancestor = None
        if archive_dir:
            arc, arc_ancestor = self._archive_location(archive_dir)
        wt = self._worktree()
        if wt["merge_theirs"] is not None:
            raise FirnpackError(
                "a merge is pending — resolve it, or merge --abort, before compacting"
            )
        if keep < 1:
            raise FirnpackError("--keep must be >= 1")
        # first-parent chain from head, newest first
        chain = []
        cursor = wt["head_id"]
        seen = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            row = self._db.execute(
                "SELECT parent FROM layers WHERE id = ?", (cursor,)
            ).fetchone()
            cursor = row[0] if row else None
        if keep >= len(chain):
            return {
                "status": "nothing_to_compact",
                "layers": len(chain),
            }
        kept = chain[:keep]
        collapsed = set(chain[keep:])

        # --- reference-based safety analysis (SPEC 9.9, owner item
        # (a) 2026-10-20) ---
        # Rows going away: collapsed ones, plus every layer the
        # rewrite replaces (kept chain layers AND off-chain survivors
        # that hang off them — a branch forked from the kept range
        # follows the rewrite instead of blocking it). Anything still
        # pointing at the deleted set is remapped, rewritten, refused,
        # or garbage-collected:
        #   - branch heads at a rewritten id (kept or survivor) ->
        #     remapped to the new id
        #   - branch heads or the pin inside collapsed -> refusal
        #   - a reachable survivor whose ancestry passes through
        #     COLLAPSED rows -> refusal: that history is genuinely
        #     squashed away; a fork on top of KEPT rows is rewritten
        #     instead (no cliff for the common working-branch case)
        #   - an unreachable survivor referencing the deleted set ->
        #     garbage-collected (amend orphans, second-parent stubs
        #     of collapsed merges), archived like any dropped layer
        deleted = collapsed | set(kept)
        all_rows = {
            r[0]: (r[1], r[2])
            for r in self._db.execute("SELECT id, parent, second_parent FROM layers")
        }
        head_names = {
            h: n for n, h in self._db.execute("SELECT name, head FROM branches")
        }
        if wt["head_kind"] == "pin" and wt["head_id"] in collapsed:
            raise FirnpackError("the pinned head reaches into the collapsed range")
        for lid in kept:
            second = all_rows[lid][1]
            if second is not None and second in collapsed:
                raise FirnpackError(
                    f"kept layer {lid[:12]} is a merge whose second parent "
                    f"{second[:12]} falls below the cut — raise --keep so the "
                    f"second parent is kept too"
                )
        # live roots in old-row space: branch heads outside the
        # deleted set, plus kept merges' second parents (rewritten
        # kept layers keep referencing them)
        roots = {h for h in head_names if h not in deleted}
        for lid in kept:
            s = all_rows[lid][1]
            if s is not None and s not in deleted:
                roots.add(s)
        reachable: set[str] = set()
        stack = list(roots)
        while stack:
            cur = stack.pop()
            if cur in reachable or cur in deleted or cur not in all_rows:
                continue
            reachable.add(cur)
            for ref in all_rows[cur]:
                if ref:
                    stack.append(ref)
        # refusal: a reachable survivor whose ancestry passes through
        # COLLAPSED rows loses genuine history — refuse, naming the
        # branch when the survivor is itself a head
        for rid in sorted(reachable - deleted):
            p, s = all_rows[rid]
            if p in collapsed or s in collapsed:
                who = head_names.get(rid)
                if who:
                    raise FirnpackError(
                        f"branch {who!r} descends from layers that would be "
                        f"collapsed — raise --keep, or merge/delete the branch first"
                    )
                # no branch points here: the reference is structural (a
                # merge edge or a surviving fork). Deleting a branch cannot
                # clear it, so do not advise that.
                raise FirnpackError(
                    f"layer {rid[:12]} is still referenced, but its ancestry "
                    f"reaches into the collapsed range — raise --keep "
                    f"(a merge edge is cleared only by a higher --keep)"
                )
        # garbage: UNREACHABLE rows referencing anything that vanishes
        # (deleted chain rows or rewritten survivors) or already-
        # collected garbage — amend orphans, collapsed-merge stubs,
        # orphans left behind on rewritten forks. Monotone fixpoint:
        # every wave row leaves the candidate pool by joining gc, so
        # the loop terminates.
        vanish = deleted | reachable
        gc: set[str] = set()
        while True:
            wave = {
                rid
                for rid, (p, s) in all_rows.items()
                if rid not in vanish
                and rid not in gc
                and (p in vanish or p in gc or s in vanish or s in gc)
            }
            if not wave:
                break
            gc |= wave
        # the rewrite set: kept chain layers plus every reachable
        # survivor (forks, stacked branches, merge stubs) — each is
        # re-parented through remap, so no live line of history is
        # stranded on a deleted id
        rewrite_set = set(kept) | (reachable - deleted)
        branch_remaps: dict[str, str] = {}
        for name, head in self._db.execute("SELECT name, head FROM branches"):
            if name == wt.get("head_branch"):
                continue  # the worktree branch repoints to final_head
            if head in collapsed:
                raise FirnpackError(
                    f"branch {name!r} points into the collapsed range — "
                    f"raise --keep or delete the branch first"
                )
            if head in rewrite_set:
                branch_remaps[name] = head

        # the cut layer's tree becomes the foundation
        cut_id = chain[keep]
        _, cut_manifest, _ = self._read_layer(cut_id)
        old_total = sum(f.stat().st_size for f in self.layers_dir.glob("*.firn"))
        new_writes = 1 + len(rewrite_set)  # foundation + every rewrite
        avg = old_total / max(len(all_rows), 1)
        peak = old_total + int(avg * new_writes)
        archive_need = 0
        if arc is not None:
            archive_need = (
                sum(self._layer_path(lid).stat().st_size for lid in collapsed | gc)
                + 1024 * len(collapsed | gc)
                + 4096
            )
            if arc_ancestor.stat().st_dev == self.root.stat().st_dev:
                peak += archive_need

        if dry_run:
            return {
                "status": "plan",
                "keep": keep,
                "layers_total": len(chain),
                "layers_dropped": len(collapsed),
                "layers_rewritten": len(rewrite_set),
                "off_chain_rewritten": len(rewrite_set) - len(kept),
                "layers_garbage_collected": len(gc),
                # "to" is unknowable at plan time: rewritten ids hash a
                # record whose created_at is stamped at execution
                "branches_remapped": [
                    {"name": n, "from": old} for n, old in branch_remaps.items()
                ],
                "bytes_reclaimed": sum(
                    self._layer_path(lid).stat().st_size for lid in collapsed | gc
                ),
                "peak_disk_needed": peak,
            }

        if not yes:
            raise FirnpackError(
                "compact rewrites history and deletes layer files — "
                "pass --yes to confirm (or --dry-run to preview)"
            )
        free = shutil.disk_usage(self.root).free
        if free < peak:
            raise FirnpackError(
                f"not enough disk for the rewrite: ~{peak - free} bytes "
                f"short of the peak need (~{peak}). Compact writes new "
                f"layers before deleting old ones (crash safety); free "
                f"space, or copy the glacier to a roomier drive and "
                f"compact there — it is self-contained."
            )
        if arc is not None and shutil.disk_usage(arc_ancestor).free < archive_need:
            raise FirnpackError(
                f"not enough free disk space for the archive (~{archive_need} bytes); "
                "choose a roomier archive destination and try again"
            )

        # archive first (SPEC 9.9: export before deletion)
        archive_path = None
        if arc is not None:
            archive_path = self._write_compact_archive(arc, collapsed | gc)
            self._compact_archive_path = archive_path

        # build the foundation layer: tree of the cut, new root
        foundation_record = {
            "format_version": FORMAT_VERSION,
            "tree_id": self._db.execute(
                "SELECT tree_id FROM layers WHERE id = ?", (cut_id,)
            ).fetchone()[0],
            "parent": None,
            "second_parent": None,
            "message": (
                f"compact: {len(collapsed)} layers collapsed "
                f"({collapsed and min(collapsed)[:12]}..{cut_id[:12]}), "
                f"{len(chain)} -> {len(kept) + 1} layers"
            ),
            "author": self.author,
            "created_at": _utcnow(),
        }
        foundation_id = _layer_id(foundation_record)
        fmeta = dict(foundation_record)
        fmeta["layer_id"] = foundation_id
        fmeta["profile"] = "gzip"
        # the foundation's tree IS the cut layer's tree: copy it from
        # the cut layer itself. _write_layer reads the WORKTREE (which
        # sits at HEAD, not at the cut) — using it here crashed on any
        # cut file missing from HEAD and silently gave same-named files
        # HEAD's bytes in the oldest history.
        cut_files, cut_links = [], []
        for path, v in sorted(cut_manifest.items()):
            if v["target"] is not None:
                cut_links.append(path)
            else:
                cut_files.append(path)
        self._compact_wrote = True
        fsize = self._rewrite_layer_from(
            cut_id, foundation_id, fmeta, cut_files, cut_links
        )

        # rewrite every layer of the rewrite set onto the foundation:
        # the kept chain first (oldest first), then off-chain
        # survivors (forks, stacked branches, merge stubs) once their
        # parents are placed — topological order keeps every parent
        # reference resolvable as the walk runs
        remap = {cut_id: foundation_id}
        new_rows = []
        # topological order over the WHOLE rewrite set (kept chain AND
        # survivors together): a kept merge layer's second parent can be
        # an off-chain survivor, which must be rewritten first or the
        # merge would be re-parented onto an id that is about to be
        # deleted. Parents/second parents outside the rewrite set are
        # either the cut (remapped to the foundation) or untouched.
        order: list[str] = []
        placed = {cut_id}
        pending = set(rewrite_set)
        while pending:
            progressed = False
            for lid in sorted(
                pending, key=lambda x: chain.index(x) if x in chain else -1
            ):
                p, s = all_rows[lid]
                ready = all(
                    ref is None or ref in placed or ref not in rewrite_set
                    for ref in (p, s)
                )
                if ready:
                    order.append(lid)
                    placed.add(lid)
                    pending.discard(lid)
                    progressed = True
            if not progressed:
                # the guard phase proved this unreachable: every
                # survivor's deleted-set references were refused or
                # collected before we got here
                raise FirnpackError(
                    "internal: rewrite order stuck — this is a bug, "
                    "report it with the repository"
                )
        for lid in order:
            row = self._db.execute(
                "SELECT tree_id, parent, second_parent, message, author,"
                " created_at FROM layers WHERE id = ?",
                (lid,),
            ).fetchone()
            tree_id, parent, second, message, author, created = row
            new_parent = remap.get(parent, parent)
            new_second = remap.get(second, second) if second else None
            record = {
                "format_version": FORMAT_VERSION,
                "tree_id": tree_id,
                "parent": new_parent,
                "second_parent": new_second,
                "message": message,
                "author": author,
                "created_at": created,
            }
            new_id = _layer_id(record)
            remap[lid] = new_id
            meta = dict(record)
            meta["layer_id"] = new_id
            meta["profile"] = "gzip"
            # copy the layer file byte-for-byte except meta: simplest
            # correct path is re-writing the tree from the old layer
            _, manifest, _ = self._read_layer(lid)
            kfiles = [p for p, v in manifest.items() if v["target"] is None]
            klinks = [p for p, v in manifest.items() if v["target"] is not None]
            # _write_layer reads from the worktree — but the worktree
            # is at HEAD, not at this layer. Write from the layer's own
            # contents instead.
            size = self._rewrite_layer_from(lid, new_id, meta, kfiles, klinks)
            new_rows.append(
                (
                    new_id,
                    tree_id,
                    new_parent,
                    new_second,
                    message,
                    author,
                    created,
                    "gzip",
                    None,
                    size,
                )
            )
        # fill file_count from old rows
        new_rows = [
            (
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6],
                r[7],
                self._db.execute(
                    "SELECT file_count FROM layers WHERE id = ?", (lid,)
                ).fetchone()[0],
                r[9],
            )
            for r, lid in zip(new_rows, order, strict=True)
        ]

        # ONE DB transaction: swap everything. defer_foreign_keys
        # moves FK enforcement to COMMIT, so intra-transaction
        # statement ordering no longer matters — what matters is that
        # the FINAL state is consistent: every referencer of a deleted
        # row was remapped, garbage-collected, or refused above.
        final_head = remap[kept[0]]
        with self._db:
            self._db.execute("PRAGMA defer_foreign_keys = ON")
            self._db.execute(
                "INSERT OR REPLACE INTO layers (id, tree_id, parent, "
                "second_parent, message, author, created_at, profile, "
                "file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    foundation_id,
                    foundation_record["tree_id"],
                    None,
                    None,
                    foundation_record["message"],
                    self.author,
                    foundation_record["created_at"],
                    "gzip",
                    len(cut_files) + len(cut_links),
                    fsize,
                ),
            )
            for r in new_rows:
                self._db.execute(
                    "INSERT OR REPLACE INTO layers (id, tree_id, parent,"
                    " second_parent, message, author, created_at, "
                    "profile, file_count, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    r,
                )
            if wt["head_kind"] == "branch":
                self._db.execute(
                    "UPDATE branches SET head = ? WHERE name = ?",
                    (final_head, wt["head_branch"]),
                )
            for name, old in branch_remaps.items():
                self._db.execute(
                    "UPDATE branches SET head = ? WHERE name = ?",
                    (remap[old], name),
                )
            self._db.execute(
                "UPDATE worktree SET head_id = ?, head_pin = ?, "
                "updated_at = ? WHERE id = 1",
                (
                    final_head,
                    final_head if wt["head_kind"] == "pin" else None,
                    _utcnow(),
                ),
            )
            for lid in gc | (rewrite_set - set(kept)):
                self._db.execute("DELETE FROM layers WHERE id = ?", (lid,))
            for lid in sorted(
                collapsed | set(kept),
                key=lambda x: chain.index(x) if x in chain else 10**9,
            ):
                self._db.execute("DELETE FROM layers WHERE id = ?", (lid,))
        # delete now-replaced old files (write-once discipline ends
        # only here, under explicit permission): collapsed content,
        # collected garbage, and the old ids of every rewritten layer
        for lid in collapsed | rewrite_set | gc:
            old = self._layer_path(lid)
            if old.exists() and lid not in remap.values():
                with contextlib.suppress(OSError):
                    old.unlink()
        self._sweep_orphans()
        return {
            "status": "compacted",
            "kept": len(kept) + 1,
            "dropped": len(collapsed),
            "off_chain_rewritten": len(rewrite_set) - len(kept),
            "garbage_collected": len(gc),
            "branches_remapped": [
                {"name": n, "from": old, "to": remap[old]}
                for n, old in branch_remaps.items()
            ],
            "new_head": final_head,
            "archive": str(archive_path) if archive_path else None,
        }

    def _rewrite_layer_from(self, old_id, new_id, meta, files, links) -> int:
        """Write a new layer whose tree equals an existing layer's,
        with new meta. Reads content from the OLD layer, not the
        worktree."""
        _check_storable(files, links)
        manifest = self._read_layer(old_id)[1]
        regular = {p for p, v in manifest.items() if v["target"] is None}
        contents = self._layer_contents(old_id, regular)
        tmp = self.layers_dir / f"{new_id}.firn.tmp"
        meta_bytes = _canonical_json(meta)
        try:
            with open(tmp, "wb") as raw:
                with (
                    gzip.GzipFile(
                        fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0
                    ) as gz,
                    tarfile.open(
                        fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT
                    ) as tar,
                ):
                    ti = _CanonicalTarInfo(META_MEMBER)
                    ti.size = len(meta_bytes)
                    ti.mtime = 0
                    ti.mode = 0o644
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    tar.addfile(ti, io.BytesIO(meta_bytes))
                    for path in sorted(files, key=_stored_utf8):
                        v = manifest[path]
                        data = contents[path]
                        ti = _CanonicalTarInfo(path)
                        ti.size = len(data)
                        ti.mtime = 0
                        ti.uid = ti.gid = 0
                        ti.uname = ti.gname = ""
                        ti.mode = v.get("mode", 0o644)
                        tar.addfile(ti, io.BytesIO(data))
                    for path in sorted(links, key=_stored_utf8):
                        ti = _CanonicalTarInfo(path)
                        ti.type = tarfile.SYMTYPE
                        ti.linkname = manifest[path]["target"]
                        ti.mtime = 0
                        ti.uid = ti.gid = 0
                        ti.uname = ti.gname = ""
                        ti.mode = 0o777
                        tar.addfile(ti)
                raw.flush()
                os.fsync(raw.fileno())
            _replace_with_retry(tmp, self._layer_path(new_id))
            _fsync_dir(self.layers_dir)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        return self._layer_path(new_id).stat().st_size

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

    def _scan_markers(self, entries) -> list[str]:
        """Tracked text files holding a line that STARTS with <<<<<<< or
        >>>>>>> (SPEC §11). Works on the bytes commit() already read for
        hashing (no second disk pass). A UTF-8 BOM before the first marker
        is ignored, and only \\n, \\r\\n and \\r end a line: form feeds, NEL
        and the Unicode line separators inside a line are not line breaks."""
        marked = []
        for path, (_size, data, _target, _exec) in entries.items():
            if data is None or b"\0" in data:
                continue  # symlink or binary
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            if _MARKER_LINE.search(data):
                marked.append(path)
        return sorted(marked)

    def _check_markers(self, entries, *, allow: bool, pending_merge: bool) -> None:
        """The unresolved-marker guard shared by commit and amend (SPEC §9.2,
        §11; owner decision 2026-10-22). Refuse, or warn when allowed."""
        marked = self._scan_markers(entries)
        if not marked:
            return
        block = self._cfg.getboolean("merge", "block_unresolved_markers", fallback=True)
        if block and not allow:
            raise MergeConflict(marked, pending_merge=pending_merge)
        print(
            f"warning: conflict markers remain in: {', '.join(marked)}",
            file=sys.stderr,
        )

    def _unresolved_binary_conflicts(self, merge_theirs: str, entries) -> list[str]:
        """Paths of a pending merge whose BINARY conflict nobody has decided:
        both sides changed the file differently, one of them holds a NUL byte
        (no text merge exists), and the worktree still holds exactly OUR
        version. Committing would silently drop theirs (owner decision
        2026-10-23). Detected from the layers, so no extra state is stored."""
        head = self._worktree()["head_id"]
        base_id = self._lca(head, merge_theirs)
        if base_id is None:
            return []
        base = self._tree_of(base_id)
        ours = self._tree_of(head)
        theirs = self._tree_of(merge_theirs)
        undecided = []
        for path in sorted(set(ours) & set(theirs)):
            o, t, b = ours[path], theirs[path], base.get(path)
            if o == t or b in (o, t):
                continue  # not a both-changed-differently conflict
            if o[1] is not None or t[1] is not None:
                continue  # symlinks have their own rule
            if b"\x00" not in o[0] and b"\x00" not in t[0]:
                continue  # text: markers or a clean merge, handled elsewhere
            mine = entries.get(path)
            if mine is not None and mine[1] == o[0]:
                undecided.append(path)
        return undecided

    def _maybe_remind(self, result: dict) -> None:
        """The history reminder is a courtesy printed after a commit that has
        ALREADY landed, so it must never turn that success into a failure."""
        try:
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
        except (sqlite3.Error, ValueError):
            result.pop("history_reminder", None)

    def _sweep_orphans(self) -> None:
        """Remove crash leftovers: stray .tmp files and layer files with no
        DB row (SPEC §13). Referenced layers are never touched. A file that
        cannot be removed (locked by OneDrive or antivirus) is reported on
        stderr rather than silently kept; the next commit, amend or compact
        tries again."""
        stuck: list[str] = []
        for tmp in list(self.layers_dir.glob("*.tmp")):
            try:
                tmp.unlink()
            except OSError:
                stuck.append(tmp.name)
        known = {row[0] for row in self._db.execute("SELECT id FROM layers")}
        for layer_file in list(self.layers_dir.glob("*.firn")):
            if layer_file.stem not in known:
                try:
                    layer_file.unlink()
                except OSError:
                    stuck.append(layer_file.name)
        if stuck:
            print(
                f"warning: could not remove leftover file(s) in "
                f"{self.layers_dir}: {', '.join(sorted(stuck))} (they are not "
                f"part of your history; the next commit, amend or compact "
                f"will try again)",
                file=sys.stderr,
            )

    def status(self, *, full: bool = False, list_ignored: bool = False) -> dict:
        files, symlinks, ignored, nested = self._walk()
        wt = self._worktree()
        cache = json.loads(wt["stat_cache"] or "{}")
        if wt["merge_theirs"] is not None:
            # Mid-merge the stat cache describes the files the merge itself
            # just wrote (markers included), so the fast path would call a
            # half-resolved worktree "clean". Drift is measured against the
            # head layer (docs/usage.md), so rehash everything.
            full = True

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
        if ref and len(ref) < 6 and all(c in "0123456789abcdef" for c in ref):
            raise FirnpackError(
                f"no branch named {ref!r}, and as a layer id it is too short "
                f"(use at least 6 hex characters)"
            )
        raise FirnpackError(f"no branch or layer named {ref!r}")

    def _reachable(self, starts) -> set[str]:
        """Every layer reachable from `starts` through parent and
        second_parent links (the starts themselves included)."""
        links = {
            r[0]: (r[1], r[2])
            for r in self._db.execute("SELECT id, parent, second_parent FROM layers")
        }
        seen: set[str] = set()
        stack = [s for s in starts if s]
        while stack:
            lid = stack.pop()
            if lid in seen:
                continue
            seen.add(lid)
            stack.extend(p for p in links.get(lid, ()) if p)
        return seen

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
        if row:
            layer_id = row[0]
            head_change = ("branch", ref)
        else:
            layer_id = self._resolve_id(ref)
            head_change = ("pin", None)
        # Preflight BEFORE the pointer changes: a protected blocker or an
        # impossible transition refuses with the worktree untouched.
        baseline = set(self._read_layer(wt["head_id"])[1]) if wt["head_id"] else set()
        prepared = self._prepare_tree_write(
            set(self._read_layer(layer_id)[1]), baseline, rescued_ok=False
        )
        self._move_head_and_sync(
            wt,
            layer_id,
            wt["head_kind"],
            prepared=prepared,
            head_change=head_change,
        )
        return {"ref": ref, "kind": head_change[0], "head": layer_id}

    def _sync_worktree_to(
        self,
        layer_id: str,
        *,
        baseline: set[str] | None = None,
        prepared: tuple[set[str], set[str]] | None = None,
        rescued_ok: bool = False,
        created_paths: set[str] | None = None,
        created_dirs: set[str] | None = None,
    ) -> None:
        """Extract the layer's tree into the worktree and refresh the
        snapshot/stat cache. Two ordered phases:

        1. The layer is read and validated in ONE pass, then
           _prepare_tree_write inspects every target leaf and ancestor
           (deletion baseline = the previous-state manifest, never
           stat_cache). A damaged layer or a protected worktree entry
           aborts here, before anything is deleted.
        2. Only then are eligible leaves unlinked, EMPTY obstructing
           directories removed with rmdir, and leaves written with
           exclusive create — nothing pre-existing is ever truncated.

        `created_paths`/`created_dirs` (when given) collect what this
        call actually created, so a caller's rollback removes only its
        own work."""
        meta, manifest, _ = self._read_layer(layer_id)
        _warn_patch_level_once()
        regular_paths = {p for p, v in manifest.items() if v["target"] is None}
        contents = self._layer_contents(layer_id, regular_paths)

        if prepared is None:
            if baseline is None:
                raise FirnpackError(
                    "internal error: _sync_worktree_to requires an explicit "
                    "deletion baseline (the previous-state manifest)"
                )
            prepared = self._prepare_tree_write(
                set(manifest), baseline, rescued_ok=rescued_ok
            )
        self._apply_prepared(prepared)

        directory_links = _directory_symlinks(
            {p: v["target"] for p, v in manifest.items()}
        )
        entries = {}
        stats = {}
        for path in sorted(manifest):
            v = manifest[path]
            fp = self.root / path
            self._ensure_parents(fp, created_dirs)
            if v["target"] is not None:
                _symlink(fp, v["target"], target_is_directory=path in directory_links)
                if created_paths is not None:
                    created_paths.add(path)
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
                _write_file(fp, data)
                if created_paths is not None:
                    created_paths.add(path)
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
            allow_binary_conflict=args.allow_binary_conflict,
        )
    except (MergeConflict, BinaryConflict) as exc:
        if args.json:
            # SPEC §9.2: the refusal is machine-readable too; the humane
            # sentence still goes to stderr via main()
            key = (
                "blocked_by_binary_conflict"
                if isinstance(exc, BinaryConflict)
                else "blocked_by_markers"
            )
            print(json.dumps({"status": key, key: exc.paths}, indent=2))
        raise
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
    return 0


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


def _cmd_amend(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.amend(args.message, allow_markers=args.allow_markers)
    except MergeConflict as exc:
        if args.json:
            payload = {"status": "blocked_by_markers", "blocked_by_markers": exc.paths}
            print(json.dumps(payload, indent=2))
        raise
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["status"] == "nothing_to_amend":
        print("nothing to amend — tree and message unchanged")
        return 0
    print(
        f"amended: layer {result['layer_id'][:12]} (replaced {result['replaced'][:12]})"
    )
    return 0


def _cmd_restore(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.restore(args.ref, to_dir=args.to_dir)
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["status"] == "exported":
        print(f"extracted layer {result['layer_id'][:12]} to {result['to']}")
        return 0
    rescue = result.get("rescue_layer")
    print(f"restored layer {result['layer_id'][:12]} into the worktree")
    prev = result.get("previous_head")
    if prev and prev != result["layer_id"]:
        print(
            f"the head moved from {prev[:12]} to {result['layer_id'][:12]}; "
            f"to come back: firnpack restore {prev[:12]}"
        )
    if rescue:
        print(
            f"your uncommitted work was saved as layer {rescue[:12]} "
            f"(export it with: firnpack export {rescue[:12]} --to DIR)"
        )
    return 0


def _cmd_export(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.export(args.ref, args.to_dir)
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"exported layer {result['layer_id'][:12]} to {result['to']}")
    return 0


def _cmd_verify(args) -> int:
    repo = Repo.discover(recovering=bool(args.rebuild_db))
    try:
        if args.rebuild_db:
            rebuilt = repo.rebuild_db()
            verify_result = repo.verify()
            if args.json:
                print(json.dumps({**rebuilt, "verify": verify_result}, indent=2))
                return 0
            head = rebuilt["main_head"]
            print(
                f"rebuilt database from {rebuilt['layers']} layer(s); "
                f"main at {head[:12] if head else '(no layers)'}"
            )
            lost = rebuilt["lost_branches"]
            if lost is None:
                print(
                    "lost branches: unknown — the database was missing or "
                    "empty, so branch names could not be recovered"
                )
            elif lost:
                print(
                    "lost (branch names live only in the database): " + ", ".join(lost)
                )
            print(
                f"verify ok — {verify_result['layers']} layers re-hashed and consistent"
            )
            return 0
        result = repo.verify()
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"verify ok — {result['layers']} layers re-hashed and consistent")
    return 0


def _cmd_compact(args) -> int:
    repo = Repo.discover()
    try:
        result = repo.compact(
            args.keep,
            archive_dir=args.archive_dir,
            dry_run=args.dry_run,
            yes=args.yes,
        )
    finally:
        repo.close()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] != "error" else 1
    status = result["status"]
    if status == "nothing_to_compact":
        print(f"nothing to compact — {result['layers']} layers on the chain")
        return 0
    if status == "plan":
        print("compact plan (dry run):")
        print(f"  layers total:    {result['layers_total']}")
        print(f"  layers dropped:  {result['layers_dropped']}")
        print(f"  layers rewritten:{result['layers_rewritten']}")
        if result["layers_garbage_collected"]:
            print(f"  garbage collected:{result['layers_garbage_collected']}")
        print(f"  bytes reclaimed: {result['bytes_reclaimed']}")
        print(f"  peak disk needed:{result['peak_disk_needed']}")
        for b in result["branches_remapped"]:
            print(
                f"  branch {b['name']!r} at {b['from'][:12]} will follow "
                f"the rewrite (remapped to its new id)"
            )
        print("  run again with --yes to execute")
        return 0
    if status == "compacted":
        print(
            f"compacted: {result['kept']} layers kept, "
            f"{result['dropped']} dropped; new head "
            f"{result['new_head'][:12]}"
        )
        if result["garbage_collected"]:
            print(
                f"  {result['garbage_collected']} unreachable layer(s) "
                f"garbage-collected"
            )
        for b in result["branches_remapped"]:
            print(
                f"  branch {b['name']!r} remapped {b['from'][:12]} -> "
                f"{b['to'][:12]} (follows the rewrite)"
            )
        if result["archive"]:
            print(f"  removed layers archived to {result['archive']}")
        return 0
    print(f"compact: {status}")
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
    except MergeConflict as exc:
        # A clean three-way result can still contain marker-shaped text.
        # commit() leaves the merge pending; report the same JSON refusal
        # as an explicit commit, then let main() print the human sentence.
        if args.json:
            print(
                json.dumps(
                    {"status": "blocked_by_markers", "blocked_by_markers": exc.paths},
                    indent=2,
                )
            )
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
        rescue = result.get("rescue_layer")
        line = f"merge aborted — back at {result['head'][:12]}"
        if rescue:
            # the rescue id is printed, not just stored: the layer is
            # reachable only by id (no branch tip points at it), so
            # this line is the user's map to their recovered work
            line += (
                f"\nyour uncommitted work was saved as layer "
                f"{rescue[:12]} (restore it with: firnpack export "
                f"{rescue[:12]} --to DIR)"
            )
        print(line)
        return 0
    if status == "conflict":
        print(
            f"merge conflict in {len(result['conflicts'])} file(s):",
            file=sys.stderr,
        )
        for path in result["conflicts"]:
            kept = result.get("kept_sides", {}).get(path)
            detail = f" (kept modified side: {kept})" if kept else ""
            print(f"  {path}{detail}", file=sys.stderr)
        print(
            f"source layers: ours {result['head']}, theirs {result['theirs']}. "
            f"Inspect either with: firnpack export <layer-id> --to DIR",
            file=sys.stderr,
        )
        print(
            "resolve the listed files, then commit: text conflicts have "
            "markers (<<<<<<< ... >>>>>>>); binary conflicts keep ours "
            "(replace the file, or use commit --allow-binary-conflict to "
            "keep ours on purpose). Or: firnpack merge --abort",
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
        source = repo._db.execute(
            "SELECT 1 FROM branches WHERE name = ?", (args.old,)
        ).fetchone()
        if source is None:
            raise FirnpackError(f"no branch named {args.old!r}")
        if not args.new or "/" in args.new or args.new.startswith("."):
            raise FirnpackError(f"invalid branch name {args.new!r}")
        row = repo._db.execute(
            "SELECT 1 FROM branches WHERE name = ?", (args.new,)
        ).fetchone()
        if row:
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
        remaining = [h for (h,) in repo._db.execute("SELECT head FROM branches")]
        remaining += [x for x in (wt["head_id"], wt["merge_theirs"]) if x]
        stranded = row[0] not in repo._reachable(remaining)
    finally:
        repo.close()
    print(f"deleted branch {args.name} (layers remain on disk)")
    if stranded:
        # documented in SPEC §9.4 / usage: deleting is allowed, silently
        # stranding work is not
        print(
            f"warning: layer {row[0][:12]} (the tip of {args.name!r}) is no "
            f"longer reachable from any branch or from where you are now. It "
            f"stays on disk until a compact. To keep it: "
            f"firnpack branch {args.name} {row[0][:12]}",
            file=sys.stderr,
        )
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
    p_commit.add_argument(
        "--allow-binary-conflict",
        action="store_true",
        help="finish a merge keeping YOUR version of an undecided binary file",
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

    p_amend = sub.add_parser("amend", help="replace the head layer")
    p_amend.add_argument("-m", "--message", default=None)
    p_amend.add_argument(
        "--allow-markers",
        action="store_true",
        help="amend despite leftover conflict markers",
    )
    p_amend.add_argument("--json", action="store_true")
    p_amend.set_defaults(func=_cmd_amend)

    p_restore = sub.add_parser(
        "restore", help="extract a layer over the worktree (or --to DIR)"
    )
    p_restore.add_argument("ref")
    p_restore.add_argument(
        "--to", dest="to_dir", default=None, help="extract to a directory"
    )
    p_restore.add_argument("--json", action="store_true")
    p_restore.set_defaults(func=_cmd_restore)

    p_export = sub.add_parser("export", help="extract a layer to a fresh directory")
    p_export.add_argument("ref")
    p_export.add_argument("--to", dest="to_dir", required=True)
    p_export.add_argument("--json", action="store_true")
    p_export.set_defaults(func=_cmd_export)

    p_verify = sub.add_parser("verify", help="re-hash all layers; check DB consistency")
    p_verify.add_argument(
        "--rebuild-db",
        action="store_true",
        help="reconstruct the database from in-band layer meta first",
    )
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=_cmd_verify)

    p_compact = sub.add_parser("compact", help="squash old layers, reclaim disk")
    p_compact.add_argument("--keep", type=int, required=True)
    p_compact.add_argument("--archive", dest="archive_dir", default=None)
    p_compact.add_argument(
        "--dry-run", action="store_true", help="plan without mutating"
    )
    p_compact.add_argument("--yes", action="store_true", help="confirm the rewrite")
    p_compact.add_argument("--json", action="store_true")
    p_compact.set_defaults(func=_cmd_compact)

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
    except BinaryConflict as exc:
        print(
            f"firnpack: the merge left a binary file undecided: "
            f"{', '.join(exc.paths)}. Committing now would silently keep YOUR "
            f"version and drop theirs. Copy theirs in first (firnpack export "
            f"<their branch> --to DIR, then copy the file over), or commit "
            f"anyway on purpose with --allow-binary-conflict, or give up "
            f"with 'firnpack merge --abort'.",
            file=sys.stderr,
        )
        return 1
    except MergeConflict as exc:
        if exc.pending_merge:
            print(
                f"firnpack: merge conflict — resolve these files first: "
                f"{', '.join(exc.paths)}",
                file=sys.stderr,
            )
        else:
            print(
                f"firnpack: conflict markers (<<<<<<< / >>>>>>>) are still in: "
                f"{', '.join(exc.paths)}. Remove them and commit again, or "
                f"commit anyway with --allow-markers (or set [merge] "
                f"block_unresolved_markers = false in .firn/config).",
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
    except BrokenPipeError:
        # `firnpack history | head -1`: the reader left, which is not an
        # error. Point stdout at the null device so the interpreter's
        # shutdown flush cannot raise again, and leave quietly.
        with contextlib.suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "no such table" in text or "no such column" in text:
            # an empty or half-written database: store damage, not contention
            print(
                f"firnpack: integrity failure: glacier.db looks damaged "
                f"({text}); run 'firnpack verify --rebuild-db' to rebuild "
                f"the index from the layers",
                file=sys.stderr,
            )
            return 3
        print(
            f"firnpack: the database could not be used ({text}). If another "
            f"firnpack command, OneDrive or antivirus has it open, wait a "
            f"moment and try again; run 'firnpack verify' if in doubt.",
            file=sys.stderr,
        )
        return 1
    except sqlite3.DatabaseError as exc:
        print(
            f"firnpack: integrity failure: glacier.db looks damaged ({exc}); "
            f"run 'firnpack verify --rebuild-db' to rebuild the index from "
            f"the layers",
            file=sys.stderr,
        )
        return 3
    except OSError as exc:
        # a full disk, a read-only folder, a file held by OneDrive or
        # antivirus: one plain sentence naming the file, never a traceback
        where = f" ({exc.filename})" if exc.filename else ""
        print(
            f"firnpack: file system error: {exc.strerror or exc}{where}. "
            f"Check that the disk has free space and that no other program "
            f"(OneDrive, antivirus) is holding the file, then try again. "
            f"Run 'firnpack verify' to confirm your history is intact.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
