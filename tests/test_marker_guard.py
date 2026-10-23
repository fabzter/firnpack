"""The unresolved-marker guard covers every commit, not only a merge's.

SPEC §9.2/§11 (owner decision 2026-10-22): a commit that would write a layer
refuses when a tracked text file still holds a line starting `<<<<<<<` or
`>>>>>>>`. The refusal is exit 1, nothing written, and in `--json` mode the
payload carries `blocked_by_markers`. Overrides: `--allow-markers` for one
commit, or `[merge] block_unresolved_markers = false` (warn and commit).
`=======` alone, indented or mid-line markers, binary files and symlinks are
never flagged. A commit that would write nothing still says "nothing to pack".
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402

MARKED = "intro\n<<<<<<< ours\nmine\n=======\ntheirs\n>>>>>>> theirs\n"


@pytest.fixture()
def repo_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    repo = firnpack.Repo.init(tmp_path)
    repo.commit("first")
    repo.close()
    return tmp_path


def _layers(project):
    return sorted(p.name for p in (project / ".firn" / "layers").iterdir())


def _commit(capsys, *flags, message="second"):
    rc = firnpack.main(["commit", "-m", message, *flags])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_plain_commit_with_markers_is_refused(repo_dir, capsys):
    (repo_dir / "notes.txt").write_text(MARKED, encoding="utf-8")
    before = _layers(repo_dir)
    rc, _out, err = _commit(capsys)
    assert rc == 1, err
    assert "Traceback" not in err
    assert "notes.txt" in err
    assert "--allow-markers" in err  # the way out is named
    assert "block_unresolved_markers" in err
    assert _layers(repo_dir) == before  # nothing was written


@pytest.mark.parametrize("line", ["<<<<<<< ours", ">>>>>>> theirs", "<<<<<<<"])
def test_either_marker_line_blocks(repo_dir, capsys, line):
    (repo_dir / "a.txt").write_text(f"one\n{line}\ntwo\n", encoding="utf-8")
    rc, _out, err = _commit(capsys)
    assert rc == 1, err
    assert "a.txt" in err


@pytest.mark.parametrize(
    "text",
    [
        "one\n=======\ntwo\n",  # the divider alone is markdown/RST, never flagged
        "one\n    <<<<<<< indented\n",  # must START the line
        "one\nx <<<<<<< mid-line\n",
        "one\n<<<<<< six only\n",
    ],
    ids=["divider-only", "indented", "mid-line", "six-chars"],
)
def test_marker_shaped_but_not_markers_commit_fine(repo_dir, capsys, text):
    (repo_dir / "a.txt").write_text(text, encoding="utf-8")
    rc, _out, err = _commit(capsys)
    assert rc == 0, err
    assert "conflict markers" not in err


def test_binary_file_is_never_scanned(repo_dir, capsys):
    """A NUL byte marks the file binary; a marker-shaped line inside it is
    data, not a conflict."""
    (repo_dir / "blob.bin").write_bytes(b"\x00\x01\n<<<<<<< not text\n\x00")
    rc, _out, err = _commit(capsys)
    assert rc == 0, err


def test_invalid_utf8_text_is_still_scanned(repo_dir, capsys):
    """No NUL byte means text; undecodable bytes must not hide a marker."""
    (repo_dir / "latin.txt").write_bytes(b"caf\xe9\n<<<<<<< ours\n")
    rc, _out, err = _commit(capsys)
    assert rc == 1, err
    assert "latin.txt" in err


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_symlink_target_text_is_not_scanned(repo_dir, capsys):
    (repo_dir / "link").symlink_to("<<<<<<<-looking-target")
    rc, _out, err = _commit(capsys)
    assert rc == 0, err


def test_allow_markers_commits_and_warns(repo_dir, capsys):
    (repo_dir / "notes.txt").write_text(MARKED, encoding="utf-8")
    rc, _out, err = _commit(capsys, "--allow-markers")
    assert rc == 0, err
    assert "warning: conflict markers remain in: notes.txt" in err
    assert len(_layers(repo_dir)) == 2


def test_config_can_switch_the_guard_to_warn_only(repo_dir, capsys):
    cfg = repo_dir / ".firn" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "block_unresolved_markers = true", "block_unresolved_markers = false"
        ),
        encoding="utf-8",
    )
    (repo_dir / "notes.txt").write_text(MARKED, encoding="utf-8")
    rc, _out, err = _commit(capsys)
    assert rc == 0, err
    assert "conflict markers remain in: notes.txt" in err


def test_no_op_commit_still_says_nothing_to_pack(repo_dir, capsys):
    """A repository that already holds marker-shaped lines (committed with
    --allow-markers) must not fail a commit that writes nothing."""
    (repo_dir / "notes.txt").write_text(MARKED, encoding="utf-8")
    assert _commit(capsys, "--allow-markers")[0] == 0
    rc, out, err = _commit(capsys, message="again")
    assert rc == 0, err
    assert "nothing to pack" in out


def test_json_refusal_carries_blocked_by_markers(repo_dir, capsys):
    (repo_dir / "z.txt").write_text(MARKED, encoding="utf-8")
    (repo_dir / "b.txt").write_text(MARKED, encoding="utf-8")
    rc = firnpack.main(["commit", "-m", "x", "--json"])
    cap = capsys.readouterr()
    assert rc == 1
    payload = json.loads(cap.out)  # exactly one JSON object on stdout
    assert payload == {
        "status": "blocked_by_markers",
        "blocked_by_markers": ["b.txt", "z.txt"],  # sorted, stable for agents
    }
    assert "firnpack: conflict markers" in cap.err  # humane line still on stderr


def _conflicted_merge(project):
    repo = firnpack.Repo.discover()
    (project / "shared.txt").write_text("base\n", encoding="utf-8")
    repo.commit("base")
    repo.branch("side")
    repo.switch("side")
    (project / "shared.txt").write_text("side\n", encoding="utf-8")
    repo.commit("side")
    repo.switch("main")
    (project / "shared.txt").write_text("main\n", encoding="utf-8")
    repo.commit("main")
    assert repo.merge("side")["status"] == "conflict"
    repo.close()


def test_pending_merge_keeps_its_own_wording_and_json_key(repo_dir, capsys):
    _conflicted_merge(repo_dir)
    rc, _out, err = _commit(capsys, message="forgot")
    assert rc == 1
    assert "merge conflict — resolve these files first: shared.txt" in err
    rc = firnpack.main(["commit", "-m", "forgot", "--json"])
    cap = capsys.readouterr()
    assert rc == 1
    assert json.loads(cap.out)["blocked_by_markers"] == ["shared.txt"]


def test_resolving_the_markers_completes_the_merge(repo_dir, capsys):
    _conflicted_merge(repo_dir)
    (repo_dir / "shared.txt").write_text("resolved\n", encoding="utf-8")
    rc, _out, err = _commit(capsys, message="merged")
    assert rc == 0, err
    repo = firnpack.Repo.discover()
    try:
        assert repo.history()["commits"][0]["second_parent"] is not None
    finally:
        repo.close()
