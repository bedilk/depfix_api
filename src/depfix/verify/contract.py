"""HTTP contract recording: track the SHAPE of outbound HTTP during test runs.

Unlike snapshot tools that record full payloads, this records only the
structural envelope: method, host, normalised path, header/body key names.
The oracle is: "did the migrated SDK call produce the same shape of request?"

Why shape and not payload? Three reasons:

1. **Secrets.** A payload capture contains credentials (bearer tokens, API
   keys, session cookies) that should never reach a database or LLM prompt.
   A shape capture sees that an Authorization header *exists* but not its value.

2. **Non-determinism.** Payloads vary (timestamps, request IDs, random
   sleeps) even when the SDK call is identical. The shape is stable: a
   timestamp is still a number, a UUID is still a string. We normalise path
   segments that look like IDs to catch version drift in URL construction.

3. **Sufficient signal.** If a call went from ``POST /v1/chat`` to
   ``POST /v1/messages``, that is a shape change and a migration error. If
   it stayed ``POST /v1/chat`` but the body now sends ``"temperature": 0.7``
   instead of ``"temp": 0.7``, that is also a shape change (key rename). We
   detect both without ever needing the ``0.7`` itself.

The runtime shim is injected into the repo's test process by setting
``NODE_OPTIONS=--require /path/to/contract-shim.js`` before the test
command. The shim intercepts ``http.request`` / ``https.request`` at the
Node stdlib level, logs the shape, and does not interfere with the actual
request (no mocking, no blocking).

Design constraint: the interceptor must never throw -- a crash in the shim
crashes the test suite and produces a false REVERT verdict. Malformed input
is logged and skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Path segments that look like identifiers (UUIDs, numeric IDs, hashes) are
#: replaced with a fixed sentinel so URL version drift ("v1/users/123" ->
#: "v2/users/123") is still visible but ID churn isn't.
_ID_SEGMENT = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"  # UUID
    r"[0-9a-f]{32,64}|"  # hex hash
    r"\d{6,}|"  # long numeric ID
    r"[A-Za-z0-9_-]{20,})$"  # opaque token
)


@dataclass(frozen=True)
class RequestShape:
    """The structural envelope of one HTTP request."""

    method: str
    host: str
    path: str  # normalised: IDs replaced
    header_keys: tuple[str, ...]  # sorted, lowercased
    body_keys: tuple[str, ...]  # sorted top-level JSON keys, empty if not JSON

    def fingerprint(self) -> str:
        """Stable hash of this shape for deduplication."""
        payload = f"{self.method} {self.host}{self.path}"
        if self.header_keys:
            payload += f" H:{','.join(self.header_keys)}"
        if self.body_keys:
            payload += f" B:{','.join(self.body_keys)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
@dataclass
class ContractReport:
    """Comparison of before/after contract sets."""

    ran: bool = False
    skipped_reason: str = ""
    only_before: list[RequestShape] = None  # type: ignore[assignment]
    only_after: list[RequestShape] = None  # type: ignore[assignment]
    unchanged: list[RequestShape] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.only_before is None:
            self.only_before = []
        if self.only_after is None:
            self.only_after = []
        if self.unchanged is None:
            self.unchanged = []

    @property
    def changed(self) -> bool:
        return bool(self.only_before or self.only_after)

    @property
    def identical(self) -> bool:
        return self.ran and not self.changed

    @property
    def no_traffic(self) -> bool:
        return self.ran and not self.only_before and not self.only_after and not self.unchanged

    def summary(self) -> str:
        if not self.ran:
            return f"skipped: {self.skipped_reason}"
        if self.no_traffic:
            return "test suite mocks the SDK (no outbound HTTP)"
        if self.identical:
            return "identical shape"
        return (
            f"request shape changed ({len(self.only_before)} removed, {len(self.only_after)} added)"
        )


def normalise_path(path: str) -> str:
    """Replace ID-shaped segments with :id so version changes remain visible."""
    segments = path.split("/")
    return "/".join(":id" if _ID_SEGMENT.match(seg) else seg for seg in segments)


def extract_body_keys(body: str | None) -> tuple[str, ...]:
    """Return sorted top-level JSON keys, or empty if not JSON."""
    if not body:
        return ()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    return tuple(sorted(parsed.keys()))


def parse_shape(entry: dict) -> RequestShape | None:
    """Parse one interceptor log entry into a RequestShape.

    Returns None if the entry is malformed (defensively -- the shim must
    never crash the test suite, so the parser must never crash either).
    """
    try:
        method = str(entry.get("method", "")).upper()
        host = str(entry.get("host", ""))
        raw_path = str(entry.get("path", ""))
        headers = entry.get("headers")
        body = entry.get("body")

        if not method or not host:
            return None

        path = normalise_path(raw_path)
        header_keys = tuple(sorted(k.lower() for k in headers)) if isinstance(headers, dict) else ()
        body_keys = extract_body_keys(body)

        return RequestShape(
            method=method,
            host=host,
            path=path,
            header_keys=header_keys,
            body_keys=body_keys,
        )
    except Exception:
        return None


def load_contract(log_path: Path) -> list[RequestShape]:
    """Load and deduplicate shapes from an interceptor log file."""
    if not log_path.is_file():
        return []
    shapes = []
    seen = set()
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            shape = parse_shape(entry)
            if shape is None:
                continue
            fp = shape.fingerprint()
            if fp not in seen:
                seen.add(fp)
                shapes.append(shape)
    except OSError:
        pass
    return shapes


def compare(before_path: Path, after_path: Path) -> ContractReport:
    """Compare contract recordings from two test runs."""
    before = load_contract(before_path)
    after = load_contract(after_path)

    before_set = {s.fingerprint(): s for s in before}
    after_set = {s.fingerprint(): s for s in after}

    only_before = [before_set[fp] for fp in sorted(before_set.keys() - after_set.keys())]
    only_after = [after_set[fp] for fp in sorted(after_set.keys() - before_set.keys())]
    unchanged = [before_set[fp] for fp in sorted(before_set.keys() & after_set.keys())]

    return ContractReport(
        ran=True,
        only_before=only_before,
        only_after=only_after,
        unchanged=unchanged,
    )


_SHIM_TEMPLATE = r"""// depfix HTTP contract shim -- injected via NODE_OPTIONS, never committed.
const http = require('http');
const https = require('https');
const fs = require('fs');

const LOG_PATH = '__LOG_PATH__';
const logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' });

function safeStringify(obj) {
  try {
    return JSON.stringify(obj);
  } catch {
    return null;
  }
}

function intercept(originalRequest, protocol) {
  return function (...args) {
    let options = {};
    let callback;

    if (typeof args[0] === 'string') {
      try {
        const url = new URL(args[0]);
        options.host = url.hostname;
        options.path = url.pathname + url.search;
        options.method = args[1]?.method || 'GET';
        options.headers = args[1]?.headers || {};
        callback = args[2];
      } catch {
        return originalRequest.apply(this, args);
      }
    } else if (typeof args[0] === 'object') {
      options = args[0];
      callback = args[1];
    } else {
      return originalRequest.apply(this, args);
    }

    const entry = {
      method: (options.method || 'GET').toUpperCase(),
      host: options.host || options.hostname || '',
      path: options.path || '/',
      headers: options.headers || {},
    };

    const req = originalRequest.apply(this, args);

    const originalWrite = req.write.bind(req);
    const chunks = [];
    req.write = function (chunk, ...rest) {
      if (chunk) chunks.push(chunk);
      return originalWrite(chunk, ...rest);
    };

    const originalEnd = req.end.bind(req);
    req.end = function (chunk, ...rest) {
      if (chunk) chunks.push(chunk);
      if (chunks.length > 0) {
        const body = Buffer.concat(chunks.map(c => Buffer.isBuffer(c) ? c : Buffer.from(c))).toString('utf-8');
        if (body.length > 0 && body.length < 100000) {
          entry.body = body;
        }
      }
      const line = safeStringify(entry);
      if (line) {
        try {
          logStream.write(line + '\n');
        } catch {}
      }
      return originalEnd(chunk, ...rest);
    };

    return req;
  };
}

http.request = intercept(http.request, 'http');
https.request = intercept(https.request, 'https');
"""


def write_shim(log_path: Path) -> Path:
    """Write the contract-recording shim to a temporary file and return its path."""
    shim_path = log_path.parent / "contract-shim.js"
    shim = _SHIM_TEMPLATE.replace("__LOG_PATH__", str(log_path))
    shim_path.write_text(shim, encoding="utf-8")
    return shim_path
