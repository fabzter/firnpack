"""Ignore-rule regression for the fnmatch.translate suffix change.

Python ≤3.13 ``fnmatch.translate`` ends each segment regex with ``\\Z``;
3.14+ documents ``\\z`` (docs.python.org/3/library/fnmatch.html). The
ignore engine strips only the documented legacy ending before joining
segments, so on 3.14 a mid-pattern ``\\z`` silently un-ignores every
multi-segment rule — including dir patterns whose children were
previously protected. These tests wrap the PUBLIC ``fnmatch.translate``
output and pin identical verdicts under BOTH endings, plus one native
end-to-end case.
"""

from __future__ import annotations

import fnmatch
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import Repo  # noqa: E402


def _forced_translate(ending: str):
    """Wrap the public translate() and re-stamp its end anchor, so the
    SAME translated body is presented under each documented suffix."""
    real = fnmatch.translate

    def wrapper(pattern: str) -> str:
        t = real(pattern)
        if t.endswith(("\\Z", "\\z")):
            return t[:-2] + ending
        return t

    return wrapper


def _write(root: Path, rel: str, data: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")
    return p


@pytest.mark.parametrize("ending", ["\\Z", "\\z"])
def test_seg_regex_strips_both_documented_endings(monkeypatch, ending):
    """_seg_regex must unwrap (?s:...)\\Z and (?s:...)\\z identically —
    never leak the anchor into a joined multi-segment regex."""
    monkeypatch.setattr(fnmatch, "translate", _forced_translate(ending))
    assert firnpack._seg_regex("x") == "x"
    assert firnpack._seg_regex("*.tmp") == ".*\\.tmp"
    assert firnpack._seg_regex("swap.?") == "swap\\.."


@pytest.mark.parametrize("ending", ["\\Z", "\\z"])
def test_multisegment_rules_under_both_endings(monkeypatch, ending):
    """Every rule containing '/' joins per-segment translations; under
    either documented suffix the joined verdicts must be identical."""
    monkeypatch.setattr(fnmatch, "translate", _forced_translate(ending))

    # literal multi-segment file rule
    rules = firnpack._IgnoreRules("x/swap.tmp\n")
    assert rules.matches("x/swap.tmp", is_dir=False) is True
    assert rules.matches("x/keep.txt", is_dir=False) is False
    assert rules.matches("y/swap.tmp", is_dir=False) is False
    # it IS an exact file rule — a directory of that name matches too
    assert rules.matches("x/swap.tmp", is_dir=True) is True

    # multi-segment dir pattern protects all children
    rules = firnpack._IgnoreRules("logs/x/\n")
    assert rules.matches("logs/x", is_dir=True) is True
    assert rules.matches("logs/x/swap.tmp", is_dir=False) is True
    assert rules.matches("logs/x/deep/swap.tmp", is_dir=False) is True
    assert rules.matches("logs/other.txt", is_dir=False) is False
    assert rules.matches("x/swap.tmp", is_dir=False) is False

    # fnmatch '*' translates to '.*' per segment — once segments are joined
    # it does match across '/', so the slashed wildcard still ignores nested
    # logs; it just stays anchored at its own leading segment
    rules = firnpack._IgnoreRules("logs/*.log\n")
    assert rules.matches("logs/a.log", is_dir=False) is True
    assert rules.matches("other/a.log", is_dir=False) is False
    assert rules.matches("logs/deep/a.log", is_dir=False) is True


@pytest.mark.parametrize("ending", ["\\Z", "\\z"])
def test_anchored_and_negated_rules_under_both_endings(monkeypatch, ending):
    """Rooted single-segment rules anchor to the root; '!' un-ignores —
    including a slashed negation inside an ignored directory pattern."""
    monkeypatch.setattr(fnmatch, "translate", _forced_translate(ending))

    rules = firnpack._IgnoreRules("/exact.txt\n")
    assert rules.matches("exact.txt", is_dir=False) is True
    assert rules.matches("sub/exact.txt", is_dir=False) is False

    rules = firnpack._IgnoreRules("x/\n!x/keep.tmp\n")
    assert rules.matches("x", is_dir=True) is True
    assert rules.matches("x/drop.tmp", is_dir=False) is True
    assert rules.matches("x/keep.tmp", is_dir=False) is False

    # basename wildcard ignores at any depth; later negation wins locally
    rules = firnpack._IgnoreRules("*.log\n!keep.log\n")
    assert rules.matches("deep/a.log", is_dir=False) is True
    assert rules.matches("keep.log", is_dir=False) is False


def test_native_runtime_keeps_multisegment_ignored_children(tmp_path):
    """End-to-end on the CURRENT interpreter: files under a multi-segment
    ignored directory (and files named by slashed rules) stay ignored,
    land in status --ignored, and never enter a packed layer."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(
        proj,
        ".gitignore",
        "*.log\nlogs/x/\nlogs/junk.txt\n",
    )
    _write(proj, "src/a.py", "print('hi')\n")
    _write(proj, "logs/x/swap.tmp", "scratch\n")
    _write(proj, "logs/x/deep/more.tmp", "scratch\n")
    _write(proj, "logs/junk.txt", "junk\n")
    _write(proj, "logs/keep.txt", "keep\n")
    _write(proj, "deep/a.log", "log noise\n")

    repo = Repo.init(proj)
    status = repo.status(list_ignored=True)
    for ignored in ("logs/x/swap.tmp", "logs/junk.txt", "deep/a.log"):
        assert ignored in status["ignored"], (
            f"{ignored} not ignored on this runtime: {status['ignored']}"
        )
    assert "logs/keep.txt" not in status["ignored"]
    assert "src/a.py" not in status["ignored"]

    packed = repo.commit("seed")
    assert packed["status"] == "packed"
    layer = repo._layer_path(packed["layer_id"])
    with tarfile.open(layer, "r:gz") as tar:
        names = tar.getnames()
    assert "logs/x/swap.tmp" not in names
    assert "logs/x/deep/more.tmp" not in names
    assert "logs/junk.txt" not in names
    assert "deep/a.log" not in names
    assert "logs/keep.txt" in names
    assert "src/a.py" in names
    assert ".gitignore" in names
    assert firnpack.META_MEMBER in names
    repo.close()
