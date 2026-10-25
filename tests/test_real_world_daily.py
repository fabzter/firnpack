"""Real-world daily-lifecycle journeys (campaign plan: daily group).

Chained multi-session CLI journeys — never a fresh repo per assertion:

* a Unicode-named project holding Office lock files, explicit empty /
  binary / CRLF / BOM bytes: add, edit, rename and delete across three
  commit sessions, with status/diff/history drift oracles and a
  byte-exact export;
* init preserves an existing .gitignore (and so does not seed ``~$*``),
  while a nested .gitignore is reported but not applied;
* no-op commit and amend rewrite nothing / write new immutable layers
  with preserved parents;
* a historical branch, a drift-refused switch, pinned exploratory
  commits, promotion of a pin into a branch, rename/delete protection
  and recovery of a deleted tip by layer id;
* config failures: a wrong-typed value blocks only the writers while
  inspection/export/verify keep working, and malformed INI or invalid
  UTF-8 blocks every command until the file is fixed or removed;
* the history-size reminder at a low threshold and the documented
  compression-profile fallback (zstd request -> recorded gzip layer);
* stored-path boundaries: refusal past the 255-char stored limit, the
  >240-char absolute-path warning, and an unsplittable ustar name below
  the hard limit (documented to pack via pax record).
"""

import shutil

import pytest
from real_world_support import SEED_IGNORE, World, tree_files


def _drift(world):
    """{path: status} for the tracked drift as diff reports it."""
    return {e["path"]: e for e in world.json("diff")["entries"]}


def _heads(world):
    """history id -> commit row, newest first, from the current head."""
    return {c["id"]: c for c in world.json("history")["commits"]}


def _config(name="T", profile="gzip", reminder="50"):
    return (
        f"[user]\nname = {name}\n"
        f"[core]\nprofile = {profile}\nautoname = true\n"
        "[merge]\nblock_unresolved_markers = true\n"
        f"[history]\nreminder_threshold = {reminder}\n"
    )


def test_daily_pack_loop_unicode_project(tmp_path):
    """Three sessions of add/edit/rename/delete under a project path with
    spaces and accents: ignore visibility, explicit-byte files, drift
    oracles, history parentage and a byte-exact export."""
    world = World.create(tmp_path / "informe trimestral — áéí")

    # -- session 1: seed the project --------------------------------------
    world.write("reporte final.txt", "línea 1\nlínea 2\n")
    world.write("números.bin", bytes(range(256)))
    world.write("vacío.txt", b"")
    world.write("plan de trabajo.txt", b"primero\r\nsegundo\r\n")
    world.write("notas/日本語 メモ.txt", b"\xef\xbb\xbfnota con BOM\n")
    world.write("~$reporte.docx", b"lock")
    world.write("sub/.gitignore", "*.txt\n")
    world.write("sub/inner.txt", "the nested ignore must not hide me\n")

    st = world.json("status")
    assert st["head"] is None
    assert st["clean"] is False
    assert st["added"] == [
        ".gitignore",
        "notas/日本語 メモ.txt",
        "números.bin",
        "plan de trabajo.txt",
        "reporte final.txt",
        "sub/.gitignore",
        "sub/inner.txt",
        "vacío.txt",
    ]
    assert st["ignored_count"] == 1
    assert st["ignored"] == ["~$reporte.docx"]
    assert st["nested_gitignore"] is True

    tracked1 = {
        ".gitignore": SEED_IGNORE,
        "reporte final.txt": "línea 1\nlínea 2\n".encode(),
        "números.bin": bytes(range(256)),
        "vacío.txt": b"",
        "plan de trabajo.txt": b"primero\r\nsegundo\r\n",
        "notas/日本語 メモ.txt": b"\xef\xbb\xbfnota con BOM\n",
        "sub/.gitignore": b"*.txt\n",
        "sub/inner.txt": b"the nested ignore must not hide me\n",
    }
    c1 = world.commit("sesión 1 — primer paquete ✓")
    assert c1["files"] == 8
    # the lock file is ignored, not destroyed
    assert world.tree() == {**tracked1, "~$reporte.docx": b"lock"}
    world.clean()

    # -- session 2: edit / rename / delete --------------------------------
    world.write("reporte final.txt", "línea 1\nlínea 2\nlínea 3\n")
    world.write("números.bin", bytes(range(256)) + b"\xde\xad\xbe\xef")
    world.write("archivo vacío.txt", b"")
    (world.path / "notas" / "日本語 メモ.txt").rename(
        world.path / "notas" / "レポート メモ.txt"
    )
    (world.path / "vacío.txt").unlink()

    st = world.json("status", "--full")
    assert st["modified"] == ["números.bin", "reporte final.txt"]
    assert st["added"] == ["archivo vacío.txt", "notas/レポート メモ.txt"]
    assert st["missing"] == ["notas/日本語 メモ.txt", "vacío.txt"]
    assert st["ignored_count"] == 1
    assert st["nested_gitignore"] is True

    entries = _drift(world)
    assert entries["reporte final.txt"]["status"] == "modified"
    assert entries["reporte final.txt"]["binary"] is False
    assert "+línea 3" in entries["reporte final.txt"]["hunks"]
    assert entries["números.bin"]["status"] == "modified"
    assert entries["números.bin"]["binary"] is True
    assert entries["números.bin"]["size"] == "256 -> 260"
    assert entries["notas/日本語 メモ.txt"]["status"] == "deleted"
    assert entries["notas/レポート メモ.txt"]["status"] == "added"
    assert entries["vacío.txt"]["status"] == "deleted"
    assert entries["archivo vacío.txt"]["status"] == "added"

    tracked2 = {
        ".gitignore": SEED_IGNORE,
        "reporte final.txt": "línea 1\nlínea 2\nlínea 3\n".encode(),
        "números.bin": bytes(range(256)) + b"\xde\xad\xbe\xef",
        "plan de trabajo.txt": b"primero\r\nsegundo\r\n",
        "notas/レポート メモ.txt": b"\xef\xbb\xbfnota con BOM\n",
        "sub/.gitignore": b"*.txt\n",
        "sub/inner.txt": b"the nested ignore must not hide me\n",
        "archivo vacío.txt": b"",
    }
    c2 = world.commit("sesión 2 — correcciones")
    assert c2["files"] == 8
    assert world.tree() == {**tracked2, "~$reporte.docx": b"lock"}
    world.clean()

    # -- session 3: history + byte-exact export ----------------------------
    history = _heads(world)
    assert history[c2["layer_id"]]["parent"] == c1["layer_id"]
    assert history[c1["layer_id"]]["parent"] is None
    assert history[c2["layer_id"]]["message"] == "sesión 2 — correcciones"
    assert history[c2["layer_id"]]["second_parent"] is None

    out = tmp_path / "export session 3"
    result = world.json("export", c2["layer_id"][:10], "--to", str(out))
    assert result["status"] == "exported"
    assert tree_files(out) == tracked2  # lock file never travels
    assert world.verify()["layers"] == 2


def test_init_preserves_existing_gitignore_and_nested_only_warns(tmp_path):
    """init seeds ``~$*`` only when no .gitignore exists; a pre-existing
    file is never overwritten — so Office locks are NOT auto-ignored.
    A nested .gitignore is reported by status but its rules are not
    applied (root-only ignore support in v1)."""
    path = tmp_path / "pre seeded"
    path.mkdir()
    world = World(path)
    world.write(".gitignore", b"*.log\n")
    world.write("existing.txt", "already here before init\n")
    world.raw("init")
    assert (path / ".gitignore").read_bytes() == b"*.log\n"

    world.write("debug.log", b"noise\n")  # ignored by the user's rule
    world.write("~$lock.docx", b"lock")  # the ~$* seed never happened
    world.write("docs/.gitignore", b"*.txt\n")
    world.write("docs/inner.txt", "nested rules are not read\n")

    st = world.json("status")
    assert st["ignored_count"] == 1
    assert st["ignored"] == ["debug.log"]
    assert st["nested_gitignore"] is True
    assert st["added"] == [
        ".gitignore",
        "docs/.gitignore",
        "docs/inner.txt",
        "existing.txt",
        "~$lock.docx",
    ]

    tracked = {
        ".gitignore": b"*.log\n",
        "~$lock.docx": b"lock",
        "docs/.gitignore": b"*.txt\n",
        "docs/inner.txt": b"nested rules are not read\n",
        "existing.txt": b"already here before init\n",
    }
    c1 = world.commit("v")
    assert c1["files"] == 5
    assert world.tree() == {**tracked, "debug.log": b"noise\n"}

    out = tmp_path / "out"
    world.json("export", c1["layer_id"], "--to", str(out))
    assert tree_files(out) == tracked

    # init in a directory that already is a repository refuses
    proc = world.raw("init", rc=1)
    assert "already" in proc.stderr


def test_noop_commit_and_amend_preserve_parents_and_old_layers(tmp_path):
    """A commit with an identical tree is a no-op; amend writes a NEW
    immutable layer with the same parent(s) — content amend and
    message-only amend — and the replaced layer's bytes stay on disk."""
    world = World.create(tmp_path / "amend journey")
    world.write("a.txt", "one\n")
    c1 = world.commit("one")
    world.write("a.txt", "two\n")
    c2 = world.commit("two")

    noop = world.json("commit", "-m", "nothing changed")
    assert noop["status"] == "nothing_to_pack"
    assert world.json("history")["count"] == 2

    before_layers = world.layer_bytes()
    world.write("a.txt", "two revised\n")
    amended = world.json("amend", "-m", "two (revised)")
    assert amended["status"] == "amended"
    assert amended["layer_id"] not in {c1["layer_id"], c2["layer_id"]}
    assert amended["replaced"] == c2["layer_id"]

    commits = _heads(world)
    assert commits[amended["layer_id"]]["parent"] == c1["layer_id"]
    assert commits[amended["layer_id"]]["message"] == "two (revised)"
    # write-once: the replaced layer file is still there, byte-identical
    assert set(world.layer_bytes()) == set(before_layers) | {
        f"{amended['layer_id']}.firn"
    }
    for name, blob in before_layers.items():
        assert world.layer_bytes()[name] == blob
    assert world.clean()["head"]["id"] == amended["layer_id"]

    # same tree, new message: still a new layer, same parent
    amended2 = world.json("amend", "-m", "two (revised, again)")
    assert amended2["status"] == "amended"
    assert amended2["replaced"] == amended["layer_id"]
    commits = _heads(world)
    assert commits[amended2["layer_id"]]["parent"] == c1["layer_id"]

    # identical tree and no -m: nothing to amend at all
    same = world.json("amend")
    assert same["status"] == "nothing_to_amend"
    assert same["layer_id"] == amended2["layer_id"]
    assert "replaced" not in same

    assert world.verify()["layers"] == 4  # c1, c2 + two orphan layers


def test_branch_pin_rename_delete_and_old_id_recovery(tmp_path):
    """branch FROM a historical commit; a drifted switch refuses without
    touching store or worktree; pinned commits advance the pin; the pin
    promotes into a branch; rename/delete protections and the unreachable
    warning all hold; a deleted tip is recoverable by layer id."""
    world = World.create(tmp_path / "rama principal")
    world.write("capítulo 1.txt", "uno\n")
    c1 = world.commit("capítulo 1")
    world.write("capítulo 2.txt", "dos\n")
    c2 = world.commit("capítulo 2")

    tracked1 = {
        ".gitignore": SEED_IGNORE,
        "capítulo 1.txt": b"uno\n",
    }
    tracked2 = {**tracked1, "capítulo 2.txt": b"dos\n"}

    # historical branch at the first commit
    branch = world.json("branch", "oldie", c1["layer_id"])
    assert branch["head"] == c1["layer_id"]

    # a dirty switch refuses and mutates nothing
    world.write("capítulo 1.txt", "uno — drift\n")
    store_before = world.store_bytes()
    proc = world.raw("switch", "oldie", rc=1)
    assert "drift" in proc.stderr
    assert world.store_bytes() == store_before
    assert world.tree() == {
        **tracked2,
        "capítulo 1.txt": "uno — drift\n".encode(),
    }
    assert world.json("status", "--full")["modified"] == ["capítulo 1.txt"]

    c3 = world.commit("capítulo 2 — más texto")

    # switching to the historical branch materializes its tree
    switched = world.json("switch", "oldie")
    assert switched["kind"] == "branch"
    assert switched["head"] == c1["layer_id"]
    assert world.tree() == tracked1

    # pin the SECOND commit by id prefix; the worktree follows
    pinned = world.json("switch", c2["layer_id"][:8])
    assert pinned["kind"] == "pin"
    assert pinned["head"] == c2["layer_id"]
    assert world.tree() == tracked2
    st = world.json("status")
    assert st["head"]["kind"] == "pin"
    assert "branch" not in st["head"]

    # exploratory commits while pinned advance the pin
    world.write("exploración.txt", "idea 1\n")
    p4 = world.commit("pinned experiment 1")
    assert world.json("status")["head"]["id"] == p4["layer_id"]
    assert _heads(world)[p4["layer_id"]]["parent"] == c2["layer_id"]
    branches = world.json("list-branches")["branches"]
    assert {b["name"]: b["head"] for b in branches} == {
        "main": c3["layer_id"],
        "oldie": c1["layer_id"],
    }

    world.write("exploración.txt", "idea 1\nidea 2\n")
    p5 = world.commit("pinned experiment 2")
    assert _heads(world)[p5["layer_id"]]["parent"] == p4["layer_id"]

    tracked3 = {**tracked2, "exploración.txt": b"idea 1\nidea 2\n"}
    assert world.tree() == tracked3

    # promote the pin into a real branch and keep working on it
    promoted = world.json("branch", "experiment", p5["layer_id"])
    assert promoted["head"] == p5["layer_id"]
    sw = world.json("switch", "experiment")
    assert sw["kind"] == "branch"
    world.write("experimento.txt", "result\n")
    e1 = world.commit("experimento 1")
    assert _heads(world)[e1["layer_id"]]["parent"] == p5["layer_id"]
    tracked4 = {**tracked3, "experimento.txt": b"result\n"}
    assert world.tree() == tracked4

    # protections: current branch cannot be deleted; existing names are kept
    proc = world.raw("delete-branch", "experiment", rc=1)
    assert "you are on" in proc.stderr
    proc = world.raw("rename-branch", "experiment", "main", rc=1)
    assert "already exists" in proc.stderr
    proc = world.raw("delete-branch", "ghost", rc=1)
    assert "no branch" in proc.stderr

    proc = world.raw("rename-branch", "experiment", "experimento")
    assert proc.returncode == 0
    assert "experimento" in proc.stdout
    assert {b["name"] for b in world.json("list-branches")["branches"]} == {
        "main",
        "oldie",
        "experimento",
    }

    sw = world.json("switch", "main")
    assert sw["head"] == c3["layer_id"]

    # oldie's tip is still reachable from main: deletion stays quiet
    proc = world.raw("delete-branch", "oldie")
    assert proc.returncode == 0
    assert "no longer reachable" not in proc.stderr

    # experimento's tip becomes unreachable: allowed, but loudly warned
    proc = world.raw("delete-branch", "experimento")
    assert proc.returncode == 0
    assert "no longer reachable" in proc.stderr
    assert "firnpack branch experimento" in proc.stderr
    assert {b["name"] for b in world.json("list-branches")["branches"]} == {"main"}

    # nothing is lost: the deleted tip resolves as a pin by its id…
    pinned = world.json("switch", e1["layer_id"][:12])
    assert pinned["kind"] == "pin"
    assert pinned["head"] == e1["layer_id"]
    assert world.tree() == tracked4
    # …and can be turned back into a named branch
    revived = world.json("branch", "recuperado", e1["layer_id"])
    assert revived["head"] == e1["layer_id"]
    sw = world.json("switch", "recuperado")
    assert sw["kind"] == "branch"
    assert world.json("status")["head"]["branch"] == "recuperado"

    assert world.verify()["layers"] == 6


def test_rename_branch_reports_a_missing_source(tmp_path):
    """delete-branch names a missing branch; rename-branch must refuse
    the same way — a no-op rename reported as success is silent loss."""
    world = World.create(tmp_path / "renames")
    world.write("a.txt", "x\n")
    world.commit("v")
    before = world.store_bytes()
    proc = world.raw("rename-branch", "ghost", "renamed", rc=1)
    assert "ghost" in proc.stderr
    assert world.store_bytes() == before


def test_wrong_typed_config_blocks_writers_not_readers(tmp_path):
    """[history] reminder_threshold = abc: commit and amend refuse before
    writing anything, while status/history/verify and export all keep
    working. Fixing the value unblocks the writers."""
    world = World.create(tmp_path / "config types")
    world.write("a.txt", "x\n")
    c1 = world.commit("v")
    world.write(".firn/config", _config(reminder="abc"))

    assert world.json("status")["clean"] is True
    assert _heads(world)[c1["layer_id"]]["message"] == "v"
    assert world.verify()["status"] == "ok"
    out = tmp_path / "still exportable"
    result = world.json("export", c1["layer_id"], "--to", str(out))
    assert result["status"] == "exported"
    assert tree_files(out) == {
        ".gitignore": SEED_IGNORE,
        "a.txt": b"x\n",
    }

    world.write("a.txt", "y\n")
    store_before = world.store_bytes()
    proc = world.raw("commit", "-m", "blocked", rc=1)
    assert "cannot use" in proc.stderr
    assert "reminder_threshold" in proc.stderr
    assert world.store_bytes() == store_before
    proc = world.raw("amend", "-m", "blocked", rc=1)
    assert "cannot use" in proc.stderr
    assert world.store_bytes() == store_before

    world.write(".firn/config", _config(reminder="50"))
    assert world.commit("unblocked")["status"] == "packed"


def test_zstd_profile_falls_back_to_a_gzip_layer(tmp_path):
    """v1.0 ships no zstd writer; a ``profile = zstd`` request falls back
    to gzip and the layer records the real codec (docs/decisions D-05,
    storage.md §3). The bytes must be genuine gzip, not a mislabeled
    zstd stream that readers on 3.13 cannot open."""
    world = World.create(tmp_path / "profiles")
    world.write("a.txt", "x\n")
    world.write(".firn/config", _config(profile="zstd"))
    c1 = world.commit("v")
    blob = world.layer_bytes()[f"{c1['layer_id']}.firn"]
    assert blob[:2] == b"\x1f\x8b"  # gzip magic
    assert world.verify()["status"] == "ok"


def test_unloadable_config_blocks_everything_until_fixed_or_removed(tmp_path):
    """Malformed INI and invalid UTF-8 are load-time failures — even
    read-only commands refuse. Removing the file restores defaults."""
    world = World.create(tmp_path / "config syntax")
    world.write("a.txt", "x\n")
    world.commit("v")

    world.write(".firn/config", b"[user\nname = broken\n")
    for args in (("status",), ("history",), ("verify",)):
        proc = world.raw(*args, "--json", rc=1)
        assert "cannot read" in proc.stderr
    proc = world.raw("commit", "-m", "blocked", rc=1)
    assert "cannot read" in proc.stderr

    world.write(".firn/config", b"\xff\xfe[user]\nname = X\n")
    proc = world.raw("status", "--json", rc=1)
    assert "cannot read" in proc.stderr

    # fixed file -> defaults for missing keys, commands work again
    world.write(".firn/config", "[user]\nname = Fixed\n")
    world.write("a.txt", "y\n")
    assert world.commit("fixed")["status"] == "packed"

    # removed file -> defaults apply; the repo keeps working
    (world.path / ".firn" / "config").unlink()
    assert world.json("status")["clean"] is True
    world.write("a.txt", "more\n")
    assert world.commit("still works")["status"] == "packed"


def test_history_reminder_fires_once_at_the_threshold(tmp_path):
    """[history] reminder_threshold = 2: the reminder appears in the
    commit result exactly on the crossing commit, not before or after.
    The configured [user] name becomes the layer author."""
    world = World.create(tmp_path / "reminders")
    world.write(".firn/config", _config(name="María Curie ñ", reminder="2"))

    world.write("a.txt", "1\n")
    c1 = world.commit("one")
    assert "history_reminder" not in c1
    world.write("a.txt", "2\n")
    c2 = world.commit("two")
    assert c2["history_reminder"]["layers"] == 2
    assert "bytes" in c2["history_reminder"]
    assert "compact" in c2["history_reminder"]["hint"]
    world.write("a.txt", "3\n")
    c3 = world.commit("three")
    assert "history_reminder" not in c3

    commits = _heads(world)
    assert commits[c1["layer_id"]]["author"] == "María Curie ñ"
    assert commits[c3["layer_id"]]["author"] == "María Curie ñ"


def test_stored_path_length_boundary(tmp_path):
    """A stored path over 255 chars refuses the commit before anything
    is written; a path under the limit whose absolute path exceeds 240
    chars packs with a maxpath_warning."""
    world = World.create(tmp_path / "límites")
    world.write("a.txt", "x\n")
    world.commit("v")

    rel_huge = "/".join(["a" * 80, "b" * 80, "c" * 80, "d" * 80, "huge.txt"])
    rel_long = "/".join(["d" * 50, "e" * 50, "f" * 50, "g" * 55, "n.txt"])
    assert len(rel_huge) > 255
    assert 200 < len(rel_long) <= 255
    try:
        world.write(rel_huge, b"too deep\n")
        world.write(rel_long, b"deep\n")
    except OSError as exc:
        pytest.skip(f"fixture filesystem cannot create the path: {exc}")

    # the refusal names the path and writes nothing — not even a .tmp
    store_before = world.store_bytes()
    proc = world.raw("commit", "-m", "too long", rc=1)
    assert "path too long" in proc.stderr
    assert "huge.txt" in proc.stderr
    assert world.store_bytes() == store_before

    # with the over-limit file gone, the long-but-storable path packs
    shutil.rmtree(world.path / rel_huge.split("/")[0])
    c2 = world.commit("deep path")
    # the 214-char stored path makes the absolute path exceed 240 chars
    # under any realistic project root on any host — the warning is
    # unconditional, not an environment-dependent expectation
    assert c2["maxpath_warning"] == [rel_long]
    assert world.clean()["clean"] is True

    out = tmp_path / "deep export"
    world.json("export", c2["layer_id"], "--to", str(out))
    assert tree_files(out) == {
        ".gitignore": SEED_IGNORE,
        "a.txt": b"x\n",
        rel_long: b"deep\n",
    }


def test_unsplittable_ustar_name_below_the_hard_limit_packs(tmp_path):
    """A stored path under the 255-char limit whose layout cannot split
    into ustar's 155-char prefix + 100-char name fields must still pack —
    docs/storage.md §3 promises a pax path record for paths >100 chars,
    and the >240-char absolute-path rule only warns. Documented behavior:
    the commit succeeds and the layer round-trips byte-exactly."""
    world = World.create(tmp_path / "nombres")
    world.write("a.txt", "x\n")
    world.commit("v")

    # a 160-char directory forces any ustar prefix to exceed the 155-char
    # field while the 5-char tail fits the 100-char name field — no legal
    # ustar split exists, but the name is < 255
    rel = "/".join(["x" * 160, "y" * 30, "f.txt"])
    assert len(rel) <= 255
    try:
        world.write(rel, b"deep\n")
    except OSError as exc:
        pytest.skip(f"fixture filesystem cannot create the path: {exc}")

    c2 = world.commit("deep unsplittable")
    assert c2["status"] == "packed"
    # same reasoning as the boundary test: a 197-char stored path means the
    # absolute path always exceeds the 240-char warning threshold
    assert c2["maxpath_warning"] == [rel]
    world.clean()

    out = tmp_path / "deep out"
    world.json("export", c2["layer_id"], "--to", str(out))
    assert tree_files(out) == {
        ".gitignore": SEED_IGNORE,
        "a.txt": b"x\n",
        rel: b"deep\n",
    }
