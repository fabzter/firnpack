"""CLI read-side probes: damaged or hostile stores must fail loudly.

Every assertion runs the CLI in a subprocess (``sys.executable`` +
the ``firnpack.py`` next to this file's repo root) so the check covers
the whole process contract, not just the library call:

- exit 3 for integrity failures — docs/usage.md:15 ("integrity
  failure (verify, or any command that hits a corrupt glacier)") and
  docs/agent-contract.md:13 ("3 integrity failure"); stderr carries the
  ``firnpack: integrity failure:`` prefix (agent-contract.md:18,
  "firnpack: prefixes error text on stderr");
- exit 1 for refusals — docs/usage.md:13 and
  docs/agent-contract.md:12–14;
- never a ``Traceback`` on stderr.

Tar fixtures are built with :mod:`tarfile` in this file; hostile layers
are planted under a fake 40-hex id with a matching ``layers`` row so
``export`` can resolve the ref (an id with no row fails name resolution
long before the layer is ever opened).
"""

import gzip
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firnpack import Repo  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "firnpack.py"
META_MEMBER = "__firnpack_meta__"


def _firn(cwd, *args):
    p = subprocess.run(
        [sys.executable, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return p.returncode, p.stdout, p.stderr


def _expect_integrity(rc, out, err, cmd):
    assert rc == 3, (cmd, rc, out, err)
    assert "firnpack: integrity failure:" in err, (cmd, err)
    assert "Traceback" not in err, (cmd, err)


def _make_repo(root, monkeypatch, n=4):
    """A straight n-layer repo through the Repo API (fast, no CLI)."""
    import firnpack

    counter = {"n": 0}

    def _now():
        counter["n"] += 1
        return f"2026-04-01T00:{counter['n'] % 60:02d}:00+00:00"

    monkeypatch.setattr(firnpack, "_utcnow", _now)
    root.mkdir()
    repo = Repo.init(root, author="probe")
    ids = []
    for i in range(n):
        (root / f"n{i}.txt").write_text(f"n{i}\n", encoding="utf-8")
        (root / "main.txt").write_text(f"c{i}\n", encoding="utf-8")
        ids.append(repo.commit(f"c{i}")["layer_id"])
    repo.close()
    return ids


def _layers_dir(root):
    return root / ".firn" / "layers"


def _plant_layer(root, members):
    """Write a crafted tar.gz layer under a fake 40-hex id plus the
    layers-table row that makes the id resolvable."""
    fake = "deadbeef" * 5
    buf = io.BytesIO()
    meta = {
        "format_version": 1,
        "tree_id": "0" * 40,
        "parent": None,
        "second_parent": None,
        "message": "crafted",
        "author": "probe",
        "created_at": "2026-04-01T00:00:00+00:00",
        "layer_id": fake,
        "profile": "gzip",
    }
    meta_bytes = json.dumps(meta).encode("utf-8")
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar,
    ):
        ti = tarfile.TarInfo(META_MEMBER)
        ti.size = len(meta_bytes)
        ti.mtime = 0
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(meta_bytes))
        for m, data in members:
            tar.addfile(m, io.BytesIO(data) if data is not None else None)
    (_layers_dir(root) / f"{fake}.firn").write_bytes(buf.getvalue())
    db = sqlite3.connect(root / ".firn" / "glacier.db")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO layers (id, tree_id, parent, second_parent, "
            "message, author, created_at, profile, file_count, size) "
            "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)",
            (
                fake,
                "0" * 40,
                "crafted",
                "probe",
                "2026-04-01T00:00:00+00:00",
                "gzip",
                1,
                buf.getbuffer().nbytes,
            ),
        )
        db.commit()
    finally:
        db.close()
    return fake


def _regular_member(name, data):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = 0o644
    return ti


def _file_map(dest):
    return {
        str(p.relative_to(dest)): p.read_bytes()
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }


@pytest.fixture()
def repo4(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    ids = _make_repo(root, monkeypatch, 4)
    return root, ids


# ------------------------------------------------------------------ (a)
# content tamper inside a still-valid gzip stream -> verify must exit 3.


def test_tamper_compressed_body(repo4):
    """Flip one byte inside a layer's compressed body: a member's stored
    byte is altered and the layer is repacked as a valid gzip with a
    valid trailer, so decoding succeeds and only the verify content
    re-hash can catch the tamper (docs/usage.md:289-291, exit table
    :15)."""
    root, ids = repo4
    path = _layers_dir(root) / f"{ids[1]}.firn"
    items = []
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            fh = tar.extractfile(m) if m.isreg() else None
            data = fh.read() if fh is not None else None
            if m.name == "main.txt":
                assert data is not None
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            items.append((m, data))
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar,
    ):
        for m, data in items:
            tar.addfile(m, io.BytesIO(data) if data is not None else None)
    rc, out, err = _firn(root, "verify")
    _expect_integrity(rc, out, err, "verify")


def test_tamper_gzip_trailer(repo4):
    """Flip one byte inside the 8-byte gzip trailer (CRC32/ISIZE). tar
    readers stop at the tar end-marker and never consume the trailer, so
    only an explicit drain catches this — exit 3, no traceback."""
    root, ids = repo4
    path = _layers_dir(root) / f"{ids[1]}.firn"
    raw = bytearray(path.read_bytes())
    raw[-5] ^= 0xFF  # inside the trailer: CRC32 occupies raw[-8:-4]
    path.write_bytes(bytes(raw))
    rc, out, err = _firn(root, "verify")
    _expect_integrity(rc, out, err, "verify")


# ------------------------------------------------------------------ (b)
# truncated layer -> every reader that opens it exits 3.


def test_truncated_layer_verify_and_status(repo4):
    """Cut the last 20 bytes off the HEAD layer: verify re-reads every
    layer and status re-reads the head manifest, so both must fail with
    an integrity error — never a bare EOFError traceback."""
    root, ids = repo4
    path = _layers_dir(root) / f"{ids[-1]}.firn"
    path.write_bytes(path.read_bytes()[:-20])
    for cmd in (["verify"], ["status"]):
        rc, out, err = _firn(root, *cmd)
        _expect_integrity(rc, out, err, cmd)


# ------------------------------------------------------------------ (c)
# unreadable glacier.db -> exit 3 and the message names the recovery.


def test_garbage_database_status(repo4):
    """Random bytes for glacier.db is store damage (exit 3), and the
    humane message names `verify --rebuild-db` — the recovery path, not
    `init` (docs/storage.md §6)."""
    root, _ids = repo4
    (root / ".firn" / "glacier.db").write_bytes(b"\x00\xff" * 2048)
    rc, out, err = _firn(root, "status")
    _expect_integrity(rc, out, err, "status")
    assert "verify --rebuild-db" in err, err


# ------------------------------------------------------------------ (d)
# hostile member names: backslash and '..' traversal.


@pytest.mark.parametrize(
    "member_name",
    ["dir\\evil.txt", "../evil.txt"],
    ids=["backslash", "traversal"],
)
def test_hostile_member_name(repo4, tmp_path, member_name):
    """A layer whose member name is Windows-unsafe (backslash) or a
    traversal (`..`) is rejected by the full pre-pass — verify and
    export exit 3 and nothing is written anywhere (SPEC §5 layer 3,
    usage.md exit table)."""
    root, _ids = repo4
    dest = tmp_path / "dest"
    fake = _plant_layer(root, [(_regular_member(member_name, b"evil\n"), b"evil\n")])
    for cmd in (["verify"], ["export", fake, "--to", str(dest)]):
        rc, out, err = _firn(root, *cmd)
        _expect_integrity(rc, out, err, cmd)
    # the pre-pass must have fired before any write: the destination is
    # empty and nothing escaped next to it
    assert dest.exists()
    assert list(dest.rglob("*")) == []
    assert not (tmp_path / "evil.txt").exists()


# ------------------------------------------------------------------ (e)
# symlink escape: a member beneath a symlink member must never write
# through it.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
@pytest.mark.parametrize(
    "target_kind", ["relative", "absolute"], ids=["relpath", "abspath"]
)
def test_symlink_escape(repo4, tmp_path, target_kind):
    """Layer member `d` is a symlink to an escape target and `d/pwn.txt`
    a regular file. Extraction would write through the link — the
    'beneath a non-directory member' check must refuse first. Docs pin
    exit 3 for integrity failures (usage.md:15); the symlink limitation
    note in the restore section also allows exit 1 — both are accepted
    here, and nothing may be written outside the destination either way.
    """
    root, _ids = repo4
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    outside.mkdir()
    if target_kind == "relative":
        target = "../outside"
    else:
        # a guaranteed-absent absolute path, short enough for the
        # 100-char ustar linkname cap
        target = str(
            Path(tempfile.gettempdir()) / f"firnpack-probe-absent-{os.getpid()}"
        )
    link = tarfile.TarInfo("d")
    link.type = tarfile.SYMTYPE
    link.linkname = target
    link.mode = 0o777
    member = (_regular_member("d/pwn.txt", b"pwn\n"), b"pwn\n")
    fake = _plant_layer(root, [(link, None), member])

    rc, out, err = _firn(root, "export", fake, "--to", str(dest))
    assert rc in (1, 3), (rc, out, err)
    if rc == 3:
        assert "firnpack: integrity failure:" in err, err
    else:
        assert err.startswith("firnpack: "), err
    assert "Traceback" not in err, err
    assert not (outside / "pwn.txt").exists()
    if target_kind == "absolute":
        assert not Path(target, "pwn.txt").exists()


# ------------------------------------------------------------------ (f)
# the compact foundation inherits the cut layer's bytes.


def test_foundation_bytes_equal_cut_layer(tmp_path, monkeypatch):
    """After `compact --keep 1 --yes` on a 4-layer repo the foundation
    layer must hold the tree of the layer at the cut — chain[1], the
    second-newest layer — not the worktree tree (SPEC 9.9). The cut
    layer's bytes are captured by exporting it BEFORE compacting."""
    root = tmp_path / "proj"
    ids = _make_repo(root, monkeypatch, 4)
    cut_id = ids[2]  # newest-first chain; cut = chain[keep] = chain[1]
    cut_dest = tmp_path / "cut"
    rc, out, err = _firn(root, "export", cut_id, "--to", str(cut_dest))
    assert rc == 0, (out, err)
    cut_map = _file_map(cut_dest)

    rc, out, err = _firn(root, "compact", "--keep", "1", "--yes")
    assert rc == 0, (out, err)
    rc, out, err = _firn(root, "history", "--json")
    assert rc == 0, (out, err)
    roots = [c["id"] for c in json.loads(out)["commits"] if c["parent"] is None]
    assert len(roots) == 1, out
    foundation_dest = tmp_path / "foundation"
    rc, out, err = _firn(root, "export", roots[0], "--to", str(foundation_dest))
    assert rc == 0, (out, err)
    assert _file_map(foundation_dest) == cut_map


# ------------------------------------------------------------------ (g)
# a kept merge whose second parent reaches below the cut must refuse
# naming --keep.


def test_compact_merge_chain_refusal_names_keep(tmp_path, monkeypatch):
    """main: c1 c2, side: s1 off c2, main: c3 then merge side — the head
    is a kept merge whose second parent's ancestry reaches into the
    collapsed range at --keep 1. Both the dry-run plan and the executed
    command refuse (exit 1, usage.md:13; refusal list usage.md:336-341)
    and the message names `--keep` — the documented remedy is raising
    the cutoff."""
    import firnpack

    counter = {"n": 0}

    def _now():
        counter["n"] += 1
        return f"2026-04-01T00:{counter['n'] % 60:02d}:00+00:00"

    monkeypatch.setattr(firnpack, "_utcnow", _now)
    root = tmp_path / "proj"
    root.mkdir()
    repo = Repo.init(root, author="probe")
    (root / "main.txt").write_text("c1\n", encoding="utf-8")
    repo.commit("c1")
    (root / "main.txt").write_text("c2\n", encoding="utf-8")
    repo.commit("c2")
    repo.branch("side")
    repo.switch("side")
    (root / "side.txt").write_text("s1\n", encoding="utf-8")
    repo.commit("s1")
    repo.switch("main")
    (root / "main.txt").write_text("c3\n", encoding="utf-8")
    repo.commit("c3")
    merged = repo.merge("side", message="merge side")
    assert merged["status"] == "merged"
    repo.close()

    for args in (["--dry-run"], ["--yes"]):
        rc, out, err = _firn(root, "compact", "--keep", "1", *args)
        assert rc == 1, (args, rc, out, err)
        assert "--keep" in err, (args, err)
        assert "Traceback" not in err, (args, err)
