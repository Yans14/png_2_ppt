from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .image_analysis import analyze_image, extract_image_assets
from .models import ImageElement, SlideSpec, clamp_slide_spec
from .openai_vision import OpenAIReconstructionError, reconstruct_slide, refine_slide
from .powerpoint import (
    PowerPointCompatibilityError,
    validate_ooxml,
    validate_powerpoint,
)
from .qa import QualityCheckError, audit_pptx, compare_images, render_first_slide, write_report
from .quality import (
    apply_font_policy,
    candidate_decision,
    choose_refinement_model,
    local_adjustment_proposals,
    profile_for,
)
from .renderer import RenderError, render_pptx
from .version import METRIC_VERSION, __version__


class ConversionError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image-to-editable-pptx",
        description="Reconstruct a reference image as native editable PowerPoint objects.",
    )
    parser.add_argument("--input", required=True, help="Reference image path")
    parser.add_argument("--output", required=True, help="Output .pptx path")
    parser.add_argument(
        "--model",
        default=None,
        help="Override the quality profile's OpenAI vision model for every LLM pass",
    )
    parser.add_argument(
        "--quality-profile",
        choices=["budget", "balanced", "max"],
        default="balanced",
        help="Quality/cost routing and local optimization profile",
    )
    parser.add_argument("--iterations", type=int, default=1, help="Maximum visual refinement passes")
    parser.add_argument(
        "--raster-policy",
        choices=["none", "photos-only", "allow"],
        default="photos-only",
        help="When separately editable raster picture objects are allowed",
    )
    parser.add_argument("--target-score", type=float, default=0.93, help="Stop refinement at this score")
    parser.add_argument("--spec-in", help="Render an existing JSON spec without an initial API call")
    parser.add_argument("--spec-out", help="Where to save the best editable JSON spec")
    parser.add_argument("--report", help="Where to save the JSON quality report")
    parser.add_argument("--workdir", help="Keep intermediate files in this directory")
    parser.add_argument("--timeout", type=int, default=300, help="Per API/render operation timeout in seconds")
    parser.add_argument("--max-output-tokens", type=int, default=64000)
    parser.add_argument(
        "--local-optimization",
        choices=["auto", "on", "off"],
        default="auto",
        help="Evaluate stable-ID local geometry/color proposals before LLM refinement",
    )
    parser.add_argument(
        "--powerpoint-validation",
        choices=["off", "auto", "required"],
        default="auto",
        help="Run OOXML checks and, when available, a real Microsoft PowerPoint round-trip",
    )
    parser.add_argument(
        "--font-policy",
        choices=["portable", "exact"],
        default="portable",
        help="Use portable Office fonts or preserve requested fonts exactly",
    )
    return parser


def _load_spec(path: str | Path) -> SlideSpec:
    return SlideSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _validate_policy(spec: SlideSpec, raster_policy: str, width: int, height: int) -> None:
    if spec.source_width != width or spec.source_height != height:
        raise ConversionError(
            f"Spec dimensions {spec.source_width}x{spec.source_height} do not match image {width}x{height}"
        )
    images = [element for element in spec.elements if isinstance(element, ImageElement)]
    if raster_policy == "none" and images:
        raise ConversionError(f"Raster policy 'none' rejected {len(images)} image element(s)")
    if raster_policy == "photos-only":
        invalid = [item.id for item in images if item.content_type not in {"photo", "texture"}]
        if invalid:
            raise ConversionError(f"Raster policy 'photos-only' rejected: {', '.join(invalid)}")
    suspicious = spec.suspicious_full_slide_images()
    if suspicious:
        raise ConversionError(f"Full-slide raster flattening rejected: {', '.join(suspicious)}")


def _save_spec(spec: SlideSpec, path: str | Path) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return output


def convert(args: argparse.Namespace) -> dict[str, object]:
    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source.exists():
        raise ConversionError(f"Input image not found: {source}")
    if output.suffix.lower() != ".pptx":
        raise ConversionError("Output path must end in .pptx")
    if args.iterations < 0:
        raise ConversionError("--iterations cannot be negative")
    if not 0 <= args.target_score <= 1:
        raise ConversionError("--target-score must be between 0 and 1")
    profile = profile_for(args.quality_profile)
    initial_model = args.model or profile.initial_model

    facts = analyze_image(source)
    facts_dict = facts.to_dict()
    spec_path = Path(args.spec_out).resolve() if args.spec_out else output.with_suffix(".spec.json")
    report_path = Path(args.report).resolve() if args.report else output.with_suffix(".report.json")

    temporary = None
    if args.workdir:
        workspace = Path(args.workdir).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="editable-pptx-")
        workspace = Path(temporary.name)

    try:
        if args.spec_in:
            current_spec = _load_spec(args.spec_in)
        else:
            current_spec = reconstruct_slide(
                source,
                image_facts=facts_dict,
                raster_policy=args.raster_policy,
                model=initial_model,
                timeout_seconds=args.timeout,
                max_output_tokens=args.max_output_tokens,
            )
        current_spec = clamp_slide_spec(current_spec)
        current_spec, initial_font_substitutions = apply_font_policy(
            current_spec,
            args.font_policy,
        )
        _validate_policy(current_spec, args.raster_policy, facts.width, facts.height)

        best_spec: SlideSpec | None = None
        best_metrics: dict[str, object] | None = None
        best_pptx: Path | None = None
        best_rendered: Path | None = None
        iterations: list[dict[str, object]] = []
        font_substitutions: list[dict[str, str]] = list(initial_font_substitutions)

        def evaluate_candidate(
            candidate_spec: SlideSpec,
            *,
            phase: str,
            model: str | None,
            changed_object_ids: list[str] | None = None,
            proposal: str | None = None,
        ) -> bool:
            nonlocal best_spec, best_metrics, best_pptx, best_rendered
            index = len(iterations)
            safe_phase = "".join(
                character if character.isalnum() or character in {"-", "_"} else "-"
                for character in phase
            )
            iteration_dir = workspace / f"candidate-{index:03d}-{safe_phase}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            _save_spec(candidate_spec, iteration_dir / "spec.json")
            assets = extract_image_assets(candidate_spec, source, iteration_dir / "assets")
            candidate_pptx = render_pptx(
                candidate_spec,
                iteration_dir / "candidate.pptx",
                assets=assets,
                timeout_seconds=args.timeout,
            )
            rendered_png = render_first_slide(
                candidate_pptx,
                iteration_dir / "rendered.png",
                timeout_seconds=args.timeout,
            )
            metrics = compare_images(
                source,
                rendered_png,
                background_color=facts.background_color,
                spec=candidate_spec,
            )
            audit = audit_pptx(candidate_pptx, candidate_spec)
            accepted, acceptance_reason = candidate_decision(
                metrics,
                audit,
                best_metrics,
                changed_object_ids,
            )
            entry = {
                "iteration": index,
                "phase": phase,
                "model": model,
                "proposal": proposal,
                "changed_object_ids": changed_object_ids or [],
                "metrics": metrics,
                "audit": audit,
                "reconstruction_notes": candidate_spec.reconstruction_notes,
                "accepted": accepted,
                "acceptance_reason": acceptance_reason,
            }
            iterations.append(entry)

            if accepted:
                best_spec = candidate_spec
                best_metrics = metrics
                best_pptx = candidate_pptx
                best_rendered = rendered_png
            return accepted

        evaluate_candidate(
            current_spec,
            phase="initial",
            model=None if args.spec_in else initial_model,
        )

        if best_spec is None or best_metrics is None or best_pptx is None:
            raise ConversionError("No valid native reconstruction was produced")

        local_enabled = (
            args.local_optimization == "on"
            or (
                args.local_optimization == "auto"
                and args.iterations > 0
                and profile.local_candidate_limit > 0
            )
        )
        if local_enabled and best_metrics["similarity_score"] < args.target_score:
            proposals = local_adjustment_proposals(
                best_spec,
                best_metrics,
                source,
                background_color=facts.background_color,
                candidate_limit=profile.local_candidate_limit,
                object_limit=profile.local_object_limit,
            )
            for proposal_name, changed_ids, proposal_spec in proposals:
                evaluate_candidate(
                    proposal_spec,
                    phase="local",
                    model=None,
                    changed_object_ids=changed_ids,
                    proposal=proposal_name,
                )

        for correction_index in range(1, args.iterations + 1):
            if best_metrics["similarity_score"] >= args.target_score:
                break
            if best_spec is None or best_metrics is None or best_rendered is None:
                raise ConversionError("No accepted reconstruction is available for refinement")
            refinement_model, model_reason = choose_refinement_model(
                profile,
                correction_index=correction_index,
                metrics=best_metrics,
                model_override=args.model,
            )
            refined_spec = refine_slide(
                source,
                best_rendered,
                best_spec,
                image_facts=facts_dict,
                metrics=best_metrics,
                raster_policy=args.raster_policy,
                model=refinement_model,
                timeout_seconds=args.timeout,
                max_output_tokens=args.max_output_tokens,
            )
            refined_spec = clamp_slide_spec(refined_spec)
            refined_spec, substitutions = apply_font_policy(refined_spec, args.font_policy)
            font_substitutions.extend(substitutions)
            _validate_policy(refined_spec, args.raster_policy, facts.width, facts.height)
            before_ids = {element.id for element in best_spec.elements}
            after_ids = {element.id for element in refined_spec.elements}
            changed_ids = sorted(
                before_ids.symmetric_difference(after_ids)
                | {
                    element.id
                    for element in refined_spec.elements
                    if element.id in before_ids
                    and element
                    != next(item for item in best_spec.elements if item.id == element.id)
                }
            )
            evaluate_candidate(
                refined_spec,
                phase="llm",
                model=refinement_model,
                changed_object_ids=changed_ids,
                proposal=model_reason,
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(best_pptx, output)
        _save_spec(best_spec, spec_path)
        final_audit = audit_pptx(output, best_spec)
        ooxml_validation = validate_ooxml(output, font_policy=args.font_policy)
        powerpoint_validation = validate_powerpoint(
            output,
            mode=args.powerpoint_validation,
            font_policy=args.font_policy,
            reference_image=source,
            workspace=workspace / "powerpoint-validation",
            timeout_seconds=args.timeout,
        )
        if args.powerpoint_validation == "required" and not powerpoint_validation.get(
            "compatible", False
        ):
            raise ConversionError("The generated deck failed required PowerPoint validation")
        models_used = [
            item["model"] for item in iterations if isinstance(item.get("model"), str)
        ]
        report = {
            "engine_version": __version__,
            "metric_version": METRIC_VERSION,
            "input": str(source),
            "output": str(output),
            "model": args.model or initial_model,
            "model_override": args.model,
            "models_used": models_used,
            "quality_profile": profile.name,
            "requested_iterations": args.iterations,
            "target_score": args.target_score,
            "raster_policy": args.raster_policy,
            "font_policy": args.font_policy,
            "font_substitutions": font_substitutions,
            "local_optimization": args.local_optimization,
            "powerpoint_validation_mode": args.powerpoint_validation,
            "image_facts": facts_dict,
            "best_metrics": best_metrics,
            "audit": final_audit,
            "iterations": iterations,
            "spec": str(spec_path),
            "ooxml_validation": ooxml_validation,
            "powerpoint_compatibility": powerpoint_validation,
            "fully_editable_contract": {
                "no_full_slide_screenshot": not final_audit["flattened_slide"],
                "text_and_graphics_are_native_objects": (
                    int(final_audit["native_shape_objects"])
                    + int(final_audit["picture_objects"])
                    > 0
                ),
                "photos_if_any_are_separate_picture_objects": True,
                "ooxml_is_powerpoint_compatible": bool(ooxml_validation["compatible"]),
            },
        }
        write_report(report, report_path)
        report["report"] = str(report_path)
        return report
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = convert(args)
    except (
        ConversionError,
        OpenAIReconstructionError,
        PowerPointCompatibilityError,
        RenderError,
        QualityCheckError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "output": report["output"],
        "spec": report["spec"],
        "report": report["report"],
        "similarity_score": report["best_metrics"]["similarity_score"],
        "audit": report["audit"],
    }, ensure_ascii=False, indent=2))
    return 0
