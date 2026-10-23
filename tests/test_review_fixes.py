"""Behavior found by an execution-driven review of the humane-error work.

Each class pins one failure that used to tell the user something false or
leave the repository half-changed:

* a symlink failure during restore / fast-forward used to leave the head
  moved onto a worktree that was never rewritten, while the message said
  "history was not changed";
* a bookkeeping error after a commit landed (the reminder) reported failure;
* `amend` had no marker guard at all;
* `verify --rebuild-db` was blocked by wrong-typed unrelated config values;
* `%` in an author name triggered unwanted config interpolation on commit;
* the marker scan split lines on characters that are not line breaks and
  missed a marker after a UTF-8 BOM;
* a closed pipe (`firnpack history | head -1`) ended in a traceback.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402

MODULE = Path(firnpack.__file__).resolve()
MARKED = "intro\n<<<<<<< ours\nmine\n=======\ntheirs\n>>>>>>> theirs\n"

_REAL_SYMLINK_TO = Path.symlink_to


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _layers(project):
    return sorted(p.name for p in (project / ".firn" / "layers").iterdir())


def _snapshot(project):
    return {
        str(p.relative_to(project)): p.read_bytes()
        for p in sorted(project.rglob("*"))
        if p.is_file() and ".firn" not in p.parts
    }


def _head(project):
    repo = firnpack.Repo.discover()
    try:
        wt = repo._worktree()
        branch = repo._db.execute(
            "SELECT head FROM branches WHERE name = ?", (wt["head_branch"],)
        ).fetchone()
        return wt["head_id"], branch[0] if branch else None
    finally:
        repo.close()


def _run(capsys, *argv):
    rc = firnpack.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# --------------------------------------------------- restore / fast-forward


def _deny_symlinks(monkeypatch):
    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client", str(self))

    monkeypatch.setattr(Path, "symlink_to", refuse)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a real symlink to commit")
class TestFailedSyncKeepsTheHead:
    def _two_layers(self, project):
        (project / "real.txt").write_text("data\n", encoding="utf-8")
        (project / "lnk").symlink_to("real.txt")
        repo = firnpack.Repo.init(project)
        with_link = repo.commit("with link")["layer_id"]
        (project / "lnk").unlink()
        (project / "plain.txt").write_text("later\n", encoding="utf-8")
        repo.commit("later")
        repo.close()
        return with_link

    def test_restore_that_cannot_write_a_link_does_not_move_the_head(
        self, project, monkeypatch, capsys
    ):
        target = self._two_layers(project)
        head_before = _head(project)
        _deny_symlinks(monkeypatch)
        rc, _out, err = _run(capsys, "restore", target[:12])
        assert rc == 1, err
        assert "symbolic link" in err
        # the branch head and the worktree pointer are exactly where they were
        assert _head(project) == head_before
        monkeypatch.setattr(Path, "symlink_to", _REAL_SYMLINK_TO)  # keep the chdir
        repo = firnpack.Repo.discover()
        try:
            st = repo.status()
        finally:
            repo.close()
        assert st["clean"] is True, st  # files were put back too

    def test_fast_forward_that_cannot_write_a_link_does_not_move_the_head(
        self, project, monkeypatch, capsys
    ):
        (project / "real.txt").write_text("data\n", encoding="utf-8")
        repo = firnpack.Repo.init(project)
        repo.commit("base")
        repo.branch("dev")
        repo.switch("dev")
        (project / "lnk").symlink_to("real.txt")
        repo.commit("dev adds a link")
        repo.switch("main")
        repo.close()
        head_before = _head(project)
        _deny_symlinks(monkeypatch)
        rc, _out, err = _run(capsys, "merge", "dev")
        assert rc == 1, err
        assert _head(project) == head_before
        assert not os.path.lexists(project / "lnk")

    def test_the_message_does_not_claim_more_than_it_knows(
        self, project, monkeypatch, capsys
    ):
        target = self._two_layers(project)
        _deny_symlinks(monkeypatch)
        _rc, _out, err = _run(capsys, "restore", target[:12])
        assert "history was not changed" not in err.lower()
        assert "your history was not changed" not in err.lower()


def test_restore_reports_where_the_head_was(project, capsys):
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    repo = firnpack.Repo.init(project)
    first = repo.commit("one")["layer_id"]
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    second = repo.commit("two")["layer_id"]
    repo.close()
    rc, out, err = _run(capsys, "restore", first[:12], "--json")
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["previous_head"] == second
    rc, out, err = _run(capsys, "restore", second[:12])
    assert rc == 0, err
    assert "the head moved from" in out
    assert first[:12] in out  # the way back is named


# ---------------------------------------------------------------- reminder


def test_a_failing_reminder_never_fails_a_commit_that_landed(project, capsys):
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    firnpack.Repo.init(project).close()
    db = sqlite3.connect(project / ".firn" / "glacier.db")
    db.execute("DROP TABLE config")  # the reminder reads this table
    db.commit()
    db.close()
    rc, out, err = _run(capsys, "commit", "-m", "one", "--json")
    assert rc == 0, err
    assert json.loads(out)["status"] == "packed"
    assert "integrity failure" not in err
    assert "history_reminder" not in json.loads(out)


# ------------------------------------------------------------------- amend


class TestAmendMarkerGuard:
    def _repo(self, project):
        (project / "a.txt").write_text("one\n", encoding="utf-8")
        repo = firnpack.Repo.init(project)
        repo.commit("one")
        repo.close()

    def test_amend_refuses_markers_and_writes_nothing(self, project, capsys):
        self._repo(project)
        (project / "a.txt").write_text(MARKED, encoding="utf-8")
        before = _layers(project)
        head = _head(project)
        rc, _out, err = _run(capsys, "amend")
        assert rc == 1, err
        assert "Traceback" not in err
        assert "a.txt" in err
        assert "--allow-markers" in err
        assert _layers(project) == before
        assert _head(project) == head

    def test_amend_json_refusal_shape(self, project, capsys):
        self._repo(project)
        (project / "a.txt").write_text(MARKED, encoding="utf-8")
        rc, out, _err = _run(capsys, "amend", "--json")
        assert rc == 1
        assert json.loads(out) == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["a.txt"],
        }

    def test_amend_allow_markers_overrides_with_a_warning(self, project, capsys):
        self._repo(project)
        (project / "a.txt").write_text(MARKED, encoding="utf-8")
        rc, _out, err = _run(capsys, "amend", "--allow-markers")
        assert rc == 0, err
        assert "conflict markers remain in: a.txt" in err

    def test_amend_without_markers_is_unchanged(self, project, capsys):
        self._repo(project)
        (project / "a.txt").write_text("two\n", encoding="utf-8")
        rc, _out, err = _run(capsys, "amend")
        assert rc == 0, err


# ------------------------------------------------------------------ config


def _recoverable_repo(project):
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    repo = firnpack.Repo.init(project)
    repo.commit("one")
    repo.close()


def test_rebuild_db_still_works_with_a_damaged_config(project, capsys):
    """The command people run when they do not know what is wrong must not
    be blocked by a typo in an unrelated file."""
    _recoverable_repo(project)
    cfg = project / ".firn" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "block_unresolved_markers = true", "block_unresolved_markers = maybe"
        ),
        encoding="utf-8",
    )
    (project / ".firn" / "glacier.db").unlink()
    rc, out, err = _run(capsys, "verify", "--rebuild-db")
    assert rc == 0, err
    assert "rebuilt database" in out
    # Inspection still works; the next write names the config problem.
    rc, _out, err = _run(capsys, "status")
    assert rc == 0, err
    rc, _out, err = _run(capsys, "commit", "-m", "next")
    assert rc == 1
    assert "block_unresolved_markers" in err


def test_percent_signs_in_the_config_are_plain_text(project, capsys):
    _recoverable_repo(project)
    cfg = project / ".firn" / "config"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("name = ", "name = 100% ", 1)
    cfg.write_text(text, encoding="utf-8")
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    rc, _out, err = _run(capsys, "commit", "-m", "two")
    assert rc == 0, err
    assert "Traceback" not in err
    repo = firnpack.Repo.discover()
    try:
        assert repo.author.startswith("100% ")
    finally:
        repo.close()


def test_percent_in_a_typed_value_is_a_plain_message_not_a_traceback(project, capsys):
    _recoverable_repo(project)
    cfg = project / ".firn" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "reminder_threshold = 50", "reminder_threshold = 50%"
        ),
        encoding="utf-8",
    )
    rc, _out, err = _run(capsys, "commit", "-m", "next")
    assert rc == 1
    assert "Traceback" not in err
    assert "reminder_threshold" in err
    assert "50%" in err


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)
def test_an_unreadable_config_is_not_silently_replaced_by_defaults(project, capsys):
    _recoverable_repo(project)
    cfg = project / ".firn" / "config"
    cfg.chmod(0)
    try:
        rc, _out, err = _run(capsys, "status")
    finally:
        cfg.chmod(0o644)
    assert rc == 0, err
    assert "could not read" in err
    assert str(cfg) in err


# ---------------------------------------------------------- marker scanning


@pytest.mark.parametrize(
    "data,blocked",
    [
        (b"\xef\xbb\xbf<<<<<<< ours\n", True),  # BOM before the marker
        (b"a\r\n<<<<<<< ours\r\n", True),  # CRLF
        (b"a\r<<<<<<< ours\r", True),  # lone CR
        (b"<<<<<<< at the very start", True),
        (b"a\x0b<<<<<<< vertical tab\n", False),  # not a line break
        (b"a\x0c<<<<<<< form feed\n", False),
        (b"a\x1c<<<<<<< file separator\n", False),
        ("a\u2028<<<<<<< line separator\n".encode(), False),
        ("a\x85<<<<<<< next line\n".encode("latin-1"), False),
        (b"a\n x<<<<<<< mid\n", False),
        (b"a\n=======\nb\n", False),
    ],
    ids=[
        "bom",
        "crlf",
        "lone-cr",
        "file-start",
        "vtab",
        "formfeed",
        "fs",
        "u2028",
        "nel",
        "mid-line",
        "divider",
    ],
)
def test_marker_scan_only_treats_real_line_starts_as_markers(
    project, capsys, data, blocked
):
    (project / "a.txt").write_bytes(b"one\n")
    firnpack.Repo.init(project).close()
    (project / "t.txt").write_bytes(data)
    rc, _out, err = _run(capsys, "commit", "-m", "x")
    assert (rc == 1) is blocked, (data, rc, err)


# -------------------------------------------------------------- closed pipe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe semantics")
def test_a_closed_pipe_ends_quietly(tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def run(*args):
        return subprocess.run(
            [sys.executable, str(MODULE), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )

    run("init")
    for i in range(400):  # enough history to overflow the pipe buffer
        (tmp_path / "a.txt").write_text(f"v{i}\n" * 20, encoding="utf-8")
        run("commit", "-m", f"commit number {i} " + "x" * 80)
    proc = subprocess.run(
        f'"{sys.executable}" "{MODULE}" history | head -n 1',
        cwd=tmp_path,
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "BrokenPipeError" not in proc.stderr, proc.stderr
    assert proc.stdout.strip()  # the first line did arrive
