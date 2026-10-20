"""Seeded randomized compact fuzz — the guard classes that hand-built
repros caught (dangling survivor rows, FK failure at COMMIT, the
non-terminating analysis) get a standing oracle here.

Per seed: a random operation sequence (commit, branch, switch, merge,
amend, delete-branch), then `compact` with a random `--keep`. Oracle:

- on refusal: layer count and every branch head unchanged, and
  `verify` still ok (refusals are atomic);
- on success: `verify` ok, `PRAGMA foreign_key_check` empty, and every
  surviving branch's tree manifest byte-identical to its pre-compact
  manifest — keyed by branch NAME, since names survive the remap.
  That last check is the one that catches dangling-row and
  lost-content classes directly.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firnpack import FirnpackError, Repo  # noqa: E402


def write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _heads(repo):
    return dict(repo._db.execute("SELECT name, head FROM branches"))


def _manifests(repo, heads):
    return {name: repo._read_layer(head)[1] for name, head in heads.items()}


def _count(repo):
    return repo._db.execute("SELECT COUNT(*) FROM layers").fetchone()[0]


@pytest.mark.parametrize("seed", range(25))
def test_compact_fuzz_oracle(seed, project):
    rng = random.Random(seed)
    repo = Repo.init(project)
    branches = ["main"]
    counter = 0

    def commit():
        nonlocal counter
        counter += 1
        write(project, f"f{counter}.txt", f"seed{seed} step{counter}\n" * counter)
        repo.commit(f"c{counter}")

    commit()  # non-empty starting point
    for _step in range(rng.randint(6, 14)):
        op = rng.choice(
            [
                "commit",
                "commit",
                "commit",
                "branch",
                "switch",
                "merge",
                "amend",
                "delete",
            ]
        )
        try:
            if op == "commit":
                commit()
            elif op == "branch":
                name = f"b{len(branches)}"
                repo.branch(name)
                branches.append(name)
            elif op == "switch":
                repo.switch(rng.choice(branches))
            elif op == "merge":
                current = repo._worktree()["head_branch"]
                others = [b for b in branches if b != current]
                if others:
                    result = repo.merge(rng.choice(others))
                    if result["status"] == "conflict":
                        repo.merge_abort()
            elif op == "amend":
                write(project, f"a{counter}.txt", f"amend {counter}\n")
                repo.amend(message=f"amended {counter}")
            elif op == "delete":
                current = repo._worktree()["head_branch"]
                victims = [b for b in branches if b != current and b != "main"]
                if victims:
                    name = rng.choice(victims)
                    with repo._db:
                        repo._db.execute("DELETE FROM branches WHERE name = ?", (name,))
                    branches.remove(name)
        except FirnpackError:
            continue  # a guard refusing mid-sequence is legal fuzz input

    heads_before = _heads(repo)
    manifests_before = _manifests(repo, heads_before)
    count_before = _count(repo)
    keep = rng.randint(1, max(1, count_before))
    try:
        repo.compact(keep, yes=True)
    except FirnpackError:
        # oracle 1: the refusal is atomic
        assert _heads(repo) == heads_before
        assert _count(repo) == count_before
        assert repo.verify()["status"] == "ok"
    else:
        # oracle 2: the rewrite is sound and content-preserving
        assert repo.verify()["status"] == "ok"
        assert repo._db.execute("PRAGMA foreign_key_check").fetchall() == []
        heads_after = _heads(repo)
        manifests_after = _manifests(repo, heads_after)
        for name in heads_after:
            assert manifests_after[name] == manifests_before[name], (
                f"seed {seed}: branch {name!r} lost tree content in the rewrite"
            )
    repo.close()
