"""Refused compact must not mutate .firn/layers/ (refusals are atomic).

Contract basis: docs/agent-contract.md (refusal = nothing mutated). Red-first:
`compact --keep 1` without `--yes` exits rc1 but currently unlinks
unreferenced layers/*.firn and layers/*.firn.tmp. The --dry-run control keeps them.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "firnpack.py"
ORPHAN_ID = "0" * 39 + "1"
ORPHAN_LAYER = f"{ORPHAN_ID}.firn"
ORPHAN_TMP = "stale-write.tmp"
ORPHAN_TMP_MATCHED = f"{ORPHAN_ID}.firn.tmp"


def _firn(cwd, *args):
    return subprocess.run(
        [sys.executable, str(MODULE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _layers_listing(project):
    layers = project / ".firn" / "layers"
    return {p.name: p.read_bytes() for p in sorted(layers.iterdir()) if p.is_file()}


def _build_chain(project, n):
    proc = _firn(project, "init")
    assert proc.returncode == 0, proc.stderr
    for i in range(1, n + 1):
        (project / "a.txt").write_text(f"v{i}\n", encoding="utf-8")
        proc = _firn(project, "commit", "-m", f"c{i}")
        assert proc.returncode == 0, proc.stderr


def _plant_orphans(project):
    layers = project / ".firn" / "layers"
    (layers / ORPHAN_LAYER).write_bytes(b"orphan-layer-bytes")
    (layers / ORPHAN_TMP).write_bytes(b"orphan-tmp-bytes")
    (layers / ORPHAN_TMP_MATCHED).write_bytes(b"orphan-matched-tmp-bytes")


@pytest.mark.parametrize("n", [2, 4])
def test_refused_compact_keep1_leaves_layers_dir_identical(tmp_path, n):
    _build_chain(tmp_path, n)
    _plant_orphans(tmp_path)
    before = _layers_listing(tmp_path)
    assert ORPHAN_LAYER in before and ORPHAN_TMP in before
    assert ORPHAN_TMP_MATCHED in before

    proc = _firn(tmp_path, "compact", "--keep", "1")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    after = _layers_listing(tmp_path)
    assert after == before, sorted(set(before) ^ set(after))


@pytest.mark.parametrize("n", [2, 4])
def test_dry_run_control_keeps_orphans(tmp_path, n):
    _build_chain(tmp_path, n)
    _plant_orphans(tmp_path)
    before = _layers_listing(tmp_path)

    proc = _firn(tmp_path, "compact", "--keep", "1", "--dry-run")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _layers_listing(tmp_path) == before


def _load_firnpack():
    spec = importlib.util.spec_from_file_location("firnpack_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _crash_compact_at_swap(tmp_path, monkeypatch, capsys):
    """Run `compact --keep 1 --yes` in-process, crashing at the layer swap
    (<id>.firn.tmp -> <id>.firn), the window docs/storage.md section 3 covers.

    The crash is asserted HERE, outside any xfail: if the injected failure
    does not fire, the command succeeds (rc 0) and every caller fails loudly
    instead of passing vacuously. The user-facing contract is a one-line
    `firnpack:` message and exit 1, never a traceback (README: errors are
    plain words)."""
    real_replace = os.replace

    def crash_at_swap(src, dst, *args, **kwargs):
        if str(src).endswith(".firn.tmp") and str(dst).endswith(".firn"):
            raise OSError("injected crash at layer swap")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", crash_at_swap)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["firnpack", "compact", "--keep", "1", "--yes"])
    rc = _load_firnpack().main()
    err = capsys.readouterr().err
    assert rc == 1, f"injected crash did not surface as exit 1 (rc={rc}): {err}"
    assert "injected crash at layer swap" in err
    assert "Traceback" not in err
    assert err.count("firnpack: file system error") == 1
    assert "firnpack verify" in err  # tells the user how to confirm history
    return err


def test_crash_at_swap_sweeps_matched_orphans(tmp_path, monkeypatch, capsys):
    _build_chain(tmp_path, 3)
    _plant_orphans(tmp_path)
    before = _layers_listing(tmp_path)
    _crash_compact_at_swap(tmp_path, monkeypatch, capsys)
    after = _layers_listing(tmp_path)
    assert ORPHAN_LAYER not in after
    assert ORPHAN_TMP_MATCHED not in after
    for name in before:
        if name not in {ORPHAN_LAYER, ORPHAN_TMP, ORPHAN_TMP_MATCHED}:
            assert after[name] == before[name]


def test_crash_at_swap_sweeps_stale_write_tmp(tmp_path, monkeypatch, capsys):
    """docs/storage.md s3: ANY orphan .tmp is swept by the next mutating
    command, including a bare name that is not <id>.firn.tmp."""
    _build_chain(tmp_path, 3)
    _plant_orphans(tmp_path)
    _crash_compact_at_swap(tmp_path, monkeypatch, capsys)
    assert ORPHAN_TMP not in _layers_listing(tmp_path)
