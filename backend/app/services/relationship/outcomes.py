# backend/app/services/relationship/outcomes.py
"""Row-outcome taxonomy: the found / not_found / error buckets and the error
source + category vocabulary, shared by the relationship pipeline and AI Mode.

One authority for turning a raised exception into an
(outcome, error_source, error_category, error_detail) tuple. Pure — no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

OUTCOME_FOUND = "found"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_ERROR = "error"

SRC_SCRAPEDO = "scrapedo"
SRC_GEMINI = "gemini"
SRC_SERVER = "server"

CAT_RATE_LIMIT = "rate_limit"
CAT_AUTH = "auth"
CAT_TIMEOUT = "timeout"
CAT_HTTP_5XX = "http_5xx"
CAT_NETWORK = "network"
CAT_PARSE = "parse"
CAT_INTERNAL = "internal"


@dataclass
class OutcomeInfo:
    outcome: str
    error_source: Optional[str] = None
    error_category: Optional[str] = None
    error_detail: Optional[str] = None
    degraded_search: bool = False

    @property
    def row_status(self) -> str:
        return "failed" if self.outcome == OUTCOME_ERROR else "completed"


def categorize_http_error(status_code: Optional[int], exc_repr: str) -> str:
    if status_code is not None:
        if status_code == 429:
            return CAT_RATE_LIMIT
        if status_code in (401, 403):
            return CAT_AUTH
        if 500 <= status_code <= 599:
            return CAT_HTTP_5XX
    low = (exc_repr or "").lower()
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return CAT_RATE_LIMIT
    if "401" in low or "403" in low or "unauthor" in low or "forbidden" in low or "api key" in low:
        return CAT_AUTH
    if "timeout" in low or "timed out" in low:
        return CAT_TIMEOUT
    if "connect" in low or "name resolution" in low or "connection" in low or "network" in low:
        return CAT_NETWORK
    if "jsondecode" in low or "expecting value" in low or "parse" in low:
        return CAT_PARSE
    return CAT_INTERNAL


def classify_exception(exc: BaseException, *, default_source: str) -> OutcomeInfo:
    exc_repr = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    # A raised exception may carry an explicit source override (e.g. a relationship
    # per-pair Gemini failure tags itself SRC_GEMINI before raising to stay retryable).
    source = getattr(exc, "error_source", None) or default_source
    return OutcomeInfo(
        outcome=OUTCOME_ERROR,
        error_source=source,
        error_category=categorize_http_error(status, exc_repr),
        error_detail=exc_repr,
    )


