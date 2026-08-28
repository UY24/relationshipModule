# backend/app/services/relationship/gemini_llm.py
"""The Gemini verdict prompt for the relationship pipeline, plus the two gates that
decide what the model's answer is allowed to change.

The prompt is BUILT here and SENT by relationship_runner through the shared Gemini Batch
driver (``ai_mode/gemini_batch.py``) — there is no per-row LLM call on this path.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.services.relationship.url_utils import (
    _normalize_url_for_compare,
    canonicalize_official_url,
    is_disallowed_official_url,
    url_matches_domain,
)

def build_relationship_prompt(
    x_name: str,
    y_name: str,
    input_url: str,
    candidates: list[str],
    ai_mode_evidence: list[dict[str, Any]],
    search_attempts: list[dict[str, Any]],
    x_domain: str = "",
) -> str:
    """Verdict prompt for the relationship pipeline's Gemini Batch phase.

    The row has three inputs and no location: Company X, Company Y and X's portfolio
    page (x_domain is derived from that URL).
    """
    input_obj = {
        "company_x": x_name, "company_x_domain": x_domain or None,
        "company_x_official_portfolio_page": input_url,
        "company_y": y_name,
    }
    evidence_sections: list[str] = []
    for item in ai_mode_evidence or []:
        if not isinstance(item, dict):
            continue
        lines = [f"[{item.get('phase') or 'unknown_phase'}]"]
        if item.get("query"):
            lines.append(f"Query: {item['query']}")
        if item.get("text"):
            lines.append(str(item["text"]))
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        if sources:
            lines.append("Sources:")
            for source in sources:
                if isinstance(source, dict) and source.get("url"):
                    lines.append(f"- {source.get('name') or source['url']}: {source['url']}")
        evidence_sections.append("\n".join(lines))
    evidence_text = "\n\n".join(evidence_sections)
    return (
        "You verify FINANCIAL relationships between companies and identify official websites.\n"
        "company_y is OCR-derived text from a logo on company_x's official portfolio page;\n"
        "it may be garbled, truncated, or contain noise around\n"
        "the real company name (e.g. 'YUZU SPARKLINGWE SANZO POMELO' contains 'SANZO').\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "relationship_status": "confirmed"|"not_confirmed"|"unclear",\n'
        '  "relationship_summary": string,\n'
        '  "relationship_evidence": [string],\n'
        '  "official_website": string|null,\n'
        '  "relationship_confidence_score": number,\n'
        '  "website_confidence_score": number,\n'
        '  "reason": string,\n'
        '  "extra_flags": [string]\n'
        "}\n"
        "Rules:\n"
        "- GROUND EVERYTHING IN THE SUPPLIED MATERIAL. Your job is to READ the evidence\n"
        "  below and report what it says — not to answer from your own knowledge of these\n"
        "  companies. Do not use anything you know that is not in the supplied evidence, do\n"
        "  not infer, do not assume, and do not fill gaps. If the evidence does not settle a\n"
        "  field, say so through the schema (null, or not_confirmed/unclear with a low\n"
        "  score) rather than producing a plausible answer. Two runs given the same evidence\n"
        "  must reach the same verdict.\n"
        "- Every claim in relationship_summary and relationship_evidence must be traceable\n"
        "  to a specific statement in the evidence below. Do not paraphrase into something\n"
        "  stronger than the source, and do not add facts the source does not state.\n"
        "- relationship_status is about a FINANCIAL relationship only (investment, portfolio\n"
        "  company, funding round, acquisition, fund backing). Mere similarity or co-mention\n"
        "  without financial context is NOT a relationship.\n"
        "- 'confirmed' requires explicit supporting evidence in the provided material.\n"
        "- official_website must be company_y's site, chosen from Candidate URLs ONLY.\n"
        "  Never invent a URL. Never return company_x's own website.\n"
        "- company_x_domain (when present) is company_x's own website domain — never return it as official_website.\n"
        "- Set official_website to null unless relationship_status is 'confirmed'.\n"
        "- Never return directory/listing/social/wiki/news/search/file URLs.\n"
        "- relationship_confidence_score is 0-100 = how strongly the provided evidence\n"
        "  (AI Mode text/sources) shows a FINANCIAL relationship EXISTS between company_x\n"
        "  and company_y: 0 = no relationship or no evidence, 100 = clearly evidenced. This is\n"
        "  confidence that the relationship is real, NOT confidence in your verdict — so a\n"
        "  not_confirmed verdict must carry a LOW score.\n"
        "- website_confidence_score is 0-100 for the Company Y URL; use 0 when no URL is found.\n"
        "- relationship_summary: 1-2 sentences quoting what the evidence says.\n"
        "- relationship_evidence: at most 3 concise statements from the supplied evidence.\n"
        "- extra_flags: optional short slugs like \"company_closed\" (evidence says company\n"
        "  shut down) or \"ocr_name_suspicious\" (the OCR text may name a different company).\n\n"
        f"Input: {json.dumps(input_obj, ensure_ascii=True)}\n\n"
        f"Candidate URLs: {json.dumps(list(candidates or []), ensure_ascii=True)}\n\n"
        # Every string the provider's response contained, in order — not a rendering of the
        # keys we happen to know about. AI Mode structures the same question differently from
        # call to call (sometimes headings, sometimes one ordered_list, sometimes a single
        # paragraph), so the answer's own sections are the only structure there is.
        "AI Mode evidence — every line Google's answer contained, in order. Section\n"
        "labels, prose, evidence bullets and any URL it cited all appear as plain lines:\n"
        f"{evidence_text}\n\n"
        f"Search attempts: {json.dumps(list(search_attempts or []), ensure_ascii=True)[:6000]}"
    )


_VALID_REL_STATUSES = {"confirmed", "not_confirmed", "unclear"}


def update_relationship_block(
    relationship: dict[str, Any],
    parsed: dict[str, Any],
    status: str,
    gate_flags: list[dict[str, str]],
) -> dict[str, Any]:
    relationship["status"] = status
    relationship["summary"] = str(parsed.get("relationship_summary") or "")
    evidence = parsed.get("relationship_evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    elif not isinstance(evidence, list):
        evidence = []
    relationship["evidence"] = [str(item) for item in evidence[:3]
                                if str(item).strip()]
    relationship["relationship_confidence_score"] = int(
        parsed.get("relationship_confidence_score") or 0)
    relationship["website_confidence_score"] = int(
        parsed.get("website_confidence_score") or 0)
    flags = relationship.get("flags") if isinstance(relationship.get("flags"), list) else []
    flags.extend(gate_flags)
    extra_flags = parsed.get("extra_flags") or []
    if isinstance(extra_flags, str):
        extra_flags = [extra_flags]
    for extra in extra_flags if isinstance(extra_flags, list) else []:
        if isinstance(extra, str) and extra.strip():
            flags.append({"flag": extra.strip(), "why": "reported by LLM"})
    relationship["flags"] = flags
    return relationship


def apply_relationship_gate(
    parsed: dict[str, Any],
    candidates: list[str],
    x_domain: str,
) -> tuple[Optional[str], str, list[dict[str, str]]]:
    """Code-enforced validation of a relationship LLM output (spec §4 rules 1-4).

    Returns (gated_url, relationship_status, gate_flags). gated_url is non-None
    ONLY when status == "confirmed" AND the URL is in the candidate set AND not
    disallowed AND not on Company X's own domain. A URL that exists but fails
    the gate is preserved in a flag so the evidence isn't lost.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    status = str(parsed.get("relationship_status") or "").strip().lower()
    if status not in _VALID_REL_STATUSES:
        status = "unclear"
    parsed["relationship_status"] = status
    flags: list[dict[str, str]] = []

    def _score(value: Any) -> int:
        try:
            return max(0, min(100, int(float(value or 0))))
        except (TypeError, ValueError):
            return 0

    legacy_score = _score(parsed.get("confidence_score"))
    relationship_score = _score(
        parsed.get("relationship_confidence_score", legacy_score))
    website_score = _score(parsed.get("website_confidence_score", legacy_score))
    parsed["relationship_confidence_score"] = relationship_score
    parsed["website_confidence_score"] = website_score

    raw_url = parsed.get("official_website")
    url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
    if url is None:
        parsed["website_confidence_score"] = 0

    if url is not None:
        normalized_candidates = {
            _normalize_url_for_compare(c) for c in (candidates or []) if str(c or "").strip()
        }
        normalized_candidates.discard("")
        if _normalize_url_for_compare(url) not in normalized_candidates:
            flags.append({"flag": "llm_url_out_of_candidates",
                          "why": f"LLM returned {url} which is not in the candidate set"})
            url = None
            parsed["website_confidence_score"] = 0
    if url is not None and is_disallowed_official_url(url):
        flags.append({"flag": "disallowed_url_dropped",
                      "why": f"{url} is a directory/social/file URL"})
        url = None
        parsed["website_confidence_score"] = 0
    if url is not None and url_matches_domain(url, x_domain):
        flags.append({"flag": "x_domain_candidate_dropped",
                      "why": f"{url} is Company X's own site, never Y's"})
        url = None
        parsed["website_confidence_score"] = 0

    if status != "confirmed" and url is not None:
        flag_name = ("relationship_unclear" if status == "unclear"
                     else "url_found_no_relationship")
        flags.append({"flag": flag_name,
                      "why": f"candidate URL {url} found but relationship is {status}"})
        url = None
    elif status == "unclear":
        flags.append({"flag": "relationship_unclear",
                      "why": "evidence neither confirms nor rules out a financial relationship"})

    if status == "confirmed" and url is None:
        flags.append({"flag": "relationship_confirmed_url_missing",
                      "why": "financial relationship confirmed but no valid Company Y URL passed the evidence gate"})

    parsed["confidence_score"] = (
        min(relationship_score, parsed["website_confidence_score"])
        if status == "confirmed" and url is not None else 0
    )

    return url, status, flags
