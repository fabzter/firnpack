"""Failures a user can hit on a fragile disk must read as plain words.

README promise: errors are humane and actionable. A raw traceback (a full
disk, a database held open by OneDrive, a hand-edited config) tells a
non-developer nothing; each case below must end in exactly one `firnpack:`
line, the documented exit code, and no `Traceback`.

Exit codes are the documented contract (docs/usage.md): 1 = refused or
failed with a humane message, 3 = integrity failure.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402


@pytest.fixture()
def repo_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    repo = firnpack.Repo.init(tmp_path)
    repo.commit("first")
    repo.close()
    return tmp_path


def _run(capsys, *argv):
    rc = firnpack.main(list(argv))
    return rc, capsys.readouterr().err


def _assert_one_humane_line(err):
    assert "Traceback" not in err
    lines = [ln for ln in err.splitlines() if ln.startswith("firnpack: ")]
    assert len(lines) == 1, err


def test_out_of_space_on_commit_is_one_plain_sentence(repo_dir, monkeypatch, capsys):
    """A disk error mid-write names the problem and the way back to safety."""
    (repo_dir / "a.txt").write_text("two\n", encoding="utf-8")

    def disk_full(src, dst, *args, **kwargs):
        raise OSError(28, "No space left on device", str(dst))

    monkeypatch.setattr(os, "replace", disk_full)
    rc, err = _run(capsys, "commit", "-m", "second")
    assert rc == 1, err
    _assert_one_humane_line(err)
    assert "file system error" in err
    assert "No space left on device" in err
    assert "firnpack verify" in err


def test_locked_database_is_retryable_not_a_traceback(repo_dir, monkeypatch, capsys):
    """OneDrive or antivirus holding glacier.db open is transient: exit 1,
    one line saying to wait and retry."""
    real_connect = sqlite3.connect
    holder = real_connect(repo_dir / ".firn" / "glacier.db")
    holder.execute("BEGIN EXCLUSIVE")
    # shorten the 10 s lock wait so the test is fast; behavior is identical
    monkeypatch.setattr(
        sqlite3, "connect", lambda p, timeout=10, **kw: real_connect(p, timeout=0.05)
    )
    try:
        rc, err = _run(capsys, "status")
    finally:
        holder.close()
    assert rc == 1, err
    _assert_one_humane_line(err)
    assert "try again" in err


def test_database_without_tables_points_at_the_rebuild(repo_dir, capsys):
    """An empty glacier.db is store damage (exit 3), and the message names
    the documented recovery, not the SQLite internals alone."""
    (repo_dir / ".firn" / "glacier.db").write_bytes(b"")
    rc, err = _run(capsys, "status")
    assert rc == 3, err
    _assert_one_humane_line(err)
    assert "--rebuild-db" in err


@pytest.mark.parametrize(
    "content",
    [b"no section header here\n", b"[user]\nname = \xff\xfe broken\n"],
    ids=["no-section-header", "not-utf8"],
)
def test_unreadable_config_names_the_file(repo_dir, capsys, content):
    cfg = repo_dir / ".firn" / "config"
    cfg.write_bytes(content)
    rc, err = _run(capsys, "status")
    assert rc == 1, err
    _assert_one_humane_line(err)
    assert str(cfg) in err
    assert "delete it" in err  # the way out: defaults are always available


def test_config_with_non_ascii_author_round_trips(repo_dir, capsys):
    """init writes the config as UTF-8; it must be read back as UTF-8 on
    every platform (the locale codepage would garble the name)."""
    cfg = repo_dir / ".firn" / "config"
    cfg.write_text("[user]\nname = José Peña\n", encoding="utf-8")
    (repo_dir / "a.txt").write_text("two\n", encoding="utf-8")
    rc, _ = _run(capsys, "commit", "-m", "accented author")
    assert rc == 0
    repo = firnpack.Repo(repo_dir)
    try:
        assert repo.author == "José Peña"
    finally:
        repo.close()


def test_sweep_says_so_when_a_leftover_cannot_be_removed(repo_dir, monkeypatch, capsys):
    """A locked leftover is not silently kept: the user is told which file
    and that the next write tries again. The command itself still succeeds."""
    layers = repo_dir / ".firn" / "layers"
    (layers / "stale-write.tmp").write_bytes(b"junk")
    real_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise PermissionError(13, "locked by another program", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    (repo_dir / "a.txt").write_text("two\n", encoding="utf-8")
    rc, err = _run(capsys, "commit", "-m", "second")
    assert rc == 0, err
    assert "could not remove leftover" in err
    assert "stale-write.tmp" in err
    assert (layers / "stale-write.tmp").exists()  # not swept, and we said so


def test_delete_branch_warns_when_the_tip_becomes_unreachable(repo_dir, capsys):
    """docs/usage.md + SPEC §9.4: deleting a branch whose tip nothing else
    reaches is allowed, but never silent."""
    repo = firnpack.Repo.discover()
    repo.branch("side")
    repo.switch("side")
    (repo_dir / "a.txt").write_text("side work\n", encoding="utf-8")
    repo.commit("only on side")
    repo.switch("main")
    repo.close()
    rc, err = _run(capsys, "delete-branch", "side")
    assert rc == 0, err
    assert "warning:" in err
    assert "no longer reachable" in err
    assert "firnpack branch side" in err  # the one-line way to keep it


def test_delete_branch_is_quiet_when_the_tip_is_still_reachable(repo_dir, capsys):
    repo = firnpack.Repo.discover()
    repo.branch("same")  # same tip as main: nothing is stranded
    repo.close()
    rc, err = _run(capsys, "delete-branch", "same")
    assert rc == 0, err
    assert "warning:" not in err


def test_too_short_layer_id_says_how_many_characters(repo_dir, capsys):
    rc, err = _run(capsys, "switch", "abc")
    assert rc == 1
    _assert_one_humane_line(err)
    assert "at least 6 hex characters" in err
