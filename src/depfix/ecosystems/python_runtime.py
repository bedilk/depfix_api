"""Python install/test/smoke plumbing for the verify subsystem.

The npm equivalents live in :mod:`depfix.verify.manager` and
:mod:`depfix.verify.runner`; those modules dispatch here when the checkout's
ecosystem is python. Everything runs through the same
:func:`depfix.verify.sandbox.run_sandboxed` envelope (allowlisted env,
process-group kill, output cap).

**Isolation model.** Dependencies install into a throwaway virtualenv
*inside the checkout* (``.depfix-venv``) -- never into the operator's own
environment. That's the moral equivalent of ``node_modules``: disposable,
ignored by scanning (see ``COMMON_SKIP_DIRS``), and gone with the checkout.
There is no pip equivalent of npm's ``--ignore-scripts``: installing an
sdist runs its ``setup.py``, which is arbitrary code, exactly like npm
lifecycle scripts. The sandbox env-allowlist limits what that code can see,
but the residual risk is the same one already accepted (and documented in
docs/decisions.md) for npm installs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

VENV_DIR = ".depfix-venv"

#: pytest configuration/convention markers, checked in order. A bare
#: ``tests/`` directory alone is deliberately NOT enough -- plenty of repos
#: carry an empty or non-pytest tests dir, and a false "has tests" here
#: would send the verifier into an install cycle for nothing.
_PYTEST_MARKERS = ("pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml")


def system_python() -> str | None:
    return shutil.which("python3") or shutil.which("python")


def venv_python(root: str | Path) -> Path:
    return Path(root) / VENV_DIR / "bin" / "python"


def interpreter_for(root: str | Path) -> str | None:
    """The venv's interpreter when the venv exists, else the system one."""
    venv = venv_python(root)
    if venv.is_file():
        return str(venv)
    return system_python()


def create_venv_argv(root: str | Path) -> list[str] | None:
    """Command that creates the checkout's throwaway venv, or None when it
    already exists / no python is available."""
    if venv_python(root).is_file():
        return None
    python = system_python()
    if python is None:
        return None
    return [python, "-m", "venv", VENV_DIR]


def install_argv(root: str | Path) -> list[str] | None:
    """The pip install command for this checkout, or None when there is
    nothing installable."""
    root = Path(root)
    python = str(venv_python(root)) if venv_python(root).is_file() else system_python()
    if python is None:
        return None
    if (root / "requirements.txt").is_file():
        return [python, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"]
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        return [python, "-m", "pip", "install", "--quiet", "."]
    return None


def has_installable_manifest(root: str | Path) -> bool:
    root = Path(root)
    return any(
        (root / name).is_file() for name in ("requirements.txt", "pyproject.toml", "setup.py")
    )


def has_pytest_setup(root: str | Path) -> bool:
    """Does this checkout look like it runs pytest?

    Config-file markers must actually mention pytest (``pyproject.toml``
    and ``setup.cfg``/``tox.ini`` exist for many other reasons); as a
    fallback, a ``tests``/``test`` directory containing ``test_*.py`` /
    ``*_test.py`` files counts, because that is pytest's own default
    discovery convention.
    """
    root = Path(root)
    for marker in _PYTEST_MARKERS:
        path = root / marker
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if marker == "pytest.ini" or "pytest" in text:
            return True
    for test_dir in ("tests", "test"):
        directory = root / test_dir
        if directory.is_dir():
            for pattern in ("test_*.py", "*_test.py"):
                if next(directory.rglob(pattern), None) is not None:
                    return True
    return False


def test_argv(root: str | Path, junit_xml_path: str | Path) -> list[str] | None:
    """The pytest command producing a JUnit XML report at ``junit_xml_path``."""
    python = interpreter_for(root)
    if python is None:
        return None
    return [
        python,
        "-m",
        "pytest",
        "--quiet",
        f"--junit-xml={junit_xml_path}",
        "--continue-on-collection-errors",
    ]


def smoke_argv(root: str | Path, relpath: str) -> list[str] | None:
    """Syntax-level smoke check for one edited ``.py`` file."""
    if Path(relpath).suffix != ".py":
        return None
    python = interpreter_for(root)
    if python is None:
        return None
    return [python, "-m", "py_compile", relpath]
