"""Parsing the ``old_api`` / ``new_api`` fields of a breaking change.

These fields come from changelogs, release notes and model extraction, so
they arrive dirty: ``"openai.createChatCompletion()"``, ``"call
openai.createChatCompletion"``, ``"createChatCompletion -> chat.completions
.create"``. A codemod may only act on input it can parse into an
unambiguous dotted symbol; anything else must produce ``None`` so the
change falls through to the language model, which can cope with prose.

Refusing to guess here is what keeps the codemod layer trustworthy. A rule
that "does its best" with a malformed symbol is a rule that rewrites the
wrong identifier.

**Provider heads may be hyphenated.** ``google-cloud``, ``vercel-ai`` and
``aws-s3`` are real provider ids, and a symbol like
``google-cloud.bucket.setCorsConfiguration`` must normalize to itself, not
to ``None``. The member path after the provider head is still held to the
JS-identifier rule (a hyphen there really would be malformed); only the
head is allowed to be an opaque provider id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["RenamePaths", "normalize_api", "rename_paths"]

_ID = r"[A-Za-z_$][\w$]*"
#: A bare dotted chain of JS identifiers -- e.g. ``openai.chat.completions``.
_MEMBER_PATH = re.compile(rf"^{_ID}(?:\.{_ID})*$")
_TRAILING_CALL = re.compile(r"\s*\([^()]*\)\s*$")


def _strip_call_suffix(raw: str) -> str:
    candidate = raw.strip().strip("`\"'")
    candidate = _TRAILING_CALL.sub("", candidate)
    candidate = candidate.rstrip("().;,")
    return candidate.lstrip(".")


def normalize_api(raw: str, *, provider_id: str = "") -> str | None:
    """Return ``raw`` as a bare dotted symbol, or ``None`` if it isn't one.

    Strips a trailing argument list and surrounding punctuation, then
    requires the remainder to be a plain dotted identifier chain. Prose,
    signatures with nested parens, and anything containing whitespace are
    rejected rather than salvaged.

    When ``provider_id`` is given and ``raw`` starts with it, the provider
    head is allowed to be an opaque id (``google-cloud``, ``vercel-ai``);
    only the member path after it is held to the JS-identifier rule.
    """
    if not raw:
        return None
    candidate = _strip_call_suffix(raw)
    if not candidate:
        return None

    if provider_id and candidate == provider_id:
        return candidate
    if provider_id and candidate.startswith(f"{provider_id}."):
        member = candidate[len(provider_id) + 1 :]
        return candidate if _MEMBER_PATH.match(member) else None

    return candidate if _MEMBER_PATH.match(candidate) else None


@dataclass(frozen=True, slots=True)
class RenamePaths:
    """A namespace-preserving rename, split into the parts a codemod needs."""

    #: The shared leading segment, e.g. ``openai``. Not rewritten -- it names
    #: the receiver, which in real code is a local variable with any name.
    namespace: str
    #: Everything after the namespace, before: ``createChatCompletion``.
    old_path: str
    #: Everything after the namespace, after: ``chat.completions.create``.
    new_path: str


def rename_paths(old_api: str, new_api: str) -> RenamePaths | None:
    """Split a rename into namespace + old path + new path, or ``None``.

    Returns ``None`` unless both symbols parse, share a first segment, and
    each has at least two segments. The shared-namespace requirement is what
    makes the rewrite safe: it means we are renaming a *member* of a known
    object, so the receiver in the source can be matched structurally
    instead of by name. ``a.b`` -> ``c.d`` is a different, much harder
    change and is left to the model.

    Deliberately does *not* pass ``provider_id`` to ``normalize_api``: the
    ``old_path``/``new_path`` this returns are only ever fed into JS
    identifier regexes (see :mod:`depfix.codemods.javascript`), so a
    hyphenated head would be meaningless there. A rename whose head is a
    hyphenated provider id is left to the LLM.
    """
    old = normalize_api(old_api)
    new = normalize_api(new_api)
    if old is None or new is None:
        return None
    old_parts = old.split(".")
    new_parts = new.split(".")
    if len(old_parts) < 2 or len(new_parts) < 2:
        return None
    if old_parts[0] != new_parts[0]:
        return None
    old_path = ".".join(old_parts[1:])
    new_path = ".".join(new_parts[1:])
    if old_path == new_path:
        return None
    return RenamePaths(namespace=old_parts[0], old_path=old_path, new_path=new_path)
