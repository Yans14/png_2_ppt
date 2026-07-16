from __future__ import annotations

import argparse
import json
import sys

from .figure import FigureConversionOptions, convert_figure
from .figure_refinement import FigureRefinementError
from .openai_responses import OpenAIResponsesError
from .qa import QualityCheckError
from .renderer import RenderError


def _canvas(value: str) -> tuple[int, int]:
    normalized = value.lower().replace("×", "x")
    if "x" not in normalized:
        raise argparse.ArgumentTypeError("canvas must use WIDTHxHEIGHT, for example 1280x720")
    width, height = normalized.split("x", 1)
    try:
        parsed = (int(width), int(height))
    except ValueError as error:
        raise argparse.ArgumentTypeError("canvas width and height must be integers") from error
    if parsed[0] <= 0 or parsed[1] <= 0:
        raise argparse.ArgumentTypeError("canvas width and height must be positive")
    return parsed


def _bbox(value: str) -> tuple[float, float, float, float]:
    normalized = value.replace("×", ",").replace("x", ",")
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must use X,Y,WIDTH,HEIGHT")
    try:
        parsed = tuple(float(part) for part in parts)
    except ValueError as error:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from error
    if parsed[2] <= 0 or parsed[3] <= 0:
        raise argparse.ArgumentTypeError("bbox width and height must be positive")
    return parsed  # type: ignore[return-value]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="figure-to-editable-pptx",
        description=(
            "Convert an SVG or a raster geometric figure into native PowerPoint "
            "custom geometry with editable points."
        ),
    )
    parser.add_argument("--input", required=True, help="Input .svg, .png, .jpg, .webp, .bmp or .tiff")
    parser.add_argument("--output", required=True, help="Output .pptx path")
    parser.add_argument("--canvas", type=_canvas, default=(1280, 720), help="PowerPoint canvas in px")
    parser.add_argument("--padding", type=float, default=48, help="Padding around the fitted figure in px")
    parser.add_argument("--fit", choices=["contain", "stretch"], default="contain")
    parser.add_argument("--background", default="#FFFFFF", help="Slide background color")
    parser.add_argument("--max-colors", type=int, default=1, help="Raster color layers, from 1 to 32")
    parser.add_argument("--background-threshold", type=float, default=6)
    parser.add_argument("--alpha-threshold", type=int, default=8)
    parser.add_argument("--simplify", type=float, default=1.25, help="Raster contour tolerance in px")
    parser.add_argument("--min-area", type=float, default=12, help="Ignore smaller raster contours")
    parser.add_argument("--max-points", type=int, default=5000, help="Maximum raster contour points per layer")
    parser.add_argument(
        "--curve-error",
        type=float,
        default=1.75,
        help="Raster Bézier fit tolerance in px; use 0 for editable line segments",
    )
    parser.add_argument("--strict", action="store_true", help="Fail instead of returning approximation warnings")
    parser.add_argument("--timeout", type=int, default=180, help="Render/QA timeout in seconds")
    parser.add_argument(
        "--refine",
        choices=["none", "llm", "optimize"],
        default="llm",
        help="LLM semantic review/correction loop; use none for deterministic conversion only",
    )
    parser.add_argument("--model", default="gpt-5.5", help="OpenAI vision model for semantic review")
    parser.add_argument(
        "--iterations",
        type=int,
        default=2,
        help="Maximum LLM correction passes; each candidate is reviewed",
    )
    parser.add_argument(
        "--target-geometry-score",
        type=float,
        default=0.93,
        help="Whole-silhouette guard from 0 to 1; LLM verdict remains required",
    )
    parser.add_argument(
        "--target-local-geometry-score",
        type=float,
        default=0.90,
        help="Worst occupied local-region silhouette guard from 0 to 1",
    )
    parser.add_argument(
        "--target-foreground-style-score",
        type=float,
        default=0.96,
        help="Foreground-only color guard from 0 to 1",
    )
    parser.add_argument(
        "--target-gradient-score",
        type=float,
        default=0.95,
        help="Foreground gradient-profile guard from 0 to 1",
    )
    parser.add_argument(
        "--context-reference",
        help=(
            "Original slide/screenshot context for LLM color, gradient, and landmark review; "
            "clean input remains the deterministic geometry reference"
        ),
    )
    parser.add_argument(
        "--context-figure-bbox",
        type=_bbox,
        help="Target figure region in context pixels as X,Y,WIDTH,HEIGHT",
    )
    parser.add_argument(
        "--seed-spec",
        help="Start the refinement loop from an existing editable .shape.json candidate",
    )
    parser.add_argument("--optimizer-steps", type=int, default=6)
    parser.add_argument("--optimizer-patience", type=int, default=2)
    parser.add_argument("--optimizer-min-improvement", type=float, default=0.00015)
    parser.add_argument("--optimizer-llm-interval", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=12000)
    parser.add_argument("--workdir", help="Keep LLM iteration renders and diagnostics")
    parser.add_argument("--spec-out", help="Reusable native-shape JSON path")
    parser.add_argument("--report", help="Audit report JSON path")
    parser.add_argument("--preview", help="Rendered PNG path")
    parser.add_argument("--no-preview", action="store_true", help="Skip LibreOffice visual rendering")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    options = FigureConversionOptions(
        canvas_width=args.canvas[0],
        canvas_height=args.canvas[1],
        padding=args.padding,
        fit=args.fit,
        background_color=args.background,
        max_colors=args.max_colors,
        background_threshold=args.background_threshold,
        alpha_threshold=args.alpha_threshold,
        simplify=args.simplify,
        min_area=args.min_area,
        max_points=args.max_points,
        curve_error=args.curve_error,
        strict=args.strict,
        timeout_seconds=args.timeout,
        render_preview=not args.no_preview,
        refine_mode=args.refine,
        model=args.model,
        iterations=args.iterations,
        target_geometry_score=args.target_geometry_score,
        target_local_geometry_score=args.target_local_geometry_score,
        target_foreground_style_score=args.target_foreground_style_score,
        target_gradient_score=args.target_gradient_score,
        context_reference=args.context_reference,
        context_figure_bbox=args.context_figure_bbox,
        seed_spec=args.seed_spec,
        optimizer_steps=args.optimizer_steps,
        optimizer_patience=args.optimizer_patience,
        optimizer_min_improvement=args.optimizer_min_improvement,
        optimizer_llm_interval=args.optimizer_llm_interval,
        max_output_tokens=args.max_output_tokens,
    )
    try:
        report = convert_figure(
            args.input,
            args.output,
            options=options,
            spec_path=args.spec_out,
            report_path=args.report,
            preview_path=args.preview,
            workdir=args.workdir,
        )
    except (
        ValueError,
        RenderError,
        QualityCheckError,
        FigureRefinementError,
        OpenAIResponsesError,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": report["output"],
                "spec": report["spec"],
                "report": report["report"],
                "preview": report["preview"],
                "shape_count": report["shape_count"],
                "path_command_count": report["path_command_count"],
                "warnings": report["warnings"],
                "refinement": report["refinement"],
                "audit": report["audit"],
                "editable_contract": report["editable_contract"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
