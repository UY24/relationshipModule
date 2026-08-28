# backend/app/services/relationship/constants.py
"""The pipeline identifier and the row-error strings the relationship verdict produces.

Batch-vs-inline policy is NOT here: it lives in ``common/llm_batch.py``, which both this
package and ``ai_mode`` import.
"""
from __future__ import annotations

PIPELINE_RELATIONSHIP = "relationship"

# Relationship-mode row-error strings (user-visible in notconfirmed_relation.csv / UI).
REL_ERROR_NO_EVIDENCE = "No search evidence found for this company pair."
REL_ERROR_NO_X = "Company X missing — financial relationship cannot be verified."
REL_ERROR_NOT_CONFIRMED = "No financial relationship confirmed between Company X and Company Y."
REL_ERROR_CONFIRMED_URL_INVALID = "Relationship confirmed but no valid candidate URL survived validation."
