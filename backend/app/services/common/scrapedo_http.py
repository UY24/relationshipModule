# backend/app/services/common/scrapedo_http.py
"""Shared scrape.do HTTP plumbing: one pooled client, redaction, backoff.

One pooled AsyncClient for every scrape.do call the relationship pipeline makes,
plus the redaction and backoff its client needs.

Billing: scrape.do charges CREDITS_PER_CALL per *successful* call; failed attempts
are free, which is why retrying costs only latency.

# ponytail: ai_mode/scrapedo_client.py still carries its own near-duplicate of this.
# Both hit /plugin/google/search/ai-mode but with different params and retry shapes;
# merge them into one client if a third caller appears.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from app.services.common.env import get_float_env as _get_float_env
from app.services.common.provider_limits import scrapedo_limit

# scrape.do's published price for a plugin call. Not an env var: it's their pricing,
# not a per-deployment setting.
CREDITS_PER_CALL = 10

# Cap for any single backoff sleep, so a hostile Retry-After can't park a worker.
MAX_BACKOFF_SECONDS = 30.0


# One AsyncClient reused across rows. A fresh client per row costs ~330ms of DNS/TCP/TLS
# setup on every call (measured) and forfeits keep-alive entirely — at 500k rows that is
# ~46 hours of pure connection overhead. Keepalive is sized to the concurrency cap so
# every in-flight slot can hold a warm connection instead of re-handshaking.
_shared_client: Optional[httpx.AsyncClient] = None
_shared_client_loop: Any = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client, _shared_client_loop
    loop = asyncio.get_running_loop()
    # Rebuild if closed, or if we somehow moved loops (tests, restarted worker): an
    # AsyncClient is bound to the loop whose connections it holds.
    if (_shared_client is None or _shared_client.is_closed
            or _shared_client_loop is not loop):
        if _shared_client is not None and not _shared_client.is_closed:
            try:
                await _shared_client.aclose()
            except Exception:
                pass
        pool = max(1, scrapedo_limit())
        _shared_client = httpx.AsyncClient(
            timeout=_get_float_env("SCRAPEDO_TIMEOUT_SECONDS", 90.0),
            limits=httpx.Limits(max_connections=pool,
                                max_keepalive_connections=pool,
                                keepalive_expiry=60.0),
        )
        _shared_client_loop = loop
    return _shared_client


async def close_shared_client() -> None:
    """Release the pooled connections (called from the app/worker shutdown hook)."""
    global _shared_client, _shared_client_loop
    if _shared_client is not None and not _shared_client.is_closed:
        try:
            await _shared_client.aclose()
        except Exception:
            pass
    _shared_client = None
    _shared_client_loop = None


def _redact(value: Any) -> str:
    """Strip the API token out of anything we put in an error message or log."""
    return re.sub(r"([?&]token=)[^&'\"\s]+", r"\1[REDACTED]", str(value or ""))


def _safe_error(exc: Exception, response: Any = None, label: str = "ai-mode") -> str:
    """A durable, token-free error string for one failed scrape.do call.

    ``label`` names the ENDPOINT: this string is persisted as the row's error object
    and shown in "View failed rows".
    """
    status = getattr(response, "status_code", None)
    if status is not None:
        body = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                # _redact, exactly like the non-JSON fallback below: the provider echoes
                # the request URL (token and all) in some error bodies, and this string
                # is written to a durable per-row S3 object.
                body = _redact(payload.get("error") or payload.get("message") or "").strip()
        except Exception:
            body = _redact(response.text)[:200]
        if body:
            return f"scrape.do {label} search failed (HTTP {status}): {body.rstrip('.')}."
        return f"scrape.do {label} search failed (HTTP {status})."
    return _redact(exc) or type(exc).__name__


def _backoff_seconds(attempt: int, status: Optional[int], retry_after: Any) -> float:
    """Exponential backoff. A 429 gets a much larger base than a 5xx: rate limits need
    real room (a 1s/2s ramp exhausted every attempt on 27/50 rows in a live run), while
    a transient 5xx usually clears immediately. Retry-After wins when the server sends it.
    """
    if retry_after:
        try:
            return min(float(str(retry_after).strip()), MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    base = 5.0 if status == 429 else 1.0
    return min(base * (2 ** attempt), MAX_BACKOFF_SECONDS)
