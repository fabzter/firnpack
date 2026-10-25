"""Worktree-transition regressions for the real-world campaign (plan §4).

Unit-level (Repo API) coverage of ordinary file<->directory transitions,
protected ignored/special leaves, manifest-authority deletion, partial-write
rollback with created directories, and merge-abort failure diagnostics.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import FirnpackError, IntegrityError, Repo  # noqa: E402

SYMLINKS = os.name != "nt"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(root: Path, rel: str, data: bytes | str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return p


def tree_bytes(root: Path) -> dict:
    """relpath -> bytes (or ('link', target)) below root, .firn excluded."""
    out = {}
    for dirpath, dirs, names in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0] == ".firn":
            dirs[:] = []
            continue
        for name in names:
            p = Path(dirpath) / name
            key = (rel / name).as_posix()
            if p.is_symlink():
                out[key] = ("link", os.readlink(p))
            else:
                out[key] = p.read_bytes()
    return out


def _transition_repo(project):
    """c1: 'x' is a file. c2 (main head): 'x' is a dir with x/inner.txt."""
    write(project, "x", "file x\n")
    write(project, "keep.txt", "keep\n")
    repo = Repo.init(project)
    c1 = repo.commit("file x")["layer_id"]
    (project / "x").unlink()
    write(project, "x/inner.txt", "dir x\n")
    c2 = repo.commit("dir x")["layer_id"]
    return repo, c1, c2


class TestTrackedKindTransitions:
    """Ordinary fully-tracked file<->directory transitions must succeed."""

    def test_switch_file_to_dir_and_back_via_branch(self, project):
        repo, c1, c2 = _transition_repo(project)
        repo.branch("old", c1)
        repo.switch("old")
        assert (project / "x").is_file()
        assert (project / "x").read_bytes() == b"file x\n"
        assert not (project / "x" / "inner.txt").exists()
        assert repo.status()["clean"] is True
        repo.switch("main")
        assert (project / "x").is_dir()
        assert (project / "x" / "inner.txt").read_bytes() == b"dir x\n"
        assert repo.status()["clean"] is True
        assert repo._worktree()["head_id"] == c2
        repo.close()

    def test_restore_dir_to_file_and_back(self, project):
        repo, c1, c2 = _transition_repo(project)
        out = repo.restore(c1)  # dir -> file
        assert out["status"] == "restored"
        assert (project / "x").is_file()
        assert (project / "x").read_bytes() == b"file x\n"
        assert repo.status()["clean"] is True
        out = repo.restore(c2)  # file -> dir
        assert out["status"] == "restored"
        assert (project / "x" / "inner.txt").read_bytes() == b"dir x\n"
        assert repo.status()["clean"] is True
        repo.close()

    def test_file_to_dir_through_pinned_switch(self, project):
        repo, c1, c2 = _transition_repo(project)
        repo.restore(c1)
        out = repo.switch(c2)  # pin to the dir version
        assert out["kind"] == "pin"
        assert (project / "x" / "inner.txt").read_bytes() == b"dir x\n"
        assert repo.status()["clean"] is True
        repo.close()

    @pytest.mark.skipif(not SYMLINKS, reason="needs symlink support")
    def test_symlink_to_file_and_back(self, project):
        write(project, "real.txt", "data\n")
        (project / "lnk").symlink_to("real.txt")
        repo = Repo.init(project)
        c_link = repo.commit("link")["layer_id"]
        (project / "lnk").unlink()
        write(project, "lnk", "now a file\n")
        c_file = repo.commit("file")["layer_id"]
        repo.restore(c_link)  # file -> symlink
        assert (project / "lnk").is_symlink()
        assert os.readlink(project / "lnk") == "real.txt"
        repo.restore(c_file)  # symlink -> file
        assert (project / "lnk").is_file()
        assert (project / "lnk").read_bytes() == b"now a file\n"
        assert repo.status()["clean"] is True
        repo.close()


class TestProtectedLeaves:
    """Invisible worktree entries are protected unconditionally."""

    def test_now_ignored_leaf_at_target_refuses_and_survives(self, project):
        """c1 tracks local.dat; c2 drops it and .gitignore learns it. An
        ignored local.dat recreated on disk must not be overwritten by a
        switch to the historical version."""
        write(project, "local.dat", "historical bytes\n")
        write(project, "other.txt", "o\n")
        repo = Repo.init(project)
        c1 = repo.commit("with local.dat")["layer_id"]
        (project / "local.dat").unlink()
        (project / ".gitignore").write_bytes(b"local.dat\n")
        c2 = repo.commit("drop local.dat")["layer_id"]
        repo.close()
        write(project, "local.dat", "synthetic ignored bytes\n")
        repo = Repo(project)
        assert repo.status()["clean"] is True
        with pytest.raises(FirnpackError) as exc:
            repo.switch(c1)
        assert "local.dat" in str(exc.value)
        assert (project / "local.dat").read_bytes() == b"synthetic ignored bytes\n"
        assert repo._worktree()["head_id"] == c2
        assert repo.status()["clean"] is True
        repo.close()

    def test_ignored_child_blocks_dir_to_file(self, project):
        """Directory at the target leaf containing an ignored file must
        refuse before any tracked deletion."""
        repo, c1, c2 = _transition_repo(project)
        (project / ".gitignore").write_bytes(b"~$*\nx/swap.tmp\n")
        repo.commit("ignore swap.tmp")
        write(project, "x/swap.tmp", b"locked draft\n")
        with pytest.raises(FirnpackError) as exc:
            repo.restore(c1)
        assert "x" in str(exc.value)
        assert (project / "x" / "swap.tmp").read_bytes() == b"locked draft\n"
        assert (project / "x" / "inner.txt").read_bytes() == b"dir x\n"
        assert repo._worktree()["head_id"] != c1
        repo.close()

    @pytest.mark.skipif(not SYMLINKS, reason="needs symlink support")
    def test_symlinked_ancestor_never_written_through(self, project):
        """restore (no drift refusal) must not write through a symlinked
        directory ancestor pointing outside the repo."""
        import shutil

        write(project, "d/f.txt", "inside\n")
        repo = Repo.init(project)
        c1 = repo.commit("c1")["layer_id"]
        write(project, "d/f.txt", "inside2\n")
        repo.commit("c2")
        outside = project.parent / "o"  # short name: link target must
        outside.mkdir(exist_ok=True)  # fit the 100-char ustar linkname
        shutil.rmtree(project / "d")
        (project / "d").symlink_to("../o", target_is_directory=True)
        # the link is rescued work, so restore may REPLACE it — but must
        # unlink it first and never write through it.
        out = repo.restore(c1)
        assert out["rescue_layer"] is not None
        assert not (outside / "f.txt").exists()
        assert not (outside / "d").exists()
        assert (project / "d").is_dir() and not (project / "d").is_symlink()
        assert (project / "d" / "f.txt").read_bytes() == b"inside\n"
        assert repo.status()["clean"] is True
        repo.close()

    @pytest.mark.skipif(not SYMLINKS, reason="needs symlink support")
    def test_dangling_symlink_at_target_is_protected(self, project):
        """An ignored dangling symlink at a target path is invisible to
        _walk and must be refused, never unlinked."""
        write(project, "ghost.txt", "historical\n")
        write(project, "a.txt", "a\n")
        repo = Repo.init(project)
        c1 = repo.commit("c1")["layer_id"]
        (project / "ghost.txt").unlink()
        (project / ".gitignore").write_bytes(b"ghost.txt\n")
        c2 = repo.commit("c2")["layer_id"]
        repo.close()
        (project / "ghost.txt").symlink_to("definitely-missing")
        repo = Repo(project)
        with pytest.raises(FirnpackError) as exc:
            repo.switch(c1)
        assert "ghost.txt" in str(exc.value)
        assert (project / "ghost.txt").is_symlink()
        assert repo._worktree()["head_id"] == c2
        repo.close()

    def test_fifo_at_target_is_protected(self, project):
        if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
            pytest.skip("no mkfifo/SIGALRM")
        write(project, "a.txt", "a\n")
        repo = Repo.init(project)
        c1 = repo.commit("c1")["layer_id"]
        write(project, "a.txt", "a2\n")
        c2 = repo.commit("c2")["layer_id"]
        (project / "a.txt").unlink()
        os.mkfifo(project / "a.txt")

        def _time_up(_sig, _frame):
            raise TimeoutError("restore blocked opening a FIFO")

        signal.signal(signal.SIGALRM, _time_up)
        signal.alarm(10)
        try:
            with pytest.raises(FirnpackError):
                repo.restore(c1)
        finally:
            signal.alarm(0)
        assert stat.S_ISFIFO(os.lstat(project / "a.txt").st_mode)
        assert repo._worktree()["head_id"] == c2
        repo.close()


class TestManifestAuthority:
    """Deletion baseline is the old head manifest, never stat_cache."""

    def test_switch_after_rebuild_null_cache(self, project):
        """verify --rebuild-db writes stat_cache=NULL; the next restore must
        still delete tracked leaves of the previous head."""
        repo, c1, c2 = _transition_repo(project)
        repo.close()
        (project / ".firn" / "glacier.db").unlink()
        repo = Repo(project, recovering=True)
        repo.rebuild_db()
        repo.close()
        repo = Repo(project)
        assert repo._worktree()["stat_cache"] is None
        assert repo._worktree()["head_id"] == c2
        repo.restore(c1)
        assert (project / "x").is_file()
        assert not (project / "x" / "inner.txt").exists()
        assert repo.status()["clean"] is True
        repo.close()


class TestPartialWriteRollback:
    """Injected mid-write failures roll back DB and created paths."""

    def _two_state_repo(self, project):
        write(project, "a.txt", "one\n")
        write(project, "b.txt", "bee\n")
        repo = Repo.init(project)
        first = repo.commit("first")["layer_id"]
        write(project, "a.txt", "two\n")
        write(project, "b.txt", "bee2\n")
        second = repo.commit("second")["layer_id"]
        return repo, first, second

    def test_mid_write_failure_restores_previous_head(self, project, monkeypatch):
        repo, first, second = self._two_state_repo(project)
        original = OSError(28, "disk full")
        real_write = firnpack._write_file

        fired = {"done": False}

        def flaky(fp, data):
            # fail the second leaf write, after a.txt already landed;
            # rollback re-materializes all leaves, so fire only ONCE
            if fp.name == "b.txt" and fp.parent == repo.root and not fired["done"]:
                fired["done"] = True
                raise original
            return real_write(fp, data)

        monkeypatch.setattr(firnpack, "_write_file", flaky)
        with pytest.raises(OSError) as caught:
            repo.restore(first)
        monkeypatch.undo()
        assert caught.value is original
        assert repo._worktree()["head_id"] == second
        assert (project / "a.txt").read_bytes() == b"two\n"
        assert (project / "b.txt").read_bytes() == b"bee2\n"
        assert repo.status()["clean"] is True
        repo.close()

    def test_rollback_removes_created_directories(self, project, monkeypatch):
        """file->dir target that fails mid-write: rollback must remove the
        created directory so the previous file head can return."""
        repo, c1, c2 = _transition_repo(project)
        repo.restore(c1)  # back to file x
        original = OSError(28, "disk full")
        real_write = firnpack._write_file

        def flaky(fp, data):
            if fp.name == "inner.txt":
                raise original
            return real_write(fp, data)

        monkeypatch.setattr(firnpack, "_write_file", flaky)
        with pytest.raises(OSError):
            repo.restore(c2)
        monkeypatch.undo()
        # rollback had to rmdir the created x/ so file x could return
        assert (project / "x").is_file()
        assert (project / "x").read_bytes() == b"file x\n"
        assert repo._worktree()["head_id"] == c1
        assert repo.status()["clean"] is True
        repo.close()

    def test_rollback_failure_reports_partial(self, project, monkeypatch):
        """A rollback that itself fails must say 'partly', keep the DB at
        the previous head, and preserve the original exception."""
        repo, c1, c2 = _transition_repo(project)
        repo.restore(c1)  # file x
        original = OSError(28, "disk full")
        real_write = firnpack._write_file
        real_rmdir = Path.rmdir
        calls = {"rmdir_x": 0}

        def flaky_write(fp, data):
            if fp.name == "inner.txt":
                raise original  # forward write fails
            return real_write(fp, data)

        def flaky_rmdir(self, *a, **kw):
            if self == repo.root / "x":
                calls["rmdir_x"] += 1
                raise OSError(39, "injected rollback rmdir failure")
            return real_rmdir(self, *a, **kw)

        monkeypatch.setattr(firnpack, "_write_file", flaky_write)
        monkeypatch.setattr(Path, "rmdir", flaky_rmdir)
        with pytest.raises(OSError) as caught:
            repo.restore(c2)
        monkeypatch.undo()
        assert caught.value is original
        assert repo._worktree()["head_id"] == c1  # DB rolled back
        assert calls["rmdir_x"] >= 1
        repo.close()


class TestMergeAbortFailure:
    def test_failed_abort_keeps_pending_and_names_rescue(self, project, monkeypatch):
        write(project, "shared.txt", "l1\nl2\nl3\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "shared.txt", "l1\nSIDE\nl3\n")
        repo.commit("side")
        repo.switch("main")
        write(project, "shared.txt", "l1\nMAIN\nl3\n")
        repo.commit("main")
        result = repo.merge("side")
        assert result["status"] == "conflict"
        # touch the worktree so abort writes a rescue layer
        write(project, "shared.txt", "l1\nhalf-resolved\nl3\n")

        def boom(layer_id, **kw):
            raise OSError(28, "disk full")

        monkeypatch.setattr(repo, "_sync_worktree_to", boom)
        with pytest.raises(OSError):
            repo.merge_abort()
        monkeypatch.undo()
        wt = repo._worktree()
        assert wt["merge_theirs"] is not None, "pending merge must survive"
        row = repo._db.execute(
            "SELECT id FROM layers WHERE message LIKE '%drift rescue%'"
        ).fetchone()
        assert row is not None
        out = repo.merge_abort()
        assert out["status"] == "aborted"
        assert out["rescue_layer"] is not None
        repo.close()


class TestRescuedObstructions:
    """After a durable rescue, restore may remove NEW visible files that
    obstruct the target tree; unrelated new files survive."""

    def test_new_file_obstructing_needed_directory_removed_after_rescue(self, project):
        repo, c1, c2 = _transition_repo(project)
        repo.restore(c1)  # file x
        write(project, "extra.txt", "brand new\n")
        # 'x' is drift (old-head file still on disk, content equal -> no;
        # make it genuinely drifted so restore isn't refused-trivial)
        (project / "x").write_bytes(b"stray file where a dir must go\n")
        out = repo.restore(c2)
        assert out["status"] == "restored"
        assert out["rescue_layer"] is not None
        assert (project / "x" / "inner.txt").read_bytes() == b"dir x\n"
        assert (project / "extra.txt").read_bytes() == b"brand new\n"
        assert repo.status()["added"] == ["extra.txt"]
        repo.close()


class TestMergedTreePreflight:
    def test_file_and_descendant_merged_tree_refuses(self, project):
        """ours has dir p/*, theirs has file p: the merged tree is
        impossible; merge must refuse without touching worktree or
        setting pending state."""
        write(project, "shared.txt", "l1\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "p", "file p\n")  # theirs: p is a file
        repo.commit("side file p")
        repo.switch("main")
        write(project, "p/inner.txt", "dir p\n")  # ours: p is a dir
        repo.commit("main dir p")
        before = tree_bytes(project)
        with pytest.raises(FirnpackError):
            repo.merge("side")
        assert repo._worktree()["merge_theirs"] is None
        assert tree_bytes(project) == before
        repo.close()


class TestSwitchBranchRollback:
    def test_failed_branch_switch_restores_selection_and_files(
        self, project, monkeypatch
    ):
        """head_kind/head_branch/head_id must all roll back after a
        mid-write switch failure — not just head_id."""
        write(project, "a.txt", "one\n")
        write(project, "b.txt", "bee\n")
        repo = Repo.init(project)
        repo.commit("first")
        write(project, "a.txt", "two\n")
        write(project, "b.txt", "bee2\n")
        second = repo.commit("second")["layer_id"]
        first = repo._db.execute(
            "SELECT id FROM layers WHERE id != ?", (second,)
        ).fetchone()[0]
        repo.branch("first-branch", first)
        original = OSError(28, "disk full")
        real_write = firnpack._write_file
        fired = {"done": False}

        def flaky(fp, data):
            if fp.name == "b.txt" and not fired["done"]:
                fired["done"] = True
                raise original
            return real_write(fp, data)

        monkeypatch.setattr(firnpack, "_write_file", flaky)
        with pytest.raises(OSError):
            repo.switch("first-branch")
        monkeypatch.undo()
        wt = repo._worktree()
        assert wt["head_kind"] == "branch"
        assert wt["head_branch"] == "main"
        assert wt["head_id"] == second
        assert repo.status()["clean"] is True
        repo.close()


class TestForeignLeafCollision:
    """A leaf that appears between preflight and write is FOREIGN bytes:
    never unlinked, never claimed as our own creation (R2). Rollback must
    preserve it and report a partial restore honestly.

    RED against the frozen engine: created_paths records a path before
    the write, so collision rollback unlinks foreign data; and the
    materialize/symlink branches silently unlink raced leaves outright."""

    FOREIGN = b"foreign bytes - do not delete\n"

    def _two_state_repo(self, project):
        write(project, "a.txt", "one\n")
        write(project, "b.txt", "bee\n")
        repo = Repo.init(project)
        first = repo.commit("first")["layer_id"]
        write(project, "a.txt", "two\n")
        write(project, "b.txt", "bee2\n")
        second = repo.commit("second")["layer_id"]
        return repo, first, second

    def test_head_move_leaf_collision_preserves_foreign_bytes(
        self, project, monkeypatch, capsys
    ):
        """restore() target leaf races in after preflight: O_EXCL refuses,
        rollback removes only its own creations and skips re-materializing
        over the foreign leaf; selection restored, error is the EEXIST."""
        repo, first, second = self._two_state_repo(project)
        fired = {"done": False}
        real_write = firnpack._write_file

        def raced(fp, data):
            if fp.name == "b.txt" and not fired["done"]:
                fired["done"] = True  # once: the re-materialize must run clean
                fp.write_bytes(self.FOREIGN)  # foreign leaf lands first
                raise FileExistsError(17, "file exists", str(fp))
            return real_write(fp, data)

        monkeypatch.setattr(firnpack, "_write_file", raced)
        with pytest.raises(FileExistsError):
            repo.restore(first)
        monkeypatch.undo()

        assert (project / "b.txt").read_bytes() == self.FOREIGN
        # our own earlier write is removed, nothing pretends to be clean
        assert not (project / "a.txt").exists()
        assert repo._worktree()["head_id"] == second
        assert "partly" in capsys.readouterr().err
        assert repo.status()["clean"] is False
        # a later unobstructed restore still recovers once the user
        # clears the foreign leaf themselves
        (project / "b.txt").unlink()
        write(project, "b.txt", "bee\n")  # first's bytes, by user choice
        repo.restore(first)
        repo.close()

    def test_merge_materialize_leaf_race_never_unlinks_foreign(
        self, project, monkeypatch
    ):
        """_write_tree_leaves must not unlink+claim a leaf that appears
        between _apply_prepared and the write - EEXIST is a collision."""
        write(project, "shared.txt", "l1\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "new.txt", "theirs only\n")
        repo.commit("side adds new.txt")
        repo.switch("main")
        write(project, "main_only.txt", "main\n")  # divergent, not FF
        repo.commit("main advances")

        fired = {"done": False}
        real_apply = repo._apply_prepared

        def apply_then_race(prepared):
            real_apply(prepared)
            if not fired["done"]:
                fired["done"] = True
                write(project, "new.txt", self.FOREIGN)  # leaf lands post-apply

        monkeypatch.setattr(repo, "_apply_prepared", apply_then_race)
        with pytest.raises(OSError):
            repo.merge("side")  # clean divergent merge -> materialize
        monkeypatch.undo()

        assert (project / "new.txt").read_bytes() == self.FOREIGN
        # pending merge stays: the user decides, nothing is half-hidden
        assert repo._worktree()["merge_theirs"] is not None
        # user clears the foreign leaf, then abort finishes honestly
        (project / "new.txt").unlink()
        out = repo.merge_abort()
        assert out["status"] == "aborted"
        assert repo.status()["clean"] is True
        repo.close()

    def test_abort_leaf_collision_preserves_foreign_bytes(
        self, project, monkeypatch, capsys
    ):
        """merge --abort recreating an ours-side leaf hits a raced foreign
        file at that slot: preserved, pending merge kept, partial reported."""
        write(project, "shared.txt", "l1\nl2\nl3\n")
        write(project, "ours_only.txt", "ours\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        (project / "ours_only.txt").unlink()
        write(project, "shared.txt", "l1\nSIDE\nl3\n")
        repo.commit("side")
        repo.switch("main")
        write(project, "shared.txt", "l1\nMAIN\nl3\n")
        repo.commit("main")
        assert repo.merge("side")["status"] == "conflict"

        fired = {"done": False}
        real_write = firnpack._write_file

        def raced(fp, data):
            if fp.name == "ours_only.txt" and not fired["done"]:
                fired["done"] = True
                fp.write_bytes(self.FOREIGN)
                raise FileExistsError(17, "file exists", str(fp))
            return real_write(fp, data)

        monkeypatch.setattr(firnpack, "_write_file", raced)
        with pytest.raises(FileExistsError):
            repo.merge_abort()
        monkeypatch.undo()

        assert (project / "ours_only.txt").read_bytes() == self.FOREIGN
        assert repo._worktree()["merge_theirs"] is not None
        assert "still pending" in capsys.readouterr().err
        # user removes the foreign leaf, abort retries cleanly
        (project / "ours_only.txt").unlink()
        out = repo.merge_abort()
        assert out["status"] == "aborted"
        assert repo.status()["clean"] is True
        repo.close()

    @pytest.mark.skipif(not SYMLINKS, reason="needs symlink support")
    def test_switch_symlink_leaf_race_never_unlinks_foreign(self, project, monkeypatch):
        """The symlink branch must not unlink a leaf that lands between
        preflight and _symlink - short relative target, EEXIST collision."""
        write(project, "keep.txt", "keep\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("linked")
        repo.switch("linked")
        (project / "link.txt").symlink_to("keep.txt")
        repo.commit("link head")
        repo.switch("main")

        fired = {"done": False}
        real_ensure = repo._ensure_parents

        def ensure_then_race(fp, created_dirs=None):
            real_ensure(fp, created_dirs)
            if fp.name == "link.txt" and not fp.exists() and not fired["done"]:
                fired["done"] = True
                fp.write_bytes(self.FOREIGN)  # foreign leaf at the slot

        monkeypatch.setattr(repo, "_ensure_parents", ensure_then_race)
        with pytest.raises(OSError):
            repo.switch("linked")
        monkeypatch.undo()

        fp = project / "link.txt"
        assert fp.is_file() and fp.read_bytes() == self.FOREIGN
        # head never moved; restore is honestly partial (clean False)
        assert repo._worktree()["head_branch"] == "main"
        assert repo.status()["clean"] is False
        # honest recovery: user clears the foreign leaf, re-materializes the
        # still-current head (status clean again), then the switch succeeds
        fp.unlink()
        repo.restore(repo._worktree()["head_id"])
        assert repo.status()["clean"] is True
        repo.switch("linked")
        assert fp.is_symlink() and os.readlink(fp) == "keep.txt"
        repo.close()


class TestOwnedPartialCleanup:
    """_write_file owns its O_EXCL output: a mid-write failure removes the
    partial (identity-checked), preserving the original exception (R2).

    The repo-level test pins rollback semantics (previous head restored);
    the unit tests pin the helper contract itself - RED on the pre-fix
    helper which left the partial in place."""

    def test_failed_restore_rolls_back_to_previous_head(self, project, monkeypatch):
        """A partial leaf write inside restore() aborts the move: rollback
        returns the worktree to the PREVIOUS head (second), not the
        requested one (first), and the original error survives."""
        write(project, "a.txt", "one\n")
        repo = Repo.init(project)
        first = repo.commit("first")["layer_id"]
        write(project, "a.txt", "two\n")
        second = repo.commit("second")["layer_id"]

        original = OSError(28, "disk full")
        real_open = os.open
        real_write = os.write
        open_fds: set[int] = set()
        fired = {"done": False}

        def spy_open(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if Path(path).name == "a.txt" and flags & os.O_EXCL:
                open_fds.add(fd)
            return fd

        def flaky_write(fd, data):
            if fd in open_fds and not fired["done"]:
                fired["done"] = True
                real_write(fd, data[:4])  # partial bytes reach the fd
                raise original
            return real_write(fd, data)

        monkeypatch.setattr(firnpack.os, "open", spy_open)
        monkeypatch.setattr(firnpack.os, "write", flaky_write)
        with pytest.raises(OSError) as caught:
            repo.restore(first)
        monkeypatch.undo()
        assert caught.value is original
        # previous head restored, worktree matches second exactly
        assert (project / "a.txt").read_bytes() == b"two\n"
        assert repo._worktree()["head_id"] == second
        assert repo.status()["clean"] is True
        # first is still reachable on retry
        repo.restore(first)
        assert (project / "a.txt").read_bytes() == b"one\n"
        assert repo._worktree()["head_id"] == first
        repo.close()

    def test_write_file_removes_own_partial_on_failure(self, tmp_path, monkeypatch):
        """Direct helper pin: mid-write failure leaves NO file at the path
        and the original exception propagates (old helper leaked the
        partial)."""
        fp = tmp_path / "victim.bin"
        original = OSError(28, "disk full")
        real_write = os.write
        fired = {"done": False}

        def flaky(fd, data):
            if not fired["done"]:
                fired["done"] = True
                real_write(fd, data[:4])  # some bytes land
                raise original
            return real_write(fd, data)

        monkeypatch.setattr(firnpack.os, "write", flaky)
        with pytest.raises(OSError) as caught:
            firnpack._write_file(fp, b"abcdefghij")
        monkeypatch.undo()
        assert caught.value is original
        assert not fp.exists()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX unlink-open-fd semantics")
    def test_write_file_never_unlinks_replaced_foreign_inode(
        self, tmp_path, monkeypatch
    ):
        """If the path is swapped for a foreign file between our failed
        write and cleanup, cleanup must lstat-check identity and leave the
        FOREIGN file alone. The still-open original fd keeps the inode
        allocated; write_bytes uses io.open, not our os.write hook."""
        fp = tmp_path / "victim.bin"
        foreign = b"foreign replacement - do not delete\n"
        original = OSError(5, "io error")
        real_write = os.write
        fired = {"done": False}

        def swap_then_fail(fd, data):
            if not fired["done"]:
                fired["done"] = True
                fp.unlink()  # detach our still-open inode
                fp.write_bytes(foreign)  # foreign lands at the same path
                raise original
            return real_write(fd, data)

        monkeypatch.setattr(firnpack.os, "write", swap_then_fail)
        with pytest.raises(OSError) as caught:
            firnpack._write_file(fp, b"abcdefghij")
        monkeypatch.undo()
        assert caught.value is original
        assert fp.is_file() and fp.read_bytes() == foreign


class TestAbortFailClosedOnDamagedStore:
    """merge --abort with the theirs layer file missing fails CLOSED:
    IntegrityError, pending merge kept, zero worktree/DB mutation. The
    escape hatch is repairing the store, not a degraded abort (R1)."""

    def test_missing_theirs_layer_blocks_abort_until_restored(self, project):
        write(project, "shared.txt", "l1\nl2\nl3\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "shared.txt", "l1\nSIDE\nl3\n")
        repo.commit("side")
        repo.switch("main")
        write(project, "shared.txt", "l1\nMAIN\nl3\n")
        repo.commit("main")
        assert repo.merge("side")["status"] == "conflict"
        worktree_before = tree_bytes(project)
        wt_before = dict(repo._worktree())
        theirs = wt_before["merge_theirs"]

        layer = project / ".firn" / "layers" / f"{theirs}.firn"
        layer_bytes = layer.read_bytes()
        layer.unlink()  # damaged store: file gone, DB row remains

        with pytest.raises(IntegrityError):
            repo.merge_abort()
        wt = repo._worktree()
        assert wt["merge_theirs"] == theirs
        assert wt["head_id"] == wt_before["head_id"]
        assert wt["head_branch"] == wt_before["head_branch"]
        assert tree_bytes(project) == worktree_before

        # inspection paths still work on the damaged store
        out = project / ".." / "ours-snapshot"
        repo.export(wt_before["head_id"], str(out))

        # repair the store, then abort succeeds with exact ours bytes
        layer.write_bytes(layer_bytes)
        result = repo.merge_abort()
        assert result["status"] == "aborted"
        assert repo.status()["clean"] is True
        repo.close()
