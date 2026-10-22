"""Integrity firewall tests (adversarial review, 2026-10-21).

The founding threat model is a failing HDD plus a non-developer user, so
`verify` must catch *content* damage (not just metadata), every kind of
store damage must surface as exit 3 with one humane line (never a Python
traceback), a worktree file must never be committable into a layer that
the tool itself then refuses to read, and a hostile or damaged layer must
never write outside the target directory.

Every layer forgery below is a VALID gzip/tar whose only defect is the one
named — that is what makes these tests discriminating (a blanket "the file
is garbage" would be caught by the old code too).
"""

import gzip
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firnpack import FirnpackError, IntegrityError, Repo  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "firnpack.py"


def write(root: Path, rel: str, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "proj"


def _make(project, n=3):
    project.mkdir()
    repo = Repo.init(project)
    ids = []
    for i in range(n):
        write(project, f"f{i}.txt", f"content {i}\n")
        ids.append(repo.commit(f"c{i}")["layer_id"])
    return repo, ids


def _layer(project, layer_id) -> Path:
    return project / ".firn" / "layers" / f"{layer_id}.firn"


def _repack(path: Path, mutate) -> None:
    """Re-pack a layer as a valid gzip+tar after mutate(items), keeping
    its filename. items = [(TarInfo, bytes | None)]."""
    with tarfile.open(path, "r:gz") as tar:
        items = [
            (m, tar.extractfile(m).read() if m.isreg() else None)
            for m in tar.getmembers()
        ]
    items = mutate(items)
    with (
        gzip.GzipFile(path, "wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as out,
    ):
        for m, data in items:
            if data is not None:
                m.size = len(data)
            out.addfile(m, io.BytesIO(data) if data is not None else None)


def _firn(cwd, *args):
    p = subprocess.run(
        [sys.executable, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # the interpreter-level tarfile advisory is environment noise
    err = "\n".join(
        line for line in p.stderr.splitlines() if "tarfile security" not in line
    )
    return p.returncode, p.stdout, err


class TestVerifyHashesContent:
    def test_content_tamper_in_a_valid_layer_is_detected(self, project):
        """The old verify re-hashed only the meta record: a layer whose
        file bytes were altered (still valid gzip/tar) passed 'ok' and
        export served the altered bytes."""
        repo, ids = _make(project)
        repo.close()

        def mutate(items):
            return [(m, b"TAMPERED\n" if m.name == "f0.txt" else d) for m, d in items]

        _repack(_layer(project, ids[0]), mutate)
        with pytest.raises(IntegrityError, match="tree_id"):
            Repo(project).verify()

    def test_added_member_in_a_valid_layer_is_detected(self, project):
        repo, ids = _make(project)
        repo.close()

        def mutate(items):
            extra = tarfile.TarInfo("smuggled.txt")
            return [*items, (extra, b"hi\n")]

        _repack(_layer(project, ids[1]), mutate)
        with pytest.raises(IntegrityError, match="tree_id"):
            Repo(project).verify()

    def test_duplicate_members_are_rejected(self, project):
        """Extraction keeps the LAST of two same-named members — a layer
        with both would show one thing in the manifest and write another."""
        repo, ids = _make(project)
        repo.close()

        def mutate(items):
            first = next(i for i in items if i[0].name == "f0.txt")
            clone = tarfile.TarInfo("f0.txt")
            clone.mode = first[0].mode
            return [*items, (clone, b"second copy\n")]

        _repack(_layer(project, ids[0]), mutate)
        with pytest.raises(IntegrityError, match="duplicate member"):
            Repo(project).verify()

    def test_truncated_layer_is_an_integrity_error_not_eoferror(self, project):
        repo, ids = _make(project)
        repo.close()
        path = _layer(project, ids[1])
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])
        with pytest.raises(IntegrityError):
            Repo(project).verify()

    @pytest.mark.parametrize("where", [0.0, 0.3, 0.6, 0.9, -8, -5, -1])
    def test_bitrot_always_surfaces_as_integrity_error(self, project, where):
        """A flipped byte anywhere in the compressed body or trailer must
        be an IntegrityError — including the 8-byte gzip trailer (CRC32 +
        ISIZE), which tarfile alone never reads once the tar end-marker
        is seen. The gzip HEADER is skipped: its mtime/name/flags carry
        no data and no checksum covers them."""
        repo, ids = _make(project)
        repo.close()
        path = _layer(project, ids[1])
        raw = bytearray(path.read_bytes())
        header_end = 10
        if raw[3] & 8:  # FNAME
            header_end = raw.index(b"\0", 10) + 1
        body = len(raw) - 8 - header_end
        idx = header_end + int(body * where) if where >= 0 else len(raw) + where
        raw[idx] ^= 0xFF
        path.write_bytes(bytes(raw))
        with pytest.raises(IntegrityError):
            Repo(project).verify()

    @pytest.mark.parametrize(
        "column", ["parent", "second_parent", "message", "author", "created_at"]
    )
    def test_db_row_disagreeing_with_layer_meta_is_detected(self, project, column):
        """The DB duplicates the meta record; verify promises to cross-check
        them. A drifted row (e.g. a bad amend writing the wrong parent)
        used to pass."""
        repo, ids = _make(project, 3)
        value = {
            "parent": ids[0],  # a valid FK that is NOT ids[2]'s real parent
            "second_parent": ids[0],
            "message": "something else",
            "author": "someone else",
            "created_at": "1999-01-01T00:00:00+00:00",
        }[column]
        with repo._db:
            repo._db.execute(
                f"UPDATE layers SET {column} = ? WHERE id = ?", (value, ids[2])
            )
        repo.close()
        with pytest.raises(IntegrityError, match=f"{column} mismatch"):
            Repo(project).verify()


class TestReadSideFirewall:
    def _hostile(self, project, ids, outside):
        """ids[0] now holds a symlink d -> ../outside AND a regular
        d/pwn.txt — valid tar, valid gzip, id/meta untouched. Relative
        because the same link then escapes BOTH the export dir
        (tmp/out) and the worktree (tmp/proj) into tmp/outside, and
        ustar caps linknames at 100 bytes."""

        def mutate(items):
            link = tarfile.TarInfo("d")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            link.mode = 0o777
            return [*items, (link, None), (tarfile.TarInfo("d/pwn.txt"), b"pwn\n")]

        _repack(_layer(project, ids[0]), mutate)

    def test_export_never_writes_through_a_symlink_member(self, project, tmp_path):
        repo, ids = _make(project)
        outside = tmp_path / "outside"
        outside.mkdir()
        self._hostile(project, ids, outside)
        with pytest.raises(IntegrityError, match="beneath"):
            repo.export(ids[0], str(tmp_path / "out"))
        repo.close()
        assert not (outside / "pwn.txt").exists()
        assert not (tmp_path / "out" / "f0.txt").exists()  # zero files written

    def test_restore_never_writes_through_a_symlink_member(self, project, tmp_path):
        repo, ids = _make(project)
        outside = tmp_path / "outside"
        outside.mkdir()
        self._hostile(project, ids, outside)
        with pytest.raises(IntegrityError, match="beneath"):
            repo.restore(ids[0])
        repo.close()
        assert not (outside / "pwn.txt").exists()

    def test_file_beneath_a_regular_file_member_is_rejected(self, project):
        """'a' as a file and 'a/b' as a file cannot both be extracted —
        that used to die mid-write with NotADirectoryError."""
        repo, ids = _make(project)
        repo.close()

        def mutate(items):
            return [*items, (tarfile.TarInfo("f0.txt/inner.txt"), b"x\n")]

        _repack(_layer(project, ids[0]), mutate)
        with pytest.raises(IntegrityError, match="beneath"):
            Repo(project).verify()


class TestCommitRefusesUnstorableNames:
    """A name the layer validator rejects (backslash, drive prefix,
    reserved device name) must be refused at COMMIT. It used to pack
    fine, and then status/verify/export/restore failed forever on the
    committed-but-unreadable layer."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only filenames")
    @pytest.mark.parametrize(
        "name",
        [
            "a\\b.txt",
            "con.txt",
            "c:notes.txt",
            "dir/aux",
            "x/nul.log",
            "__firnpack_meta__",
        ],
    )
    def test_commit_refuses_and_leaves_the_repo_usable(self, project, name):
        repo, ids = _make(project, 1)
        before = sorted(p.name for p in (project / ".firn" / "layers").glob("*"))
        write(project, name, "x\n")
        with pytest.raises(FirnpackError, match="cannot store"):
            repo.commit("bad name")
        after = sorted(p.name for p in (project / ".firn" / "layers").glob("*"))
        assert after == before  # no layer written
        assert repo.verify()["status"] == "ok"
        repo.close()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only filenames")
    def test_amend_refuses_too(self, project):
        repo, ids = _make(project, 2)
        write(project, "a\\b.txt", "x\n")
        with pytest.raises(FirnpackError, match="cannot store"):
            repo.amend("oops")
        assert repo.verify()["status"] == "ok"
        repo.close()


class TestDamagedStoreExitCodes:
    """CLI contract: damaged store -> exit 3, one `firnpack:` line, no
    traceback (SPEC §9.8 exit-code contract)."""

    def _assert_humane(self, rc, err):
        assert rc == 3, err
        assert "Traceback" not in err
        assert err.startswith("firnpack: ")

    def test_garbage_database(self, project):
        repo, _ = _make(project, 1)
        repo.close()
        (project / ".firn" / "glacier.db").write_bytes(b"this is not sqlite" * 50)
        rc, _, err = _firn(project, "status")
        self._assert_humane(rc, err)
        assert "--rebuild-db" in err  # actionable: names the recovery

    def test_truncated_layer_every_reader(self, project):
        repo, ids = _make(project, 2)
        repo.close()
        path = _layer(project, ids[1])
        path.write_bytes(path.read_bytes()[:40])
        for cmd in (
            ["verify"],
            ["status"],
            ["history"],
            ["export", ids[1], "--to", "o"],
        ):
            rc, _, err = _firn(project, *cmd)
            if cmd == ["history"]:
                assert rc == 0  # history reads only the database
                continue
            self._assert_humane(rc, err)

    def test_bitrot_verify_exit_code(self, project):
        repo, ids = _make(project, 2)
        repo.close()
        path = _layer(project, ids[1])
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        path.write_bytes(bytes(raw))
        rc, _, err = _firn(project, "verify")
        self._assert_humane(rc, err)


class TestRebuildWhenDatabaseIsMissing:
    def test_other_commands_point_at_the_recovery_not_at_init(self, project):
        """'run firnpack init' is wrong advice: init refuses ('already
        contains a .firn/ directory'). The nearest .firn wins (never walk
        past it into an outer repo)."""
        repo, _ = _make(project, 2)
        repo.close()
        (project / ".firn" / "glacier.db").unlink()
        rc, _, err = _firn(project, "status")
        assert rc == 1
        assert "glacier.db is missing" in err
        assert "--rebuild-db" in err
        assert "run 'firnpack init'" not in err

    def test_rebuild_recovers_a_missing_database(self, project):
        repo, ids = _make(project, 2)
        repo.close()
        (project / ".firn" / "glacier.db").unlink()
        rc, out, err = _firn(project, "verify", "--rebuild-db", "--json")
        assert rc == 0, err
        result = json.loads(out)
        assert result["status"] == "rebuilt"
        assert result["layers"] == 2
        assert result["main_head"] == ids[-1]
        assert result["verify"]["status"] == "ok"
        # and the repo is genuinely usable again
        rc, _, err = _firn(project, "status")
        assert rc == 0, err

    def test_names_of_lost_branches_are_unknowable_not_empty(self, project):
        """With the database gone nothing can say which branches existed.
        `[]` reads as 'none were lost' — the docs promise the rebuild
        never presents itself as lossless, so the signal is null."""
        repo, _ = _make(project, 2)
        repo.branch("side")
        repo.close()
        (project / ".firn" / "glacier.db").unlink()
        rc, out, _ = _firn(project, "verify", "--rebuild-db", "--json")
        assert rc == 0
        assert json.loads(out)["lost_branches"] is None
        rc, out, _ = _firn(project, "verify", "--rebuild-db")  # second run: db exists
        assert rc == 0

    def test_empty_database_file_is_also_unknowable(self, project):
        repo, _ = _make(project, 2)
        repo.close()
        (project / ".firn" / "glacier.db").write_bytes(b"")
        rc, out, _ = _firn(project, "verify", "--rebuild-db", "--json")
        assert rc == 0
        assert json.loads(out)["lost_branches"] is None

    def test_plain_output_says_unknown(self, project):
        repo, _ = _make(project, 2)
        repo.close()
        (project / ".firn" / "glacier.db").unlink()
        rc, out, _ = _firn(project, "verify", "--rebuild-db")
        assert rc == 0
        assert "unknown" in out.lower()

    def test_readable_database_still_names_lost_branches(self, project):
        repo, _ = _make(project, 2)
        repo.branch("side")
        repo.close()
        # tables intact, worktree row gone: names are knowable
        con = sqlite3.connect(project / ".firn" / "glacier.db")
        con.execute("DELETE FROM worktree")
        con.commit()
        con.close()
        rc, out, _ = _firn(project, "verify", "--rebuild-db", "--json")
        assert rc == 0
        assert json.loads(out)["lost_branches"] == ["side"]

    def test_readable_empty_branch_table_is_empty_not_unknowable(self, project):
        """A readable `branches` table with no rows is a known empty set
        (`[]`); only a missing or unreadable database is unknowable (`null`).
        Pins the distinction so the two signals cannot silently merge."""
        repo, _ = _make(project, 2)
        repo.close()
        con = sqlite3.connect(project / ".firn" / "glacier.db")
        con.execute("DELETE FROM branches")
        con.commit()
        con.close()
        rc, out, _ = _firn(project, "verify", "--rebuild-db", "--json")
        assert rc == 0
        assert json.loads(out)["lost_branches"] == []
