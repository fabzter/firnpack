"""CLI-only helpers for multi-session working-directory journeys.

An explicit command override lets the same oracles exercise an installed
package or a Windows interpreter without silently testing the checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath

ENGINE = Path(__file__).resolve().parents[1] / "firnpack.py"
TARGET_PLATFORM = os.environ.get("FIRNPACK_E2E_PLATFORM", sys.platform)
SEED_IGNORE = b"~$*\r\n" if TARGET_PLATFORM == "win32" else b"~$*\n"
CALLS: list[dict] = []
PYTHON_CALLS: list[dict] = []


def _command(
    variable: str = "FIRNPACK_E2E_COMMAND",
    default: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    override = os.environ.get(variable)
    if override is None:
        return (sys.executable, str(ENGINE)) if default is None else default
    try:
        command = json.loads(override)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be a JSON string array") from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError(f"{variable} must be a nonempty string array")
    return tuple(command)


COMMAND = _command()


def tree_files(root: Path, *, exclude_admin: bool = True) -> dict:
    """Read actual bytes without following symlinks or applying ignore rules."""
    result = {}
    root = Path(root)

    def visit(directory: Path) -> None:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = entry.relative_to(root)
            if exclude_admin and relative.parts[0] == ".firn":
                continue
            name = relative.as_posix()
            if entry.is_symlink():
                result[name] = ("symlink", os.readlink(entry))
            elif entry.is_dir():
                visit(entry)
            elif entry.is_file():
                result[name] = entry.read_bytes()
            else:
                raise AssertionError(f"unexpected special fixture entry: {entry}")

    visit(root)
    return result


class World:
    """A synthetic user's project; every operation is a real CLI process."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def host_path(self, reported: str) -> Path:
        """Map a CLI result into this synthetic project's sandbox."""
        if TARGET_PLATFORM == "win32" and sys.platform != "win32":
            native = PureWindowsPath(reported)
            assert not native.drive or native.drive.casefold() == "z:", (
                f"unmapped Windows drive in reported path: {reported}"
            )
            assert not native.drive or native.root, (
                f"ambiguous drive-relative Windows path: {reported}"
            )
            path = (
                Path("/").joinpath(*native.parts[1:])
                if native.root
                else self.path.joinpath(*native.parts[bool(native.drive) :])
            )
        else:
            path = Path(reported)
            if not path.is_absolute():
                path = self.path / path
        path = path.resolve()
        sandbox = self.path.parent
        assert path == sandbox or sandbox in path.parents, (
            f"reported path escapes fixture sandbox: {reported}"
        )
        return path

    def target_path(self, path: Path) -> str:
        """Translate a sandbox directory into the target OS's path syntax."""
        path = Path(path).resolve()
        sandbox = self.path.parent
        assert path == sandbox or sandbox in path.parents, (
            f"target path escapes fixture sandbox: {path}"
        )
        if TARGET_PLATFORM == "win32" and sys.platform != "win32":
            return str(PureWindowsPath("Z:/").joinpath(*path.parts[1:]))
        return str(path)

    @classmethod
    def create(cls, path: Path) -> World:
        path.mkdir(parents=True)
        world = cls(path)
        world.raw("init")
        assert (world.path / ".gitignore").read_bytes() == SEED_IGNORE
        return world

    def raw(self, *args, rc: int = 0, input_text: str | None = None):
        command = [*COMMAND, *(str(arg) for arg in args)]
        started = time.monotonic()
        proc = subprocess.run(
            command,
            cwd=self.path,
            input="" if input_text is None else input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        CALLS.append(
            {
                "cwd": str(self.path),
                "args": [str(arg) for arg in args],
                "returncode": proc.returncode,
                "seconds": round(time.monotonic() - started, 4),
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
        details = (command, str(self.path), proc.returncode, proc.stdout, proc.stderr)
        assert proc.returncode == rc, details
        assert "Traceback (most recent call last)" not in proc.stderr, details
        return proc

    def json(self, *args, rc: int = 0) -> dict:
        proc = self.raw(*args, "--json", rc=rc)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError((args, proc.stdout, proc.stderr)) from exc
        assert isinstance(payload, dict), (args, payload)
        return payload

    def python_json(self, code: str, *args: str) -> dict:
        """Create fixtures and inspect filesystem metadata in the target OS.

        A POSIX host's symlink is not a Windows/Wine reparse point. Both
        creation and lstat/readlink therefore use the proven target
        interpreter, never an inferred host-side representation.
        """
        default = (sys.executable, "-I") if sys.platform == TARGET_PLATFORM else ()
        prefix = _command("FIRNPACK_E2E_PYTHON", default)
        if not prefix:
            raise ValueError(
                "FIRNPACK_E2E_PYTHON must select the target interpreter "
                "when the target and host platforms differ"
            )
        guarded_code = (
            f"import sys; assert sys.platform == {TARGET_PLATFORM!r}, sys.platform\n"
            + code
        )
        proc = subprocess.run(
            [*prefix, "-c", guarded_code, *(str(arg) for arg in args)],
            cwd=self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        PYTHON_CALLS.append(
            {
                "cwd": str(self.path),
                "command": list(prefix),
                "code": code,
                "args": [str(arg) for arg in args],
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
        assert proc.returncode == 0, (prefix, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert isinstance(payload, dict), payload
        return payload

    def commit(self, message: str | None = None, *flags: str) -> dict:
        args = ["commit"]
        if message is not None:
            args.extend(("-m", message))
        args.extend(flags)
        result = self.json(*args)
        assert result["status"] == "packed", result
        return result

    def write(self, relative: str, data: str | bytes) -> Path:
        """Fixture data only; explicit .firn paths permit fault injection."""
        relative_path = Path(relative)
        assert not relative_path.is_absolute() and ".." not in relative_path.parts
        path = self.path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def tree(self) -> dict:
        return tree_files(self.path)

    def layer_bytes(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in sorted((self.path / ".firn" / "layers").glob("*.firn"))
        }

    def store_bytes(self) -> dict:
        return tree_files(self.path / ".firn", exclude_admin=False)

    def verify(self) -> dict:
        result = self.json("verify")
        assert result["status"] == "ok", result
        return result

    def clean(self) -> dict:
        result = self.json("status", "--full")
        assert result["clean"], result
        return result
