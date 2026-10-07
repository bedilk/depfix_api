"""depfix — AI-powered fixer for breaking dependency changes."""

from depfix.core.agent import DependencyFixAgent
from depfix.core.models import (
    AgentResult,
    BreakingChange,
    ChangeKind,
    ClassificationSource,
    FileUsage,
    FixResult,
    Usage,
    ValidationResult,
)

__all__ = [
    "AgentResult",
    "BreakingChange",
    "ChangeKind",
    "ClassificationSource",
    "DependencyFixAgent",
    "FileUsage",
    "FixResult",
    "Usage",
    "ValidationResult",
]

__version__ = "0.2.0"
