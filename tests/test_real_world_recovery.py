"""Multi-session recovery and portability journeys through the real CLI.

Every command below is a separate firnpack process (see
``tests/real_world_support.py``); fixtures and corruptions are confined to
disposable ``tmp_path`` projects, and ``.firn`` writes use only the paths the
helper explicitly permits for fault injection (``glacier.db``,
``layers/*.firn``).

Journeys follow docs/storage.md §6 (recovery playbook) and docs/usage.md:

- whole-project copies are self-contained repositories (storage.md §6 step 5);
- ``restore`` rescues dirty work into a real layer parented on the previous
  head, names it, and moves the branch head (usage.md ``restore``);
- ``export``/``restore --to`` write only into absent or empty directories;
- ignored worktree files are local scratch: never packed, never deleted by
  restore or switch;
- a missing, empty or unreadable ``glacier.db`` is rebuilt from in-band layer
  meta (``verify --rebuild-db``), losing only the names that live solely in
  the database (branch names, pins);
- corrupt, truncated or missing layer files make ``verify`` and the rebuild
  refuse until the original bytes come back;
- stray ``*.tmp``/orphan ``*.firn`` files are crash leftovers: ``verify``
  names the orphans, refused commands never sweep them, the next successful
  layer-writing command does.

Directory-over-file and file-over-directory transitions exercise the
documented contract: clean tracked kind changes succeed, and a tracked
file never replaces a worktree directory that still holds protected
(ignored) local data — the command must refuse *before* touching
anything, leaving the store and the worktree untouched. The observed
v1.0.1 failures (a clean dir→file restore failing, and refusals that
left a partially rewritten worktree or did not name the blocking leaf)
are product defects under test here, not accepted behavior; the
API-level regressions pin the preflight/rollback contract.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from real_world_support import SEED_IGNORE, World, tree_files


def _commit_ids(world: World) -> list[str]:
    return [c["id"] for c in world.json("history")["commits"]]


def _layer_dir(world: World) -> Path:
    return world.path / ".firn" / "layers"


def test_whole_project_copy_is_independent(tmp_path: Path) -> None:
    """storage.md §6.5: the folder including .firn is the repository.

    Copy it under a new name and both halves keep working — the same
    history, then divergent commits that never leak across.
    """
    world = World.create(tmp_path / "original")
    world.write("src/app.txt", "hello\n")
    world.write("data.bin", b"\x00\x01\x02\xfe")
    world.commit("first")
    world.write("src/app.txt", "hello v2\n")
    world.write("README.md", "# demo project\n")
    c2 = world.commit("second")["layer_id"]
    shipped = world.tree()

    copied = World(tmp_path / "renamed copy")
    shutil.copytree(world.path, copied.path)
    # arrival check: the copy verifies, is clean and holds the same bytes
    assert copied.tree() == shipped
    assert copied.verify()["status"] == "ok"
    assert copied.clean()["head"] == {"id": c2, "kind": "branch", "branch": "main"}

    world.write("orig.txt", "only in the original\n")
    orig_c3 = world.commit("original third")["layer_id"]
    copied.write("copy.txt", "only in the copy\n")
    copy_c3 = copied.commit("copy third")["layer_id"]

    assert world.tree() == {**shipped, "orig.txt": b"only in the original\n"}
    assert copied.tree() == {**shipped, "copy.txt": b"only in the copy\n"}
    orig_history = _commit_ids(world)
    copy_history = _commit_ids(copied)
    assert len(orig_history) == len(copy_history) == 3
    assert orig_history[:2] == [orig_c3, c2]
    assert copy_history[:2] == [copy_c3, c2]
    assert orig_c3 not in copy_history
    assert copy_c3 not in orig_history
    assert orig_history[2] == copy_history[2]  # shared first commit
    world.verify()
    copied.verify()


def test_restore_older_head_rescues_drift_and_commits_on_parent(
    tmp_path: Path,
) -> None:
    """usage.md ``restore``: dirty work is packed first, the head moves.

    The rescue layer is a real layer — parented on the pre-restore head —
    and its bytes come back through ``export``.  A never-committed new
    file is also rescued and stays on disk.  Committing afterwards builds
    on the restored parent, and the way back is ``previous_head``.
    """
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]
    m1 = world.tree()
    world.write("a.txt", "two\n")
    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    m2 = world.tree()

    # dirty tracked edit + a brand-new file, then restore the older head
    world.write("a.txt", "edited but uncommitted\n")
    world.write("new.txt", "brand new\n")
    result = world.json("restore", c1)
    assert result["status"] == "restored"
    assert result["layer_id"] == c1
    assert result["previous_head"] == c2
    rescue = result["rescue_layer"]
    assert rescue is not None

    # committed state rolled back to c1; the uncommitted new file survives
    # on disk (it was never tracked, so nothing overwrote it)
    assert world.tree() == {**m1, "new.txt": b"brand new\n"}
    status = world.json("status")
    assert status["clean"] is False
    assert status["added"] == ["new.txt"]
    assert status["head"]["id"] == c1

    # the rescue layer holds the whole pre-restore worktree, exported on demand
    dest = tmp_path / "rescue-export"
    exported = world.json("export", rescue, "--to", str(dest))
    assert exported["status"] == "exported"
    assert exported["layer_id"] == rescue
    assert tree_files(dest) == {
        ".gitignore": SEED_IGNORE,
        "a.txt": b"edited but uncommitted\n",
        "b.txt": b"two\n",
        "new.txt": b"brand new\n",
    }

    # the rescue is a normal layer: a branch on its id shows it parented
    # on the previous head (c2), which is how it stays reachable
    world.json("branch", "rescued", rescue)
    rescue_entry = world.json("history", "rescued")["commits"][0]
    assert rescue_entry["id"] == rescue
    assert rescue_entry["parent"] == c2
    assert rescue_entry["second_parent"] is None

    # committing on the restored parent advances main from c1
    c3 = world.commit("on restored parent")["layer_id"]
    top = world.json("history")["commits"][0]
    assert top["id"] == c3
    assert top["parent"] == c1
    assert top["second_parent"] is None
    assert world.clean()["head"]["id"] == c3

    # the way back: previous_head is still a resolvable layer id
    back = world.json("restore", c2)
    assert back["rescue_layer"] is None
    assert back["previous_head"] == c3
    assert world.tree() == m2
    assert world.clean()["head"]["id"] == c2
    assert world.verify()["layers"] == 4  # c1, c2, rescue, c3


def test_export_and_restore_to_destination_rules(tmp_path: Path) -> None:
    """export/restore --to: absent or empty dirs only, never destructive."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]
    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    m2 = world.tree()

    # absent destination, including a path whose parents do not exist yet
    absent = tmp_path / "exports" / "deep" / "snap"
    out = world.json("export", c1, "--to", str(absent))
    assert out == {"status": "exported", "layer_id": c1, "to": out["to"]}
    assert world.host_path(out["to"]) == absent.resolve()
    assert tree_files(absent) == {".gitignore": SEED_IGNORE, "a.txt": b"one\n"}

    # pre-existing but empty directory is accepted
    empty = tmp_path / "empty-dest"
    empty.mkdir()
    out = world.json("restore", c1, "--to", str(empty))
    assert out["status"] == "exported"
    assert tree_files(empty) == {".gitignore": SEED_IGNORE, "a.txt": b"one\n"}

    # a non-empty destination is refused by both spellings, byte-identical
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_bytes(b"do not touch\n")
    store_before = world.store_bytes()
    proc = world.raw("export", c1, "--to", str(occupied), rc=1)
    assert "not empty" in proc.stderr
    proc = world.raw("restore", c1, "--to", str(occupied), rc=1)
    assert "not empty" in proc.stderr
    assert tree_files(occupied) == {"keep.txt": b"do not touch\n"}
    assert world.store_bytes() == store_before

    # a destination that is an existing FILE is refused the same way —
    # it is neither absent nor an empty directory — and stays intact
    dest_file = tmp_path / "occupied-file"
    dest_file.write_bytes(b"also do not touch\n")
    proc = world.raw("export", c1, "--to", str(dest_file), rc=1)
    assert proc.stderr
    proc = world.raw("restore", c1, "--to", str(dest_file), rc=1)
    assert proc.stderr
    assert dest_file.read_bytes() == b"also do not touch\n"
    assert world.store_bytes() == store_before

    # --to never touches the worktree or the head
    assert world.tree() == m2
    assert world.clean()["head"]["id"] == c2
    world.verify()


def test_ignored_local_files_survive_restore_and_switch(tmp_path: Path) -> None:
    """Ignored files are local scratch: never packed, never removed."""
    world = World.create(tmp_path / "proj")
    world.write(".gitignore", SEED_IGNORE + b"*.log\nkeep/\n")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]
    world.write("debug.log", "noisy local log\n")
    world.write("keep/cache.bin", b"\x00local cache\x00")
    status = world.json("status", "--ignored")
    assert status["ignored"] == ["debug.log", "keep/cache.bin"]
    assert status["clean"] is True
    ignored = {
        "debug.log": b"noisy local log\n",
        "keep/cache.bin": b"\x00local cache\x00",
    }
    m1 = world.tree()
    assert m1 == {
        ".gitignore": SEED_IGNORE + b"*.log\nkeep/\n",
        "a.txt": b"one\n",
        **ignored,
    }

    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    m2 = world.tree()

    # restore removes committed files but leaves ignored scratch alone
    world.json("restore", c1)
    assert world.tree() == {**m1}
    assert world.clean()["head"]["id"] == c1
    world.json("restore", c2)
    assert world.tree() == {**m2}

    # same guarantee through switch (which requires a clean tree)
    world.json("branch", "side", c1)
    out = world.json("switch", "side")
    assert out["kind"] == "branch"
    assert world.tree() == {**m1}
    out = world.json("switch", "main")
    assert out["head"] == c2
    assert world.tree() == {**m2}
    status = world.json("status", "--ignored")
    assert status["ignored"] == ["debug.log", "keep/cache.bin"]
    world.verify()


def test_database_loss_rebuilds_layers_and_recreates_branches(
    tmp_path: Path,
) -> None:
    """Missing glacier.db -> verify --rebuild-db (storage.md §6 step 4).

    The rebuild picks the newest TIP layer — the one no other layer
    descends from; newest created_at, ties by id — computed here from
    ``history --all`` output rather than assumed chronology.  Branch
    names are unknowable (``lost_branches: null``) and are recreated by
    hand from the layer ids the user kept.
    """
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]
    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    world.json("branch", "saved", c1)
    commits = {c["id"]: c for c in world.json("history", "--all")["commits"]}

    # snapshot oracles the recovery must honour
    exp1 = tmp_path / "exp-c1"
    exp2 = tmp_path / "exp-c2"
    world.json("export", c1, "--to", str(exp1))
    world.json("export", c2, "--to", str(exp2))
    m1 = tree_files(exp1)
    m2 = tree_files(exp2)

    parents = {
        p
        for c in commits.values()
        for p in (c["parent"], c["second_parent"])
        if p is not None
    }
    tips = [i for i in commits if i not in parents]
    expected_head = max(tips, key=lambda i: (commits[i]["created_at"], i))
    assert expected_head == c2  # single chain: the last commit is the tip

    (world.path / ".firn" / "glacier.db").unlink()
    proc = world.raw("status", rc=1)
    assert "glacier.db is missing" in proc.stderr
    assert "--rebuild-db" in proc.stderr
    proc = world.raw("verify", rc=1)
    assert "glacier.db is missing" in proc.stderr

    rebuilt = world.json("verify", "--rebuild-db")
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["layers"] == 2
    assert rebuilt["main_head"] == expected_head
    assert rebuilt["lost_branches"] is None
    assert rebuilt["verify"] == {"status": "ok", "layers": 2}
    assert (world.path / ".firn" / "glacier.db").is_file()

    # synthetic main at the newest tip; every named branch was lost
    branches = world.json("list-branches")["branches"]
    assert [(b["name"], b["head"]) for b in branches] == [("main", c2)]
    assert world.clean()["head"] == {"id": c2, "kind": "branch", "branch": "main"}

    # snapshots preserved: the layers still export byte-for-byte
    re1 = tmp_path / "re-exp-c1"
    re2 = tmp_path / "re-exp-c2"
    world.json("export", c1, "--to", str(re1))
    world.json("export", c2, "--to", str(re2))
    assert tree_files(re1) == m1
    assert tree_files(re2) == m2

    # recreate the named branch from the id kept before the loss
    made = world.json("branch", "saved", c1)
    assert made["created"] is True
    assert made["head"] == c1
    names = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert names == {"main": c2, "saved": c1}
    assert [c["id"] for c in world.json("history", "saved")["commits"]] == [c1]

    # the repository is fully usable afterwards
    world.write("c.txt", "three\n")
    c3 = world.commit("c3")["layer_id"]
    assert world.json("history")["commits"][0]["id"] == c3
    assert world.verify()["layers"] == 3


def test_empty_and_unreadable_databases_follow_the_playbook(
    tmp_path: Path,
) -> None:
    """An empty db is re-schematized in place; an unreadable one must be
    backed up and replaced with an empty file first (storage.md §6.4)."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]

    # empty file: store damage (exit 3) until --rebuild-db heals it in place
    world.write(".firn/glacier.db", b"")
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    assert "--rebuild-db" in proc.stderr
    rebuilt = world.json("verify", "--rebuild-db")
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["main_head"] == c1
    assert rebuilt["lost_branches"] is None
    assert world.clean()["head"]["id"] == c1

    # unreadable SQLite: verify and rebuild both name the documented prep
    world.write(".firn/glacier.db", b"this is not sqlite at all" * 8)
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    assert "replace it with an empty file" in proc.stderr
    proc = world.raw("verify", "--rebuild-db", rc=3)
    assert "integrity failure" in proc.stderr

    # the documented recovery: keep the damaged file, substitute an empty one
    damaged = (world.path / ".firn" / "glacier.db").read_bytes()
    (tmp_path / "glacier.db.damaged").write_bytes(damaged)
    world.write(".firn/glacier.db", b"")
    rebuilt = world.json("verify", "--rebuild-db")
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["layers"] == 1
    assert rebuilt["main_head"] == c1
    assert rebuilt["lost_branches"] is None
    assert (tmp_path / "glacier.db.damaged").read_bytes() == damaged
    assert world.clean()["head"]["id"] == c1
    world.verify()


def test_rebuild_on_healthy_database_names_lost_branches(tmp_path: Path) -> None:
    """A readable database makes lost names knowable: sorted minus main."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1 = world.commit("c1")["layer_id"]
    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    world.json("branch", "side", c1)
    world.json("branch", "other")

    rebuilt = world.json("verify", "--rebuild-db")
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["layers"] == 2
    assert rebuilt["main_head"] == c2
    assert rebuilt["lost_branches"] == ["other", "side"]
    assert rebuilt["verify"] == {"status": "ok", "layers": 2}
    names = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert names == {"main": c2}
    # layers and their trees were untouched by the rebuild
    assert world.clean()["head"]["id"] == c2
    dest = tmp_path / "still-there"
    world.json("export", c1, "--to", str(dest))
    assert tree_files(dest) == {".gitignore": SEED_IGNORE, "a.txt": b"one\n"}
    world.verify()


def test_damaged_layer_refuses_verify_and_rebuild_until_repaired(
    tmp_path: Path,
) -> None:
    """Missing, corrupt or truncated layer files: readers fail loudly
    (exit 3), the rebuild refuses, and restoring the saved original
    bytes returns the repository to service (storage.md §6 step 2)."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    c1_id = world.commit("c1")["layer_id"]
    world.write("b.txt", "two\n")
    c2_id = world.commit("c2")["layer_id"]
    world.write("c.txt", "three\n")
    c3_id = world.commit("c3")["layer_id"]
    saved = world.layer_bytes()
    assert len(saved) == 3
    ids = _commit_ids(world)
    assert ids == [c3_id, c2_id, c1_id]

    middle = _layer_dir(world) / f"{c2_id}.firn"

    # (a) missing layer file — a dangling parent blocks the rebuild too
    middle.unlink()
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    assert c2_id[:12] in proc.stderr
    assert world.layer_bytes() == {k: v for k, v in saved.items() if k != middle.name}
    proc = world.raw("verify", "--rebuild-db", rc=3)
    assert "integrity failure" in proc.stderr
    assert "missing" in proc.stderr
    middle.write_bytes(saved[middle.name])
    assert world.verify()["layers"] == 3

    # (b) corrupt bytes — fails its self-hash / cannot be opened
    middle.write_bytes(b"definitely not a layer" * 32)
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    assert c2_id[:12] in proc.stderr
    proc = world.raw("verify", "--rebuild-db", rc=3)
    assert "integrity failure" in proc.stderr
    middle.write_bytes(saved[middle.name])
    assert world.verify()["layers"] == 3

    # (c) truncated stream — damaged member, same refusal behaviour
    middle.write_bytes(saved[middle.name][:40])
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    proc = world.raw("verify", "--rebuild-db", rc=3)
    assert "integrity failure" in proc.stderr
    middle.write_bytes(saved[middle.name])
    assert world.verify()["layers"] == 3

    # refused integrity checks never mutated the store: history is intact
    assert _commit_ids(world) == ids
    assert world.clean()["head"]["id"] == c3_id
    # and the repository continues normally afterwards
    world.write("d.txt", "four\n")
    c4 = world.commit("c4")["layer_id"]
    assert world.json("history")["commits"][0]["parent"] == c3_id
    assert world.verify()["layers"] == 4
    current = world.layer_bytes()
    assert f"{c4}.firn" in current
    assert {k: v for k, v in current.items() if k != f"{c4}.firn"} == saved


def test_stray_tmp_and_orphan_layers_are_reported_then_swept(
    tmp_path: Path,
) -> None:
    """Crash leftovers in layers/: *.tmp is ignorable garbage, a *.firn
    with no database row is an orphan that verify names.  Refused
    commands never sweep; the next successful commit does (storage.md
    §3, §6 step 3)."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "one\n")
    world.commit("c1")
    world.write("b.txt", "two\n")
    c2 = world.commit("c2")["layer_id"]
    layers = _layer_dir(world)

    # a bare *.tmp is invisible to verify — ignorable by construction
    (layers / "stale-write.tmp").write_bytes(b"partial layer junk")
    assert world.verify()["layers"] == 2

    # an orphan *.firn is named by verify
    orphan_name = "f" * 40 + ".firn"
    (layers / orphan_name).write_bytes(b"orphan layer bytes")
    proc = world.raw("verify", rc=3)
    assert "integrity failure" in proc.stderr
    assert "orphan layer file" in proc.stderr

    # refused commands never sweep: both leftovers survive a refusal
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "x").write_bytes(b"busy\n")
    world.raw("export", c2, "--to", str(occupied), rc=1)
    world.raw("restore", c2, "--to", str(occupied), rc=1)
    assert (layers / "stale-write.tmp").is_file()
    assert (layers / orphan_name).is_file()

    # inspection doesn't sweep either
    world.json("status")
    assert (layers / "stale-write.tmp").is_file()
    assert (layers / orphan_name).is_file()

    # the next successful layer-writing command removes both
    world.write("c.txt", "three\n")
    world.commit("c3")
    assert not (layers / "stale-write.tmp").exists()
    assert not (layers / orphan_name).exists()
    assert world.verify()["layers"] == 3


def test_file_and_directory_transitions_across_restore_and_switch(
    tmp_path: Path,
) -> None:
    """Clean tracked kind changes succeed without manual cleanup or new layers."""
    world = World.create(tmp_path / "proj")
    world.write("x", "x was a file\n")
    c1 = world.commit("file x")["layer_id"]
    m1 = world.tree()
    world.raw("branch", "file-version")
    (world.path / "x").unlink()
    world.write("x/inner.txt", "x became a directory\n")
    c2 = world.commit("dir x")["layer_id"]
    m2 = world.tree()
    world.raw("branch", "directory-version")
    layers_before = world.layer_bytes()

    # Restore moves the current branch tip in both directions.
    for target, expected in ((c1, m1), (c2, m2), (c1, m1)):
        out = world.json("restore", target)
        assert out == {
            "status": "restored",
            "layer_id": target,
            "rescue_layer": None,
            "previous_head": c2 if target == c1 else c1,
        }
        assert world.tree() == expected
        assert world.clean()["head"] == {
            "id": target,
            "kind": "branch",
            "branch": "main",
        }
        assert world.layer_bytes() == layers_before

    # Branch selection and explicit pinned history both cross directory/file
    # boundaries; neither is allowed to leave a partial working directory.
    for ref, layer, kind, expected in (
        ("directory-version", c2, "branch", m2),
        ("file-version", c1, "branch", m1),
        (c2, c2, "pin", m2),
        (c1, c1, "pin", m1),
    ):
        assert world.json("switch", ref) == {"ref": ref, "kind": kind, "head": layer}
        assert world.tree() == expected
        expected_head = {"id": layer, "kind": kind}
        if kind == "branch":
            expected_head["branch"] = ref
        assert world.clean()["head"] == expected_head
        assert world.layer_bytes() == layers_before
        world.verify()

    for layer, expected in ((c1, m1), (c2, m2)):
        dest = tmp_path / f"snapshot-{layer}"
        world.json("export", layer, "--to", str(dest))
        assert tree_files(dest) == expected


def test_directory_with_ignored_child_blocks_file_restore(
    tmp_path: Path,
) -> None:
    """A historical file must not erase ignored children at the same path."""
    world = World.create(tmp_path / "proj")
    world.write(".gitignore", SEED_IGNORE + b"x/\n")
    world.write("x", "x was a file\n")
    c1 = world.commit("file x")["layer_id"]
    m1 = world.tree()
    (world.path / "x").unlink()
    world.write("x/precious.txt", "ignored local data\n")
    c2 = world.commit("x ignored away")["layer_id"]
    m2 = world.tree()
    assert world.clean()["head"]["id"] == c2
    assert world.json("status", "--ignored")["ignored"] == ["x/precious.txt"]
    store_before = world.store_bytes()

    proc = world.raw("restore", c1, rc=1)
    assert "x/precious.txt" in proc.stderr
    # nothing was lost: ignored child intact, head and layers unchanged
    assert world.tree() == m2
    assert world.store_bytes() == store_before
    assert world.clean()["head"]["id"] == c2
    assert world.json("status", "--ignored")["ignored"] == ["x/precious.txt"]
    world.verify()

    # c1's bytes remain reachable through the fresh-directory path
    dest = tmp_path / "c1-snapshot"
    world.json("restore", c1, "--to", str(dest))
    assert tree_files(dest) == m1
    # and after the user clears the collision, restore completes
    shutil.rmtree(world.path / "x")
    world.json("restore", c1)
    assert world.tree() == m1
    world.verify()
