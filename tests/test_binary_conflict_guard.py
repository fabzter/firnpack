"""A merge that leaves a binary file undecided must not finish silently.

Owner decision 2026-10-23. When both branches changed a binary file (it holds
a NUL byte, so no text merge exists), the merge keeps OUR version in the
worktree and sets a pending merge. A plain `commit` used to finish the merge
at once, dropping THEIR version with no refusal and with `status` saying
"clean". Now `commit` refuses until the user acts:

* copy their version in (or any other content: the file no longer equals
  ours), or
* pass `--allow-binary-conflict` to keep ours on purpose, or
* `merge --abort`.

This binary guard leaves text conflicts to the marker guard; files only
one side changed, symlinks, and binary files already replaced are not
treated as undecided binary conflicts.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402

BASE = b"\x00base\x00"
OURS = b"\x00MAIN-edit\x00"
THEIRS = b"\x00SIDE-edit\x00"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _conflicted(project, base=BASE, ours=OURS, theirs=THEIRS, name="img.bin"):
    """main and side both change `name` differently; leave the merge pending."""
    (project / name).write_bytes(base)
    repo = firnpack.Repo.init(project)
    repo.commit("base")
    repo.branch("side")
    repo.switch("side")
    (project / name).write_bytes(theirs)
    repo.commit("side")
    repo.switch("main")
    (project / name).write_bytes(ours)
    repo.commit("main")
    result = repo.merge("side")
    repo.close()
    return result


def _run(capsys, *argv):
    rc = firnpack.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _layer_count(project):
    return len(list((project / ".firn" / "layers").glob("*.firn")))


def test_the_merge_itself_reports_the_conflict(project):
    result = _conflicted(project)
    assert result["status"] == "conflict"
    assert result["conflicts"] == ["img.bin"]
    assert (project / "img.bin").read_bytes() == OURS  # ours stays in place


def test_plain_commit_refuses_and_names_the_ways_out(project, capsys):
    _conflicted(project)
    before = _layer_count(project)
    rc, _out, err = _run(capsys, "commit", "-m", "finish")
    assert rc == 1, err
    assert "Traceback" not in err
    assert "img.bin" in err
    assert "--allow-binary-conflict" in err
    assert "merge --abort" in err
    assert _layer_count(project) == before  # nothing was written


def test_json_refusal_shape(project, capsys):
    _conflicted(project)
    rc, out, err = _run(capsys, "commit", "-m", "finish", "--json")
    assert rc == 1
    assert json.loads(out) == {
        "status": "blocked_by_binary_conflict",
        "blocked_by_binary_conflict": ["img.bin"],
    }
    assert "firnpack: the merge left a binary file undecided" in err


def test_allow_flag_finishes_the_merge_keeping_ours(project, capsys):
    _conflicted(project)
    rc, _out, err = _run(capsys, "commit", "-m", "keep mine", "--allow-binary-conflict")
    assert rc == 0, err
    repo = firnpack.Repo.discover()
    try:
        top = repo.history()["commits"][0]
        assert top["second_parent"] is not None  # it IS the merge layer
        assert repo._worktree()["merge_theirs"] is None
    finally:
        repo.close()
    assert (project / "img.bin").read_bytes() == OURS
    assert "warning:" in err
    assert "img.bin" in err
    assert "keeping our version" in err


def test_putting_theirs_in_place_lets_a_plain_commit_through(project, capsys):
    _conflicted(project)
    (project / "img.bin").write_bytes(THEIRS)  # the user decided: take theirs
    rc, _out, err = _run(capsys, "commit", "-m", "took theirs")
    assert rc == 0, err
    assert (project / "img.bin").read_bytes() == THEIRS


def test_any_new_content_counts_as_a_decision(project, capsys):
    _conflicted(project)
    (project / "img.bin").write_bytes(b"\x00hand-merged\x00")
    rc, _out, err = _run(capsys, "commit", "-m", "hand merged")
    assert rc == 0, err


def test_abort_is_still_the_clean_way_out(project, capsys):
    _conflicted(project)
    rc, _out, err = _run(capsys, "merge", "--abort")
    assert rc == 0, err
    assert (project / "img.bin").read_bytes() == OURS
    rc, out, err = _run(capsys, "status")
    assert rc == 0 and "merge in progress" not in out, (out, err)


def test_status_still_reports_the_pending_merge(project, capsys):
    _conflicted(project)
    rc, out, _err = _run(capsys, "status", "--json")
    assert rc == 0
    assert json.loads(out)["merge_pending"] is True


def test_only_one_side_changed_is_not_a_conflict(project, capsys):
    (project / "img.bin").write_bytes(BASE)
    (project / "doc.txt").write_text("one\n", encoding="utf-8")
    repo = firnpack.Repo.init(project)
    repo.commit("base")
    repo.branch("side")
    repo.switch("side")
    (project / "img.bin").write_bytes(THEIRS)
    repo.commit("side changes the image")
    repo.switch("main")
    (project / "doc.txt").write_text("two\n", encoding="utf-8")
    repo.commit("main changes the doc")
    result = repo.merge("side")
    repo.close()
    assert result["status"] == "merged"  # clean: no pending merge, no guard
    assert (project / "img.bin").read_bytes() == THEIRS


def test_a_text_conflict_is_left_to_the_marker_guard(project, capsys):
    _conflicted(project, name="t.txt", base=b"base\n", ours=b"main\n", theirs=b"side\n")
    rc, _out, err = _run(capsys, "commit", "-m", "x")
    assert rc == 1
    assert "merge conflict" in err  # the marker guard's wording, not the binary one
    assert "binary" not in err


def test_two_undecided_files_are_listed_and_one_decision_is_not_enough(project, capsys):
    (project / "a.bin").write_bytes(BASE)
    (project / "b.bin").write_bytes(BASE)
    repo = firnpack.Repo.init(project)
    repo.commit("base")
    repo.branch("side")
    repo.switch("side")
    (project / "a.bin").write_bytes(THEIRS)
    (project / "b.bin").write_bytes(THEIRS)
    repo.commit("side")
    repo.switch("main")
    (project / "a.bin").write_bytes(OURS)
    (project / "b.bin").write_bytes(OURS)
    repo.commit("main")
    assert repo.merge("side")["status"] == "conflict"
    repo.close()
    rc, _out, err = _run(capsys, "commit", "-m", "x")
    assert rc == 1 and "a.bin, b.bin" in err
    (project / "a.bin").write_bytes(THEIRS)  # decide only a.bin
    rc, _out, err = _run(capsys, "commit", "-m", "x")
    assert rc == 1 and "b.bin" in err and "a.bin" not in err.split("undecided:")[1]
    (project / "b.bin").write_bytes(THEIRS)
    rc, _out, err = _run(capsys, "commit", "-m", "x")
    assert rc == 0, err


@pytest.mark.parametrize("deleted_side", ["ours", "theirs"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_binary_that_one_side_deleted_is_not_treated_as_undecided(
    project, capsys, deleted_side, json_mode
):
    """Keep-modified policy reports the kept side in text and JSON (Q7)."""
    (project / "img.bin").write_bytes(BASE)
    repo = firnpack.Repo.init(project)
    repo.commit("base")
    repo.branch("side")
    repo.switch("side")
    if deleted_side == "theirs":
        (project / "img.bin").unlink()
    else:
        (project / "img.bin").write_bytes(THEIRS)
    theirs = repo.commit("side change")["layer_id"]
    repo.switch("main")
    if deleted_side == "ours":
        (project / "img.bin").unlink()
    else:
        (project / "img.bin").write_bytes(OURS)
    ours = repo.commit("main change")["layer_id"]
    repo.close()
    rc, out, err = _run(capsys, "merge", "side", *(["--json"] if json_mode else []))
    assert rc == 1, err
    kept = "theirs" if deleted_side == "ours" else "ours"
    if json_mode:
        result = json.loads(out)
        assert result["status"] == "conflict"
        assert result["kept_sides"] == {"img.bin": kept}
        assert result["head"] == ours
        assert result["theirs"] == theirs
    else:
        assert f"kept modified side: {kept}" in err
        assert ours in err and theirs in err
    rc, _out, err = _run(capsys, "commit", "-m", "keep the edit")
    assert rc == 0, err


def test_the_guard_does_not_touch_ordinary_commits(project, capsys):
    (project / "img.bin").write_bytes(BASE)
    repo = firnpack.Repo.init(project)
    repo.commit("base")
    repo.close()
    (project / "img.bin").write_bytes(OURS)
    rc, _out, err = _run(capsys, "commit", "-m", "plain")
    assert rc == 0, err


def test_binary_conflict_output_names_the_source_layers_and_resolution(project, capsys):
    result = _conflicted(project)
    rc, _out, err = _run(capsys, "merge", "--abort")
    assert rc == 0, err
    rc, _out, err = _run(capsys, "merge", "side")
    assert rc == 1, err
    assert result["head"] in err
    assert result["theirs"] in err
    assert "binary conflicts keep ours" in err
    assert "firnpack export" in err
