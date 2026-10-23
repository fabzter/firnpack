"""Regression coverage for the follow-up consistency review."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(capsys, *args):
    rc = firnpack.main(list(args))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _seed(project):
    (project / "a.txt").write_bytes(b"base\n")
    repo = firnpack.Repo.init(project)
    head = repo.commit("base")["layer_id"]
    repo.close()
    return head


def _bad_config(project, key, value):
    path = project / ".firn" / "config"
    text = path.read_text(encoding="utf-8")
    old = {
        "reminder_threshold": "50",
        "block_unresolved_markers": "true",
        "autoname": "true",
    }[key]
    assert f"{key} = {old}" in text
    path.write_text(
        text.replace(f"{key} = {old}", f"{key} = {value}"), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "key,bad",
    [
        ("reminder_threshold", "abc"),
        ("block_unresolved_markers", "maybe"),
        ("autoname", "perhaps"),
    ],
)
@pytest.mark.parametrize(
    "command", ["status", "verify", "history", "diff", "export", "restore-to"]
)
def test_typed_config_typo_does_not_block_inspection_or_export(
    project, capsys, key, bad, command
):
    head = _seed(project)
    _bad_config(project, key, bad)
    args = [command, "--json"]
    if command in {"export", "restore-to"}:
        args = [
            "export" if command == "export" else "restore",
            head,
            "--to",
            str(project / "out"),
            "--json",
        ]
    rc, out, err = _run(capsys, *args)
    assert rc == 0, err
    assert isinstance(json.loads(out), dict)
    if command in {"export", "restore-to"}:
        assert (project / "out" / "a.txt").read_bytes() == b"base\n"


@pytest.mark.parametrize("command", ["commit", "amend", "merge"])
def test_typed_config_typo_blocks_writers_before_changes(project, capsys, command):
    _seed(project)
    repo = firnpack.Repo.discover()
    repo.branch("side")
    repo.close()
    _bad_config(project, "reminder_threshold", "abc")
    (project / "a.txt").write_bytes(b"changed\n")
    before = {
        p.relative_to(project): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    }
    args = [command, "side"] if command == "merge" else [command, "-m", "new"]
    rc, _out, err = _run(capsys, *args)
    assert rc == 1, err
    assert "reminder_threshold" in err
    after = {
        p.relative_to(project): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_clean_merge_blocked_by_markers_has_json_and_stays_pending(project, capsys):
    _seed(project)
    repo = firnpack.Repo.discover()
    repo.branch("side")
    repo.switch("side")
    (project / "guide.txt").write_bytes(b"<<<<<<< an intentional example\n")
    repo.commit("example", allow_markers=True)
    repo.switch("main")
    (project / "other.txt").write_bytes(b"independent\n")
    ours = repo.commit("main edit")["layer_id"]
    repo.close()
    capsys.readouterr()
    rc, out, err = _run(capsys, "merge", "side", "--json")
    assert rc == 1, err
    assert json.loads(out) == {
        "status": "blocked_by_markers",
        "blocked_by_markers": ["guide.txt"],
    }
    assert "guide.txt" in err
    repo = firnpack.Repo.discover()
    try:
        assert repo._worktree()["head_id"] == ours
        assert repo.status()["merge_pending"] is True
        result = repo.commit("keep example", allow_markers=True)
        assert repo._db.execute(
            "SELECT second_parent FROM layers WHERE id = ?", (result["layer_id"],)
        ).fetchone()[0]
    finally:
        repo.close()


@pytest.mark.skipif(
    os.name == "nt", reason="needs real symlinks before simulating refusal"
)
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_restore_reports_actual_rollback_result(
    project, monkeypatch, capsys, rollback_fails
):
    (project / "a.txt").write_bytes(b"base\n")
    (project / "0-target-only.txt").write_bytes(b"written before the link\n")
    (project / "z-ignored.txt").write_bytes(b"target version\n")
    (project / "link").symlink_to("a.txt")
    repo = firnpack.Repo.init(project)
    target = repo.commit("with link")["layer_id"]
    (project / "0-target-only.txt").unlink()
    (project / "z-ignored.txt").unlink()
    (project / ".gitignore").write_bytes(b"~$*\nz-ignored.txt\n")
    if not rollback_fails:
        (project / "link").unlink()
    (project / "a.txt").write_bytes(b"later\n")
    previous = repo.commit("later")["layer_id"]
    repo.close()
    (project / "z-ignored.txt").write_bytes(b"untouched ignored work\n")

    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client", str(self))

    monkeypatch.setattr(Path, "symlink_to", refuse)
    rc, _out, err = _run(capsys, "restore", target)
    assert rc == 1, err
    assert "Files written before this point stay on disk" not in err
    assert "head was put back" in err
    if rollback_fails:
        assert "files may be partly written" in err
        assert "firnpack status" in err
    else:
        assert "files were restored to the previous head" in err
        assert "files may be partly written" not in err
    repo = firnpack.Repo.discover()
    try:
        assert repo._worktree()["head_id"] == previous
        assert repo.status()["clean"] is not rollback_fails
        assert (project / "z-ignored.txt").read_bytes() == b"untouched ignored work\n"
    finally:
        repo.close()


@pytest.mark.parametrize("command", ["restore", "export"])
def test_extraction_refuses_a_nonempty_destination_without_overwriting(
    project, capsys, command
):
    head = _seed(project)
    destination = project / "out"
    destination.mkdir()
    (destination / "a.txt").write_bytes(b"not disposable\n")
    rc, _out, err = _run(capsys, command, head, "--to", str(destination), "--json")
    assert rc == 1, err
    assert "not empty" in err
    assert (destination / "a.txt").read_bytes() == b"not disposable\n"


@pytest.mark.parametrize("command", ["restore", "export"])
def test_extraction_accepts_an_existing_empty_directory(project, capsys, command):
    head = _seed(project)
    destination = project / "out"
    destination.mkdir()
    rc, out, err = _run(capsys, command, head, "--to", str(destination), "--json")
    assert rc == 0, err
    assert json.loads(out)["status"] == "exported"
    assert (destination / "a.txt").read_bytes() == b"base\n"


@pytest.mark.skipif(
    os.name == "nt", reason="needs real symlinks before simulating refusal"
)
def test_failed_restore_reports_the_rescue_of_uncommitted_work(
    project, monkeypatch, capsys
):
    _seed(project)
    repo = firnpack.Repo.discover()
    (project / "link").symlink_to("a.txt")
    target = repo.commit("with link")["layer_id"]
    (project / "link").unlink()
    repo.commit("without link")
    repo.close()
    (project / "a.txt").write_bytes(b"precious uncommitted work\n")

    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client", str(self))

    monkeypatch.setattr(Path, "symlink_to", refuse)
    rc, _out, err = _run(capsys, "restore", target)
    assert rc == 1, err
    repo = firnpack.Repo.discover()
    try:
        row = repo._db.execute(
            "SELECT id FROM layers WHERE message = 'auto: drift rescue before restore'"
        ).fetchone()
        assert row is not None
        rescue = row[0]
        assert rescue in err
        assert f"firnpack export {rescue} --to DIR" in err
        tree = repo._tree_of(rescue)
        assert tree["a.txt"][0] == b"precious uncommitted work\n"
    finally:
        repo.close()


def test_failed_database_undo_does_not_claim_the_head_was_put_back(
    project, monkeypatch, capsys
):
    target = _seed(project)
    repo = firnpack.Repo.discover()
    (project / "a.txt").write_bytes(b"later\n")
    repo.commit("later")
    wt = repo._worktree()
    connection = repo._db
    original = firnpack.FirnpackError("initial sync failed")
    calls = []

    class UndoFails:
        def __enter__(self):
            return connection.__enter__()

        def __exit__(self, *args):
            return connection.__exit__(*args)

        def execute(self, sql, parameters=()):
            if sql.startswith("UPDATE branches") and parameters[0] == wt["head_id"]:
                raise sqlite3.OperationalError("undo unavailable")
            return connection.execute(sql, parameters)

        def close(self):
            connection.close()

    def fail_sync(layer_id, *, created_paths=None):
        calls.append(layer_id)
        (project / "a.txt").write_bytes(b"partly new\n")
        raise original

    monkeypatch.setattr(repo, "_db", UndoFails())
    monkeypatch.setattr(repo, "_sync_worktree_to", fail_sync)
    try:
        with pytest.raises(firnpack.FirnpackError) as caught:
            repo._move_head_and_sync(wt, target, "branch")
        assert caught.value is original
        assert "previous head could not be restored" in str(caught.value)
        assert "head was put back" not in str(caught.value)
        assert calls == [target]
        assert (
            connection.execute("SELECT head_id FROM worktree WHERE id = 1").fetchone()[
                0
            ]
            == target
        )
        assert (
            connection.execute(
                "SELECT head FROM branches WHERE name = 'main'"
            ).fetchone()[0]
            == target
        )
        assert (project / "a.txt").read_bytes() == b"partly new\n"
    finally:
        repo.close()


def test_non_firnpack_sync_error_keeps_its_type_and_reports_rollback(
    project, monkeypatch, capsys
):
    target = _seed(project)
    repo = firnpack.Repo.discover()
    (project / "a.txt").write_bytes(b"later\n")
    repo.commit("later")
    wt = repo._worktree()
    sync = repo._sync_worktree_to
    original = OSError(28, "disk full")
    original_args = original.args
    calls = []

    def fail_once(layer_id, *, created_paths=None):
        calls.append(layer_id)
        if len(calls) == 1:
            raise original
        sync(layer_id)

    monkeypatch.setattr(repo, "_sync_worktree_to", fail_once)
    try:
        with pytest.raises(OSError) as caught:
            repo._move_head_and_sync(wt, target, "branch")
        assert caught.value is original
        assert original.args == original_args
        assert calls == [target, wt["head_id"]]
        assert repo._worktree()["head_id"] == wt["head_id"]
        assert repo.status()["clean"] is True
        assert capsys.readouterr().err.count("head was put back") == 1
    finally:
        repo.close()


def test_invalid_ini_syntax_still_explains_how_to_recover(project, capsys):
    _seed(project)
    (project / ".firn" / "config").write_text("not an INI section\n", encoding="utf-8")
    rc, out, err = _run(capsys, "verify", "--json")
    assert rc == 1
    assert not out
    assert "cannot read" in err
    assert "Fix the file" in err
    assert "Traceback" not in err


def test_full_tree_marker_scan_rechecks_unchanged_examples(project, capsys):
    """Full-tree scope retained by owner decision 2026-10-23."""
    _seed(project)
    (project / "guide.txt").write_bytes(b"<<<<<<< intentional example\n")
    rc, _out, err = _run(capsys, "commit", "-m", "example", "--allow-markers")
    assert rc == 0, err
    (project / "a.txt").write_bytes(b"unrelated\n")
    rc, out, err = _run(capsys, "commit", "-m", "unrelated", "--json")
    assert rc == 1, err
    assert json.loads(out) == {
        "status": "blocked_by_markers",
        "blocked_by_markers": ["guide.txt"],
    }
