"""The top-10 ecosystem definitions.

``javascript`` and ``python`` are fix-supported (full pipeline). The other
eight are scan-supported: depfix detects the repo, finds SDK dependency
declarations in its manifests, and flags import-level call sites -- enough
to answer "is this repo affected by this breaking change", while fix
generation declines honestly (an LLM edit nobody can syntax-check or
test-verify locally must not become a PR).

Import-pattern builders are deliberately conservative: they anchor on the
ecosystem's real import syntax, and when a package name only *suggests* a
namespace (NuGet, SwiftPM, Composer), the guess is documented at the
builder. False negatives are acceptable here; confident-looking false
positives are not.
"""

from __future__ import annotations

import re
from typing import Any

from depfix.ecosystems import manifests
from depfix.ecosystems.base import EcosystemSpec, compile_all


def _studly(segment: str) -> str:
    return "".join(word.capitalize() for word in segment.split("-") if word)


def _python_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    module = package.replace("-", "_")
    dotted = package.replace("-", ".")
    names = {re.escape(module), re.escape(dotted)}
    alternatives = "|".join(sorted(names))
    return compile_all(
        [
            rf"^\s*import\s+(?:{alternatives})\b",
            rf"^\s*from\s+(?:{alternatives})\b",
        ]
    )


def _go_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    base = re.sub(r"/v\d+$", "", package.rstrip("/"))
    return compile_all([rf'"{re.escape(base)}(?:/v\d+)?(?:/[\w./-]*)?"'])


def _rust_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    crate = package.replace("-", "_")
    return compile_all(
        [
            rf"^\s*use\s+{re.escape(crate)}\b",
            rf"^\s*extern\s+crate\s+{re.escape(crate)}\b",
            rf"\b{re.escape(crate)}::",
        ]
    )


def _ruby_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    return compile_all([rf"""require\s+['"]{re.escape(package)}(?:['"]|/)"""])


def _php_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    # "stripe/stripe-php" -> vendor namespace guess "Stripe". Composer names
    # don't determine PSR-4 namespaces; this is the common-convention guess
    # and is why PHP import hits are reported at MEDIUM confidence.
    tail = package.rsplit("/", 1)[-1]
    for suffix in ("-php", "-sdk"):
        tail = tail.removesuffix(suffix)
    namespace = _studly(tail)
    return compile_all(
        [
            rf"^\s*use\s+{re.escape(namespace)}\\",
            rf"\bnew\s+\\?{re.escape(namespace)}\\",
        ]
    )


def _jvm_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    # "com.stripe:stripe-java" -> imports under the group id.
    group = package.partition(":")[0]
    return compile_all([rf"^\s*import\s+(?:static\s+)?{re.escape(group)}\."])


def _csharp_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    # "Stripe.net" -> root namespace guess "Stripe"; plain names pass through.
    namespace = package[:-4] if package.lower().endswith(".net") else package.split(".")[0]
    return compile_all([rf"^\s*(?:global\s+)?using\s+{re.escape(namespace)}\b"])


def _swift_import_regexes(package: str) -> tuple[re.Pattern[str], ...]:
    # "stripe-ios" -> module guess "Stripe" (SwiftPM module names are not
    # derivable from repo names; convention-based guess, MEDIUM confidence).
    tail = package
    for suffix in ("-ios", "-swift", "-spm"):
        tail = tail.removesuffix(suffix)
    return compile_all([rf"^\s*import\s+{re.escape(_studly(tail))}\b"])


_C_STYLE_COMMENTS: dict[str, Any] = {
    "line_comments": ("//",),
    "block_comments": (("/*", "*/"),),
}


JAVASCRIPT = EcosystemSpec(
    id="javascript",
    display_name="JavaScript/TypeScript",
    registry_aliases=("npm",),
    source_extensions=frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}),
    markers=(
        ("package.json", 10),
        ("package-lock.json", 5),
        ("yarn.lock", 5),
        ("pnpm-lock.yaml", 5),
        ("bun.lockb", 5),
        ("tsconfig.json", 8),
    ),
    manifest_names=("package.json",),
    lockfile_names=(
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
    ),
    fix_supported=True,
    # scanning/manifests/install/test stay with the dedicated npm modules
    # (scanners.manifest, scanners.callsites, verify.manager, verify.runner);
    # this spec exists so detection/dispatch has one authority.
    **_C_STYLE_COMMENTS,
)

PYTHON = EcosystemSpec(
    id="python",
    display_name="Python",
    registry_aliases=("pypi", "pip"),
    source_extensions=frozenset({".py"}),
    markers=(
        ("pyproject.toml", 10),
        ("setup.py", 8),
        ("requirements.txt", 5),
        ("Pipfile", 5),
        ("poetry.lock", 5),
        ("uv.lock", 5),
    ),
    manifest_names=("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
    lockfile_names=("poetry.lock", "Pipfile.lock", "uv.lock"),
    fix_supported=True,
    env_allowlist_extra=frozenset(
        {
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "PYTHONHOME",
            "PIP_CACHE_DIR",
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "PIP_DISABLE_PIP_VERSION_CHECK",
        }
    ),
    line_comments=("#",),
    import_regexes=_python_import_regexes,
    scan_dependencies=manifests.scan_python_dependencies,
)

JAVA = EcosystemSpec(
    id="java",
    display_name="Java",
    registry_aliases=("maven", "gradle"),
    source_extensions=frozenset({".java"}),
    markers=(("pom.xml", 10), ("build.gradle", 10), ("settings.gradle", 4)),
    manifest_names=("pom.xml", "build.gradle"),
    import_regexes=_jvm_import_regexes,
    scan_dependencies=manifests.scan_jvm_dependencies,
    **_C_STYLE_COMMENTS,
)

KOTLIN = EcosystemSpec(
    id="kotlin",
    display_name="Kotlin",
    registry_aliases=("maven-kotlin",),
    source_extensions=frozenset({".kt", ".kts"}),
    markers=(("build.gradle.kts", 10), ("settings.gradle.kts", 5)),
    manifest_names=("build.gradle.kts",),
    import_regexes=_jvm_import_regexes,
    scan_dependencies=manifests.scan_jvm_dependencies,
    **_C_STYLE_COMMENTS,
)

GO = EcosystemSpec(
    id="go",
    display_name="Go",
    registry_aliases=("go", "gomod"),
    source_extensions=frozenset({".go"}),
    markers=(("go.mod", 10), ("go.sum", 5)),
    manifest_names=("go.mod",),
    lockfile_names=("go.sum",),
    fix_supported=True,
    env_allowlist_extra=frozenset(
        {
            "GOPATH",
            "GOBIN",
            "GOCACHE",
            "GOMODCACHE",
            "GOFLAGS",
            "GOPROXY",
            "GONOPROXY",
            "GOPRIVATE",
            "GOSUMDB",
            "GONOSUMDB",
            "GOTOOLCHAIN",
            "GOENV",
            "GOOS",
            "GOARCH",
            "CGO_ENABLED",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        }
    ),
    import_regexes=_go_import_regexes,
    scan_dependencies=manifests.scan_go_dependencies,
    **_C_STYLE_COMMENTS,
)

RUST = EcosystemSpec(
    id="rust",
    display_name="Rust",
    registry_aliases=("cargo", "crates"),
    source_extensions=frozenset({".rs"}),
    markers=(("Cargo.toml", 10), ("Cargo.lock", 5)),
    manifest_names=("Cargo.toml",),
    lockfile_names=("Cargo.lock",),
    import_regexes=_rust_import_regexes,
    scan_dependencies=manifests.scan_rust_dependencies,
    **_C_STYLE_COMMENTS,
)

RUBY = EcosystemSpec(
    id="ruby",
    display_name="Ruby",
    registry_aliases=("rubygems", "gem"),
    source_extensions=frozenset({".rb"}),
    markers=(("Gemfile", 10), ("Gemfile.lock", 5), ("Rakefile", 3)),
    manifest_names=("Gemfile",),
    lockfile_names=("Gemfile.lock",),
    line_comments=("#",),
    block_comments=(("=begin", "=end"),),
    import_regexes=_ruby_import_regexes,
    scan_dependencies=manifests.scan_ruby_dependencies,
    fix_supported=True,
    env_allowlist_extra=frozenset(
        {
            "GEM_HOME",
            "GEM_PATH",
            "BUNDLE_PATH",
            "BUNDLE_GEMFILE",
        }
    ),
)

PHP = EcosystemSpec(
    id="php",
    display_name="PHP",
    registry_aliases=("composer", "packagist"),
    source_extensions=frozenset({".php"}),
    markers=(("composer.json", 10), ("composer.lock", 5)),
    manifest_names=("composer.json",),
    lockfile_names=("composer.lock",),
    line_comments=("//", "#"),
    block_comments=(("/*", "*/"),),
    import_regexes=_php_import_regexes,
    scan_dependencies=manifests.scan_php_dependencies,
    import_confidence="medium",
)

CSHARP = EcosystemSpec(
    id="csharp",
    display_name="C#/.NET",
    registry_aliases=("nuget",),
    source_extensions=frozenset({".cs"}),
    markers=(("*.sln", 8), ("*.csproj", 10), ("packages.config", 5), ("nuget.config", 3)),
    manifest_names=("*.csproj", "packages.config", "Directory.Packages.props"),
    skip_dirs=frozenset({"bin", "obj"}),
    import_regexes=_csharp_import_regexes,
    scan_dependencies=manifests.scan_nuget_dependencies,
    import_confidence="medium",
    **_C_STYLE_COMMENTS,
)

SWIFT = EcosystemSpec(
    id="swift",
    display_name="Swift",
    registry_aliases=("swiftpm", "spm", "cocoapods"),
    source_extensions=frozenset({".swift"}),
    markers=(("Package.swift", 10), ("Package.resolved", 5), ("Podfile", 4)),
    manifest_names=("Package.swift",),
    lockfile_names=("Package.resolved", "Podfile.lock"),
    skip_dirs=frozenset({".build", "Pods"}),
    import_regexes=_swift_import_regexes,
    scan_dependencies=manifests.scan_swift_dependencies,
    import_confidence="medium",
    **_C_STYLE_COMMENTS,
)

ALL_SPECS: tuple[EcosystemSpec, ...] = (
    JAVASCRIPT,
    PYTHON,
    JAVA,
    KOTLIN,
    GO,
    RUST,
    RUBY,
    PHP,
    CSHARP,
    SWIFT,
)
