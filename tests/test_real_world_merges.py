"""Real-world merge journeys through the CLI only.

Every step is a real subprocess driven by ``World``; the expected trees,
parent/second-parent links, branch tips and stored bytes are independent
oracles — nothing here calls into the engine.

Journeys: fast-forward then a divergent clean two-parent merge; a text
conflict refused by leftover markers then resolved by hand into a merge
commit; a half-finished resolution rescued by ``merge --abort`` with
ignored files preserved; the binary both-changed guard decided by
exporting theirs or by an explicit keep-ours override; delete/modify
conflicts in both directions; marker-shaped example text that may be
packed once but then blocks layer-writing commits and amends while
no-ops still work; a clean merge blocked by marker content leaving a
recoverable pending merge; and the pending-merge fences (switch,
worktree restore, amend, merge, compact refuse; inspection, export and
``restore --to`` keep working).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from real_world_support import SEED_IGNORE, World, tree_files

GITIGNORE = ".gitignore"
PENDING_MSGS = {
    "switch": "before switching",
    "restore": "before restoring",
    "amend": "before amending",
    "merge": "a merge is already pending",
    "compact": "before compacting",
}


def _commits(world: World, *extra: str) -> list[dict]:
    return world.json("history", *extra)["commits"]


def _commit_index(world: World) -> dict[str, dict]:
    """All commits reachable from any branch tip, keyed by layer id."""
    return {c["id"]: c for c in _commits(world, "--all")}


def _head_id(world: World) -> str:
    head = world.json("status")["head"]
    assert head is not None, "expected a non-empty repository"
    return head["id"]


def _branch_heads(world: World) -> dict[str, str]:
    rows = world.json("list-branches")["branches"]
    return {row["name"]: row["head"] for row in rows}


def _layer_row(world: World, layer_id: str) -> dict:
    """Read-only cross-check of stored parentage for a layer the branch
    tips cannot reach (rescue layers are orphans by design)."""
    db = world.path / ".firn" / "glacier.db"
    uri = db.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT parent, second_parent, message FROM layers WHERE id = ?",
            (layer_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no layers row for {layer_id}"
    return {"parent": row[0], "second_parent": row[1], "message": row[2]}


def _assert_layers_preserved(before: dict[str, bytes], world: World) -> set[str]:
    """Old layer files are immutable; returns the ids added since."""
    after = world.layer_bytes()
    for name, data in before.items():
        assert after.get(name) == data, f"layer file {name} changed or vanished"
    return set(after) - set(before)


def _assert_merge_layer(
    world: World, merge_id: str, parent: str, second_parent: str
) -> dict:
    record = _commit_index(world)[merge_id]
    assert record["parent"] == parent
    assert record["second_parent"] == second_parent
    return record


def _conflict(world: World, rel: str = "shared.txt") -> tuple[str, str, dict]:
    """Base on main, divergent edits of `rel` on both branches, then a
    conflicting `merge side`. Returns (ours_id, theirs_id, result)."""
    world.write(rel, "alpha\nbeta\nomega\n")
    base = world.commit("base")["layer_id"]
    world.raw("branch", "side")
    world.raw("switch", "side")
    world.write(rel, "alpha\nside middle\nomega\n")
    world.write("from_side.txt", "from side\n")
    theirs = world.commit("side work")["layer_id"]
    world.raw("switch", "main")
    world.write(rel, "alpha\nmain middle\nomega\n")
    world.write("notes.txt", "main notes\n")
    ours = world.commit("main work")["layer_id"]
    assert base != ours != theirs
    result = world.json("merge", "side", rc=1)
    return ours, theirs, result


class TestMergeShapes:
    """Fast-forward, then a genuinely divergent clean merge."""

    def test_fast_forward_then_divergent_clean_merge(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        world.write("app.py", "print('v1')\n")
        world.write("notes.txt", "version 1\n")
        first = world.commit("start")["layer_id"]

        # A branch that only moves ahead of main: merging it back is a
        # fast-forward — no new layer, the branch tip just advances.
        world.raw("branch", "feature")
        world.raw("switch", "feature")
        world.write("feature_only.txt", "on feature\n")
        feature_tip = world.commit("feature work")["layer_id"]
        world.raw("switch", "main")
        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "app.py": b"print('v1')\n",
            "notes.txt": b"version 1\n",
        }
        layers_before_ff = world.layer_bytes()
        store_before_ff = world.store_bytes()
        ff = world.json("merge", "feature")
        assert ff["status"] == "fast_forwarded"
        assert ff["head"] == feature_tip
        assert ff["kind"] == "branch"
        assert _branch_heads(world) == {"main": feature_tip, "feature": feature_tip}
        assert _head_id(world) == feature_tip
        assert world.tree()["feature_only.txt"] == b"on feature\n"
        # The fast-forward wrote no layer: only the index moved, and every
        # existing layer file kept its bytes.
        assert _assert_layers_preserved(layers_before_ff, world) == set()
        assert world.store_bytes()["glacier.db"] != store_before_ff["glacier.db"]

        # Now the branches genuinely diverge: feature edits the shared
        # file and adds a new one while main edits another.
        world.raw("switch", "feature")
        world.write("notes.txt", "version 1\nfrom feature\n")
        world.write("new_from_them.txt", "theirs\n")
        side_tip = world.commit("feature round two")["layer_id"]
        world.raw("switch", "main")
        world.write("app.py", "print('main')\n")
        main_tip = world.commit("main round two")["layer_id"]
        world.write("~$session.tmp", b"scratch bytes\n")  # ignored by ~$*
        layers_before = world.layer_bytes()

        merged = world.json("merge", "feature", "-m", "merge feature work")
        assert merged == {
            "status": "merged",
            "layer_id": merged["layer_id"],
            "conflicts": [],
        }
        merge_id = merged["layer_id"]
        assert merge_id not in (main_tip, side_tip, feature_tip, first)

        record = _assert_merge_layer(world, merge_id, main_tip, side_tip)
        assert record["message"] == "merge feature work"
        assert _branch_heads(world)["main"] == merge_id
        assert _branch_heads(world)["feature"] == side_tip
        assert _head_id(world) == merge_id
        assert _assert_layers_preserved(layers_before, world) == {f"{merge_id}.firn"}

        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "app.py": b"print('main')\n",
            "feature_only.txt": b"on feature\n",
            "new_from_them.txt": b"theirs\n",
            "notes.txt": b"version 1\nfrom feature\n",
            "~$session.tmp": b"scratch bytes\n",
        }
        world.clean()
        world.verify()

        # Merging again is a no-op: theirs is already an ancestor
        # through the second-parent edge.
        again = world.json("merge", "feature")
        assert again == {"status": "already_up_to_date", "head": merge_id}
        assert world.tree()["notes.txt"] == b"version 1\nfrom feature\n"

    def test_drift_guard_and_abort_without_pending(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        world.write("a.txt", "one\n")
        world.commit("base")
        world.raw("branch", "side")
        world.raw("switch", "side")
        world.write("s.txt", "side file\n")
        world.commit("side")
        world.raw("switch", "main")

        # A dirty worktree blocks the merge before anything is recorded —
        # including the uncommitted bytes themselves (SPEC §10 step 0:
        # "no mutation, nothing written").
        world.write("a.txt", "uncommitted edit\n")
        store_before = world.store_bytes()
        proc = world.raw("merge", "side", rc=1)
        assert "working tree has drift" in proc.stderr
        assert world.json("status")["merge_pending"] is False
        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "a.txt": b"uncommitted edit\n",
        }
        assert world.store_bytes() == store_before

        # No pending merge means --abort has nothing to undo.
        proc = world.raw("merge", "--abort", rc=1)
        assert "no merge is pending" in proc.stderr

        # Reverting the drift unblocks the merge.
        world.write("a.txt", "one\n")
        result = world.json("merge", "side")
        assert result["status"] == "fast_forwarded"
        assert world.tree()["s.txt"] == b"side file\n"
        world.verify()


class TestTextConflicts:
    def test_markers_refuse_then_manual_resolution_commits(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        ours, theirs, result = _conflict(world)
        assert result["status"] == "conflict"
        assert result["head"] == ours
        assert result["theirs"] == theirs
        assert result["conflicts"] == ["shared.txt"]

        marker_bytes = (
            b"<<<<<<< ours\n"
            b"alpha\nmain middle\nomega\n"
            b"=======\n"
            b"alpha\nside middle\nomega\n"
            b">>>>>>> theirs\n"
        )
        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": marker_bytes,
            "notes.txt": b"main notes\n",
            "from_side.txt": b"from side\n",
        }
        st = world.json("status")
        assert st["merge_pending"] is True
        assert st["modified"] == ["shared.txt"]
        assert st["added"] == ["from_side.txt"]
        layers_before = world.layer_bytes()
        store_before = world.store_bytes()

        # The leftover markers refuse the merge-completing commit.
        proc = world.raw("commit", "-m", "done", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["shared.txt"],
        }
        assert "merge conflict" in proc.stderr
        # A refusal wrote nothing: no layer, no index change.
        assert world.layer_bytes() == layers_before
        assert world.store_bytes() == store_before
        assert world.json("status")["merge_pending"] is True

        # Resolving by hand produces a real two-parent merge commit.
        world.write("shared.txt", "alpha\nresolved middle\nomega\n")
        done = world.commit("merge side, resolved by hand")
        merge_id = done["layer_id"]
        _assert_merge_layer(world, merge_id, ours, theirs)
        assert _branch_heads(world)["main"] == merge_id
        assert _branch_heads(world)["side"] == theirs
        assert world.json("status")["merge_pending"] is False

        dest = tmp_path / "merged-out"
        world.json("export", merge_id, "--to", str(dest))
        assert tree_files(dest) == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": b"alpha\nresolved middle\nomega\n",
            "notes.txt": b"main notes\n",
            "from_side.txt": b"from side\n",
        }
        world.verify()

    def test_partial_resolution_abort_rescues_and_preserves_ignored(
        self, tmp_path: Path
    ):
        world = World.create(tmp_path / "w")
        ours, theirs, result = _conflict(world)
        assert result["status"] == "conflict"
        world.write("~$notes.tmp", b"draft lock\n")  # ignored by ~$*

        # The user resolves one file halfway, then gives up.
        world.write("shared.txt", "alpha\nhalf-resolved\nomega\n")
        layers_before = world.layer_bytes()
        aborted = world.json("merge", "--abort")
        assert aborted["status"] == "aborted"
        assert aborted["head"] == ours
        rescue = aborted["rescue_layer"]
        assert rescue is not None

        # The half-finished work is packed under the pre-merge head,
        # reachable only by id; the worktree is back at ours.
        row = _layer_row(world, rescue)
        assert row["parent"] == ours
        assert row["second_parent"] is None
        assert "rescue" in row["message"] and "merge --abort" in row["message"]
        assert rescue not in _commit_index(world)

        dest = tmp_path / "rescued"
        world.json("export", rescue, "--to", str(dest))
        assert tree_files(dest) == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": b"alpha\nhalf-resolved\nomega\n",
            "notes.txt": b"main notes\n",
            "from_side.txt": b"from side\n",
        }

        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": b"alpha\nmain middle\nomega\n",
            "notes.txt": b"main notes\n",
            "~$notes.tmp": b"draft lock\n",
        }
        assert world.json("status")["merge_pending"] is False
        assert _branch_heads(world) == {"main": ours, "side": theirs}
        # Aborted merge added exactly the rescue layer; nothing mutated.
        added = _assert_layers_preserved(layers_before, world)
        assert added == {f"{rescue}.firn"}
        world.clean()
        world.verify()

        # Abort left a coherent repo: the same merge can run again.
        retry = world.json("merge", "side", rc=1)
        assert retry["status"] == "conflict"
        assert retry["theirs"] == theirs
        again = world.json("merge", "--abort")
        assert again["status"] == "aborted" and again["head"] == ours
        assert world.clean()["merge_pending"] is False
        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": b"alpha\nmain middle\nomega\n",
            "notes.txt": b"main notes\n",
            "~$notes.tmp": b"draft lock\n",
        }
        world.verify()

    @pytest.mark.parametrize("decision", ["theirs", "ours"])
    def test_binary_conflict_guard_then_decide(self, tmp_path: Path, decision: str):
        world = World.create(tmp_path / "w")
        world.write("asset.bin", b"\x00base\xff")
        world.write("notes.txt", "base notes\n")
        world.commit("base")
        world.raw("branch", "side")
        world.raw("switch", "side")
        world.write("asset.bin", b"\x00theirs\x01\x02")
        world.write("notes.txt", "notes from side\n")
        theirs = world.commit("side")["layer_id"]
        world.raw("switch", "main")
        world.write("asset.bin", b"\x00ours\x03\x04")
        ours = world.commit("main")["layer_id"]

        result = world.json("merge", "side", rc=1)
        assert result["status"] == "conflict"
        assert result["conflicts"] == ["asset.bin"]
        # The undecided binary keeps OUR bytes in the worktree.
        assert (world.path / "asset.bin").read_bytes() == b"\x00ours\x03\x04"
        # The clean half of the merge is already materialized.
        assert (world.path / "notes.txt").read_bytes() == b"notes from side\n"
        assert world.json("status")["merge_pending"] is True

        # Committing without deciding is refused — ours would silently win.
        proc = world.raw("commit", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_binary_conflict",
            "blocked_by_binary_conflict": ["asset.bin"],
        }
        assert "binary file undecided" in proc.stderr

        if decision == "theirs":
            # The documented route: export their layer, copy the file over.
            dest = tmp_path / "their-export"
            exported = world.json("export", "side", "--to", str(dest))
            assert exported["layer_id"] == theirs
            world.write("asset.bin", (dest / "asset.bin").read_bytes())
            done = world.commit("take their asset")
            expected_asset = b"\x00theirs\x01\x02"
        else:
            proc = world.raw(
                "commit",
                "-m",
                "keep ours on purpose",
                "--allow-binary-conflict",
                "--json",
            )
            done = json.loads(proc.stdout)
            assert done["status"] == "packed"
            assert "keeping our version of undecided binary" in proc.stderr
            expected_asset = b"\x00ours\x03\x04"

        merge_id = done["layer_id"]
        _assert_merge_layer(world, merge_id, ours, theirs)
        assert world.json("status")["merge_pending"] is False
        out = tmp_path / "final"
        world.json("export", merge_id, "--to", str(out))
        assert tree_files(out) == {
            GITIGNORE: SEED_IGNORE,
            "asset.bin": expected_asset,
            "notes.txt": b"notes from side\n",
        }
        world.verify()

    def test_delete_modify_conflicts_both_directions(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        world.write("victim_a.txt", "alpha\n")
        world.write("victim_b.txt", "beta\n")
        world.write("stays.txt", "stay\n")
        world.commit("base")
        world.raw("branch", "side")
        world.raw("switch", "side")
        # side modifies what main will delete, deletes what main modifies
        world.write("victim_a.txt", "alpha from side\n")
        (world.path / "victim_b.txt").unlink()
        theirs = world.commit("side")["layer_id"]
        world.raw("switch", "main")
        (world.path / "victim_a.txt").unlink()
        world.write("victim_b.txt", "beta from main\n")
        ours = world.commit("main")["layer_id"]

        result = world.json("merge", "side", rc=1)
        assert result["status"] == "conflict"
        assert result["conflicts"] == ["victim_a.txt", "victim_b.txt"]
        # The modified side is kept in the worktree on both directions,
        # and the report says which side each survivor came from.
        assert result["kept_sides"] == {
            "victim_a.txt": "theirs",
            "victim_b.txt": "ours",
        }
        assert world.tree() == {
            GITIGNORE: SEED_IGNORE,
            "stays.txt": b"stay\n",
            "victim_a.txt": b"alpha from side\n",
            "victim_b.txt": b"beta from main\n",
        }
        assert world.json("status")["merge_pending"] is True

        # Resolution: honour the delete of victim_a, keep our victim_b.
        (world.path / "victim_a.txt").unlink()
        done = world.commit("resolve delete/modify")
        _assert_merge_layer(world, done["layer_id"], ours, theirs)
        assert world.json("status")["merge_pending"] is False
        out = tmp_path / "resolved"
        world.json("export", done["layer_id"], "--to", str(out))
        assert tree_files(out) == {
            GITIGNORE: SEED_IGNORE,
            "stays.txt": b"stay\n",
            "victim_b.txt": b"beta from main\n",
        }
        world.verify()


class TestMarkerExamples:
    """Marker-shaped prose is allowed once, then blocks the next write."""

    MARKED = (
        "marker glossary\n"
        "\n"
        "<<<<<<< left\n"
        "example region\n"
        "=======\n"
        "example region\n"
        ">>>>>>> right\n"
    )

    def test_allow_once_noop_then_block_commit_and_amend(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        world.write("guide.md", self.MARKED)

        # A plain commit refuses the marker-shaped lines.
        proc = world.raw("commit", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["guide.md"],
        }
        assert "conflict markers" in proc.stderr

        # --allow-markers packs them once, with a warning.
        proc = world.raw("commit", "-m", "glossary", "--allow-markers", "--json")
        packed = json.loads(proc.stdout)
        assert packed["status"] == "packed"
        assert "conflict markers remain in: guide.md" in proc.stderr
        first = packed["layer_id"]

        # The markers really went into the layer.
        dest = tmp_path / "check"
        world.json("export", first, "--to", str(dest))
        assert (dest / "guide.md").read_bytes() == self.MARKED.encode()

        # Nothing-to-pack stays a no-op even with markers on disk —
        # it writes no layer, so the guard does not apply. Same for a
        # nothing-to-amend.
        noop = world.json("commit")
        assert noop["status"] == "nothing_to_pack"
        assert noop["layer_id"] == first
        no_amend = world.json("amend")
        assert no_amend == {"status": "nothing_to_amend", "layer_id": first}

        # But the next layer-writing commit refuses while the committed
        # markers remain, and so does an amend.
        world.write("app.py", "v2\n")
        layers_before = world.layer_bytes()
        proc = world.raw("commit", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["guide.md"],
        }
        proc = world.raw("amend", "-m", "retitle", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["guide.md"],
        }
        assert world.layer_bytes() == layers_before
        assert _head_id(world) == first

        # Removing the marker lines unblocks normal writes.
        world.write("guide.md", "marker glossary\n(seven angle brackets)\n")
        world.commit("resolve glossary")
        assert world.tree()["guide.md"] == b"marker glossary\n(seven angle brackets)\n"
        world.verify()

    def test_clean_merge_blocked_by_markers_leaves_pending(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        world.write("story.txt", "chapter one\n")
        world.commit("base")
        world.raw("branch", "side")
        world.raw("switch", "side")
        world.write("examples.txt", self.MARKED)
        theirs = world.commit("side adds the glossary", "--allow-markers")["layer_id"]
        world.raw("switch", "main")
        world.write("main_notes.txt", "main\n")
        ours = world.commit("main work")["layer_id"]
        # A second divergent branch for the abort path below.
        world.json("branch", "side2", ours)
        world.raw("switch", "side2")
        world.write("more_examples.txt", self.MARKED)
        theirs2 = world.commit("side2 adds more examples", "--allow-markers")[
            "layer_id"
        ]
        world.raw("switch", "main")

        # The three-way merge itself is clean — disjoint files — but the
        # materialized text is marker-shaped, so the completing commit is
        # refused and the merge stays pending.
        proc = world.raw("merge", "side", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload == {
            "status": "blocked_by_markers",
            "blocked_by_markers": ["examples.txt"],
        }
        assert world.json("status")["merge_pending"] is True
        assert (world.path / "examples.txt").read_bytes() == self.MARKED.encode()

        # Recoverable by editing the markers away, then committing.
        world.write("examples.txt", "marker glossary\n(prose only)\n")
        merged = world.commit("merge side")
        _assert_merge_layer(world, merged["layer_id"], ours, theirs)
        assert world.json("status")["merge_pending"] is False
        merge_id = merged["layer_id"]

        # The same block on the other branch is recoverable by abort —
        # and the materialized (uncommitted) merge still gets rescued.
        proc = world.raw("merge", "side2", "--json", rc=1)
        payload = json.loads(proc.stdout)
        assert payload["status"] == "blocked_by_markers"
        assert payload["blocked_by_markers"] == ["more_examples.txt"]
        aborted = world.json("merge", "--abort")
        assert aborted["status"] == "aborted"
        assert aborted["head"] == merge_id
        rescue = aborted["rescue_layer"]
        assert rescue is not None
        row = _layer_row(world, rescue)
        assert row["parent"] == merge_id
        assert row["second_parent"] is None

        dest = tmp_path / "rescued"
        world.json("export", rescue, "--to", str(dest))
        rescued = tree_files(dest)
        assert rescued["more_examples.txt"] == self.MARKED.encode()
        assert rescued["examples.txt"] == b"marker glossary\n(prose only)\n"
        assert rescued["main_notes.txt"] == b"main\n"

        assert world.json("status")["merge_pending"] is False
        assert _head_id(world) == merge_id
        assert "more_examples.txt" not in world.tree()
        assert _branch_heads(world)["side2"] == theirs2
        world.clean()
        world.verify()


class TestPendingMerge:
    """While a merge is pending the mutators refuse; readers keep working."""

    def test_mutators_refuse_readers_work_then_resolve(self, tmp_path: Path):
        world = World.create(tmp_path / "w")
        ours, theirs, result = _conflict(world)
        assert result["status"] == "conflict"
        base = _commit_index(world)[ours]["parent"]
        assert base is not None
        store_before = world.store_bytes()
        pending_tree = world.tree()

        # Every mutating command refuses and changes nothing — neither the
        # store nor the marked-up worktree, which is the merge resolution
        # state itself.
        proc = world.raw("switch", "side", rc=1)
        assert "a merge is pending" in proc.stderr
        assert PENDING_MSGS["switch"] in proc.stderr
        proc = world.raw("restore", base, rc=1)
        assert "a merge is pending" in proc.stderr
        assert PENDING_MSGS["restore"] in proc.stderr
        proc = world.raw("amend", "-m", "nope", rc=1)
        assert "a merge is pending" in proc.stderr
        assert PENDING_MSGS["amend"] in proc.stderr
        proc = world.raw("merge", "side", rc=1)
        assert PENDING_MSGS["merge"] in proc.stderr
        proc = world.raw("compact", "--keep", "1", "--dry-run", rc=1)
        assert "a merge is pending" in proc.stderr
        assert PENDING_MSGS["compact"] in proc.stderr
        proc = world.raw("compact", "--keep", "1", "--yes", rc=1)
        assert PENDING_MSGS["compact"] in proc.stderr
        # branch creation is fenced too: no new branch may be recorded while
        # the merge resolution is unfinished
        proc = world.raw("branch", "escape-hatch", rc=1)
        assert "a merge is pending" in proc.stderr
        assert _branch_heads(world) == {"main": ours, "side": theirs}

        # Inspection, export and restore --to still work mid-merge.
        st = world.json("status")
        assert st["merge_pending"] is True
        assert st["modified"] == ["shared.txt"]
        world.json("status", "--ignored")
        diff = world.json("diff")
        entry = [e for e in diff["entries"] if e["path"] == "shared.txt"]
        assert len(entry) == 1 and entry[0]["status"] == "modified"
        assert "<<<<<<< ours" in entry[0]["hunks"]
        assert _commits(world)[0]["id"] == ours
        assert theirs in _commit_index(world)
        assert _branch_heads(world) == {"main": ours, "side": theirs}
        assert world.verify()["status"] == "ok"

        their_out = tmp_path / "theirs"
        world.json("export", theirs, "--to", str(their_out))
        assert (their_out / "shared.txt").read_bytes() == (
            b"alpha\nside middle\nomega\n"
        )
        # restore --to checks its destination BEFORE the pending-merge
        # refusal: extracting a layer sideways stays allowed.
        base_out = tmp_path / "base-out"
        restored = world.json("restore", base, "--to", str(base_out))
        assert restored["status"] == "exported"
        assert tree_files(base_out) == {
            GITIGNORE: SEED_IGNORE,
            "shared.txt": b"alpha\nbeta\nomega\n",
        }

        # None of it touched the store, the marked-up worktree, or the
        # pending state.
        assert world.store_bytes() == store_before
        assert world.tree() == pending_tree
        assert world.json("status")["merge_pending"] is True
        assert _head_id(world) == ours

        # Resolving ends the pending state and unblocks the mutators.
        world.write("shared.txt", "alpha\nfinal\nomega\n")
        done = world.commit("resolve")
        _assert_merge_layer(world, done["layer_id"], ours, theirs)
        world.raw("switch", "side")  # would have refused a moment ago
        assert _head_id(world) == theirs
        world.raw("switch", "main")
        world.verify()
