"""M2 amend / restore / export / verify / compact tests — SPEC §9.3,
§9.7, §9.8, §9.9 contracts, including every ratified guard.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firnpack import FirnpackError, IntegrityError, Repo  # noqa: E402


def write(root: Path, rel: str, data, mode=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    p.write_bytes(data)
    if mode is not None:
        p.chmod(mode)
    return p


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _chain(repo, n=4):
    """n sequential commits on main, returns layer ids newest-first."""
    ids = []
    for i in range(n):
        write(repo.root, f"f{i}.txt", f"content {i}\n")
        result = repo.commit(f"c{i}")
        if result["status"] == "packed":
            ids.append(result["layer_id"])
    return ids


class TestAmend:
    def test_amend_replaces_head_keeps_parent(self, project):
        repo = Repo.init(project)
        _chain(repo, 2)
        old_head = repo._worktree()["head_id"]
        write(project, "f1.txt", "content 1 amended\n")
        result = repo.amend()
        repo.close()
        assert result["status"] == "amended"
        assert result["replaced"] == old_head
        # same parent lineage
        repo = Repo(project)
        old_row = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?", (old_head,)
        ).fetchone()
        new_row = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?",
            (result["layer_id"],),
        ).fetchone()
        repo.close()
        assert new_row[0] == old_row[0]
        # old layer still on disk (write-once)
        assert (project / ".firn" / "layers" / f"{old_head}.firn").is_file()

    def test_amend_message_only(self, project):
        repo = Repo.init(project)
        _chain(repo, 1)
        result = repo.amend(message="better message")
        repo.close()
        assert result["status"] == "amended"
        repo = Repo(project)
        h = repo.history()
        repo.close()
        assert h["commits"][0]["message"] == "better message"

    def test_amend_nothing_to_amend(self, project):
        repo = Repo.init(project)
        _chain(repo, 1)
        result = repo.amend()  # no change, no message
        repo.close()
        assert result["status"] == "nothing_to_amend"

    def test_amend_refuses_pending_merge(self, project):
        repo = Repo.init(project)
        write(project, "a.txt", "base\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "a.txt", "side\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "a.txt", "main\n")
        repo.commit("main final")
        repo.merge("side")  # conflict pending
        with pytest.raises(FirnpackError, match="merge is pending"):
            repo.amend()
        repo.close()

    def test_amend_empty_repo(self, project):
        repo = Repo.init(project)
        with pytest.raises(FirnpackError, match="nothing to amend"):
            repo.amend()
        repo.close()


class TestRestore:
    def test_restore_rescues_drift_first(self, project):
        repo = Repo.init(project)
        _chain(repo, 2)
        target = repo._worktree()["head_id"]
        write(project, "f1.txt", "PRECIOUS UNCOMMITTED\n")
        result = repo.restore(target[:10])
        repo.close()
        assert result["status"] == "restored"
        assert result["rescue_layer"] is not None
        # worktree back at the head
        assert (project / "f1.txt").read_text() == "content 1\n"
        # precious edit survives in the rescue layer
        repo = Repo(project)
        _, manifest, _ = repo._read_layer(result["rescue_layer"])
        rescued = repo._layer_contents(
            result["rescue_layer"],
            {p for p, v in manifest.items() if v["target"] is None},
        )
        repo.close()
        assert b"PRECIOUS UNCOMMITTED" in rescued.get("f1.txt", b"")

    def test_restore_no_drift_no_rescue(self, project):
        repo = Repo.init(project)
        ids = _chain(repo, 3)  # newest-first: [c2, c1, c0]
        result = repo.restore(ids[1][:10])  # back to c1's tree (f0, f1)
        repo.close()
        assert result["rescue_layer"] is None
        assert (project / "f0.txt").read_text() == "content 0\n"
        assert (project / "f1.txt").read_text() == "content 1\n"
        assert not (project / "f2.txt").exists()  # added by c2, not in c1

    def test_restore_to_dir_leaves_worktree_alone(self, project):
        repo = Repo.init(project)
        ids = _chain(repo, 2)
        write(project, "f1.txt", "dirty\n")
        result = repo.restore(ids[1][:10], to_dir=str(project / "out"))
        repo.close()
        assert result["status"] == "exported"
        assert (project / "out" / "f0.txt").read_text() == "content 0\n"
        assert (project / "f1.txt").read_text() == "dirty\n"  # untouched

    def test_restore_refuses_pending_merge(self, project):
        repo = Repo.init(project)
        write(project, "a.txt", "base\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "a.txt", "side\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "a.txt", "main\n")
        repo.commit("main final")
        repo.merge("side")
        with pytest.raises(FirnpackError, match="merge is pending"):
            repo.restore("main")
        repo.close()


class TestExport:
    def test_export_fresh_dir(self, project):
        repo = Repo.init(project)
        _chain(repo, 2)
        head = repo._worktree()["head_id"]
        repo.close()
        result = Repo(project).export(head[:10], str(project / "snap"))
        assert result["status"] == "exported"
        assert (project / "snap" / "f0.txt").read_text() == "content 0\n"
        assert (project / "snap" / "f1.txt").read_text() == "content 1\n"

    def test_export_refuses_nonempty_dir(self, project):
        repo = Repo.init(project)
        _chain(repo, 1)
        head = repo._worktree()["head_id"]
        repo.close()
        dest = project / "occupied"
        dest.mkdir()
        (dest / "junk.txt").write_text("x")
        with pytest.raises(FirnpackError, match="not empty"):
            Repo(project).export(head[:10], str(dest))

    def test_export_preserves_exec_bit(self, project):
        if sys.platform == "win32":
            pytest.skip("no settable exec bit on Windows")
        write(project, "run.sh", "echo hi\n", mode=0o755)
        repo = Repo.init(project)
        repo.commit("exec")
        head = repo._worktree()["head_id"]
        repo.close()
        Repo(project).export(head[:10], str(project / "snap"))
        import stat as stat_mod

        mode = (project / "snap" / "run.sh").stat().st_mode
        assert mode & stat_mod.S_IXUSR


class TestVerify:
    def test_verify_ok(self, project):
        repo = Repo.init(project)
        _chain(repo, 3)
        repo.close()
        result = Repo(project).verify()
        assert result == {"status": "ok", "layers": 3}

    def test_verify_detects_missing_layer_file(self, project):
        repo = Repo.init(project)
        ids = _chain(repo, 2)
        repo.close()
        (project / ".firn" / "layers" / f"{ids[1]}.firn").unlink()
        with pytest.raises(IntegrityError, match="missing layer file"):
            Repo(project).verify()

    def test_verify_detects_corrupt_layer(self, project):
        repo = Repo.init(project)
        ids = _chain(repo, 2)
        repo.close()
        path = project / ".firn" / "layers" / f"{ids[0]}.firn"
        path.write_bytes(b"corrupted bytes")
        with pytest.raises(IntegrityError, match="cannot open layer"):
            Repo(project).verify()

    def test_verify_detects_tampered_meta(self, project):
        repo = Repo.init(project)
        ids = _chain(repo, 2)
        repo.close()
        # swap two layer files' contents (ids no longer match filenames)
        d = project / ".firn" / "layers"
        a = d / f"{ids[0]}.firn"
        b = d / f"{ids[1]}.firn"
        a_bytes, b_bytes = a.read_bytes(), b.read_bytes()
        a.write_bytes(b_bytes)
        b.write_bytes(a_bytes)
        with pytest.raises(IntegrityError, match="id mismatch"):
            Repo(project).verify()

    def test_verify_detects_orphan_file(self, project):
        repo = Repo.init(project)
        _chain(repo, 1)
        repo.close()
        d = project / ".firn" / "layers"
        (d / f"{'0' * 40}.firn").write_bytes(b"orphan")
        with pytest.raises(IntegrityError, match="orphan layer file"):
            Repo(project).verify()


class TestCompact:
    def _repo_with_chain(self, project, n=6):
        repo = Repo.init(project)
        for i in range(n):
            write(project, f"f{i}.txt", f"content {i}\n" * (i + 1))
            repo.commit(f"c{i}")
        return repo

    def test_dry_run_mutates_nothing(self, project):
        repo = self._repo_with_chain(project)
        before = sorted(p.name for p in (project / ".firn" / "layers").glob("*"))
        result = repo.compact(2, dry_run=True)
        repo.close()
        assert result["status"] == "plan"
        assert result["layers_dropped"] == 4
        after = sorted(p.name for p in (project / ".firn" / "layers").glob("*"))
        assert before == after

    def test_compact_keep_one_deep_clean(self, project):
        repo = self._repo_with_chain(project)
        result = repo.compact(1, yes=True)
        repo.close()
        assert result["status"] == "compacted"
        assert result["kept"] == 2  # foundation + head
        assert result["dropped"] == 5
        repo = Repo(project)
        count = repo._db.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
        repo.close()
        assert count == 2
        # worktree intact and clean at the new head
        repo = Repo(project)
        st = repo.status()
        h = repo.history()
        repo.close()
        assert st["clean"]
        assert h["count"] == 2
        # files still present
        assert (project / "f5.txt").read_text() == "content 5\n" * 6

    def test_compact_keep_n(self, project):
        repo = self._repo_with_chain(project)
        result = repo.compact(3, yes=True)
        repo.close()
        assert result["status"] == "compacted"
        repo = Repo(project)
        count = repo._db.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
        h = repo.history()
        repo.close()
        assert count == 4  # foundation + 3 kept
        assert h["count"] == 4
        # newest messages survive
        msgs = [c["message"] for c in h["commits"]]
        assert "c5" in msgs and "c4" in msgs and "c3" in msgs

    def test_compact_refuses_without_yes(self, project):
        repo = self._repo_with_chain(project)
        with pytest.raises(FirnpackError, match="--yes"):
            repo.compact(2)
        repo.close()

    def test_compact_refuses_pending_merge(self, project):
        repo = self._repo_with_chain(project)
        repo.branch("side")
        repo.switch("side")
        write(project, "f0.txt", "side change\n")
        repo.commit("side work")
        repo.switch("main")
        write(project, "f0.txt", "main change\n")
        repo.commit("main work")
        repo.merge("side")  # conflict
        with pytest.raises(FirnpackError, match="merge is pending"):
            repo.compact(1, yes=True)
        repo.close()

    def test_compact_refuses_branch_reaching_collapsed(self, project):
        repo = self._repo_with_chain(project)
        ids = []
        cursor = repo._worktree()["head_id"]
        for _ in range(6):
            ids.append(cursor)
            cursor = repo._db.execute(
                "SELECT parent FROM layers WHERE id = ?", (cursor,)
            ).fetchone()[0]
        # branch pointing at an OLD layer (inside the collapsed set)
        repo.branch("old", ids[4])
        with pytest.raises(FirnpackError, match="collapsed range"):
            repo.compact(2, yes=True)
        repo.close()

    def test_compact_archives_dropped(self, project):
        repo = self._repo_with_chain(project)
        arc = str(project / "archive")
        result = repo.compact(2, yes=True, archive_dir=arc)
        repo.close()
        assert result["archive"] is not None
        import zipfile

        with zipfile.ZipFile(result["archive"]) as zf:
            names = zf.namelist()
        assert len(names) == 4  # the 4 dropped layers

    def test_compact_nothing_to_compact(self, project):
        repo = self._repo_with_chain(project, n=3)
        result = repo.compact(5, yes=True)
        repo.close()
        assert result["status"] == "nothing_to_compact"

    def test_compact_survives_verify(self, project):
        """The post-compact glacier must pass verify — every rewritten
        layer hashes to its filename, no dangling refs, no orphans."""
        repo = self._repo_with_chain(project)
        repo.compact(2, yes=True)
        repo.close()
        result = Repo(project).verify()
        assert result["status"] == "ok"

    def test_compact_keeps_worktree_clean_and_commits_continue(self, project):
        repo = self._repo_with_chain(project)
        repo.compact(2, yes=True)
        write(project, "new.txt", "after compact\n")
        result = repo.commit("after compact")
        repo.close()
        assert result["status"] == "packed"
        repo = Repo(project)
        st = repo.status()
        repo.close()
        assert st["clean"]
