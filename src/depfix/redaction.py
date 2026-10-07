"""Reversible, HMAC-keyed secret redaction for anything that reaches an LLM.

The hard constraint is that a fixer returns the *whole file*. A one-way
placeholder (``<REDACTED>``) would be written straight back to disk and
**delete** the credential -- strictly worse than the leak it was meant to
prevent. So every secret is replaced by a stable, per-run token
(``DEPFIX_REDACTED_<12hex>``) and swapped back on the way out.

Three properties make that safe:

1. **Keyed.** The placeholder is an HMAC under a key generated fresh per
   process, so the token cannot be used offline to confirm a guess at the
   secret, and two runs never produce the same token for the same value.
2. **Stable within a run.** Identical secrets map to identical placeholders,
   so one file reads consistently and the model can reason about "the API
   key" as a single thing.
3. **Fail-closed on restore.** If the model drops a placeholder (deleted the
   secret) or invents one (hallucinated a credential), :meth:`Redaction.restore`
   raises :class:`RedactionError` and the pipeline records the file as
   ``SKIPPED``. depfix never writes a file it cannot prove it reassembled.

``redact_text`` is the one-way variant, correct for *context-only* text
(judge prompts, characterization source, tool output) that is never written
back.
"""

from __future__ import annotations

import dataclasses
import hmac
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field

from depfix.core.models import FileUsage

__all__ = [
    "PLACEHOLDER_RE",
    "Redaction",
    "RedactionError",
    "redact",
    "redact_file_usage",
    "redact_text",
]

_KEY = os.urandom(32)

PLACEHOLDER_RE = re.compile(r"DEPFIX_REDACTED_[0-9a-f]{12}")

_MIN_ENTROPY_LENGTH = 32
_MIN_ENTROPY_BITS = 4.3

# (pattern, capture group) -- group is the span that gets replaced, so a
# named assignment redacts only the value, leaving `apiKey:` intact.
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        0,
    ),
    (re.compile(r"\bsk-(?:ant-[a-z0-9-]*|proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), 0),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), 0),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), 0),
    (re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"), 0),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), 0),
    # URL password: redact only the password part
    (re.compile(r"[A-Za-z][\w+.\-]*://[^\s:/@]+:([^\s/@]{3,})@"), 1),
    # Named assignments: apiKey: "...", STRIPE_SECRET=..., etc.
    (
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token
               |auth[_-]?token|private[_-]?key|passwd|password|secret|token)\b
            \s*[:=]\s*
            ["']([^"'\n]{8,})["']
            """
        ),
        1,
    ),
)

_QUOTED_LITERAL_RE = re.compile(rf"""["']([A-Za-z0-9+/=_\-]{{{_MIN_ENTROPY_LENGTH},}})["']""")


class RedactionError(RuntimeError):
    """The model's output could not be safely un-redacted.

    Raised when a placeholder was dropped (the model deleted a secret) or an
    unknown placeholder appeared (the model invented one). Both mean the
    output cannot be written to disk without either losing a credential or
    inventing one.
    """


def _shannon_bits(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _looks_high_entropy(value: str) -> bool:
    if len(value) < _MIN_ENTROPY_LENGTH:
        return False
    if PLACEHOLDER_RE.fullmatch(value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    if not (has_digit and has_alpha):
        return False
    return _shannon_bits(value) >= _MIN_ENTROPY_BITS


def _placeholder(value: str) -> str:
    digest = hmac.new(_KEY, value.encode("utf-8"), "sha256").hexdigest()
    return f"DEPFIX_REDACTED_{digest[:12]}"


def _find_spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, secret) spans, earliest-longest first."""
    spans: list[tuple[int, int, str]] = []
    for pattern, group in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            if start < 0 or end <= start:
                continue
            spans.append((start, end, text[start:end]))
    for match in _QUOTED_LITERAL_RE.finditer(text):
        start, end = match.span(1)
        candidate = text[start:end]
        if _looks_high_entropy(candidate):
            spans.append((start, end, candidate))

    # Earliest start wins; on a tie the longer span wins
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    chosen: list[tuple[int, int, str]] = []
    cursor = -1
    for start, end, value in spans:
        if start < cursor:
            continue
        chosen.append((start, end, value))
        cursor = end
    return chosen


def _apply(text: str, spans: list[tuple[int, int, str]]) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    out = text
    for start, end, value in reversed(spans):  # right-to-left keeps offsets valid
        token = _placeholder(value)
        mapping[token] = value
        out = out[:start] + token + out[end:]
    return out, mapping


@dataclass(frozen=True)
class Redaction:
    """A prompt-safe rendering of one file, plus the map back."""

    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def redacted_count(self) -> int:
        return len(self.mapping)

    @property
    def redacted(self) -> bool:
        """True when at least one secret was found. Kept for backward compat."""
        return bool(self.mapping)

    def cover(self, other: str) -> str:
        """Redact *other* using the same keying as this file's secrets."""
        if not other:
            return other
        covered, _ = _apply(other, _find_spans(other))
        return covered

    def restore(self, model_output: str) -> str:
        """Swap placeholders back, or refuse if the model misbehaved."""
        if not self.mapping:
            return model_output

        found = set(PLACEHOLDER_RE.findall(model_output))
        foreign = found - set(self.mapping)
        if foreign:
            raise RedactionError(
                f"model output contains {len(foreign)} placeholder(s) that do not belong "
                "to this file; refusing to write it"
            )
        missing = set(self.mapping) - found
        if missing:
            raise RedactionError(
                f"model output dropped {len(missing)} redacted value(s); writing it would "
                "delete a credential, so this file is skipped"
            )

        out = model_output
        for token, value in self.mapping.items():
            out = out.replace(token, value)
        return out


def redact(text: str) -> Redaction:
    """Reversible redaction, for text that will be written back."""
    redacted, mapping = _apply(text, _find_spans(text))
    return Redaction(text=redacted, mapping=mapping)


def redact_text(text: str) -> str:
    """One-way redaction, for context-only text that is never written back."""
    return redact(text).text


def redact_file_usage(file_usage: FileUsage) -> tuple[FileUsage, Redaction]:
    """A prompt-safe copy of ``file_usage`` plus the restore map."""
    redaction = redact(file_usage.file_content)
    if not redaction.mapping:
        return file_usage, redaction

    safe_usages = [
        dataclasses.replace(
            usage,
            line_content=redaction.cover(usage.line_content),
            context_before=[redaction.cover(line) for line in usage.context_before],
            context_after=[redaction.cover(line) for line in usage.context_after],
            match_text=redaction.cover(usage.match_text),
        )
        for usage in file_usage.usages
    ]
    safe = FileUsage(
        filepath=file_usage.filepath,
        usages=safe_usages,
        file_content=redaction.text,
    )
    return safe, redaction
