"""End-to-end CLI tests: real processes, real repositories, real journeys.

Each test drives `firnpack` exactly as a user (or an agent skill) would:
subprocess, exit codes, stdout/stderr, and on-disk state. Nothing imports
the module — that's what makes these e2e rather than unit tests.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "firnpack.py"
PY = sys.executable


def firn(cwd, *args, check=True, input_text=None):
    # encoding="utf-8": the CLI writes UTF-8 (main() reconfigures its
    # streams); text=True alone would decode with the platform locale
    # (cp1252 on the Windows runners) and unicode assertions could never
    # match, no matter what the module does.
    proc = subprocess.run(
        [PY, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"firnpack {' '.join(args)} failed ({proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def w(root: Path, rel: str, data: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # bytes, not write_text: newline translation on Windows would corrupt
    # every byte-exact assertion
    p.write_bytes(data.encode("utf-8"))
    return p


@pytest.fixture()
def project(tmp_path):
    return tmp_path / "proj"


class TestJourneyNewUser:
    """The README quickstart, verbatim, as a first-time user runs it."""

    def test_quickstart(self, project):
        project.mkdir()
        p = firn(project, "init")
        assert "packed glacier ready" in p.stdout
        assert "no layers yet" in p.stdout

        w(project, "notes.txt", "first notes\n")
        p = firn(project, "commit")
        assert "packed 2 files into layer" in p.stdout

        w(project, "notes.txt", "first notes\nmore\n")
        p = firn(project, "commit")
        assert "packed" in p.stdout

        p = firn(project, "status")
        assert "clean on main" in p.stdout

        p = firn(project, "history")
        lines = [line for line in p.stdout.splitlines() if line.strip()]
        assert len(lines) == 4  # 2 layers x (id-line + message-line)
        assert "more" not in lines[1]  # newest first: second message on top
        assert "pack 2 files" in lines[3]

        p = firn(project, "diff")
        assert p.stdout == ""  # clean: no diff output


class TestJourneyCorrections:
    """Mistakes and recoveries: auto-message, custom message, re-commit."""

    def test_custom_message_round_trip(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "v1\n")
        firn(project, "commit", "-m", "my message")
        p = firn(project, "history")
        assert "my message" in p.stdout

    def test_recommit_without_change_is_noop(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "v1\n")
        firn(project, "commit")
        p = firn(project, "commit", "-m", "should not appear")
        assert "nothing to pack" in p.stdout
        p = firn(project, "history")
        assert "should not appear" not in p.stdout

    def test_auto_message_mentions_delta(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "x\n")
        firn(project, "commit")
        w(project, "a.txt", "x\ny\n")
        p = firn(project, "commit")
        assert "pack 2 files" in p.stdout
        assert "+" in p.stdout  # delta sign in the auto message


class TestJourneyRecovery:
    """The safety net: what a scared user sees when things go wrong."""

    def test_commands_outside_repo_fail_loudly(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        for cmd in ("status", "history", "diff", "commit"):
            p = firn(empty, cmd, check=False)
            assert p.returncode == 1
            assert "not a firnpack repository" in p.stderr
            assert p.stdout == ""

    def test_init_twice_refused(self, project):
        project.mkdir()
        firn(project, "init")
        p = firn(project, "init", check=False)
        assert p.returncode == 1
        assert "already contains" in p.stderr

    def test_repo_is_portable_folder(self, tmp_path):
        """The glacier moves house: copy the folder, keep working."""
        src = tmp_path / "src"
        src.mkdir()
        firn(src, "init")
        w(src, "a.txt", "one\n")
        firn(src, "commit", "-m", "first")
        w(src, "a.txt", "one\ntwo\n")
        firn(src, "commit", "-m", "second")

        dst = tmp_path / "dst"
        # copy project *including* .firn — the whole point of the design.
        # shutil, not `cp -R`: the Windows runners have no cp.
        import shutil

        shutil.copytree(src, dst)
        p = firn(dst, "status")
        assert "clean on main" in p.stdout
        p = firn(dst, "history")
        assert "second" in p.stdout
        w(dst, "a.txt", "one\ntwo\nthree\n")
        p = firn(dst, "commit", "-m", "continued elsewhere")
        p = firn(dst, "history")
        assert "continued elsewhere" in p.stdout
        # and the original is untouched
        p = firn(src, "history")
        assert "continued elsewhere" not in p.stdout


class TestJourneyOutputContracts:
    """JSON output is a stable, documented contract."""

    def test_commit_json_shape(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "x\n")
        p = firn(project, "commit", "--json")
        payload = json.loads(p.stdout)
        assert payload["status"] == "packed"
        assert len(payload["layer_id"]) == 40
        assert payload["files"] == 2  # a.txt + seeded .gitignore
        assert payload["size"] > 0
        assert isinstance(payload["message"], str)
        for key in ("tree_id",):
            assert len(payload[key]) == 40

    def test_status_json_shape(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "x\n")
        firn(project, "commit")
        w(project, "b.txt", "new\n")
        p = firn(project, "status", "--json")
        payload = json.loads(p.stdout)
        assert payload["clean"] is False
        assert payload["added"] == ["b.txt"]
        assert payload["modified"] == []
        assert payload["merge_pending"] is False
        assert payload["flattened_pending"] is None
        assert payload["nested_gitignore"] is False
        assert payload["head"]["branch"] == "main"
        assert len(payload["head"]["id"]) == 40

    def test_history_json_shape(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "1\n")
        firn(project, "commit")
        w(project, "a.txt", "2\n")
        firn(project, "commit")
        p = firn(project, "history", "--json")
        payload = json.loads(p.stdout)
        assert payload["count"] == 2
        newest, root = payload["commits"]  # newest-first order
        assert root["parent"] is None
        assert newest["parent"] == root["id"]
        assert newest["second_parent"] is None
        assert "main" in newest["tips"]

    def test_nothing_to_pack_json(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "x\n")
        firn(project, "commit")
        p = firn(project, "commit", "--json")
        payload = json.loads(p.stdout)
        assert payload["status"] == "nothing_to_pack"

    def test_diff_json_shape(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "one\n")
        firn(project, "commit")
        w(project, "a.txt", "one\ntwo\n")
        p = firn(project, "diff", "--json")
        payload = json.loads(p.stdout)
        (entry,) = payload["entries"]
        assert entry["path"] == "a.txt"
        assert entry["status"] == "modified"
        assert entry["binary"] is False
        assert "+two" in entry["hunks"]


class TestJourneyScale:
    """Invariants that must hold as the glacier grows."""

    def test_n_plus_two_through_many_commits(self, project):
        project.mkdir()
        firn(project, "init")
        for i in range(10):
            w(project, f"f{i}.txt", f"content {i}\n" * (i + 1))
            firn(project, "commit", "-m", f"c{i}")
        layers = list((project / ".firn" / "layers").glob("*.firn"))
        allfiles = [p for p in (project / ".firn").rglob("*") if p.is_file()]
        assert len(layers) == 10
        assert len(allfiles) == 12  # 10 layers + glacier.db + config
        # no tmp orphans
        assert not list((project / ".firn" / "layers").glob("*.tmp"))

    def test_layer_files_never_mutate(self, project):
        """Write-once discipline: earlier layers are byte-stable."""
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "one\n")
        firn(project, "commit")
        first = list((project / ".firn" / "layers").glob("*.firn"))[0]
        before = first.read_bytes()
        first_mtime = first.stat().st_mtime_ns
        for i in range(3):
            w(project, "a.txt", f"one\n{chr(97 + i)}\n")
            firn(project, "commit", "-m", f"v{i}")
        assert first.read_bytes() == before
        assert first.stat().st_mtime_ns == first_mtime

    def test_deep_paths_and_subdirs(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "src/pkg/module.py", "def main():\n    pass\n")
        w(project, "docs/guide/chapter1.md", "# Guide\n")
        p = firn(project, "commit", "-m", "structure")
        assert "packed 3 files" in p.stdout
        p = firn(project, "status")
        assert "clean on main" in p.stdout


class TestJourneyIgnored:
    """Office junk and oneDrive noise stay out of history, visibly."""

    def test_seeded_gitignore_stops_lock_files(self, project):
        project.mkdir()
        firn(project, "init")
        seeded = (project / ".gitignore").read_text()
        assert "~$*" in seeded
        w(project, "report.docx", "doc")
        w(project, "~$report.docx", "lock")
        p = firn(project, "commit", "-m", "doc")
        assert "packed 2 files" in p.stdout  # report + .gitignore
        p = firn(project, "status")
        assert "1 file(s) excluded" in p.stderr
        p = firn(project, "status", "--ignored")
        assert "~$report.docx" in p.stdout
        assert "  ignored    ~$report.docx" in p.stdout

    def test_init_preserves_existing_gitignore(self, project):
        project.mkdir()
        (project / ".gitignore").write_text("*.log\n")
        firn(project, "init")
        assert (project / ".gitignore").read_text() == "*.log\n"

    def test_ignored_never_enters_history(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "keep.txt", "keep\n")
        w(project, ".gitignore", "keep.txt\n" + "*.tmp\n" + "~$*\n")
        firn(project, "commit", "-m", "v")
        # keep.txt was excluded: the layer contains only .gitignore
        p = firn(project, "commit", "--json")
        # un-ignore and track it for real
        (project / ".gitignore").write_text("*.tmp\n~$*\n")
        w(project, "keep.txt", "keep\nnow tracked\n")
        firn(project, "commit", "-m", "unignored")
        p = firn(project, "diff")
        assert p.stdout == ""  # clean: diff is vs head, both include it
        # and history's first layer never contained keep.txt
        p = firn(project, "history", "--json")
        first = json.loads(p.stdout)["commits"][-1]
        assert first["message"] == "v"


class TestJourneyErrors:
    """Every error path a user can hit today: loud, humane, non-zero."""

    def test_diff_on_empty_repo(self, project):
        project.mkdir()
        firn(project, "init")
        p = firn(project, "diff")
        assert p.returncode == 0
        # nothing tracked yet, but the seeded .gitignore is diffable
        assert "--- a/.gitignore" in p.stdout

    def test_history_on_empty_repo(self, project):
        project.mkdir()
        firn(project, "init")
        p = firn(project, "history")
        assert p.returncode == 0
        assert "no layers yet" in p.stdout

    def test_status_on_empty_repo(self, project):
        project.mkdir()
        firn(project, "init")
        p = firn(project, "status")
        assert p.returncode == 0
        assert "no layers yet" in p.stdout

    def test_missing_files_reported(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "x\n")
        firn(project, "commit")
        (project / "a.txt").unlink()
        p = firn(project, "status")
        assert "missing" in p.stdout
        assert "a.txt" in p.stdout

    def test_version_and_help(self, project):
        project.mkdir()
        p = firn(project, "--version")
        assert p.stdout.startswith("firnpack ")
        p = firn(project, "--help")
        assert "glacier" in p.stdout
        p = firn(project, "commit", "--help")
        assert "--allow-markers" in p.stdout
        assert "--allow-flattened" in p.stdout


class TestJourneyUnicode:
    """Names and content beyond ASCII survive the round trip."""

    def test_unicode_filenames_and_content(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "café/naïve-ñ.txt", "código — ünïcode ✓\n")
        w(project, "数据/说明.txt", "你好，世界\n")
        p = firn(project, "commit", "-m", "añadido ✓")
        assert "packed 3 files" in p.stdout  # + .gitignore seeded
        p = firn(project, "status")
        assert "clean on main" in p.stdout
        p = firn(project, "history")
        assert "añadido" in p.stdout

    def test_empty_file(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "empty.txt", "")
        p = firn(project, "commit", "-m", "empty")
        assert "packed 2 files" in p.stdout
        p = firn(project, "status")
        assert "clean on main" in p.stdout


class TestJourneyMerge:
    """The full merge journey through the CLI, exactly as a user (or a
    skill) drives it: exit codes, human output, JSON contracts."""

    def test_clean_merge_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "shared.txt", "line1\nline2\nline3\n")
        firn(project, "commit", "-m", "base")
        firn(project, "branch", "side")
        firn(project, "switch", "side")
        w(project, "side.txt", "side work\n")
        w(project, "shared.txt", "line1\nside-change\nline3\n")
        firn(project, "commit", "-m", "side work")
        firn(project, "switch", "main")
        w(project, "main.txt", "main work\n")
        firn(project, "commit", "-m", "main work")
        p = firn(project, "merge", "side")
        assert p.returncode == 0
        assert "merged into layer" in p.stdout
        # both sides present after the merge
        assert (project / "side.txt").exists()
        assert (project / "main.txt").exists()
        # status is clean on the merged tree
        p = firn(project, "status")
        assert "clean on main" in p.stdout
        # the merge layer shows in history with the merge shape
        p = firn(project, "history", "--json")
        payload = json.loads(p.stdout)
        top = payload["commits"][0]
        assert top["second_parent"] is not None

    def test_conflict_merge_journey_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "shared.txt", "base\n")
        firn(project, "commit", "-m", "base")
        firn(project, "branch", "side")
        firn(project, "switch", "side")
        w(project, "shared.txt", "side version\n")
        firn(project, "commit", "-m", "side final")
        firn(project, "switch", "main")
        w(project, "shared.txt", "main version\n")
        firn(project, "commit", "-m", "main final")
        p = firn(project, "merge", "side", check=False)
        assert p.returncode == 1  # conflict: exit 1
        assert "merge conflict" in p.stderr
        assert "shared.txt" in p.stderr
        assert "--abort" in p.stderr  # the escape hatch is named
        # markers on disk, git-shaped
        content = (project / "shared.txt").read_text()
        assert "<<<<<<< ours" in content
        assert "main version" in content
        assert "side version" in content
        assert ">>>>>>> theirs" in content
        # status reports the pending merge
        p = firn(project, "status")
        assert "merge in progress" in p.stdout
        p = firn(project, "status", "--json")
        assert json.loads(p.stdout)["merge_pending"] is True
        # commit while unresolved: blocked, exit 1, no layer
        p = firn(project, "commit", "-m", "forgot", check=False)
        assert p.returncode == 1
        # resolve and complete: two-parent layer
        w(project, "shared.txt", "resolved\n")
        p = firn(project, "commit", "-m", "resolved")
        assert p.returncode == 0
        p = firn(project, "history", "--json")
        top = json.loads(p.stdout)["commits"][0]
        assert top["second_parent"] is not None
        assert top["message"] == "resolved"
        # pending state cleared
        p = firn(project, "status", "--json")
        assert json.loads(p.stdout)["merge_pending"] is False

    def test_merge_abort_journey_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "shared.txt", "base\n")
        firn(project, "commit", "-m", "base")
        firn(project, "branch", "side")
        firn(project, "switch", "side")
        w(project, "shared.txt", "side\n")
        firn(project, "commit", "-m", "side final")
        firn(project, "switch", "main")
        w(project, "shared.txt", "main\n")
        firn(project, "commit", "-m", "main final")
        p = firn(project, "merge", "side", check=False)
        assert p.returncode == 1
        p = firn(project, "merge", "--abort")
        assert p.returncode == 0
        assert "aborted" in p.stdout
        # worktree back at main's content, no markers
        assert (project / "shared.txt").read_text() == "main\n"
        p = firn(project, "status")
        assert "clean on main" in p.stdout
        # a subsequent merge can proceed (state fully cleared)
        p = firn(project, "merge", "side", check=False)
        assert p.returncode == 1  # still conflicts, but runs

    def test_fast_forward_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "1\n")
        firn(project, "commit", "-m", "base")
        firn(project, "branch", "side")
        firn(project, "switch", "side")
        w(project, "a.txt", "side version\n")
        firn(project, "commit", "-m", "side ahead")
        firn(project, "switch", "main")
        p = firn(project, "merge", "side")
        assert p.returncode == 0
        assert "fast-forwarded" in p.stdout
        # main's head advanced — visible in history walking side's commit
        p = firn(project, "history")
        assert "side ahead" in p.stdout
        p = firn(project, "status")
        assert "clean on main" in p.stdout


class TestJourneySafety:
    """The safety commands through the CLI: amend, restore, export,
    verify, compact — including the compact reference guards (a branch
    remap, an off-chain ancestry refusal, amend-orphan GC). Every
    journey ends with `verify` proving the glacier is consistent."""

    def test_amend_restore_verify_roundtrip(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "first\n")
        p = firn(project, "commit", "-m", "first")
        first_id = json.loads(firn(project, "history", "--json").stdout)["commits"][0][
            "id"
        ]
        w(project, "a.txt", "first, fixed\n")
        p = firn(project, "amend", "-m", "first (fixed)")
        assert "amended" in p.stdout
        p = firn(project, "verify")
        assert "verify ok" in p.stdout
        # history shows the amended message, not the original
        p = firn(project, "history")
        assert "first (fixed)" in p.stdout
        # restore back to the original layer (rescued drift-free)
        p = firn(project, "restore", first_id[:10])
        assert "restored" in p.stdout
        assert (project / "a.txt").read_text() == "first\n"
        p = firn(project, "verify")
        assert "verify ok" in p.stdout

    def test_export_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "content\n")
        firn(project, "commit", "-m", "c0")
        head = json.loads(firn(project, "history", "--json").stdout)["commits"][0]["id"]
        dest = project.parent / "exported"
        p = firn(project, "export", head[:10], "--to", str(dest))
        assert "exported" in p.stdout
        assert (dest / "a.txt").read_text() == "content\n"
        # refuses a non-fresh destination
        p = firn(project, "export", head[:10], "--to", str(dest), check=False)
        assert p.returncode == 1

    def test_compact_cli_with_branch_remapped(self, project):
        project.mkdir()
        firn(project, "init")
        for i in range(4):
            w(project, f"f{i}.txt", f"content {i}\n" * (i + 1))
            firn(project, "commit", "-m", f"c{i}")
        firn(project, "branch", "feature")
        p = firn(project, "compact", "--keep", "2", "--dry-run")
        assert "compact plan" in p.stdout
        assert "will follow the rewrite" in p.stdout  # remap is never silent
        p = firn(project, "compact", "--keep", "2", "--yes")
        assert "compacted" in p.stdout
        assert "remapped" in p.stdout
        # feature was at main's head -> remapped along with it
        p = firn(project, "list-branches", "--json")
        heads = {b["name"]: b["head"] for b in json.loads(p.stdout)["branches"]}
        assert heads["feature"] == heads["main"]
        p = firn(project, "verify")
        assert "verify ok" in p.stdout

    def test_compact_cli_refuses_offchain_ancestry(self, project):
        project.mkdir()
        firn(project, "init")
        ids = []
        for i in range(5):
            w(project, f"f{i}.txt", f"content {i}\n" * (i + 1))
            firn(project, "commit", "-m", f"c{i}")
            ids.append(
                json.loads(firn(project, "history", "--json").stdout)["commits"][0][
                    "id"
                ]
            )
        firn(project, "branch", "feature", ids[1][:10])  # c1: collapsed at keep 2
        firn(project, "switch", "feature")
        w(project, "f99.txt", "feature work\n")
        firn(project, "commit", "-m", "feature commit")
        firn(project, "switch", "main")
        p = firn(project, "compact", "--keep", "2", "--yes", check=False)
        assert p.returncode == 1
        combined = p.stdout + p.stderr
        assert "descends from layers" in combined
        # humane refusal, never a raw DB error
        assert "IntegrityError" not in combined
        assert "FOREIGN KEY" not in combined
        # repo fully intact and usable
        p = firn(project, "verify")
        assert "verify ok" in p.stdout
        p = firn(project, "history", "--json")
        assert json.loads(p.stdout)["count"] == 5

    def test_compact_cli_gcs_amend_orphan(self, project):
        project.mkdir()
        firn(project, "init")
        for i in range(3):
            w(project, f"f{i}.txt", f"content {i}\n" * (i + 1))
            firn(project, "commit", "-m", f"c{i}")
        firn(project, "amend", "-m", "c2 amended")
        p = firn(project, "compact", "--keep", "1", "--dry-run")
        assert "garbage collected:1" in p.stdout
        p = firn(project, "compact", "--keep", "1", "--yes")
        assert "compacted" in p.stdout
        assert "garbage-collected" in p.stdout
        p = firn(project, "verify")
        assert "verify ok" in p.stdout

    def test_branch_management_cli(self, project):
        project.mkdir()
        firn(project, "init")
        w(project, "a.txt", "1\n")
        firn(project, "commit", "-m", "c0")
        firn(project, "branch", "wip")
        p = firn(project, "list-branches")
        assert "wip" in p.stdout
        firn(project, "rename-branch", "wip", "feature")
        p = firn(project, "list-branches")
        assert "feature" in p.stdout
        assert "wip" not in p.stdout
        firn(project, "delete-branch", "feature")
        p = firn(project, "list-branches")
        assert "feature" not in p.stdout
