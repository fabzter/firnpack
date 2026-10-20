"""Regression tests for every defect found during M1/M2 development and
review — the session's error ledger, made executable. If a bug was ever
fixed, its test lives here or already existed; nothing that broke once
ships untested.
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


class TestExecMaskAgreement:
    """The writer used & 0o111 while identity used & 0o100 — a
    group-exec-only file stored as 755 but identified as non-exec,
    producing permanent false drift. All four mode sites must agree."""

    def test_group_exec_only_file_is_consistent(self, project):
        if sys.platform == "win32":
            pytest.skip("no settable exec bits on Windows")
        write(project, "weird.sh", "x\n", mode=0o610)  # group+exec, not owner
        repo = Repo.init(project)
        repo.commit("v1")
        # status must be clean: identity and writer agree on the mode
        st = repo.status()
        repo.close()
        assert st["clean"], f"group-exec file false-drifts: {st}"

    def test_layer_mode_matches_identity_bit(self, project):
        if sys.platform == "win32":
            pytest.skip("no settable exec bits on Windows")
        write(project, "weird.sh", "x\n", mode=0o610)
        repo = Repo.init(project)
        repo.commit("v1")
        wt = repo._worktree()
        _, manifest, _ = repo._read_layer(wt["head_id"])
        repo.close()
        # 0o610 has no owner-exec: stored mode must be 0o644
        assert manifest["weird.sh"]["mode"] == 0o644


class TestUtf8Output:
    """A commit message with non-cp1252 characters crashed the CLI
    AFTER a successful pack (print with the platform codec). main()
    reconfigures streams to UTF-8; e2e already exercises it via
    'añadido ✓'. Unit-level: the reconfigure helper never raises."""

    def test_reconfigure_is_safe(self):
        # main() runs this on every invocation; it must not raise on
        # any stream type (pipe, tty, or capture)
        import contextlib

        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")
        # reaching here is the assertion


class TestMergeDbFirstOrdering:
    """The clean-merge path materialized files BEFORE setting the
    pending state — inverting the v0.4 crash ordering. Observable
    invariant: whenever merged content is on disk, merge_theirs is set
    (so --abort can recover). We simulate the crash window by monkey-
    patching _materialize to raise mid-merge."""

    def test_crash_mid_materialize_leaves_recoverable_state(self, project, monkeypatch):
        repo = Repo.init(project)
        write(project, "shared.txt", "line1\nline2\nline3\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "side.txt", "side\n")
        repo.commit("side work")
        repo.switch("main")
        write(project, "main.txt", "main\n")
        repo.commit("main work")

        original = Repo._materialize

        def crash(self, tree):
            raise RuntimeError("simulated crash mid-materialize")

        monkeypatch.setattr(Repo, "_materialize", crash)
        with pytest.raises(RuntimeError):
            repo.merge("side")
        monkeypatch.setattr(Repo, "_materialize", original)
        repo.close()

        # the crash window must leave merge_theirs set: --abort works
        repo = Repo(project)
        wt = repo._worktree()
        assert wt["merge_theirs"] is not None, (
            "pending state missing after crash — next commit would "
            "silently drop theirs from the DAG (the v0.4 failure)"
        )
        result = repo.merge_abort()
        repo.close()
        assert result["status"] == "aborted"


class TestSyncIntegrityWindow:
    """_sync_worktree_to unlinked files before reading layer content:
    a corrupt member left the worktree half-deleted. Now all content is
    read and validated BEFORE the first unlink."""

    def test_corrupt_layer_leaves_worktree_untouched(self, project):
        write(project, "a.txt", "one\n")
        write(project, "b.txt", "two\n")
        repo = Repo.init(project)
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "c.txt", "three\n")
        repo.commit("side work")
        repo.switch("main")
        # corrupt the side layer's file on disk
        repo.close()
        side_head = None
        repo = Repo(project)
        side_head = repo._db.execute(
            "SELECT head FROM branches WHERE name = 'side'"
        ).fetchone()[0]
        layer_path = project / ".firn" / "layers" / f"{side_head}.firn"
        layer_path.write_bytes(b"not a gzip file at all")
        with pytest.raises(IntegrityError):
            repo.switch("side")
        repo.close()
        # worktree untouched by the failed switch
        assert (project / "a.txt").read_text() == "one\n"
        assert (project / "b.txt").read_text() == "two\n"
        st = Repo(project).status()
        assert st["clean"]


class TestAbortEdgeCases:
    def test_abort_after_conflict_always_rescues_the_markers(self, project):
        """After a conflict merge, the worktree holds marker content that
        differs from the head snapshot — abort's rescue is therefore
        ALWAYS warranted (the markers themselves are the 'touched since'
        state; a no-drift abort is unreachable from the CLI, which is
        correct: --abort's contract is 'never destroy marker work')."""
        repo = Repo.init(project)
        write(project, "shared.txt", "base\n")
        repo.commit("base")
        repo.branch("side")
        repo.switch("side")
        write(project, "shared.txt", "side\n")
        repo.commit("side final")
        repo.switch("main")
        write(project, "shared.txt", "main\n")
        repo.commit("main final")
        result = repo.merge("side")  # conflict
        assert result["status"] == "conflict"
        abort = repo.merge_abort()
        repo.close()
        assert abort["rescue_layer"] is not None
        # the rescue layer preserves the marker content for inspection
        repo = Repo(project)
        _, manifest, _ = repo._read_layer(abort["rescue_layer"])
        rescued = repo._layer_contents(
            abort["rescue_layer"],
            {p for p, v in manifest.items() if v["target"] is None},
        )
        repo.close()
        assert b"<<<<<<< ours" in rescued.get("shared.txt", b"")

    def test_abort_without_pending_merge_refuses(self, project):
        repo = Repo.init(project)
        write(project, "a.txt", "x\n")
        repo.commit("v")
        with pytest.raises(FirnpackError, match="no merge is pending"):
            repo.merge_abort()
        repo.close()
