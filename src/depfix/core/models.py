"""
Data models for the Dependency Fix Agent.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum


class ValidationStatus(Enum):
    """Status of fix validation."""

    VALID = "valid"
    SYNTAX_ERROR = "syntax_error"
    AST_MISMATCH = "ast_mismatch"
    NO_CHANGES = "no_changes"
    UNEXPECTED_CHANGES = "unexpected_changes"


class FixConfidence(Enum):
    """Confidence levels for generated fixes."""

    HIGH = "high"  # >= 0.9
    MEDIUM = "medium"  # >= 0.7
    LOW = "low"  # >= 0.5
    VERY_LOW = "very_low"  # < 0.5


class ChangeKind(StrEnum):
    """The shape of a breaking change, independent of where it was found.

    Deliberately mirrors ``sources.models.SpecChangeKind`` in spirit but is
    the classify-layer's own vocabulary: ``spec_rules.py`` maps *from*
    ``SpecChangeKind`` into this enum, and ``notes.py``'s LLM path produces
    it directly. Neither module reaches back into ``sources``.
    """

    METHOD_RENAMED = "method_renamed"
    METHOD_REMOVED = "method_removed"
    METHOD_DEPRECATED = "method_deprecated"
    MODULE_SHAPE_CHANGED = "module_shape_changed"
    PARAM_RENAMED = "param_renamed"
    PARAM_REMOVED = "param_removed"
    PARAM_NOW_REQUIRED = "param_now_required"
    PARAM_TYPE_CHANGED = "param_type_changed"
    FIELD_REMOVED = "field_removed"
    FIELD_RENAMED = "field_renamed"
    FIELD_TYPE_CHANGED = "field_type_changed"
    RESPONSE_SHAPE_CHANGED = "response_shape_changed"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    API_VERSION_BUMP = "api_version_bump"
    AUTH_CHANGED = "auth_changed"
    ERROR_SHAPE_CHANGED = "error_shape_changed"
    #: A registry reported a newer SDK version.  Unlike an API migration,
    #: this is a manifest-level fact and may have no source call site.
    DEPENDENCY_VERSION_BUMP = "dependency_version_bump"
    SECURITY_ADVISORY = "security_advisory"
    UNKNOWN = "unknown"


# Kinds where there is no meaningful "new_api" — the call site must be
# deleted or restructured rather than pointed at a replacement symbol.
REMOVAL_KINDS: frozenset[ChangeKind] = frozenset(
    {
        ChangeKind.METHOD_REMOVED,
        ChangeKind.PARAM_REMOVED,
        ChangeKind.FIELD_REMOVED,
        ChangeKind.ENUM_VALUE_REMOVED,
    }
)

# Kinds allowed to carry an empty ``new_api``. REMOVAL_KINDS is about
# *API shape* (the call site must be deleted, not redirected) and is read by
# the classifiers; a security advisory with no published fix is a different
# thing -- a real finding with no upgrade path yet -- so it is listed here
# only, and never leaks into removal-shaped classification logic.
# Deprecations may also have no named successor yet.
_NEW_API_OPTIONAL_KINDS: frozenset[ChangeKind] = REMOVAL_KINDS | {
    ChangeKind.SECURITY_ADVISORY,
    ChangeKind.METHOD_DEPRECATED,
}

# Kinds that represent a method/function/operation that was removed entirely.
METHOD_REMOVAL_KINDS: frozenset = frozenset({ChangeKind.METHOD_REMOVED})


def is_method_removal(change: "BreakingChange") -> bool:
    """A method/function/operation/export that no longer exists upstream."""
    return change.kind in METHOD_REMOVAL_KINDS


class ClassificationSource(StrEnum):
    """Where a ``BreakingChange`` came from, and therefore how much to trust it."""

    SPEC_DIFF = "spec_diff"  # deterministic OpenAPI diff — confidence always 1.0
    RELEASE_NOTES = "release_notes"  # LLM-derived from prose — confidence < 1.0
    LLM_SYNTHESIS = "llm_synthesis"  # grounded structural-diff migration spec
    MANUAL = "manual"  # hand-written (eval corpus, CLI --change-file)
    REGISTRY = "registry"  # deterministic package-registry version movement
    SECURITY = "security"  # sourced from an advisory database (OSV/GHSA)
    AGENT = "agent"  # agent-discovered via repo+feed scan (old→new mapping)


@dataclass
class BreakingChange:
    """
    Represents a breaking change in a package.
    """

    package: str
    old_version: str
    new_version: str
    old_api: str
    new_api: str
    description: str
    migration_guide: str
    kind: ChangeKind = ChangeKind.UNKNOWN
    source: ClassificationSource = ClassificationSource.MANUAL
    provider_id: str = ""
    source_url: str | None = None
    evidence: str | None = None
    confidence: float = 1.0
    call_site_hints: list[str] = field(default_factory=list)
    examples: list[tuple[str, str]] = field(default_factory=list)
    advisory_severity: str | None = None

    def __post_init__(self):
        """Validate the breaking change data."""
        if not self.package:
            raise ValueError("Package name is required")
        if not self.old_api:
            raise ValueError("old_api is required")
        if not self.new_api and self.kind not in _NEW_API_OPTIONAL_KINDS:
            raise ValueError(
                f"new_api is required unless kind is a removal kind or an unfixed "
                f"security advisory, got kind={self.kind.value}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0.0, 1.0], got {self.confidence}")

    @property
    def replacement(self) -> str:
        """Human-readable replacement, or a removal note when there is none."""
        if not self.new_api:
            return "(removed, no replacement)"
        return self.new_api

    @property
    def dedupe_key(self) -> str:
        """Stable identity for this change, independent of description text.

        Used as the unique constraint on ``storage.schema.BreakingChangeRow``
        so re-classifying the same event doesn't insert duplicate rows.
        """
        payload = f"{self.provider_id}|{self.package}|{self.kind.value}|{self.old_api}"
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Usage:
    """
    Represents a single usage of an API in code.
    """

    line_number: int
    column: int
    line_content: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    match_text: str = ""

    @property
    def context_window(self) -> str:
        """Get the full context window as a string."""
        lines = [*self.context_before, self.line_content, *self.context_after]
        return "\n".join(lines)


@dataclass
class FileUsage:
    """
    Represents all usages of an API in a single file.
    """

    filepath: str
    usages: list[Usage]
    file_content: str

    @property
    def usage_count(self) -> int:
        """Get the number of usages in this file."""
        return len(self.usages)


@dataclass
class ValidationResult:
    """
    Result of validating a generated fix.
    """

    status: ValidationStatus
    is_valid: bool
    syntax_valid: bool
    changes_detected: bool
    error_message: str | None = None
    error_line: int | None = None

    @classmethod
    def valid(cls) -> "ValidationResult":
        """Create a valid result."""
        return cls(
            status=ValidationStatus.VALID, is_valid=True, syntax_valid=True, changes_detected=True
        )

    @classmethod
    def syntax_error(cls, message: str, line: int | None = None) -> "ValidationResult":
        """Create a syntax error result."""
        return cls(
            status=ValidationStatus.SYNTAX_ERROR,
            is_valid=False,
            syntax_valid=False,
            changes_detected=False,
            error_message=message,
            error_line=line,
        )


@dataclass
class LLMCall:
    """
    Record of an LLM API call for logging/debugging.
    """

    timestamp: datetime
    prompt: str
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate: float
    duration_ms: int

    @property
    def total_tokens(self) -> int:
        """Get total tokens used."""
        return self.input_tokens + self.output_tokens


@dataclass
class FixResult:
    """
    Result of fixing a single file.
    """

    filepath: str
    original_code: str
    fixed_code: str
    diff: str
    confidence: float
    usages_fixed: int
    validation: ValidationResult
    llm_call: LLMCall | None = None

    @property
    def confidence_level(self) -> FixConfidence:
        """Get the confidence level category."""
        if self.confidence >= 0.9:
            return FixConfidence.HIGH
        elif self.confidence >= 0.7:
            return FixConfidence.MEDIUM
        elif self.confidence >= 0.5:
            return FixConfidence.LOW
        else:
            return FixConfidence.VERY_LOW

    @property
    def is_successful(self) -> bool:
        """Check if the fix was successful."""
        return self.validation.is_valid and self.confidence >= 0.5


@dataclass
class AgentResult:
    """
    Complete result of running the agent on a codebase.
    """

    package: str
    breaking_change: BreakingChange
    files_scanned: int
    files_affected: int
    fixes: list[FixResult]
    total_usages_fixed: int
    success_rate: float
    summary: str
    total_cost: float = 0.0
    total_tokens: int = 0
    duration_ms: int = 0

    @property
    def successful_fixes(self) -> list[FixResult]:
        """Get list of successful fixes."""
        return [f for f in self.fixes if f.is_successful]

    @property
    def failed_fixes(self) -> list[FixResult]:
        """Get list of failed fixes."""
        return [f for f in self.fixes if not f.is_successful]
