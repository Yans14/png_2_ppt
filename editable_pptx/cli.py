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
from .qa import QualityCheckError, audit_pptx, compare_images, render_first_slide, write_report
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
    parser.add_argument("--model", default="gpt-5.5", help="OpenAI vision model")
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
                model=args.model,
                timeout_seconds=args.timeout,
                max_output_tokens=args.max_output_tokens,
            )
        current_spec = clamp_slide_spec(current_spec)
        _validate_policy(current_spec, args.raster_policy, facts.width, facts.height)

        best_spec: SlideSpec | None = None
        best_metrics: dict[str, object] | None = None
        best_pptx: Path | None = None
        best_rendered: Path | None = None
        iterations: list[dict[str, object]] = []

        for index in range(args.iterations + 1):
            iteration_dir = workspace / f"iteration-{index}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            assets = extract_image_assets(current_spec, source, iteration_dir / "assets")
            candidate_pptx = render_pptx(
                current_spec,
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
            )
            audit = audit_pptx(candidate_pptx, current_spec)
            accepted = best_metrics is None or metrics["similarity_score"] > best_metrics["similarity_score"]
            entry = {
                "iteration": index,
                "metrics": metrics,
                "audit": audit,
                "reconstruction_notes": current_spec.reconstruction_notes,
                "accepted": accepted,
            }
            iterations.append(entry)

            if accepted:
                best_spec = current_spec
                best_metrics = metrics
                best_pptx = candidate_pptx
                best_rendered = rendered_png

            if (
                best_metrics is not None
                and best_metrics["similarity_score"] >= args.target_score
            ) or index >= args.iterations:
                break
            if best_spec is None or best_metrics is None or best_rendered is None:
                raise ConversionError("No accepted reconstruction is available for refinement")
            current_spec = refine_slide(
                source,
                best_rendered,
                best_spec,
                image_facts=facts_dict,
                metrics=best_metrics,
                raster_policy=args.raster_policy,
                model=args.model,
                timeout_seconds=args.timeout,
                max_output_tokens=args.max_output_tokens,
            )
            current_spec = clamp_slide_spec(current_spec)
            _validate_policy(current_spec, args.raster_policy, facts.width, facts.height)

        if best_spec is None or best_metrics is None or best_pptx is None:
            raise ConversionError("No valid reconstruction was produced")

        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(best_pptx, output)
        _save_spec(best_spec, spec_path)
        final_audit = audit_pptx(output, best_spec)
        report = {
            "engine_version": __version__,
            "metric_version": METRIC_VERSION,
            "input": str(source),
            "output": str(output),
            "model": args.model,
            "requested_iterations": args.iterations,
            "target_score": args.target_score,
            "raster_policy": args.raster_policy,
            "image_facts": facts_dict,
            "best_metrics": best_metrics,
            "audit": final_audit,
            "iterations": iterations,
            "spec": str(spec_path),
            "fully_editable_contract": {
                "no_full_slide_screenshot": not final_audit["flattened_slide"],
                "text_and_graphics_are_native_objects": True,
                "photos_if_any_are_separate_picture_objects": True,
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
    except (ConversionError, OpenAIReconstructionError, RenderError, QualityCheckError, ValueError) as error:
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
