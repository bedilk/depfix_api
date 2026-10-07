"""
Main Dependency Fix Agent class.
"""

import logging
import time
from pathlib import Path

from depfix.core.models import (
    AgentResult,
    BreakingChange,
    FileUsage,
    FixResult,
)
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator
from depfix.scanners.base import CodebaseScanner, build_pattern_for_api
from depfix.validators.javascript import FixValidator, create_unified_diff

logger = logging.getLogger(__name__)


class DependencyFixAgent:
    """
    AI agent that detects and fixes breaking dependency changes.
    """

    def __init__(
        self,
        google_api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        context_lines: int = 5,
        max_retries: int = 3,
        provider: str = "gemini",
        ollama_model: str = "qwen2.5-coder:7b",
        ollama_base_url: str = "http://localhost:11434",
        ollama_timeout: float = 120.0,
    ):
        self.scanner = CodebaseScanner(context_lines=context_lines)
        if provider == "ollama":
            self.fixer: FixGenerator | OllamaFixGenerator = OllamaFixGenerator(
                model=ollama_model,
                base_url=ollama_base_url,
                max_retries=max_retries,
                timeout=ollama_timeout,
            )
            logger.info(f"Initialized DependencyFixAgent with Ollama model {ollama_model}")
        else:
            self.fixer = FixGenerator(
                api_key=google_api_key,
                model=model,
                max_retries=max_retries,
            )
            logger.info(f"Initialized DependencyFixAgent with Gemini model {model}")
        self.validator = FixValidator()

    def fix_breaking_change(
        self,
        breaking_change: BreakingChange,
        codebase_path: str,
        output_dir: str | None = None,
    ) -> AgentResult:
        """
        Fix a breaking change across a codebase.
        """
        start_time = time.time()

        logger.info(f"Starting fix for {breaking_change.package} breaking change")
        logger.info(f"  Old API: {breaking_change.old_api}")
        logger.info(f"  New API: {breaking_change.new_api}")

        # Step 1: Build search patterns
        patterns = build_pattern_for_api(breaking_change.old_api, breaking_change.package)
        logger.info(f"Search patterns: {patterns}")

        # Step 2: Scan codebase
        logger.info(f"Scanning codebase at {codebase_path}")
        file_usages = self.scanner.scan_codebase(codebase_path, patterns)

        files_scanned = self._count_files(codebase_path)
        files_affected = len(file_usages)
        total_usages = sum(fu.usage_count for fu in file_usages)

        logger.info(f"Found {total_usages} usages in {files_affected} files")

        if not file_usages:
            return AgentResult(
                package=breaking_change.package,
                breaking_change=breaking_change,
                files_scanned=files_scanned,
                files_affected=0,
                fixes=[],
                total_usages_fixed=0,
                success_rate=1.0,  # No failures if nothing to fix
                summary="No usages of the affected API found in the codebase.",
                total_cost=0.0,
                total_tokens=0,
                duration_ms=int((time.time() - start_time) * 1000),
            )

        # Step 3: Generate fixes for each affected file
        fixes: list[FixResult] = []

        for idx, file_usage in enumerate(file_usages, 1):
            logger.info(f"[{idx}/{files_affected}] Processing {file_usage.filepath}")

            try:
                fix_result = self._process_file(file_usage, breaking_change)
                fixes.append(fix_result)

                status = "✅" if fix_result.is_successful else "❌"
                logger.info(
                    f"  {status} {fix_result.usages_fixed} usages, "
                    f"confidence: {fix_result.confidence:.2f}"
                )

            except Exception as e:
                logger.error(f"  ❌ Failed to process: {e}")
                # Create a failed fix result
                fixes.append(
                    FixResult(
                        filepath=file_usage.filepath,
                        original_code=file_usage.file_content,
                        fixed_code=file_usage.file_content,
                        diff="",
                        confidence=0.0,
                        usages_fixed=0,
                        validation=self.validator.validate(
                            file_usage.file_content, file_usage.file_content, file_usage.filepath
                        ),
                    )
                )

        # Step 4: Write diffs to output directory if specified
        if output_dir:
            self._write_diffs(fixes, output_dir)

        # Step 5: Calculate summary statistics
        successful_fixes = [f for f in fixes if f.is_successful]
        total_usages_fixed = sum(f.usages_fixed for f in successful_fixes)
        success_rate = len(successful_fixes) / len(fixes) if fixes else 0.0

        duration_ms = int((time.time() - start_time) * 1000)

        summary = self._generate_summary(
            breaking_change=breaking_change,
            files_scanned=files_scanned,
            files_affected=files_affected,
            total_usages=total_usages,
            total_usages_fixed=total_usages_fixed,
            success_rate=success_rate,
        )

        return AgentResult(
            package=breaking_change.package,
            breaking_change=breaking_change,
            files_scanned=files_scanned,
            files_affected=files_affected,
            fixes=fixes,
            total_usages_fixed=total_usages_fixed,
            success_rate=success_rate,
            summary=summary,
            total_cost=self.fixer.total_cost,
            total_tokens=self.fixer.total_tokens,
            duration_ms=duration_ms,
        )

    def _process_file(self, file_usage: FileUsage, breaking_change: BreakingChange) -> FixResult:
        """Process a single file and generate a fix."""
        # Generate fix using LLM
        fixed_code, confidence, llm_call = self.fixer.generate_fix(file_usage, breaking_change)

        # Validate the fix
        validation = self.validator.validate(
            file_usage.file_content, fixed_code, file_usage.filepath
        )

        # Create diff
        diff = ""
        if validation.is_valid:
            diff = create_unified_diff(file_usage.file_content, fixed_code, file_usage.filepath)

        return FixResult(
            filepath=file_usage.filepath,
            original_code=file_usage.file_content,
            fixed_code=fixed_code,
            diff=diff,
            confidence=confidence,
            usages_fixed=file_usage.usage_count if validation.is_valid else 0,
            validation=validation,
            llm_call=llm_call,
        )

    def _count_files(self, path: str) -> int:
        """Count the total number of scannable files."""
        count = 0
        for ext in self.scanner.SUPPORTED_EXTENSIONS:
            count += len(list(Path(path).rglob(f"*{ext}")))
        return count

    def _write_diffs(self, fixes: list[FixResult], output_dir: str) -> None:
        """Write diffs to the output directory."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for fix in fixes:
            if fix.diff:
                # Create filename from filepath
                safe_name = fix.filepath.replace("/", "_").replace("\\", "_")
                diff_file = output_path / f"{safe_name}.patch"
                diff_file.write_text(fix.diff)
                logger.info(f"Wrote diff to {diff_file}")

    def _generate_summary(
        self,
        breaking_change: BreakingChange,
        files_scanned: int,
        files_affected: int,
        total_usages: int,
        total_usages_fixed: int,
        success_rate: float,
    ) -> str:
        """Generate a human-readable summary."""
        return f"""
Dependency Fix Summary
======================
Package: {breaking_change.package}
Version: {breaking_change.old_version} → {breaking_change.new_version}
Change: {breaking_change.old_api} → {breaking_change.new_api}

Results:
- Files scanned: {files_scanned}
- Files affected: {files_affected}
- Total usages found: {total_usages}
- Usages fixed: {total_usages_fixed}
- Success rate: {success_rate:.0%}

Cost: ${self.fixer.total_cost:.4f} ({self.fixer.total_tokens} tokens)
""".strip()
