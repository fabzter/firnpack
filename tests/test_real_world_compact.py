"""Real-world compaction and archive journeys (plan: Compaction/archive).

Every operation is a real CLI subprocess through ``World``. Oracles are
explicit expected path->bytes maps, precompact layer snapshots, CLI
topology and read-only SQLite cross-checks — never engine code.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from real_world_support import SEED_IGNORE, World, tree_files


def _chain(world: World) -> list[dict]:
    """First-parent chain from the current head, newest first."""
    entries = world.json("history")["commits"]
    by_id = {entry["id"]: entry for entry in entries}
    head = world.json("status")["head"]["id"]
    chain = []
    cursor = head
    while cursor is not None:
        entry = by_id[cursor]
        chain.append(entry)
        cursor = entry["parent"]
    return chain


def _layer_ids(world: World) -> set[str]:
    return {name[: -len(".firn")] for name in world.layer_bytes()}


def _db_layer_rows(world: World) -> tuple[dict[str, tuple], dict[str, str]]:
    """Read-only SQLite cross-check of the documented schema."""
    db = world.path / ".firn" / "glacier.db"
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, parent, second_parent FROM layers").fetchall()
        branches = con.execute("SELECT name, head FROM branches").fetchall()
    finally:
        con.close()
    return {row[0]: (row[1], row[2]) for row in rows}, dict(branches)


def _export(world: World, ref: str, dest: Path) -> dict:
    result = world.json("export", ref, "--to", str(dest))
    assert result["status"] == "exported", result
    return tree_files(dest)


def _assert_store_unchanged(world: World, snapshot: dict) -> None:
    assert world.store_bytes() == snapshot


def _archive_members(world: World, result: dict) -> dict[str, bytes]:
    """The archive zip maps ``<layer_id>.firn`` -> bytes; nothing else."""
    assert result["archive"], result
    path = world.host_path(result["archive"])
    assert path.is_file()
    firn_root = (world.path / ".firn").resolve()
    assert path.resolve() != firn_root and firn_root not in path.resolve().parents
    with zipfile.ZipFile(path) as zf:
        assert all(name.endswith(".firn") for name in zf.namelist())
        return {name: zf.read(name) for name in zf.namelist()}


# ---------------------------------------------------------------- refusals


def test_plan_and_refusals_leave_the_store_untouched(tmp_path):
    """Dry-run plans, invalid --keep and a missing --yes never mutate."""
    world = World.create(tmp_path / "proj")
    world.write("notes.txt", "one\n")
    world.write("assets/blob.bin", bytes(range(64)))
    world.commit("first")
    world.write("notes.txt", "one\ntwo\n")
    world.commit("second")
    world.write("notes.txt", "one\ntwo\nthree\n")
    world.commit("third")
    world.write("assets/blob.bin", bytes(range(128)))
    world.commit("fourth")

    snapshot = world.store_bytes()

    plan = world.json("compact", "--keep", "2", "--dry-run")
    assert plan["status"] == "plan", plan
    assert plan["keep"] == 2
    assert plan["layers_total"] == 4
    assert plan["layers_dropped"] == 2
    assert plan["layers_rewritten"] == 2
    assert plan["off_chain_rewritten"] == 0
    assert plan["layers_garbage_collected"] == 0
    assert plan["branches_remapped"] == []
    assert plan["bytes_reclaimed"] > 0
    assert plan["peak_disk_needed"] > plan["bytes_reclaimed"]
    _assert_store_unchanged(world, snapshot)
    # a plan is repeatable and side-effect free
    assert world.json("compact", "--keep", "2", "--dry-run") == plan
    _assert_store_unchanged(world, snapshot)

    # --keep must be >= 1, in dry-run and real modes alike
    for bad in ("0", "-3"):
        proc = world.raw("compact", "--keep", bad, "--yes", rc=1)
        assert "--keep must be >= 1" in proc.stderr, proc.stderr
        proc = world.raw("compact", "--keep", bad, "--dry-run", rc=1)
        assert "--keep must be >= 1" in proc.stderr, proc.stderr
    # a non-number is a usage error, not a refusal
    proc = world.raw("compact", "--keep", "abc", rc=2)
    assert "invalid" in proc.stderr or "error" in proc.stderr, proc.stderr
    # a real run demands --yes (non-TTY: no prompt is offered)
    proc = world.raw("compact", "--keep", "2", rc=1)
    assert "--yes" in proc.stderr, proc.stderr
    _assert_store_unchanged(world, snapshot)

    # --keep at or past the chain length is a no-op, even without --yes
    for keep in ("4", "99"):
        result = world.json("compact", "--keep", keep)
        assert result == {"status": "nothing_to_compact", "layers": 4}, result
    _assert_store_unchanged(world, snapshot)
    world.verify()


def test_pending_merge_blocks_compact_until_resolved(tmp_path):
    """A durable conflict state refuses compact outright, then clears."""
    world = World.create(tmp_path / "proj")
    world.write("shared.txt", "base\n")
    world.commit("base")
    world.json("branch", "theirs")
    world.json("switch", "theirs")
    world.write("shared.txt", "their version\n")
    world.commit("their work")
    world.json("switch", "main")
    world.write("shared.txt", "our version\n")
    world.commit("our work")

    conflict = world.json("merge", "theirs", rc=1)
    assert conflict["status"] == "conflict", conflict
    assert world.json("status")["merge_pending"] is True

    snapshot = world.store_bytes()
    for args in (
        ("compact", "--keep", "1", "--yes"),
        ("compact", "--keep", "1", "--dry-run"),
    ):
        proc = world.raw(*args, rc=1)
        assert "a merge is pending" in proc.stderr, proc.stderr
        _assert_store_unchanged(world, snapshot)

    aborted = world.json("merge", "--abort")
    assert aborted["status"] == "aborted", aborted
    assert world.json("status")["merge_pending"] is False

    # the pending-merge gate is cleared; the next refusal is a different
    # honest guard — 'theirs' still descends through the collapsed range
    snapshot = world.store_bytes()
    proc = world.raw("compact", "--keep", "1", "--yes", rc=1)
    assert "a merge is pending" not in proc.stderr, proc.stderr
    assert "branch 'theirs'" in proc.stderr, proc.stderr
    _assert_store_unchanged(world, snapshot)

    world.raw("delete-branch", "theirs")
    plan = world.json("compact", "--keep", "1", "--dry-run")
    assert plan["status"] == "plan", plan
    result = world.json("compact", "--keep", "1", "--yes")
    assert result["status"] == "compacted", result
    world.verify()


def test_branch_below_cut_refuses_then_keep_or_delete(tmp_path):
    """A branch head inside the collapsed range blocks compact; a higher
    --keep that includes it rewrites and remaps the branch instead, and
    deleting the branch also clears the refusal."""
    world = World.create(tmp_path / "proj")
    ids = []
    expected = []
    for i in range(6):
        world.write("log.txt", "\n".join(f"line {j}" for j in range(i + 2)) + "\n")
        ids.append(world.commit(f"m{i + 1}")["layer_id"])
        expected.append(world.tree())
    world.json("branch", "side", ids[1])  # side points at m2

    snapshot = world.store_bytes()
    for keep in ("3", "4"):
        proc = world.raw("compact", "--keep", keep, "--yes", rc=1)
        assert "branch 'side' points into the collapsed range" in proc.stderr
        assert "raise --keep" in proc.stderr, proc.stderr
        _assert_store_unchanged(world, snapshot)

    # keep=5 keeps m2: the plan remaps 'side' rather than refusing
    plan = world.json("compact", "--keep", "5", "--dry-run")
    assert plan["status"] == "plan", plan
    assert plan["branches_remapped"] == [{"name": "side", "from": ids[1]}]

    result = world.json("compact", "--keep", "5", "--yes")
    assert result["status"] == "compacted", result
    assert result["branches_remapped"] == [
        {"name": "side", "from": ids[1], "to": result["branches_remapped"][0]["to"]}
    ]
    remapped = result["branches_remapped"][0]["to"]
    assert remapped != ids[1]
    # the branch followed the rewrite: its new head re-parents onto the
    # foundation and keeps the old tree
    side_history = world.json("history", "side")["commits"]
    assert side_history[0]["id"] == remapped
    assert side_history[0]["parent"] is not None
    foundation = next(e for e in side_history if e["parent"] is None)
    assert side_history[0]["parent"] == foundation["id"]
    assert _export(world, remapped, tmp_path / "side-out") == expected[1]
    world.verify()

    # the alternative to a higher keep: delete the branch, then compact
    world.raw("delete-branch", "side")
    result = world.json("compact", "--keep", "4", "--yes")
    assert result["status"] == "compacted", result
    # the deleted branch's tip sat on the collapsed chain itself: it
    # drops with the collapsed set, nothing extra is garbage-collected
    assert result["dropped"] == 2, result
    assert result["garbage_collected"] == 0, result
    assert remapped not in _layer_ids(world)
    world.verify()


# -------------------------------------------------------------- executions


def test_executed_compact_archive_and_exact_storage(tmp_path):
    """Evolving text+binary history compacted with --archive: archive
    members are byte-identical copies of exactly the collapsed and
    garbage-collected layers, surviving ids/topology are exact, and the
    at-rest inventory is layer files + glacier.db + config."""
    world = World.create(tmp_path / "proj")
    tree_at_commit = []
    for i in range(6):
        world.write("notes.txt", "\n".join(f"n{j}" for j in range(i + 2)) + "\n")
        world.write(f"assets/blob{i % 2}.bin", bytes(range((i + 1) * 32)))
        if i % 2:
            world.write("docs/readme.md", f"# draft {i}\n")
        world.commit(f"m{i + 1}")
        tree_at_commit.append(world.tree())
    chain = _chain(world)
    assert len(chain) == 6
    cut_tree_id = chain[2]["tree_id"]  # m4's tree becomes the foundation

    # amend creates an unreachable orphan row/file that compact collects
    amended = world.json("amend", "-m", "m6 revised")
    orphan_id = amended["replaced"]
    tree_at_commit[-1] = world.tree()

    pre_layers = world.layer_bytes()
    assert orphan_id + ".firn" in pre_layers

    archive_dir = tmp_path / "archives"
    plan = world.json("compact", "--keep", "2", "--dry-run")
    assert plan["status"] == "plan", plan
    assert plan["layers_dropped"] == 4
    assert plan["layers_rewritten"] == 2
    assert plan["layers_garbage_collected"] == 1

    result = world.json(
        "compact", "--keep", "2", "--yes", "--archive", str(archive_dir)
    )
    assert result["status"] == "compacted", result
    assert result["kept"] == 3  # 2 kept + foundation
    assert result["dropped"] == 4
    assert result["garbage_collected"] == 1
    assert result["off_chain_rewritten"] == 0
    assert result["branches_remapped"] == []

    collapsed_ids = {entry["id"] for entry in chain[2:]}
    members = _archive_members(world, result)
    # exactly collapsed + garbage-collected layers: no db, no config,
    # no rewritten kept layers
    assert set(members) == {f"{lid}.firn" for lid in collapsed_ids | {orphan_id}}
    for name, data in members.items():
        assert data == pre_layers[name], name

    # surviving store: foundation + rewritten kept pair, exact id set
    live_ids = _layer_ids(world)
    db_rows, db_branches = _db_layer_rows(world)
    assert live_ids == set(db_rows)
    assert len(live_ids) == 3
    assert live_ids.isdisjoint(collapsed_ids | {orphan_id})
    verified = world.verify()
    assert verified["layers"] == len(live_ids)

    # at rest the repo is layer files plus glacier.db and config — not
    # the original commit count + 2
    at_rest = world.store_bytes()
    assert set(at_rest) - {f"layers/{lid}.firn" for lid in live_ids} == {
        "glacier.db",
        "config",
    }
    assert len(at_rest) == len(live_ids) + 2
    assert len(at_rest) != 6 + 2

    # topology: head -> kept -> foundation -> root, tree ids preserved
    history = _chain(world)
    assert len(history) == 3
    assert history[0]["id"] == result["new_head"]
    assert history[1]["parent"] == history[2]["id"]
    assert history[2]["parent"] is None
    assert history[2]["tree_id"] == cut_tree_id
    assert history[2]["message"].startswith("compact: 4 layers collapsed")

    # exported trees of the retained history are unchanged
    assert _export(world, history[0]["id"], tmp_path / "head-out") == tree_at_commit[-1]
    assert _export(world, history[1]["id"], tmp_path / "kept-out") == tree_at_commit[-2]
    assert _export(world, history[2]["id"], tmp_path / "found-out") == tree_at_commit[3]

    status = world.json("status", "--full")
    assert status["clean"], status
    assert status["head"] == {
        "id": result["new_head"],
        "kind": "branch",
        "branch": "main",
    }


def test_collapsed_merge_collects_its_deleted_branch_stub(tmp_path):
    """A stub only becomes garbage once the merge that referenced it is
    itself collapsed: while the merge is kept its second parent is a
    live root (test_kept_merge_edge_* covers that refusal). Here a
    post-merge commit lets --keep 1 collapse the merge; the archive
    carries the stub byte-identically and the foundation holds the
    post-merge tree."""
    world = World.create(tmp_path / "proj")
    world.write("main.txt", "m1\n")
    world.commit("m1")
    world.write("main.txt", "m2\n")
    world.commit("m2")
    world.json("branch", "feat")
    world.json("switch", "feat")
    world.write("feat.txt", "f1\n")
    feat_tip = world.commit("f1")["layer_id"]
    world.json("switch", "main")
    world.write("main.txt", "m3\n")
    world.commit("m3")
    merged = world.json("merge", "feat", "-m", "merge feat")
    assert merged["status"] == "merged", merged
    merge_tree = world.tree()
    world.write("post.txt", "after the merge\n")
    world.commit("post-merge work")
    post_tree = world.tree()
    world.raw("delete-branch", "feat")

    chain = _chain(world)
    assert len(chain) == 5
    pre_layers = world.layer_bytes()
    assert len(pre_layers) == 6  # chain + unreachable feat tip

    plan = world.json("compact", "--keep", "1", "--dry-run")
    assert plan["status"] == "plan", plan
    assert plan["layers_dropped"] == 4
    assert plan["layers_garbage_collected"] == 1

    result = world.json(
        "compact", "--keep", "1", "--yes", "--archive", str(tmp_path / "arc")
    )
    assert result["status"] == "compacted", result
    assert result["kept"] == 2  # 1 kept + foundation
    assert result["dropped"] == 4
    assert result["garbage_collected"] == 1

    members = _archive_members(world, result)
    assert set(members) == {
        f"{lid}.firn" for lid in {e["id"] for e in chain[1:]} | {feat_tip}
    }
    for name, data in members.items():
        assert data == pre_layers[name], name

    live_ids = _layer_ids(world)
    db_rows, _branches = _db_layer_rows(world)
    assert set(db_rows) == live_ids
    assert len(live_ids) == 2  # foundation + rewritten kept head
    history = _chain(world)
    assert len(history) == 2
    assert history[0]["id"] == result["new_head"]
    assert history[0]["second_parent"] is None
    assert history[1]["parent"] is None
    assert history[1]["message"].startswith("compact: 4 layers collapsed")
    assert _export(world, history[1]["id"], tmp_path / "found") == merge_tree
    assert _export(world, history[0]["id"], tmp_path / "head") == post_tree
    world.verify()


def test_kept_merge_edge_below_cut_refuses_then_succeeds(tmp_path):
    """A kept merge whose second parent descends through the collapsed
    range refuses with the merge-edge message; a --keep that covers the
    stub's ancestry rewrites both sides of the edge."""
    world = World.create(tmp_path / "proj")
    for i in range(3):
        world.write("main.txt", f"m{i + 1}\n")
        world.commit(f"m{i + 1}")
    world.json("branch", "feat")
    world.json("switch", "feat")
    world.write("feat.txt", "f1\n")
    feat_tip = world.commit("f1")["layer_id"]
    feat_tree = world.tree()
    world.json("switch", "main")
    world.write("main.txt", "m4\n")
    world.commit("m4")
    merged = world.json("merge", "feat", "-m", "merge feat")
    assert merged["status"] == "merged", merged
    merge_id = merged["layer_id"]
    merge_tree = world.tree()
    world.write("post.txt", "post\n")
    world.commit("post-merge work")
    world.raw("delete-branch", "feat")

    snapshot = world.store_bytes()
    for keep in ("2", "3"):
        proc = world.raw("compact", "--keep", keep, "--yes", rc=1)
        assert feat_tip[:12] in proc.stderr, proc.stderr
        assert "still referenced" in proc.stderr, proc.stderr
        assert "merge edge is cleared only by a higher --keep" in proc.stderr
        _assert_store_unchanged(world, snapshot)

    result = world.json("compact", "--keep", "4", "--yes")
    assert result["status"] == "compacted", result
    assert result["dropped"] == 2
    assert result["off_chain_rewritten"] == 1
    assert result["garbage_collected"] == 0

    history = world.json("history", "--all")["commits"]
    merge_entry = next(e for e in history if e["message"] == "merge feat")
    stub_entry = next(e for e in history if e["message"] == "f1")
    assert merge_entry["id"] != merge_id
    assert stub_entry["id"] != feat_tip
    assert merge_entry["second_parent"] == stub_entry["id"]
    assert stub_entry["parent"] is not None
    # the stub re-parented onto the rewritten m3, not onto a deleted id
    db_rows, _b = _db_layer_rows(world)
    assert stub_entry["parent"] in db_rows

    assert _export(world, merge_entry["id"], tmp_path / "mg") == merge_tree
    assert _export(world, stub_entry["id"], tmp_path / "stub") == feat_tree
    assert merge_id not in _layer_ids(world)
    assert feat_tip not in _layer_ids(world)
    world.verify()


def test_forks_and_stacked_descendants_remap_preserving_trees(tmp_path):
    """Branches on the kept range and stacked descendants on top of them
    join the rewrite: remapped heads, re-parented ancestry, unchanged
    trees."""
    world = World.create(tmp_path / "proj")
    expected = []
    for i in range(6):
        world.write("main.txt", f"m{i + 1}\n")
        world.commit(f"m{i + 1}")
        expected.append(world.tree())
    chain = _chain(world)
    m5 = chain[1]["id"]  # second newest: inside any --keep >= 2 range

    world.json("branch", "at-m5", m5)
    m5_tree = expected[4]

    world.json("branch", "fork", m5)
    world.json("switch", "fork")
    world.write("fork.txt", "fork work\n")
    fork_tip = world.commit("fork work")["layer_id"]
    fork_tree = world.tree()

    world.json("branch", "stacked")
    world.json("switch", "stacked")
    world.write("stacked.txt", "stacked work\n")
    stacked_tip = world.commit("stacked work")["layer_id"]
    stacked_tree = world.tree()

    world.json("switch", "main")
    plan = world.json("compact", "--keep", "3", "--dry-run")
    assert plan["status"] == "plan", plan
    assert plan["off_chain_rewritten"] == 2
    assert {b["name"] for b in plan["branches_remapped"]} == {
        "at-m5",
        "fork",
        "stacked",
    }

    result = world.json("compact", "--keep", "3", "--yes")
    assert result["status"] == "compacted", result
    assert result["dropped"] == 3
    assert result["off_chain_rewritten"] == 2
    assert result["garbage_collected"] == 0
    remaps = {b["name"]: b for b in result["branches_remapped"]}
    assert remaps["at-m5"]["from"] == m5
    assert remaps["fork"]["from"] == fork_tip
    assert remaps["stacked"]["from"] == stacked_tip

    branches = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert branches["at-m5"] == remaps["at-m5"]["to"]
    assert branches["fork"] == remaps["fork"]["to"]
    assert branches["stacked"] == remaps["stacked"]["to"]

    db_rows, _ = _db_layer_rows(world)
    for old in (m5, fork_tip, stacked_tip):
        assert old not in db_rows
        assert f"{old}.firn" not in world.layer_bytes()

    stacked_history = world.json("history", "stacked")["commits"]
    assert stacked_history[0]["id"] == branches["stacked"]
    assert stacked_history[0]["parent"] == branches["fork"]
    assert stacked_history[1]["id"] == branches["fork"]
    assert stacked_history[1]["parent"] == branches["at-m5"]
    foundation = next(e for e in stacked_history if e["parent"] is None)

    assert _export(world, branches["at-m5"], tmp_path / "m5-out") == m5_tree
    assert _export(world, branches["fork"], tmp_path / "f-out") == fork_tree
    assert _export(world, branches["stacked"], tmp_path / "s-out") == stacked_tree
    assert world.json("status", "--full")["clean"]
    world.verify()
    assert foundation["message"].startswith("compact:")


def test_pinned_head_compacts_in_place(tmp_path):
    """An ordinary pinned worktree compacts: the pin and the branch head
    below it both advance onto the rewritten chain."""
    world = World.create(tmp_path / "proj")
    ids = []
    for i in range(4):
        world.write("work.txt", f"rev {i}\n")
        ids.append(world.commit(f"m{i}")["layer_id"])

    switched = world.json("switch", ids[3])
    assert switched["kind"] == "pin", switched
    world.write("work.txt", "pinned work\n")
    pinned_tip = world.commit("work on the pin")["layer_id"]
    assert world.json("status")["head"] == {"id": pinned_tip, "kind": "pin"}
    pinned_tree = world.tree()

    result = world.json("compact", "--keep", "2", "--yes")
    assert result["status"] == "compacted", result
    assert result["dropped"] == 3
    assert result["branches_remapped"] == [
        {
            "name": "main",
            "from": ids[3],
            "to": result["branches_remapped"][0]["to"],
        }
    ]

    status = world.json("status", "--full")
    assert status["clean"], status
    assert status["head"] == {"id": result["new_head"], "kind": "pin"}
    history = _chain(world)
    assert [e["id"] for e in history] == [
        result["new_head"],
        result["branches_remapped"][0]["to"],
        history[2]["id"],
    ]
    assert history[2]["parent"] is None
    assert _export(world, result["new_head"], tmp_path / "out") == pinned_tree
    branches = {b["name"]: b["head"] for b in world.json("list-branches")["branches"]}
    assert branches["main"] == result["branches_remapped"][0]["to"]
    world.verify()


# ---------------------------------------------------------------- archives


def test_same_archive_dir_reuse_never_overwrites_prior_archives(tmp_path):
    """Every returned archive remains distinct and byte-identical after reuse."""
    world = World.create(tmp_path / "proj")
    for i in range(4):
        world.write("pace.txt", f"v{i}\n")
        world.commit(f"m{i}")

    archive_dir = tmp_path / "archives"
    seen: dict[str, bytes] = {}

    for round_ in range(3):
        result = world.json(
            "compact", "--keep", "1", "--yes", "--archive", str(archive_dir)
        )
        assert result["status"] == "compacted", result
        path = world.host_path(result["archive"])
        assert path.is_file()
        assert path.name not in seen, "compact reused an earlier archive path"
        for name, data in seen.items():
            current = archive_dir / name
            assert current.is_file(), f"archive {name} vanished"
            assert current.read_bytes() == data, (
                f"archive {name} was overwritten by a later compact"
            )
        seen[path.name] = path.read_bytes()
        world.write("pace.txt", f"after compact {round_}\n")
        world.commit(f"after compact {round_}")

    assert len(seen) == len(list(archive_dir.glob("*.zip"))) == 3
    world.verify()


def test_quarantined_archive_recovery_rebuilds_parent_closed_chain(tmp_path):
    """Archive content recovery is a rebuild path, not a backup restore:
    restoring a parent-closed subset into a fresh .firn and rebuilding
    recovers that slice of history; a subset whose parents are absent
    refuses to rebuild."""
    world = World.create(tmp_path / "src")
    ids = []
    for i in range(7):
        world.write("g.txt", f"g{i}\n")
        ids.append(world.commit(f"g{i}")["layer_id"])
    amended = world.json("amend", "-m", "g6 revised")
    orphan_id = amended["replaced"]

    result = world.json(
        "compact", "--keep", "2", "--yes", "--archive", str(tmp_path / "arc")
    )
    assert result["garbage_collected"] == 1, result
    members = _archive_members(world, result)
    collapsed_names = {f"{lid}.firn" for lid in ids[:5]}
    assert set(members) == collapsed_names | {f"{orphan_id}.firn"}
    # archived layers are the OLD ids: the rewritten kept layers and the
    # rewritten parents are not in here
    assert result["new_head"] + ".firn" not in members

    # quarantined fixture: restore ONLY the parent-closed collapsed chain
    dst = World.create(tmp_path / "dst")
    for name, data in members.items():
        if name in collapsed_names:
            dst.write(f".firn/layers/{name}", data)
    rebuilt = dst.json("verify", "--rebuild-db")
    assert rebuilt["status"] == "rebuilt", rebuilt
    assert rebuilt["layers"] == 5
    assert rebuilt["main_head"] == ids[4]
    assert rebuilt["lost_branches"] == []
    assert rebuilt["verify"]["status"] == "ok"

    history = dst.json("history")["commits"]
    assert [e["id"] for e in history] == list(reversed(ids[:5]))
    assert history[0]["tips"] == ["main"]
    exported = _export(dst, ids[4], tmp_path / "dst-out")
    assert exported == {".gitignore": SEED_IGNORE, "g.txt": b"g4\n"}

    # a non-parent-closed subset (the GC'd orphan alone) cannot rebuild
    dst2 = World.create(tmp_path / "dst2")
    dst2.write(f".firn/layers/{orphan_id}.firn", members[f"{orphan_id}.firn"])
    proc = dst2.raw("verify", "--rebuild-db", rc=3)
    assert "parent layer file(s) missing" in proc.stderr, proc.stderr


def test_missing_and_orphaned_layer_file_routes(tmp_path):
    """Two distinct file-loss routes: a layer file missing under a live
    DB row fails verify until restored; a file copied in without a row
    is an orphan that verify names and the next layer write sweeps."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "a1\n")
    world.commit("c1")
    world.write("a.txt", "a2\n")
    world.commit("c2")
    world.write("a.txt", "a3\n")
    world.commit("c3")

    layers = world.layer_bytes()
    victim = f"{_chain(world)[1]['id']}.firn"  # the middle layer: removing it
    # exercises the dangling-parent path deterministically
    victim_path = world.path / ".firn" / "layers" / victim
    victim_path.unlink()
    proc = world.raw("verify", rc=3)
    assert victim[: -len(".firn")][:12] in proc.stderr, proc.stderr
    # restore the file under its live row: integrity returns
    world.write(f".firn/layers/{victim}", layers[victim])
    world.verify()

    # a compact-dropped layer restored without its DB row is an orphan:
    # verify names it and a layer-writing command sweeps it
    result = world.json(
        "compact", "--keep", "1", "--yes", "--archive", str(tmp_path / "arc")
    )
    members = _archive_members(world, result)
    assert len(members) == 2
    for name, data in members.items():
        world.write(f".firn/layers/{name}", data)
    proc = world.raw("verify", rc=3)
    assert "orphan layer file" in proc.stderr, proc.stderr

    world.write("a.txt", "a4\n")
    world.commit("c4")
    # foundation + rewritten c3 + c4: the orphan copies were swept
    assert len(world.layer_bytes()) == 3
    world.verify()


def test_archive_dir_inside_firn_is_refused(tmp_path):
    """SPEC §9.9: the archive is exported outside ``.firn/`` —
    human-visible, syncable, and never polluting the repository
    internals. Pointing --archive at ``.firn/archives`` or, worst case,
    ``.firn/layers`` must refuse before touching anything."""
    world = World.create(tmp_path / "proj")
    world.write("a.txt", "a1\n")
    world.commit("c1")
    world.write("a.txt", "a2\n")
    world.commit("c2")
    world.write("a.txt", "a3\n")
    world.commit("c3")

    snapshot = world.store_bytes()
    for target in (".firn/archives", ".firn/layers"):
        proc = world.raw("compact", "--keep", "1", "--yes", "--archive", target, rc=1)
        assert ".firn" in proc.stderr or "administr" in proc.stderr, proc.stderr
        _assert_store_unchanged(world, snapshot)
    world.verify()
