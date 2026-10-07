"""Unit tests for non-npm manifest scanning (``ecosystems/manifests.py``).

One parser per ecosystem, each answering "which of these SDK packages does
this checkout declare, at what version". The parsers are deliberately
tolerant, so alongside the happy paths every ecosystem gets a
garbage-manifest test asserting it contributes nothing rather than raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from depfix.ecosystems.manifests import (
    iter_manifest_files,
    parse_requirements_txt,
    scan_go_dependencies,
    scan_jvm_dependencies,
    scan_nuget_dependencies,
    scan_php_dependencies,
    scan_python_dependencies,
    scan_ruby_dependencies,
    scan_rust_dependencies,
    scan_swift_dependencies,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _only(declarations: list) -> object:
    assert len(declarations) == 1, declarations
    return declarations[0]


# -- iter_manifest_files -------------------------------------------------------


def test_iter_manifest_files_finds_nested_manifests_and_skips_skip_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", "")
    _write(tmp_path / "crates" / "api" / "Cargo.toml", "")
    _write(tmp_path / "target" / "vendored" / "Cargo.toml", "")

    found = iter_manifest_files(tmp_path, ["Cargo.toml"], frozenset({"target"}))

    relative = {p.relative_to(tmp_path).as_posix() for p in found}
    assert relative == {"Cargo.toml", "crates/api/Cargo.toml"}


def test_iter_manifest_files_supports_glob_names(tmp_path: Path) -> None:
    _write(tmp_path / "App.csproj", "")
    _write(tmp_path / "lib" / "Lib.csproj", "")
    _write(tmp_path / "notes.txt", "")

    found = iter_manifest_files(tmp_path, ["*.csproj"], frozenset())

    assert {p.name for p in found} == {"App.csproj", "Lib.csproj"}


# -- Python (PyPI) -------------------------------------------------------------


def test_parse_requirements_txt_reads_pin_extras_and_skips_comments_and_editable() -> None:
    text = (
        "# a comment line\n"
        "openai==1.2.3\n"
        "anthropic[vertex]>=0.27  # trailing comment\n"
        "-e .\n"
        "-r other.txt\n"
        "git+https://github.com/psf/requests.git#egg=requests\n"
        "bare-package\n"
    )

    parsed = parse_requirements_txt(text, {"openai", "anthropic", "requests", "bare-package"})

    assert parsed == {"openai": "==1.2.3", "anthropic": ">=0.27", "bare-package": None}


def test_parse_requirements_txt_keeps_requirements_named_like_a_url() -> None:
    """The URL-line guard filters ``http://``/``https://`` direct references
    only -- a requirement legitimately *named* ``httpx`` must survive it."""
    parsed = parse_requirements_txt("httpx==0.27.0\nhttps://example.com/pkg.whl\n", {"httpx"})

    assert parsed == {"httpx": "==0.27.0"}


def test_scan_python_dependencies_reads_requirements_txt_pin(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "openai==1.2.3\n")

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.package == "openai"
    assert declaration.declared_version == "==1.2.3"
    assert declaration.resolved_version is None
    assert declaration.source == "manifest"
    assert declaration.manifest_path == str(tmp_path / "requirements.txt")


def test_scan_python_dependencies_reads_pep621_project_dependencies(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "app"\ndependencies = ["openai>=1.0,<2", "rich"]\n',
    )

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.declared_version == ">=1.0,<2"
    assert declaration.source == "manifest"


def test_scan_python_dependencies_reads_poetry_dependency_table(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        '[tool.poetry.dependencies]\npython = "^3.12"\nopenai = "^1.2.0"\n',
    )

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.package == "openai"
    assert declaration.declared_version == "^1.2.0"


def test_scan_python_dependencies_resolves_version_from_poetry_lock(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "openai>=1.0\n")
    _write(
        tmp_path / "poetry.lock",
        '[[package]]\nname = "openai"\nversion = "1.6.1"\n\n'
        '[[package]]\nname = "rich"\nversion = "13.0.0"\n',
    )

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.declared_version == ">=1.0"
    assert declaration.resolved_version == "1.6.1"
    assert declaration.source == "lockfile"


def test_scan_python_dependencies_resolves_version_from_pipfile_lock(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "openai\n")
    _write(
        tmp_path / "Pipfile.lock",
        json.dumps({"default": {"openai": {"version": "==1.4.0"}}}),
    )

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.declared_version is None
    assert declaration.resolved_version == "1.4.0"
    assert declaration.source == "lockfile"


def test_scan_python_dependencies_normalizes_names_per_pep503(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "Openai_X==2.0.0\n")

    declaration = _only(scan_python_dependencies(tmp_path, ["openai-x"]))

    # The declaration reports the package as providers.yaml spells it, not
    # as the requirement line spells it.
    assert declaration.package == "openai-x"
    assert declaration.declared_version == "==2.0.0"


def test_scan_python_dependencies_prefers_first_manifest_for_a_package(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "openai==1.0.0\n")
    _write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["openai==9.9.9"]\n')

    declaration = _only(scan_python_dependencies(tmp_path, ["openai"]))

    assert declaration.manifest_path == str(tmp_path / "requirements.txt")
    assert declaration.declared_version == "==1.0.0"


def test_scan_python_dependencies_ignores_unparseable_manifests(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "this is [not valid toml\n")
    (tmp_path / "requirements.txt").write_bytes(b"\xff\xfe openai==1.0\n")

    assert scan_python_dependencies(tmp_path, ["openai"]) == []


# -- Go ------------------------------------------------------------------------


def test_scan_go_dependencies_reads_require_block(tmp_path: Path) -> None:
    _write(
        tmp_path / "go.mod",
        "module example.com/app\n\ngo 1.21\n\n"
        "require (\n"
        "\tgithub.com/stripe/stripe-go/v76 v76.12.0\n"
        "\tgithub.com/pkg/errors v0.9.1\n"
        ")\n",
    )

    declaration = _only(scan_go_dependencies(tmp_path, ["github.com/stripe/stripe-go"]))

    assert declaration.package == "github.com/stripe/stripe-go"
    assert declaration.declared_version == "v76.12.0"
    # go.mod pins exactly, so declared and resolved agree even without go.sum.
    assert declaration.resolved_version == "v76.12.0"
    assert declaration.source == "manifest"


def test_scan_go_dependencies_matches_exact_module_path(tmp_path: Path) -> None:
    _write(tmp_path / "go.mod", "require github.com/openai/openai-go v0.1.0\n")

    declaration = _only(scan_go_dependencies(tmp_path, ["github.com/openai/openai-go"]))

    assert declaration.declared_version == "v0.1.0"


def test_scan_go_dependencies_ignores_unrelated_modules(tmp_path: Path) -> None:
    _write(tmp_path / "go.mod", "require (\n\tgithub.com/pkg/errors v0.9.1\n)\n")

    assert scan_go_dependencies(tmp_path, ["github.com/stripe/stripe-go"]) == []


def test_scan_go_dependencies_ignores_unreadable_gomod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_bytes(b"\xff\xfe require x v1.0.0\n")

    assert scan_go_dependencies(tmp_path, ["x"]) == []


# -- Rust (crates.io) ----------------------------------------------------------


def test_scan_rust_dependencies_reads_string_and_table_constraints(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "app"\n\n'
        "[dependencies]\n"
        'async-openai = "0.18"\n'
        'reqwest = { version = "0.11", features = ["json"] }\n',
    )

    declarations = scan_rust_dependencies(tmp_path, ["async-openai", "reqwest"])

    by_name = {d.package: d for d in declarations}
    assert by_name["async-openai"].declared_version == "0.18"
    assert by_name["reqwest"].declared_version == "0.11"
    assert by_name["reqwest"].source == "manifest"
    assert by_name["reqwest"].resolved_version is None


def test_scan_rust_dependencies_resolves_version_from_cargo_lock(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[dependencies]\nasync-openai = "0.18"\n')
    _write(
        tmp_path / "Cargo.lock",
        '[[package]]\nname = "async-openai"\nversion = "0.18.3"\n',
    )

    declaration = _only(scan_rust_dependencies(tmp_path, ["async-openai"]))

    assert declaration.declared_version == "0.18"
    assert declaration.resolved_version == "0.18.3"
    assert declaration.source == "lockfile"


def test_scan_rust_dependencies_treats_underscore_and_hyphen_as_equivalent(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[dependencies]\nasync_openai = "0.18"\n')

    declaration = _only(scan_rust_dependencies(tmp_path, ["async-openai"]))

    assert declaration.package == "async-openai"
    assert declaration.declared_version == "0.18"


def test_scan_rust_dependencies_reads_dev_dependencies(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[dev-dependencies]\nasync-openai = "0.18"\n')

    assert len(scan_rust_dependencies(tmp_path, ["async-openai"])) == 1


def test_scan_rust_dependencies_ignores_invalid_cargo_toml(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", "[dependencies\nasync-openai = \n")

    assert scan_rust_dependencies(tmp_path, ["async-openai"]) == []


# -- Ruby (RubyGems) -----------------------------------------------------------


def test_scan_ruby_dependencies_reads_gemfile_constraint(tmp_path: Path) -> None:
    _write(
        tmp_path / "Gemfile",
        'source "https://rubygems.org"\n\ngem "stripe", "~> 5.0"\ngem "rails"\n',
    )

    declaration = _only(scan_ruby_dependencies(tmp_path, ["stripe"]))

    assert declaration.package == "stripe"
    assert declaration.declared_version == "~> 5.0"
    assert declaration.resolved_version is None
    assert declaration.source == "manifest"


def test_scan_ruby_dependencies_resolves_version_from_gemfile_lock(tmp_path: Path) -> None:
    _write(tmp_path / "Gemfile", 'gem "stripe", "~> 5.0"\n')
    _write(
        tmp_path / "Gemfile.lock",
        "GEM\n  remote: https://rubygems.org/\n  specs:\n    stripe (5.29.0)\n    rake (13.0.6)\n",
    )

    declaration = _only(scan_ruby_dependencies(tmp_path, ["stripe"]))

    assert declaration.declared_version == "~> 5.0"
    assert declaration.resolved_version == "5.29.0"
    assert declaration.source == "lockfile"


def test_scan_ruby_dependencies_ignores_gems_not_declared_in_gemfile(tmp_path: Path) -> None:
    _write(tmp_path / "Gemfile", 'gem "rails"\n')
    _write(tmp_path / "Gemfile.lock", "  specs:\n    stripe (5.29.0)\n")

    assert scan_ruby_dependencies(tmp_path, ["stripe"]) == []


# -- PHP (Composer) ------------------------------------------------------------


def test_scan_php_dependencies_reads_composer_require(tmp_path: Path) -> None:
    _write(
        tmp_path / "composer.json",
        json.dumps({"require": {"php": ">=8.1", "stripe/stripe-php": "^7.0"}}),
    )

    declaration = _only(scan_php_dependencies(tmp_path, ["stripe/stripe-php"]))

    assert declaration.package == "stripe/stripe-php"
    assert declaration.declared_version == "^7.0"
    assert declaration.source == "manifest"


def test_scan_php_dependencies_strips_v_prefix_from_lockfile_version(tmp_path: Path) -> None:
    _write(tmp_path / "composer.json", json.dumps({"require": {"stripe/stripe-php": "^7.0"}}))
    _write(
        tmp_path / "composer.lock",
        json.dumps({"packages": [{"name": "stripe/stripe-php", "version": "v7.128.0"}]}),
    )

    declaration = _only(scan_php_dependencies(tmp_path, ["stripe/stripe-php"]))

    assert declaration.resolved_version == "7.128.0"
    assert declaration.source == "lockfile"


def test_scan_php_dependencies_reads_require_dev_section(tmp_path: Path) -> None:
    _write(
        tmp_path / "composer.json",
        json.dumps({"require-dev": {"stripe/stripe-php": "^7.0"}}),
    )

    assert len(scan_php_dependencies(tmp_path, ["stripe/stripe-php"])) == 1


def test_scan_php_dependencies_ignores_invalid_composer_json(tmp_path: Path) -> None:
    _write(tmp_path / "composer.json", "{not json,,,")

    assert scan_php_dependencies(tmp_path, ["stripe/stripe-php"]) == []


# -- Java / Kotlin (Maven coordinates) -----------------------------------------


_POM_WITH_NAMESPACE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>com.stripe</groupId>
      <artifactId>stripe-java</artifactId>
      <version>24.16.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
    </dependency>
  </dependencies>
</project>
"""

_POM_WITHOUT_NAMESPACE = """<project>
  <dependencies>
    <dependency>
      <groupId>com.stripe</groupId>
      <artifactId>stripe-java</artifactId>
      <version>25.0.0</version>
    </dependency>
  </dependencies>
</project>
"""


def test_scan_jvm_dependencies_reads_namespaced_pom(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", _POM_WITH_NAMESPACE)

    declaration = _only(scan_jvm_dependencies(tmp_path, ["com.stripe:stripe-java"]))

    assert declaration.package == "com.stripe:stripe-java"
    assert declaration.declared_version == "24.16.0"
    assert declaration.source == "manifest"


def test_scan_jvm_dependencies_reads_pom_without_namespace(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", _POM_WITHOUT_NAMESPACE)

    declaration = _only(scan_jvm_dependencies(tmp_path, ["com.stripe:stripe-java"]))

    assert declaration.declared_version == "25.0.0"


def test_scan_jvm_dependencies_matches_bare_artifact_id(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", _POM_WITH_NAMESPACE)

    declaration = _only(scan_jvm_dependencies(tmp_path, ["stripe-java"]))

    assert declaration.package == "stripe-java"
    assert declaration.declared_version == "24.16.0"


def test_scan_jvm_dependencies_ignores_wrong_group_for_same_artifact(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", _POM_WITH_NAMESPACE)

    assert scan_jvm_dependencies(tmp_path, ["com.example:stripe-java"]) == []


def test_scan_jvm_dependencies_reads_gradle_coordinate_string(tmp_path: Path) -> None:
    _write(
        tmp_path / "build.gradle",
        'dependencies {\n    implementation "com.stripe:stripe-java:24.16.0"\n}\n',
    )

    declaration = _only(scan_jvm_dependencies(tmp_path, ["com.stripe:stripe-java"]))

    assert declaration.declared_version == "24.16.0"
    assert declaration.manifest_path == str(tmp_path / "build.gradle")


def test_scan_jvm_dependencies_reads_gradle_kts_coordinate_without_version(tmp_path: Path) -> None:
    _write(
        tmp_path / "build.gradle.kts",
        'dependencies {\n    implementation("com.stripe:stripe-java")\n}\n',
    )

    declaration = _only(scan_jvm_dependencies(tmp_path, ["com.stripe:stripe-java"]))

    assert declaration.declared_version is None


def test_scan_jvm_dependencies_ignores_malformed_pom(tmp_path: Path) -> None:
    _write(tmp_path / "pom.xml", "<project><dependencies>")

    assert scan_jvm_dependencies(tmp_path, ["com.stripe:stripe-java"]) == []


# -- C# (NuGet) ----------------------------------------------------------------


def test_scan_nuget_dependencies_finds_package_reference_with_version_attribute(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "App.csproj",
        '<Project Sdk="Microsoft.NET.Sdk">\n  <ItemGroup>\n'
        '    <PackageReference Include="Stripe.net" Version="43.20.0" />\n'
        "  </ItemGroup>\n</Project>\n",
    )

    declaration = _only(scan_nuget_dependencies(tmp_path, ["Stripe.net"]))

    assert declaration.package == "Stripe.net"
    assert declaration.source == "manifest"
    assert declaration.declared_version == "43.20.0"


def test_scan_nuget_dependencies_finds_package_reference_without_version_attribute(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "App.csproj",
        '<Project>\n  <ItemGroup>\n    <PackageReference Include="Stripe.net" />\n'
        "  </ItemGroup>\n</Project>\n",
    )

    declaration = _only(scan_nuget_dependencies(tmp_path, ["Stripe.net"]))

    assert declaration.declared_version is None


def test_scan_nuget_dependencies_matches_package_name_case_insensitively(tmp_path: Path) -> None:
    _write(tmp_path / "App.csproj", '<PackageReference Include="stripe.NET" />')

    declaration = _only(scan_nuget_dependencies(tmp_path, ["Stripe.net"]))

    # The declaration keeps the providers.yaml spelling, not the csproj's.
    assert declaration.package == "Stripe.net"


def test_scan_nuget_dependencies_skips_bin_and_obj_directories(tmp_path: Path) -> None:
    _write(tmp_path / "obj" / "Gen.csproj", '<PackageReference Include="Stripe.net" />')

    assert scan_nuget_dependencies(tmp_path, ["Stripe.net"]) == []


def test_scan_nuget_dependencies_ignores_unreadable_csproj(tmp_path: Path) -> None:
    (tmp_path / "App.csproj").write_bytes(b'\xff\xfe<PackageReference Include="Stripe.net" />')

    assert scan_nuget_dependencies(tmp_path, ["Stripe.net"]) == []


# -- Swift (SwiftPM) -----------------------------------------------------------


_PACKAGE_SWIFT = """// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "App",
    dependencies: [
        .package(url: "https://github.com/stripe/stripe-ios.git", from: "23.18.0"),
    ]
)
"""


def test_scan_swift_dependencies_reads_package_url_from_constraint(tmp_path: Path) -> None:
    _write(tmp_path / "Package.swift", _PACKAGE_SWIFT)

    declaration = _only(scan_swift_dependencies(tmp_path, ["stripe-ios"]))

    assert declaration.package == "stripe-ios"
    assert declaration.declared_version == "23.18.0"
    assert declaration.resolved_version is None
    assert declaration.source == "manifest"


def test_scan_swift_dependencies_reads_exact_constraint(tmp_path: Path) -> None:
    _write(
        tmp_path / "Package.swift",
        '.package(url: "https://github.com/stripe/stripe-ios", exact: "23.18.0")\n',
    )

    declaration = _only(scan_swift_dependencies(tmp_path, ["stripe-ios"]))

    assert declaration.declared_version == "23.18.0"


def test_scan_swift_dependencies_resolves_v2_package_resolved_pins(tmp_path: Path) -> None:
    _write(tmp_path / "Package.swift", _PACKAGE_SWIFT)
    _write(
        tmp_path / "Package.resolved",
        json.dumps(
            {
                "version": 2,
                "pins": [{"identity": "stripe-ios", "state": {"version": "23.21.1"}}],
            }
        ),
    )

    declaration = _only(scan_swift_dependencies(tmp_path, ["stripe-ios"]))

    assert declaration.resolved_version == "23.21.1"
    assert declaration.source == "lockfile"


def test_scan_swift_dependencies_resolves_v1_object_pins(tmp_path: Path) -> None:
    _write(tmp_path / "Package.swift", _PACKAGE_SWIFT)
    _write(
        tmp_path / "Package.resolved",
        json.dumps(
            {
                "version": 1,
                "object": {"pins": [{"package": "stripe-ios", "state": {"version": "23.20.0"}}]},
            }
        ),
    )

    declaration = _only(scan_swift_dependencies(tmp_path, ["stripe-ios"]))

    assert declaration.resolved_version == "23.20.0"
    assert declaration.source == "lockfile"


def test_scan_swift_dependencies_ignores_unrelated_package_urls(tmp_path: Path) -> None:
    _write(
        tmp_path / "Package.swift",
        '.package(url: "https://github.com/apple/swift-log.git", from: "1.0.0")\n',
    )

    assert scan_swift_dependencies(tmp_path, ["stripe-ios"]) == []


def test_scan_swift_dependencies_ignores_invalid_package_resolved(tmp_path: Path) -> None:
    _write(tmp_path / "Package.swift", _PACKAGE_SWIFT)
    _write(tmp_path / "Package.resolved", "{ not json")

    declaration = _only(scan_swift_dependencies(tmp_path, ["stripe-ios"]))

    assert declaration.resolved_version is None
    assert declaration.source == "manifest"
