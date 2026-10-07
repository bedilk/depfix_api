"""Ruby install/test/smoke plumbing for the verify subsystem.

The Python equivalents live in :mod:`depfix.ecosystems.python_runtime`;
this is dispatched to when the checkout's ecosystem is ``ruby``.

**Isolation model.** ``bundle install --path .depfix-bundle`` installs gems
into a throwaway directory inside the checkout, never into the operator's
system gem set -- the moral equivalent of Python's ``.depfix-venv`` or
npm's ``node_modules``. Like installing an sdist, running a gem's install
hook is arbitrary code; the sandbox env-allowlist bounds it, and the
residual risk is the same one already accepted for npm and pip installs.

**Test reporting.** RSpec's ``--format json`` gives per-example identities;
minitest is run through ``minitest-reporters`` JUnit output when present,
falling back to plain ``rake test`` whose aggregate counts the fallback
parser can still read. RSpec is preferred when both look present, because
its structured output is what the identity-diffing verifier wants.
"""

from __future__ import annotations

import shutil
from pathlib import Path

BUNDLE_PATH = ".depfix-bundle"


def bundler() -> str | None:
    return shutil.which("bundle")


def ruby() -> str | None:
    return shutil.which("ruby")


def has_gemfile(root: str | Path) -> bool:
    return (Path(root) / "Gemfile").is_file()


def install_argv(root: str | Path) -> list[str] | None:
    """`bundle install` into the in-checkout throwaway path, or None."""
    if bundler() is None or not has_gemfile(root):
        return None
    return [
        "bundle",
        "install",
        "--path",
        BUNDLE_PATH,
        "--jobs",
        "4",
        "--retry",
        "1",
    ]


def build_ruby_install_argv(root: Path, *, frozen: bool = True) -> list[str]:
    argv = ["bundle", "install", "--path", BUNDLE_PATH, "--jobs", "4"]
    if frozen and (root / "Gemfile.lock").is_file():
        argv.append("--frozen")
    return argv


def _has_rspec(root: Path) -> bool:
    return (root / "spec").is_dir() or (root / ".rspec").is_file()


def _has_minitest(root: Path) -> bool:
    if not (root / "test").is_dir():
        return False
    for pattern in ("*_test.rb", "test_*.rb"):
        if next((root / "test").rglob(pattern), None) is not None:
            return True
    return False


def has_test_setup(root: str | Path) -> bool:
    root = Path(root)
    return _has_rspec(root) or _has_minitest(root)


def test_argv(root: str | Path, junit_xml_path: str | Path) -> list[str] | None:
    """The test command, framework auto-detected.

    RSpec emits JSON to stdout (parsed as structured output). Minitest is
    asked for JUnit XML via minitest-reporters when available; the plain
    ``rake test`` fallback still yields aggregate counts.
    """
    root = Path(root)
    if bundler() is None:
        return None
    if _has_rspec(root):
        return ["bundle", "exec", "rspec", "--format", "json"]
    if _has_minitest(root):
        # minitest-reporters, if the repo bundles it, honours these env
        # vars (set by the runner) to write JUnit XML; otherwise the run
        # still prints a summary line the fallback parser reads.
        return ["bundle", "exec", "rake", "test"]
    return None


def build_ruby_test_argv(root: Path) -> list[str] | None:
    if (root / ".rspec").is_file() or (root / "spec").is_dir():
        return ["bundle", "exec", "rspec", "--format", "json"]
    if (root / "Rakefile").is_file():
        return ["bundle", "exec", "rake", "test"]
    return None


def minitest_junit_env(junit_xml_path: str | Path) -> dict[str, str]:
    """Env for minitest-reporters' JUnit reporter, harmless if unused."""
    return {
        "MINITEST_REPORTERS": "JUnitReporter",
        "MINITEST_REPORTERS_REPORTS_DIR": str(Path(junit_xml_path).parent),
    }


def smoke_argv(root: str | Path, relpath: str) -> list[str] | None:
    """`ruby -c` syntax check for one edited .rb file."""
    if Path(relpath).suffix != ".rb" or ruby() is None:
        return None
    return ["ruby", "-c", relpath]


def detect_ruby_test_framework(root: Path) -> str | None:
    if (root / ".rspec").is_file() or (root / "spec").is_dir():
        return "rspec"
    if (root / "Rakefile").is_file():
        return "minitest"
    return None
