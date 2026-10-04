"""Proxy pool adapter for reusable registration flows.

The adapter normalizes proxy URLs and records per-attempt success/failure through
an injected pool object. It keeps the reusable framework decoupled from the
application's SQLite-backed ``core.proxy_pool`` implementation.
"""

from __future__ import annotations

from typing import Any


class ProxyPoolAdapter:
    """Wrap a pool exposing ``get_next``/``report_success``/``report_fail``."""

    def __init__(self, pool: Any):
        self._pool = pool

    def acquire(self, region: str = "") -> str | None:
        raw = self._pool.get_next(region=region) if region else self._pool.get_next()
        return self._normalize(raw)

    def report_success(self, proxy: str | None) -> None:
        if not proxy:
            return
        report = getattr(self._pool, "report_success", None)
        if callable(report):
            report(proxy)

    def report_failure(self, proxy: str | None) -> None:
        if not proxy:
            return
        report = getattr(self._pool, "report_fail", None)
        if callable(report):
            report(proxy)

    @staticmethod
    def _normalize(proxy: str | None) -> str | None:
        if not proxy:
            return None
        from core.proxy_utils import normalize_proxy_url

        return normalize_proxy_url(proxy)


def create_application_proxy_pool_adapter() -> ProxyPoolAdapter:
    """Assemble the adapter over this application's database-backed proxy pool."""
    from core.proxy_pool import proxy_pool

    return ProxyPoolAdapter(proxy_pool)
