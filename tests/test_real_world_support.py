"""Host-side path oracles must not confuse Windows results with POSIX paths."""

from __future__ import annotations

import json
import sys
from pathlib import PureWindowsPath

import pytest
import real_world_support as support


def test_native_reported_paths_resolve_against_the_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "TARGET_PLATFORM", sys.platform)
    world = support.World(tmp_path / "project")
    assert world.host_path("output") == world.path / "output"
    assert (
        world.host_path(str(tmp_path / "archives")) == tmp_path.resolve() / "archives"
    )
    assert world.host_path("../archives") == tmp_path.resolve() / "archives"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-host Wine path conversion")
@pytest.mark.parametrize("drive", ["Z:", "z:", ""])
def test_windows_rooted_reports_map_to_the_host(tmp_path, monkeypatch, drive):
    monkeypatch.setattr(support, "TARGET_PLATFORM", "win32")
    world = support.World(tmp_path / "project")
    target = tmp_path.resolve() / "archives" / "layer.zip"
    rooted = str(PureWindowsPath(str(target)))
    assert world.host_path(drive + rooted) == target


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-host Wine path conversion")
def test_windows_relative_reports_use_the_project_not_pytest_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "TARGET_PLATFORM", "win32")
    world = support.World(tmp_path / "project")
    assert world.host_path(r"..\archives\layer.zip") == (
        tmp_path.resolve() / "archives" / "layer.zip"
    )
    with pytest.raises(AssertionError, match="drive-relative Windows path"):
        world.host_path(r"Z:output\tree")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-host Wine path conversion")
@pytest.mark.parametrize("reported", [r"C:\archives\layer.zip", r"\\server\share\x"])
def test_windows_unmapped_drives_fail_closed(tmp_path, monkeypatch, reported):
    monkeypatch.setattr(support, "TARGET_PLATFORM", "win32")
    world = support.World(tmp_path / "project")
    with pytest.raises(AssertionError, match="unmapped Windows drive"):
        world.host_path(reported)


@pytest.mark.parametrize("target_platform", [sys.platform, "win32"])
def test_reported_paths_cannot_escape_the_fixture_sandbox(
    tmp_path, monkeypatch, target_platform
):
    monkeypatch.setattr(support, "TARGET_PLATFORM", target_platform)
    world = support.World(tmp_path / "project")
    with pytest.raises(AssertionError, match="escapes fixture sandbox"):
        world.host_path("../../outside")


@pytest.mark.parametrize("override", ["not json", "[]", "{}", '[""]', "[42]"])
def test_target_python_command_rejects_invalid_overrides(monkeypatch, override):
    monkeypatch.setenv("FIRNPACK_E2E_PYTHON", override)
    with pytest.raises(ValueError, match="FIRNPACK_E2E_PYTHON must be"):
        support._command("FIRNPACK_E2E_PYTHON", (sys.executable, "-I"))


def test_native_python_json_uses_the_fixture_and_records_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(support, "TARGET_PLATFORM", sys.platform)
    monkeypatch.setattr(support, "PYTHON_CALLS", [])
    monkeypatch.delenv("FIRNPACK_E2E_PYTHON", raising=False)
    world = support.World(tmp_path)
    observed = world.python_json(
        "import json;from pathlib import Path;"
        'Path("fixture.bin").write_bytes(bytes([0,10,255]));'
        'print(json.dumps({"cwd":str(Path.cwd()),"value":sys.argv[1]}))',
        "argument with spaces",
    )
    assert observed == {
        "cwd": str(tmp_path.resolve()),
        "value": "argument with spaces",
    }
    assert (tmp_path / "fixture.bin").read_bytes() == b"\0\n\xff"
    assert len(support.PYTHON_CALLS) == 1
    assert support.PYTHON_CALLS[0]["returncode"] == 0


def test_cross_platform_python_json_cannot_fall_back_to_the_host(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "TARGET_PLATFORM", "different-from-the-host")
    monkeypatch.delenv("FIRNPACK_E2E_PYTHON", raising=False)
    with pytest.raises(ValueError, match="must select the target interpreter"):
        support.World(tmp_path).python_json('print("{}")')


def test_wrong_target_python_fails_before_fixture_code(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "TARGET_PLATFORM", "different-from-the-host")
    monkeypatch.setenv("FIRNPACK_E2E_PYTHON", json.dumps([sys.executable, "-I"]))
    with pytest.raises(AssertionError, match="AssertionError"):
        support.World(tmp_path).python_json(
            'from pathlib import Path;Path("must-not-exist").touch();print("{}")'
        )
    assert not (tmp_path / "must-not-exist").exists()


def test_target_paths_are_sandboxed_and_use_the_target_syntax(tmp_path, monkeypatch):
    world = support.World(tmp_path / "project")
    monkeypatch.setattr(support, "TARGET_PLATFORM", "win32")
    path = tmp_path.resolve() / "export"
    expected = (
        str(PureWindowsPath("Z:/").joinpath(*path.parts[1:]))
        if sys.platform != "win32"
        else str(path)
    )
    assert world.target_path(path) == expected
    with pytest.raises(AssertionError, match="target path escapes fixture sandbox"):
        world.target_path(tmp_path.parent / "outside")
