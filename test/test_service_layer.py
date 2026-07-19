from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from editable_pptx.api import create_app
from editable_pptx.job_store import JobStore, sha256_file
from editable_pptx.models import SlideSpec
from editable_pptx.ooxml_edit import (
    apply_ooxml_patch,
    extract_production_instructions,
    extract_shape_graph,
    extract_text_manifest,
)
from editable_pptx.openai_responses import normalize_structured_output_schema
from editable_pptx.pptx_agent import review_approved
from editable_pptx.powerpoint import validate_ooxml
from editable_pptx.renderer import render_deck, render_pptx
from editable_pptx.service_config import ServiceSettings
from editable_pptx.service_models import (
    ArtifactKind,
    JobOperation,
    PptxPatchPlan,
    PptxPatchReview,
)


def service_spec() -> SlideSpec:
    return SlideSpec.model_validate(
        {
            "version": "1.0",
            "source_width": 640,
            "source_height": 360,
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
                    "id": "business_content",
                    "name": "Business content",
                    "layer": 1,
                    "group_id": None,
                    "bounds": {"x": 40, "y": 40, "width": 300, "height": 40},
                    "rotation_deg": 0,
                    "text": "Revenue 2026: US$500m",
                    "font_family": "Arial",
                    "font_size_pt": 18,
                    "bold": False,
                    "italic": False,
                    "color": "#111111",
                    "opacity": 1,
                    "alignment": "left",
                    "vertical_alignment": "middle",
                    "line_spacing": 1,
                    "margin_px": 0,
                },
                {
                    "kind": "shape",
                    "id": "instruction_box",
                    "name": "Production instruction box",
                    "layer": 2,
                    "group_id": None,
                    "bounds": {"x": 150, "y": 160, "width": 340, "height": 80},
                    "rotation_deg": 0,
                    "preset": "rect",
                    "fill": {
                        "kind": "solid",
                        "color": "#C90000",
                        "opacity": 1,
                        "angle_deg": None,
                        "stops": [],
                    },
                    "stroke": {"color": "#C90000", "opacity": 1, "width_px": 1, "dash": "solid"},
                    "corner_radius": None,
                },
                {
                    "kind": "text",
                    "id": "instruction_text",
                    "name": "Production instruction",
                    "layer": 3,
                    "group_id": None,
                    "bounds": {"x": 165, "y": 175, "width": 310, "height": 50},
                    "rotation_deg": 0,
                    "text": "Please move the business content down by 10 points",
                    "font_family": "Arial",
                    "font_size_pt": 14,
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


class ServiceLayerTests(unittest.TestCase):
    def test_exact_note_operations_override_visual_unit_estimation(self) -> None:
        review = PptxPatchReview(
            instruction_fulfilled=False,
            content_preserved=True,
            production_notes_removed=True,
            layout_valid=True,
            editability_preserved=True,
            balanced_density=True,
            issues=["The move appears larger when estimated from rendered pixels."],
            repair_instruction="Move the card by exactly 28 points.",
            score=6,
        )
        deterministic = {
            "operation": "notes",
            "operation_checks_passed": True,
            "production_notes_removed": True,
            "ooxml_compatible": True,
            "content_preserved": True,
        }
        self.assertTrue(review_approved(review, deterministic))

        deterministic["operation"] = "beautify"
        self.assertFalse(review_approved(review, deterministic))

    def test_multi_slide_renderer_writes_two_native_slides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "two-slides.pptx"
            render_deck([service_spec(), service_spec()], output, assets_by_slide=[{}, {}])
            with zipfile.ZipFile(output) as archive:
                slides = [
                    name for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                ]
            self.assertEqual(len(slides), 2)

    def test_bearer_auth_protects_non_health_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", {"EDITABLE_PPTX_BEARER_TOKEN": "test-token"}
        ):
            app = create_app(ServiceSettings(home=Path(temporary)))
            client = TestClient(app)
            self.assertEqual(client.get("/v1/health").status_code, 200)
            self.assertEqual(client.get("/v1/jobs").status_code, 401)
            self.assertEqual(
                client.get(
                    "/v1/jobs", headers={"Authorization": "Bearer test-token"}
                ).status_code,
                200,
            )

    def test_patch_schema_is_responses_strict(self) -> None:
        schema = normalize_structured_output_schema(PptxPatchPlan)
        operation = schema["$defs"]["PptxPatchOperation"]
        self.assertEqual(set(operation["required"]), set(operation["properties"]))
        self.assertFalse(operation["additionalProperties"])

    def test_ooxml_note_cleanup_preserves_business_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pptx"
            output = root / "output.pptx"
            render_pptx(service_spec(), source)
            instructions = extract_production_instructions(source)
            self.assertTrue(instructions)
            shapes = extract_shape_graph(source)
            business = next(item for item in shapes if item.text == "Revenue 2026: US$500m")
            plan = PptxPatchPlan.model_validate(
                {
                    "operation": "notes",
                    "source_sha256": sha256_file(source),
                    "instruction_summary": "Move business content and remove the production note",
                    "instructions": [item.model_dump(mode="json") for item in instructions],
                    "operations": [
                        {
                            "op_id": "move-business",
                            "action": "move",
                            "slide_index": 1,
                            "target_shape_id": business.shape_id,
                            "dy_pt": 10,
                            "source_instruction_ids": [instructions[0].id],
                            "rationale": "Execute the note",
                            "expected_effect": "Business content moves down by 10 points",
                        }
                    ],
                    "protected_shape_ids": [],
                    "content_invariant": False,
                    "cleanup_executed_instructions": True,
                    "layout_strategy": "local_reflow",
                    "minimum_font_size_pt": 7.5,
                }
            )
            apply_ooxml_patch(source, output, plan)
            manifest = extract_text_manifest(output)
            flat = [text for texts in manifest.values() for text in texts]
            self.assertIn("Revenue 2026: US$500m", flat)
            self.assertFalse(any("Please move" in text for text in flat))
            self.assertFalse(
                any("instruction" in item.name.lower() for item in extract_shape_graph(output))
            )
            moved = next(
                item for item in extract_shape_graph(output)
                if item.text == "Revenue 2026: US$500m"
            )
            self.assertAlmostEqual((moved.y_pt or 0) - (business.y_pt or 0), 10, places=2)
            self.assertTrue(validate_ooxml(output)["compatible"])

    def test_job_store_plan_and_artifact_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root / "service")
            source = root / "source.pptx"
            render_pptx(service_spec(), source)
            job = store.create_job(JobOperation.NOTES, {}, mode="plan")
            artifact = store.ingest_input(job.id, source)
            plan_file = store.job_dir(job.id) / "artifacts" / "plan.json"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text("{}", encoding="utf-8")
            plan_artifact = store.register_artifact(
                job.id, plan_file, kind=ArtifactKind.PLAN
            )
            plan = store.create_plan(
                job.id, JobOperation.NOTES, artifact.sha256, plan_artifact.id
            )
            self.assertEqual(store.get_plan(plan.id).artifact_id, plan_artifact.id)
            self.assertEqual(store.artifact_path(artifact.id).read_bytes(), source.read_bytes())

    def test_api_accepts_pptx_and_artifact_chaining(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pptx"
            render_pptx(service_spec(), source)
            app = create_app(ServiceSettings(home=root / "service"))
            client = TestClient(app)
            with source.open("rb") as handle:
                response = client.post(
                    "/v1/notes",
                    files={"file": ("source.pptx", handle, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
                    data={"mode": "plan", "slides": "1"},
                )
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()
            self.assertEqual(job["operation"], "notes")
            self.assertEqual(job["request"]["slides"], {"all": False, "indices": [1]})
            source_id = job["request"]["input_artifact_ids"][0]
            bundle = client.post(f"/v1/jobs/{job['id']}/bundle")
            self.assertEqual(bundle.status_code, 201, bundle.text)
            bundle_path = app.state.store.artifact_path(bundle.json()["id"])
            with zipfile.ZipFile(bundle_path) as archive:
                self.assertIn("manifest.json", archive.namelist())
                self.assertTrue(any(name.endswith("source.pptx") for name in archive.namelist()))
            chained = client.post("/v1/validate", data={"source_artifact_id": source_id})
            self.assertEqual(chained.status_code, 202, chained.text)
            self.assertEqual(chained.json()["request"]["source_artifact_id"], source_id)


if __name__ == "__main__":
    unittest.main()
