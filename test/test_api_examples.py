from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_api_examples import MANIFEST_PATH
from scripts.run_api_examples import ENDPOINT_KEYS, _is_external_blocker, _write_summary


class ApiExamplesBenchmarkTests(unittest.TestCase):
    def test_manifest_has_five_cases_per_endpoint(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertTrue(set(ENDPOINT_KEYS).issubset(manifest))
        for endpoint in ENDPOINT_KEYS:
            self.assertEqual(len(manifest[endpoint]), 5, endpoint)

    def test_quota_is_reported_as_external_blocker_not_quality_failure(self) -> None:
        error = {
            "type": "OpenAIResponsesError",
            "message": "You exceeded your current quota; check billing details.",
        }
        self.assertTrue(_is_external_blocker(error))
        records = [
            {
                "endpoint": "beautify",
                "case_id": "blocked",
                "status": "failed",
                "duration_seconds": 1,
                "error": error,
                "quality": {"passed": False},
                "input": "missing.pptx",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_summary(root, records)
            summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        self.assertIsNone(records[0]["quality"]["passed"])
        self.assertEqual(summary["aggregate"]["beautify"]["blocked_external"], 1)
        self.assertEqual(summary["aggregate"]["beautify"]["evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
