from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .cli import convert as convert_image
from .agents_runtime import AgentsSdkRuntime
from .figure import FigureConversionOptions, convert_figure
from .image_analysis import extract_image_assets
from .invariants import (
    create_invariant_manifest,
    font_size_report,
    has_unsupported_rebuild_objects,
    verify_invariants,
)
from .job_store import JobStore, JobStoreError, sha256_file
from .models import SlideSpec, clamp_slide_spec
from .native_rebuild import NativeRebuildError, native_rebuild_deck
from .ooxml_edit import OoxmlEditError
from .powerpoint import validate_ooxml, validate_powerpoint
from .pptx_agent import (
    PptxAgentError,
    _selected,
    apply_ooxml_patch,
    deterministic_checks,
    extract_production_instructions,
    extract_shape_graph,
    extract_text_manifest,
    request_patch_plan,
    review_approved,
    review_patch,
    validate_patch_plan,
    write_json,
)
from .qa import audit_pptx
from .renderer import render_deck
from .service_models import (
    ArtifactKind,
    BeautifyAnalysis,
    JobOperation,
    JobResource,
    JobStatus,
    PptxPatchPlan,
    PptxPatchReview,
    ProductionInstruction,
)
from .template_apply import TemplateApplyError, import_template_layout
from .template_catalog import TemplateCatalog, TemplateCatalogError


class OperationCancelled(RuntimeError):
    pass


class OperationExecutionError(RuntimeError):
    pass


class OperationQualityError(OperationExecutionError):
    """A job failed its quality gate but has a downloadable best candidate."""

    def __init__(self, message: str, result: "OperationResult") -> None:
        super().__init__(message)
        self.result = result


class CandidatePlanError(PptxAgentError):
    def __init__(self, message: str, design_results: list[tuple[int, Any]]) -> None:
        super().__init__(message)
        self.design_results = design_results


@dataclass
class ProducedArtifact:
    path: Path
    kind: ArtifactKind
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_artifact_id: str | None = None


@dataclass
class OperationResult:
    artifacts: list[ProducedArtifact]
    best_path: Path | None = None
    plan_path: Path | None = None
    source_sha256: str | None = None


ProgressCallback = Callable[[float, str, dict[str, Any] | None], None]


class OperationExecutor:
    def __init__(
        self,
        store: JobStore,
        *,
        model: str = "gpt-5.5",
        slide_concurrency: int = 2,
        template_match_threshold: float = 0.75,
        default_max_cost_usd: float = 5.0,
        default_timeout_seconds: int = 900,
    ) -> None:
        self.store = store
        self.model = model
        self.slide_concurrency = max(1, int(slide_concurrency))
        self.template_match_threshold = max(
            0.0, min(1.0, float(template_match_threshold))
        )
        self.default_max_cost_usd = max(0.01, float(default_max_cost_usd))
        self.default_timeout_seconds = max(60, int(default_timeout_seconds))
        self.catalog = TemplateCatalog(store.root / "template-catalog")

    def execute(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        self._cancel_guard(job.id)
        handlers = {
            JobOperation.IMAGE_TO_EDITABLE: self._image_to_editable,
            JobOperation.NOTES: self._notes,
            JobOperation.BEAUTIFY: self._beautify,
            JobOperation.FIGURE_TO_EDITABLE: self._figure_to_editable,
            JobOperation.RENDER: self._render,
            JobOperation.VALIDATE: self._validate,
            JobOperation.TEMPLATE_IMPORT: self._template_import,
            JobOperation.TEMPLATE_REINDEX: self._template_reindex,
        }
        handler = handlers.get(job.operation)
        if handler is None:
            raise OperationExecutionError(
                f"operation {job.operation.value} is not wired into the worker yet"
            )
        return handler(job, progress)

    def _notes(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        return self._pptx_edit(job, progress, operation="notes")

    def _beautify(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        if job.request.get("plan_artifact_id"):
            return self._pptx_edit(job, progress, operation="beautify")
        return self._beautify_quality(job, progress)

    def _beautify_quality(
        self,
        job: JobResource,
        progress: ProgressCallback,
    ) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        if source.suffix.lower() != ".pptx":
            raise OperationExecutionError("beautify input must be a .pptx file")
        source_validation = validate_ooxml(
            source, font_policy=job.request.get("font_policy", "portable")
        )
        if not source_validation["compatible"]:
            raise OperationExecutionError(
                "source presentation failed OOXML validation: "
                + "; ".join(source_validation.get("errors", []))
            )
        output_dir = self.store.job_dir(job.id) / "artifacts"
        workdir = self.store.job_dir(job.id) / "work"
        output_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        timeout = int(job.request.get("timeout_seconds", self.default_timeout_seconds))
        font_policy = job.request.get("font_policy", "portable")
        body_minimum = float(job.request.get("body_min_font_size_pt", 8.0))
        source_minimum = float(job.request.get("source_min_font_size_pt", 6.0))
        max_candidates = max(1, min(int(job.request.get("max_candidates", 3)), 3))
        trace_level = str(job.request.get("trace_level", "metadata"))
        selected_slides = _selected(
            job.request.get("slides"), _pptx_slide_count(source)
        )
        source_sha = sha256_file(source)

        progress(0.04, "capturing immutable content manifest", None)
        invariant_manifest = create_invariant_manifest(source)
        manifest_path = write_json(
            output_dir / "beautify.content-manifest.json", invariant_manifest
        )
        source_text_manifest = extract_text_manifest(source)
        unsupported = has_unsupported_rebuild_objects(source)
        source_rendered = render_all_slides(
            source, workdir / "source-render", timeout_seconds=timeout
        )
        source_images = _select_slide_images(source_rendered, selected_slides)
        source_image_by_slide = dict(zip(selected_slides, source_images))

        progress(0.10, "matching template catalog", None)
        template_artifact_id = job.request.get("template_artifact_id")
        forced_family_id: str | None = None
        if template_artifact_id:
            template_path = self.store.artifact_path(template_artifact_id)
            if template_path.suffix.lower() != ".pptx":
                raise OperationExecutionError("uploaded beautify template must be a .pptx")
            template_rendered = render_all_slides(
                template_path,
                workdir / "uploaded-template-render",
                timeout_seconds=timeout,
            )
            template_resource, _ = self.catalog.import_deck(
                template_path,
                preview_paths=[item for item in template_rendered if item.suffix == ".png"],
                name=template_path.stem,
            )
            forced_family_id = template_resource.family_id
        catalog_enabled = bool(job.request.get("catalog_enabled", True))
        matches = (
            self.catalog.match_deck(
                source,
                top_k=5,
                forced_family_id=forced_family_id,
            )
            if catalog_enabled or forced_family_id
            else []
        )
        matches = [item for item in matches if item.source_slide_index in selected_slides]
        match_threshold = float(
            job.request.get("template_match_threshold", self.template_match_threshold)
        )
        selected_matches = _ranked_template_matches(
            matches,
            threshold=match_threshold,
            force=forced_family_id is not None,
            rank=0,
        )
        reference_images: list[Path] = []
        for match in selected_matches:
            preview = self.catalog.preview_path(match.template_slide_id)
            if preview and preview not in reference_images:
                reference_images.append(preview)
        for target_artifact_id in job.request.get("target_artifact_ids", []):
            target_path = self.store.artifact_path(target_artifact_id)
            if target_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
                raise OperationExecutionError(
                    "beautify target references must be PNG, JPEG, WebP, or SVG images"
                )
            reference_images.append(target_path)
        match_path = write_json(
            output_dir / "beautify.template-match.json",
            {
                "threshold": match_threshold,
                "forced_family_id": forced_family_id,
                "matches": [item.model_dump(mode="json") for item in matches],
                "selected": [item.model_dump(mode="json") for item in selected_matches],
            },
        )

        runtime = AgentsSdkRuntime(
            model=self.model,
            job_id=job.id,
            trace_level=trace_level,
            max_cost_usd=float(
                job.request.get("max_cost_usd", self.default_max_cost_usd)
            ),
            timeout_seconds=timeout,
            cancellation_check=lambda: self._cancel_guard(job.id),
        )
        analysis_payload = {
            "source_sha256": source_sha,
            "selected_slides": selected_slides,
            "requested_restyle_mode": job.request.get("restyle_mode", "auto"),
            "instruction": job.request.get("instruction"),
            "template_match_threshold": match_threshold,
            "forced_family_id": forced_family_id,
            "unsupported_rebuild_objects": unsupported,
            "template_matches": [item.model_dump(mode="json") for item in matches],
            "shape_inventory": [
                item.model_dump(mode="json")
                for item in extract_shape_graph(source)
                if item.slide_index in selected_slides
            ],
            "priority_order": [
                "invariants_and_powerpoint",
                "user_instruction",
                "uploaded_template",
                "target_images",
                "catalog",
            ],
        }
        progress(0.15, "running AnalystAgent", None)
        analysis_result = runtime.analyze(
            analysis_payload,
            image_paths=[*source_images, *reference_images],
        )
        analysis = analysis_result.output
        if not isinstance(analysis, BeautifyAnalysis):
            raise OperationExecutionError("AnalystAgent returned an unexpected output type")
        requested_mode = str(job.request.get("restyle_mode", "auto"))
        if requested_mode not in {"auto", "conservative", "structural", "rebuild"}:
            raise OperationExecutionError(f"invalid restyle_mode: {requested_mode}")
        resolved_mode = analysis.restyle_mode
        if requested_mode != "auto":
            resolved_mode = requested_mode
        if resolved_mode == "rebuild" and unsupported:
            resolved_mode = "structural"
        selected_matches = _gpt_reranked_template_matches(
            matches,
            analysis,
            threshold=match_threshold,
            forced_family_id=forced_family_id,
            fallback=selected_matches,
        )
        resolved_family = forced_family_id or (
            selected_matches[0].family_id if selected_matches else None
        )
        resolved_layouts = {
            item.source_slide_index: item.template_slide_id for item in selected_matches
        }
        analysis = analysis.model_copy(
            update={
                "source_sha256": source_sha,
                "restyle_mode": resolved_mode,
                "selected_family_id": resolved_family,
                "selected_layouts": resolved_layouts,
                "template_matches": matches,
                "fallback_reasons": list(
                    dict.fromkeys(
                        [
                            *analysis.fallback_reasons,
                            *(unsupported if requested_mode == "rebuild" or resolved_mode != "rebuild" else []),
                            *([] if selected_matches else ["no_template_above_threshold"]),
                        ]
                    )
                ),
                "confidence": (
                    max((item.confidence for item in selected_matches), default=analysis.confidence)
                ),
            }
        )
        analysis_path = write_json(output_dir / "beautify.analysis.json", analysis)
        self.store.add_event(
            job.id,
            "template.selected",
            {
                "family_id": resolved_family,
                "layouts": resolved_layouts,
                "threshold": match_threshold,
                "forced": forced_family_id is not None,
            },
        )
        self.store.add_event(
            job.id,
            "agent.completed",
            {
                "agent": "AnalystAgent",
                "trace_id": analysis_result.trace.trace_id,
                "cost_usd": analysis_result.trace.estimated_cost_usd,
            },
        )
        trace_records = [analysis_result.trace]

        artifacts: list[ProducedArtifact] = [
            ProducedArtifact(
                manifest_path, ArtifactKind.MANIFEST, parent_artifact_id=artifact_id
            ),
            ProducedArtifact(
                match_path, ArtifactKind.TEMPLATE_MATCH, parent_artifact_id=artifact_id
            ),
            ProducedArtifact(
                analysis_path, ArtifactKind.PLAN, parent_artifact_id=artifact_id
            ),
        ]
        candidate_scores: list[dict[str, Any]] = []
        candidate_records: list[tuple[Path, PptxPatchReview, bool, dict[str, Any]]] = []
        candidate_limit = 1 if job.mode == "plan" else max_candidates
        plan_paths: list[Path] = []
        for candidate_index in range(1, candidate_limit + 1):
            self._cancel_guard(job.id)
            strategy = _candidate_strategy(
                requested_mode=requested_mode,
                resolved_mode=resolved_mode,
                candidate_index=candidate_index,
                rebuild_blocked=bool(unsupported),
            )
            ranked_matches = _ranked_template_matches(
                matches,
                threshold=match_threshold,
                force=forced_family_id is not None,
                rank=candidate_index - 1,
            )
            if candidate_index == 1 and selected_matches:
                ranked_matches = selected_matches
            candidate_base = source
            if job.mode != "plan" and strategy["mode"] == "rebuild":
                rebuild_output = workdir / f"candidate-{candidate_index}-native-rebuild.pptx"
                rebuild_report = output_dir / f"beautify.candidate-{candidate_index}.rebuild.json"
                try:
                    candidate_base = native_rebuild_deck(
                        source,
                        rebuild_output,
                        report_path=rebuild_report,
                        minimum_font_size_pt=body_minimum,
                        timeout_seconds=timeout,
                    )
                    artifacts.append(
                        ProducedArtifact(
                            rebuild_report,
                            ArtifactKind.REPORT,
                            parent_artifact_id=artifact_id,
                            metadata={
                                "candidate_index": candidate_index,
                                "stage": "native_rebuild",
                            },
                        )
                    )
                except NativeRebuildError as error:
                    strategy = {
                        "mode": "structural",
                        "variation": "native_rebuild_fallback",
                    }
                    self.store.add_event(
                        job.id,
                        "candidate.strategy_fallback",
                        {
                            "candidate_index": candidate_index,
                            "from": "rebuild",
                            "to": "structural",
                            "message": str(error),
                        },
                    )
            if job.mode != "plan" and ranked_matches:
                candidate_base = self._apply_candidate_layouts(
                    source=candidate_base,
                    matches=ranked_matches,
                    output_dir=workdir / f"candidate-{candidate_index}-template",
                    job_id=job.id,
                )
            candidate_image_by_slide = source_image_by_slide
            if candidate_base.resolve() != source.resolve():
                base_rendered = render_all_slides(
                    candidate_base,
                    workdir / f"candidate-{candidate_index}-base-render",
                    timeout_seconds=timeout,
                )
                candidate_image_by_slide = dict(
                    zip(
                        selected_slides,
                        _select_slide_images(base_rendered, selected_slides),
                    )
                )
            candidate_source_sha = sha256_file(candidate_base)
            candidate_references = []
            for match in ranked_matches:
                preview = self.catalog.preview_path(match.template_slide_id)
                if preview and preview not in candidate_references:
                    candidate_references.append(preview)
            if not candidate_references:
                candidate_references = reference_images
            plan_payload = {
                "operation": "beautify",
                "source_sha256": candidate_source_sha,
                "selected_slides": selected_slides,
                "analysis": analysis.model_dump(mode="json"),
                "candidate_index": candidate_index,
                "candidate_strategy": strategy,
                "body_minimum_font_size_pt": body_minimum,
                "source_minimum_font_size_pt": source_minimum,
                "instruction": job.request.get("instruction"),
                "selected_template_matches": [
                    item.model_dump(mode="json") for item in ranked_matches
                ],
                "shape_inventory": [
                    item.model_dump(mode="json")
                    for item in extract_shape_graph(candidate_base)
                    if item.slide_index in selected_slides
                ],
            }
            progress(
                0.20 + (candidate_index - 1) * 0.22,
                f"running DesignerAgent candidate {candidate_index}/{candidate_limit}",
                {"candidate_index": candidate_index, "strategy": strategy},
            )
            try:
                plan, design_results = self._build_candidate_plan(
                    runtime=runtime,
                    plan_payload=plan_payload,
                    source_image_by_slide=candidate_image_by_slide,
                    reference_images=candidate_references,
                    candidate_index=candidate_index,
                    selected_slides=selected_slides,
                    candidate_base=candidate_base,
                    candidate_source_sha=candidate_source_sha,
                    strategy_mode=strategy["mode"],
                    body_minimum=body_minimum,
                )
            except (PptxAgentError, ValueError) as error:
                for slide_index, design_result in getattr(error, "design_results", []):
                    trace_records.append(design_result.trace)
                    self.store.add_event(
                        job.id,
                        "agent.completed",
                        {
                            "agent": "DesignerAgent",
                            "candidate_index": candidate_index,
                            "slide_index": slide_index,
                            "trace_id": design_result.trace.trace_id,
                            "cost_usd": design_result.trace.estimated_cost_usd,
                            "plan_valid": False,
                        },
                    )
                scorecard = self._record_candidate_execution_failure(
                    job_id=job.id,
                    output_dir=output_dir,
                    artifact_id=artifact_id,
                    artifacts=artifacts,
                    candidate_index=candidate_index,
                    strategy=strategy,
                    stage="plan_validation",
                    error=error,
                )
                candidate_scores.append(scorecard)
                continue
            for slide_index, design_result in design_results:
                trace_records.append(design_result.trace)
                self.store.add_event(
                    job.id,
                    "agent.completed",
                    {
                        "agent": "DesignerAgent",
                        "candidate_index": candidate_index,
                        "slide_index": slide_index,
                        "trace_id": design_result.trace.trace_id,
                        "cost_usd": design_result.trace.estimated_cost_usd,
                    },
                )
            plan_path = write_json(
                output_dir / f"beautify.candidate-{candidate_index}.plan.json", plan
            )
            plan_paths.append(plan_path)
            artifacts.append(
                ProducedArtifact(
                    plan_path,
                    ArtifactKind.PLAN,
                    parent_artifact_id=artifact_id,
                    metadata={"candidate_index": candidate_index},
                )
            )
            candidate_root = workdir if job.mode == "plan" else output_dir
            candidate = candidate_root / f"beautify.candidate-{candidate_index}.pptx"
            try:
                apply_ooxml_patch(candidate_base, candidate, plan)
            except OoxmlEditError as error:
                scorecard = self._record_candidate_execution_failure(
                    job_id=job.id,
                    output_dir=output_dir,
                    artifact_id=artifact_id,
                    artifacts=artifacts,
                    candidate_index=candidate_index,
                    strategy=strategy,
                    stage="deterministic_executor",
                    error=error,
                )
                candidate_scores.append(scorecard)
                continue
            invariant_report = verify_invariants(invariant_manifest, candidate)
            font_report = font_size_report(
                candidate,
                body_minimum_pt=body_minimum,
                source_minimum_pt=source_minimum,
            )
            deterministic = deterministic_checks(
                source_manifest=source_text_manifest,
                candidate_path=candidate,
                instructions=[],
                font_policy=font_policy,
                source_path=candidate_base,
                plan=plan,
            )
            powerpoint_report = validate_powerpoint(
                candidate,
                mode=job.request.get("powerpoint_validation", "auto"),
                font_policy=font_policy,
                workspace=workdir / f"candidate-{candidate_index}-powerpoint",
                timeout_seconds=timeout,
            )
            audit = audit_pptx(candidate)
            deterministic.update(
                {
                    "invariants_passed": invariant_report.passed,
                    "invariant_report": invariant_report.model_dump(mode="json"),
                    "font_sizes_passed": bool(font_report["passed"]),
                    "font_size_report": font_report,
                    "powerpoint_compatible": bool(powerpoint_report.get("compatible")),
                    "powerpoint": powerpoint_report,
                    "layout_passed": int(audit.get("canvas_overflow_count", 0)) == 0,
                    "editability_passed": not bool(audit.get("flattened_slide", False)),
                    "audit": audit,
                    "selected_slides": selected_slides,
                }
            )
            hard_gate_passed = all(
                (
                    bool(deterministic.get("ooxml_compatible")),
                    bool(deterministic.get("invariants_passed")),
                    bool(deterministic.get("font_sizes_passed")),
                    bool(deterministic.get("powerpoint_compatible")),
                    bool(deterministic.get("layout_passed")),
                    bool(deterministic.get("editability_passed")),
                    bool(deterministic.get("operation_checks_passed")),
                )
            )
            candidate_rendered = render_all_slides(
                candidate,
                workdir / f"candidate-{candidate_index}-render",
                timeout_seconds=timeout,
            )
            candidate_images = _select_slide_images(candidate_rendered, selected_slides)
            for slide_index, preview in zip(selected_slides, candidate_images):
                preview_copy = output_dir / (
                    f"beautify.candidate-{candidate_index}.slide-{slide_index}.png"
                )
                shutil.copy2(preview, preview_copy)
                artifacts.append(
                    ProducedArtifact(
                        preview_copy,
                        ArtifactKind.PREVIEW,
                        parent_artifact_id=artifact_id,
                        metadata={
                            "candidate_index": candidate_index,
                            "slide_index": slide_index,
                        },
                    )
                )
            if hard_gate_passed:
                progress(
                    0.34 + candidate_index * 0.20,
                    f"running ReviewerAgent candidate {candidate_index}/{candidate_limit}",
                    {"candidate_index": candidate_index},
                )
                review_payload = {
                    "candidate_index": candidate_index,
                    "candidate_strategy": strategy,
                    "selected_slides": selected_slides,
                    "deterministic_hard_gates": deterministic,
                    "image_order": "source slides, candidate slides, template or target references",
                }
                review_result = runtime.review(
                    review_payload,
                    image_paths=[*source_images, *candidate_images, *candidate_references],
                    candidate_index=candidate_index,
                )
                review = review_result.output
                assert isinstance(review, PptxPatchReview)
                trace_records.append(review_result.trace)
                self.store.add_event(
                    job.id,
                    "agent.completed",
                    {
                        "agent": "ReviewerAgent",
                        "candidate_index": candidate_index,
                        "trace_id": review_result.trace.trace_id,
                        "score": review.score,
                        "cost_usd": review_result.trace.estimated_cost_usd,
                    },
                )
                if 7.5 <= review.score <= 8.5:
                    second_result = runtime.review(
                        {**review_payload, "near_threshold_second_vote": True},
                        image_paths=[*source_images, *candidate_images, *candidate_references],
                        candidate_index=candidate_index,
                    )
                    second = second_result.output
                    assert isinstance(second, PptxPatchReview)
                    review = _average_reviews(review, second)
                    trace_records.append(second_result.trace)
                    self.store.add_event(
                        job.id,
                        "agent.completed",
                        {
                            "agent": "ReviewerAgent",
                            "candidate_index": candidate_index,
                            "vote": 2,
                            "trace_id": second_result.trace.trace_id,
                            "score": second.score,
                            "cost_usd": second_result.trace.estimated_cost_usd,
                        },
                    )
            else:
                issues = [
                    item["message"]
                    for item in invariant_report.model_dump(mode="json")["violations"]
                ]
                if not font_report["passed"]:
                    issues.append("minimum font-size policy failed")
                if not powerpoint_report.get("compatible"):
                    issues.append("PowerPoint compatibility validation failed")
                review = PptxPatchReview(
                    instruction_fulfilled=False,
                    content_preserved=invariant_report.passed,
                    production_notes_removed=True,
                    layout_valid=bool(deterministic.get("layout_passed")),
                    editability_preserved=bool(deterministic.get("editability_passed")),
                    balanced_density=False,
                    issues=issues[:32],
                    repair_instruction="Resolve deterministic hard-gate failures before visual review.",
                    score=0,
                    rubric_scores={},
                    slide_scores={slide_index: 0 for slide_index in selected_slides},
                    relative_improvement=None,
                )
            approved = hard_gate_passed and review_approved(review, deterministic)
            scorecard = {
                "candidate_index": candidate_index,
                "strategy": strategy,
                "hard_gate_passed": hard_gate_passed,
                "approved": approved,
                "deterministic": deterministic,
                "review": review.model_dump(mode="json"),
                "candidate": candidate.name,
            }
            scorecard_path = write_json(
                output_dir / f"beautify.candidate-{candidate_index}.scorecard.json",
                scorecard,
            )
            artifacts.append(
                ProducedArtifact(
                    scorecard_path,
                    ArtifactKind.SCORECARD,
                    parent_artifact_id=artifact_id,
                    metadata={
                        "candidate_index": candidate_index,
                        "hard_gate_passed": hard_gate_passed,
                        "approved": approved,
                        "reviewer_score": review.score,
                    },
                )
            )
            if hard_gate_passed and job.mode != "plan":
                artifacts.append(
                    ProducedArtifact(
                        candidate,
                        ArtifactKind.PPTX,
                        parent_artifact_id=artifact_id,
                        metadata={
                            "candidate_index": candidate_index,
                            "approved": approved,
                            "reviewer_score": review.score,
                            "diagnostic_candidate": True,
                        },
                    )
                )
                candidate_records.append((candidate, review, approved, scorecard))
            candidate_scores.append(scorecard)
            self.store.add_event(
                job.id,
                "candidate.decided",
                {
                    "candidate_index": candidate_index,
                    "hard_gate_passed": hard_gate_passed,
                    "approved": approved,
                    "reviewer_score": review.score,
                    "cumulative_cost_usd": round(runtime.budget.spent_usd, 6),
                },
            )
            budget_pressure = (
                runtime.budget.maximum_usd > 0
                and runtime.budget.spent_usd
                >= runtime.budget.maximum_usd * 0.8
            )
            if approved and (review.score >= 9.0 or budget_pressure):
                self.store.add_event(
                    job.id,
                    "candidate.early_accept",
                    {
                        "candidate_index": candidate_index,
                        "reviewer_score": review.score,
                        "reason": (
                            "high_confidence" if review.score >= 9.0 else "budget_pressure"
                        ),
                    },
                )
                break

        if trace_level != "none":
            trace_path = write_json(
                output_dir / "beautify.trace.json",
                {
                    "trace_level": trace_level,
                    "model": self.model,
                    "estimated_cost_usd": round(runtime.budget.spent_usd, 6),
                    "records": [item.model_dump(mode="json") for item in trace_records],
                },
            )
            artifacts.append(
                ProducedArtifact(
                    trace_path, ArtifactKind.TRACE, parent_artifact_id=artifact_id
                )
            )
        report_path = write_json(
            output_dir / "beautify.report.json",
            {
                "operation": "beautify",
                "model": self.model,
                "source_sha256": source_sha,
                "selected_slides": selected_slides,
                "analysis": analysis.model_dump(mode="json"),
                "candidate_scorecards": candidate_scores,
                "estimated_cost_usd": round(runtime.budget.spent_usd, 6),
            },
        )
        artifacts.append(
            ProducedArtifact(report_path, ArtifactKind.REPORT, parent_artifact_id=artifact_id)
        )
        if job.mode == "plan":
            return OperationResult(
                artifacts=artifacts,
                plan_path=plan_paths[0] if plan_paths else analysis_path,
                source_sha256=source_sha,
            )
        if not candidate_records:
            result = OperationResult(artifacts=artifacts, source_sha256=source_sha)
            raise OperationQualityError(
                "all beautify candidates failed deterministic hard gates", result
            )
        best_candidate, best_review, best_approved, _ = max(
            candidate_records,
            key=lambda item: item[1].score,
        )
        final = output_dir / "beautify.pptx"
        shutil.copy2(best_candidate, final)
        artifacts.append(
            ProducedArtifact(
                final,
                ArtifactKind.PPTX,
                parent_artifact_id=artifact_id,
                metadata={
                    "final": True,
                    "approved": best_approved,
                    "reviewer_score": best_review.score,
                },
            )
        )
        result = OperationResult(
            artifacts=artifacts,
            best_path=final,
            plan_path=plan_paths[0] if plan_paths else None,
            source_sha256=source_sha,
        )
        if not best_approved:
            raise OperationQualityError(
                f"no invariant-safe beautify candidate reached reviewer score 8 after {candidate_limit} candidates",
                result,
            )
        return result

    def _design_candidate_plans(
        self,
        *,
        runtime: AgentsSdkRuntime,
        plan_payload: dict[str, Any],
        source_image_by_slide: dict[int, Path],
        reference_images: list[Path],
        candidate_index: int,
        selected_slides: list[int],
    ) -> list[tuple[int, Any]]:
        """Run independent per-slide DesignerAgent calls with a local limit of two."""

        inventory = list(plan_payload.get("shape_inventory", []))

        def design_one(slide_index: int) -> tuple[int, Any]:
            self._cancel_guard(runtime.job_id)
            payload = {
                **plan_payload,
                "selected_slides": [slide_index],
                "shape_inventory": [
                    item for item in inventory if int(item.get("slide_index", 0)) == slide_index
                ],
            }
            images = [source_image_by_slide[slide_index], *reference_images]
            return (
                slide_index,
                runtime.design(
                    payload,
                    image_paths=images,
                    candidate_index=candidate_index,
                ),
            )

        if len(selected_slides) <= 1 or self.slide_concurrency <= 1:
            return [design_one(slide_index) for slide_index in selected_slides]
        completed: dict[int, Any] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.slide_concurrency, len(selected_slides))
        ) as pool:
            futures = {pool.submit(design_one, slide): slide for slide in selected_slides}
            for future in as_completed(futures):
                slide_index, result = future.result()
                completed[slide_index] = result
        return [(slide, completed[slide]) for slide in selected_slides]

    def _build_candidate_plan(
        self,
        *,
        runtime: AgentsSdkRuntime,
        plan_payload: dict[str, Any],
        source_image_by_slide: dict[int, Path],
        reference_images: list[Path],
        candidate_index: int,
        selected_slides: list[int],
        candidate_base: Path,
        candidate_source_sha: str,
        strategy_mode: str,
        body_minimum: float,
    ) -> tuple[PptxPatchPlan, list[tuple[int, Any]]]:
        design_results = self._design_candidate_plans(
            runtime=runtime,
            plan_payload=plan_payload,
            source_image_by_slide=source_image_by_slide,
            reference_images=reference_images,
            candidate_index=candidate_index,
            selected_slides=selected_slides,
        )
        try:
            partial_plans: list[PptxPatchPlan] = []
            for slide_index, design_result in design_results:
                partial = design_result.output
                if not isinstance(partial, PptxPatchPlan):
                    raise PptxAgentError(
                        "DesignerAgent returned an unexpected output type"
                    )
                validate_patch_plan(partial, candidate_base, [slide_index])
                partial_plans.append(partial)
            plan = _merge_patch_plans(
                partial_plans,
                source_sha256=candidate_source_sha,
                strategy_mode=strategy_mode,
                minimum_font_size_pt=body_minimum,
            )
            analyst_protected = list(
                plan_payload.get("analysis", {}).get("protected_shape_ids", [])
            )
            plan = plan.model_copy(
                update={
                    "protected_shape_ids": list(
                        dict.fromkeys(
                            [*plan.protected_shape_ids, *analyst_protected]
                        )
                    )
                }
            )
            validate_patch_plan(plan, candidate_base, selected_slides)
        except (PptxAgentError, ValueError) as error:
            raise CandidatePlanError(str(error), design_results) from error
        return plan, design_results

    def _record_candidate_execution_failure(
        self,
        *,
        job_id: str,
        output_dir: Path,
        artifact_id: str,
        artifacts: list[ProducedArtifact],
        candidate_index: int,
        strategy: dict[str, str],
        stage: str,
        error: Exception,
    ) -> dict[str, Any]:
        scorecard = {
            "candidate_index": candidate_index,
            "strategy": strategy,
            "hard_gate_passed": False,
            "approved": False,
            "deterministic": {
                "stage": stage,
                "operation_checks_passed": False,
            },
            "review": {
                "score": 0,
                "issues": [str(error)],
                "repair_instruction": "Generate a valid deterministic operation plan.",
            },
            "candidate": None,
        }
        scorecard_path = write_json(
            output_dir / f"beautify.candidate-{candidate_index}.scorecard.json",
            scorecard,
        )
        artifacts.append(
            ProducedArtifact(
                scorecard_path,
                ArtifactKind.SCORECARD,
                parent_artifact_id=artifact_id,
                metadata={
                    "candidate_index": candidate_index,
                    "hard_gate_passed": False,
                    "approved": False,
                    "reviewer_score": 0,
                    "failure_stage": stage,
                },
            )
        )
        self.store.add_event(
            job_id,
            "candidate.rejected",
            {
                "candidate_index": candidate_index,
                "stage": stage,
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )
        return scorecard

    def _apply_candidate_layouts(
        self,
        *,
        source: Path,
        matches: list[Any],
        output_dir: Path,
        job_id: str,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        current = source
        grouped: dict[tuple[str, int], list[int]] = {}
        for match in matches:
            grouped.setdefault((match.template_id, match.slide_index), []).append(
                match.source_slide_index
            )
        for index, ((template_id, template_slide_index), source_slides) in enumerate(
            grouped.items(), start=1
        ):
            target = output_dir / f"layout-{index}.pptx"
            try:
                import_template_layout(
                    current,
                    self.catalog.template_path(template_id),
                    target,
                    template_slide_index=template_slide_index,
                    source_slide_indices=source_slides,
                )
                current = target
            except (TemplateApplyError, TemplateCatalogError) as error:
                self.store.add_event(
                    job_id,
                    "template.layout_fallback",
                    {
                        "template_id": template_id,
                        "template_slide_index": template_slide_index,
                        "source_slides": source_slides,
                        "message": str(error),
                    },
                )
                return source
        return current


    def _pptx_edit(
        self,
        job: JobResource,
        progress: ProgressCallback,
        *,
        operation: str,
    ) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        if source.suffix.lower() != ".pptx":
            raise OperationExecutionError(f"{operation} input must be a .pptx file")
        source_validation = validate_ooxml(source, font_policy=job.request.get("font_policy", "portable"))
        if not source_validation["compatible"]:
            raise OperationExecutionError(
                "source presentation failed OOXML validation: "
                + "; ".join(source_validation.get("errors", []))
            )
        output_dir = self.store.job_dir(job.id) / "artifacts"
        workdir = self.store.job_dir(job.id) / "work"
        output_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        timeout = int(job.request.get("timeout_seconds", 360))
        font_policy = job.request.get("font_policy", "portable")
        minimum_font_size = float(job.request.get("minimum_font_size_pt", 7.5))
        source_manifest = extract_text_manifest(source)
        invariant_manifest = (
            create_invariant_manifest(source) if operation == "beautify" else None
        )
        slide_count = _pptx_slide_count(source)
        selected_slides = _selected(job.request.get("slides"), slide_count)

        progress(0.08, "rendering source deck", {"slides": selected_slides})
        source_rendered = render_all_slides(source, workdir / "source-render", timeout_seconds=timeout)
        source_images = _select_slide_images(source_rendered, selected_slides)
        extra_images: list[Path] = []
        template_present = False
        template_artifact_id = job.request.get("template_artifact_id")
        if template_artifact_id:
            template_path = self.store.artifact_path(template_artifact_id)
            if template_path.suffix.lower() == ".pptx":
                template_rendered = render_all_slides(
                    template_path, workdir / "template-render", timeout_seconds=timeout
                )
                extra_images.extend(path for path in template_rendered if path.suffix == ".png")
                template_present = True
            else:
                extra_images.append(template_path)
        for target_artifact_id in job.request.get("target_artifact_ids", []):
            extra_images.append(self.store.artifact_path(target_artifact_id))

        instructions = extract_production_instructions(
            source,
            api_instruction=job.request.get("instruction"),
            selected_slides=selected_slides,
        )
        if operation == "notes" and not instructions:
            raise OperationExecutionError(
                "no production instruction found in API text, comments, speaker notes, or visible callouts"
            )
        if operation == "beautify" and not instructions:
            instructions = [
                ProductionInstruction(
                    id=f"beautify-{slide_index}",
                    slide_index=slide_index,
                    source="api",
                    raw_text=(
                        job.request.get("instruction")
                        or "Improve hierarchy, alignment, spacing, consistency, and readability without changing business content."
                    ),
                    priority=100,
                )
                for slide_index in selected_slides
            ]

        supplied_plan_id = job.request.get("plan_artifact_id")
        if supplied_plan_id:
            plan = PptxPatchPlan.model_validate_json(
                self.store.artifact_path(supplied_plan_id).read_text(encoding="utf-8")
            )
            if plan.source_sha256 != sha256_file(source):
                raise OperationExecutionError("saved plan does not match the source deck")
        else:
            progress(0.2, "planning OOXML edits with GPT-5.5", None)
            plan = request_patch_plan(
                source_path=source,
                operation=operation,
                images=source_images,
                instructions=instructions,
                selected_slides=selected_slides,
                model=self.model,
                timeout_seconds=timeout,
                minimum_font_size_pt=minimum_font_size,
                style_instruction=job.request.get("instruction"),
                extra_images=extra_images,
                template_present=template_present,
            )
        plan = plan.model_copy(
            update={
                "instructions": instructions,
                "content_invariant": operation == "beautify",
                "cleanup_executed_instructions": operation == "notes",
            }
        )
        plan_path = write_json(output_dir / f"{operation}.plan.json", plan)
        if job.mode == "plan":
            return OperationResult(
                artifacts=[
                    ProducedArtifact(plan_path, ArtifactKind.PLAN, parent_artifact_id=artifact_id),
                ],
                plan_path=plan_path,
                source_sha256=sha256_file(source),
            )

        max_attempts = max(1, min(int(job.request.get("max_attempts", 3)), 5))
        current_source = source
        current_plan = plan
        best_candidate: Path | None = None
        best_score = -1.0
        attempts: list[dict[str, Any]] = []
        allowed_content_changes: set[str] = set()
        approved = False
        for attempt in range(1, max_attempts + 1):
            self._cancel_guard(job.id)
            candidate = output_dir / f"{operation}.candidate-{attempt}.pptx"
            progress(
                0.25 + (attempt - 1) * 0.2,
                f"applying OOXML patch {attempt}/{max_attempts}",
                {"attempt": attempt},
            )
            if operation == "notes":
                current_shapes = {
                    (item.slide_index, item.shape_id): item
                    for item in extract_shape_graph(current_source)
                }
                for patch_operation in current_plan.operations:
                    if patch_operation.action not in {"delete", "replace_text"}:
                        continue
                    target = current_shapes.get(
                        (patch_operation.slide_index, patch_operation.target_shape_id)
                    )
                    if target and target.text:
                        allowed_content_changes.add(target.text)
            apply_ooxml_patch(current_source, candidate, current_plan)
            checks = deterministic_checks(
                source_manifest=source_manifest,
                candidate_path=candidate,
                instructions=instructions if operation == "notes" else [],
                allowed_removed_texts=allowed_content_changes,
                font_policy=font_policy,
                source_path=current_source,
                plan=current_plan,
            )
            if invariant_manifest is not None:
                invariant_report = verify_invariants(invariant_manifest, candidate)
                font_report = font_size_report(
                    candidate,
                    body_minimum_pt=max(8.0, minimum_font_size),
                    source_minimum_pt=float(
                        job.request.get("source_min_font_size_pt", 6.0)
                    ),
                )
                powerpoint_report = validate_powerpoint(
                    candidate,
                    mode=job.request.get("powerpoint_validation", "auto"),
                    font_policy=font_policy,
                    workspace=workdir / f"candidate-{attempt}-powerpoint",
                    timeout_seconds=timeout,
                )
                audit = audit_pptx(candidate)
                checks.update(
                    {
                        "invariants_passed": invariant_report.passed,
                        "invariant_report": invariant_report.model_dump(mode="json"),
                        "font_sizes_passed": bool(font_report["passed"]),
                        "font_size_report": font_report,
                        "powerpoint_compatible": bool(
                            powerpoint_report.get("compatible")
                        ),
                        "powerpoint": powerpoint_report,
                        "layout_passed": int(
                            audit.get("canvas_overflow_count", 0)
                        )
                        == 0,
                        "editability_passed": not bool(
                            audit.get("flattened_slide", False)
                        ),
                        "audit": audit,
                    }
                )
            rendered = render_all_slides(
                candidate, workdir / f"candidate-{attempt}-render", timeout_seconds=timeout
            )
            candidate_images = _select_slide_images(rendered, selected_slides)
            progress(0.35 + attempt * 0.2, "reviewing rendered candidate", {"attempt": attempt})
            review = review_patch(
                operation=operation,
                source_images=source_images,
                candidate_images=candidate_images,
                instructions=instructions,
                deterministic=checks,
                model=self.model,
                timeout_seconds=timeout,
            )
            attempt_payload = {
                "attempt": attempt,
                "plan": current_plan.model_dump(mode="json"),
                "deterministic": checks,
                "review": review.model_dump(mode="json"),
                "candidate": candidate.name,
            }
            attempts.append(attempt_payload)
            write_json(output_dir / f"{operation}.attempt-{attempt}.review.json", attempt_payload)
            if review.score > best_score:
                best_score = review.score
                best_candidate = candidate
            if review_approved(review, checks):
                approved = True
                best_candidate = candidate
                break
            if attempt == max_attempts:
                break
            current_source = candidate
            current_plan = request_patch_plan(
                source_path=current_source,
                operation=operation,
                images=candidate_images,
                instructions=instructions,
                selected_slides=selected_slides,
                model=self.model,
                timeout_seconds=timeout,
                minimum_font_size_pt=minimum_font_size,
                style_instruction=job.request.get("instruction"),
                prior_review=review,
                extra_images=extra_images,
                template_present=template_present,
            ).model_copy(
                update={
                    "instructions": instructions,
                    "content_invariant": operation == "beautify",
                    "cleanup_executed_instructions": operation == "notes",
                }
            )

        assert best_candidate is not None
        final = output_dir / f"{operation}.pptx"
        shutil.copy2(best_candidate, final)
        report_path = write_json(
            output_dir / f"{operation}.report.json",
            {
                "operation": operation,
                "approved": approved,
                "model": self.model,
                "source_sha256": sha256_file(source),
                "best_score": best_score,
                "selected_slides": selected_slides,
                "instructions": [item.model_dump(mode="json") for item in instructions],
                "attempts": attempts,
            },
        )
        artifacts = [
            ProducedArtifact(final, ArtifactKind.PPTX, parent_artifact_id=artifact_id),
            ProducedArtifact(plan_path, ArtifactKind.PLAN, parent_artifact_id=artifact_id),
            ProducedArtifact(report_path, ArtifactKind.REPORT, parent_artifact_id=artifact_id),
        ]
        result = OperationResult(artifacts=artifacts, best_path=final)
        if not approved:
            raise OperationQualityError(
                f"{operation} candidate failed the quality gate after {max_attempts} attempts",
                result,
            )
        return result

    def _resolve_inputs(self, job: JobResource) -> list[tuple[str, Path]]:
        artifact_ids = list(job.request.get("input_artifact_ids") or [])
        if not artifact_ids and job.request.get("source_artifact_id"):
            artifact_ids = [job.request["source_artifact_id"]]
        if not artifact_ids:
            raise OperationExecutionError("job has no input artifact")
        resolved: list[tuple[str, Path]] = []
        for artifact_id in artifact_ids:
            artifact = self.store.get_artifact(artifact_id)
            resolved.append((artifact_id, self.store.artifact_path(artifact_id)))
        return resolved

    def _cancel_guard(self, job_id: str) -> None:
        if self.store.is_cancel_requested(job_id):
            raise OperationCancelled("job cancellation requested")

    def _image_to_editable(
        self, job: JobResource, progress: ProgressCallback
    ) -> OperationResult:
        inputs = self._resolve_inputs(job)
        workspace = self.store.job_dir(job.id) / "work"
        output_dir = self.store.job_dir(job.id) / "artifacts"
        workspace.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        specs: list[SlideSpec] = []
        assets_by_slide: list[dict[str, str]] = []
        slide_reports: list[dict[str, Any]] = []
        parent_ids: list[str] = []

        def reconstruct(index: int, artifact_id: str, source: Path):
            self._cancel_guard(job.id)
            slide_dir = workspace / f"slide-{index + 1:03d}"
            slide_dir.mkdir(parents=True, exist_ok=True)
            slide_pptx = slide_dir / "slide.pptx"
            slide_spec = slide_dir / "slide.spec.json"
            slide_report = slide_dir / "slide.report.json"
            arguments = argparse.Namespace(
                input=str(source),
                output=str(slide_pptx),
                model=self.model,
                quality_profile="max",
                iterations=int(job.request.get("iterations", 2)),
                raster_policy=job.request.get("raster_policy", "photos-only"),
                target_score=float(job.request.get("target_score", 0.93)),
                spec_in=None,
                spec_out=str(slide_spec),
                report=str(slide_report),
                workdir=str(slide_dir / "iterations"),
                timeout=int(job.request.get("timeout_seconds", 300)),
                max_output_tokens=int(job.request.get("max_output_tokens", 64000)),
                local_optimization=job.request.get("local_optimization", "on"),
                powerpoint_validation="off",
                font_policy=job.request.get("font_policy", "portable"),
            )
            report = convert_image(arguments)
            spec = SlideSpec.model_validate_json(slide_spec.read_text(encoding="utf-8"))
            # Crop raster assets before normalizing the slide canvas; source_region
            # coordinates are expressed in the original uploaded image.
            assets = extract_image_assets(spec, source, slide_dir / "assets-final")
            return index, artifact_id, spec, assets, report

        max_workers = max(1, min(int(job.request.get("parallel_slides", 4)), len(inputs)))
        results: dict[int, tuple[str, SlideSpec, dict[str, str], dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(reconstruct, index, artifact_id, source): index
                for index, (artifact_id, source) in enumerate(inputs)
            }
            completed = 0
            for future in as_completed(futures):
                index, artifact_id, spec, assets, report = future.result()
                results[index] = (artifact_id, spec, assets, report)
                completed += 1
                progress(
                    0.1 + 0.65 * (completed / len(inputs)),
                    f"reconstructed {completed}/{len(inputs)} slides",
                    {"slide_index": index + 1},
                )

        first_spec = results[0][1]
        for index in range(len(inputs)):
            artifact_id, spec, assets, report = results[index]
            if index:
                spec = _scale_spec_to_canvas(
                    spec,
                    first_spec.source_width,
                    first_spec.source_height,
                )
            specs.append(spec)
            assets_by_slide.append(assets)
            slide_reports.append(report)
            parent_ids.append(artifact_id)

        deck_spec = output_dir / "deck.spec.json"
        deck_spec.write_text(
            json.dumps(
                {"slides": [spec.model_dump(mode="json") for spec in specs]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if job.mode == "plan":
            plan = output_dir / "image-reconstruction.plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "operation": JobOperation.IMAGE_TO_EDITABLE.value,
                        "source_sha256": [sha256_file(path) for _, path in inputs],
                        "spec_artifact_name": deck_spec.name,
                        "slides": len(specs),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return OperationResult(
                artifacts=[
                    ProducedArtifact(deck_spec, ArtifactKind.SPEC),
                    ProducedArtifact(plan, ArtifactKind.PLAN),
                ],
                plan_path=plan,
                source_sha256=sha256_file(inputs[0][1]),
            )

        deck = output_dir / "editable-deck.pptx"
        render_deck(
            specs,
            deck,
            assets_by_slide=assets_by_slide,
            timeout_seconds=int(job.request.get("timeout_seconds", 300)),
        )
        audit = audit_pptx(deck)
        validation = validate_ooxml(deck, font_policy=job.request.get("font_policy", "portable"))
        if not validation["compatible"]:
            raise OperationExecutionError("generated deck failed OOXML validation")
        report_path = output_dir / "image-to-editable.report.json"
        report_path.write_text(
            json.dumps(
                {
                    "operation": JobOperation.IMAGE_TO_EDITABLE.value,
                    "slides": slide_reports,
                    "audit": audit,
                    "ooxml_validation": validation,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(0.9, "rendered editable deck", {"slides": len(specs)})
        return OperationResult(
            artifacts=[
                ProducedArtifact(deck, ArtifactKind.PPTX, parent_artifact_id=parent_ids[0]),
                ProducedArtifact(deck_spec, ArtifactKind.SPEC),
                ProducedArtifact(report_path, ArtifactKind.REPORT),
            ],
            best_path=deck,
        )

    def _figure_to_editable(
        self, job: JobResource, progress: ProgressCallback
    ) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        output_dir = self.store.job_dir(job.id) / "artifacts"
        workdir = self.store.job_dir(job.id) / "work"
        output_dir.mkdir(parents=True, exist_ok=True)
        width, height = job.request.get("canvas", [1280, 720])
        output = output_dir / "editable-figure.pptx"
        spec = output_dir / "editable-figure.shape.json"
        report = output_dir / "figure.report.json"
        preview = output_dir / "figure.preview.png"
        options = FigureConversionOptions(
            canvas_width=int(width),
            canvas_height=int(height),
            padding=float(job.request.get("padding", 48)),
            fit=job.request.get("fit", "contain"),
            background_color=job.request.get("background", "#FFFFFF"),
            max_colors=int(job.request.get("max_colors", 1)),
            strict=bool(job.request.get("strict", False)),
            timeout_seconds=int(job.request.get("timeout_seconds", 180)),
            refine_mode=job.request.get("refine", "none"),
            model=self.model,
            iterations=int(job.request.get("iterations", 2)),
            render_preview=True,
        )
        progress(0.1, "converting figure", None)
        convert_figure(
            source,
            output,
            options=options,
            spec_path=spec,
            report_path=report,
            preview_path=preview,
            workdir=workdir,
        )
        return OperationResult(
            artifacts=[
                ProducedArtifact(output, ArtifactKind.PPTX, parent_artifact_id=artifact_id),
                ProducedArtifact(spec, ArtifactKind.SPEC),
                ProducedArtifact(report, ArtifactKind.REPORT),
                ProducedArtifact(preview, ArtifactKind.PREVIEW),
            ],
            best_path=output,
        )

    def _render(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        if source.suffix.lower() != ".pptx":
            raise OperationExecutionError("render input must be a .pptx file")
        output_dir = self.store.job_dir(job.id) / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        progress(0.1, "rendering presentation", None)
        produced = render_all_slides(
            source,
            output_dir,
            timeout_seconds=int(job.request.get("timeout_seconds", 300)),
        )
        artifacts = [
            ProducedArtifact(path, ArtifactKind.PDF if path.suffix == ".pdf" else ArtifactKind.PREVIEW, parent_artifact_id=artifact_id)
            for path in produced
        ]
        return OperationResult(artifacts=artifacts)

    def _validate(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        if source.suffix.lower() != ".pptx":
            raise OperationExecutionError("validation input must be a .pptx file")
        output_dir = self.store.job_dir(job.id) / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        progress(0.2, "auditing OOXML package", None)
        font_policy = job.request.get("font_policy", "portable")
        report = {
            "operation": JobOperation.VALIDATE.value,
            "source_sha256": sha256_file(source),
            "audit": audit_pptx(source),
            "ooxml": validate_ooxml(source, font_policy=font_policy),
            "powerpoint": validate_powerpoint(
                source,
                mode=job.request.get("powerpoint_validation", "auto"),
                font_policy=font_policy,
                workspace=self.store.job_dir(job.id) / "work" / "powerpoint",
                timeout_seconds=int(job.request.get("timeout_seconds", 180)),
            ),
        }
        report_path = output_dir / "validation.report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not report["ooxml"]["compatible"]:
            raise OperationExecutionError("presentation failed OOXML validation")
        return OperationResult(
            artifacts=[ProducedArtifact(report_path, ArtifactKind.REPORT, parent_artifact_id=artifact_id)]
        )

    def _template_import(
        self,
        job: JobResource,
        progress: ProgressCallback,
    ) -> OperationResult:
        [(artifact_id, source)] = self._resolve_inputs(job)
        if source.suffix.lower() != ".pptx":
            raise OperationExecutionError("template import input must be a .pptx file")
        output_dir = self.store.job_dir(job.id) / "artifacts"
        workdir = self.store.job_dir(job.id) / "work"
        output_dir.mkdir(parents=True, exist_ok=True)
        progress(0.15, "rendering template deck", None)
        rendered = render_all_slides(
            source,
            workdir / "template-render",
            timeout_seconds=int(job.request.get("timeout_seconds", 900)),
        )
        previews = [item for item in rendered if item.suffix.lower() == ".png"]
        progress(0.65, "indexing template deck", {"slides": len(previews)})
        try:
            template, duplicate = self.catalog.import_deck(
                source,
                preview_paths=previews,
                name=job.request.get("name"),
            )
        except TemplateCatalogError as error:
            raise OperationExecutionError(str(error)) from error
        report = write_json(
            output_dir / "template-import.report.json",
            {
                "operation": JobOperation.TEMPLATE_IMPORT.value,
                "template": template.model_dump(mode="json"),
                "duplicate": duplicate,
                "index_version": template.index_version,
            },
        )
        return OperationResult(
            artifacts=[
                ProducedArtifact(
                    report,
                    ArtifactKind.REPORT,
                    parent_artifact_id=artifact_id,
                    metadata={"template_id": template.id, "duplicate": duplicate},
                )
            ],
        )

    def _template_reindex(
        self,
        job: JobResource,
        progress: ProgressCallback,
    ) -> OperationResult:
        output_dir = self.store.job_dir(job.id) / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        progress(0.2, "reindexing template catalog", None)
        result = self.catalog.reindex()
        report = write_json(
            output_dir / "template-reindex.report.json",
            {"operation": JobOperation.TEMPLATE_REINDEX.value, **result},
        )
        return OperationResult(
            artifacts=[ProducedArtifact(report, ArtifactKind.REPORT)]
        )


def _gpt_reranked_template_matches(
    matches: list[Any],
    analysis: BeautifyAnalysis,
    *,
    threshold: float,
    forced_family_id: str | None,
    fallback: list[Any],
) -> list[Any]:
    """Validate the Analyst's rerank against deterministic top-five candidates."""

    available_families = {item.family_id for item in matches}
    family_id = forced_family_id
    if family_id is None and analysis.selected_family_id in available_families:
        family_id = analysis.selected_family_id
    if family_id is None and fallback:
        family_id = fallback[0].family_id
    if family_id is None and matches:
        family_id = max(matches, key=lambda item: item.confidence).family_id
    fallback_by_slide = {item.source_slide_index: item for item in fallback}
    grouped: dict[int, list[Any]] = {}
    for item in matches:
        if item.family_id == family_id:
            grouped.setdefault(item.source_slide_index, []).append(item)
    selected: list[Any] = []
    for source_slide_index in sorted(grouped):
        candidates = sorted(
            grouped[source_slide_index], key=lambda item: item.confidence, reverse=True
        )
        eligible = (
            candidates
            if forced_family_id is not None
            else [item for item in candidates if item.confidence >= threshold]
        )
        if not eligible:
            continue
        desired_id = analysis.selected_layouts.get(source_slide_index)
        winner = next(
            (item for item in eligible if item.template_slide_id == desired_id),
            None,
        )
        if winner is None:
            prior = fallback_by_slide.get(source_slide_index)
            winner = next(
                (
                    item
                    for item in eligible
                    if prior is not None
                    and item.template_slide_id == prior.template_slide_id
                ),
                eligible[0],
            )
        selected.append(
            winner.model_copy(
                update={
                    "selected": True,
                    "rationale": (
                        "GPT-5.5 rerank of deterministic top-five"
                        if winner.template_slide_id == desired_id
                        else "deterministic fallback after invalid or absent GPT rerank"
                    ),
                }
            )
        )
    return selected


def _merge_patch_plans(
    plans: list[PptxPatchPlan],
    *,
    source_sha256: str,
    strategy_mode: str,
    minimum_font_size_pt: float,
) -> PptxPatchPlan:
    if not plans:
        raise OperationExecutionError("DesignerAgent produced no per-slide plans")
    instructions = []
    operations = []
    section_operations = []
    protected: list[str] = []
    summaries: list[str] = []
    for plan in plans:
        summaries.append(plan.instruction_summary)
        instructions.extend(plan.instructions)
        section_operations.extend(
            operation.model_copy(
                update={"op_id": f"section-{operation.op_id}"}
            )
            for operation in plan.section_operations
        )
        protected.extend(plan.protected_shape_ids)
        for operation in plan.operations:
            operations.append(
                operation.model_copy(
                    update={"op_id": f"s{operation.slide_index}-{operation.op_id}"}
                )
            )
    return PptxPatchPlan(
        operation="beautify",
        source_sha256=source_sha256,
        instruction_summary=" | ".join(dict.fromkeys(summaries)),
        instructions=instructions,
        operations=operations,
        section_operations=section_operations,
        protected_shape_ids=list(dict.fromkeys(protected)),
        content_invariant=True,
        cleanup_executed_instructions=False,
        layout_strategy={
            "conservative": "preserve",
            "structural": "global_reflow",
            "rebuild": "rebuild",
        }[strategy_mode],
        minimum_font_size_pt=minimum_font_size_pt,
    )


def _ranked_template_matches(
    matches: list[Any],
    *,
    threshold: float,
    force: bool,
    rank: int,
) -> list[Any]:
    grouped: dict[int, list[Any]] = {}
    for match in matches:
        grouped.setdefault(match.source_slide_index, []).append(match)
    selected = []
    for source_slide_index in sorted(grouped):
        candidates = sorted(
            grouped[source_slide_index], key=lambda item: item.confidence, reverse=True
        )
        eligible = (
            candidates
            if force
            else [item for item in candidates if item.confidence >= threshold]
        )
        if not eligible:
            continue
        selected.append(eligible[min(rank, len(eligible) - 1)])
    return selected


def _candidate_strategy(
    *,
    requested_mode: str,
    resolved_mode: str,
    candidate_index: int,
    rebuild_blocked: bool,
) -> dict[str, str]:
    variations = ("hierarchy_first", "grid_first", "density_first")
    if requested_mode == "auto":
        modes = ["conservative", "structural", "rebuild"]
        mode = modes[min(candidate_index - 1, len(modes) - 1)]
        if mode == "rebuild" and rebuild_blocked:
            mode = "structural"
    else:
        mode = resolved_mode
    return {
        "mode": mode,
        "variation": variations[(candidate_index - 1) % len(variations)],
    }


def _average_reviews(
    first: PptxPatchReview,
    second: PptxPatchReview,
) -> PptxPatchReview:
    rubric_keys = set(first.rubric_scores) | set(second.rubric_scores)
    rubric = {
        key: round(
            (first.rubric_scores.get(key, 0.0) + second.rubric_scores.get(key, 0.0))
            / 2,
            4,
        )
        for key in rubric_keys
    }
    slide_keys = set(first.slide_scores) | set(second.slide_scores)
    slide_scores = {
        key: round(
            (
                first.slide_scores.get(key, 0.0)
                + second.slide_scores.get(key, 0.0)
            )
            / 2,
            4,
        )
        for key in slide_keys
    }
    improvements = [
        value
        for value in (first.relative_improvement, second.relative_improvement)
        if value is not None
    ]
    return first.model_copy(
        update={
            "instruction_fulfilled": first.instruction_fulfilled
            and second.instruction_fulfilled,
            "content_preserved": first.content_preserved and second.content_preserved,
            "production_notes_removed": first.production_notes_removed
            and second.production_notes_removed,
            "layout_valid": first.layout_valid and second.layout_valid,
            "editability_preserved": first.editability_preserved
            and second.editability_preserved,
            "balanced_density": first.balanced_density and second.balanced_density,
            "issues": list(dict.fromkeys([*first.issues, *second.issues]))[:32],
            "repair_instruction": first.repair_instruction
            or second.repair_instruction,
            "score": round((first.score + second.score) / 2, 4),
            "rubric_scores": rubric,
            "slide_scores": slide_scores,
            "relative_improvement": (
                round(sum(improvements) / len(improvements), 4)
                if improvements
                else None
            ),
        }
    )


def _scale_spec_to_canvas(spec: SlideSpec, width: int, height: int) -> SlideSpec:
    if (spec.source_width, spec.source_height) == (width, height):
        return spec
    source_ratio = spec.source_width / spec.source_height
    target_ratio = width / height
    if abs(source_ratio - target_ratio) > 0.02:
        raise OperationExecutionError(
            "multi-image decks require all source images to share one aspect ratio"
        )
    sx = width / spec.source_width
    sy = height / spec.source_height
    scale = min(sx, sy)
    payload = deepcopy(spec.model_dump(mode="json"))
    payload["source_width"] = width
    payload["source_height"] = height

    def scale_element(element: dict[str, Any]) -> None:
        bounds = element.get("bounds")
        if isinstance(bounds, dict):
            bounds["x"] *= sx
            bounds["y"] *= sy
            bounds["width"] *= sx
            bounds["height"] *= sy
        for key, factor in (("x1", sx), ("x2", sx), ("y1", sy), ("y2", sy)):
            if key in element:
                element[key] *= factor
        if "font_size_pt" in element:
            element["font_size_pt"] *= scale
        if "margin_px" in element:
            element["margin_px"] *= scale
        if "corner_radius" in element and element["corner_radius"] is not None:
            element["corner_radius"] *= scale
        stroke = element.get("stroke")
        if isinstance(stroke, dict) and "width_px" in stroke:
            stroke["width_px"] *= scale

    for element in payload["elements"]:
        scale_element(element)
    return clamp_slide_spec(SlideSpec.model_validate(payload))


def render_all_slides(
    pptx_path: str | Path,
    output_dir: str | Path,
    *,
    timeout_seconds: int = 300,
) -> list[Path]:
    source = Path(pptx_path).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        raise OperationExecutionError("LibreOffice and pdftoppm are required for rendering")
    with tempfile.TemporaryDirectory(prefix="editable-pptx-service-render-") as temporary:
        root = Path(temporary)
        profile = (root / "profile").resolve().as_uri()
        converted = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(root),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        pdf = root / f"{source.stem}.pdf"
        if converted.returncode != 0 or not pdf.exists():
            raise OperationExecutionError(
                "LibreOffice rendering failed: " + (converted.stderr or converted.stdout).strip()
            )
        target_pdf = output / "rendered-deck.pdf"
        shutil.copy2(pdf, target_pdf)
        rasterized = subprocess.run(
            [pdftoppm, "-png", "-r", "144", str(pdf), str(output / "slide")],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if rasterized.returncode != 0:
            raise OperationExecutionError(
                "PDF rasterization failed: " + (rasterized.stderr or rasterized.stdout).strip()
            )
    slides = sorted(output.glob("slide-*.png"))
    if not slides:
        raise OperationExecutionError("rendering produced no slide previews")
    return [target_pdf, *slides]


def _pptx_slide_count(pptx_path: str | Path) -> int:
    import zipfile

    from .ooxml_edit import slide_part_names

    with zipfile.ZipFile(Path(pptx_path).resolve()) as archive:
        return len(slide_part_names(archive))


def _select_slide_images(rendered: list[Path], slide_indices: list[int]) -> list[Path]:
    images = [path for path in rendered if path.suffix.lower() == ".png"]
    if not images or max(slide_indices) > len(images):
        raise OperationExecutionError("rendered slide count does not match the presentation")
    return [images[index - 1] for index in slide_indices]
