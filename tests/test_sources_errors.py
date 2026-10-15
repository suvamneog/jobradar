"""Regression tests for safe, contextual ATS fetch failures."""
import os
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobradar import sources


class FetchCompanyErrorTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"name": "ExampleCo", "board": "greenhouse", "slug": "example"}

    def test_http_error_logs_company_source_operation_and_status(self):
        response = requests.Response()
        response.status_code = 429
        # Deliberately include a URL-like detail to ensure it is not echoed.
        error = requests.exceptions.HTTPError(
            "request failed at https://example.invalid/?token=secret", response=response
        )

        with patch.object(sources, "_get", side_effect=error):
            with self.assertLogs("jobradar.sources", level="WARNING") as captured:
                jobs, err = sources.fetch_company(self.entry)

        self.assertEqual(jobs, [])
        self.assertEqual(
            err, "source=greenhouse operation=fetch reason=HTTP 429"
        )
        message = " ".join(captured.output)
        self.assertIn("company=ExampleCo", message)
        self.assertIn("source=greenhouse", message)
        self.assertIn("operation=fetch", message)
        self.assertIn("reason=HTTP 429", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("example.invalid", message)

    def test_timeout_is_reported_without_aborting_fetch_contract(self):
        with patch.object(sources, "_get", side_effect=requests.exceptions.Timeout):
            with self.assertLogs("jobradar.sources", level="WARNING") as captured:
                jobs, err = sources.fetch_company(self.entry)

        self.assertEqual(jobs, [])
        self.assertEqual(
            err, "source=greenhouse operation=fetch reason=request timed out"
        )
        self.assertIn("company=ExampleCo", captured.output[0])
        self.assertIn("reason=request timed out", captured.output[0])


if __name__ == "__main__":
    unittest.main()
