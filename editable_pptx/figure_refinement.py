from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import PathElement, SlideSpec, StrictModel
from .openai_responses import OpenAIResponsesError, request_structured_response


class FigureRefinementError(RuntimeError):
    pass


class FigureIssue(StrictModel):
    category: Literal[
        "silhouette",
        "proportions",
        "curvature",
        "corners",
        "topology",
        "color",
        "gradient",
        "stroke",
    ]
    severity: Literal["minor", "major"]
    region: Literal["whole", "start", "middle", "end", "arrowhead", "other"]
    explanation: str
    correction: str


class FigureReview(StrictModel):
    verdict: Literal["accept", "revise"]
    shape_family: Literal[
        "curved_arrow",
        "straight_arrow",
        "icon",
        "logo",
        "compound_shape",
        "abstract_shape",
        "other",
    ]
    geometry_score: float = Field(ge=0, le=1)
    local_detail_score: float = Field(ge=0, le=1)
    style_score: float = Field(ge=0, le=1)
    color_score: float = Field(ge=0, le=1)
    gradient_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    summary: str
    issues: list[FigureIssue]
    corrected_paths: list[PathElement]

    @model_validator(mode="after")
    def corrected_paths_match_verdict(self) -> "FigureReview":
        if self.verdict == "accept" and self.corrected_paths:
            raise ValueError("accepted review must return an empty corrected_paths list")
        if self.verdict == "revise" and not self.corrected_paths:
            raise ValueError("revision review must return complete corrected_paths")
        return self


class FigureOptimizationAdvice(StrictModel):
    focus: Literal["geometry", "arrowhead", "color", "gradient", "mixed"]
    expected_loss_reduction: float = Field(ge=0, le=1)
    summary: str
    corrected_paths: list[PathElement]

    @model_validator(mode="after")
    def requires_complete_candidate(self) -> "FigureOptimizationAdvice":
        if not self.corrected_paths:
            raise ValueError("optimization advice must return complete corrected_paths")
        return self


SYSTEM_PROMPT = """You are a senior vector illustrator and PowerPoint freeform-path reviewer.

Judge geometric identity and visual style, not literal pixel identity. The target is the same
editable shape a demanding human reviewer would recognize as the same drawing: same topology,
silhouette, proportions, curvature, true corners, taper, direction, distinctive landmarks,
colors, and gradient progression. Ignore only antialiasing, compression, subpixel rendering,
and isolated one-pixel noise. Never let a high whole-shape score hide a bad local landmark.

You receive two images in this exact order: source reference, then current PowerPoint render.
You also receive the current native path JSON and deterministic silhouette metrics.

Accept only when every supplied deterministic threshold is met and every critical region is
visually correct. Color and gradient are mandatory acceptance criteria, not secondary polish.
If the geometry is correct but the fill is wrong, return verdict=revise with the same commands
and corrected native fill/gradient. If anything is wrong, return a complete replacement list
for every native path. Keep path IDs, path count, layer order, and topology stable. Use the
fewest useful anchors: L for true corners and straight arrowhead edges, C for smooth curves.
Coordinates and Bezier controls are normalized inside each path bounds. Preserve native fills,
gradients, strokes, and editability. Never return images, text, preset shapes, masks, or effects.
Do not optimize for a literal pixel match.

For arrows, explicitly compare centerline trajectory, shaft width/taper, inner and outer curves,
neck position, arrowhead base width, both arrowhead shoulders, straightness of head edges,
notch depth, tip angle, tip position, and transition continuity. A visibly malformed arrowhead
requires verdict=revise even when the rest of the shaft is excellent.

Scoring contract: geometry_score covers the whole silhouette; local_detail_score covers the
worst critical landmark; style_score is overall visual style; color_score covers foreground
colors only; gradient_score covers direction, stops, contrast, and progression. For a property
that is genuinely absent, use 1.0. An accept verdict requires every score to meet its supplied
threshold, no major issue, and all deterministic thresholds to pass."""


OPTIMIZATION_SYSTEM_PROMPT = """You are the structural proposal engine inside a numerical
PowerPoint figure optimizer. You never accept or reject a candidate and you never terminate
the loop. Python owns the objective function and will keep only proposals that lower measured
loss after JavaScript/PowerPoint rendering.

Always return one complete native editable candidate for every supplied path. Preserve path
IDs, path count, layer order, topology, and editability. Use L for real corners and straight
arrowhead edges and C for smooth curves. If geometry already matches, copy every bound and
command exactly and improve only native fill/gradient values. If style already matches, keep
the fill and propose a small targeted geometric improvement in the worst measured region.
Never introduce images, masks, effects, text, or preset arrows.

For curved arrows, inspect centerline trajectory, shaft width/taper, inner and outer curvature,
neck, both head shoulders, straight head edges, notch depth, tip angle/position, and tail cutoff.
For gradients, inspect direction, stop locations, per-stop RGB values, contrast, and smoothness.
Use the target slide for appearance, the clean reference for topology/silhouette, and the current
render for the candidate. Prefer conservative proposals with a plausible measurable decrease
in the supplied weighted loss."""


def _user_prompt(
    current_spec: SlideSpec,
    geometry_metrics: dict[str, object],
    style_metrics: dict[str, object],
    *,
    target_geometry_score: float,
    target_local_geometry_score: float,
    target_foreground_style_score: float,
    target_gradient_score: float,
    has_context_reference: bool,
    iteration: int,
) -> str:
    paths = [
        element.model_dump(mode="json")
        for element in current_spec.elements
        if isinstance(element, PathElement)
    ]
    style_threshold = max(target_foreground_style_score, target_gradient_score)
    acceptance_thresholds: dict[str, float] = {
        "geometry_score": target_geometry_score,
        "worst_local_iou": target_local_geometry_score,
        "llm_geometry_score": target_geometry_score,
        "llm_local_detail_score": target_local_geometry_score,
        "llm_style_score": style_threshold,
        "llm_color_score": target_foreground_style_score,
        "llm_gradient_score": target_gradient_score,
    }
    if not has_context_reference:
        acceptance_thresholds.update(
            {
                "foreground_color_similarity": target_foreground_style_score,
                "gradient_profile_similarity": target_gradient_score,
            }
        )
    payload = {
        "iteration": iteration,
        "canvas": {
            "width": current_spec.source_width,
            "height": current_spec.source_height,
        },
        "acceptance_thresholds": acceptance_thresholds,
        "deterministic_geometry_metrics": geometry_metrics,
        "deterministic_style_metrics": (
            {
                "not_applicable": True,
                "reason": (
                    "The clean geometry aid has approximate colors. Judge color and gradient "
                    "visually from the original slide context, not from these omitted metrics."
                ),
            }
            if has_context_reference
            else style_metrics
        ),
        "current_native_paths": paths,
        "image_order": (
            [
                "original_slide_context_with_target_figure",
                "clean_geometry_reference",
                "current_powerpoint_render",
            ]
            if has_context_reference
            else ["source_reference", "current_powerpoint_render"]
        ),
    }
    comparison_instruction = (
        "Image 1 is the original slide context. Judge the large target figure's color, gradient, "
        "landmarks, and intended appearance from it, ignoring overlapping labels/icons. Image 2 "
        "is a clean geometry aid whose colors may be approximate. Image 3 is the current PowerPoint render. "
        if has_context_reference
        else "Compare source image 1 with PowerPoint render image 2. "
    )
    return (
        comparison_instruction
        + "Evaluate whether they are the same geometric form and native visual style. "
        "A threshold failure requires revision; whole-slide white space must not dilute foreground errors. "
        "When revising, return all paths, not a patch.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def review_figure(
    source_reference: str | Path,
    rendered_candidate: str | Path,
    current_spec: SlideSpec,
    *,
    geometry_metrics: dict[str, object],
    style_metrics: dict[str, object],
    target_geometry_score: float,
    target_local_geometry_score: float,
    target_foreground_style_score: float,
    target_gradient_score: float,
    iteration: int,
    context_reference: str | Path | None = None,
    model: str = "gpt-5.5",
    api_key: str | None = None,
    timeout_seconds: int = 300,
    max_output_tokens: int = 12000,
) -> FigureReview:
    try:
        image_paths = (
            [context_reference, source_reference, rendered_candidate]
            if context_reference is not None
            else [source_reference, rendered_candidate]
        )
        return request_structured_response(
            FigureReview,
            schema_name="editable_figure_review",
            system_text=SYSTEM_PROMPT,
            user_text=_user_prompt(
                current_spec,
                geometry_metrics,
                style_metrics,
                target_geometry_score=target_geometry_score,
                target_local_geometry_score=target_local_geometry_score,
                target_foreground_style_score=target_foreground_style_score,
                target_gradient_score=target_gradient_score,
                has_context_reference=context_reference is not None,
                iteration=iteration,
            ),
            image_paths=image_paths,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            reasoning_effort="medium",
            max_retries=1,
        )
    except OpenAIResponsesError as error:
        raise FigureRefinementError(str(error)) from error


def advise_figure_optimization(
    source_reference: str | Path,
    rendered_candidate: str | Path,
    current_spec: SlideSpec,
    *,
    geometry_metrics: dict[str, object],
    context_style_metrics: dict[str, object],
    objective: dict[str, object],
    history: list[dict[str, object]],
    iteration: int,
    context_reference: str | Path,
    model: str = "gpt-5.5",
    api_key: str | None = None,
    timeout_seconds: int = 300,
    max_output_tokens: int = 12000,
) -> FigureOptimizationAdvice:
    paths = [
        element.model_dump(mode="json")
        for element in current_spec.elements
        if isinstance(element, PathElement)
    ]
    payload = {
        "iteration": iteration,
        "canvas": {
            "width": current_spec.source_width,
            "height": current_spec.source_height,
        },
        "weighted_objective": objective,
        "deterministic_geometry_metrics": geometry_metrics,
        "target_context_style_metrics": context_style_metrics,
        "recent_history": history[-4:],
        "current_native_paths": paths,
        "image_order": [
            "target_slide_context",
            "clean_geometry_reference",
            "current_powerpoint_render",
        ],
    }
    try:
        return request_structured_response(
            FigureOptimizationAdvice,
            schema_name="editable_figure_optimization_advice",
            system_text=OPTIMIZATION_SYSTEM_PROMPT,
            user_text=(
                "Propose the next complete editable candidate. This is an optimization step, "
                "not a verdict. Minimize the largest weighted loss terms while avoiding regressions.\n\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
            image_paths=[context_reference, source_reference, rendered_candidate],
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            reasoning_effort="medium",
            max_retries=1,
        )
    except OpenAIResponsesError as error:
        raise FigureRefinementError(str(error)) from error


def apply_figure_optimization_advice(
    current_spec: SlideSpec,
    advice: FigureOptimizationAdvice,
) -> SlideSpec:
    style_focus = advice.focus in {"color", "gradient"}
    issue = FigureIssue(
        category="gradient" if style_focus else "silhouette",
        severity="minor",
        region="whole",
        explanation=advice.summary,
        correction=advice.summary,
    )
    synthetic_review = FigureReview(
        verdict="revise",
        shape_family="curved_arrow",
        geometry_score=0.0,
        local_detail_score=0.0,
        style_score=0.0,
        color_score=0.0,
        gradient_score=0.0,
        confidence=1.0,
        summary=advice.summary,
        issues=[issue],
        corrected_paths=advice.corrected_paths,
    )
    return apply_figure_review(current_spec, synthetic_review)


def _validate_coordinate(value: float | None, *, endpoint: bool) -> None:
    if value is None:
        return
    number = float(value)
    if not math.isfinite(number):
        raise FigureRefinementError("LLM correction contains a non-finite path coordinate")
    lower, upper = (-0.08, 1.08) if endpoint else (-1.0, 2.0)
    if not lower <= number <= upper:
        kind = "endpoint" if endpoint else "Bezier control"
        raise FigureRefinementError(f"LLM correction contains out-of-range {kind}: {number}")


def apply_figure_review(
    current_spec: SlideSpec,
    review: FigureReview,
    *,
    max_commands: int = 1024,
) -> SlideSpec:
    if review.verdict == "accept":
        return current_spec

    current_paths = [element for element in current_spec.elements if isinstance(element, PathElement)]
    if len(current_paths) != len(current_spec.elements):
        raise FigureRefinementError("Figure refinement accepts path-only specs")
    if len(review.corrected_paths) != len(current_paths):
        raise FigureRefinementError("LLM correction changed native path count")
    current_ids = {path.id for path in current_paths}
    corrected_ids = {path.id for path in review.corrected_paths}
    if corrected_ids != current_ids:
        raise FigureRefinementError("LLM correction changed native path IDs")

    command_count = sum(len(path.commands) for path in review.corrected_paths)
    if command_count > max_commands:
        raise FigureRefinementError(
            f"LLM correction exceeds native command limit ({command_count} > {max_commands})"
        )

    canvas_width = current_spec.source_width
    canvas_height = current_spec.source_height
    for path in review.corrected_paths:
        bounds = path.bounds
        if (
            bounds.x < -canvas_width * 0.05
            or bounds.y < -canvas_height * 0.05
            or bounds.x + bounds.width > canvas_width * 1.05
            or bounds.y + bounds.height > canvas_height * 1.05
        ):
            raise FigureRefinementError("LLM correction moved a path outside the slide canvas")
        for command in path.commands:
            _validate_coordinate(command.x, endpoint=True)
            _validate_coordinate(command.y, endpoint=True)
            _validate_coordinate(command.x1, endpoint=False)
            _validate_coordinate(command.y1, endpoint=False)
            _validate_coordinate(command.x2, endpoint=False)
            _validate_coordinate(command.y2, endpoint=False)

    ordered = {path.id: path for path in review.corrected_paths}
    style_only = bool(review.issues) and all(
        issue.category in {"color", "gradient", "stroke"}
        for issue in review.issues
    )
    if style_only:
        corrected_elements = []
        for path in current_paths:
            corrected = ordered[path.id]
            element = path.model_dump(mode="json")
            element["fill"] = corrected.fill.model_dump(mode="json")
            element["stroke"] = corrected.stroke.model_dump(mode="json")
            corrected_elements.append(element)
    else:
        corrected_elements = [ordered[path.id].model_dump(mode="json") for path in current_paths]
    payload = current_spec.model_dump(mode="json")
    payload["elements"] = corrected_elements
    payload["reconstruction_notes"] = [
        *current_spec.reconstruction_notes,
        f"LLM vector review: {review.shape_family}, {review.verdict}",
        *( ["Style-only LLM correction preserved path geometry"] if style_only else [] ),
        review.summary,
    ]
    return SlideSpec.model_validate(payload)


__all__ = [
    "FigureIssue",
    "FigureOptimizationAdvice",
    "FigureRefinementError",
    "FigureReview",
    "advise_figure_optimization",
    "apply_figure_optimization_advice",
    "apply_figure_review",
    "review_figure",
]
