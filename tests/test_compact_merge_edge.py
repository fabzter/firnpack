"""Compact refusal when the HEAD is a merge whose second parent reaches below
the cut (adversarial review item 6, owner decision 2026-10-21: keep the
refusal; name the structural merge edge instead of advising branch deletion).

Black-box: every check runs the CLI in a subprocess. A refusal must leave the
repository byte-identical, and the refusal advice must be actionable.
"""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "firnpack.py"


def _firn(cwd, *args):
    p = subprocess.run(
        [sys.executable, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


def _tree_digest(root: Path) -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def head_merge_repo(tmp_path):
    """main: m1..m3, feat forks at m3 (f1), main gets m4, merge feat is HEAD.
    feat is then deleted: the only remaining reference to f1 is the merge edge."""
    proj = tmp_path / "proj"
    proj.mkdir()

    def step(*args):
        rc, out, err = _firn(proj, *args)
        assert rc == 0, (args, out, err)
        return out

    step("init")
    for i in (1, 2, 3):
        (proj / "main.txt").write_text(f"m{i}\n")
        step("commit", "-m", f"m{i}")
    step("branch", "feat")
    step("switch", "feat")
    (proj / "feat.txt").write_text("f1\n")
    step("commit", "-m", "f1")
    step("switch", "main")
    (proj / "main.txt").write_text("m4\n")
    step("commit", "-m", "m4")
    step("merge", "feat", "-m", "merge feat")
    step("delete-branch", "feat")
    return proj


def test_keep_1_refusal_names_the_merge_edge_not_branch_deletion(head_merge_repo):
    proj = head_merge_repo
    before = _tree_digest(proj)

    rc, out, err = _firn(proj, "compact", "--keep", "1", "--dry-run")

    assert rc == 1, (out, err)
    # Deleting a branch cannot clear a structural merge edge: never advise it.
    assert "delete" not in err.lower(), err
    # The refusal must name the second parent it cannot cut through.
    _, hist, _ = _firn(proj, "history")
    m = re.search(r"\(merge ([0-9a-f]{8,})\)", hist)
    assert m, hist
    assert m.group(1) in err, err
    assert "merge" in err.lower(), err
    # The actionable advice that does work: raise --keep.
    assert "raise --keep" in err, err
    # Refusals are atomic: nothing on disk changed.
    assert _tree_digest(proj) == before


def test_raising_keep_past_the_second_parent_succeeds(head_merge_repo):
    """Control: the advice 'raise --keep' is real. keep=3 keeps m3, below which
    the second parent does not reach, so the plan is produced."""
    proj = head_merge_repo
    rc, out, err = _firn(proj, "compact", "--keep", "3", "--dry-run")
    assert rc == 0, (out, err)
    assert "compact plan (dry run)" in out, out
