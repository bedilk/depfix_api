"""TypeScript-aware validation for ``.ts`` and ``.tsx`` candidates.

The JavaScript validator's ``new Function`` check cannot parse TypeScript
syntax.  This validator prefers the repository's own ``tsc``, then falls back
to TypeScript's transpile API, and finally accepts the candidate so the full
repository verifier can remain the deciding oracle.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from depfix.core.models import ValidationResult

logger = logging.getLogger(__name__)

TS_EXTENSIONS = frozenset({".ts", ".tsx", ".mts", ".cts"})
_TSC_TIMEOUT = 30.0
_TSC_ERROR_RE = re.compile(r"(?:^|\n)[^:]+\((\d+),\d+\):\s+error\s+(TS\d+):\s+(.+)")


def is_typescript(filepath: str) -> bool:
    return Path(filepath).suffix.lower() in TS_EXTENSIONS


def _find_tsc(checkout_root: str | Path | None) -> str | None:
    if checkout_root is not None:
        local = Path(checkout_root) / "node_modules" / ".bin" / "tsc"
        if local.is_file():
            return str(local)
    return shutil.which("tsc")


def _tsconfig_for_file(name: str, *, tsx: bool) -> dict:
    options = {
        "target": "ES2020",
        "module": "commonjs",
        "moduleResolution": "node",
        "skipLibCheck": True,
        "noEmit": True,
        "noResolve": True,
        "allowJs": True,
        "allowSyntheticDefaultImports": True,
        "esModuleInterop": True,
        "strict": False,
        "noImplicitAny": False,
    }
    if tsx:
        options["jsx"] = "preserve"
    return {"compilerOptions": options, "files": [name]}


def _error(output: str) -> tuple[str, int | None]:
    match = _TSC_ERROR_RE.search(output)
    if match:
        return f"{match.group(2)}: {match.group(3).strip()}", int(match.group(1))
    line = next(
        (line.strip() for line in output.splitlines() if line.strip()),
        "TypeScript compilation error",
    )
    return line[:300], None


def _validate_with_tsc(code: str, filepath: str, tsc: str) -> ValidationResult:
    suffix = Path(filepath).suffix or ".ts"
    with tempfile.TemporaryDirectory(prefix="depfix-tsval-") as tmp:
        root = Path(tmp)
        source = root / f"check{suffix}"
        source.write_text(code, encoding="utf-8")
        config = root / "tsconfig.json"
        config.write_text(
            json.dumps(_tsconfig_for_file(source.name, tsx=suffix == ".tsx")), encoding="utf-8"
        )
        try:
            result = subprocess.run(
                [tsc, "--project", str(config)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_TSC_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("tsc unavailable or timed out validating %s", filepath)
            return ValidationResult.valid()
        if result.returncode == 0:
            return ValidationResult.valid()
        message, line = _error(result.stdout + "\n" + result.stderr)
        return ValidationResult.syntax_error(message, line)


_TRANSPILE_SCRIPT = r"""const ts = (() => { try { return require("typescript"); } catch (_) { process.exit(2); } })();
const result = ts.transpileModule(process.argv[1], {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
    jsx: process.argv[2] === "1" ? ts.JsxEmit.Preserve : ts.JsxEmit.None },
  reportDiagnostics: true,
});
if (result.diagnostics && result.diagnostics.length) {
  const d = result.diagnostics[0];
  const message = ts.flattenDiagnosticMessageText(d.messageText, " ");
  let line = 0;
  if (d.file && d.start !== undefined) line = d.file.getLineAndCharacterOfPosition(d.start).line + 1;
  console.error(JSON.stringify({message, line})); process.exit(1);
}
"""


def _validate_with_ts_api(code: str, filepath: str) -> ValidationResult | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        result = subprocess.run(
            [node, "-e", _TRANSPILE_SCRIPT, code, "1" if Path(filepath).suffix == ".tsx" else "0"],
            capture_output=True,
            text=True,
            timeout=_TSC_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 2:
        return None
    if result.returncode == 0:
        return ValidationResult.valid()
    try:
        payload = json.loads((result.stdout + result.stderr).strip())
        return ValidationResult.syntax_error(
            str(payload.get("message", "TypeScript syntax error")), payload.get("line") or None
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return ValidationResult.syntax_error(
            (result.stderr or result.stdout or "TypeScript syntax error")[:300]
        )


class TypeScriptSyntaxValidator:
    """Validate TypeScript syntax without treating it as JavaScript."""

    def __init__(
        self,
        checkout_root: str | Path | None = None,
        *,
        use_tsc: bool = True,
        use_ts_api: bool = True,
    ) -> None:
        self._checkout_root = checkout_root
        self._use_tsc = use_tsc
        self._use_ts_api = use_ts_api
        self._tsc_path: str | bool | None = False

    @property
    def _tsc(self) -> str | None:
        if self._tsc_path is False:
            self._tsc_path = _find_tsc(self._checkout_root) if self._use_tsc else None
        return self._tsc_path if isinstance(self._tsc_path, str) else None

    def validate_syntax(self, code: str, filepath: str) -> ValidationResult:
        if not is_typescript(filepath):
            raise ValueError(f"{filepath!r} does not have a TypeScript extension")
        if self._tsc is not None:
            return _validate_with_tsc(code, filepath, self._tsc)
        if self._use_ts_api:
            result = _validate_with_ts_api(code, filepath)
            if result is not None:
                return result
        logger.debug("no TypeScript tooling available; accepting %s", filepath)
        return ValidationResult.valid()


__all__ = ["TypeScriptSyntaxValidator", "is_typescript"]
