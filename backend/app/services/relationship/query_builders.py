# backend/app/services/relationship/query_builders.py
"""The one search query a relationship row sends to scrape.do's Google AI Mode.

The whole research prompt goes out as ``q=`` — see prompts/relationship_search.txt.
"""
from __future__ import annotations

from typing import Optional

from app.core.config import PROMPTS_DIR

_RELATIONSHIP_PROMPT_CACHE: Optional[str] = None


def load_relationship_prompt() -> str:
    """The single AI Mode search prompt, read once per process.

    Lives in app/prompts/ so it can be tuned without a code change — edit the file and
    restart the worker. The filename is fixed, exactly like AI Mode's own prompts in
    ``ai_mode/mode_config.py``.
    """
    global _RELATIONSHIP_PROMPT_CACHE
    if _RELATIONSHIP_PROMPT_CACHE is None:
        _RELATIONSHIP_PROMPT_CACHE = (
            PROMPTS_DIR / "relationship_search.txt"
        ).read_text(encoding="utf-8").strip()
    return _RELATIONSHIP_PROMPT_CACHE


def build_relationship_search_query(
    x_name: str,
    y_name: str,
    x_domain: str,
    input_url: str,
) -> str:
    """Fill the prompt for ONE row.

    The prompt file may use exactly these four placeholders: {x_name}, {y_name},
    {x_domain} (derived from input_url) and {input_url}. There is no location — the
    CSV carries only Company X, Company Y and the portfolio-page URL.

    Company Y is passed VERBATIM including OCR noise — that noise is meaningful input
    the model is explicitly asked to resolve, not something to clean up.
    """
    return load_relationship_prompt().format(
        x_name=(x_name or "").strip(),
        y_name=(y_name or "").strip(),
        x_domain=(x_domain or "").strip() or "an unknown domain",
        input_url=(input_url or "").strip() or "not provided",
    )
