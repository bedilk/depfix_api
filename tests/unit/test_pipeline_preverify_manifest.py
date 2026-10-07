"""The verifier must install the target SDK before checking a migration."""

from __future__ import annotations

import json
from pathlib import Path

from depfix.apply.workspace import WorkspaceEditor
from depfix.clone.service import Checkout
from depfix.codemods.lockfile import (
    build_dependency_drift_bumps,
    build_dependency_drift_plan,
    build_manifest_bumps,
    dependency_drift_policy,
)
from depfix.config import Settings
from depfix.core.fix_service import run_fix_for_change
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.core.pipeline import FixPipeline
from depfix.repoconfig.models import SUPPORTED_VERSION, RepoConfig
from depfix.scanners.models import CallSite, CallSiteKind, MatchConfidence, RepoScanResult
from depfix.verify.verifier import VerificationReport


class _FakeFixer:
    total_cost = 0.0
    total_tokens = 0

    def generate_fix(self, file_usage, breaking_change):
        return file_usage.file_content.replace("legacyCall()", "modernCall()"), 1.0, None


class _CapturingVerifier:
    """Verifier seam that records the dependency state at verification time."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.manifest_during_verification = ""

    def verify(self, edits, *, change):
        self.manifest_during_verification = (self._root / "package.json").read_text()
        return VerificationReport(ran=True, edits=tuple(edits))


def test_pipeline_bumps_manifest_only_while_verifying_migration(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"openai": "^3.2.1"}}) + "\n")
    source = "function legacyCall() {}\nlegacyCall()\n"
    (root / "client.js").write_text(source)
    original_manifest = (root / "package.json").read_text()
    site = CallSite(
        filepath="client.js",
        line_number=2,
        column=0,
        line_content="legacyCall()",
        kind=CallSiteKind.METHOD_CALL,
        confidence=MatchConfidence.HIGH,
        symbol="openai.createChatCompletion",
        provider_id="openai",
    )
    scan = RepoScanResult(repo_full_name="acme/widgets", commit_sha="abc", call_sites=[site])
    change = BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.1",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="OpenAI v4 migration.",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
    )
    editor = WorkspaceEditor(Checkout(path=root, is_temporary=False))
    verifier = _CapturingVerifier(root)

    FixPipeline(_FakeFixer(), verifier=verifier).run(scan, change, editor)

    assert '"openai": "^4.0.0"' in verifier.manifest_during_verification
    assert (root / "package.json").read_text() == original_manifest


def test_minor_dependency_drift_preserves_the_npm_range_operator(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"acme-sdk": "~1.2.0"}}))
    change = BreakingChange(
        package="acme-sdk",
        old_version="1.2.0",
        new_version="1.4.3",
        old_api="acme-sdk@1.2.0",
        new_api="acme-sdk@1.4.3",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    bumps = build_dependency_drift_bumps(root, change)

    assert dependency_drift_policy(change) == "automatic"
    assert len(bumps) == 1
    assert bumps[0].new_range == "~1.4.3"


def test_major_dependency_drift_becomes_a_draft_review_candidate(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"acme-sdk": "^1.2.0"}}))
    change = BreakingChange(
        package="acme-sdk",
        old_version="1.2.0",
        new_version="2.0.0",
        old_api="acme-sdk@1.2.0",
        new_api="acme-sdk@2.0.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    assert dependency_drift_policy(change) == "review_pr"
    bumps = build_dependency_drift_bumps(root, change)
    assert len(bumps) == 1
    assert bumps[0].new_range == "^2.0.0"


def test_major_review_preserves_an_exact_manifest_pin(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"acme-sdk": "1.2.0"}}))
    change = BreakingChange(
        package="acme-sdk",
        old_version="1.2.0",
        new_version="2.0.0",
        old_api="acme-sdk@1.2.0",
        new_api="acme-sdk@2.0.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    bump = build_dependency_drift_bumps(root, change)[0]

    assert bump.new_range == "2.0.0"


def test_dependency_drift_does_not_rewrite_a_manifest_at_another_version(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"acme-sdk": "^4.24.7"}}))
    change = BreakingChange(
        package="acme-sdk",
        old_version="7.17.0",
        new_version="7.19.0",
        old_api="acme-sdk@7.17.0",
        new_api="acme-sdk@7.19.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    plan = build_dependency_drift_plan(root, change)

    assert len(plan.bumps) == 0
    assert len(plan.declines) == 1
    assert "major 4" in plan.declines[0] or "major 7" in plan.declines[0]


def test_dependency_drift_updates_the_pnpm_catalog_not_consuming_package_json(
    tmp_path: Path,
) -> None:
    """``catalog:`` dependencies are owned by pnpm-workspace.yaml."""
    root = tmp_path / "checkout"
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "package.json").write_text(
        json.dumps({"dependencies": {"@prisma/client": "catalog:"}}) + "\n"
    )
    workspace = root / "pnpm-workspace.yaml"
    workspace.write_text("catalog:\n  '@prisma/client': ^7.8.0\n", encoding="utf-8")
    change = BreakingChange(
        package="@prisma/client",
        old_version="7.8.0",
        new_version="7.10.0",
        old_api="@prisma/client@7.8.0",
        new_api="@prisma/client@7.10.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    plan = build_dependency_drift_plan(root, change)

    assert len(plan.bumps) == 1
    assert plan.bumps[0].manifest_relpath == "pnpm-workspace.yaml"
    assert "'@prisma/client': ^7.10.0" in plan.bumps[0].fixed_content
    assert (root / "apps" / "web" / "package.json").read_text() == (
        json.dumps({"dependencies": {"@prisma/client": "catalog:"}}) + "\n"
    )


def test_dependency_drift_updates_the_named_pnpm_catalog_not_consumer(
    tmp_path: Path,
) -> None:
    """``catalog:name`` is owned by ``catalogs.name`` in the workspace file."""
    root = tmp_path / "checkout"
    (root / "apps" / "web").mkdir(parents=True)
    consumer = root / "apps" / "web" / "package.json"
    consumer.write_text(json.dumps({"dependencies": {"@prisma/client": "catalog:platform"}}) + "\n")
    workspace = root / "pnpm-workspace.yaml"
    workspace.write_text(
        "catalogs:\n  platform:\n    '@prisma/client': ^7.8.0\n",
        encoding="utf-8",
    )
    change = BreakingChange(
        package="@prisma/client",
        old_version="7.8.0",
        new_version="7.10.0",
        old_api="@prisma/client@7.8.0",
        new_api="@prisma/client@7.10.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )

    plan = build_dependency_drift_plan(root, change)

    assert len(plan.bumps) == 1
    assert plan.bumps[0].manifest_relpath == "pnpm-workspace.yaml"
    assert "'@prisma/client': ^7.10.0" in plan.bumps[0].fixed_content
    assert consumer.read_text() == (
        json.dumps({"dependencies": {"@prisma/client": "catalog:platform"}}) + "\n"
    )


def test_api_migration_bumps_the_pnpm_catalog_not_consumer(tmp_path: Path) -> None:
    """The regular API-migration path uses the same source-of-truth resolver."""
    root = tmp_path / "checkout"
    (root / "apps" / "web").mkdir(parents=True)
    consumer = root / "apps" / "web" / "package.json"
    consumer.write_text(json.dumps({"dependencies": {"openai": "catalog:"}}) + "\n")
    (root / "pnpm-workspace.yaml").write_text("catalog:\n  openai: ^3.3.0\n")
    change = BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.1",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="OpenAI v4",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
    )

    bumps = build_manifest_bumps(root, change)

    assert len(bumps) == 1
    assert bumps[0].manifest_relpath == "pnpm-workspace.yaml"
    assert "openai: ^4.0.0" in bumps[0].fixed_content
    assert consumer.read_text() == json.dumps({"dependencies": {"openai": "catalog:"}}) + "\n"


def test_dependency_drift_uses_the_fix_service_without_call_sites(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"dependencies": {"acme-sdk": "^1.2.0"}}))
    change = BreakingChange(
        package="acme-sdk",
        old_version="1.2.0",
        new_version="1.3.0",
        old_api="acme-sdk@1.2.0",
        new_api="acme-sdk@1.3.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )
    scan = RepoScanResult(repo_full_name="acme/widgets", commit_sha="abc")

    result = run_fix_for_change(
        checkout=Checkout(path=root, is_temporary=False),
        scan_result=scan,
        change=change,
        settings=Settings(verify_enabled=False),
        repo_config=RepoConfig(version=SUPPORTED_VERSION, verify=False),
        fixer=_FakeFixer(),
    )

    assert len(result.committable) == 1
    assert result.committable[0].relpath == "package.json"
    assert '"acme-sdk": "^1.3.0"' in (root / "package.json").read_text()


def test_pypi_dependency_drift_uses_the_fix_service_without_call_sites(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (root / "requirements.txt").write_text("acme-sdk==1.2.0\n")
    change = BreakingChange(
        package="acme-sdk",
        old_version="1.2.0",
        new_version="1.3.0",
        old_api="acme-sdk@1.2.0",
        new_api="acme-sdk@1.3.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )
    scan = RepoScanResult(repo_full_name="acme/widgets", commit_sha="abc")

    result = run_fix_for_change(
        checkout=Checkout(path=root, is_temporary=False),
        scan_result=scan,
        change=change,
        settings=Settings(verify_enabled=False),
        repo_config=RepoConfig(version=SUPPORTED_VERSION, verify=False),
        fixer=_FakeFixer(),
    )

    assert len(result.committable) == 1
    assert result.committable[0].relpath == "requirements.txt"
    assert "acme-sdk==1.3.0" in (root / "requirements.txt").read_text()


def test_unsupported_ecosystem_drift_is_reported_not_applied(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "build.gradle").write_text("dependencies { implementation 'acme:sdk:1.2.0' }\n")
    change = BreakingChange(
        package="acme:sdk",
        old_version="1.2.0",
        new_version="1.3.0",
        old_api="acme:sdk@1.2.0",
        new_api="acme:sdk@1.3.0",
        description="Registry version drift",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )
    scan = RepoScanResult(repo_full_name="acme/widgets", commit_sha="abc")

    result = run_fix_for_change(
        checkout=Checkout(path=root, is_temporary=False),
        scan_result=scan,
        change=change,
        settings=Settings(verify_enabled=False),
        repo_config=RepoConfig(version=SUPPORTED_VERSION, verify=False),
        fixer=_FakeFixer(),
    )

    assert result.committable == []
