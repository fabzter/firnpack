"""Guard tests: packaging and CI invariants that would otherwise drift.

SPEC §19 (workflow guards) and §15 (single-file discipline): these are
cheap assertions that catch config drift before a release does.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestPackagingGuards:
    def _pyproject(self) -> dict:
        with open(ROOT / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)

    def test_single_module(self):
        data = self._pyproject()
        assert data["tool"]["setuptools"]["py-modules"] == ["firnpack"]

    def test_both_entry_points(self):
        scripts = self._pyproject()["project"]["scripts"]
        assert scripts["firnpack"] == "firnpack:main"
        assert scripts["firn"] == "firnpack:main"

    def test_zero_runtime_dependencies(self):
        assert self._pyproject()["project"]["dependencies"] == []

    def test_python_floor(self):
        assert self._pyproject()["project"]["requires-python"] == ">=3.13.16"

    def test_version_single_source(self):
        import sys

        sys.path.insert(0, str(ROOT))
        import firnpack

        assert self._pyproject()["project"]["version"] == firnpack.__version__

    def test_license_file_present(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert text.startswith("MIT License")

    def test_sdist_manifest_ships_the_documents(self):
        """SPEC §18 / docs/README.md promise that all documents ship in the
        sdist. setuptools only includes README and tests by default, so the
        promise lives in MANIFEST.in (the published 1.0.0 sdist shipped no
        docs). The wheel stays one module: MANIFEST.in does not touch it."""
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for needle in (
            "SPEC.md",
            "CHANGELOG.md",
            "recursive-include docs",
            "recursive-include research",
            "graft .github",
        ):
            assert needle in manifest, f"MANIFEST.in misses {needle!r}"


class TestWorkflowGuards:
    def _ci(self) -> str:
        return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_ci_workflow_exists(self):
        assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()

    def test_floor_is_pinned_exactly(self):
        ci = self._ci()
        # SPEC §19: the shipped floor must be what gets tested
        assert ci.count('"3.13.16"') >= 2  # ubuntu + windows legs

    def test_target_os_present(self):
        ci = self._ci()
        assert "windows-latest" in ci
        assert "macos-latest" in ci

    def test_lint_and_test_steps(self):
        ci = self._ci()
        assert "ruff check" in ci
        assert "ruff format --check" in ci
        assert "pytest" in ci

    def test_wheel_guard_present(self):
        ci = self._ci()
        assert "wheel-guard" in ci
        assert re.search(r"\bfirnpack\.py\b", ci)

    def _release(self) -> str:
        return (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

    def test_release_workflow_exists(self):
        # SPEC 19: both workflow files exist (the guard that caught
        # release.yml never having been committed)
        assert (ROOT / ".github" / "workflows" / "release.yml").is_file()

    def test_release_tag_trigger_and_trusted_publishing(self):
        rel = self._release()
        assert "v*" in rel  # tag-push trigger
        assert "pypa/gh-action-pypi-publish@release/v1" in rel
        assert "id-token: write" in rel  # OIDC, no API tokens
        assert "contents: read" in rel

    def test_release_builds_with_supported_python(self):
        rel = self._release()
        ci = self._ci()
        # the release build runs a supported interpreter
        versions = re.findall(r'python-version: "?([\d.]+)"?', rel)
        assert versions, "release.yml must pin a build interpreter"
        assert any(v in ci for v in versions), (
            "release.yml builds on a Python the CI matrix never tests"
        )

    def test_release_tag_version_gate(self):
        rel = self._release()
        # the tag gate reads the pushed tag name from the environment
        assert "github.ref_name" in rel
        assert 'os.environ["GITHUB_REF_NAME"]' in rel
        # and compares it against pyproject and firnpack.__version__
        assert "tomllib" in rel
        assert "firnpack.__version__" in rel
        assert 'f"v{version}"' in rel

    def test_release_build_needs_test(self):
        rel = self._release()
        # a green test job is required before anything is built or published
        assert "needs: test" in rel

    def test_release_test_job_legs(self):
        rel = self._release()
        # SPEC 19: the floor on both OSes plus the newest supported
        assert rel.count('py: "3.13.16"') == 2  # ubuntu floor + windows floor
        assert "- os: ubuntu-latest" in rel
        assert "- os: windows-latest" in rel
        assert 'py: "3.15"' in rel  # newest supported, ubuntu

    def test_release_artifact_guards(self):
        rel = self._release()
        # wheel: exactly firnpack.py plus dist-info (same check as ci.yml)
        assert 'names == ["firnpack.py"]' in rel
        # sdist: the documents ship (SPEC 18)
        assert '"SPEC.md"' in rel
        assert '"CHANGELOG.md"' in rel
        assert '"/docs/"' in rel
        assert '"/.github/workflows/"' in rel

    def test_release_publish_environment(self):
        rel = self._release()
        assert "environment: pypi" in rel


class TestProjectUrls:
    def _urls(self) -> dict:
        with open(ROOT / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["urls"]

    def test_urls_point_at_the_repo(self):
        urls = self._urls()
        assert urls["Homepage"] == "https://github.com/fabzter/firnpack"
        assert urls["Repository"] == "https://github.com/fabzter/firnpack"

    def test_issue_and_changelog_urls(self):
        urls = self._urls()
        assert urls["Issues"] == "https://github.com/fabzter/firnpack/issues"
        assert urls["Changelog"] == (
            "https://github.com/fabzter/firnpack/blob/main/CHANGELOG.md"
        )
