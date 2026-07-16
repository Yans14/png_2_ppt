from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from editable_pptx.image_analysis import analyze_image, extract_image_assets
from editable_pptx.models import (
    SlidePatch,
    SlideSpec,
    apply_slide_patch,
    clamp_slide_spec,
    openai_schema,
)
from editable_pptx.qa import audit_pptx, compare_images
from editable_pptx.renderer import render_pptx


def invisible_stroke() -> dict[str, object]:
    return {"color": "#FFFFFF", "opacity": 0, "width_px": 0, "dash": "solid"}


def arrow_spec() -> SlideSpec:
    return SlideSpec.model_validate(
        {
            "version": "1.0",
            "source_width": 960,
            "source_height": 540,
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
                    "kind": "text",
                    "id": "title",
                    "name": "Title",
                    "layer": 10,
                    "group_id": None,
                    "bounds": {"x": 45, "y": 32, "width": 500, "height": 60},
                    "rotation_deg": 0,
                    "text": "Editable curved arrow",
                    "font_family": "Arial",
                    "font_size_pt": 26,
                    "bold": True,
                    "italic": False,
                    "color": "#111111",
                    "opacity": 1,
                    "alignment": "left",
                    "vertical_alignment": "middle",
                    "line_spacing": 1,
                    "margin_px": 0,
                },
                {
                    "kind": "path",
                    "id": "curved-arrow",
                    "name": "True cubic curved arrow",
                    "layer": 20,
                    "group_id": None,
                    "bounds": {"x": 260, "y": 100, "width": 500, "height": 330},
                    "rotation_deg": 0,
                    "fill": {
                        "kind": "linear_gradient",
                        "color": None,
                        "opacity": 1,
                        "angle_deg": 330,
                        "stops": [
                            {"position": 0, "color": "#DCEFF4", "opacity": 0.1},
                            {"position": 0.55, "color": "#1B91AD", "opacity": 0.8},
                            {"position": 1, "color": "#087893", "opacity": 1},
                        ],
                    },
                    "stroke": invisible_stroke(),
                    "commands": [
                        {"op": "M", "x": 0.02, "y": 0.98, "x1": None, "y1": None, "x2": None, "y2": None},
                        {"op": "C", "x": 0.72, "y": 0.22, "x1": 0.18, "y1": 0.70, "x2": 0.50, "y2": 0.52},
                        {"op": "L", "x": 0.63, "y": 0.18, "x1": None, "y1": None, "x2": None, "y2": None},
                        {"op": "L", "x": 0.97, "y": 0.03, "x1": None, "y1": None, "x2": None, "y2": None},
                        {"op": "L", "x": 0.99, "y": 0.34, "x1": None, "y1": None, "x2": None, "y2": None},
                        {"op": "L", "x": 0.84, "y": 0.25, "x1": None, "y1": None, "x2": None, "y2": None},
                        {"op": "C", "x": 0.04, "y": 1.00, "x1": 0.58, "y1": 0.60, "x2": 0.25, "y2": 0.88},
                        {"op": "Z", "x": None, "y": None, "x1": None, "y1": None, "x2": None, "y2": None},
                    ],
                },
            ],
            "reconstruction_notes": ["Synthetic renderer regression fixture"],
        }
    )


class EditablePptxTests(unittest.TestCase):
    def test_local_analysis_counts_repeated_horizontal_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "bars.png"
            image = Image.new("RGB", (600, 340), "white")
            draw = ImageDraw.Draw(image)
            for y in (80, 130, 180, 230):
                draw.rectangle((360, y, 520, y + 14), fill="#0F5E7E")
            image.save(image_path)
            facts = analyze_image(image_path)
            bars = [
                item
                for item in facts.horizontal_rectangles
                if item["width"] >= 150 and 10 <= item["height"] <= 18
            ]
            self.assertEqual(len(bars), 4)

    def test_openai_schema_uses_supported_any_of(self) -> None:
        serialized = str(openai_schema())
        self.assertNotIn("'oneOf'", serialized)
        self.assertNotIn("'discriminator'", serialized)
        self.assertIn("'anyOf'", serialized)

    def test_path_validation_requires_cubic_controls(self) -> None:
        payload = arrow_spec().model_dump(mode="json")
        payload["elements"][1]["commands"][1]["x1"] = None
        with self.assertRaises(ValueError):
            SlideSpec.model_validate(payload)

    def test_full_bleed_photo_is_not_confused_with_flattened_artwork(self) -> None:
        payload = arrow_spec().model_dump(mode="json")
        payload["elements"].append(
            {
                "kind": "image",
                "id": "photo",
                "name": "Full bleed photo",
                "layer": 0,
                "group_id": None,
                "bounds": {"x": 0, "y": 0, "width": 960, "height": 540},
                "source_region": {"x": 0, "y": 0, "width": 960, "height": 540},
                "rotation_deg": 0,
                "opacity": 1,
                "preserve_aspect": True,
                "alt_text": "Photo",
                "content_type": "photo",
            }
        )
        spec = SlideSpec.model_validate(payload)
        self.assertEqual(spec.full_slide_images(), ["photo"])
        self.assertEqual(spec.suspicious_full_slide_images(), [])

        payload["elements"][-1]["content_type"] = "raster_illustration"
        spec = SlideSpec.model_validate(payload)
        self.assertEqual(spec.suspicious_full_slide_images(), ["photo"])

    def test_small_patch_preserves_untouched_elements_and_order(self) -> None:
        spec = arrow_spec()
        changed_title = spec.elements[0].model_copy(update={"text": "Improved title"})
        patch = SlidePatch(
            background=None,
            upsert_components=[],
            remove_component_ids=[],
            upsert_elements=[changed_title],
            remove_element_ids=["curved-arrow"],
            reconstruction_notes=None,
        )

        refined = apply_slide_patch(spec, patch)

        self.assertEqual([item.id for item in refined.elements], ["title"])
        self.assertEqual(refined.elements[0].text, "Improved title")
        self.assertEqual(refined.reconstruction_notes, spec.reconstruction_notes)

    def test_slide_score_prefers_matching_geometry_over_matching_color(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = Image.new("RGB", (320, 180), "white")
            recolored = Image.new("RGB", reference.size, "white")
            shifted = Image.new("RGB", reference.size, "white")
            ImageDraw.Draw(reference).rectangle((40, 55, 170, 105), fill="#006D87")
            ImageDraw.Draw(recolored).rectangle((40, 55, 170, 105), fill="#6B2D90")
            ImageDraw.Draw(shifted).rectangle((130, 55, 260, 105), fill="#006D87")
            paths = [root / "reference.png", root / "recolored.png", root / "shifted.png"]
            for image, path in zip((reference, recolored, shifted), paths, strict=True):
                image.save(path)

            recolored_score = compare_images(paths[0], paths[1])
            shifted_score = compare_images(paths[0], paths[2])

            self.assertGreater(recolored_score["similarity_score"], shifted_score["similarity_score"])
            self.assertGreater(recolored_score["structural_score"], shifted_score["structural_score"])

    def test_photo_asset_removes_text_pixels_recreated_as_native_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = Image.new("RGB", (960, 540), "#1A6875")
            draw = ImageDraw.Draw(source)
            draw.rectangle((50, 35, 320, 75), fill="white")
            source_path = root / "source.png"
            source.save(source_path)
            payload = arrow_spec().model_dump(mode="json")
            payload["elements"][0]["bounds"] = {
                "x": 50,
                "y": 35,
                "width": 270,
                "height": 40,
            }
            payload["elements"][0]["color"] = "#FFFFFF"
            payload["elements"].insert(
                0,
                {
                    "kind": "image",
                    "id": "photo",
                    "name": "Header photo",
                    "layer": 0,
                    "group_id": None,
                    "bounds": {"x": 0, "y": 0, "width": 960, "height": 540},
                    "source_region": {"x": 0, "y": 0, "width": 960, "height": 540},
                    "rotation_deg": 0,
                    "opacity": 1,
                    "preserve_aspect": True,
                    "alt_text": "Photo",
                    "content_type": "photo",
                },
            )
            spec = SlideSpec.model_validate(payload)

            assets = extract_image_assets(spec, source_path, root / "assets")
            with Image.open(assets["photo"]) as cleaned:
                cleaned_array = np.asarray(cleaned.convert("RGB"))

            self.assertLess(float(cleaned_array[40:70, 60:310].mean()), 150.0)

    def test_generated_elements_are_clamped_to_the_slide_canvas(self) -> None:
        payload = arrow_spec().model_dump(mode="json")
        payload["elements"][0]["bounds"] = {
            "x": 900,
            "y": -10,
            "width": 100,
            "height": 80,
        }
        clamped = clamp_slide_spec(SlideSpec.model_validate(payload))
        bounds = clamped.elements[0].bounds

        self.assertEqual((bounds.x, bounds.y, bounds.width, bounds.height), (900, 0, 60, 70))
        self.assertIn("title", clamped.reconstruction_notes[-1])

    def test_renderer_writes_native_cubic_gradient_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "arrow.pptx"
            render_pptx(arrow_spec(), output)
            audit = audit_pptx(output, arrow_spec())
            self.assertGreaterEqual(audit["cubic_bezier_segments"], 2)
            self.assertGreaterEqual(audit["native_gradient_fills"], 1)
            self.assertGreaterEqual(audit["native_text_runs"], 1)
            self.assertEqual(audit["picture_objects"], 0)
            self.assertEqual(audit["media_files"], 0)
            self.assertFalse(audit["flattened_slide"])

            with zipfile.ZipFile(output) as archive:
                xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            self.assertIn("Editable curved arrow", xml)
            self.assertIn("<a:cubicBezTo>", xml)
            self.assertIn("<a:gradFill", xml)


if __name__ == "__main__":
    unittest.main()
