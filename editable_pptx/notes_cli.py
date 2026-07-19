from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .image_analysis import extract_image_assets
from .models import SlideSpec
from .powerpoint import validate_ooxml, validate_powerpoint
from .powerpoint import PowerPointCompatibilityError
from .qa import QualityCheckError, audit_pptx, render_first_slide
from .quality import apply_font_policy
from .renderer import RenderError, render_pptx
from .visible_notes import (
    VisibleNotesError,
    offline_note_modification,
    offline_note_review,
    request_note_modification,
    review_note_modification,
    validate_raster_policy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apply-slide-notes",
        description="Execute production notes visible on an editable reconstructed slide.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Source slide image")
    parser.add_argument("--spec-in", required=True, type=Path, help="Editable SlideSpec JSON")
    parser.add_argument("--output", required=True, type=Path, help="Modified PPTX output")
    parser.add_argument("--spec-out", type=Path, help="Modified SlideSpec JSON")
    parser.add_argument("--report", type=Path, help="Modification and QA report")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--instruction", help="Optional instruction in addition to visible notes")
    parser.add_argument(
        "--engine",
        choices=["offline", "openai"],
        default="offline",
        help="Offline deterministic execution or explicitly authorized OpenAI execution",
    )
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--review-iterations", type=int, default=1)
    parser.add_argument("--raster-policy", choices=["none", "photos-only", "allow"], default="none")
    parser.add_argument("--font-policy", choices=["portable", "exact"], default="portable")
    parser.add_argument("--powerpoint-validation", choices=["off", "auto", "required"], default="auto")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-output-tokens", type=int, default=64000)
    return parser


def _load_spec(path: Path) -> SlideSpec:
    return SlideSpec.model_validate_json(path.read_text(encoding="utf-8"))


def _save_spec(spec: SlideSpec, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")


def modify(args: argparse.Namespace) -> dict[str, object]:
    source = args.input.resolve()
    spec_input = args.spec_in.resolve()
    output = args.output.resolve()
    spec_output = (args.spec_out or output.with_suffix(".spec.json")).resolve()
    report_path = (args.report or output.with_suffix(".report.json")).resolve()
    if not source.exists():
        raise VisibleNotesError(f"Input image not found: {source}")
    if not spec_input.exists():
        raise VisibleNotesError(f"Input spec not found: {spec_input}")
    if output.suffix.lower() != ".pptx":
        raise VisibleNotesError("Output path must end in .pptx")
    if args.review_iterations < 0:
        raise VisibleNotesError("--review-iterations cannot be negative")

    temporary = None
    if args.workdir:
        workspace = args.workdir.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="editable-pptx-notes-")
        workspace = Path(temporary.name)

    try:
        current_spec = _load_spec(spec_input)
        patches = []
        reviews = []
        rendered = None
        review = None
        maximum_iterations = 0 if args.engine == "offline" else args.review_iterations
        for iteration in range(maximum_iterations + 1):
            if args.engine == "offline":
                current_spec, patch = offline_note_modification(
                    current_spec,
                    supplemental_instruction=args.instruction,
                )
            else:
                current_spec, patch = request_note_modification(
                    source,
                    current_spec,
                    model=args.model,
                    supplemental_instruction=(
                        args.instruction
                        if review is None or not review.repair_instruction
                        else f"{args.instruction or ''}\nRepair: {review.repair_instruction}".strip()
                    ),
                    previous_review=review,
                    timeout_seconds=args.timeout,
                    max_output_tokens=args.max_output_tokens,
                )
            current_spec, substitutions = apply_font_policy(current_spec, args.font_policy)
            validate_raster_policy(current_spec, args.raster_policy)
            iteration_dir = workspace / f"note-edit-{iteration:03d}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            _save_spec(current_spec, iteration_dir / "spec.json")
            assets = extract_image_assets(current_spec, source, iteration_dir / "assets")
            candidate = render_pptx(
                current_spec,
                iteration_dir / "candidate.pptx",
                assets=assets,
                timeout_seconds=args.timeout,
            )
            rendered = render_first_slide(
                candidate,
                iteration_dir / "rendered.png",
                timeout_seconds=args.timeout,
            )
            audit = audit_pptx(candidate, current_spec)
            patches.append(
                {
                    "iteration": iteration,
                    "detected_notes": patch.detected_notes,
                    "instruction_summary": patch.instruction_summary,
                    "removed_element_ids": patch.remove_element_ids,
                    "upserted_element_ids": [item.id for item in patch.upsert_elements],
                    "font_substitutions": substitutions,
                    "audit": audit,
                }
            )
            review = (
                offline_note_review(current_spec, patch)
                if args.engine == "offline"
                else review_note_modification(
                    rendered,
                    current_spec,
                    patch,
                    model=args.model,
                    supplemental_instruction=args.instruction,
                    timeout_seconds=args.timeout,
                )
            )
            reviews.append(review.model_dump(mode="json"))
            if (
                review.instruction_fulfilled
                and review.visible_notes_removed
                and review.layout_preserved
            ):
                break

        if rendered is None:
            raise VisibleNotesError("No modified slide candidate was produced")
        final_dir = workspace / f"note-edit-{len(patches) - 1:03d}"
        final_candidate = final_dir / "candidate.pptx"
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_candidate, output)
        _save_spec(current_spec, spec_output)
        final_audit = audit_pptx(output, current_spec)
        ooxml = validate_ooxml(output, font_policy=args.font_policy)
        powerpoint = validate_powerpoint(
            output,
            mode=args.powerpoint_validation,
            font_policy=args.font_policy,
            workspace=workspace / "powerpoint-validation",
            timeout_seconds=args.timeout,
        )
        report = {
            "input": str(source),
            "input_spec": str(spec_input),
            "output": str(output),
            "output_spec": str(spec_output),
            "model": args.model,
            "engine": args.engine,
            "supplemental_instruction": args.instruction,
            "patches": patches,
            "reviews": reviews,
            "audit": final_audit,
            "ooxml_validation": ooxml,
            "powerpoint_compatibility": powerpoint,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = modify(args)
    except (
        VisibleNotesError,
        RenderError,
        QualityCheckError,
        PowerPointCompatibilityError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
