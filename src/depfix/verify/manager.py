"""Package-manager detection and dependency installation for a checkout
about to have its test suite run.

Detection is lockfile-based (not ``package.json``-based) because the
lockfile is what actually determines which client *must* be used --
installing an npm-locked repo with yarn (or vice versa) routinely produces
a different dependency tree than CI would have used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES, SandboxResult, run_sandboxed

logger = logging.getLogger(__name__)

#: Ordered (lockfile, package_manager) pairs -- first match wins. Order
#: matters only in the pathological case where multiple lockfiles coexist
#: (e.g. a repo mid-migration between package managers); npm is the most
#: conservative default since it's always present with Node.
_LOCKFILE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
    ("yarn.lock", "yarn"),
    ("pnpm-lock.yaml", "pnpm"),
)

DEFAULT_PACKAGE_MANAGER = "npm"
_SUPPORTED_MANAGED_CLIENTS = frozenset({"yarn", "pnpm"})
_PACKAGE_MANAGER_SPEC = re.compile(
    r"^(?P<name>yarn|pnpm)@(?P<version>\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?)$"
)


@dataclass(frozen=True)
class PackageManagerPreparation:
    """A repository-specific JavaScript package-manager runtime.

    The scanner is responsible for preparing this runtime once a repository
    has been cloned. Verification calls the same deep module defensively so
    ``plan`` remains safe when invoked without a preceding ``scan``.
    """

    package_manager: str
    executable: Path | None = None
    version: str = ""
    installed: bool = False
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.executable is not None and not self.detail


@dataclass
class InstallResult:
    """Outcome of installing a checkout's dependencies."""

    package_manager: str
    sandbox: SandboxResult | None = None
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        if self.skipped_reason:
            return False
        return self.sandbox is not None and self.sandbox.ok


def detect_package_manager(root: str | Path) -> str:
    """Return the package manager implied by ``root``'s lockfile, falling
    back to :data:`DEFAULT_PACKAGE_MANAGER` (npm) if none is present."""
    root = Path(root)
    for lockfile, manager in _LOCKFILE_MANAGERS:
        if (root / lockfile).is_file():
            return manager
    return DEFAULT_PACKAGE_MANAGER


def prepare_package_manager(
    root: str | Path,
    *,
    cache_dir: str | Path | None = None,
    timeout: float = 90.0,
    package_manager: str | None = None,
) -> PackageManagerPreparation:
    """Make the repository's declared JS package manager available.

    npm is supplied by Node and is only checked. Yarn and pnpm are installed
    at the exact version declared by ``packageManager`` into a Depfix-owned
    cache, never globally and never into the target checkout. Bun is detected
    but not bootstrapped because its vendor installer is a shell script; the
    operator must install it explicitly.
    """
    from depfix.scanners.workspace import find_workspace_root

    workspace = find_workspace_root(Path(root))
    manager = package_manager or detect_package_manager(workspace)
    declared_version = _declared_manager_version(workspace, manager)

    installed = _which(manager)
    if installed is not None:
        return PackageManagerPreparation(
            package_manager=manager,
            executable=Path(installed),
            version=declared_version,
        )

    if manager == "npm":
        return PackageManagerPreparation(
            package_manager=manager,
            detail="npm not found on PATH (install Node.js, which includes npm)",
        )
    if manager == "bun":
        return PackageManagerPreparation(
            package_manager=manager,
            detail="bun not found on PATH; automatic Bun installation is disabled",
        )
    if manager not in _SUPPORTED_MANAGED_CLIENTS:
        return PackageManagerPreparation(
            package_manager=manager,
            detail=f"automatic provisioning is not supported for {manager}",
        )

    # Reusing an already-cached runtime needs neither npm nor network access.
    version = declared_version or "latest"
    base = Path(cache_dir) if cache_dir is not None else _default_runtime_cache()
    prefix = base / manager / version
    executable = prefix / "node_modules" / ".bin" / manager
    if executable.is_file():
        return PackageManagerPreparation(
            package_manager=manager, executable=executable, version=version, installed=True
        )

    if _which("npm") is None:
        return PackageManagerPreparation(
            package_manager=manager,
            version=declared_version,
            detail=f"{manager} not found and npm is unavailable to provision it",
        )

    # A lockfile tells us which manager is required. A repository that omits
    # packageManager gets the current official client, but a declared version
    # is always honoured so a Yarn Berry monorepo does not receive Yarn 1.
    package = _runtime_package(manager, version)

    argv = [
        "npm",
        "install",
        "--ignore-scripts",
        "--prefix",
        str(prefix),
        "--no-save",
        f"{package}@{version}",
    ]
    # Package-manager bootstrap must reach npm's registry.
    net_allowlist = frozenset(
        {"http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    )
    result = run_sandboxed(
        argv,
        cwd=workspace,
        timeout=timeout,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        extra_allowlist=net_allowlist,
    )
    if not result.ok:
        message = result.error or result.stderr.strip() or f"exit code {result.exit_code}"
        return PackageManagerPreparation(
            package_manager=manager,
            version=version,
            detail=f"could not provision {manager}@{version}: {message}",
        )
    if not executable.is_file():
        return PackageManagerPreparation(
            package_manager=manager,
            version=version,
            detail=f"provisioning {manager}@{version} completed but no executable was installed",
        )
    return PackageManagerPreparation(
        package_manager=manager, executable=executable, version=version, installed=True
    )


def _declared_manager_version(root: Path, manager: str) -> str:
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    raw = package.get("packageManager")
    if not isinstance(raw, str):
        return ""
    match = _PACKAGE_MANAGER_SPEC.fullmatch(raw.strip())
    if match is None or match.group("name") != manager:
        return ""
    return match.group("version")


def _runtime_package(manager: str, version: str) -> str:
    # Yarn v2+ is Berry and is distributed as the official CLI dist package;
    # the `yarn` npm package only supplies classic Yarn v1.
    if manager == "yarn" and version not in {"latest", ""}:
        try:
            if int(version.split(".", 1)[0]) >= 2:
                return "@yarnpkg/cli-dist"
        except ValueError:
            pass
    return manager


def _default_runtime_cache() -> Path:
    return Path.home() / ".depfix" / "package-managers"


def _is_yarn_berry(version: str) -> bool:
    try:
        return int(version.split(".", 1)[0]) >= 2
    except ValueError:
        return False


def _install_argv(
    package_manager: str,
    root: Path,
    *,
    ignore_scripts: bool,
    version: str = "",
    frozen: bool = True,
) -> list[str]:
    has_lockfile = frozen and any(
        (root / lockfile).is_file()
        for lockfile, mgr in _LOCKFILE_MANAGERS
        if mgr == package_manager
    )
    yarn_berry = package_manager == "yarn" and _is_yarn_berry(version)

    if package_manager == "bun":
        argv = ["bun", "install"]
        if has_lockfile:
            argv.append("--frozen-lockfile")
    elif package_manager == "npm":
        argv = ["npm", "ci"] if has_lockfile else ["npm", "install"]
    elif package_manager == "yarn":
        argv = ["yarn", "install"]
        if has_lockfile:
            argv.append("--immutable" if yarn_berry else "--frozen-lockfile")
    elif package_manager == "pnpm":
        argv = ["pnpm", "install"]
        if has_lockfile:
            argv.append("--frozen-lockfile")
        elif not frozen:
            argv.append("--prefer-offline")
    else:
        argv = ["npm", "install"]

    if yarn_berry and ignore_scripts:
        argv.append("--mode=skip-build")
    elif ignore_scripts:
        argv.append("--ignore-scripts")
    return argv


def build_install_argv(
    root: str | Path,
    *,
    ignore_scripts: bool,
    package_manager: str | None = None,
    frozen: bool = True,
) -> list[str]:
    """Public entry point for the install command :func:`install_dependencies`
    would run for ``root``, for callers (e.g. :mod:`depfix.codemods.lockfile`)
    that need to regenerate a lockfile without duplicating the detection
    logic and without reaching into this module's private helpers."""
    root = Path(root)
    manager = package_manager or detect_package_manager(root)
    return _install_argv(
        manager,
        root,
        ignore_scripts=ignore_scripts,
        version=_declared_manager_version(root, manager),
        frozen=frozen,
    )


def install_dependencies(
    root: str | Path,
    *,
    timeout: float,
    ignore_scripts: bool = True,
    package_manager: str | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ecosystem: str = "javascript",
    frozen: bool = True,
) -> InstallResult:
    """Install ``root``'s dependencies, defaulting to ``--ignore-scripts``
    so a hostile ``postinstall``/``preinstall`` in a dependency (or the repo
    itself) doesn't get to run arbitrary code as a side effect of merely
    verifying a fix -- at the cost of tests that genuinely need a build
    step (native addons, codegen) failing for reasons unrelated to the fix
    under test. That tradeoff is intentional: see docs/decisions.md.

    ``ecosystem="python"`` installs into a throwaway in-checkout virtualenv
    instead (see :mod:`depfix.ecosystems.python_runtime` for the isolation
    model and its own note on why pip has no ``--ignore-scripts`` analogue).
    """
    root = Path(root)
    if ecosystem == "go":
        return _install_go_dependencies(root, timeout=timeout, max_output_bytes=max_output_bytes)
    if ecosystem == "python":
        return _install_python_dependencies(
            root, timeout=timeout, max_output_bytes=max_output_bytes
        )
    if ecosystem == "ruby":
        return _install_ruby_dependencies(
            root, timeout=timeout, max_output_bytes=max_output_bytes, frozen=frozen
        )

    if not (root / "package.json").is_file():
        return InstallResult(package_manager="none", skipped_reason="no package.json found")

    from depfix.scanners.workspace import find_workspace_root

    install_root = find_workspace_root(root)
    manager = package_manager or detect_package_manager(install_root)
    prepared = prepare_package_manager(
        install_root, timeout=min(timeout, 90.0), package_manager=manager
    )
    if not prepared.ready:
        return InstallResult(package_manager=manager, skipped_reason=prepared.detail)
    argv = _install_argv(
        manager,
        install_root,
        ignore_scripts=ignore_scripts,
        version=prepared.version,
        frozen=frozen,
    )
    # Preserve the familiar bare command when it was already installed. A
    # managed runtime needs its absolute path because its cache directory is
    # intentionally not added to the operator's global PATH.
    if prepared.installed:
        argv[0] = str(prepared.executable)

    logger.info("installing dependencies: %s (cwd=%s)", " ".join(argv), install_root)
    result = run_sandboxed(
        argv, cwd=install_root, timeout=timeout, max_output_bytes=max_output_bytes
    )
    return InstallResult(package_manager=manager, sandbox=result)


def _install_go_dependencies(
    root: Path, *, timeout: float, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
) -> InstallResult:
    """Populate the module cache for this checkout.

    ``go mod download`` fetches and hashes modules without executing any
    package code, which makes it the least dangerous install step of the four
    fix-supported ecosystems.
    """
    from depfix.ecosystems.go_runtime import go, install_argv
    from depfix.ecosystems.specs import GO

    if go() is None:
        return InstallResult(package_manager="go", skipped_reason="go not on PATH")
    argv = install_argv(root)
    if argv is None:
        return InstallResult(package_manager="go", skipped_reason="no go.mod found")
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=GO.env_allowlist_extra,
    )
    return InstallResult(package_manager="go", sandbox=result)


def _install_python_dependencies(
    root: Path, *, timeout: float, max_output_bytes: int
) -> InstallResult:
    """Create the checkout's ``.depfix-venv`` (once) and pip-install into it."""
    from depfix.ecosystems.python_runtime import create_venv_argv, install_argv
    from depfix.ecosystems.specs import PYTHON

    if not _python_has_manifest(root):
        return InstallResult(
            package_manager="none",
            skipped_reason="no requirements.txt/pyproject.toml/setup.py found",
        )

    venv_argv = create_venv_argv(root)
    if venv_argv is not None:
        venv_result = run_sandboxed(
            venv_argv,
            cwd=root,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            extra_allowlist=PYTHON.env_allowlist_extra,
        )
        if not venv_result.ok:
            return InstallResult(package_manager="pip", sandbox=venv_result)

    argv = install_argv(root)
    if argv is None:
        return InstallResult(package_manager="pip", skipped_reason="python3 not found on PATH")
    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=PYTHON.env_allowlist_extra,
    )
    return InstallResult(package_manager="pip", sandbox=result)


def _python_has_manifest(root: Path) -> bool:
    from depfix.ecosystems.python_runtime import has_installable_manifest

    return has_installable_manifest(root)


def _install_ruby_dependencies(
    root: Path,
    *,
    timeout: float,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    frozen: bool = True,
) -> InstallResult:
    from depfix.ecosystems.ruby_runtime import build_ruby_install_argv, bundler

    if bundler() is None:
        return InstallResult(package_manager="bundler", skipped_reason="bundler not on PATH")
    if not (root / "Gemfile").is_file():
        return InstallResult(package_manager="bundler", skipped_reason="no Gemfile found")
    argv = build_ruby_install_argv(root, frozen=frozen)
    from depfix.ecosystems.specs import RUBY

    result = run_sandboxed(
        argv,
        cwd=root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        extra_allowlist=RUBY.env_allowlist_extra,
    )
    return InstallResult(package_manager="bundler", sandbox=result)


def _which(executable: str) -> str | None:
    import shutil

    return shutil.which(executable)
