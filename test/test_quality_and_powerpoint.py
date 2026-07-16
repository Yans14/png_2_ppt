from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from editable_pptx.models import SlideSpec
from editable_pptx.openai_vision import _focus_crops
from editable_pptx.powerpoint import (
    PowerPointCompatibilityError,
    validate_ooxml,
    validate_powerpoint,
)
from editable_pptx.qa import compare_images
from editable_pptx.quality import (
    apply_font_policy,
    candidate_decision,
    choose_refinement_model,
    local_adjustment_proposals,
    profile_for,
)
from editable_pptx.renderer import render_pptx


def simple_spec(*, font: str = "Arial") -> SlideSpec:
    return SlideSpec.model_validate(
        {
            "version": "1.0",
            "source_width": 320,
            "source_height": 180,
            "background": {
                "kind": "solid",
                "color": "#FFFFFF",
                "opacity": 1,
                "angle_deg": None,
                "stops": [],
            },
            "components": [],
            "elements": [
                {
                    "kind": "shape",
                    "id": "box",
                    "name": "Box",
                    "layer": 1,
                    "group_id": None,
                    "bounds": {"x": 40, "y": 50, "width": 120, "height": 45},
                    "rotation_deg": 0,
                    "preset": "roundRect",
                    "fill": {
                        "kind": "solid",
                        "color": "#087893",
                        "opacity": 1,
                        "angle_deg": None,
                        "stops": [],
                    },
                    "stroke": {
                        "color": "#FFFFFF",
                        "opacity": 0,
                        "width_px": 0,
                        "dash": "solid",
                    },
                    "corner_radius": 8,
                },
                {
                    "kind": "text",
                    "id": "label",
                    "name": "Label",
                    "layer": 2,
                    "group_id": None,
                    "bounds": {"x": 48, "y": 56, "width": 104, "height": 30},
                    "rotation_deg": 0,
                    "text": "Editable",
                    "font_family": font,
                    "font_size_pt": 16,
                    "bold": True,
                    "italic": False,
                    "color": "#FFFFFF",
                    "opacity": 1,
                    "alignment": "center",
                    "vertical_alignment": "middle",
                    "line_spacing": 1,
                    "margin_px": 0,
                },
            ],
            "reconstruction_notes": [],
        }
    )


class QualityAndPowerPointTests(unittest.TestCase):
    def test_object_regions_identify_the_shifted_editable_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = Image.new("RGB", (320, 180), "white")
            rendered = Image.new("RGB", (320, 180), "white")
            ImageDraw.Draw(reference).rounded_rectangle((40, 50, 160, 95), 8, fill="#087893")
            ImageDraw.Draw(rendered).rounded_rectangle((65, 50, 185, 95), 8, fill="#087893")
            reference_path = root / "reference.png"
            rendered_path = root / "rendered.png"
            reference.save(reference_path)
            rendered.save(rendered_path)

            metrics = compare_images(reference_path, rendered_path, spec=simple_spec())

            self.assertTrue(metrics["object_regions"])
            self.assertEqual(metrics["object_regions"][0]["id"], "box")
            self.assertLess(metrics["worst_significant_object_similarity"], 0.9)

    def test_local_proposals_keep_stable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            Image.new("RGB", (320, 180), "white").save(reference)
            metrics = {
                "reference_size": [320, 180],
                "object_regions": [
                    {
                        "id": "box",
                        "similarity_score": 0.4,
                        "pixel_mae": 0.2,
                        "high_error_fraction": 0.3,
                    }
                ]
            }

            proposals = local_adjustment_proposals(
                simple_spec(),
                metrics,
                reference,
                background_color="#FFFFFF",
                candidate_limit=4,
                object_limit=1,
            )

            self.assertEqual(len(proposals), 4)
            self.assertTrue(all(changed == ["box"] for _, changed, _ in proposals))
            self.assertTrue(
                all({item.id for item in proposal.elements} == {"box", "label"} for _, _, proposal in proposals)
            )

    def test_focus_crops_prioritize_the_largest_error_mass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            rendered = root / "rendered.png"
            Image.new("RGB", (320, 180), "white").save(source)
            Image.new("RGB", (320, 180), "white").save(rendered)
            metrics = {
                "object_regions": [
                    {
                        "id": "small",
                        "kind": "shape",
                        "bounds": {"x": 10, "y": 10, "width": 20, "height": 20},
                        "pixel_mae": 0.4,
                        "high_error_fraction": 0.5,
                        "similarity_score": 0.2,
                        "error_mass": 160,
                    },
                    {
                        "id": "large",
                        "kind": "path",
                        "bounds": {"x": 80, "y": 30, "width": 100, "height": 70},
                        "pixel_mae": 0.2,
                        "high_error_fraction": 0.3,
                        "similarity_score": 0.5,
                        "error_mass": 2800,
                    },
                ]
            }

            paths, regions = _focus_crops(source, rendered, metrics, root / "focus")

            self.assertEqual(regions[0]["object_id"], "large")
            self.assertEqual(regions[0]["source_image_position"], 3)
            self.assertEqual(len(paths), 4)
            self.assertTrue(all(path.exists() for path in paths))

    def test_targeted_gain_can_be_accepted_with_small_global_guard(self) -> None:
        audit = {
            "native_shape_objects": 2,
            "picture_objects": 0,
            "flattened_slide": False,
            "canvas_overflow_count": 0,
        }
        accepted, reason = candidate_decision(
            {
                "similarity_score": 0.897,
                "structural_score": 0.91,
                "worst_significant_object_similarity": 0.72,
            },
            audit,
            {
                "similarity_score": 0.9,
                "structural_score": 0.89,
                "worst_significant_object_similarity": 0.65,
            },
        )

        self.assertTrue(accepted)
        self.assertIn("structural", reason)

    def test_tiny_global_score_noise_does_not_accept_a_patch(self) -> None:
        audit = {
            "native_shape_objects": 2,
            "picture_objects": 0,
            "flattened_slide": False,
            "canvas_overflow_count": 0,
        }

        accepted, _ = candidate_decision(
            {
                "similarity_score": 0.90008,
                "structural_score": 0.899,
                "worst_significant_object_similarity": 0.65,
            },
            audit,
            {
                "similarity_score": 0.9,
                "structural_score": 0.9,
                "worst_significant_object_similarity": 0.65,
            },
        )

        self.assertFalse(accepted)

    def test_changed_object_gain_is_not_hidden_by_the_full_slide_score(self) -> None:
        audit = {
            "native_shape_objects": 2,
            "picture_objects": 0,
            "flattened_slide": False,
            "canvas_overflow_count": 0,
        }
        accepted, reason = candidate_decision(
            {
                "similarity_score": 0.899,
                "structural_score": 0.9,
                "worst_significant_object_similarity": 0.5,
                "object_regions": [
                    {"id": "farmer", "structural_score": 0.62, "similarity_score": 0.61}
                ],
            },
            audit,
            {
                "similarity_score": 0.9,
                "structural_score": 0.9,
                "worst_significant_object_similarity": 0.5,
                "object_regions": [
                    {"id": "farmer", "structural_score": 0.55, "similarity_score": 0.54}
                ],
            },
            ["farmer"],
        )

        self.assertTrue(accepted)
        self.assertIn("changed objects", reason)

    def test_balanced_profile_escalates_only_after_a_persistent_error(self) -> None:
        profile = profile_for("balanced")
        model, _ = choose_refinement_model(
            profile,
            correction_index=1,
            metrics={"structural_score": 0.5},
        )
        escalated, _ = choose_refinement_model(
            profile,
            correction_index=2,
            metrics={"structural_score": 0.5},
        )

        self.assertEqual(model, "gpt-5.6-luna")
        self.assertEqual(escalated, "gpt-5.6-terra")

    def test_portable_font_policy_reports_substitution(self) -> None:
        portable, substitutions = apply_font_policy(
            simple_spec(font="Brand Sans Custom"),
            "portable",
        )

        label = next(item for item in portable.elements if item.id == "label")
        self.assertEqual(label.font_family, "Arial")
        self.assertEqual(substitutions[0]["requested"], "Brand Sans Custom")

    def test_generated_package_passes_powerpoint_ooxml_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "valid.pptx"
            render_pptx(simple_spec(), output)

            result = validate_ooxml(output)

            self.assertTrue(result["compatible"])
            self.assertEqual(result["errors"], [])
            self.assertIn("Arial", result["fonts"])

    def test_broken_relationship_fails_ooxml_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "valid.pptx"
            broken = root / "broken.pptx"
            render_pptx(simple_spec(), valid)
            relationship_name = "ppt/slides/_rels/slide1.xml.rels"
            with zipfile.ZipFile(valid) as source, zipfile.ZipFile(broken, "w") as target:
                for member in source.infolist():
                    data = source.read(member.filename)
                    if member.filename == relationship_name:
                        text = data.decode("utf-8").replace(
                            "</Relationships>",
                            '<Relationship Id="rIdBroken" '
                            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                            'Target="../media/missing.png"/></Relationships>',
                        )
                        data = text.encode("utf-8")
                    target.writestr(member, data)

            result = validate_ooxml(broken)

            self.assertFalse(result["compatible"])
            self.assertIn("Broken relationship", " ".join(result["errors"]))

    @patch("editable_pptx.powerpoint.available_powerpoint_adapter", return_value=None)
    def test_required_real_powerpoint_validation_fails_when_unavailable(self, _mock: object) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "valid.pptx"
            render_pptx(simple_spec(), output)

            with self.assertRaises(PowerPointCompatibilityError):
                validate_powerpoint(output, mode="required")


if __name__ == "__main__":
    unittest.main()
