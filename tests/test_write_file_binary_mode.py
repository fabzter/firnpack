"""Materialized files must preserve bytes, including Windows CRT newlines."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firnpack  # noqa: E402


@pytest.mark.parametrize(
    "payload",
    [b"", b"line\n", b"line\r\n", b"LF\nCRLF\r\n\0\x1a\xff\n", bytes(range(256))],
    ids=["empty", "lf", "crlf", "mixed", "all-byte-values"],
)
def test_write_file_preserves_exact_bytes(tmp_path, payload):
    target = tmp_path / "materialized.bin"
    firnpack._write_file(target, payload)
    assert target.read_bytes() == payload


def test_write_file_requests_binary_mode_where_available(tmp_path, monkeypatch):
    """Exercise the flag contract even on hosts without Windows CRT I/O."""
    real_open = os.open
    binary_flag = getattr(os, "O_BINARY", 0) or (1 << 30)
    observed = []
    monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)

    def open_spy(path, flags, mode=0o777):
        observed.append(flags)
        # A synthetic Windows flag must not reach a POSIX kernel.
        native_flags = flags if os.name == "nt" else flags & ~binary_flag
        return real_open(path, native_flags, mode)

    monkeypatch.setattr(firnpack.os, "open", open_spy)
    target = tmp_path / "flag.bin"
    firnpack._write_file(target, b"bytes\n")
    assert observed and all(flags & binary_flag for flags in observed)
    assert all(flags & os.O_WRONLY and flags & os.O_EXCL for flags in observed)
    assert target.read_bytes() == b"bytes\n"
