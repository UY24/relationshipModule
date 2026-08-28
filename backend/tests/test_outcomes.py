import unittest
from app.services.relationship import outcomes as o
from app.services.relationship.constants import REL_ERROR_NOT_CONFIRMED, REL_ERROR_NO_EVIDENCE


def _result(official=None, phases=None, llm_error=None):
    fr = []
    for p in (phases or []):
        fr.append({"phase": p.get("phase", "x"), "success": p.get("used", False),
                   "error": p.get("error"), "status_code": p.get("status_code"),
                   "error_category": p.get("error_category")})
    ctx = {"formatted_results": fr}
    if llm_error is not None:
        ctx["llm_error"] = llm_error
    return {"official_website": official, "context": ctx}


class TestOutcomeInfo(unittest.TestCase):
    def test_row_status_error_is_failed(self):
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_ERROR, o.SRC_SCRAPEDO, o.CAT_TIMEOUT).row_status, "failed")

    def test_row_status_notfound_is_completed(self):
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_NOT_FOUND).row_status, "completed")
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_FOUND).row_status, "completed")


class TestCategorizeHttp(unittest.TestCase):
    def test_429_rate_limit(self):
        self.assertEqual(o.categorize_http_error(429, "HTTP 429"), o.CAT_RATE_LIMIT)

    def test_401_403_auth(self):
        self.assertEqual(o.categorize_http_error(401, "x"), o.CAT_AUTH)
        self.assertEqual(o.categorize_http_error(403, "x"), o.CAT_AUTH)

    def test_5xx(self):
        self.assertEqual(o.categorize_http_error(502, "x"), o.CAT_HTTP_5XX)

    def test_timeout_from_repr(self):
        self.assertEqual(o.categorize_http_error(None, "ReadTimeout: timed out"), o.CAT_TIMEOUT)

    def test_connect_network(self):
        self.assertEqual(o.categorize_http_error(None, "ConnectError: name resolution"), o.CAT_NETWORK)

    def test_json_parse(self):
        self.assertEqual(o.categorize_http_error(None, "JSONDecodeError: line 1"), o.CAT_PARSE)

    def test_default_internal(self):
        self.assertEqual(o.categorize_http_error(None, "ValueError: nope"), o.CAT_INTERNAL)


class TestClassifyException(unittest.TestCase):
    def test_gemini_timeout(self):
        info = o.classify_exception(TimeoutError("timed out"), default_source=o.SRC_GEMINI)
        self.assertEqual((info.outcome, info.error_source, info.error_category),
                         (o.OUTCOME_ERROR, o.SRC_GEMINI, o.CAT_TIMEOUT))

    def test_server_default_internal(self):
        info = o.classify_exception(ValueError("bug"), default_source=o.SRC_SERVER)
        self.assertEqual(info.error_category, o.CAT_INTERNAL)
        self.assertIn("bug", info.error_detail)

    def test_error_source_override_from_exc_attr(self):
        exc = RuntimeError("relationship LLM error: boom")
        exc.error_source = o.SRC_GEMINI
        info = o.classify_exception(exc, default_source=o.SRC_SERVER)
        self.assertEqual(info.error_source, o.SRC_GEMINI)

    def test_error_source_falls_back_to_default_without_attr(self):
        info = o.classify_exception(RuntimeError("boom"), default_source=o.SRC_SERVER)
        self.assertEqual(info.error_source, o.SRC_SERVER)


