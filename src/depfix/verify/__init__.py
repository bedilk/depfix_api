"""Week 4 test-based verification of candidate fixes.

:class:`Verifier` runs a checkout's own test suite before and after a
batch of fixes is applied, comparing results by test identity to decide
whether each fix should be kept, reverted, or left SUSPECT (unconfirmed).
"""

from depfix.verify.attribution import AttributionResult, attribute_failures
from depfix.verify.characterize import CharacterizationReport, Characterizer, Probe
from depfix.verify.confidence import ConfidenceTier
from depfix.verify.contract import ContractReport
from depfix.verify.coverage import CoverageReport, changed_line_numbers, check_changed_lines
from depfix.verify.manager import InstallResult, detect_package_manager, install_dependencies
from depfix.verify.models import TestCase, TestFramework, TestRunResult, TestStatus
from depfix.verify.runner import detect_framework, has_test_script, run_tests
from depfix.verify.selection import select_test_files
from depfix.verify.static_check import StaticCheckResult, run_static_check
from depfix.verify.typecheck import (
    Diagnostic,
    TypecheckResult,
    has_typescript,
    new_diagnostics,
    run_typecheck,
)
from depfix.verify.verifier import VerificationReport, Verifier

__all__ = [
    "AttributionResult",
    "CharacterizationReport",
    "Characterizer",
    "ConfidenceTier",
    "ContractReport",
    "CoverageReport",
    "Diagnostic",
    "InstallResult",
    "Probe",
    "StaticCheckResult",
    "TestCase",
    "TestFramework",
    "TestRunResult",
    "TestStatus",
    "TypecheckResult",
    "VerificationReport",
    "Verifier",
    "attribute_failures",
    "changed_line_numbers",
    "check_changed_lines",
    "detect_framework",
    "detect_package_manager",
    "has_test_script",
    "has_typescript",
    "install_dependencies",
    "new_diagnostics",
    "run_static_check",
    "run_tests",
    "run_typecheck",
    "select_test_files",
]
