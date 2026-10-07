"""Maps newly-failing tests back to the specific fixed file responsible,
so :class:`depfix.verify.verifier.Verifier` can revert just that file
rather than every edit in the batch.

Trust order, most to least reliable:

1. **Import graph** -- if the failing test file actually imports the
   touched file (via the same relative-import resolution the scanner
   itself uses, see :func:`depfix.scanners.callsites.resolve_relative_import`),
   that's about as direct evidence as static analysis can offer.
2. **Filename convention** -- ``chat.test.js``/``chat.spec.ts`` naming
   ``chat.js``, or a ``__tests__/chat.js`` sibling -- catches the common
   case where a test file doesn't literally import its subject (e.g. it's
   exercised via a barrel file) but the convention still names it.
3. **Sole-candidate elimination** -- if only one file was touched at all,
   every new failure is attributed to it by definition; there's nothing
   else it could be.
4. **Unattributed** -- anything left over is reported, not guessed at;
   see :class:`AttributionResult`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from depfix.scanners.callsites import resolve_relative_import
from depfix.verify.models import TestCase

#: Matches the common ``foo.test.js`` / ``foo.spec.ts`` / ``foo.test.jsx``
#: test-filename convention.
_TEST_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$")

#: Best-effort extraction of relative import/require specs from a test
#: file's own source, to feed :func:`resolve_relative_import`. Deliberately
#: simpler than the scanner's own import regexes (which exist to build
#: symbol *bindings*) -- attribution only needs "what files does this test
#: touch", not what's imported from them.
_RELATIVE_IMPORT_SPEC_RE = re.compile(r"""(?:from\s+|require\(\s*)['"](\.[^'"]+)['"]""")


@dataclass
class AttributionResult:
    """Which touched file (if any) each newly-failing test is blamed on."""

    by_file: dict[str, list[TestCase]] = field(default_factory=dict)
    unattributed: list[TestCase] = field(default_factory=list)


def _candidate_source_names(test_file: str) -> list[str]:
    """Filename-convention guesses at the source file a test file exercises,
    e.g. ``chat.test.js`` -> ``chat.js``/``chat.ts``/..., and
    ``__tests__/chat.js`` -> ``chat.js`` (Jest's directory convention)."""
    path = Path(test_file)
    candidates: list[str] = []

    match = _TEST_SUFFIX_RE.match(path.name)
    if match:
        base = match.group("base")
        for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            candidates.append(str(path.with_name(base + ext)))

    if path.parent.name == "__tests__":
        sibling_dir = path.parent.parent
        candidates.append(str(sibling_dir / path.name))

    return candidates


def _imports_touched_file(test_file_abs: Path, touched_abs: set[Path]) -> bool:
    """True if ``test_file_abs`` has a relative import/require resolving to
    one of ``touched_abs``. Silently returns ``False`` (rather than raising)
    if the test file can't be read -- attribution degrading to a weaker
    signal is preferable to crashing verification over an unreadable file."""
    try:
        source = test_file_abs.read_text(encoding="utf-8")
    except OSError:
        return False

    for match in _RELATIVE_IMPORT_SPEC_RE.finditer(source):
        spec = match.group(1)
        resolved = resolve_relative_import(test_file_abs, spec)
        if resolved is not None and resolved.resolve() in touched_abs:
            return True
    return False


def attribute_failures(
    new_failures: list[TestCase],
    *,
    touched_relpaths: tuple[str, ...],
    checkout_root: str | Path,
) -> AttributionResult:
    """Attribute each of ``new_failures`` to one of ``touched_relpaths``,
    using the trust order documented on this module. Every failure ends up
    in exactly one of ``result.by_file`` or ``result.unattributed``.
    """
    root = Path(checkout_root)
    touched_abs = {(root / rel).resolve() for rel in touched_relpaths}
    touched_by_name = {Path(rel).name: rel for rel in touched_relpaths}
    result = AttributionResult()

    for case in new_failures:
        attributed_to: str | None = None
        test_file_abs = (root / case.file) if case.file else None

        if test_file_abs is not None and test_file_abs.is_file():
            if test_file_abs.resolve() in touched_abs:
                # The "test" itself is one of the touched files -- can
                # happen if a fix landed directly in a spec file.
                attributed_to = case.file
            else:
                # Import graph gives a definitive answer, but only when
                # there's exactly one touched file it resolves to --
                # multiple touched files imported by the same test file
                # can't be disambiguated this way, so fall through instead
                # of guessing.
                matches = [
                    rel
                    for rel in touched_relpaths
                    if _imports_touched_file(test_file_abs, {(root / rel).resolve()})
                ]
                if len(matches) == 1:
                    attributed_to = matches[0]

        if attributed_to is None and case.file:
            for candidate in _candidate_source_names(case.file):
                name = Path(candidate).name
                if name in touched_by_name:
                    attributed_to = touched_by_name[name]
                    break

        if attributed_to is None and len(touched_relpaths) == 1:
            attributed_to = touched_relpaths[0]

        if attributed_to is not None:
            result.by_file.setdefault(attributed_to, []).append(case)
        else:
            result.unattributed.append(case)

    return result
