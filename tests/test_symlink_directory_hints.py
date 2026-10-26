"""Forward directory links need an explicit type on Windows.

The portable oracle captures Path.symlink_to's directory argument before
any target directory exists; the end-to-end journey additionally checks
real restored/exported links with is_dir, listdir and file reads.
"""

from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path
from types import SimpleNamespace

import pytest

import firnpack
from firnpack import Repo


@pytest.mark.parametrize("operation", ["export", "sync", "merge-tree"])
@pytest.mark.parametrize(
    ("link", "target", "is_directory"),
    [
        ("assets-link", "data", True),
        ("nested/assets-link", "../data", True),
        ("latest.txt", "data/payload.bin", False),
    ],
)
def test_snapshot_directory_link_hint(
    tmp_path, monkeypatch, operation, link, target, is_directory
):
    root = tmp_path / "project"
    root.mkdir()
    repo = Repo.init(root)
    manifest = {
        link: {"target": target, "mode": 0o777, "size": 0},
        "data/payload.bin": {"target": None, "mode": 0o644, "size": 8},
    }
    monkeypatch.setattr(repo, "_read_layer", lambda _: ({}, manifest, {}))
    monkeypatch.setattr(
        repo, "_layer_contents", lambda *_: {"data/payload.bin": b"payload\n"}
    )
    calls = []

    def capture_link(path, link_target, target_is_directory=False):
        calls.append((path, link_target, target_is_directory))
        # A non-link stand-in lets the sync path collect lstat metadata
        # without relying on Windows Developer Mode in this unit test.
        path.write_bytes(b"link stand-in")

    monkeypatch.setattr(Path, "symlink_to", capture_link)
    try:
        if operation == "export":
            destination = tmp_path / "exported"
            destination.mkdir()
            repo._extract_layer_to("synthetic", destination)
        elif operation == "sync":
            destination = root
            repo._sync_worktree_to("synthetic", baseline=set())
        else:
            destination = root
            repo._write_tree_leaves(
                {
                    link: (None, target, False),
                    "data/payload.bin": (b"payload\n", None, False),
                }
            )
        assert calls == [(destination / link, target, is_directory)]
        assert (destination / "data/payload.bin").read_bytes() == b"payload\n"
    finally:
        repo.close()


@pytest.mark.parametrize("path_module", [posixpath, ntpath], ids=["posix", "windows"])
@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ({"a": "bridge", "bridge": "data"}, {"a", "bridge"}),
        ({"a": "outer/sub", "outer": "data"}, {"a", "outer"}),
        ({"a": "outer/../sub", "outer": "data/sub"}, {"a", "outer"}),
        ({"a": "outer/../data", "outer": "data/sub"}, {"outer"}),
        ({"a": "outer/../outer", "outer": "data"}, {"a", "outer"}),
        ({"a": "b", "b": "a"}, set()),
        ({"a": "a/sub"}, set()),
        ({"a": "data/payload.bin/../sub"}, set()),
        ({"nested/a": "../data"}, {"nested/a"}),
        ({"a": "../../data"}, set()),
        ({"a": "."}, {"a"}),
        ({"a": "data/sub/.."}, {"a"}),
        ({"a": "missing/../data"}, set()),
    ],
)
def test_directory_alias_resolution(monkeypatch, path_module, links, expected):
    monkeypatch.setattr(
        firnpack, "os", SimpleNamespace(path=path_module, sep=path_module.sep)
    )
    targets = {"data/payload.bin": None, "data/sub/deep.bin": None, **links}
    assert firnpack._directory_symlinks(targets) == expected


@pytest.mark.parametrize("path_module", [posixpath, ntpath], ids=["posix", "windows"])
@pytest.mark.parametrize("target", ["DATA", r"data\sub"])
def test_directory_lookup_uses_target_platform_paths(monkeypatch, path_module, target):
    monkeypatch.setattr(
        firnpack, "os", SimpleNamespace(path=path_module, sep=path_module.sep)
    )
    targets = {"data/sub/payload.bin": None, "a": target}
    expected = {"a"} if path_module is ntpath else set()
    assert firnpack._directory_symlinks(targets) == expected


@pytest.mark.parametrize("path_module", [posixpath, ntpath], ids=["posix", "windows"])
def test_expanding_alias_graph_has_bounded_inference(monkeypatch, path_module):
    calls = 0

    def bounded_normcase(path):
        nonlocal calls
        calls += 1
        assert calls <= 10000, "directory inference expanded an alias graph unboundedly"
        return path_module.normcase(path)

    monkeypatch.setattr(
        firnpack,
        "os",
        SimpleNamespace(
            sep=path_module.sep,
            path=SimpleNamespace(
                normcase=bounded_normcase,
                isabs=path_module.isabs,
                splitdrive=path_module.splitdrive,
            ),
        ),
    )
    targets = {f"l{i}": f"l{i + 1}/l{i + 1}" for i in range(20)}
    targets["l20"] = "."
    result = firnpack._directory_symlinks(targets)
    assert "l20" in result
    assert "l0" not in result
