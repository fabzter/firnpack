"""Deterministic compact matrix: oracle-checked refusals and rewrites.

Each cell builds a small repository in-process through the Repo API in
``tmp_path`` (``firnpack._utcnow`` is monkeypatched to a counter clock,
so layer ids are deterministic and runs stay fast), then asks
``compact --keep N`` for a dry-run plan and, when permitted, executes it.
Expectations come from an oracle written here from SPEC section 9.9
(reference-based refusal analysis, rewrite closure and remap, garbage
collection) and the ``compact`` section of docs/usage.md — never from
the engine's own helpers.

Repository shape per cell (first-parent chain, oldest first)::

    c1 - c2 - ... - c8 - c9 - M - post0 - ... - post(d-1)
                       \\           /
                        s1 (branch "side")

* merge-depth absent: a straight chain ``c1..c10`` (length 10).
* merge-depth ``d`` in {0, 3}: branch ``side`` forks at ``c8`` with one
  commit ``s1``; main adds ``c9`` and merges side (merge layer ``M``),
  then ``d`` more commits — chain length ``10 + d``. ``s1``'s parent
  ``c8`` sits at first-parent depth ``d + 2`` below head, so the kept
  merge's second parent reaches below the cut exactly when
  ``keep <= d + 2``.
* fork present: one commit ``f1`` on branch ``fork``, cut from the
  deepest kept layer ``chain[keep - 1]`` — a working branch on top of
  kept rows must be rewritten and remapped, never refused.
* delete-feature-branch: ``side`` (and ``fork`` when present) is deleted
  before compacting, leaving a merge stub / fork tip unreachable.

Oracle refusal rule: compact refuses exactly when a *reachable* layer
off the collapsed first-parent chain keeps a parent or second parent
inside the collapsed range. Here only ``s1`` can refuse: it stays
reachable while branch ``side`` lives, or while the merge layer ``M``
(at depth ``d``) is kept and still references it. ``f1`` always parents
a kept layer, so it is rewritten — never a refusal.

That oracle predicts 22 refusals across the 96 cells; engine and oracle
agree on every cell.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import FirnpackError, Repo  # noqa: E402


@pytest.fixture()
def clock(monkeypatch):
    """Deterministic timestamps: a counter clock, not the wall clock."""
    state = {"n": 0}

    def _now():
        state["n"] += 1
        n = state["n"]
        return f"2026-01-01T{n // 3600:02d}:{(n // 60) % 60:02d}:{n % 60:02d}+00:00"

    monkeypatch.setattr(firnpack, "_utcnow", _now)
    return state


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def _first_parent_chain(repo):
    chain = []
    cursor = repo._worktree()["head_id"]
    while cursor is not None:
        chain.append(cursor)
        cursor = repo._db.execute(
            "SELECT parent FROM layers WHERE id = ?", (cursor,)
        ).fetchone()[0]
    return chain


def _delete_branch(repo, name):
    # delete-branch exists only at CLI level; its repository effect is
    # exactly this row delete. These branches never carry the worktree.
    with repo._db:
        repo._db.execute("DELETE FROM branches WHERE name = ?", (name,))


def _build(root, keep, fork, depth, delete):
    root.mkdir()
    repo = Repo.init(root, author="matrix")
    ids = {}

    def commit(msg, path="main.txt"):
        _write(root, path, f"{msg}\n")
        return repo.commit(msg)["layer_id"]

    for i in range(1, 9):
        commit(f"c{i}")
    if depth is None:
        commit("c9")
        commit("c10")
        length = 10
    else:
        repo.branch("side")
        repo.switch("side")
        ids["s1"] = commit("s1", path="side.txt")
        repo.switch("main")
        commit("c9")
        merged = repo.merge("side", message="merge side")
        assert merged["status"] == "merged"
        ids["M"] = merged["layer_id"]
        for i in range(depth):
            commit(f"post{i}")
        length = 10 + depth
    chain = _first_parent_chain(repo)
    assert len(chain) == length
    if fork:
        repo.branch("fork", chain[keep - 1])  # the deepest kept layer
        repo.switch("fork")
        ids["f1"] = commit("f1", path="fork.txt")
        repo.switch("main")
    if delete:
        if depth is not None:
            _delete_branch(repo, "side")
        if fork:
            _delete_branch(repo, "fork")
    return repo, chain, ids


def _oracle(keep, fork, depth, delete, chain_length):
    """Expected outcome derived from SPEC 9.9 and docs/usage.md compact.

    - dropped: every first-parent layer below the cut squashes into the
      foundation — ``len(chain) - keep`` rows.
    - refuse: a reachable layer outside the collapsed set references
      collapsed rows. The only candidate here is ``s1`` (parent at depth
      ``depth + 2``); it stays reachable while ``side`` lives or while
      the kept merge ``M`` (depth ``depth``) points at it.
    - gc: unreachable rows referencing anything that vanishes — the
      deleted ``fork`` tip (its parent is kept) and the deleted ``side``
      tip once ``M`` itself is collapsed (``keep <= depth``).
    - off_chain_rewritten: reachable survivors re-parented through the
      remap — ``f1`` while ``fork`` lives, ``s1`` while reachable.
    - remapped: branch heads sitting on rewritten ids.
    - history: the rewritten chain (foundation + kept) plus ``s1`` when
      it survives the rewrite — ``history`` walks second parents.
    """
    side_alive = depth is not None and not delete
    s1_reachable = depth is not None and (side_alive or keep > depth)
    refuse = bool(s1_reachable and depth is not None and keep <= depth + 2)
    gc = (1 if delete and fork else 0) + (
        1 if delete and depth is not None and keep <= depth else 0
    )
    off_chain = (1 if fork and not delete else 0) + (1 if s1_reachable else 0)
    remapped = (1 if fork and not delete else 0) + (1 if side_alive else 0)
    history = keep + 1 + (1 if depth is not None and keep > depth else 0)
    return {
        "refuse": refuse,
        "dropped": chain_length - keep,
        "off_chain": off_chain,
        "gc": gc,
        "remapped": remapped,
        "history": history,
    }


def _layers_snapshot(repo):
    return {p.name: p.read_bytes() for p in repo.layers_dir.iterdir() if p.is_file()}


def _db_snapshot(repo):
    return {
        table: sorted(repo._db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        for table in ("layers", "branches", "worktree", "refs", "config")
    }


def _worktree_snapshot(root):
    # every tracked file in these fixtures lives at the top level
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def _export_tree(repo, ref, dest):
    """Byte map of a layer's reconstructed worktree content."""
    repo.export(ref, str(dest))
    return {
        str(p.relative_to(dest)): p.read_bytes()
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("delete", [False, True], ids=["keptbranch", "delbranch"])
@pytest.mark.parametrize("depth", [None, 0, 3], ids=["nomerge", "merge0", "merge3"])
@pytest.mark.parametrize("fork", [False, True], ids=["nofork", "fork"])
@pytest.mark.parametrize("keep", range(1, 9))
def test_compact_matrix(tmp_path, clock, keep, fork, depth, delete):
    root = tmp_path / "proj"
    repo, chain, _ids = _build(root, keep, fork, depth, delete)
    heads_before = dict(repo._db.execute("SELECT name, head FROM branches").fetchall())
    before = {
        name: _export_tree(repo, head, tmp_path / "before" / name)
        for name, head in heads_before.items()
    }
    # the cut layer is the oldest layer squashed into the foundation:
    # chain[keep] with the chain ordered newest-first
    cut_map = _export_tree(repo, chain[keep], tmp_path / "before" / "cut")
    layers_before = _layers_snapshot(repo)
    db_before = _db_snapshot(repo)
    tree_before = _worktree_snapshot(root)
    oracle = _oracle(keep, fork, depth, delete, len(chain))

    try:
        plan = repo.compact(keep, dry_run=True)
    except FirnpackError as exc:
        # a refusal is exit-1 territory: FirnpackError itself, never an
        # IntegrityError (which maps to exit 3 and would mean the guard
        # ran too late — after store damage was suspected)
        assert type(exc) is FirnpackError, exc
        assert oracle["refuse"], (
            f"cell keep={keep} fork={fork} depth={depth} delete={delete}: "
            f"oracle predicts success but the engine refused: {exc}"
        )
        # refusals are atomic (docs/usage.md: nothing mutated,
        # .firn/layers/ byte-identical)
        assert _layers_snapshot(repo) == layers_before
        assert _db_snapshot(repo) == db_before
        assert _worktree_snapshot(root) == tree_before
        assert repo.verify()["status"] == "ok"
        assert repo.status()["clean"]
        repo.close()
        return

    assert not oracle["refuse"], (
        f"cell keep={keep} fork={fork} depth={depth} delete={delete}: "
        "oracle predicts refusal but the engine returned a plan "
        f"{plan}"
    )
    assert plan["status"] == "plan"
    assert plan["keep"] == keep
    assert plan["layers_total"] == len(chain)
    # plan-internal consistency: the rewrite set is the kept chain plus
    # the off-chain survivors it reports
    assert plan["layers_rewritten"] == keep + plan["off_chain_rewritten"]
    # a dry run is a read (docs/agent-contract.md section 2): it must
    # leave the store, the index and the worktree untouched
    assert _layers_snapshot(repo) == layers_before
    assert _db_snapshot(repo) == db_before
    assert _worktree_snapshot(root) == tree_before

    executed = repo.compact(keep, yes=True)
    assert executed["status"] == "compacted"
    # the plan promised exactly what the rewrite delivered (SPEC 9.9:
    # the dry-run plan and the result agree — nothing silent)
    assert executed["dropped"] == plan["layers_dropped"]
    assert executed["off_chain_rewritten"] == plan["off_chain_rewritten"]
    assert executed["garbage_collected"] == plan["layers_garbage_collected"]
    assert len(executed["branches_remapped"]) == len(plan["branches_remapped"])
    # the executed numbers match the independent oracle
    assert executed["dropped"] == oracle["dropped"]
    assert executed["off_chain_rewritten"] == oracle["off_chain"]
    assert executed["garbage_collected"] == oracle["gc"]
    assert len(executed["branches_remapped"]) == oracle["remapped"]
    assert executed["kept"] == keep + 1

    assert repo.verify()["status"] == "ok"
    assert repo.status()["clean"]

    heads_after = dict(repo._db.execute("SELECT name, head FROM branches").fetchall())
    assert heads_after.keys() == heads_before.keys()
    for name, head in heads_after.items():
        exported = _export_tree(repo, head, tmp_path / "after" / name)
        assert exported == before[name], (
            f"branch {name!r} lost or changed content in the rewrite"
        )

    # the foundation layer is the cut layer's tree, byte for byte — the
    # sole root after compaction
    roots = repo._db.execute("SELECT id FROM layers WHERE parent IS NULL").fetchall()
    assert len(roots) == 1
    foundation = _export_tree(repo, roots[0][0], tmp_path / "after" / "foundation")
    assert foundation == cut_map

    assert repo.history()["count"] == oracle["history"]

    # no .firn file may be left unreferenced on disk
    referenced = {r[0] for r in repo._db.execute("SELECT id FROM layers")}
    stray = [p.name for p in repo.layers_dir.glob("*.firn") if p.stem not in referenced]
    assert stray == []
    repo.close()
