"""M2 merge tests — SPEC §10/§11 contract.

The load-bearing one: conflict -> resolve -> commit MUST produce a
two-parent layer (second_parent == theirs head) — the v0.4 blocker
regression test. Plus: drift guard, pending-merge guards, fast-forward,
already-up-to-date, clean merge, abort with restore, marker guard.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firnpack import DriftError, FirnpackError, Repo  # noqa: E402


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


def _diverge(repo, project):
    """base commit on main, then divergent commits on main + side."""
    write(project, "shared.txt", "line1\nline2\nline3\n")
    base = repo.commit("base")
    repo.branch("side")
    # diverge on side
    repo.switch("side")
    write(project, "side.txt", "side work\n")
    write(project, "shared.txt", "line1\nside-change\nline3\n")
    repo.commit("side work")
    # back on main, diverge differently
    repo.switch("main")
    write(project, "main.txt", "main work\n")
    repo.commit("main work")
    return base


class TestCleanMerge:
    def test_non_overlapping_changes_merge(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        result = repo.merge("side")
        repo.close()
        assert result["status"] == "merged"
        layer_id = result["layer_id"]
        row = (
            repo._db.execute(
                "SELECT parent, second_parent FROM layers WHERE id = ?",
                (layer_id,),
            ).fetchone()
            if False
            else None
        )
        repo = Repo(project)
        row = repo._db.execute(
            "SELECT parent, second_parent, message FROM layers WHERE id = ?",
            (layer_id,),
        ).fetchone()
        repo.close()
        assert row[1] is not None, "clean merge lost second_parent"
        assert "side" in row[2]
        # both sides' files present in the worktree
        assert (project / "side.txt").exists()
        assert (project / "main.txt").exists()
        assert (project / "shared.txt").read_text() == ("line1\nside-change\nline3\n")

    def test_merge_commits_show_two_parents_in_history(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.merge("side")
        repo.close()
        repo = Repo(project)
        h = repo.history()
        repo.close()
        merge_commit = h["commits"][0]
        assert merge_commit["second_parent"] is not None
        assert merge_commit["parent"] != merge_commit["second_parent"]

    def test_already_up_to_date(self, project):
        repo = Repo.init(project)
        write(project, "a.txt", "1\n")
        repo.commit("one")
        repo.branch("side")  # side == main head
        result = repo.merge("side")
        repo.close()
        assert result["status"] == "already_up_to_date"


class TestFastForward:
    def test_main_behind_side_fast_forwards(self, project):
        repo = Repo.init(project)
        write(project, "a.txt", "1\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "a.txt", "side version\n")
        ahead = repo.commit("side ahead")
        repo.switch("main")
        result = repo.merge("side")
        repo.close()
        assert result["status"] == "fast_forwarded"
        assert result["head"] == ahead["layer_id"]
        repo = Repo(project)
        row = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'main'"
        ).fetchone()
        repo.close()
        assert row[0] == ahead["layer_id"]
        assert (project / "a.txt").read_text() == "side version\n"


class TestConflictPath:
    def test_conflict_sets_pending_and_markers(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        # make a real conflict: both sides change the SAME line
        repo.switch("side")
        write(project, "shared.txt", "conflict-side\n")
        repo.commit("side conflict")
        repo.switch("main")
        write(project, "shared.txt", "conflict-main\n")
        repo.commit("main conflict")
        result = repo.merge("side")
        repo.close()
        assert result["status"] == "conflict"
        assert result["conflicts"] == ["shared.txt"]
        content = (project / "shared.txt").read_text()
        assert "<<<<<<< ours" in content
        assert "=======" in content
        assert ">>>>>>> theirs" in content
        assert "conflict-main" in content
        assert "conflict-side" in content
        repo = Repo(project)
        wt = repo._worktree()
        assert wt["merge_theirs"] is not None
        repo.close()

    def test_resolve_then_commit_yields_two_parents(self, project):
        """THE v0.4 blocker regression: conflict -> resolve -> commit
        must record second_parent == theirs head. Without the pending
        state, theirs-head silently vanished from the DAG."""
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side wins\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main wins\n")
        repo.commit("main final")
        theirs_id = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'side'"
        ).fetchone()[0]
        result = repo.merge("side")
        assert result["status"] == "conflict"
        # resolve: pick side's version
        write(project, "shared.txt", "side wins\n")
        resolved = repo.commit("resolved: take side")
        repo.close()
        assert resolved["status"] == "packed"
        repo = Repo(project)
        row = repo._db.execute(
            "SELECT parent, second_parent FROM layers WHERE id = ?",
            (resolved["layer_id"],),
        ).fetchone()
        wt = repo._worktree()
        repo.close()
        assert row[1] == theirs_id, "second_parent lost — the v0.4 blocker regressed"
        assert wt["merge_theirs"] is None  # consumed

    def test_marker_guard_blocks_conflicted_commit(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side wins\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main wins\n")
        repo.commit("main final")
        result = repo.merge("side")  # conflict, markers on disk
        assert result["status"] == "conflict"
        with pytest.raises(FirnpackError, match="merge conflict"):
            repo.commit("forgot to resolve")
        repo.close()
        # the block left no layer behind
        repo = Repo(project)
        count = repo._db.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
        repo.close()
        assert count == 5  # base, side work, main work, side final, main final

    def test_merge_abort_restores_pre_merge(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side wins\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main wins\n")
        repo.commit("main final")
        repo.merge("side")  # conflict
        result = repo.merge_abort()
        repo.close()
        assert result["status"] == "aborted"
        assert (project / "shared.txt").read_text() == "main wins\n"
        assert "<<<<<<<" not in (project / "shared.txt").read_text()
        repo = Repo(project)
        wt = repo._worktree()
        st = repo.status()
        repo.close()
        assert wt["merge_theirs"] is None
        assert st["clean"]

    def test_abort_rescues_uncommitted_resolution_work(self, project):
        """The v0.4-family blocker: abort must not silently destroy
        resolution work. Resolve file A, edit file B, abort — B's edit
        must survive in a rescue layer (SPEC 9.7 semantics)."""
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side wins\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main wins\n")
        repo.commit("main final")
        result = repo.merge("side")
        assert result["status"] == "conflict"
        # resolve the conflict in shared.txt ...
        write(project, "shared.txt", "resolved compromise\n")
        # ... and make an unrelated edit the user expects to keep
        write(project, "main.txt", "main work\nPRECIOUS EDIT\n")
        abort = repo.merge_abort()
        repo.close()
        assert abort["status"] == "aborted"
        assert abort["rescue_layer"] is not None
        # the worktree is restored to main's head ...
        assert (project / "shared.txt").read_text() == "main wins\n"
        assert (
            project / "main.txt"
        ).read_text() == "main work\n"  # NOT the precious edit
        # ... but the edit survives in the rescue layer
        repo = Repo(project)
        _, manifest, _ = repo._read_layer(abort["rescue_layer"])
        rescued = repo._layer_contents(
            abort["rescue_layer"],
            {p for p, v in manifest.items() if v["target"] is None},
        )
        row = repo._db.execute(
            "SELECT message, second_parent FROM layers WHERE id = ?",
            (abort["rescue_layer"],),
        ).fetchone()
        repo.close()
        assert b"PRECIOUS EDIT" in rescued.get("main.txt", b"")
        assert "rescue before merge --abort" in row[0]
        assert row[1] is None  # plain single-parent layer, not a merge


class TestMergeGuards:
    def test_drift_guard_blocks_merge(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        write(project, "uncommitted.txt", "drift\n")
        with pytest.raises(DriftError, match="drift"):
            repo.merge("side")
        repo.close()
        # nothing happened
        assert (project / "uncommitted.txt").exists()

    def test_second_merge_refused_while_pending(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main\n")
        repo.commit("main final")
        repo.merge("side")  # pending now
        with pytest.raises(FirnpackError, match="already pending"):
            repo.merge("side")
        repo.close()

    def test_switch_refused_while_pending(self, project):
        repo = Repo.init(project)
        _diverge(repo, project)
        repo.switch("side")
        write(project, "shared.txt", "side\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main\n")
        repo.commit("main final")
        repo.merge("side")
        with pytest.raises(FirnpackError, match="merge is pending"):
            repo.switch("side")
        repo.close()

    def test_delete_modify_keeps_modified_and_reports(self, project):
        repo = Repo.init(project)
        write(project, "keep.txt", "original\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        (project / "keep.txt").unlink()
        repo.commit("side deletes")
        repo.switch("main")
        write(project, "keep.txt", "modified on main\n")
        repo.commit("main modifies")
        result = repo.merge("side")
        repo.close()
        # owner decision Q7: modified content kept, reported as conflict
        assert result["status"] == "conflict"
        assert "keep.txt" in result["conflicts"]
        assert (project / "keep.txt").read_text() == "modified on main\n"


class TestMerge3Text:
    """Table tests for the merge kernel's conflict rule (git-style
    adjacency) — every silent-corruption case found in review, pinned:
    same-spot inserts must conflict, identical inserts must dedupe,
    insert-vs-replace must conflict, and far-apart edits must merge."""

    @pytest.mark.parametrize(
        "base,ours,theirs,expected",
        [
            # different inserts at the same spot: conflict (both lines
            # silently interleaving was corruption)
            ("A\nB\n", "A\nX\nB\n", "A\nY\nB\n", None),
            # identical inserts: dedupe — the line appears ONCE
            ("A\nB\n", "A\nX\nB\n", "A\nX\nB\n", "A\nX\nB\n"),
            # ours inserts before line 1, theirs replaces line 1:
            # touching -> conflict (dropping ours' X was corruption)
            ("b\nc\n", "X\nb\nc\n", "B\nc\n", None),
            # far-apart edits: clean merge
            (
                "1\n2\n3\n4\n5\n",
                "one\n2\n3\n4\n5\n",
                "1\n2\n3\n4\nfive\n",
                "one\n2\n3\n4\nfive\n",
            ),
            # adjacent replacements (1 vs 2, 2 vs 3) — touching but
            # non-overlapping ranges: git conflicts on adjacency
            ("1\n2\n3\n", "a\n2\n3\n", "1\nb\n3\n", "a\nb\n3\n"),
            # identical replacement on both sides: dedupe to one
            ("1\n2\n3\n", "1\nX\n3\n", "1\nX\n3\n", "1\nX\n3\n"),
        ],
        ids=[
            "same-spot-inserts-conflict",
            "identical-inserts-dedupe",
            "insert-vs-replace-conflict",
            "far-apart-merges",
            "adjacent-lines-merge",
            "identical-replacement-dedupe",
        ],
    )
    def test_table(self, base, ours, theirs, expected):
        result = firnpack_merge3(base, ours, theirs)
        assert result == expected

    def test_nonconflicting_merge_equals_both_diffs_applied(self):
        """Property: a None (non-conflicting) merge must equal base with
        both sides' diffs applied — no lines lost, no duplicates beyond
        dedupe."""
        import random

        random.seed(42)
        for _case in range(50):
            base_lines = [f"l{i}\n" for i in range(8)]
            ours_lines = list(base_lines)
            theirs_lines = list(base_lines)
            # ours edits even indices, theirs odd indices — disjoint
            for i in range(0, 8, 2):
                ours_lines[i] = f"O{i}\n"
            for i in range(1, 8, 2):
                theirs_lines[i] = f"T{i}\n"
            base = "".join(base_lines)
            ours = "".join(ours_lines)
            theirs = "".join(theirs_lines)
            result = firnpack_merge3(base, ours, theirs)
            expected = "".join(f"O{i}\n" if i % 2 == 0 else f"T{i}\n" for i in range(8))
            assert result == expected, f"property violated for {base!r}"


def firnpack_merge3(base, ours, theirs):
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import firnpack as _fp

    return _fp._merge3_text("t", base, ours, theirs)
