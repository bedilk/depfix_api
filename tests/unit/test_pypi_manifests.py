"""Unit tests for the PyPI dependency-drift manifest writer."""

from __future__ import annotations

from pathlib import Path

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.ecosystems.pypi_manifests import build_pypi_dependency_drift_bumps


def _drift(package: str, old: str, new: str) -> BreakingChange:
    return BreakingChange(
        package=package,
        old_version=old,
        new_version=new,
        old_api=f"{package}@{old}",
        new_api=f"{package}@{new}",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )


def _only(bumps: list):
    assert len(bumps) == 1, bumps
    return bumps[0]


def test_bumps_an_exact_requirements_pin(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai==1.2.0\nrequests==2.31.0\n")

    bump = _only(build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.2.0", "1.4.3")))

    assert bump.manifest_relpath == "requirements.txt"
    assert bump.new_spec == "==1.4.3"
    assert bump.fixed_content == "openai==1.4.3\nrequests==2.31.0\n"


def test_preserves_extras_and_environment_marker(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text('openai[datalib]>=1.0 ; python_version >= "3.9"\n')

    bump = _only(build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.0.0", "1.4.3")))

    assert bump.fixed_content == 'openai[datalib]==1.4.3 ; python_version >= "3.9"\n'


def test_normalizes_package_name_per_pep503(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("Google_Cloud.Storage==1.0.0\n")

    bump = _only(
        build_pypi_dependency_drift_bumps(
            tmp_path, _drift("google-cloud-storage", "1.0.0", "1.1.0")
        )
    )

    assert bump.fixed_content == "Google_Cloud.Storage==1.1.0\n"


def test_bumps_pep621_list_dependency(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["openai>=1.0,<2", "rich"]\n'
    )

    bump = _only(build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.0.0", "1.4.3")))

    assert bump.manifest_relpath == "pyproject.toml"
    assert '"openai==1.4.3"' in bump.fixed_content
    assert '"rich"' in bump.fixed_content


def test_bumps_poetry_table_dependency(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.12"\nopenai = "^1.2.0"\n'
    )

    bump = _only(build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.2.0", "1.4.3")))

    assert 'openai = "==1.4.3"' in bump.fixed_content
    assert 'python = "^3.12"' in bump.fixed_content


def test_major_drift_declines(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai==1.2.0\n")

    assert build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.2.0", "2.0.0")) == []


def test_same_change_major_does_not_cross_a_manifest_major(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai>=4.24.7\n")

    assert build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "7.17.0", "7.19.0")) == []


def test_leaves_unrelated_packages_alone(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")

    assert build_pypi_dependency_drift_bumps(tmp_path, _drift("openai", "1.0.0", "1.1.0")) == []
