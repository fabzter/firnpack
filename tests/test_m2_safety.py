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
    """n sequential commits on main, returns layer ids in commit order."""
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
        ids = _chain(repo, 3)  # commit order: [c0, c1, c2]
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

    @pytest.mark.parametrize("keep", [0, -1])
    @pytest.mark.parametrize("flags", [{}, {"yes": True}, {"dry_run": True}])
    def test_compact_keep_below_one_refuses_and_changes_nothing(
        self, project, keep, flags
    ):
        """`--keep` is the number of layers to keep above the foundation, so
        0 (or less) would mean "keep nothing", which no command promises:
        refuse in every mode, exit-1 style, with the layers directory
        byte-identical (docs/usage.md)."""
        repo = self._repo_with_chain(project)
        layers = project / ".firn" / "layers"
        before = {p.name: p.read_bytes() for p in layers.iterdir()}
        with pytest.raises(FirnpackError, match="--keep must be >= 1"):
            repo.compact(keep, **flags)
        repo.close()
        assert {p.name: p.read_bytes() for p in layers.iterdir()} == before

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

    def test_compact_foundation_holds_the_cut_layers_bytes(self, project):
        """The foundation must be a byte-exact copy of the cut layer's
        tree. It used to be written from the WORKTREE (which sits at
        HEAD): same-named files got HEAD's bytes — silent corruption of
        the oldest history, invisible to verify — and a file present in
        the cut but gone from the worktree crashed compact outright."""
        repo = Repo.init(project)
        write(project, "old_only.txt", "only in early history\n")
        write(project, "a.txt", "v1\n")
        repo.commit("c1")
        write(project, "a.txt", "v2 longer content\n")
        repo.commit("c2")
        cut_id = repo._worktree()["head_id"]
        cut_files = {
            p: repo._layer_contents(cut_id, {p})[p]
            for p, v in repo._read_layer(cut_id)[1].items()
            if v["target"] is None
        }
        (project / "old_only.txt").unlink()
        write(project, "b.txt", "new b\n")
        repo.commit("c3")
        write(project, "a.txt", "v4 final\n")
        repo.commit("c4")
        result = repo.compact(2, yes=True)
        assert result["status"] == "compacted"
        root_id = repo._db.execute(
            "SELECT id FROM layers WHERE parent IS NULL"
        ).fetchone()[0]
        _, manifest, _ = repo._read_layer(root_id)
        got = {
            p: repo._layer_contents(root_id, {p})[p]
            for p, v in manifest.items()
            if v["target"] is None
        }
        repo.close()
        assert got == cut_files


def _fk_check(repo):
    return repo._db.execute("PRAGMA foreign_key_check").fetchall()


def _delete_branch(repo, name):
    with repo._db:
        repo._db.execute("DELETE FROM branches WHERE name = ?", (name,))


class TestCompactRefs:
    """Reference-based compact safety: the guard must see ANCESTRY, not
    just branch heads — every survivor of the rewrite that points into
    the deleted set is remapped, refused, or garbage-collected, and the
    final DB state passes PRAGMA foreign_key_check.
    """

    def test_refuses_offchain_branch_ancestry(self, project):
        """A branch cut from inside the COLLAPSED range, then committed
        on: its head is outside collapsed but its parent is inside —
        that history is genuinely squashed away, so the refusal names
        the branch (the old head-membership guard missed this and died
        on a raw IntegrityError)."""
        repo = Repo.init(project)
        ids = _chain(repo, 5)  # commit order: ids[1] = c1, collapsed at keep 2
        repo.branch("feature", ids[1][:10])
        repo.switch("feature")
        write(project, "f99.txt", "feature work\n")
        repo.commit("feature commit")
        repo.switch("main")
        with pytest.raises(FirnpackError, match="feature.*descends"):
            repo.compact(2, yes=True)
        # repo fully intact
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        assert repo.history()["count"] == 5
        repo.close()

    def test_remaps_branch_at_kept_head(self, project):
        """A branch sitting exactly at main's HEAD (an old kept id) is
        repointed to the rewritten id — not refused, not dangling —
        and the remap is reported, never silent."""
        repo = Repo.init(project)
        ids = _chain(repo, 4)
        repo.branch("feature")
        plan = repo.compact(2, dry_run=True)
        assert plan["branches_remapped"] == [{"name": "feature", "from": ids[3]}]
        result = repo.compact(2, yes=True)
        assert result["branches_remapped"] == [
            {"name": "feature", "from": ids[3], "to": result["new_head"]}
        ]
        fh = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'feature'"
        ).fetchone()[0]
        mh = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'main'"
        ).fetchone()[0]
        assert fh == mh == result["new_head"]
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_ff_merge_leaves_no_stale_pin(self, project):
        """head_pin is pin-only: a fast-forward on a branch worktree
        must not leave one behind, or a later legal compact trips the
        pin refusal spuriously."""
        repo = Repo.init(project)
        _chain(repo, 2)
        repo.branch("feature")
        repo.switch("feature")
        write(project, "f10.txt", "feature\n")
        repo.commit("feature commit")
        repo.switch("main")
        m = repo.merge("feature")
        assert m["status"] == "fast_forwarded"
        pin = repo._db.execute("SELECT head_pin FROM worktree WHERE id = 1").fetchone()[
            0
        ]
        assert pin is None
        _delete_branch(repo, "feature")
        write(project, "f20.txt", "more\n")
        repo.commit("after ff")
        result = repo.compact(1, yes=True)
        assert result["status"] == "compacted"
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_gcs_collapsed_merge_stub(self, project):
        """When the merge layer itself is collapsed and its branch is
        gone, the second-parent stub is unreachable garbage: collected
        (row + file), archived, listed in the plan."""
        repo = Repo.init(project)
        write(project, "f0.txt", "zero\n")
        repo.commit("c0")
        write(project, "f1.txt", "one\n")
        repo.commit("c1")
        repo.branch("feature")
        repo.switch("feature")
        write(project, "feat.txt", "feature work\n")
        repo.commit("feature commit")
        stub_id = repo._worktree()["head_id"]
        repo.switch("main")
        write(project, "f2.txt", "main moves\n")
        repo.commit("c2")  # diverge -> real merge, not FF
        m = repo.merge("feature")
        assert m["status"] == "merged"
        write(project, "f3.txt", "after merge\n")
        repo.commit("c3")
        _delete_branch(repo, "feature")
        plan = repo.compact(1, dry_run=True)
        assert plan["layers_garbage_collected"] == 1
        arc = str(project / "archive")
        result = repo.compact(1, yes=True, archive_dir=arc)
        assert result["garbage_collected"] == 1
        rows = repo._db.execute(
            "SELECT count(*) FROM layers WHERE id = ?", (stub_id,)
        ).fetchone()[0]
        assert rows == 0
        assert not (project / ".firn" / "layers" / f"{stub_id}.firn").exists()
        import zipfile

        with zipfile.ZipFile(result["archive"]) as zf:
            assert f"{stub_id}.firn" in zf.namelist()
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_refuses_kept_merge_stub_diving_into_collapsed(self, project):
        """A KEPT merge layer keeps referencing its second parent; if
        that stub's ancestry dives into the collapsed range, deleting
        those layers would orphan a live reference — refuse."""
        repo = Repo.init(project)
        write(project, "f0.txt", "zero\n")
        repo.commit("c0")
        write(project, "f1.txt", "one\n")
        repo.commit("c1")
        repo.branch("feature")
        repo.switch("feature")
        write(project, "feat.txt", "feature work\n")
        repo.commit("feature commit")
        repo.switch("main")
        write(project, "f2.txt", "main moves\n")
        repo.commit("c2")
        m = repo.merge("feature")
        assert m["status"] == "merged"
        _delete_branch(repo, "feature")
        with pytest.raises(FirnpackError, match="still referenced"):
            repo.compact(1, yes=True)
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_gcs_amend_orphan(self, project):
        """Amend leaves the replaced head as an unreachable row (its
        file stays, write-once). Compact must collect it when its
        parent is rewritten away — the disk-reclaim promise covers
        amend orphans too."""
        repo = Repo.init(project)
        ids = _chain(repo, 3)
        old_head = ids[2]  # the head amend replaces (commit order)
        repo.amend(message="c2 amended")
        result = repo.compact(1, yes=True)
        assert result["garbage_collected"] == 1
        rows = repo._db.execute(
            "SELECT count(*) FROM layers WHERE id = ?", (old_head,)
        ).fetchone()[0]
        assert rows == 0
        assert not (project / ".firn" / "layers" / f"{old_head}.firn").exists()
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_rewrites_fork_on_kept_head(self, project):
        """Owner item (a): a working branch forked from main's HEAD is
        the common case — compact must not cliff on it. The fork layer
        is rewritten onto the new head (same tree, message, author,
        timestamps), the branch remaps, and the plan/result report it."""
        repo = Repo.init(project)
        _chain(repo, 5)
        repo.branch("feature")
        repo.switch("feature")
        write(project, "f99.txt", "feature work\n")
        fork = repo.commit("feature commit")["layer_id"]
        repo.switch("main")
        plan = repo.compact(1, dry_run=True)
        assert plan["off_chain_rewritten"] == 1
        assert plan["branches_remapped"] == [{"name": "feature", "from": fork}]
        result = repo.compact(1, yes=True)
        assert result["off_chain_rewritten"] == 1
        assert result["garbage_collected"] == 0  # forks rewrite, not collect
        remap = result["branches_remapped"]
        assert remap == [{"name": "feature", "from": fork, "to": remap[0]["to"]}]
        fh = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'feature'"
        ).fetchone()[0]
        assert fh == remap[0]["to"]
        parent = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?", (fh,)
        ).fetchone()[0]
        assert parent == result["new_head"]  # sits on the rewritten head
        # content survived the rewrite
        _, manifest, _ = repo._read_layer(fh)
        assert "f99.txt" in manifest
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_rewrites_stacked_forks_in_order(self, project):
        """Stacked branches (branch2 on branch1's commit, both off the
        kept chain) rewrite in topological order: branch2's new parent
        is branch1's new head, and both remap."""
        repo = Repo.init(project)
        _chain(repo, 4)
        repo.branch("one")
        repo.switch("one")
        write(project, "one.txt", "one work\n")  # clean commits no-op
        b1 = repo.commit("one work")["layer_id"]
        repo.branch("two")
        repo.switch("two")
        write(project, "two.txt", "two work\n")
        b2 = repo.commit("two work")["layer_id"]
        repo.switch("main")
        result = repo.compact(1, yes=True)
        assert result["off_chain_rewritten"] == 2
        assert result["garbage_collected"] == 0  # forks rewrite, not collect
        remapped = {b["name"]: b for b in result["branches_remapped"]}
        assert set(remapped) == {"one", "two"}
        assert remapped["one"]["from"] == b1
        assert remapped["two"]["from"] == b2
        two_head = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'two'"
        ).fetchone()[0]
        one_head = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'one'"
        ).fetchone()[0]
        parent = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?", (two_head,)
        ).fetchone()[0]
        assert parent == one_head  # stacked parentage preserved
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    def test_gcs_orphan_on_rewritten_fork(self, project):
        """Amend on a fork: fork H1, commit H2 on it, amend H2 — the
        replaced H2 is an unreachable row whose parent (H1) is itself
        rewritten away. Compact rewrites H1 and the amended head, and
        collects exactly the orphan: old H1 and old H2 rows and files
        all vanish, nothing reachable is touched."""
        repo = Repo.init(project)
        _chain(repo, 4)
        repo.branch("feature")
        repo.switch("feature")
        write(project, "h1.txt", "fork one\n")
        h1 = repo.commit("fork one")["layer_id"]
        write(project, "h2.txt", "fork two\n")
        h2 = repo.commit("fork two")["layer_id"]
        repo.amend(message="fork two, amended")
        repo.switch("main")
        result = repo.compact(1, yes=True)
        assert result["off_chain_rewritten"] == 2  # H1 + amended head
        assert result["garbage_collected"] == 1  # the replaced H2 only
        for gone in (h1, h2):
            rows = repo._db.execute(
                "SELECT count(*) FROM layers WHERE id = ?", (gone,)
            ).fetchone()[0]
            assert rows == 0
            assert not (project / ".firn" / "layers" / f"{gone}.firn").exists()
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        repo.close()

    @pytest.mark.parametrize("delete_feature", [True, False])
    def test_compact_rewrites_kept_merge_stub_before_the_merge(
        self, project, delete_feature
    ):
        """A kept merge layer references its second parent, an off-chain
        survivor that is itself rewritten. The survivor must be placed
        BEFORE the merge: otherwise the rewritten merge points at an id
        about to be deleted and the swap dies on FOREIGN KEY at COMMIT
        (adversarial review: keep=6 over a merge-bearing chain)."""
        repo = Repo.init(project)
        write(project, "c0.txt", "0\n")
        repo.commit("c0")
        write(project, "c1.txt", "1\n")
        repo.commit("c1")
        repo.branch("feature")
        repo.switch("feature")
        write(project, "x1.txt", "x\n")
        repo.commit("x1")
        repo.switch("main")
        write(project, "c2.txt", "2\n")
        repo.commit("c2")
        assert repo.merge("feature")["status"] == "merged"
        for i in (3, 4, 5):
            write(project, f"c{i}.txt", f"{i}\n")
            repo.commit(f"c{i}")
        if delete_feature:
            _delete_branch(repo, "feature")
        names = [n for (n,) in repo._db.execute("SELECT name FROM branches")]
        before = {
            n: repo._read_layer(
                repo._db.execute(
                    "SELECT head FROM branches WHERE name = ?", (n,)
                ).fetchone()[0]
            )[1]
            for n in names
        }
        # chain: c5 c4 c3 merge c2 c1 c0 -> keep 6 puts the cut on c0
        result = repo.compact(6, yes=True)
        assert result["status"] == "compacted"
        assert _fk_check(repo) == []
        assert repo.verify()["status"] == "ok"
        rows = repo._db.execute("SELECT count(*) FROM layers").fetchone()[0]
        files = len(list((project / ".firn" / "layers").glob("*.firn")))
        assert files == rows
        for n in names:
            head = repo._db.execute(
                "SELECT head FROM branches WHERE name = ?", (n,)
            ).fetchone()[0]
            assert repo._read_layer(head)[1] == before[n]
        if not delete_feature:
            merge_second = repo._db.execute(
                "SELECT second_parent FROM layers WHERE message = 'merge feature'"
            ).fetchone()[0]
            feature_head = repo._db.execute(
                "SELECT head FROM branches WHERE name = 'feature'"
            ).fetchone()[0]
            assert merge_second == feature_head  # one rewritten stub, shared
        repo.close()

    def test_compact_failure_leaves_no_orphan_layer_files(self, project, monkeypatch):
        """New layer files are written before the swap transaction; a
        failure anywhere after the first write must not strand them
        (verify would then report orphan files and nothing sweeps them)."""
        repo = Repo.init(project)
        _chain(repo, 5)
        layers = project / ".firn" / "layers"
        files_before = sorted(p.name for p in layers.glob("*.firn"))
        real = Repo._rewrite_layer_from
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk exploded")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Repo, "_rewrite_layer_from", flaky)
        with pytest.raises(RuntimeError, match="disk exploded"):
            repo.compact(3, yes=True)
        assert sorted(p.name for p in layers.glob("*.firn")) == files_before
        assert repo.verify()["status"] == "ok"
        repo.close()
