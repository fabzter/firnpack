"""Docs-shipping guard: the documentation tree promised by SPEC §20
exists, and the front door covers the whole command surface.

Docs ship with behavior (AGENTS.md §9): if a command is added or the
docs tree reshapes, this test fails until the docs catch up.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COMMANDS = [
    "init",
    "commit",
    "status",
    "history",
    "diff",
    "branch",
    "switch",
    "list-branches",
    "rename-branch",
    "delete-branch",
    "merge",
    "amend",
    "restore",
    "export",
    "verify",
    "compact",
]

DOCS = [
    "README.md",
    "usage.md",
    "agent-contract.md",
    "storage.md",
    "decisions.md",
]


@pytest.mark.parametrize("name", DOCS)
def test_doc_exists(name):
    path = ROOT / "docs" / name
    assert path.is_file(), f"SPEC §20 promises docs/{name}"
    assert path.stat().st_size > 200, f"docs/{name} is a stub"


def test_readme_command_table_covers_every_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in COMMANDS:
        assert f"`firnpack {cmd}" in readme, f"README misses {cmd}"


def test_usage_documents_every_command():
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    for cmd in COMMANDS:
        # synopsis line in a code fence, and a heading that names it
        # (the branch family shares one heading: "## branch / switch / …")
        assert f"firnpack {cmd}" in usage, f"usage.md misses synopsis for {cmd}"
        assert (
            f"## {cmd}" in usage
            or f"/ {cmd}" in usage
            or f" {cmd} " in usage.split("```")[0]
        ), f"usage.md misses a heading naming {cmd}"


def test_usage_documents_exit_codes():
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    for code in ("| 0 |", "| 1 |", "| 2 |", "| 3 |"):
        assert code in usage, f"usage.md exit-code table misses {code}"


def test_agent_contract_pins_the_error_taxonomy():
    contract = (ROOT / "docs" / "agent-contract.md").read_text(encoding="utf-8")
    for phrase in (
        "Never auto-overwrite",
        "Always rescue",
        "Compact only with human parameters",
        "Never silent",
    ):
        assert phrase in contract, f"agent-contract misses rule: {phrase}"


def test_storage_documents_schema_and_recovery():
    storage = (ROOT / "docs" / "storage.md").read_text(encoding="utf-8")
    for table in ("layers", "branches", "worktree", "config", "refs"):
        assert table in storage, f"storage.md misses table {table}"
    assert "Recovery playbook" in storage


def test_decisions_log_covers_the_ratified_items():
    decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    for marker in (
        "D-04",  # python floor
        "D-08",  # compact permission model
        "D-13",  # windows carry-forward (B)
        "D-14",  # tree_id formula (B2)
        "D-16",  # reference-based guards
        "D-18",  # rewrite closure (a)
        "O-01",  # rebuild-db open item
    ):
        assert marker in decisions, f"decisions.md misses {marker}"
