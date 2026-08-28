# backend/app/services/relationship/reporting.py
"""retry.csv: the rerun list a relationship run produces, and the passthrough that keeps
the user's own input columns in it.

retry.csv exists to be RE-UPLOADED, so it carries the original input cells verbatim under
the original header and adds exactly one column saying why the row is in there.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.services.common.text import passthrough_fieldnames, passthrough_row

# retry.csv's own column, added after the input header. Deduped by retry_column() when an
# input CSV already has one by that name.
RETRY_REASON_COLUMN = "retry_reason"

# iter_input_rows overwrites a real "row_index" input column with its own int index and
# parks the original value here — so passing an input row back out has to read from here.
_INPUT_SOURCE_OVERRIDES = {"row_index": "row_index__input"}


def s3_passthrough(header: list[str], reserved: Iterable[str]) -> list[tuple[str, str]]:
    """`passthrough_fieldnames` for rows that come from `s3_run_store.iter_input_rows`
    and therefore carry its injected `row_index`."""
    return passthrough_fieldnames(header, reserved, _INPUT_SOURCE_OVERRIDES)


def retry_column(header: list[str]) -> str:
    """The reason column's name, suffixed until it cannot collide with an input column."""
    name = RETRY_REASON_COLUMN
    while name in header:
        name += "_"
    return name


def retry_row(original: dict[str, Any], header: list[str], reason_column: str, *,
              attempts: int, credits: int, error: str = "",
              billed_empty: bool = False, llm_incomplete: bool = False,
              ) -> Optional[dict[str, str]]:
    """One retry.csv row, or None when the row got a real answer.

    Only rows with nothing to show for themselves belong in it — a row the provider
    answered is finished, and running it again just buys the same answer twice:

    - ``billed_empty`` — HTTP 200 that carried no data. The only case where credits were
      charged for nothing, i.e. the scrape.do refund claim. Listed first because it is the
      only reason that costs money.
    - ``error``      — died after every retry, or never ran at all.
    - ``llm_incomplete`` — the provider answered and we were billed, but the Gemini shard
      that had to read that answer never delivered (job FAILED/EXPIRED, or our poll gave
      up). The row has a scrape and no verdict. Listed LAST because it is the only reason
      whose rerun costs nothing from the provider: the scrape objects already exist, so a
      re-drive skips the row and redoes the LLM half alone.
    """
    if billed_empty:
        reason = "billed_empty: HTTP 200 returned no data — refundable"
    elif error:
        # Credits on an errored row mean the call returned HTTP 200 and the BODY carried
        # the error — charged for nothing, exactly like billed_empty. A row that died
        # before any 200 cost nothing, so it must not claim to be refundable.
        reason = f"error: {error}" + (" — billed, refundable" if credits else "")
    elif llm_incomplete:
        reason = ("llm_incomplete: scraped and billed, but the Gemini batch never returned "
                  "a result for this row — rerun redoes the LLM only, no provider re-spend")
    else:
        return None
    row = passthrough_row(original, s3_passthrough(header, ()))
    row[reason_column] = f"{reason} | attempts={attempts} credits={credits}"
    return row
