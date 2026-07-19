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
from .figure import FigureConversionOptions, convert_figure
from .image_analysis import extract_image_assets
from .job_store import JobStore, JobStoreError, sha256_file
from .models import SlideSpec, clamp_slide_spec
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
    write_json,
)
from .qa import audit_pptx
from .renderer import render_deck
from .service_models import (
    ArtifactKind,
    JobOperation,
    JobResource,
    JobStatus,
    PptxPatchPlan,
    ProductionInstruction,
)


class OperationCancelled(RuntimeError):
    pass


class OperationExecutionError(RuntimeError):
    pass


class OperationQualityError(OperationExecutionError):
    """A job failed its quality gate but has a downloadable best candidate."""

    def __init__(self, message: str, result: "OperationResult") -> None:
        super().__init__(message)
        self.result = result


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
    def __init__(self, store: JobStore, *, model: str = "gpt-5.5") -> None:
        self.store = store
        self.model = model

    def execute(self, job: JobResource, progress: ProgressCallback) -> OperationResult:
        self._cancel_guard(job.id)
        handlers = {
            JobOperation.IMAGE_TO_EDITABLE: self._image_to_editable,
            JobOperation.NOTES: self._notes,
            JobOperation.BEAUTIFY: self._beautify,
            JobOperation.FIGURE_TO_EDITABLE: self._figure_to_editable,
            JobOperation.RENDER: self._render,
            JobOperation.VALIDATE: self._validate,
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
        return self._pptx_edit(job, progress, operation="beautify")

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
