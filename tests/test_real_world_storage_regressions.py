"""Storage-layer regressions: archive safety, tar member encoding, and the
storage-adjacent contract gaps found by the real-world campaign.

These are API/unit-level tests with independent oracles (stdlib tarfile,
zipfile, sqlite read-only connections and raw directory listings) — no
firnpack internals are reimplemented here. The engine under test is the
``firnpack`` module as imported: a normal suite run picks up the checkout,
while the red-run harness preloads a pinned copy into ``sys.modules`` first.
"""

from __future__ import annotations

import errno
import gzip
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import FirnpackError, Repo  # noqa: E402

MODULE = Path(firnpack.__file__).resolve()
POSIX = os.name != "nt"
FROZEN = "2026-10-25T08:00:00+00:00"
# ustar name field is 100 bytes, prefix 155: these are the three member-name
# regimes a canonical layer writer must handle (SPEC §5; docs/storage §3).
UNSPLITTABLE_197 = "u" * 197  # one component, > 100 bytes, no '/' to split on
UNSPLITTABLE_PREFIX = "d" * 160 + "/" + "f" * 70  # '/' exists, prefix > 155
SPLITTABLE_LONG = "d" * 120 + "/" + "f" * 76  # prefix <= 155, name <= 100
UNICODE_NAME = "árbol-✓-ñ.txt"
BLOCK = 512


# ------------------------------------------------------------------ helpers


def _write(root: Path, rel: str, data: bytes | str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def _make_repo(root: Path, commits: int, *, author: str = "storage-test") -> Repo:
    root.mkdir(parents=True)
    repo = Repo.init(root, author=author)
    for i in range(commits):
        _write(root, "f.txt", f"content {i}\n")
        packed = repo.commit(f"c{i}")
        assert packed["status"] == "packed", packed
    return repo


def _firn(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _listing(root: Path) -> dict:
    """Every entry under ``root``: files as bytes, dirs and symlinks marked.

    Raw filesystem oracle — follows nothing, knows nothing about firnpack.
    """
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in sorted(dirnames + filenames):
            p = base / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", p.read_bytes())
    return out


def _store(repo: Repo) -> dict:
    return _listing(repo.firn)


def _layer_rows(repo: Repo) -> set:
    return {r[0] for r in repo._db.execute("SELECT id FROM layers")}


def _oldest_layer(repo: Repo) -> str:
    """First-parent chain end — walk parents, never sort (ids are hashes;
    created_at is second-granular)."""
    cursor = repo._worktree()["head_id"]
    oldest = cursor
    while cursor is not None:
        oldest = cursor
        row = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?", (cursor,)
        ).fetchone()
        cursor = row[0] if row else None
    return oldest


def _branches_and_head(db_path: Path) -> tuple:
    """Independent oracle: read-only SQLite, never a live Repo handle."""
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        branches = sorted(conn.execute("SELECT name, head FROM branches"))
        (head_branch,) = conn.execute(
            "SELECT head_branch FROM worktree WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    return branches, head_branch


def _freeze_clock(monkeypatch, stamp: str = FROZEN) -> None:
    monkeypatch.setattr(firnpack, "_utcnow", lambda: stamp)


def _archive_basename(stamp: str = FROZEN) -> str:
    return f"firnpack-archive-{stamp.replace(':', '')}.zip"


def _zips(root: Path) -> list:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


# ---- raw-tar oracle ---------------------------------------------------------


def _tar_records(raw: bytes) -> list[dict]:
    """Parse a decompressed tar stream into 512-byte member records."""
    out = []
    off = 0
    while off + BLOCK <= len(raw):
        blk = raw[off : off + BLOCK]
        if blk == b"\x00" * BLOCK:
            break
        size = int(blk[124:136].split(b"\x00", 1)[0].strip() or b"0", 8)
        prefix = blk[345:500].split(b"\x00", 1)[0]
        name = blk[0:100].split(b"\x00", 1)[0]
        out.append(
            {
                "offset": off,
                "block": blk,
                "name": (prefix + b"/" + name) if prefix else name,
                "typeflag": blk[156:157],
                "mode": int(blk[100:108].split(b"\x00", 1)[0].strip() or b"0", 8),
                "uid": int(blk[108:116].split(b"\x00", 1)[0].strip() or b"0", 8),
                "gid": int(blk[116:124].split(b"\x00", 1)[0].strip() or b"0", 8),
                "size": size,
                "mtime": int(blk[136:148].split(b"\x00", 1)[0].strip() or b"0", 8),
                "linkname": blk[157:257].split(b"\x00", 1)[0],
                "data": raw[off + BLOCK : off + BLOCK + size],
            }
        )
        off += BLOCK + ((size + BLOCK - 1) // BLOCK) * BLOCK
    return out


def _ustar_oracle(
    name: str,
    *,
    typeflag: bytes = tarfile.REGTYPE,
    size: int = 0,
    mode: int = 0o644,
    linkname: str = "",
) -> bytes:
    """The exact 512-byte header a canonical member must serialize to —
    stdlib TarInfo in USTAR format with firnpack's zeroed canonical fields."""
    ti = tarfile.TarInfo(name)
    ti.type = typeflag
    ti.size = size
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = mode
    ti.linkname = linkname
    return ti.tobuf(tarfile.USTAR_FORMAT, "utf-8", "surrogateescape")


def _pax_records(data: bytes) -> dict:
    """Parse the ``<len> key=value\n`` record sequence of a pax 'x' member."""
    out = {}
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        length = int(data[pos:space])
        record = data[space + 1 : pos + length - 1]  # strip trailing newline
        key, sep, value = record.partition(b"=")
        assert sep == b"=", record
        out[key.decode("ascii")] = value
        pos += length
    return out


def _raw_layer(repo: Repo, layer_id: str) -> bytes:
    return gzip.decompress(repo._layer_path(layer_id).read_bytes())


# ============================================================== archive safety


def test_repeated_compact_archive_names_never_collide_or_overwrite(
    tmp_path, monkeypatch
):
    """Frozen project clock: two compacts in the same second must yield two
    distinct archives, and the first archive's bytes are preserved exactly."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 6)
    arc = tmp_path / "archives"

    first = repo.compact(1, yes=True, archive_dir=str(arc))
    assert first["status"] == "compacted" and first["archive"]
    first_path = Path(first["archive"])
    assert first_path.is_file() and first_path.parent == arc
    first_bytes = first_path.read_bytes()
    with zipfile.ZipFile(first_path) as zf:
        first_names = sorted(zf.namelist())
    assert first_names and all(n.endswith(".firn") for n in first_names)

    _write(proj, "later.txt", "after the first compact\n")
    repo.commit("c6")
    second = repo.compact(1, yes=True, archive_dir=str(arc))
    assert second["status"] == "compacted" and second["archive"]
    second_path = Path(second["archive"])

    zips = [p for p in arc.iterdir() if p.is_file()]
    assert len(zips) == 2, f"expected 2 distinct archives, got {zips}"
    assert second_path != first_path
    assert second_path.is_file()
    assert first_path.read_bytes() == first_bytes, "first archive was rewritten"
    with zipfile.ZipFile(second_path) as zf:
        # the second archive holds the newly-collapsed range only: no member
        # carried over from the first archive's collapsed set
        assert set(zf.namelist()).isdisjoint(first_names)
        assert zf.namelist()
    assert repo.verify()["status"] == "ok"
    repo.close()


@pytest.mark.parametrize("occupant", ["file", "dir", "link"])
def test_archive_basename_occupant_is_preserved(tmp_path, monkeypatch, occupant):
    """A pre-existing non-archive file, directory or symlink sitting exactly
    at the would-be archive basename must be left untouched; the archive
    lands under a non-colliding name."""
    if occupant == "link" and not POSIX:
        pytest.skip("symlink creation needs POSIX or Windows developer mode")
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    arc = tmp_path / "archives"
    arc.mkdir()

    base = arc / _archive_basename()
    link_target = None
    if occupant == "file":
        base.write_bytes(b"precious pre-existing bytes\n")
    elif occupant == "dir":
        base.mkdir()
        (base / "inner.txt").write_text("keep me\n", encoding="utf-8")
    else:
        link_target = tmp_path / "precious-target.bin"
        link_target.write_bytes(b"precious link target\n")
        base.symlink_to(link_target)
    before = _listing(arc)

    result = repo.compact(1, yes=True, archive_dir=str(arc))
    assert result["status"] == "compacted", result
    produced = Path(result["archive"])
    assert produced.is_file(), result
    assert produced != base, "archive overwrote the pre-existing basename"

    if occupant == "file":
        assert base.read_bytes() == b"precious pre-existing bytes\n"
    elif occupant == "dir":
        assert base.is_dir()
        assert (base / "inner.txt").read_bytes() == b"keep me\n"
    else:
        assert base.is_symlink()
        assert os.readlink(base) == str(link_target)
        assert link_target.read_bytes() == b"precious link target\n"
    after = _listing(arc)
    added = set(after) - set(before)
    assert added == {produced.name}, f"unexpected entries appeared: {added}"
    assert after[produced.name][0] == "file"
    with zipfile.ZipFile(produced) as zf:
        assert zf.namelist() and all(n.endswith(".firn") for n in zf.namelist())
    repo.close()


@pytest.mark.parametrize("mode", ["plan", "execute"])
def test_archive_dir_inside_firn_is_refused(tmp_path, monkeypatch, mode):
    """SPEC §9.9: the archive lives outside .firn/. A directory inside the
    administrative area — or resolving into it — is refused BEFORE any
    mutation, including dry-run validation, and nothing is created."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    firn = proj / ".firn"

    candidates = {
        "firn-itself": firn,
        "descendant-existing": firn / "layers",
        "descendant-missing": firn / "saved-archives",
    }
    for label, archive_dir in candidates.items():
        store_before = _store(repo)
        with pytest.raises(FirnpackError, match=r"(?i)\.firn|administr|archive"):
            if mode == "plan":
                repo.compact(1, dry_run=True, archive_dir=str(archive_dir))
            else:
                repo.compact(1, yes=True, archive_dir=str(archive_dir))
        assert _store(repo) == store_before, f"{label}: .firn/ mutated"
        if label == "descendant-missing":
            assert not archive_dir.exists(), "refusal created the archive dir"
    repo.close()


@pytest.mark.skipif(not POSIX, reason="symlink fixture needs POSIX")
def test_archive_dir_symlink_alias_into_firn_is_refused(tmp_path, monkeypatch):
    """An archive dir reached through a symlink alias into .firn/ is refused
    too — the resolved location is what matters, not the spelling."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    alias = tmp_path / "alias"
    alias.symlink_to(proj / ".firn", target_is_directory=True)

    store_before = _store(repo)
    with pytest.raises(FirnpackError):
        repo.compact(1, yes=True, archive_dir=str(alias / "saved"))
    assert _store(repo) == store_before
    assert not (proj / ".firn" / "saved").exists()
    repo.close()


@pytest.mark.skipif(not POSIX, reason="symlink fixture needs POSIX")
def test_archive_dir_rejected_when_firn_itself_is_a_symlink(tmp_path, monkeypatch):
    """With .firn relocated behind a symlink, the resolved administrative
    directory is still refused as an archive destination."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    repo = Repo.init(proj, author="storage-test")
    repo.close()
    real = tmp_path / "real-admin"
    (proj / ".firn").rename(real)
    (proj / ".firn").symlink_to(real, target_is_directory=True)
    repo = Repo(proj)
    for i in range(3):
        _write(proj, "f.txt", f"content {i}\n")
        packed = repo.commit(f"c{i}")
        assert packed["status"] == "packed", packed

    for archive_dir in (real, real / "archives", proj / ".firn" / "x"):
        store_before = _store(repo)
        with pytest.raises(FirnpackError):
            repo.compact(1, yes=True, archive_dir=str(archive_dir))
        assert _store(repo) == store_before
    assert not (real / "archives").exists()
    repo.close()


def test_preview_noop_and_refusals_create_no_archive_folders(tmp_path, monkeypatch):
    """Nothing that does not actually delete history may materialize the
    archive directory: --dry-run plans, nothing-to-compact, a missing --yes
    and an invalid --keep."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    missing = tmp_path / "no-such-archive-dir"

    plan = repo.compact(1, dry_run=True, archive_dir=str(missing))
    assert plan["status"] == "plan"
    assert not missing.exists()

    idle = repo.compact(99, yes=True, archive_dir=str(missing))
    assert idle["status"] == "nothing_to_compact"
    assert not missing.exists()

    with pytest.raises(FirnpackError):
        repo.compact(1, archive_dir=str(missing))  # no --yes
    assert not missing.exists()

    with pytest.raises(FirnpackError):
        repo.compact(0, yes=True, archive_dir=str(missing))
    assert not missing.exists()

    # a protective refusal: a branch head reaches into the collapsed range
    oldest = _oldest_layer(repo)
    assert oldest != repo._worktree()["head_id"]
    repo.branch("stuck", oldest)
    store_before = _store(repo)
    with pytest.raises(FirnpackError, match="stuck"):
        repo.compact(1, yes=True, archive_dir=str(missing))
    assert not missing.exists()
    assert _store(repo) == store_before
    repo.close()


def test_archive_write_failure_preserves_history_and_old_archive(tmp_path, monkeypatch):
    """An OSError mid-archive-write: history and the previous archive are
    intact, and this invocation's incomplete new archive is cleaned up."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 4)
    arc = tmp_path / "archives"

    first = repo.compact(1, yes=True, archive_dir=str(arc))
    assert first["status"] == "compacted" and first["archive"]
    old_path = Path(first["archive"])
    old_bytes = old_path.read_bytes()
    _write(proj, "more.txt", "post-archive commit\n")
    repo.commit("c4")

    calls = {"n": 0}
    real_write = zipfile.ZipFile.write

    def failing_write(self, filename, arcname=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected archive write failure")
        return real_write(self, filename, arcname, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", failing_write)
    store_before = _store(repo)
    rows_before = _layer_rows(repo)
    arc_before = _listing(arc)
    with pytest.raises((OSError, FirnpackError)):
        repo.compact(1, yes=True, archive_dir=str(arc))

    assert calls["n"] >= 2, "the injected failure never fired"
    assert _store(repo) == store_before, "history was mutated"
    assert _layer_rows(repo) == rows_before
    assert _listing(arc) == arc_before, "incomplete archive left behind"
    assert old_path.read_bytes() == old_bytes, "old archive was overwritten"
    assert repo.verify()["status"] == "ok"
    repo.close()


def test_archive_close_failure_preserves_history_and_cleans_up(tmp_path, monkeypatch):
    """An OSError from zf.close() mid-archive: same guarantees as a write
    failure — old history and archives intact, owned output removed."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 4)
    arc = tmp_path / "archives"
    first = repo.compact(1, yes=True, archive_dir=str(arc))
    assert first["status"] == "compacted" and first["archive"]
    old_path = Path(first["archive"])
    old_bytes = old_path.read_bytes()
    _write(proj, "more.txt", "post-archive commit\n")
    repo.commit("c4")

    closed = {"n": 0}
    real_close = zipfile.ZipFile.close

    def failing_close(self):
        closed["n"] += 1
        if closed["n"] == 1:
            try:
                raise OSError("injected archive close failure")
            finally:
                real_close(self)  # never leak the descriptor
        return real_close(self)  # ZipFile.__del__ calls close() again

    monkeypatch.setattr(zipfile.ZipFile, "close", failing_close)
    store_before = _store(repo)
    rows_before = _layer_rows(repo)
    arc_before = _listing(arc)
    with pytest.raises((OSError, FirnpackError)):
        repo.compact(1, yes=True, archive_dir=str(arc))

    assert closed["n"] >= 1
    assert _store(repo) == store_before
    assert _layer_rows(repo) == rows_before
    assert _listing(arc) == arc_before
    assert old_path.read_bytes() == old_bytes
    repo.close()


@pytest.mark.skipif(not POSIX, reason="fd/inode fsync oracle needs POSIX")
def test_archive_fsync_failure_preserves_history_and_old_archive(tmp_path, monkeypatch):
    """The archive file and its parent directory must be fsynced before
    history is deleted. An injected fsync failure on either proves both
    that the call exists and that failure leaves history untouched."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 4)
    arc = tmp_path / "archives"
    first = repo.compact(1, yes=True, archive_dir=str(arc))
    assert first["status"] == "compacted" and first["archive"]
    old_path = Path(first["archive"])
    old_bytes = old_path.read_bytes()
    _write(proj, "more.txt", "post-archive commit\n")
    repo.commit("c4")

    arc_res = arc.resolve()
    real_fsync = os.fsync
    hits = []

    def fsync_guard(fd):
        st = os.fstat(fd)
        victims = {arc_res.stat().st_ino}
        victims.update(e.stat(follow_symlinks=False).st_ino for e in arc_res.iterdir())
        if st.st_ino in victims:
            hits.append(fd)
            raise OSError("injected archive fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_guard)
    store_before = _store(repo)
    rows_before = _layer_rows(repo)
    arc_before = _listing(arc)
    with pytest.raises((OSError, FirnpackError)):
        repo.compact(1, yes=True, archive_dir=str(arc))

    assert hits, "no fsync ever touched the archive file or its directory"
    assert _store(repo) == store_before
    assert _layer_rows(repo) == rows_before
    assert _listing(arc) == arc_before
    assert old_path.read_bytes() == old_bytes
    repo.close()


@pytest.mark.parametrize("placement", ["same-tree", "separate-volume"])
def test_archive_volume_free_space_is_budgeted_before_writing(
    tmp_path, monkeypatch, placement
):
    """The peak-disk check must cover archive bytes on the ARCHIVE volume:
    a roomy repository next to a starved archive destination must refuse
    before a single archive byte is written."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    if placement == "same-tree":
        arc = tmp_path / "archives"
        starved = tmp_path.resolve()
    else:
        starved = tmp_path / "other-volume"
        starved.mkdir()  # the probe must hit this dir, not walk past it
        starved = starved.resolve()
        arc = starved / "archives"

    zips_before = _zips(tmp_path)
    real_usage = shutil.disk_usage

    def fake_usage(path):
        try:
            p = Path(path).resolve()
            if p == starved or starved in p.parents:
                return shutil._ntuple_diskusage(10**12, 10**12 - 1, 1)
        except OSError:
            pass
        return real_usage(path)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    store_before = _store(repo)
    with pytest.raises(FirnpackError):
        repo.compact(1, yes=True, archive_dir=str(arc))

    assert _store(repo) == store_before
    assert _zips(tmp_path) == zips_before, "a partial archive was written anyway"
    assert not arc.exists() or _listing(arc) == {}, "archive dir was created"
    repo.close()


def test_same_volume_disk_budget_refuses_before_archive_write(tmp_path, monkeypatch):
    """Stated before write: with everything on one volume and free space
    below the peak, compact refuses and materializes nothing."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    arc = tmp_path / "archives"

    def fake_usage(_path):
        return shutil._ntuple_diskusage(10**12, 10**12 - 1, 1)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    store_before = _store(repo)
    with pytest.raises(FirnpackError, match=r"(?i)space|disk|free|short"):
        repo.compact(1, yes=True, archive_dir=str(arc))
    assert _store(repo) == store_before
    assert not arc.exists()
    repo.close()


# ======================================================= canonical long paths


@pytest.mark.skipif(os.name == "nt", reason="MAX_PATH cap is a Windows limit")
def test_unsplittable_197_char_path_full_roundtrip(tmp_path):
    """A 197-char unsplittable name commits, amends, exports, survives
    compact and rebuilds from layers — the deterministic pax fallback."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 0)

    _write(proj, UNSPLITTABLE_197, "payload\n")
    packed = repo.commit("long path")
    assert packed["status"] == "packed"

    out = tmp_path / "exported"
    result = repo.export(packed["layer_id"], str(out))
    assert result["status"] == "exported"
    assert (out / UNSPLITTABLE_197).read_bytes() == b"payload\n"

    _write(proj, "other.txt", "more\n")
    amended = repo.amend("amend with long path")
    assert amended["status"] == "amended"

    _write(proj, "other.txt", "even more\n")
    repo.commit("c2")
    _write(proj, "other.txt", "newest\n")
    repo.commit("c3")
    compacted = repo.compact(1, yes=True)
    assert compacted["status"] == "compacted"
    assert repo.verify()["status"] == "ok"

    repo.close()
    (proj / ".firn" / "glacier.db").unlink()
    repo = Repo(proj, recovering=True)
    rebuilt = repo.rebuild_db()
    assert rebuilt["status"] == "rebuilt"
    assert repo.verify()["status"] == "ok"
    out2 = tmp_path / "exported-after-rebuild"
    repo.export("main", str(out2))
    assert (out2 / UNSPLITTABLE_197).read_bytes() == b"payload\n"
    repo.close()


@pytest.mark.skipif(os.name == "nt", reason="MAX_PATH cap is a Windows limit")
def test_short_unicode_and_splittable_names_stay_byte_identical_ustar(
    tmp_path,
):
    """Short ASCII, Unicode and ustar-splittable long member names keep
    byte-identical USTAR headers vs a stdlib TarInfo oracle — no pax
    'x' member may appear anywhere in the layer."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 0)
    _write(proj, "plain.txt", "plain\n")
    _write(proj, UNICODE_NAME, "unicode name\n")
    _write(proj, SPLITTABLE_LONG, "splittable long path\n")
    if POSIX:
        (proj / "short-link").symlink_to("plain.txt")
    packed = repo.commit("names")
    assert packed["status"] == "packed"
    repo.close()

    raw = _raw_layer(repo, packed["layer_id"])
    records = _tar_records(raw)
    names = [r["name"] for r in records]
    assert b"x" not in [r["typeflag"] for r in records], (
        "pax extended header written for ustar-representable names"
    )
    expected = {
        firnpack.META_MEMBER.encode("utf-8"),
        b".gitignore",  # Repo.init seeds one; it is tracked like any file
        b"plain.txt",
        UNICODE_NAME.encode("utf-8"),
        SPLITTABLE_LONG.encode("utf-8"),
    }
    if POSIX:
        expected.add(b"short-link")
    assert set(names) == expected
    for rec in records:
        rec_name = rec["name"].decode("utf-8")
        if rec_name == firnpack.META_MEMBER:
            oracle = _ustar_oracle(
                firnpack.META_MEMBER, size=rec["size"], mode=rec["mode"]
            )
        elif rec["typeflag"] == tarfile.SYMTYPE:
            oracle = _ustar_oracle(
                rec_name,
                typeflag=tarfile.SYMTYPE,
                mode=rec["mode"],
                linkname=rec["linkname"].decode("utf-8"),
            )
        else:
            oracle = _ustar_oracle(rec_name, size=rec["size"], mode=rec["mode"])
        assert rec["block"] == oracle, (
            f"member {rec_name!r}: header drifted from canonical USTAR bytes"
        )


@pytest.mark.skipif(os.name == "nt", reason="MAX_PATH cap is a Windows limit")
def test_unsplittable_name_gets_deterministic_path_only_pax(tmp_path, monkeypatch):
    """The pax fallback is USTAR-first: only a genuinely unsplittable name
    gets an 'x' member, it carries a 'path' record ONLY, and two identical
    repos produce byte-identical layer files."""
    _freeze_clock(monkeypatch)
    layer_bytes = []
    for n in range(2):
        proj = tmp_path / f"proj{n}"
        repo = _make_repo(proj, 0)
        _write(proj, UNSPLITTABLE_PREFIX, "deep\n")
        _write(proj, "plain.txt", "plain\n")
        packed = repo.commit("pax")
        assert packed["status"] == "packed"
        layer_bytes.append(repo._layer_path(packed["layer_id"]).read_bytes())
        repo.close()

    assert layer_bytes[0] == layer_bytes[1], "pax fallback is not deterministic"

    records = _tar_records(gzip.decompress(layer_bytes[0]))
    pax_members = [r for r in records if r["typeflag"] == b"x"]
    assert len(pax_members) == 1, f"expected exactly one pax member: {pax_members}"
    pax = pax_members[0]
    fields = _pax_records(pax["data"])
    assert set(fields) == {"path"}, f"unexpected pax keys: {set(fields)}"
    assert fields["path"].decode("utf-8") == UNSPLITTABLE_PREFIX
    assert pax["mtime"] == 0 and pax["uid"] == 0 and pax["gid"] == 0
    # the 'x' member immediately precedes the member it extends
    after = records[records.index(pax) + 1]
    assert after["typeflag"] in (tarfile.REGTYPE, tarfile.SYMTYPE)


# ====================================== adjacent contract regressions (§5/§13)


@pytest.mark.skipif(os.name == "nt", reason="MAX_PATH cap is a Windows limit")
def test_amend_enforces_commit_path_limits(tmp_path, capsys):
    """amend rewrites a layer through the same writers as commit, so the
    §13 limits must apply identically: warn past 240, refuse past 255."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 1)

    warn_rel = "w" * 120 + "/" + "y" * 90  # splittable, resolved path > 240
    _write(proj, warn_rel, "warn\n")
    result = repo.amend("warn-length amend")
    assert result["status"] == "amended"
    err = capsys.readouterr().err
    assert "warning" in err.lower() and "240" in err, (
        "amend packed a >240-char resolved path without the commit warning"
    )
    assert result.get("maxpath_warning"), result

    refuse_rel = "r" * 200 + "/" + "z" * 60  # 261 chars > 255 hard limit
    _write(proj, refuse_rel, "refuse\n")
    head_before = repo._worktree()["head_id"]
    rows_before = _layer_rows(repo)
    with pytest.raises(FirnpackError, match=r"(?i)too long"):
        repo.amend("over-limit amend")
    assert repo._worktree()["head_id"] == head_before
    assert _layer_rows(repo) == rows_before

    # parity evidence: commit refuses the very same worktree the same way
    with pytest.raises(FirnpackError, match=r"(?i)too long"):
        repo.commit("over-limit commit")
    proc = _firn(proj, "amend", "-m", "cli over-limit")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "too long" in proc.stderr
    repo.close()


@pytest.mark.skipif(not POSIX, reason="non-UTF-8 names need POSIX bytes APIs")
def test_commit_refuses_non_utf8_filename_humanely(tmp_path):
    """A filename that is not valid UTF-8 must be a humane FirnpackError
    refusal at commit — never a raw UnicodeEncodeError traceback — and
    nothing may be written."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 1)
    try:
        fd = os.open(
            os.path.join(os.fsencode(proj), b"bad-\xff-name.txt"),
            os.O_CREAT | os.O_WRONLY,
            0o644,
        )
    except OSError as exc:
        if exc.errno == errno.EILSEQ:
            pytest.skip("this filesystem enforces UTF-8 filenames (e.g. APFS)")
        raise
    os.write(fd, b"bad bytes\n")
    os.close(fd)

    layers_before = _layer_rows(repo)
    with pytest.raises(FirnpackError):
        repo.commit("bad name")
    assert _layer_rows(repo) == layers_before
    repo.close()

    proc = _firn(proj, "commit", "-m", "bad name")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert proc.stderr.strip(), "refusal printed nothing"


@pytest.mark.skipif(not POSIX, reason="symlink fixture needs POSIX")
def test_commit_refuses_non_utf8_symlink_target_humanely(tmp_path):
    """A symlink whose TARGET is not valid UTF-8 gets the same humane
    refusal — before hashing, so no traceback and no layer."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 1)
    os.symlink(b"target-\xff.bin", os.fsencode(proj) + b"/bad-link")

    layers_before = _layer_rows(repo)
    with pytest.raises(FirnpackError):
        repo.commit("bad target")
    assert _layer_rows(repo) == layers_before
    repo.close()

    proc = _firn(proj, "commit", "-m", "bad target")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr


@pytest.mark.skipif(not POSIX, reason="symlink fixture needs POSIX")
def test_commit_refuses_ustar_unrepresentable_symlink_target(tmp_path):
    """A symlink target over 100 chars cannot be a ustar linkname, and v1
    adds no long-link policy: the refusal is a FirnpackError, never the
    tarfile ValueError traceback that today escapes main()."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 1)
    (proj / "long-target-link").symlink_to("t" * 120 + ".txt")

    layers_before = _layer_rows(repo)
    with pytest.raises(FirnpackError):
        repo.commit("long target")
    assert _layer_rows(repo) == layers_before
    repo.close()

    proc = _firn(proj, "commit", "-m", "long target")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr


# ------------------------------------------------------------- rename-branch


@pytest.mark.parametrize(
    "old,new,expect",
    [
        ("ghost", "fresh", "no branch named"),  # OLD missing: refuse naming OLD
        ("ghost", "main", "no branch named"),  # OLD check precedes NEW collision
        ("main", "", "invalid"),  # NEW must satisfy branch-name constraints
        ("main", "a/b", "invalid"),
        ("main", ".hidden", "invalid"),
        ("main", "main", "already exists"),  # NEW collision, same-name included
        ("main", "taken", "already exists"),
    ],
)
def test_rename_branch_contract(tmp_path, old, new, expect):
    """rename-branch OLD NEW: OLD existence is checked first ('no branch
    named'), then the SAME name constraints as `branch` apply to NEW,
    then destination collision. Every refusal is zero-mutation."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _firn(proj, "init").returncode == 0
    _write(proj, "f.txt", "seed\n")
    assert _firn(proj, "commit", "-m", "seed").returncode == 0
    if new == "taken":
        assert _firn(proj, "branch", "taken").returncode == 0
    db = proj / ".firn" / "glacier.db"
    branches_before, head_before = _branches_and_head(db)

    proc = _firn(proj, "rename-branch", old, new)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert expect in proc.stderr, proc.stderr
    if expect == "no branch named":
        assert old in proc.stderr, proc.stderr

    branches_after, head_after = _branches_and_head(db)
    assert branches_after == branches_before, "a refused rename mutated rows"
    assert head_after == head_before


# ================================ layer-write durability (SPEC §13)


@pytest.mark.skipif(not POSIX, reason="fd-level pread/fsync oracle needs POSIX")
@pytest.mark.parametrize("op", ["commit", "amend", "compact"])
def test_gzip_and_tar_trailers_reach_disk_before_fsync(tmp_path, monkeypatch, op):
    """The durable bytes under fsync must be the COMPLETE stream: tar end
    blocks and the gzip trailer flushed to the fd before fsync, not after —
    a crash between fsync and close would otherwise persist a truncated
    member forever."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)

    opened = {}  # fd -> path, from builtins.open (fd is write-only)
    real_open = open

    def tracking_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        opened[fh.fileno()] = str(file)
        return fh

    snapshots = []  # path + bytes as durably flushed at fsync time
    real_fsync = os.fsync

    def capture_fsync(fd):
        path = opened.get(fd)
        if path and path.endswith(".firn.tmp"):
            snapshots.append((path, Path(path).read_bytes()))
        return real_fsync(fd)

    monkeypatch.setattr("builtins.open", tracking_open)

    if op in ("commit", "amend"):
        _write(proj, "f.txt", "changed\n")  # compact works on layers only

    monkeypatch.setattr(os, "fsync", capture_fsync)
    if op == "commit":
        assert repo.commit("durable")["status"] == "packed"
    elif op == "amend":
        assert repo.amend("durable")["status"] == "amended"
    else:
        assert repo.compact(1, yes=True)["status"] == "compacted"
    repo.close()

    assert len(snapshots) == (2 if op == "compact" else 1), snapshots
    for path, payload in snapshots:
        assert payload[:2] == b"\x1f\x8b", f"{path}: fsynced non-gzip bytes"
        tar_stream = gzip.decompress(payload)  # CRC/ISIZE trailer required
        records = _tar_records(tar_stream)
        assert records and records[0]["name"] == firnpack.META_MEMBER.encode()
        assert tar_stream.endswith(b"\x00" * 2 * BLOCK), (
            "tar end-of-archive blocks missing at fsync time"
        )


@pytest.mark.parametrize("op", ["commit", "amend", "compact"])
def test_owned_tmp_removed_after_write_failure(tmp_path, monkeypatch, op):
    """A write fault inside a layer writer must not leak the owned
    ``<id>.firn.tmp`` — and neither the head pointer nor history may move."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    _write(proj, "f.txt", "changed\n")
    head_before = repo._worktree()["head_id"]
    rows_before = _layer_rows(repo)

    fired = {"n": 0}
    real_addfile = tarfile.TarFile.addfile

    def failing_addfile(self, tarinfo, fileobj=None):
        fired["n"] += 1
        # member 1 is __firnpack_meta__; the second member is mid-stream —
        # the .tmp already holds a partial tar
        if fired["n"] == 2:
            raise OSError("injected layer write failure")
        return real_addfile(self, tarinfo, fileobj)

    monkeypatch.setattr(tarfile.TarFile, "addfile", failing_addfile)
    with pytest.raises((OSError, FirnpackError)):
        if op == "commit":
            repo.commit("boom")
        elif op == "amend":
            repo.amend("boom")
        else:
            repo.compact(1, yes=True)

    assert fired["n"] >= 1, "the injected write failure never fired"
    leftovers = sorted(p.name for p in repo.layers_dir.glob("*.tmp"))
    assert leftovers == [], f"owned .tmp leaked: {leftovers}"
    assert repo._worktree()["head_id"] == head_before
    assert _layer_rows(repo) == rows_before
    assert repo.verify()["status"] == "ok"
    repo.close()


@pytest.mark.skipif(not POSIX, reason="fd/inode fsync oracle needs POSIX")
@pytest.mark.parametrize("op", ["commit", "amend", "compact"])
def test_owned_tmp_removed_after_fsync_failure(tmp_path, monkeypatch, op):
    """An fsync fault on the layer file: same cleanup guarantee — owned
    ``.tmp`` removed, head and history untouched."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    _write(proj, "f.txt", "changed\n")
    head_before = repo._worktree()["head_id"]
    rows_before = _layer_rows(repo)

    fired = {"n": 0}
    real_fsync = os.fsync

    def failing_fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            fired["n"] += 1
            raise OSError("injected layer fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises((OSError, FirnpackError)):
        if op == "commit":
            repo.commit("boom")
        elif op == "amend":
            repo.amend("boom")
        else:
            repo.compact(1, yes=True)

    assert fired["n"] >= 1, "no fsync on a layer file ever ran"
    leftovers = sorted(p.name for p in repo.layers_dir.glob("*.tmp"))
    assert leftovers == [], f"owned .tmp leaked: {leftovers}"
    assert repo._worktree()["head_id"] == head_before
    assert _layer_rows(repo) == rows_before
    assert repo.verify()["status"] == "ok"
    repo.close()


@pytest.mark.parametrize("op", ["commit", "amend"])
def test_owned_tmp_removed_after_unrepresentable_member(tmp_path, monkeypatch, op):
    """A member name the archive cannot represent (POSIX '\\' byte) is a
    humane refusal that must not leave a partial owned .tmp behind."""
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 1)
    _write(proj, "weird\\name.txt", "backslash\n")  # POSIX: a name, not a sep

    head_before = repo._worktree()["head_id"]
    rows_before = _layer_rows(repo)
    with pytest.raises(FirnpackError):
        if op == "commit":
            repo.commit("unrepresentable")
        else:
            repo.amend("unrepresentable")

    leftovers = sorted(p.name for p in repo.layers_dir.glob("*.tmp"))
    assert leftovers == [], f"owned .tmp leaked: {leftovers}"
    assert repo._worktree()["head_id"] == head_before
    assert _layer_rows(repo) == rows_before
    repo.close()


def test_archive_bytes_are_counted_in_the_same_device_budget(tmp_path, monkeypatch):
    """Same-device accounting: the free-space check must add the archive
    payload to the peak, so free ≥ rewrite-peak but < peak+archive refuses
    before anything is written."""
    _freeze_clock(monkeypatch)
    proj = tmp_path / "proj"
    repo = _make_repo(proj, 3)
    arc = tmp_path / "archives"  # same device as the repo
    plan = repo.compact(1, dry_run=True)
    peak = plan["peak_disk_needed"]
    reclaimed = plan["bytes_reclaimed"]
    assert reclaimed > 0, "empty archive budget test is meaningless"

    fake_free = peak + 1  # roomy for the rewrite alone, short of the archive
    real_usage = shutil.disk_usage

    def fake_usage(path):
        try:
            p = Path(path).resolve()
            if p == tmp_path.resolve() or tmp_path.resolve() in p.parents:
                return shutil._ntuple_diskusage(10**12, 10**12 - 1, fake_free)
        except OSError:
            pass
        return real_usage(path)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    store_before = _store(repo)
    with pytest.raises(FirnpackError, match=r"(?i)space|disk|free|short"):
        repo.compact(1, yes=True, archive_dir=str(arc))
    assert _store(repo) == store_before
    assert not arc.exists()
    repo.close()


def test_completed_archive_is_reported_when_later_rewrite_fails(
    tmp_path, monkeypatch, capsys
):
    repo = _make_repo(tmp_path / "proj", 3)
    arc = tmp_path / "archives"
    before = _store(repo)
    layers = {
        path.name: path.read_bytes()
        for path in (repo.root / ".firn" / "layers").glob("*.firn")
    }
    original = OSError("injected rewrite failure after archive")

    def fail_rewrite(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(repo, "_rewrite_layer_from", fail_rewrite)
    with pytest.raises(OSError) as caught:
        repo.compact(1, yes=True, archive_dir=str(arc))
    assert caught.value is original
    assert _store(repo) == before
    archives = list(arc.glob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        names = archive.namelist()
        assert len(names) == 2
        assert set(names) <= set(layers)
        for name in names:
            assert archive.read(name) == layers[name]
    assert str(archives[0]) in capsys.readouterr().err
    repo.close()
