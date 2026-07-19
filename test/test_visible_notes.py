from __future__ import annotations

import unittest

from editable_pptx.models import SlideNotePatch, SlideSpec, apply_slide_patch
from editable_pptx.visible_notes import note_modification_prompt, validate_raster_policy


def note_spec() -> SlideSpec:
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
                    "id": "note_box",
                    "name": "Production note",
                    "layer": 3,
                    "group_id": None,
                    "bounds": {"x": 40, "y": 30, "width": 220, "height": 60},
                    "rotation_deg": 0,
                    "preset": "rect",
                    "fill": {
                        "kind": "solid",
                        "color": "#C90000",
                        "opacity": 1,
                        "angle_deg": None,
                        "stops": [],
                    },
                    "stroke": {
                        "color": "#C90000",
                        "opacity": 0,
                        "width_px": 0,
                        "dash": "solid",
                    },
                    "corner_radius": None,
                },
                {
                    "kind": "text",
                    "id": "note_text",
                    "name": "Production note text",
                    "layer": 4,
                    "group_id": None,
                    "bounds": {"x": 45, "y": 35, "width": 210, "height": 50},
                    "rotation_deg": 0,
                    "text": "Add four rows",
                    "font_family": "Arial",
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


class VisibleNotesTests(unittest.TestCase):
    def test_structural_patch_can_touch_more_than_refinement_limit(self) -> None:
        base = note_spec()
        row_template = base.elements[1].model_dump(mode="json")
        rows = []
        for index in range(7):
            row = dict(row_template)
            row["id"] = f"row_{index + 1}"
            row["name"] = f"Editable row {index + 1}"
            row["text"] = f"Placeholder {index + 1}"
            row["bounds"] = {
                "x": 20,
                "y": 10 + index * 20,
                "width": 120,
                "height": 18,
            }
            rows.append(row)
        patch = SlideNotePatch.model_validate(
            {
                "detected_notes": ["Add four rows"],
                "instruction_summary": "Expanded table to seven rows",
                "background": None,
                "upsert_components": [],
                "remove_component_ids": [],
                "upsert_elements": rows,
                "remove_element_ids": ["note_box", "note_text"],
                "reconstruction_notes": ["Visible note executed"],
            }
        )

        result = apply_slide_patch(base, patch)

        self.assertEqual(len(result.elements), 7)
        self.assertNotIn("note_text", {item.id for item in result.elements})
        self.assertEqual(result.reconstruction_notes, ["Visible note executed"])

    def test_prompt_includes_visible_note_and_supplemental_instruction(self) -> None:
        prompt = note_modification_prompt(
            note_spec(), supplemental_instruction="Preserve footer"
        )

        self.assertIn("Add four rows", prompt)
        self.assertIn("Preserve footer", prompt)

    def test_raster_none_accepts_native_note_edit(self) -> None:
        validate_raster_policy(note_spec(), "none")


if __name__ == "__main__":
    unittest.main()
