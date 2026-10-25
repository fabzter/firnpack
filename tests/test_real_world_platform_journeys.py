"""Platform/usage journeys through the real CLI (no module imports).

- Nested working directories: commands run from a subdirectory must
  discover the enclosing .firn and act on the repository root.
- Pinned-head mutations: amend, restore and merge while pinned — the pin
  advances, branch tips stay put, parents stay honest.
- Native symlinks and stored executable modes through switch/export.

Every command is a separate firnpack process (tests/real_world_support.py).
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

from real_world_support import TARGET_PLATFORM, World, tree_files


def test_commands_from_a_nested_directory_act_on_the_root(tmp_path):
    """Repo.discover walks up: status/commit from a nested cwd must see the
    same tracked tree and advance the same head."""
    root = tmp_path / "project"
    world = World.create(root)
    world.write("top.txt", "top\n")
    nested = World(root / "nested" / "inner")
    (root / "nested" / "inner").mkdir(parents=True)
    world.write("nested/inner/deep.txt", "deep\n")
    c1 = world.commit("root commit")

    st = nested.json("status")
    assert st["clean"] is True
    assert st["head"]["id"] == c1["layer_id"]

    # commit from the nested cwd tracks a new root-level file
    world.write("from-nested.txt", "made below\n")
    c2 = nested.commit("nested commit")
    assert c2["status"] == "packed"
    assert c2["layer_id"] != c1["layer_id"]

    # the root sees the nested commit as its head and its files tracked
    assert world.clean()["head"]["id"] == c2["layer_id"]
    hist = world.json("history")
    ids = [c["id"] for c in hist["commits"]]
    assert ids[:2] == [c2["layer_id"], c1["layer_id"]]

    # export of the nested-made commit contains the whole root tree
    dest = tmp_path / "exp"
    world.json("export", c2["layer_id"], "--to", str(dest))
    exported = tree_files(dest)
    assert exported["top.txt"] == b"top\n"
    assert exported["nested/inner/deep.txt"] == b"deep\n"
    assert exported["from-nested.txt"] == b"made below\n"
    world.verify()


def test_pinned_head_mutations(tmp_path):
    """Pins are heads: amend/restore/merge while pinned move the pin only,
    never the branch tip."""
    world = World.create(tmp_path / "pinned")
    world.write("shared.txt", "l1\nl2\nl3\n")
    base = world.commit("base")
    world.write("shared.txt", "l1\nMAIN\nl3\n")
    world.write("main.txt", "m\n")
    main_tip = world.commit("main work")
    world.json("branch", "side", base["layer_id"])

    # pin to main's tip
    world.json("switch", main_tip["layer_id"])
    st = world.json("status")
    assert st["head"]["kind"] == "pin"
    assert st["head"]["id"] == main_tip["layer_id"]

    # amend while pinned: new layer, same parent, pin advances, main doesn't
    world.write("note.txt", "amended in\n")
    amended = world.json("amend", "-m", "amended while pinned")
    assert amended["status"] == "amended"
    assert amended["replaced"] == main_tip["layer_id"]
    st = world.json("status")
    assert st["head"] == {"id": amended["layer_id"], "kind": "pin"}
    branches = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert branches["main"] == main_tip["layer_id"]
    assert branches["side"] == base["layer_id"]
    # restore a different historical commit while pinned: pin moves to it
    restored = world.json("restore", base["layer_id"])
    assert restored["status"] == "restored"
    assert restored["layer_id"] == base["layer_id"]
    assert world.json("status")["head"]["id"] == base["layer_id"]
    assert (world.path / "shared.txt").read_bytes() == b"l1\nl2\nl3\n"
    assert not (world.path / "main.txt").exists()
    assert not (world.path / "note.txt").exists()
    branches = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert branches["main"] == main_tip["layer_id"]

    # divergent merge under the pin: pin at the AMENDED layer — a sibling
    # of side_tip (both descend from base, neither is the other's
    # ancestor), so this is a real 3-way merge, not a fast-forward.
    # side only ADDS a file ours lacks -> the merge is clean.
    world.json("switch", "side")
    world.write("side.txt", "s\n")
    side_tip = world.commit("side work")
    world.json("switch", amended["layer_id"])  # pin at the amended layer
    assert world.json("status")["head"] == {
        "id": amended["layer_id"],
        "kind": "pin",
    }
    # the amend kept base as its only parent (history from the pin walks it)
    top = world.json("history")["commits"][0]
    assert top["id"] == amended["layer_id"]
    assert top["parent"] == base["layer_id"]
    assert top["second_parent"] is None
    merged = world.json("merge", "side")
    assert merged["status"] == "merged", merged
    merge_layer = merged["layer_id"]
    st = world.json("status")
    assert st["head"] == {"id": merge_layer, "kind": "pin"}
    top = world.json("history")["commits"][0]
    assert top["id"] == merge_layer
    assert top["parent"] == amended["layer_id"]
    assert top["second_parent"] == side_tip["layer_id"]
    # merged tree is the byte-exact union
    assert (world.path / "shared.txt").read_bytes() == b"l1\nMAIN\nl3\n"
    assert (world.path / "main.txt").read_bytes() == b"m\n"
    assert (world.path / "note.txt").read_bytes() == b"amended in\n"
    assert (world.path / "side.txt").read_bytes() == b"s\n"
    # branch tips never moved: the merge lives only on the pin
    branches = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert branches["main"] == main_tip["layer_id"]
    assert branches["side"] == side_tip["layer_id"]
    dest = tmp_path / "merged-export"
    world.json("export", merge_layer, "--to", str(dest))
    exported = tree_files(dest)
    assert exported["shared.txt"] == b"l1\nMAIN\nl3\n"
    assert exported["side.txt"] == b"s\n"
    world.verify()


def test_symlink_and_exec_bit_round_trip(tmp_path):
    """Never skip: create and inspect links in the actual target runtime.

    Windows chmod cannot change executable bits. Its stored modes follow
    the documented CRT/carry-forward policy; POSIX physical mode changes
    remain exact assertions. Forward directory links also exercise
    extraction into an initially empty export destination.
    """
    world = World.create(tmp_path / "links")
    world.write("script.sh", "#!/bin/sh\necho v1\n")
    world.write("run.bat", b"@echo v1\r\n")
    world.write("data/payload.bin", b"v1\0\xff\n")
    created = world.python_json(
        """
import json, os
from pathlib import Path
if os.name != "nt":
    Path("script.sh").chmod(0o755)
    Path("run.bat").chmod(0o644)
Path("latest.sh").symlink_to("script.sh")
Path("assets-link").symlink_to("data", target_is_directory=True)
print(json.dumps({"file_link": os.readlink("latest.sh"),
                  "directory_link": os.readlink("assets-link")}))
"""
    )
    assert created == {"file_link": "script.sh", "directory_link": "data"}
    c1 = world.commit("v1 with native file and directory links")

    world.write("script.sh", "#!/bin/sh\necho v2\n")
    world.write("run.bat", b"@echo v2\r\n")
    world.write("data/payload.bin", b"v2\r\n\0")
    changed = world.python_json(
        """
import json, os
from pathlib import Path
if os.name != "nt":
    Path("script.sh").chmod(0o644)
print(json.dumps({"platform": os.name}))
"""
    )
    assert changed["platform"] == ("nt" if TARGET_PLATFORM == "win32" else "posix")
    c2 = world.commit("v2 content and POSIX mode change")

    modes1 = {"script.sh": 0o644, "run.bat": 0o755}
    modes2 = dict(modes1)
    if TARGET_PLATFORM != "win32":
        modes1 = {"script.sh": 0o755, "run.bat": 0o644}
        modes2 = {"script.sh": 0o644, "run.bat": 0o644}
    for commit, expected_modes in ((c1, modes1), (c2, modes2)):
        layer = world.path / ".firn" / "layers" / f"{commit['layer_id']}.firn"
        with tarfile.open(layer, "r:gz") as archive:
            for name, mode in expected_modes.items():
                member = archive.getmember(name)
                assert member.isfile()
                assert member.mode == mode, (name, member.mode, mode)
            for name, target in (("latest.sh", "script.sh"), ("assets-link", "data")):
                member = archive.getmember(name)
                assert member.issym()
                assert member.linkname == target
                assert member.mode == 0o777

    def assert_materialized(root, version, modes):
        observed = world.python_json(
            """
import json, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
files = {}
for name in ("script.sh", "run.bat"):
    path = root / name
    mode = stat.S_IMODE(path.lstat().st_mode)
    files[name] = {"bytes": path.read_bytes().hex(), "mode": mode,
                   "exec": bool(mode & stat.S_IXUSR)}
file_link = root / "latest.sh"
directory_link = root / "assets-link"
print(json.dumps({
    "files": files,
    "file_link": {"is_link": file_link.is_symlink(),
                  "target": os.readlink(file_link),
                  "bytes": file_link.read_bytes().hex()},
    "directory_link": {"is_link": directory_link.is_symlink(),
                       "target": os.readlink(directory_link),
                       "is_directory": directory_link.is_dir(),
                       "children": sorted(os.listdir(directory_link)),
                       "bytes": (directory_link / "payload.bin").read_bytes().hex()}
}))
""",
            world.target_path(root),
        )
        script = f"#!/bin/sh\necho v{version}\n".encode()
        batch = f"@echo v{version}\r\n".encode()
        payload = b"v1\0\xff\n" if version == 1 else b"v2\r\n\0"
        assert observed["file_link"] == {
            "is_link": True,
            "target": "script.sh",
            "bytes": script.hex(),
        }
        assert observed["directory_link"] == {
            "is_link": True,
            "target": "data",
            "is_directory": True,
            "children": ["payload.bin"],
            "bytes": payload.hex(),
        }
        for name, data in (("script.sh", script), ("run.bat", batch)):
            actual = observed["files"][name]
            assert actual["bytes"] == data.hex()
            assert actual["exec"] == (modes[name] == 0o755)
            if TARGET_PLATFORM != "win32":
                assert actual["mode"] == modes[name], (name, actual, modes)

    world.json("switch", c1["layer_id"])
    assert_materialized(world.path, 1, modes1)
    world.clean()
    world.json("switch", c2["layer_id"])
    assert_materialized(world.path, 2, modes2)
    world.clean()
    dest = tmp_path / "exported"
    world.json("export", c1["layer_id"], "--to", str(dest))
    assert_materialized(dest, 1, modes1)
    assert not (world.path.parent / "script.sh").exists()
    assert not (world.path.parent / "payload.bin").exists()
    world.verify()


def test_imported_posix_mode_survives_edit_switch_and_export(tmp_path):
    """A stored executable .sh must not lose its mode on Windows.

    Build a format-valid POSIX-mode layer independently of the engine.
    Rebuild and verify must accept its hashes before the journey starts.
    On Windows the disk's non-executable CRT value differs from the
    stored 0755, so this distinguishes carry-forward from a fresh stat.
    """
    world = World.create(tmp_path / "imported-posix")
    world.write("deploy.sh", "#!/bin/sh\necho original\n")
    world.write("note.txt", "notes\n")
    files = {
        ".gitignore": (0o644, (world.path / ".gitignore").read_bytes()),
        "deploy.sh": (0o755, (world.path / "deploy.sh").read_bytes()),
        "note.txt": (0o644, b"notes\n"),
    }
    tree_digest = hashlib.sha256()
    for name, (mode, data) in sorted(files.items()):
        entry = b"f" + bytes([int(mode == 0o755)]) + name.encode() + b"\0" + data
        tree_digest.update(hashlib.sha256(entry).digest())
    record = {
        "format_version": 1,
        "tree_id": tree_digest.hexdigest()[:40],
        "parent": None,
        "second_parent": None,
        "message": "portable POSIX executable fixture",
        "author": "fixture",
        "created_at": "2026-10-25T00:00:00+00:00",
    }

    def canonical(value):
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    seed_id = hashlib.sha256(canonical(record)).hexdigest()[:40]
    members = {
        "__firnpack_meta__": (
            0o644,
            canonical(record | {"layer_id": seed_id, "profile": "gzip"}),
        ),
        **files,
    }
    layer = world.path / ".firn" / "layers" / f"{seed_id}.firn"
    with tarfile.open(layer, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name in ("__firnpack_meta__", *sorted(files)):
            mode, data = members[name]
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    disk = world.python_json(
        """
import json, os, stat
from pathlib import Path
path = Path("deploy.sh")
if os.name != "nt":
    path.chmod(0o755)
print(json.dumps({"exec": bool(path.stat().st_mode & stat.S_IXUSR)}))
"""
    )
    assert disk["exec"] is (TARGET_PLATFORM != "win32")
    rebuilt = world.json("verify", "--rebuild-db")
    assert rebuilt["main_head"] == seed_id
    assert rebuilt["verify"] == {"status": "ok", "layers": 1}
    assert world.clean()["modified"] == []
    world.write("deploy.sh", "#!/bin/sh\necho edited\n")
    assert world.json("status", "--full")["modified"] == ["deploy.sh"]
    edited = world.commit("edit imported executable")["layer_id"]
    with tarfile.open(world.path / ".firn" / "layers" / f"{edited}.firn") as archive:
        assert archive.getmember("deploy.sh").mode == 0o755
    world.json("switch", seed_id)
    assert (world.path / "deploy.sh").read_bytes() == files["deploy.sh"][1]
    world.clean()
    world.json("switch", edited)
    world.clean()
    dest = tmp_path / "imported-export"
    world.json("export", edited, "--to", str(dest))
    observed = world.python_json(
        """
import json, stat, sys
from pathlib import Path
path = Path(sys.argv[1]) / "deploy.sh"
print(json.dumps({"bytes": path.read_bytes().hex(),
                  "mode": stat.S_IMODE(path.stat().st_mode)}))
""",
        world.target_path(dest),
    )
    assert observed["bytes"] == b"#!/bin/sh\necho edited\n".hex()
    if TARGET_PLATFORM != "win32":
        assert observed["mode"] == 0o755
    world.verify()
