"""firnpack M1 test suite — implements the SPEC §22 contract.

Every test here pins a documented behavior; docs and code change together
(AGENTS.md §9). Run: python -m pytest tests/ -q
"""

import gzip
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402
from firnpack import FirnpackError, IntegrityError, Repo  # noqa: E402

MODULE = Path(firnpack.__file__).resolve()
PY = sys.executable


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(root: Path, rel: str, data: bytes | str, mode: int | None = None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    p.write_bytes(data)
    if mode is not None:
        p.chmod(mode)
    return p


# ------------------------------------------------------------ init/commit


class TestInit:
    def test_creates_minimal_layout(self, project):
        repo = Repo.init(project)
        firn = project / ".firn"
        assert (firn / "glacier.db").is_file()
        assert (firn / "config").is_file()
        assert (firn / "layers").is_dir()
        repo.close()
        # N_commits + 2 invariant, empty case
        assert len([p for p in firn.rglob("*") if p.is_file()]) == 2

    def test_init_refuses_twice(self, project):
        Repo.init(project).close()
        with pytest.raises(FirnpackError, match="already contains"):
            Repo.init(project)

    def test_commands_refuse_outside_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(
            tmp_path / "empty" if (tmp_path / "empty").exists() else tmp_path
        )
        (tmp_path / "empty").mkdir(exist_ok=True)
        monkeypatch.chdir(tmp_path / "empty")
        with pytest.raises(FirnpackError):
            Repo.discover()


class TestCommit:
    def test_first_commit_and_invariant(self, project):
        write(project, "a.txt", "alpha\n")
        write(project, "sub/b.txt", "beta\n")
        repo = Repo.init(project)
        result = repo.commit("first")
        repo.close()
        assert result["status"] == "packed"
        assert result["files"] == 3  # + seeded .gitignore
        layer = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        assert layer.is_file()
        files = [p for p in (project / ".firn").rglob("*") if p.is_file()]
        assert len(files) == 3  # 2 files at rest + 1 layer

    def test_nothing_to_pack(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("first")
        again = repo.commit("first again")
        repo.close()
        assert again["status"] == "nothing_to_pack"
        files = [p for p in (project / ".firn").rglob("*") if p.is_file()]
        assert len(files) == 3  # no second layer

    def test_identical_record_dedup(self, project):
        """Same tree/message/author/parent, same second -> same layer_id."""
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        first = repo.commit("msg")
        # force identical created_at by patching the clock
        import firnpack as fp

        real = fp._utcnow
        frozen = "2026-10-19T00:00:00+00:00"
        fp._utcnow = lambda: frozen  # type: ignore[assignment]
        try:
            second = repo.commit("msg")
        finally:
            fp._utcnow = real  # type: ignore[assignment]
        repo.close()
        assert first["layer_id"] == second["layer_id"]
        assert second["status"] == "nothing_to_pack"

    def test_layer_is_valid_gzip_tar_with_meta_first(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        result = repo.commit("first")
        repo.close()
        path = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
            names = [m.name for m in tar]
        assert names[0] == firnpack.META_MEMBER
        assert "a.txt" in names

    def test_meta_carries_contract_fields(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        result = repo.commit("first")
        repo.close()
        path = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
            meta = json.loads(tar.extractfile(firnpack.META_MEMBER).read())
        assert meta["layer_id"] == result["layer_id"]
        assert meta["tree_id"] == result["tree_id"]
        assert meta["message"] == "first"
        assert meta["profile"] == "gzip"
        assert meta["format_version"] == 1
        assert meta["parent"] is None

    def test_layer_id_covers_message(self, project):
        """SPEC §5: layer_id hashes the record INCLUDING the message.

        Regression: a message-only difference must change layer_id —
        otherwise amend could never rewrite a message (v0.4 bug class).
        """
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        result = repo.commit("first")
        repo.close()
        path = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
            meta = json.loads(tar.extractfile(firnpack.META_MEMBER).read())
        record = {
            key: meta[key]
            for key in (
                "format_version",
                "tree_id",
                "parent",
                "second_parent",
                "message",
                "author",
                "created_at",
            )
        }
        assert firnpack._layer_id(record) == result["layer_id"]
        altered = dict(record, message="something else")
        assert firnpack._layer_id(altered) != result["layer_id"]

    def test_deterministic_bytes(self, project):
        """Same tree+meta -> byte-identical layer file (gzip mtime=0)."""
        ids = []
        for i in range(2):
            root = project / f"r{i}"
            root.mkdir()
            write(root, "a.txt", "alpha\n")
            repo = Repo.init(root)
            import firnpack as fp

            real = fp._utcnow
            fp._utcnow = lambda: "2026-10-19T00:00:00+00:00"
            try:
                r = repo.commit("same")
            finally:
                fp._utcnow = real
            repo.close()
            ids.append(
                (root / ".firn" / "layers" / f"{r['layer_id']}.firn").read_bytes()
            )
        assert ids[0] == ids[1]

    @pytest.mark.skipif(os.name == "nt", reason="no exec bit on Windows")
    def test_exec_bit_normalized(self, project):
        write(project, "run.sh", "echo hi\n", mode=0o755)
        write(project, "plain.txt", "x\n")
        repo = Repo.init(project)
        result = repo.commit("bits")
        repo.close()
        path = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
            modes = {m.name: m.mode for m in tar}
        assert modes["run.sh"] == 0o755
        assert modes["plain.txt"] == 0o644

    def test_symlink_stored_and_reported(self, project):
        write(project, "real.txt", "data\n")
        link = project / "link.txt"
        link.symlink_to("real.txt")
        repo = Repo.init(project)
        result = repo.commit("with link")
        repo.close()
        assert result["files"] == 3  # + seeded .gitignore
        path = project / ".firn" / "layers" / f"{result['layer_id']}.firn"
        with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
            lm = {m.name: m for m in tar}["link.txt"]
        assert lm.issym()
        assert lm.linkname == "real.txt"

    def test_auto_message(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        r1 = repo.commit()
        assert r1["message"] == "pack 2 files"  # a.txt + seeded .gitignore
        write(project, "a.txt", "alpha\nbeta\n")
        r2 = repo.commit()
        repo.close()
        assert "pack 2 files" in r2["message"]


# ---------------------------------------------------------------- gitignore


class TestGitignore:
    def test_office_locks_excluded_not_hidden(self, project):
        write(project, "doc.docx", "doc\n")
        write(project, "~$doc.docx", "lock\n")
        write(project, ".gitignore", "~$*\n")
        repo = Repo.init(project)
        result = repo.commit("first")
        assert result["files"] == 2  # doc.docx + .gitignore
        st = repo.status()
        repo.close()
        assert st["ignored_count"] == 1
        assert "~$doc.docx" in st["ignored"]

    def test_negation_last_rule_wins(self, project):
        write(project, "keep.log", "x\n")
        write(project, "drop.log", "y\n")
        write(project, ".gitignore", "*.log\n!keep.log\n")
        repo = Repo.init(project)
        r = repo.commit("v")
        st = repo.status()
        repo.close()
        assert r["files"] == 2
        assert "drop.log" in st["ignored"]
        assert "keep.log" not in st["ignored"]

    def test_dir_pattern_excludes_tree(self, project):
        write(project, "build/out.txt", "x\n")
        write(project, "src/main.txt", "y\n")
        write(project, ".gitignore", "build/\n")
        repo = Repo.init(project)
        r = repo.commit("v")
        st = repo.status()
        repo.close()
        assert r["files"] == 2
        assert "build/out.txt" in st["ignored"]

    def test_firn_dir_auto_ignored(self, project):
        write(project, "a.txt", "x\n")
        repo = Repo.init(project)
        repo.commit("v")
        files, symlinks, _ig, _nested = repo._walk()
        repo.close()
        assert "a.txt" in files
        assert all(not f.startswith(".firn") for f in files)

    def test_nested_gitignore_warned_not_read(self, project):
        write(project, "a.txt", "x\n")
        write(project, "sub/.gitignore", "*.txt\n")
        write(project, "sub/inner.txt", "y\n")
        repo = Repo.init(project)
        r = repo.commit("v")
        st = repo.status()
        repo.close()
        assert st["nested_gitignore"] is True
        assert r["files"] == 4
        # a.txt + seeded .gitignore + sub/.gitignore + sub/inner.txt
        # (nested rules are NOT read in v1 — inner.txt stays tracked)


# ------------------------------------------------------------------ status


class TestStatus:
    def test_mtime_touch_stays_clean(self, project):
        """The §22 mtime test: touch without content change -> clean."""
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("v")
        st = os.stat(project / "a.txt")
        os.utime(project / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 1))
        result = repo.status()
        repo.close()
        assert result["clean"] is True
        assert result["modified"] == []

    def test_modified_detected(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("v")
        write(project, "a.txt", "beta\n")
        result = repo.status()
        repo.close()
        assert result["modified"] == ["a.txt"]

    def test_added_and_missing(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("v")
        write(project, "b.txt", "new\n")
        (project / "a.txt").unlink()
        result = repo.status()
        repo.close()
        assert result["added"] == ["b.txt"]
        assert result["modified"] == []  # added files are never also modified
        assert result["missing"] == ["a.txt"]

    def test_new_symlink_is_added_not_modified(self, project):
        """A brand-new symlink must appear in `added` only — the symlink
        loop previously double-reported it as modified too."""
        write(project, "real.txt", "data\n")
        repo = Repo.init(project)
        repo.commit("base")
        (project / "link.txt").symlink_to("real.txt")
        result = repo.status()
        repo.close()
        assert result["added"] == ["link.txt"]
        assert result["modified"] == []
        assert result["missing"] == []

    def test_empty_repo_status(self, project):
        repo = Repo.init(project)
        result = repo.status()
        repo.close()
        assert result["head"] is None
        # the seeded .gitignore exists but is not yet packed
        assert result["added"] == [".gitignore"]
        assert result["clean"] is False


# ----------------------------------------------------------------- history


class TestHistory:
    def test_newest_first_and_count(self, project):
        write(project, "a.txt", "1\n")
        repo = Repo.init(project)
        repo.commit("one")
        write(project, "a.txt", "2\n")
        repo.commit("two")
        h = repo.history()
        repo.close()
        assert h["count"] == 2
        assert h["commits"][0]["message"] == "two"
        assert h["commits"][1]["message"] == "one"
        assert h["commits"][0]["parent"] == h["commits"][1]["id"]

    def test_limit_n(self, project):
        write(project, "a.txt", "1\n")
        repo = Repo.init(project)
        for i in range(5):
            write(project, "a.txt", f"{i}\n")
            repo.commit(f"c{i}")
        h = repo.history(count=2)
        repo.close()
        assert h["count"] == 2
        assert h["commits"][0]["message"] == "c4"


# -------------------------------------------------------------------- diff


class TestDiff:
    def test_unified_diff_shape(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("v")
        write(project, "a.txt", "alpha\nbeta\n")
        d = repo.diff()
        repo.close()
        assert len(d["entries"]) == 1
        e = d["entries"][0]
        assert e["path"] == "a.txt"
        assert e["status"] == "modified"
        assert not e["binary"]
        assert "+beta" in e["hunks"]
        assert e["hunks"].startswith("--- a/a.txt")

    def test_added_and_deleted(self, project):
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        repo.commit("v")
        write(project, "b.txt", "new\n")
        (project / "a.txt").unlink()
        d = repo.diff()
        repo.close()
        by_path = {e["path"]: e for e in d["entries"]}
        assert by_path["b.txt"]["status"] == "added"
        assert by_path["a.txt"]["status"] == "deleted"
        assert "-alpha" in by_path["a.txt"]["hunks"]
        assert "+new" in by_path["b.txt"]["hunks"]

    def test_binary_file_note(self, project):
        write(project, "img.bin", b"\x00\x01\x02")
        repo = Repo.init(project)
        repo.commit("v")
        write(project, "img.bin", b"\x00\x01\x03")
        d = repo.diff()
        repo.close()
        e = d["entries"][0]
        assert e["binary"] is True
        assert "3 -> 3" in e["size"]


# ------------------------------------------------------- hostile layers


class TestHostileLayers:
    def _build_hostile(self, project, *, name="evil", members):
        repo = Repo.init(project)
        write(project, "a.txt", "x\n")
        repo.commit("base")
        repo.close()
        bad = project / ".firn" / "layers" / f"{'0' * 40}.firn"
        with (
            open(bad, "wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar,
        ):
            for m in members:
                tar.addfile(m)
        return bad

    def _member(self, name, **kw):
        ti = tarfile.TarInfo(name)
        for k, v in kw.items():
            setattr(ti, k, v)
        return ti

    def test_hardlink_rejected(self, project):
        members = [self._member("hl", type=tarfile.LNKTYPE, linkname="a.txt")]
        self._build_hostile(project, members=members)
        repo = Repo(project)
        with pytest.raises(IntegrityError, match="LNKTYPE|hardlink|member"):
            repo._read_layer("0" * 40)
        repo.close()

    def test_backslash_name_rejected(self, project):
        members = [self._member("evil\\..\\escape.txt")]
        self._build_hostile(project, members=members)
        repo = Repo(project)
        with pytest.raises(IntegrityError, match="backslash"):
            repo._read_layer("0" * 40)
        repo.close()

    def test_traversal_rejected(self, project):
        members = [self._member("../escape.txt")]
        self._build_hostile(project, members=members)
        repo = Repo(project)
        with pytest.raises(IntegrityError, match="traversal"):
            repo._read_layer("0" * 40)
        repo.close()

    def test_reserved_device_name_rejected(self, project):
        members = [self._member("CON.txt")]
        self._build_hostile(project, members=members)
        repo = Repo(project)
        with pytest.raises(IntegrityError, match="reserved"):
            repo._read_layer("0" * 40)
        repo.close()

    def test_drive_prefix_rejected(self, project):
        members = [self._member("C:evil.txt")]
        self._build_hostile(project, members=members)
        repo = Repo(project)
        with pytest.raises(IntegrityError, match="drive"):
            repo._read_layer("0" * 40)
        repo.close()


# ------------------------------------------------------------------- CLI


class TestCLI:
    def run_cli(self, project, *args, check=True):
        proc = subprocess.run(
            [PY, str(MODULE), *args],
            cwd=project,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"CLI {args} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def test_full_lifecycle_exit_codes(self, project):
        write(project, "a.txt", "alpha\n")
        p = self.run_cli(project, "init")
        assert p.returncode == 0
        p = self.run_cli(project, "commit", "-m", "first")
        assert p.returncode == 0
        p = self.run_cli(project, "status")
        assert "clean on main" in p.stdout
        p = self.run_cli(project, "history")
        assert "first" in p.stdout
        p = self.run_cli(project, "diff")
        assert p.stdout == ""

    def test_outside_repo_exit_code(self, tmp_path):
        empty = tmp_path / "elsewhere"
        empty.mkdir()
        proc = subprocess.run(
            [PY, str(MODULE), "status"], cwd=empty, capture_output=True, text=True
        )
        assert proc.returncode == 1
        assert "not a firnpack repository" in proc.stderr

    def test_json_contract_shapes(self, project):
        write(project, "a.txt", "alpha\n")
        self.run_cli(project, "init")
        p = self.run_cli(project, "commit", "-m", "first", "--json")
        payload = json.loads(p.stdout)
        assert payload["status"] == "packed"
        assert set(payload) >= {
            "status",
            "layer_id",
            "tree_id",
            "files",
            "size",
            "message",
        }
        p = self.run_cli(project, "status", "--json")
        st = json.loads(p.stdout)
        assert set(st) >= {
            "head",
            "clean",
            "modified",
            "added",
            "missing",
            "ignored_count",
            "ignored",
            "nested_gitignore",
            "merge_pending",
            "flattened_pending",
        }
        p = self.run_cli(project, "history", "--json")
        h = json.loads(p.stdout)
        assert h["count"] == 1
        assert set(h["commits"][0]) >= {
            "id",
            "tree_id",
            "parent",
            "second_parent",
            "message",
            "author",
            "created_at",
            "tips",
        }

    def test_version_flag(self, project):
        p = self.run_cli(project, "--version")
        assert firnpack.__version__ in p.stdout


# ------------------------------------------------- round-trip / verify


class TestRoundTrip:
    def test_layer_manifest_round_trip(self, project):
        write(project, "a.txt", "alpha\n")
        write(project, "sub/b.txt", "beta\n", mode=0o755)
        repo = Repo.init(project)
        r = repo.commit("v")
        meta, manifest, _ = repo._read_layer(r["layer_id"])
        repo.close()
        assert meta["tree_id"] == r["tree_id"]
        assert manifest["a.txt"]["size"] == 6
        if os.name == "nt":
            # First commit: no head to carry the mode from, and Windows
            # derives the exec bit from the extension — sub/b.txt reads
            # 0o644 there. The mode round-trip is proven on POSIX (and
            # by TestWindowsCarryForward via the carry-forward path).
            return
        assert manifest["sub/b.txt"]["mode"] == 0o755

    def test_tree_id_matches_recompute(self, project):
        """The self-certifying check: recompute tree_id from disk."""
        write(project, "a.txt", "alpha\n")
        repo = Repo.init(project)
        r = repo.commit("v")
        entries = {}
        for path in ("a.txt", ".gitignore"):
            fp = project / path
            if fp.exists():
                entries[path] = (
                    fp.stat().st_size,
                    fp.read_bytes(),
                    None,
                    bool(fp.stat().st_mode & 0o100),
                )
        repo.close()
        assert firnpack._hash_tree(entries) == r["tree_id"]


class TestTreeIdCollisions:
    """Identity ambiguity guards (collision audit 2026-10-20): the
    per-path digest needs the kind byte and the NUL separator, or
    distinct trees hash identically — and status would report drift
    while commit says nothing-to-pack, forever."""

    def test_path_content_boundary(self):
        t1 = firnpack._hash_tree({"a": (2, b"bc", None, False)})
        t2 = firnpack._hash_tree({"ab": (2, b"c", None, False)})
        assert t1 != t2

    def test_kind_disambiguates_symlink_target_from_content(self):
        t3 = firnpack._hash_tree({"link": (3, None, "foo", False)})
        t4 = firnpack._hash_tree({"link": (3, b"foo", None, False)})
        assert t3 != t4


class TestWindowsStatCache:
    def test_replaced_symlink_not_clean_via_sentinel(self, project, monkeypatch):
        """0o777 is the symlink sentinel in the stat cache. On Windows,
        the carry-forward made _file_mode return the sentinel as the disk
        mode, so a same-size/same-mtime regular file replacing a cached
        symlink passed Stage 1 and status reported clean while the head
        still held a symlink. Stage 1 must reject sentinel modes."""
        import firnpack as fp

        monkeypatch.setattr(fp, "_WINDOWS", True)
        import json as _json
        import os as _os
        import sqlite3

        write(project, "a.txt", "data\n")
        (project / "link.txt").symlink_to("real1")  # target name: 5 bytes
        repo = fp.Repo.init(project)
        repo.commit("base")
        repo.close()
        (project / "link.txt").unlink()
        (project / "link.txt").write_bytes(b"12345")  # content: 5 bytes
        db = sqlite3.connect(project / ".firn" / "glacier.db")
        cache = _json.loads(
            db.execute("SELECT stat_cache FROM worktree WHERE id=1").fetchone()[0]
        )
        db.close()
        mtime = cache["link.txt"][1]
        _os.utime(project / "link.txt", ns=(mtime, mtime))
        repo = fp.Repo.discover()
        st = repo.status()
        repo.close()
        assert st["modified"] == ["link.txt"]
        assert st["clean"] is False


# ----------------------------------------------------- repo file invariant


class TestFileCountInvariant:
    def test_n_plus_two(self, project):
        repo = Repo.init(project)
        repo.close()
        for i in range(5):
            write(project, f"f{i}.txt", f"{i}\n")
            repo = Repo.discover()
            repo.commit(f"c{i}")
            repo.close()
        files = [p for p in (project / ".firn").rglob("*") if p.is_file()]
        assert len(files) == 5 + 2


class TestOrphanSweep:
    def test_next_commit_sweeps_crash_leftovers(self, project):
        """SPEC §13: a crash leaves a stray .tmp or an unreferenced layer
        file; the next commit sweeps both, and never touches referenced
        layers."""
        write(project, "a.txt", "one\n")
        repo = Repo.init(project)
        r1 = repo.commit("first")
        repo.close()
        layers = project / ".firn" / "layers"
        # simulate both crash windows
        (layers / f"{r1['layer_id']}.firn.tmp").write_bytes(b"partial junk")
        (layers / f"{'0' * 40}.firn").write_bytes(b"orphan layer")
        write(project, "a.txt", "one\ntwo\n")
        repo = Repo.discover()
        repo.commit("second")
        repo.close()
        assert not list(layers.glob("*.tmp"))
        assert not (layers / f"{'0' * 40}.firn").exists()
        assert (layers / f"{r1['layer_id']}.firn").is_file()


def _retarget(root: Path):
    (root / "link.txt").unlink()
    (root / "link.txt").symlink_to("a.txt")


def _retarget_dangling(root: Path):
    (root / "link.txt").unlink()
    (root / "link.txt").symlink_to("gone.txt")


def _file_to_symlink(root: Path):
    (root / "a.txt").unlink()
    (root / "a.txt").symlink_to("other.txt")


def _symlink_to_file(root: Path):
    (root / "link.txt").unlink()
    (root / "link.txt").write_bytes(b"was a link\n")


def _delete(root: Path):
    (root / "a.txt").unlink()


def _add(root: Path):
    (root / "added.txt").write_bytes(b"fresh\n")


class TestChangeMatrix:
    """The anti-silent-drift family guard: every mutation a user can make
    must produce drift -> pack -> new identity. If a new tracked
    attribute is ever added without extending this matrix, the matrix is
    the reminder (owner decision 1a, 2026-10-20).

    Rows: content, symlink retarget, file<->symlink swaps, delete, add.
    Exec-bit is POSIX-only (Windows has no settable exec bit; nothing can
    drift there by definition, and carry-forward keeps cross-platform
    history stable).
    """

    POSIX_MUTATIONS = [
        ("exec_bit", lambda root: (root / "a.txt").chmod(0o755)),
    ]
    MUTATIONS = [
        (
            "content",
            lambda root: (root / "a.txt").write_bytes(b"start\nmore\n"),
        ),
        ("symlink_retarget", _retarget),
        ("symlink_retarget_dangling", _retarget_dangling),
        ("file_to_symlink", _file_to_symlink),
        ("symlink_to_file", _symlink_to_file),
        ("delete", _delete),
        ("add", _add),
    ]

    def _baseline(self, project):
        write(project, "a.txt", "start\n")
        write(project, "other.txt", "target\n")
        (project / "link.txt").symlink_to("other.txt")
        repo = Repo.init(project)
        first = repo.commit("base")
        repo.close()
        return first

    @pytest.mark.parametrize(
        "name,mutate", MUTATIONS, ids=lambda v: v if isinstance(v, str) else ""
    )
    def test_mutation_never_silent(self, project, name, mutate):
        first = self._baseline(project)
        mutate(project)
        repo = Repo.discover()
        st = repo.status()
        assert not st["clean"], f"{name} change was invisible: {st}"
        st2 = repo.commit("after " + name)
        repo.close()
        assert st2["status"] == "packed", f"{name} change did not pack"
        assert st2["layer_id"] != first["layer_id"], (
            f"{name} change did not alter identity"
        )

    @pytest.mark.skipif(os.name == "nt", reason="no settable exec bit")
    @pytest.mark.parametrize(
        "name,mutate", POSIX_MUTATIONS, ids=lambda v: v if isinstance(v, str) else ""
    )
    def test_posix_mutation_never_silent(self, project, name, mutate):
        first = self._baseline(project)
        mutate(project)
        repo = Repo.discover()
        st = repo.status()
        assert st["modified"] == ["a.txt"], f"{name} change was invisible"
        st2 = repo.commit("after " + name)
        repo.close()
        assert st2["status"] == "packed", f"{name} change did not pack"
        assert st2["layer_id"] != first["layer_id"], (
            f"{name} change did not alter identity"
        )

    @pytest.mark.skipif(os.name == "nt", reason="no settable exec bit")
    def test_chmod_round_trip(self, project):
        """chmod +x then commit: the layer stores 0o755, and a chmod back
        is again detectable (both directions)."""
        write(project, "s.sh", "echo hi\n")
        repo = Repo.init(project)
        repo.commit("plain")
        (project / "s.sh").chmod(0o755)
        repo.commit("now exec")
        _, manifest, _ = repo._read_layer(repo._worktree()["head_id"])
        assert manifest["s.sh"]["mode"] == 0o755
        repo.close()
        (project / "s.sh").chmod(0o644)
        repo = Repo.discover()
        st = repo.status()
        repo.close()
        assert st["modified"] == ["s.sh"]


class TestHeadIdContract:
    def test_head_id_null_only_before_first_commit(self, project):
        """2a: NULL is legal exactly until the first layer exists."""
        import sqlite3

        write(project, "a.txt", "x\n")
        repo = Repo.init(project)
        wt = repo._worktree()
        assert wt["head_id"] is None  # empty repo: NULL allowed
        repo.commit("first")
        repo.close()
        db = sqlite3.connect(project / ".firn" / "glacier.db")
        try:
            db.execute("UPDATE worktree SET head_id = NULL WHERE id = 1")
            db.commit()
            raise AssertionError("trigger did not reject head_id -> NULL")
        except sqlite3.IntegrityError:
            pass
        finally:
            db.close()


class TestWindowsCarryForward:
    """The carry-forward policy, exercised on POSIX by faking _WINDOWS."""

    def test_symlink_to_file_does_not_inherit_777(self, project, monkeypatch):
        """Head has link.txt as a SYMLINK (mode 0o777); the worktree now
        holds a regular file there. On Windows the carried mode must NOT
        be 0o777 — only regular-file head entries donate their mode, or
        a POSIX restore would apply an all-executable regular file."""
        import firnpack as fp

        monkeypatch.setattr(fp, "_WINDOWS", True)
        write(project, "real.txt", "data\n")
        (project / "link.txt").symlink_to("real.txt")
        repo = fp.Repo.init(project)
        repo.commit("base")  # head: link.txt is a symlink (mode 0o777)
        # replace the symlink with a regular file
        (project / "link.txt").unlink()
        write(project, "link.txt", "now a file\n")
        result = repo.commit("replaced")
        repo.close()
        _, manifest, _ = fp.Repo(project)._read_layer(result["layer_id"])
        mode = manifest["link.txt"]["mode"]
        assert mode in (0o644, 0o755), (
            f"symlink head entry donated its mode: {oct(mode)}"
        )

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="extension-derived exec bit is Windows CRT behavior",
    )
    def test_new_executable_extension_packs_exec_on_windows(self, project):
        """Genuinely new paths (no head entry to carry from) take the
        disk value: on Windows the CRT reports S_IEXEC from the file
        extension, so a new .bat packs 0o755 (and stays executable when
        restored on POSIX) while a new .txt packs 0o644 (SPEC §5, item
        B note). Runs on the windows-latest CI legs."""
        write(project, "run.bat", "echo hi\n")
        write(project, "note.txt", "hi\n")
        repo = Repo.init(project)
        repo.commit("new files")
        wt = repo._worktree()
        _, manifest, _ = repo._read_layer(wt["head_id"])
        repo.close()
        assert manifest["run.bat"]["mode"] == 0o755
        assert manifest["note.txt"]["mode"] == 0o644
