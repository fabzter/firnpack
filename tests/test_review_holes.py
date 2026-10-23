"""Test-hole closures from the owner's review tracker.

- GOLDEN: ``tree_id`` recomputed by an independent SPEC §5 implementation
  and pinned to one literal hex value — any drift in the formula (kind
  byte, exec byte, NUL separator, byte-order sort, truncation) fails
  loudly here instead of silently renumbering every layer.
- RESCUE-PATH: ``restore`` over a drifted worktree and ``merge --abort``
  both pack a drift-rescue layer first. When the drift contains a name the
  read side would refuse, the command must refuse cleanly — one humane
  line, no layer written, worktree and layers directory untouched.
- CONFIG: ``[core] autoname = false`` makes a messageless commit refuse;
  ``[history] reminder_threshold`` fires ``history_reminder`` exactly once
  per crossing; ``[user] name`` becomes the layer author.
- IN-PROCESS ``main()``: a full CLI journey through ``firnpack.main``
  asserting return codes and that stderr never carries a traceback.
- CRISS-CROSS: a DAG whose merge has two lowest common ancestors must
  merge cleanly, lose no file content from either side, pick the
  SPEC §10 base, and produce the same result on two copies of the repo.
- REBUILD: same-second commits must rebuild ``main`` at the newest TIP,
  not at the largest-id layer (the second-granular created_at tie is
  broken by tip-preference, then id).
"""

import getpass
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import Repo  # noqa: E402


def write(root: Path, rel: str, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _tree_digest(root: Path) -> dict:
    """relpath -> sha256 of every regular file below root."""
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _layer_names(project: Path) -> list:
    layers = project / ".firn" / "layers"
    return sorted(p.name for p in layers.iterdir())


# ------------------------------------------------------------------ golden


class TestGoldenTreeId:
    """An independent re-implementation of the SPEC §5 tree_id formula —
    written from the spec text, not from firnpack's source — plus one
    literal hex pin so formula drift is caught even if both
    implementations drifted together."""

    ENTRIES = {
        "plain.txt": (5, b"hello", None, False),
        "run.sh": (7, b"#!/bin/", None, True),
        "empty.txt": (0, b"", None, False),
        "link.lnk": (8, None, "target/x", False),
        "üñïçødé.txt": (4, b"hola", None, False),
    }

    @staticmethod
    def _spec_tree_id(entries) -> str:
        outer = hashlib.sha256()
        for path in sorted(entries, key=lambda p: p.encode("utf-8")):
            _size, content, target, x = entries[path]
            body = content if content is not None else target.encode("utf-8")
            kind = 0x66 if content is not None else 0x6C
            frame = bytes((kind, 0x01 if x else 0x00))
            frame += path.encode("utf-8") + b"\x00" + body
            outer.update(hashlib.sha256(frame).digest())
        return outer.hexdigest()[:40]

    def test_engine_matches_independent_formula(self):
        assert firnpack._hash_tree(self.ENTRIES) == self._spec_tree_id(self.ENTRIES)

    def test_literal_hex_pin(self):
        # computed once from the independent implementation above; a
        # formula change anywhere must update this pin deliberately.
        assert self._spec_tree_id(self.ENTRIES) == (
            "3596698168cee7df252469f31ddbc5211edc27e1"
        )
        assert firnpack._hash_tree(self.ENTRIES) == (
            "3596698168cee7df252469f31ddbc5211edc27e1"
        )


# ------------------------------------------------------------- rescue path


class TestRescuePathRefusal:
    """The drift-rescue layer written before a worktree-clobbering command
    goes through the same name validation as a commit. A name the read
    side would refuse must turn the whole command into a clean refusal —
    not a layer that wedges every future read."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX filename: backslash")
    def test_restore_rescue_refuses_a_backslash_name(self, project, capsys):
        assert firnpack.main(["init"]) == 0
        write(project, "a.txt", "v1\n")
        assert firnpack.main(["commit", "-m", "v1"]) == 0
        write(project, "a.txt", "v2\n")
        assert firnpack.main(["commit", "-m", "v2"]) == 0
        repo = Repo(project)
        older = repo.history()["commits"][-1]["id"]
        repo.close()
        write(project, "bad\\name.txt", "drift\n")
        before = _tree_digest(project)
        before_layers = _layer_names(project)
        rc = firnpack.main(["restore", older])
        err = capsys.readouterr().err

        assert rc == 1
        assert "Traceback" not in err
        assert _layer_names(project) == before_layers
        assert _tree_digest(project) == before

    def test_abort_rescue_refuses_the_reserved_meta_name(self, project, capsys):
        assert firnpack.main(["init"]) == 0
        write(project, "shared.txt", "base\n")
        assert firnpack.main(["commit", "-m", "base"]) == 0
        assert firnpack.main(["branch", "side"]) == 0
        assert firnpack.main(["switch", "side"]) == 0
        write(project, "shared.txt", "side\n")
        assert firnpack.main(["commit", "-m", "side"]) == 0
        assert firnpack.main(["switch", "main"]) == 0
        write(project, "shared.txt", "main\n")
        assert firnpack.main(["commit", "-m", "main"]) == 0
        capsys.readouterr()
        assert firnpack.main(["merge", "side"]) == 1  # conflict
        # drift added after the conflict: the reserved meta member name
        write(project, "__firnpack_meta__", "oops\n")
        before = _tree_digest(project)
        before_layers = _layer_names(project)

        rc = firnpack.main(["merge", "--abort"])
        err = capsys.readouterr().err

        assert rc == 1
        assert "Traceback" not in err
        assert "cannot store" in err
        # no rescue layer file, and the pending merge is still pending
        assert _layer_names(project) == before_layers
        assert _tree_digest(project) == before
        repo = Repo(project)
        try:
            assert repo.status()["merge_pending"] is True
        finally:
            repo.close()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX filename: backslash")
    def test_restore_rescue_refusal_leaves_no_tmp_files(self, project, capsys):
        """The refused write must not strand even a .firn.tmp orphan."""
        assert firnpack.main(["init"]) == 0
        write(project, "a.txt", "v1\n")
        assert firnpack.main(["commit", "-m", "v1"]) == 0
        write(project, "a.txt", "v2\n")
        assert firnpack.main(["commit", "-m", "v2"]) == 0
        repo = Repo(project)
        older = repo.history()["commits"][-1]["id"]
        repo.close()
        write(project, "bad\\name.txt", "drift\n")
        before = _tree_digest(project)
        before_layers = _layer_names(project)

        rc = firnpack.main(["restore", older])
        capsys.readouterr()

        assert rc == 1
        assert _layer_names(project) == before_layers
        assert _tree_digest(project) == before


# ------------------------------------------------------------------ config


class TestConfig:
    def _set_config(self, project: Path, section: str, key: str, value: str):
        cfg = project / ".firn" / "config"
        text = cfg.read_text(encoding="utf-8")
        import re as _re

        pattern = _re.compile(rf"(?m)^({_re.escape(key)}\s*=).*$", _re.MULTILINE)
        assert pattern.search(text), f"{key} not found in config"
        cfg.write_text(pattern.sub(rf"\g<1> {value}", text), encoding="utf-8")

    def test_autoname_false_refuses_messageless_commit(self, project, capsys):
        assert firnpack.main(["init"]) == 0
        write(project, "a.txt", "x\n")
        self._set_config(project, "core", "autoname", "false")
        layers = project / ".firn" / "layers"
        rc = firnpack.main(["commit"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "Traceback" not in err
        assert "commit needs a message" in err
        assert not list(layers.glob("*.firn"))  # nothing was written
        # an explicit -m still works
        assert firnpack.main(["commit", "-m", "named"]) == 0
        assert len(list(layers.glob("*.firn"))) == 1

    def test_reminder_fires_once_per_threshold_crossing(self, project, capsys):
        assert firnpack.main(["init"]) == 0
        self._set_config(project, "history", "reminder_threshold", "2")
        write(project, "a.txt", "1\n")

        def commit():
            capsys.readouterr()
            assert firnpack.main(["commit", "--json"]) == 0
            return json.loads(capsys.readouterr().out)

        assert "history_reminder" not in commit()  # layer 1 < 2
        write(project, "a.txt", "2\n")
        second = commit()  # layer 2 crosses the threshold
        assert "history_reminder" in second
        assert second["history_reminder"]["layers"] == 2
        write(project, "a.txt", "3\n")
        # layer 3: already past the threshold — no repeat reminder
        assert "history_reminder" not in commit()

    def test_configured_user_name_becomes_layer_author(self, project):
        assert firnpack.main(["init"]) == 0
        cfg = project / ".firn" / "config"
        cfg.write_text(
            "[user]\nname = María Curie\n"
            "[core]\nprofile = gzip\nautoname = true\n"
            "[merge]\nblock_unresolved_markers = true\n"
            "[history]\nreminder_threshold = 50\n",
            encoding="utf-8",
        )
        write(project, "a.txt", "x\n")
        repo = Repo(project)
        try:
            layer_id = repo.commit("c1")["layer_id"]
            author = repo._db.execute(
                "SELECT author FROM layers WHERE id = ?", (layer_id,)
            ).fetchone()[0]
        finally:
            repo.close()
        assert author == "María Curie"
        assert author != getpass.getuser()


# ------------------------------------------------------------ main() drive


class TestMainInProcess:
    """Every user-facing command driven through ``firnpack.main`` in the
    same process — the contract is the return code and a traceback-free
    stderr, not a subprocess. (``history`` returns ``None`` on success;
    ``sys.exit(None)`` is exit 0.)"""

    def test_full_journey_return_codes(self, project, capsys):
        def run(*argv):
            capsys.readouterr()
            rc = firnpack.main(list(argv))
            captured = capsys.readouterr()
            assert "Traceback" not in captured.err, (
                f"firnpack {' '.join(argv)} leaked a traceback:\n{captured.err}"
            )
            return rc, captured.out, captured.err

        rc, out, _ = run("init")
        assert rc == 0 and "packed glacier ready" in out

        write(project, "a.txt", "v1\n")
        write(project, "shared.txt", "base\n")
        rc, _, _ = run("commit", "-m", "c1")
        assert rc == 0

        rc, out, _ = run("status", "--json")
        assert rc == 0
        assert json.loads(out)["clean"] is True

        rc, out, _ = run("status")
        assert rc == 0 and "clean" in out

        rc, out, _ = run("history", "--json")
        assert rc in (0, None)  # _cmd_history returns None; sys.exit -> 0
        assert len(json.loads(out)["commits"]) == 1

        rc, out, _ = run("history")
        assert rc in (0, None) and "c1" in out

        rc, _, _ = run("branch", "side")
        assert rc == 0
        rc, _, _ = run("switch", "side")
        assert rc == 0
        write(project, "shared.txt", "side\n")
        rc, _, _ = run("commit", "-m", "c2")
        assert rc == 0
        rc, _, _ = run("switch", "main")
        assert rc == 0
        write(project, "shared.txt", "main\n")
        rc, _, _ = run("commit", "-m", "c3")
        assert rc == 0

        # conflicted merge -> exit 1; abort -> exit 0
        rc, _, _ = run("merge", "side")
        assert rc == 1
        rc, out, _ = run("merge", "--abort")
        assert rc == 0 and "merge aborted" in out

        rc, _, _ = run("verify")
        assert rc == 0

        export_dir = project.parent / "export-dest"
        rc, _, _ = run("export", "HEAD", "--to", str(export_dir))
        assert rc == 1  # HEAD is not a ref firnpack resolves
        # export by layer-id prefix instead
        repo = Repo(project)
        head = repo._worktree()["head_id"]
        repo.close()
        rc, out, _ = run("export", head[:8], "--to", str(export_dir))
        assert rc == 0 and (export_dir / "shared.txt").exists()

        rc, _, _ = run("delete-branch", "side")
        assert rc == 0
        rc, out, _ = run("compact", "--keep", "1", "--dry-run")
        assert rc == 0 and "compact plan" in out

        # an integrity failure surfaces as exit 3, not a traceback
        db = project / ".firn" / "glacier.db"
        db.write_bytes(b"not a sqlite file")
        rc, _, _ = run("status")
        assert rc == 3


# ------------------------------------------------------------- criss-cross


class TestCrissCrossMerge:
    """SPEC §10: base = LCA(theirs-head, ours-head) over the parent DAG by
    BFS from theirs — with two LCAs (criss-cross) the first one the
    theirs-side walk reaches wins. The test builds:

        B -- forks to A1 (main) and C1 (feature)
        A1 (main)            C1 (feature)
        |                    |
        M_A = merge C1       M_C = merge A1
        |                    |
        A3                   C3

    Merging feature (C3) into main (A3) has two lowest common ancestors,
    A1 and C1; the theirs-side BFS reaches C1 first, so C1 is the merge
    base. ``both.txt`` pins this end to end: its value matches C1's tree
    on the theirs side but differs from A1's, so the merge stays clean
    only when C1 is chosen.
    """

    def _build(self, root: Path) -> dict:
        repo = Repo.init(root)
        write(root, "a.txt", "a0\n")
        write(root, "c.txt", "c0\n")
        write(root, "both.txt", "b0\n")
        write(root, "shared.txt", "shared0\n")
        repo.commit("base")
        repo.branch("feature")
        # main: touch a.txt only
        write(root, "a.txt", "a-main\n")
        a1 = repo.commit("A1")["layer_id"]
        # feature: touch c.txt AND both.txt
        repo.switch("feature")
        write(root, "c.txt", "c-feat\n")
        write(root, "both.txt", "bfeat\n")
        c1 = repo.commit("C1")["layer_id"]
        # feature merges A1 -> M_C (clean: disjoint changes)
        res = repo.merge(a1[:12])
        assert res["status"] == "merged"
        # feature: files only feature has
        write(root, "g.txt", "theirs-gg\n")
        write(root, "post.txt", "theirs post\n")
        c3 = repo.commit("C3")["layer_id"]
        # main merges C1 -> M_A (clean)
        repo.switch("main")
        res = repo.merge(c1[:12])
        assert res["status"] == "merged"
        # main: only-ours file + the discriminating rewrite of both.txt
        write(root, "both.txt", "b-final-ours\n")
        write(root, "mainpost.txt", "ours post\n")
        a3 = repo.commit("A3")["layer_id"]
        repo.close()
        return {"a1": a1, "c1": c1, "a3": a3, "c3": c3}

    @staticmethod
    def _snapshot(root: Path) -> dict:
        return {
            rel: (root / rel).read_bytes()
            for rel in (
                "a.txt",
                "c.txt",
                "both.txt",
                "shared.txt",
                "g.txt",
                "post.txt",
                "mainpost.txt",
            )
        }

    def test_two_lca_merge_is_clean_and_loses_nothing(self, project, monkeypatch):
        ids = self._build(project)
        # determinism harness: a byte-identical copy of the repo gets the
        # same merge; identical inputs must produce identical results
        copy = project.parent / (project.name + "-copy")
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(project, copy)

        # the two candidate bases: the BFS-from-theirs LCA must be C1
        repo = Repo(project)
        assert repo._lca(ids["a3"], ids["c3"]) == ids["c1"], (
            "SPEC §10 base rule: first common ancestor reached walking "
            "up from theirs-head — in this DAG that is C1, not A1"
        )
        frozen = "2026-10-23T00:00:00+00:00"
        monkeypatch.setattr(firnpack, "_utcnow", lambda: frozen)
        result = repo.merge("feature")
        assert result["status"] == "merged", (
            f"criss-cross merge must be clean: {result}"
        )
        # content invariants — nothing from either side is lost, and the
        # discriminating file proves the base was C1 (both.txt equals
        # C1's tree on theirs, so only-ours-changed -> ours wins; under
        # base A1 both sides would have changed it -> conflict)
        assert (project / "a.txt").read_text() == "a-main\n"
        assert (project / "c.txt").read_text() == "c-feat\n"  # theirs
        assert (project / "both.txt").read_text() == "b-final-ours\n"
        assert (project / "shared.txt").read_text() == "shared0\n"
        assert (project / "g.txt").read_text() == "theirs-gg\n"
        assert (project / "post.txt").read_text() == "theirs post\n"
        assert (project / "mainpost.txt").read_text() == "ours post\n"
        merge_id = result["layer_id"]
        row = repo._db.execute(
            "SELECT parent, second_parent FROM layers WHERE id = ?",
            (merge_id,),
        ).fetchone()
        assert row == (ids["a3"], ids["c3"])
        repo.close()

        # same merge on the copy, same frozen clock: identical layer_id
        repo2 = Repo(copy)
        try:
            result2 = repo2.merge("feature")
        finally:
            repo2.close()
        assert result2["status"] == "merged"
        assert result2["layer_id"] == merge_id, (
            "same inputs must produce the same merge layer — the merge "
            "is nondeterministic"
        )
        for rel, data in self._snapshot(copy).items():
            assert (project / rel).read_bytes() == data
        shutil.rmtree(copy)


# ------------------------------------------------------------------ rebuild


class TestRebuildTipTieBreak:
    """created_at is second-granular: two commits in the same second tie.
    The rebuild must still pick the newest TIP (a layer nothing descends
    from) — SPEC 13, Q9 — not the largest id, which can be a parent."""

    def test_same_second_tip_beats_larger_id(self, project, monkeypatch):
        monkeypatch.setattr(firnpack, "_utcnow", lambda: "2026-10-23T00:00:00+00:00")
        repo = Repo.init(project)
        ids = []
        tip = None
        # extend the chain until the newest tip does NOT win the naive
        # (created_at, id) max — expected within a couple of commits
        for i in range(40):
            write(project, f"f{i}.txt", "x\n")
            tip = repo.commit(f"c{i}")["layer_id"]
            ids.append(tip)
            if len(ids) > 1 and tip < max(ids[:-1]):
                break
        else:
            pytest.fail("40 same-second commits never beat the naive max")
        repo.close()

        (project / ".firn" / "glacier.db").unlink()
        repo = Repo(project, recovering=True)
        try:
            result = repo.rebuild_db()
        finally:
            repo.close()
        assert result["status"] == "rebuilt"
        assert result["main_head"] == tip, (
            "same-second rebuild must prefer the tip layer, not the "
            "largest id (a parent would silently roll history back)"
        )
