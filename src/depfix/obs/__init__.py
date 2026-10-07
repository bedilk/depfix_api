"""Observability: structured logging context and optional Sentry reporting.

See :mod:`depfix.obs.logging` for the actual implementation -- this package
exists so future observability concerns (metrics, tracing) have a home
without overloading ``depfix.obs.logging`` itself.
"""

from __future__ import annotations
