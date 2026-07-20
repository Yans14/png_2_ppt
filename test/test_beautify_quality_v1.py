from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from editable_pptx.api import create_app
from editable_pptx.agents_runtime import AgentResult, AgentsSdkRuntime, CostBudget
from editable_pptx.invariants import create_invariant_manifest, verify_invariants
from editable_pptx.job_store import JobStore, sha256_file
from editable_pptx.models import SlideSpec
from editable_pptx.native_rebuild import native_rebuild_deck
from editable_pptx.ooxml_edit import apply_ooxml_patch, extract_shape_graph
from editable_pptx.powerpoint import validate_ooxml
from editable_pptx.pptx_agent import review_approved
from editable_pptx.renderer import render_pptx
from editable_pptx.service_config import ServiceSettings
from editable_pptx.service_models import (
    BeautifyAnalysis,
    BeautifyTraceRecord,
    JobOperation,
    JobStatus,
    PptxPatchPlan,
    PptxPatchReview,
    TERMINAL_JOB_STATUSES,
    utc_now,
)
from editable_pptx.service_ops import OperationExecutor
from editable_pptx.template_apply import import_template_layout
from editable_pptx.template_catalog import TemplateCatalog


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "test" / "fixtures" / "powerpoint-compatibility-spec.json"


def fixture_deck(path: Path) -> Path:
    spec = SlideSpec.model_validate_json(SPEC.read_text(encoding="utf-8"))
    render_pptx(spec, path)
    return path


def replace_text_plan(source: Path, shape_id: int, text: str) -> PptxPatchPlan:
    return PptxPatchPlan.model_validate(
        {
            "operation": "beautify",
            "source_sha256": sha256_file(source),
            "instruction_summary": "test lexical invariant",
            "operations": [
                {
                    "op_id": "replace",
                    "action": "replace_text",
                    "slide_index": 1,
                    "target_shape_id": shape_id,
                    "text": text,
                    "rationale": "test",
                    "expected_effect": "text changes",
                }
            ],
            "protected_shape_ids": [],
            "content_invariant": True,
            "cleanup_executed_instructions": False,
            "layout_strategy": "preserve",
            "minimum_font_size_pt": 8,
        }
    )


class BeautifyQualityV1Tests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for logo fixture")
    def test_logo_crop_is_a_hard_invariant_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "cases.json"
            manifest.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "visual-logo",
                                "category": "visual",
                                "title": "Synthetic logo invariant fixture",
                                "variant": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    shutil.which("node") or "node",
                    str(ROOT / "scripts" / "generate_beautify_eval_corpus.js"),
                    str(manifest),
                    str(root),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            source = root / "visual-logo.pptx"
            shapes = extract_shape_graph(source)
            logo = next(item for item in shapes if "logo" in item.name.casefold())
            output = root / "cropped-logo.pptx"
            plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(source),
                    "instruction_summary": "attempt to crop a protected logo",
                    "operations": [
                        {
                            "op_id": "crop-logo",
                            "action": "crop_picture",
                            "slide_index": logo.slide_index,
                            "target_shape_id": logo.shape_id,
                            "crop_left": 0.05,
                            "rationale": "test invariant",
                            "expected_effect": "cropped logo",
                        }
                    ],
                    "layout_strategy": "preserve",
                    "minimum_font_size_pt": 8,
                }
            )
            manifest = create_invariant_manifest(source)
            self.assertTrue(manifest.slides[0].logo_states)
            apply_ooxml_patch(source, output, plan)
            report = verify_invariants(manifest, output)
            self.assertFalse(report.passed)
            self.assertIn(
                "logo_geometry_or_effect_changed",
                {item.code for item in report.violations},
            )

    def test_native_powerpoint_section_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            added = root / "section-added.pptx"
            add_plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(source),
                    "instruction_summary": "add a native PowerPoint section",
                    "section_operations": [
                        {
                            "op_id": "add-section",
                            "action": "add",
                            "name": "Executive summary",
                            "slide_indices": [1],
                        }
                    ],
                    "layout_strategy": "preserve",
                    "minimum_font_size_pt": 8,
                }
            )
            apply_ooxml_patch(source, added, add_plan)
            with zipfile.ZipFile(added) as archive:
                presentation = archive.read("ppt/presentation.xml").decode("utf-8")
            self.assertIn("Executive summary", presentation)
            self.assertIn("sectionLst", presentation)
            self.assertTrue(validate_ooxml(added)["compatible"])
            self.assertTrue(
                verify_invariants(create_invariant_manifest(source), added).passed
            )

            renamed = root / "section-renamed.pptx"
            rename_plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(added),
                    "instruction_summary": "rename the native PowerPoint section",
                    "section_operations": [
                        {
                            "op_id": "rename-section",
                            "action": "rename",
                            "name": "Executive summary",
                            "new_name": "Overview",
                        }
                    ],
                    "layout_strategy": "preserve",
                    "minimum_font_size_pt": 8,
                }
            )
            apply_ooxml_patch(added, renamed, rename_plan)
            with zipfile.ZipFile(renamed) as archive:
                presentation = archive.read("ppt/presentation.xml").decode("utf-8")
            self.assertIn("Overview", presentation)
            self.assertNotIn("Executive summary", presentation)

            removed = root / "section-removed.pptx"
            remove_plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(renamed),
                    "instruction_summary": "remove the native PowerPoint section",
                    "section_operations": [
                        {
                            "op_id": "remove-section",
                            "action": "remove",
                            "name": "Overview",
                        }
                    ],
                    "layout_strategy": "preserve",
                    "minimum_font_size_pt": 8,
                }
            )
            apply_ooxml_patch(renamed, removed, remove_plan)
            with zipfile.ZipFile(removed) as archive:
                presentation = archive.read("ppt/presentation.xml").decode("utf-8")
            self.assertNotIn("sectionLst", presentation)
            self.assertTrue(validate_ooxml(removed)["compatible"])

    def test_lexical_invariant_allows_case_but_blocks_word_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            text_shape = next(item for item in extract_shape_graph(source) if item.text)
            manifest = create_invariant_manifest(source)

            case_only = root / "case-only.pptx"
            apply_ooxml_patch(
                source,
                case_only,
                replace_text_plan(source, text_shape.shape_id, text_shape.text.upper()),
            )
            self.assertTrue(verify_invariants(manifest, case_only).passed)

            changed = root / "changed.pptx"
            apply_ooxml_patch(
                source,
                changed,
                replace_text_plan(source, text_shape.shape_id, "Modern PowerPoint compatibility"),
            )
            report = verify_invariants(manifest, changed)
            self.assertFalse(report.passed)
            self.assertIn("words_changed", {item.code for item in report.violations})

    def test_compound_align_operation_remains_native(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            shapes = extract_shape_graph(source)
            first, second = shapes[:2]
            output = root / "aligned.pptx"
            plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(source),
                    "instruction_summary": "align two native shapes",
                    "operations": [
                        {
                            "op_id": "align",
                            "action": "align",
                            "slide_index": 1,
                            "target_shape_id": first.shape_id,
                            "peer_shape_ids": [second.shape_id],
                            "alignment": "left",
                            "rationale": "shared grid",
                            "expected_effect": "same left coordinate",
                        }
                    ],
                    "protected_shape_ids": [],
                    "content_invariant": True,
                    "cleanup_executed_instructions": False,
                    "layout_strategy": "global_reflow",
                    "minimum_font_size_pt": 8,
                }
            )
            apply_ooxml_patch(source, output, plan)
            result = {item.shape_id: item for item in extract_shape_graph(output)}
            self.assertAlmostEqual(
                result[first.shape_id].x_pt or 0,
                result[second.shape_id].x_pt or 0,
                places=2,
            )
            self.assertTrue(validate_ooxml(output)["compatible"])

    def test_compound_group_operation_creates_a_native_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            shapes = extract_shape_graph(source)
            first, second = shapes[:2]
            new_id = max(item.shape_id for item in shapes) + 100
            output = root / "grouped.pptx"
            plan = PptxPatchPlan.model_validate(
                {
                    "operation": "beautify",
                    "source_sha256": sha256_file(source),
                    "instruction_summary": "group two native shapes",
                    "operations": [
                        {
                            "op_id": "group",
                            "action": "group",
                            "slide_index": 1,
                            "target_shape_id": first.shape_id,
                            "peer_shape_ids": [second.shape_id],
                            "new_shape_id": new_id,
                            "new_name": "Native test group",
                            "rationale": "keep related items together",
                            "expected_effect": "native editable group",
                        }
                    ],
                    "protected_shape_ids": [],
                    "content_invariant": True,
                    "cleanup_executed_instructions": False,
                    "layout_strategy": "global_reflow",
                    "minimum_font_size_pt": 8,
                }
            )
            apply_ooxml_patch(source, output, plan)
            result = {item.shape_id: item for item in extract_shape_graph(output)}
            self.assertEqual(result[new_id].kind, "grpSp")
            self.assertTrue(validate_ooxml(output)["compatible"])
            self.assertTrue(
                verify_invariants(create_invariant_manifest(source), output).passed
            )

    def test_catalog_deduplicates_and_matches_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            preview = root / "preview.png"
            Image.new("RGB", (320, 180), "white").save(preview)
            catalog = TemplateCatalog(root / "catalog")
            template, duplicate = catalog.import_deck(source, preview_paths=[preview])
            repeated, repeated_duplicate = catalog.import_deck(source, preview_paths=[preview])
            matches = catalog.match_deck(source)

            self.assertFalse(duplicate)
            self.assertTrue(repeated_duplicate)
            self.assertEqual(template.id, repeated.id)
            self.assertEqual(len(catalog.list_templates()), 1)
            self.assertTrue(matches)
            self.assertEqual(matches[0].source_slide_index, 1)

    def test_template_layout_import_preserves_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            output = root / "with-template-layout.pptx"
            manifest = create_invariant_manifest(source)
            import_template_layout(
                source,
                source,
                output,
                template_slide_index=1,
                source_slide_indices=[1],
            )
            self.assertTrue(validate_ooxml(output)["compatible"])
            self.assertTrue(verify_invariants(manifest, output).passed)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for native rebuild")
    def test_native_rebuild_remains_editable_and_invariant_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            output = root / "rebuilt.pptx"
            native_rebuild_deck(
                source,
                output,
                report_path=root / "rebuild.json",
                minimum_font_size_pt=8,
                timeout_seconds=60,
            )
            self.assertTrue(validate_ooxml(output)["compatible"])
            self.assertTrue(
                verify_invariants(create_invariant_manifest(source), output).passed
            )

    def test_api_exposes_additive_quality_and_catalog_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            app = create_app(ServiceSettings(home=root / "service"))
            client = TestClient(app)
            with source.open("rb") as handle:
                response = client.post(
                    "/v1/beautify",
                    files={
                        "file": (
                            "source.pptx",
                            handle,
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        )
                    },
                    data={
                        "mode": "plan",
                        "max_attempts": "5",
                        "restyle_mode": "structural",
                        "trace_level": "metadata",
                    },
                )
            self.assertEqual(response.status_code, 202, response.text)
            request = response.json()["request"]
            self.assertEqual(request["max_candidates"], 3)
            self.assertTrue(request["legacy_max_attempts_clamped"])
            self.assertEqual(request["body_min_font_size_pt"], 8)
            warnings = [
                item
                for item in app.state.store.events_after(response.json()["id"])
                if item.event_type == "request.warning"
            ]
            self.assertEqual(warnings[0].payload["code"], "max_attempts_clamped")
            self.assertEqual(client.get("/v1/templates").json(), [])
            with source.open("rb") as handle:
                no_trace = client.post(
                    "/v1/beautify",
                    files={
                        "file": (
                            "source.pptx",
                            handle,
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        )
                    },
                    data={"mode": "plan", "trace_level": "none"},
                )
            self.assertEqual(no_trace.status_code, 202, no_trace.text)
            self.assertEqual(
                client.get(f"/v1/jobs/{no_trace.json()['id']}/trace").json()["status"],
                "disabled",
            )
            reindex = client.post("/v1/templates/reindex")
            self.assertEqual(reindex.status_code, 202, reindex.text)
            self.assertEqual(reindex.json()["operation"], "template_reindex")

    def test_failed_quality_is_terminal(self) -> None:
        self.assertIn(JobStatus.FAILED_QUALITY, TERMINAL_JOB_STATUSES)

    def test_missing_slide_vote_rejects_the_whole_deck(self) -> None:
        review = PptxPatchReview(
            instruction_fulfilled=True,
            content_preserved=True,
            production_notes_removed=True,
            layout_valid=True,
            editability_preserved=True,
            balanced_density=True,
            score=9,
            slide_scores={},
        )
        deterministic = {
            "selected_slides": [1],
            "ooxml_compatible": True,
            "content_preserved": True,
            "invariants_passed": True,
            "font_sizes_passed": True,
            "powerpoint_compatible": True,
        }
        self.assertFalse(review_approved(review, deterministic))

    def test_agents_sdk_adapter_uses_typed_output_without_network(self) -> None:
        output = BeautifyAnalysis(
            source_sha256="0" * 64,
            archetypes={1: "content"},
            restyle_mode="conservative",
            selected_layouts={},
            template_matches=[],
            protected_shape_ids=[],
            fallback_reasons=["no_template_above_threshold"],
            confidence=0.4,
        )
        response = SimpleNamespace(
            final_output=output,
            raw_responses=[
                SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=100, output_tokens=50)
                )
            ],
        )
        runtime = AgentsSdkRuntime(
            model="gpt-5.5",
            job_id="test-job",
            trace_level="metadata",
            max_cost_usd=1,
        )
        with patch("editable_pptx.agents_runtime.resolve_api_key", return_value="test-key"), patch(
            "agents.Runner.run_sync", return_value=response
        ):
            result = runtime.analyze(
                {"source_sha256": "0" * 64, "selected_slides": [1]},
                image_paths=[],
            )
        self.assertIsInstance(result.output, BeautifyAnalysis)
        self.assertEqual(result.trace.input_tokens, 100)
        self.assertAlmostEqual(result.trace.estimated_cost_usd or 0, 0.002, places=6)

    def test_quality_pipeline_publishes_only_after_hard_gates(self) -> None:
        class FakeRuntime:
            def __init__(self, *, model: str, job_id: str, **_: object) -> None:
                self.model = model
                self.job_id = job_id
                self.budget = CostBudget(5)

            def trace(self, stage: str, candidate_index: int | None = None) -> BeautifyTraceRecord:
                return BeautifyTraceRecord(
                    trace_id=f"{stage}-{candidate_index or 0}",
                    job_id=self.job_id,
                    stage=stage,
                    agent=stage,
                    model=self.model,
                    candidate_index=candidate_index,
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    latency_ms=1,
                )

            def analyze(self, payload: dict, *, image_paths: list[Path]) -> AgentResult:
                del image_paths
                return AgentResult(
                    output=BeautifyAnalysis(
                        source_sha256=payload["source_sha256"],
                        archetypes={1: "content"},
                        restyle_mode="conservative",
                        selected_layouts={},
                        template_matches=[],
                        protected_shape_ids=[],
                        fallback_reasons=["catalog_disabled"],
                        confidence=0.4,
                    ),
                    trace=self.trace("analysis"),
                )

            def design(
                self,
                payload: dict,
                *,
                image_paths: list[Path],
                candidate_index: int,
            ) -> AgentResult:
                del image_paths
                return AgentResult(
                    output=PptxPatchPlan(
                        operation="beautify",
                        source_sha256=payload["source_sha256"],
                        instruction_summary="retain content and normalize hierarchy",
                        operations=[],
                        protected_shape_ids=[],
                        content_invariant=True,
                        cleanup_executed_instructions=False,
                        layout_strategy="preserve",
                        minimum_font_size_pt=8,
                    ),
                    trace=self.trace("design", candidate_index),
                )

            def review(
                self,
                payload: dict,
                *,
                image_paths: list[Path],
                candidate_index: int,
            ) -> AgentResult:
                del image_paths
                selected = payload["selected_slides"]
                return AgentResult(
                    output=PptxPatchReview(
                        instruction_fulfilled=True,
                        content_preserved=True,
                        production_notes_removed=True,
                        layout_valid=True,
                        editability_preserved=True,
                        balanced_density=True,
                        score=9.1,
                        rubric_scores={"polish": 9.1},
                        slide_scores={int(item): 9.1 for item in selected},
                        relative_improvement=1.1,
                    ),
                    trace=self.trace("review", candidate_index),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture_deck(root / "source.pptx")
            store = JobStore(root / "service")
            job = store.create_job(
                JobOperation.BEAUTIFY,
                {
                    "catalog_enabled": False,
                    "max_candidates": 1,
                    "powerpoint_validation": "off",
                    "trace_level": "metadata",
                    "slides": {"all": True, "indices": []},
                },
            )
            uploaded = store.ingest_input(job.id, source)
            job = store.replace_request(
                job.id,
                {**job.request, "input_artifact_ids": [uploaded.id]},
            )
            with patch("editable_pptx.service_ops.AgentsSdkRuntime", FakeRuntime):
                result = OperationExecutor(store, slide_concurrency=2).execute(
                    job,
                    lambda *_: None,
                )
            self.assertIsNotNone(result.best_path)
            assert result.best_path is not None
            self.assertTrue(validate_ooxml(result.best_path)["compatible"])
            self.assertTrue(
                verify_invariants(
                    create_invariant_manifest(source), result.best_path
                ).passed
            )

            plan_job = store.create_job(
                JobOperation.BEAUTIFY,
                {
                    "catalog_enabled": False,
                    "max_candidates": 3,
                    "powerpoint_validation": "off",
                    "trace_level": "metadata",
                    "slides": {"all": True, "indices": []},
                },
                mode="plan",
            )
            plan_upload = store.ingest_input(plan_job.id, source)
            plan_job = store.replace_request(
                plan_job.id,
                {**plan_job.request, "input_artifact_ids": [plan_upload.id]},
            )
            with patch("editable_pptx.service_ops.AgentsSdkRuntime", FakeRuntime):
                plan_result = OperationExecutor(store, slide_concurrency=2).execute(
                    plan_job,
                    lambda *_: None,
                )
            self.assertIsNone(plan_result.best_path)
            self.assertTrue(any(item.kind.value == "preview" for item in plan_result.artifacts))
            self.assertFalse(any(item.kind.value == "pptx" for item in plan_result.artifacts))


if __name__ == "__main__":
    unittest.main()
