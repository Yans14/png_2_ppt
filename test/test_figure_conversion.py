from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from editable_pptx.figure import (
    FigureConversionOptions,
    _acceptance_checks,
    convert_figure,
    figure_to_slide_spec,
    load_figure,
)
from editable_pptx.figure_refinement import (
    FigureOptimizationAdvice,
    FigureRefinementError,
    FigureReview,
    apply_figure_review,
)
from editable_pptx.figure_optimizer import _gradient_profile_proposal
from editable_pptx.openai_responses import normalize_structured_output_schema
from editable_pptx.qa import (
    compare_context_figure_style,
    compare_figure_geometry,
    compare_images,
)


SVG_FIXTURE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160">
  <defs>
    <linearGradient id="arrowGradient" x1="0%" y1="100%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#DDEFF4" stop-opacity="0.2"/>
      <stop offset="55%" stop-color="#4D99AF"/>
      <stop offset="100%" stop-color="#196D8F"/>
    </linearGradient>
  </defs>
  <g transform="translate(10 8)">
    <path id="curved-arrow"
      d="M 5 142 C 45 52, 132 112, 190 25 L 170 26 L 218 4 L 220 55 L 202 39 C 142 130, 58 78, 28 150 Z"
      fill="url(#arrowGradient)" stroke="none"/>
    <circle id="badge" cx="60" cy="72" r="15" fill="#FFFFFF" stroke="#196D8F" stroke-width="2"/>
  </g>
</svg>
"""


class FigureConversionTests(unittest.TestCase):
    def test_default_semantic_review_model_is_gpt_5_5(self) -> None:
        self.assertEqual(FigureConversionOptions().model, "gpt-5.5")

    def test_semantic_geometry_metric_ignores_fill_color(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            candidate = Path(temp_dir) / "candidate.png"
            first = Image.new("RGB", (240, 160), "white")
            second = Image.new("RGB", (240, 160), "white")
            ImageDraw.Draw(first).polygon([(20, 130), (110, 45), (185, 45), (185, 20), (225, 60), (185, 100), (185, 75), (120, 75)], fill="#167A95")
            ImageDraw.Draw(second).polygon([(20, 130), (110, 45), (185, 45), (185, 20), (225, 60), (185, 100), (185, 75), (120, 75)], fill="#D95720")
            first.save(reference)
            second.save(candidate)
            metrics = compare_figure_geometry(reference, candidate)
            self.assertGreater(metrics["geometry_score"], 0.99)
            self.assertGreater(metrics["silhouette_iou"], 0.99)

    def test_local_geometry_metric_exposes_bad_arrowhead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            candidate = Path(temp_dir) / "candidate.png"
            first = Image.new("RGB", (420, 240), "white")
            second = Image.new("RGB", (420, 240), "white")
            ImageDraw.Draw(first).polygon(
                [(20, 100), (335, 100), (335, 72), (400, 120), (335, 168), (335, 140), (20, 140)],
                fill="#167A95",
            )
            ImageDraw.Draw(second).polygon(
                [(20, 100), (345, 100), (345, 88), (382, 120), (345, 152), (345, 140), (20, 140)],
                fill="#167A95",
            )
            first.save(reference)
            second.save(candidate)
            metrics = compare_figure_geometry(reference, candidate)
            self.assertGreater(metrics["geometry_score"], metrics["worst_local_iou"])
            self.assertLess(metrics["worst_local_iou"], 0.9)

    def test_foreground_style_metric_is_not_diluted_by_white_slide(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            candidate = Path(temp_dir) / "candidate.png"
            first = Image.new("RGB", (500, 300), "white")
            second = Image.new("RGB", (500, 300), "white")
            ImageDraw.Draw(first).rectangle((220, 125, 280, 175), fill="#167A95")
            ImageDraw.Draw(second).rectangle((220, 125, 280, 175), fill="#D95720")
            first.save(reference)
            second.save(candidate)
            metrics = compare_images(reference, candidate)
            self.assertGreater(metrics["similarity_score"], 0.95)
            self.assertLess(metrics["foreground_color_similarity"], 0.5)

    def test_context_target_region_drives_style_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.png"
            matching = Path(temp_dir) / "matching.png"
            wrong = Path(temp_dir) / "wrong.png"
            context = Image.new("RGB", (240, 140), "white")
            ImageDraw.Draw(context).rectangle((80, 30, 180, 120), fill="#247C98")
            context.save(target)
            candidate = Image.new("RGB", (160, 120), "white")
            ImageDraw.Draw(candidate).rectangle((30, 20, 130, 110), fill="#247C98")
            candidate.save(matching)
            bad_candidate = Image.new("RGB", (160, 120), "white")
            ImageDraw.Draw(bad_candidate).rectangle((30, 20, 130, 110), fill="#D95720")
            bad_candidate.save(wrong)
            good = compare_context_figure_style(
                target,
                matching,
                figure_bbox=(80, 30, 101, 91),
            )
            bad = compare_context_figure_style(
                target,
                wrong,
                figure_bbox=(80, 30, 101, 91),
            )
            self.assertGreater(
                good["foreground_color_similarity"],
                bad["foreground_color_similarity"],
            )
            self.assertLess(good["profile_mae"], bad["profile_mae"])

    def test_profile_descent_moves_native_stops_toward_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "figure.svg"
            source.write_text(SVG_FIXTURE, encoding="utf-8")
            spec = figure_to_slide_spec(load_figure(source), FigureConversionOptions())
            arrow = next(element for element in spec.elements if element.kind == "path")
            original = arrow.fill.stops[0].color
            proposal = _gradient_profile_proposal(
                spec,
                {
                    "profile": [
                        {
                            "progress": 0.0,
                            "target_rgb": [0.9, 0.95, 0.98],
                            "candidate_rgb": [0.98, 0.98, 0.98],
                        },
                        {
                            "progress": 1.0,
                            "target_rgb": [0.05, 0.4, 0.55],
                            "candidate_rgb": [0.15, 0.5, 0.65],
                        },
                    ]
                },
                learning_rate=0.5,
            )
            self.assertIsNotNone(proposal)
            optimized_arrow = next(
                element for element in proposal.elements if element.kind == "path"
            )
            self.assertNotEqual(optimized_arrow.fill.stops[0].color, original)

    def test_figure_review_schema_is_strict_responses_compatible(self) -> None:
        serialized = str(normalize_structured_output_schema(FigureReview))
        self.assertNotIn("'oneOf'", serialized)
        self.assertNotIn("'discriminator'", serialized)
        self.assertIn("'corrected_paths'", serialized)
        optimization_schema = str(normalize_structured_output_schema(FigureOptimizationAdvice))
        self.assertIn("'expected_loss_reduction'", optimization_schema)

    def test_llm_context_scores_must_meet_requested_thresholds(self) -> None:
        review = FigureReview.model_validate(
            {
                "verdict": "accept",
                "shape_family": "curved_arrow",
                "geometry_score": 0.96,
                "local_detail_score": 0.94,
                "style_score": 0.96,
                "color_score": 0.94,
                "gradient_score": 0.96,
                "confidence": 0.9,
                "summary": "Color is still below the requested threshold.",
                "issues": [],
                "corrected_paths": [],
            }
        )
        checks = _acceptance_checks(
            {"geometry_score": 0.96, "worst_local_iou": 0.94},
            {"foreground_color_similarity": 1.0, "gradient_profile_similarity": 1.0},
            review,
            options=FigureConversionOptions(
                target_geometry_score=0.93,
                target_local_geometry_score=0.90,
                target_foreground_style_score=0.95,
                target_gradient_score=0.95,
            ),
            use_reference_style=False,
        )
        self.assertFalse(checks["llm_color"])
        self.assertFalse(all(checks.values()))

    def test_llm_correction_preserves_path_identity_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "figure.svg"
            source.write_text(SVG_FIXTURE, encoding="utf-8")
            spec = figure_to_slide_spec(load_figure(source))
            paths = [element.model_dump(mode="json") for element in spec.elements]
            paths[0]["id"] = "wrong-id"
            review = FigureReview.model_validate(
                {
                    "verdict": "revise",
                    "shape_family": "curved_arrow",
                    "geometry_score": 0.7,
                    "local_detail_score": 0.6,
                    "style_score": 0.9,
                    "color_score": 0.9,
                    "gradient_score": 0.9,
                    "confidence": 0.95,
                    "summary": "Arrowhead and curve require correction.",
                    "issues": [
                        {
                            "category": "curvature",
                            "severity": "major",
                            "region": "middle",
                            "explanation": "Curve differs.",
                            "correction": "Move cubic controls.",
                        }
                    ],
                    "corrected_paths": paths,
                }
            )
            with self.assertRaisesRegex(FigureRefinementError, "path IDs"):
                apply_figure_review(spec, review)

    def test_style_only_llm_correction_cannot_move_path_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "figure.svg"
            source.write_text(SVG_FIXTURE, encoding="utf-8")
            spec = figure_to_slide_spec(load_figure(source))
            paths = [element.model_dump(mode="json") for element in spec.elements]
            original_x = paths[0]["commands"][1]["x"]
            paths[0]["commands"][1]["x"] = min(1.0, original_x + 0.1)
            paths[0]["fill"]["stops"][0]["color"] = "#D95720"
            review = FigureReview.model_validate(
                {
                    "verdict": "revise",
                    "shape_family": "curved_arrow",
                    "geometry_score": 0.96,
                    "local_detail_score": 0.95,
                    "style_score": 0.8,
                    "color_score": 0.7,
                    "gradient_score": 0.7,
                    "confidence": 0.95,
                    "summary": "Only the gradient needs correction.",
                    "issues": [
                        {
                            "category": "gradient",
                            "severity": "major",
                            "region": "whole",
                            "explanation": "Gradient differs.",
                            "correction": "Change stops only.",
                        }
                    ],
                    "corrected_paths": paths,
                }
            )
            corrected = apply_figure_review(spec, review)
            corrected_path = corrected.elements[0]
            self.assertEqual(corrected_path.commands[1].x, original_x)
            self.assertEqual(corrected_path.fill.stops[0].color, "#D95720")

    def test_svg_import_preserves_cubic_geometry_transform_and_gradient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "figure.svg"
            source.write_text(SVG_FIXTURE, encoding="utf-8")
            document = load_figure(source)
            self.assertEqual(document.input_type, "svg")
            self.assertEqual(len(document.paths), 2)
            arrow = next(item for item in document.paths if item.id == "curved-arrow")
            self.assertEqual(arrow.fill["kind"], "linear_gradient")
            self.assertTrue(any(command["op"] == "C" for command in arrow.commands))
            self.assertGreaterEqual(arrow.bbox[0], 10)
            spec = figure_to_slide_spec(
                document,
                FigureConversionOptions(canvas_width=960, canvas_height=540, padding=36),
            )
            self.assertEqual(len(spec.elements), 2)
            for element in spec.elements:
                if element.kind != "path":
                    continue
                for command in element.commands:
                    for coordinate in ("x", "y", "x1", "y1", "x2", "y2"):
                        value = getattr(command, coordinate)
                        if value is not None:
                            self.assertGreaterEqual(value, -1e-6)
                            self.assertLessEqual(value, 1.000001)

    def test_transparent_raster_ring_becomes_compound_native_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "ring.png"
            image = Image.new("RGBA", (220, 180), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((20, 10, 200, 170), fill="#D95720")
            draw.ellipse((72, 55, 148, 125), fill=(0, 0, 0, 0))
            image.save(source)
            document = load_figure(
                source,
                FigureConversionOptions(max_colors=1, simplify=1.0, min_area=8),
            )
            self.assertEqual(len(document.paths), 1)
            commands = document.paths[0].commands
            self.assertGreaterEqual(sum(command["op"] == "M" for command in commands), 2)
            self.assertGreaterEqual(sum(command["op"] == "Z" for command in commands), 2)
            self.assertGreaterEqual(sum(command["op"] == "C" for command in commands), 2)

    def test_raster_trace_removes_tiny_detached_specks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "speck.png"
            image = Image.new("RGB", (260, 180), "white")
            draw = ImageDraw.Draw(image)
            draw.polygon([(20, 150), (90, 35), (220, 20), (185, 80), (95, 100)], fill="#197C98")
            draw.rectangle((242, 164, 245, 167), fill="#197C98")
            image.save(source)
            document = load_figure(
                source,
                FigureConversionOptions(
                    background_threshold=6,
                    simplify=1.5,
                    min_area=12,
                ),
            )
            self.assertEqual(sum(command["op"] == "M" for command in document.paths[0].commands), 1)
            self.assertTrue(any("speck" in warning for warning in document.warnings))

    def test_raster_linear_color_ramp_becomes_native_gradient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "gradient.png"
            image = Image.new("RGB", (260, 160), "white")
            pixels = image.load()
            for y in range(30, 130):
                for x in range(25, 235):
                    position = (x - 25) / 209
                    pixels[x, y] = (
                        round(215 - position * 180),
                        round(235 - position * 105),
                        round(244 - position * 75),
                    )
            image.save(source)
            document = load_figure(
                source,
                FigureConversionOptions(
                    background_threshold=8,
                    max_colors=1,
                    simplify=1,
                ),
            )
            self.assertEqual(document.paths[0].fill["kind"], "linear_gradient")
            self.assertGreaterEqual(len(document.paths[0].fill["stops"]), 4)

    def test_strict_mode_rejects_unsupported_svg_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "masked.svg"
            source.write_text(
                """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
                <mask id="m"><rect width="10" height="10" fill="white"/></mask>
                <path d="M0 0L10 0L10 10Z" mask="url(#m)"/>
                </svg>""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Strict conversion rejected warnings"):
                load_figure(source, FigureConversionOptions(strict=True))

    def test_conversion_writes_custom_geometry_without_embedded_raster(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "figure.svg"
            output = Path(temp_dir) / "figure.pptx"
            source.write_text(SVG_FIXTURE, encoding="utf-8")
            report = convert_figure(
                source,
                output,
                options=FigureConversionOptions(
                    canvas_width=960,
                    canvas_height=540,
                    padding=36,
                    render_preview=False,
                ),
            )
            self.assertTrue(output.exists())
            self.assertGreaterEqual(report["audit"]["custom_geometry_paths"], 2)
            self.assertGreaterEqual(report["audit"]["cubic_bezier_segments"], 1)
            self.assertEqual(report["audit"]["picture_objects"], 0)
            self.assertEqual(report["audit"]["media_files"], 0)
            self.assertTrue(report["editable_contract"]["edit_points_available_in_powerpoint"])
            with zipfile.ZipFile(output) as archive:
                slide_xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            self.assertIn('name="editable:curved-arrow"', slide_xml)
            self.assertIn("<a:custGeom>", slide_xml)
            self.assertIn("<a:cubicBezTo>", slide_xml)
            self.assertIn("<a:gradFill", slide_xml)
            self.assertNotIn("<p:pic>", slide_xml)


if __name__ == "__main__":
    unittest.main()
