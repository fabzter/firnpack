"""Runs the Windows exec-bit check on the windows-latest CI legs.

`wine_exec_bit_check.py` proves two Windows-only facts the macOS/Linux suites
cannot reach: the CRT derives the exec bit from the file extension, and a
later commit takes a regular file's mode from the head manifest, not the
disk. By hand it runs under wine (see its docstring); here the same script
runs under a real Windows interpreter. Everywhere else this test is skipped,
because the script has nothing to verify off Windows.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "wine_exec_bit_check.py"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="extension-derived exec bit is Windows CRT behavior",
)
def test_windows_exec_bit_facts_hold(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "work")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    # without a seed argument: 2 CRT facts + 2 commits x 2 files
    assert sum(ln.startswith("PASS ") for ln in lines) == 6, proc.stdout
    assert not any(ln.startswith("FAIL ") for ln in lines), proc.stdout


def test_the_check_script_itself_is_present_and_documented():
    """The manual/CI check must not rot unnoticed: the script exists and
    still names the two facts it verifies."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "CRT derives the exec bit" in text
    assert "carry-forward" in text
