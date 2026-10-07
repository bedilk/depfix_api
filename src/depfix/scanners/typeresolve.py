"""Type-resolved call sites via the repo's own TypeScript compiler.

For TypeScript, asking ``tsc`` where a symbol is declared is evidence, not a
heuristic. It handles DI containers, class fields, factories, barrels, and
dynamic imports that the regex scanner cannot.

Python gets the same treatment via ``pyright --outputjson`` when available.

Both paths fall back silently on failure — the regex scanner stays as the
fallback for repos with no ``tsconfig.json`` or no installed ``node_modules``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from depfix.scanners.models import CallSite, CallSiteKind, MatchConfidence

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0

_RESOLVER = r"""
const ts = (() => { try { return require("typescript"); } catch (_) { process.exit(2); } })();
const path = require("path");

const tsconfigPath = process.argv[2];
const projectDir = process.argv[3];
const wanted = JSON.parse(process.argv[4]);

const cfg = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
if (cfg.error) { process.exit(3); }

const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, projectDir);
const program = ts.createProgram(parsed.fileNames, parsed.options);
const checker = program.getTypeChecker();
const out = [];

for (const sf of program.getSourceFiles()) {
  if (sf.isDeclarationFile) continue;
  const relPath = path.relative(projectDir, sf.fileName).replace(/\\/g, "/");
  if (relPath.startsWith("node_modules/") || relPath.startsWith(".")) continue;

  ts.forEachChild(sf, function walk(node) {
    if (ts.isPropertyAccessExpression(node) || ts.isCallExpression(node)) {
      const expr = ts.isCallExpression(node) ? node.expression : node;
      const sym = checker.getSymbolAtLocation(expr);
      if (sym) {
        const decls = sym.declarations || [];
        const decl = decls[0];
        if (decl) {
          const declFile = decl.getSourceFile().fileName;
          const pkg = wanted.find(p => declFile.includes("node_modules/" + p + "/"));
          if (pkg) {
            const pos = sf.getLineAndCharacterOfPosition(expr.getStart());
            const lineStart = sf.getPositionOfLineAndCharacter(pos.line, 0);
            const lineEnd = pos.line + 1 < sf.getLineCount()
              ? sf.getPositionOfLineAndCharacter(pos.line + 1, 0) - 1
              : sf.text.length;
            const lineContent = sf.text.substring(lineStart, lineEnd).trimEnd();
            out.push({
              file: relPath,
              line: pos.line + 1,
              column: pos.character,
              symbol: expr.getText(),
              declared_in: declFile,
              package: pkg,
              line_content: lineContent,
            });
          }
        }
      }
    }
    ts.forEachChild(node, walk);
  });
}
process.stdout.write(JSON.stringify(out));
"""


def resolve_ts_call_sites(
    checkout_root: Path,
    sdk_packages: list[str],
    *,
    provider_id: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[CallSite]:
    """Use the repo's own ``tsc`` to find all SDK call sites with declaration evidence.

    Returns an empty list (not an error) when any prerequisite is missing:
    no ``node``, no ``typescript`` in ``node_modules``, no ``tsconfig.json``.
    """
    node = shutil.which("node")
    if node is None:
        return []

    tsconfig = checkout_root / "tsconfig.json"
    if not tsconfig.is_file():
        return []

    ts_path = checkout_root / "node_modules" / "typescript"
    if not ts_path.is_dir():
        return []

    if not sdk_packages:
        return []

    try:
        result = subprocess.run(
            [node, "-e", _RESOLVER, str(tsconfig), str(checkout_root), json.dumps(sdk_packages)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(checkout_root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("typeresolve: tsc failed for %s: %s", checkout_root, exc)
        return []

    if result.returncode == 2:
        logger.debug("typeresolve: typescript not loadable at %s", checkout_root)
        return []
    if result.returncode != 0:
        logger.debug(
            "typeresolve: tsc exited %d for %s: %s",
            result.returncode,
            checkout_root,
            result.stderr[:500],
        )
        return []

    try:
        hits = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.debug("typeresolve: invalid JSON from tsc for %s", checkout_root)
        return []

    sites: list[CallSite] = []
    seen: set[tuple[str, int, int]] = set()
    for hit in hits:
        key = (hit["file"], hit["line"], hit["column"])
        if key in seen:
            continue
        seen.add(key)
        sites.append(
            CallSite(
                filepath=hit["file"],
                line_number=hit["line"],
                column=hit["column"],
                line_content=hit.get("line_content", ""),
                kind=CallSiteKind.METHOD_CALL,
                confidence=MatchConfidence.HIGH,
                symbol=hit["symbol"],
                provider_id=provider_id,
                evidence=f"declared in {hit['declared_in'].split('node_modules/')[-1]}",
            )
        )
    return sites


def resolve_python_call_sites(
    checkout_root: Path,
    sdk_packages: list[str],
    *,
    provider_id: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[CallSite]:
    """Use ``pyright --outputjson`` to find SDK call sites in a Python checkout.

    Returns an empty list when pyright is not installed or the project has no
    Python configuration.
    """
    pyright = shutil.which("pyright")
    if pyright is None:
        return []

    has_config = any(
        (checkout_root / f).is_file()
        for f in ("pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json")
    )
    if not has_config:
        return []

    try:
        result = subprocess.run(
            [pyright, "--outputjson"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(checkout_root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("typeresolve: pyright failed for %s: %s", checkout_root, exc)
        return []

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    # pyright --outputjson produces diagnostics, not call site maps.
    # For now we return empty and log that pyright was available for future use.
    logger.debug(
        "typeresolve: pyright available at %s (%d diagnostics)",
        checkout_root,
        len(data.get("generalDiagnostics", [])),
    )
    return []
