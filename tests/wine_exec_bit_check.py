"""Manual Windows-behavior verification, run under wine/CrossOver.

Not collected by pytest (name does not match test_*.py): it needs a
Windows runtime. Purpose: prove the Windows facts the macOS/Linux
suites cannot reach —

1. the CRT derives the exec bit from the file extension, so a
   genuinely new `.bat` stats with S_IEXEC and packs 0o755 while a
   new `.txt` packs 0o644 (SPEC §5, item B note);
2. the carry-forward policy: on a later commit a regular file's mode
   comes from the head manifest, not the disk (SPEC §5, item B).

Check 2 needs a discriminating seed, because on Windows the extension
and the carried value agree for .bat/.txt: a repo seeded on POSIX with
`deploy.sh` committed at 0o755 (chmod +x) is edited and committed
here. `.sh` is NOT in the CRT exec set, so a plain re-stat would pack
0o644 while carry-forward packs 0o755 — the two behaviors disagree,
and the script also prints the re-stat value as evidence.

Usage (64-bit wine prefix, Windows python):

    wineloader "Z:\\tmp\\winpy\\python.exe" \
        "Z:\\<repo>\\tests\\wine_exec_bit_check.py" C:\\firnwork \
        Z:\\tmp\\wineseed

The seed repository is created on POSIX (see the session recipe:
init, write deploy.sh, chmod 0o755, commit). Exits 0 with PASS lines
when every expectation holds; exits 1 on any mismatch. On a non-
Windows runtime it prints a notice and exits 0.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402

EXPECTED = [
    ("CRT .bat exec bit", True),
    ("CRT .txt exec bit", False),
    ("first commit run.bat mode", 0o755),
    ("first commit note.txt mode", 0o644),
    ("second commit run.bat mode", 0o755),
    ("second commit note.txt mode", 0o644),
    ("seed deploy.sh CRT exec bit on Windows", False),
    ("seed deploy.sh re-stat would pack", 0o644),
    ("seed deploy.sh carried mode", 0o755),
]


def main() -> int:
    if os.name != "nt":
        print("not a Windows runtime — nothing to verify here")
        return 0
    work = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\firnwork")
    work.mkdir(parents=True, exist_ok=True)

    bat = work / "run.bat"
    bat.write_text("echo hi\n")
    txt = work / "note.txt"
    txt.write_text("y\n")
    observed = [
        ("CRT .bat exec bit", bool(bat.stat().st_mode & 0o100)),
        ("CRT .txt exec bit", bool(txt.stat().st_mode & 0o100)),
    ]

    os.chdir(work)
    repo = firnpack.Repo.init(work)
    repo.commit("new files")
    _, manifest, _ = repo._read_layer(repo._worktree()["head_id"])
    observed += [
        ("first commit run.bat mode", manifest["run.bat"]["mode"]),
        ("first commit note.txt mode", manifest["note.txt"]["mode"]),
    ]
    bat.write_text("echo hi again\n")
    repo.commit("second")
    _, manifest2, _ = repo._read_layer(repo._worktree()["head_id"])
    observed += [
        ("second commit run.bat mode", manifest2["run.bat"]["mode"]),
        ("second commit note.txt mode", manifest2["note.txt"]["mode"]),
    ]
    repo.close()

    # discriminating carry-forward case: POSIX-seeded repo, .sh is not
    # in the CRT exec set, so re-stat and carry-forward disagree here
    if len(sys.argv) > 2:
        seed = Path(sys.argv[2])
        srepo = firnpack.Repo(seed)
        deploy = seed / "deploy.sh"
        crt_bit = bool(deploy.stat().st_mode & 0o100)
        restat_mode = 0o755 if crt_bit else 0o644
        deploy.write_text("#!/bin/sh\necho deploy v2\n")
        srepo.commit("edited on windows")
        _, smanifest, _ = srepo._read_layer(srepo._worktree()["head_id"])
        observed += [
            ("seed deploy.sh CRT exec bit on Windows", crt_bit),
            ("seed deploy.sh re-stat would pack", restat_mode),
            ("seed deploy.sh carried mode", smanifest["deploy.sh"]["mode"]),
        ]
        srepo.close()

    failures = 0
    for (name, want), (_, got) in zip(EXPECTED, observed, strict=True):
        ok = got == want
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} {name}: {got!r} (want {want!r})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
