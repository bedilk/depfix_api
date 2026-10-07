"""Unit tests for the Python install/test/smoke plumbing
(``ecosystems/python_runtime.py``).

Every helper here only *builds* argv lists and inspects the checkout -- no
subprocess is ever launched -- so the tests are pure filesystem fixtures.
A ``.depfix-venv/bin/python`` file is enough to make the module believe the
throwaway venv exists; it never executes it.
"""

from __future__ import annotations

from pathlib import Path

from depfix.ecosystems import python_runtime


def _fake_venv(root: Path) -> Path:
    """Create the ``.depfix-venv/bin/python`` marker file (never executed)."""
    python = root / python_runtime.VENV_DIR / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return python


# -- interpreter selection -----------------------------------------------------


def test_venv_python_points_inside_the_checkout(tmp_path: Path) -> None:
    assert python_runtime.venv_python(tmp_path) == tmp_path / ".depfix-venv" / "bin" / "python"


def test_interpreter_for_prefers_the_checkout_venv(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path)

    assert python_runtime.interpreter_for(tmp_path) == str(venv)


def test_interpreter_for_falls_back_to_system_python(tmp_path: Path) -> None:
    interpreter = python_runtime.interpreter_for(tmp_path)

    assert interpreter == python_runtime.system_python()
    assert interpreter is not None


# -- create_venv_argv ----------------------------------------------------------


def test_create_venv_argv_builds_venv_command_when_none_exists(tmp_path: Path) -> None:
    argv = python_runtime.create_venv_argv(tmp_path)

    assert argv is not None
    assert argv[1:] == ["-m", "venv", ".depfix-venv"]
    assert argv[0] == python_runtime.system_python()


def test_create_venv_argv_is_none_when_the_venv_already_exists(tmp_path: Path) -> None:
    _fake_venv(tmp_path)

    assert python_runtime.create_venv_argv(tmp_path) is None


def test_create_venv_argv_accepts_a_string_root(tmp_path: Path) -> None:
    _fake_venv(tmp_path)

    assert python_runtime.create_venv_argv(str(tmp_path)) is None


# -- install_argv --------------------------------------------------------------


def test_install_argv_is_none_without_an_installable_manifest(tmp_path: Path) -> None:
    assert python_runtime.install_argv(tmp_path) is None
    assert python_runtime.has_installable_manifest(tmp_path) is False


def test_install_argv_uses_requirements_txt_when_present(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai==1.2.3\n", encoding="utf-8")

    argv = python_runtime.install_argv(tmp_path)

    assert argv is not None
    assert argv[1:] == ["-m", "pip", "install", "--quiet", "-r", "requirements.txt"]


def test_install_argv_prefers_requirements_txt_over_pyproject(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("openai\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    argv = python_runtime.install_argv(tmp_path)

    assert argv is not None
    assert argv[-2:] == ["-r", "requirements.txt"]


def test_install_argv_installs_the_project_for_pyproject_only_checkout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    argv = python_runtime.install_argv(tmp_path)

    assert argv is not None
    assert argv[1:] == ["-m", "pip", "install", "--quiet", "."]


def test_install_argv_installs_the_project_for_setup_py_checkout(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")

    argv = python_runtime.install_argv(tmp_path)

    assert argv is not None
    assert argv[-1] == "."
    assert python_runtime.has_installable_manifest(tmp_path) is True


def test_install_argv_uses_the_checkout_venv_interpreter_when_it_exists(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path)
    (tmp_path / "requirements.txt").write_text("openai\n", encoding="utf-8")

    argv = python_runtime.install_argv(tmp_path)

    assert argv is not None
    assert argv[0] == str(venv)


# -- has_pytest_setup ----------------------------------------------------------


def test_has_pytest_setup_true_for_pytest_ini_even_when_empty(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is True


def test_has_pytest_setup_true_for_pyproject_with_pytest_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        encoding="utf-8",
    )

    assert python_runtime.has_pytest_setup(tmp_path) is True


def test_has_pytest_setup_false_for_pyproject_without_pytest_mention(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is False


def test_has_pytest_setup_true_for_setup_cfg_mentioning_pytest(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = tests\n", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is True


def test_has_pytest_setup_false_for_tox_ini_without_pytest_mention(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py312\n", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is False


def test_has_pytest_setup_true_for_test_file_naming_convention(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_client.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is True


def test_has_pytest_setup_true_for_suffix_style_test_file_in_test_dir(tmp_path: Path) -> None:
    tests = tmp_path / "test"
    (tests / "nested").mkdir(parents=True)
    (tests / "nested" / "client_test.py").write_text("", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is True


def test_has_pytest_setup_false_for_bare_empty_tests_directory(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()

    assert python_runtime.has_pytest_setup(tmp_path) is False


def test_has_pytest_setup_false_for_tests_directory_without_test_named_files(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "helpers.py").write_text("", encoding="utf-8")

    assert python_runtime.has_pytest_setup(tmp_path) is False


def test_has_pytest_setup_false_for_empty_checkout(tmp_path: Path) -> None:
    assert python_runtime.has_pytest_setup(tmp_path) is False


def test_has_pytest_setup_ignores_an_undecodable_config_file(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_bytes(b"\xff\xfe[tool:pytest]\n")

    assert python_runtime.has_pytest_setup(tmp_path) is False


# -- test_argv / smoke_argv ----------------------------------------------------


def test_test_argv_requests_a_junit_xml_report(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"

    argv = python_runtime.test_argv(tmp_path, report)

    assert argv is not None
    assert argv[1:3] == ["-m", "pytest"]
    assert f"--junit-xml={report}" in argv
    assert "--continue-on-collection-errors" in argv


def test_test_argv_uses_the_checkout_venv_interpreter(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path)

    argv = python_runtime.test_argv(tmp_path, tmp_path / "junit.xml")

    assert argv is not None
    assert argv[0] == str(venv)


def test_smoke_argv_compiles_an_edited_python_file(tmp_path: Path) -> None:
    argv = python_runtime.smoke_argv(tmp_path, "src/app.py")

    assert argv is not None
    assert argv[1:] == ["-m", "py_compile", "src/app.py"]


def test_smoke_argv_is_none_for_a_non_python_file(tmp_path: Path) -> None:
    assert python_runtime.smoke_argv(tmp_path, "src/app.ts") is None
    assert python_runtime.smoke_argv(tmp_path, "README") is None
