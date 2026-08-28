# backend/app/services/relationship/cost.py
"""Gemini pricing math, per 1M tokens.

scrape.do is billed in credits, not USD, so it has no entry here — the counters in
s3_run_store carry the credit totals.
"""
from __future__ import annotations

from typing import Any, Optional

from app.services.common.env import get_float_env


def calculate_gemini_cost_usd(usage: Optional[dict[str, Any]]) -> float:
    if not usage:
        return 0.0

    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    input_usd_per_1m = get_float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10)
    output_usd_per_1m = get_float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40)

    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)


