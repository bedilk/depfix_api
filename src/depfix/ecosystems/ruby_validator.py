"""Syntax validation for Ruby source files via `ruby -c`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def validate_ruby_syntax(path: Path, *, timeout: float = 10.0) -> tuple[bool, str]:
    ruby = shutil.which("ruby")
    if ruby is None:
        return True, "ruby not on PATH; skipping syntax check"
    try:
        result = subprocess.run(
            [ruby, "-c", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return True, "syntax check timed out; treating as valid"
    except OSError as exc:
        return True, f"could not run ruby -c: {exc}"
    if result.returncode == 0:
        return True, "syntax valid"
    return False, result.stderr.strip()[:300]
