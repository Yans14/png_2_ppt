from __future__ import annotations

import importlib.util
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from PIL import Image


RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_benchmarks.py"
SPEC = importlib.util.spec_from_file_location("run_benchmarks", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class BenchmarkRunnerTests(unittest.TestCase):
    def test_full_run_uses_canonical_summary_name(self) -> None:
        self.assertEqual(RUNNER.summary_stem(None), "summary")
        self.assertEqual(RUNNER.summary_stem([]), "summary")
        self.assertEqual(
            RUNNER.summary_stem(None, rescore_existing=True),
            "summary-rescore",
        )

    def test_subset_run_cannot_overwrite_full_summary(self) -> None:
        self.assertEqual(
            RUNNER.summary_stem(["ida-hybrid-model"]),
            "summary-ida-hybrid-model",
        )
        self.assertEqual(
            RUNNER.summary_stem(["oecd", "agrifood"]),
            "summary-agrifood-oecd",
        )
        self.assertEqual(
            RUNNER.summary_stem(["oecd"], rescore_existing=True),
            "summary-rescore-oecd",
        )

    def test_cache_must_match_engine_metric_and_run_configuration(self) -> None:
        args = Namespace(
            model="gpt-5.5",
            iterations=1,
            target_score=0.93,
        )
        report = {
            "engine_version": RUNNER.__version__,
            "metric_version": RUNNER.METRIC_VERSION,
            "model": "gpt-5.5",
            "raster_policy": "photos-only",
            "requested_iterations": 1,
            "target_score": 0.93,
            "audit": {
                "native_shape_objects": 1,
                "picture_objects": 0,
                "flattened_slide": False,
                "canvas_overflow_count": 0,
            },
        }
        self.assertTrue(RUNNER.cache_matches(report, args))
        report["metric_version"] = "old"
        self.assertFalse(RUNNER.cache_matches(report, args))

    def test_blank_report_never_counts_as_a_valid_cached_reconstruction(self) -> None:
        report = {
            "audit": {
                "native_shape_objects": 0,
                "picture_objects": 0,
                "flattened_slide": False,
                "canvas_overflow_count": 0,
            }
        }
        self.assertFalse(RUNNER.report_meets_editability_contract(report))

    def test_targets_are_validated_before_paid_work_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            Image.new("RGB", (1280, 720), "white").save(root / "valid.png")
            RUNNER.validate_targets(
                [{"id": "valid"}],
                targets_dir=root,
                expected_width=1280,
                expected_height=720,
            )
            Image.new("RGB", (640, 480), "white").save(root / "wrong.png")
            with self.assertRaisesRegex(SystemExit, "expected 1280x720"):
                RUNNER.validate_targets(
                    [{"id": "wrong"}],
                    targets_dir=root,
                    expected_width=1280,
                    expected_height=720,
                )

    def test_offline_rescore_forces_zero_llm_iterations(self) -> None:
        self.assertEqual(
            RUNNER.effective_iterations(
                Namespace(rescore_existing=True, iterations=9)
            ),
            0,
        )
        self.assertEqual(
            RUNNER.effective_iterations(
                Namespace(rescore_existing=False, iterations=2)
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
