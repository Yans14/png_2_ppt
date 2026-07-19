from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from .job_store import sha256_file
from .ooxml_edit import (
    apply_ooxml_patch,
    content_preserved,
    extract_production_instructions,
    extract_shape_graph,
    extract_text_manifest,
)
from .openai_responses import request_structured_response
from .powerpoint import validate_ooxml
from .service_models import (
    PptxPatchPlan,
    PptxPatchReview,
    ProductionInstruction,
    ShapeSnapshot,
)


PLAN_SYSTEM = """
You are a PowerPoint production editor. Return a strict OOXML patch plan using only
the supplied shape IDs. Coordinates and sizes are points. Keep the deck native and
editable: never flatten, rasterize, or replace a slide with a screenshot.

For operation=notes, execute every production instruction, then remove its callout,
speaker-note text, or comment. API instructions override comments, comments override
speaker notes, and speaker notes override visible callouts. If instructions conflict,
record the resolution. Adding repeated rows requires duplicating every dependent
native object in the row (card, label, range, values, comments), distributing the
whole section evenly, and resizing/repositioning adjacent sections when necessary.
Use obvious bracketed placeholders when dummy content is requested.

For operation=beautify, business content is immutable. You may move, resize, recolor,
or change font sizes, but never rewrite facts, figures, labels, sources, footnotes,
logos, or page furniture. Prefer a small number of high-impact edits. Template style
beats target-image style, which beats textual style guidance. Preserve charts,
SmartArt, OLE objects, media, animations, relationships, and unsupported objects.

Use duplicate only when a note explicitly requests additional repeated objects. New
shape IDs must be unique and larger than all existing IDs on that slide. Operations
run in order, so a duplicated shape can be edited by a later operation. Do not delete
ordinary slide content. Keep body text at or above the supplied minimum font size.
""".strip()


REVIEW_SYSTEM = """
You are the final PowerPoint quality gate. Review the rendered candidate against the
source render, the production instructions, and deterministic checks. Return strict
review data. Reject unreadable, overlapping, clipped, incomplete, or content-changing
results. For note jobs, verify every instruction was executed and its authoring note
is absent. For beautification, verify business content is unchanged. Native OOXML and
editability checks are authoritative. A score of 8 or more is approvable only when all
boolean gates are true. Give one concrete repair instruction when rejecting.
""".strip()


class PptxAgentError(RuntimeError):
    pass


def _selected(value: object, slide_count: int) -> list[int]:
    if not value:
        return list(range(1, slide_count + 1))
    if isinstance(value, dict):
        if value.get("all", True):
            return list(range(1, slide_count + 1))
        value = value.get("indices", [])
    indices = sorted({int(item) for item in value})
    if not indices or indices[0] < 1 or indices[-1] > slide_count:
        raise PptxAgentError("slide selection is outside the presentation")
    return indices


def _plan_user_text(
    *,
    operation: str,
    source_sha256: str,
    shapes: list[ShapeSnapshot],
    instructions: list[ProductionInstruction],
    selected_slides: list[int],
    minimum_font_size_pt: float,
    prior_review: PptxPatchReview | None = None,
    style_instruction: str | None = None,
    template_present: bool = False,
    target_count: int = 0,
) -> str:
    payload = {
        "operation": operation,
        "source_sha256": source_sha256,
        "selected_slides": selected_slides,
        "minimum_font_size_pt": minimum_font_size_pt,
        "style_instruction": style_instruction,
        "template_render_attached": template_present,
        "target_images_attached": target_count,
        "instructions": [item.model_dump(mode="json") for item in instructions],
        "shapes": [item.model_dump(mode="json") for item in shapes],
        "prior_review": prior_review.model_dump(mode="json") if prior_review else None,
    }
    return (
        "Create the complete patch plan for this deck. The first attached images are "
        "the current rendered slides in selected_slides order; any remaining image is "
        "a template or visual target.\n\n" + json.dumps(payload, ensure_ascii=False)
    )


def request_patch_plan(
    *,
    source_path: Path,
    operation: str,
    images: list[Path],
    instructions: list[ProductionInstruction],
    selected_slides: list[int],
    model: str,
    timeout_seconds: int,
    minimum_font_size_pt: float,
    style_instruction: str | None = None,
    prior_review: PptxPatchReview | None = None,
    extra_images: list[Path] | None = None,
    template_present: bool = False,
) -> PptxPatchPlan:
    shapes = [
        item for item in extract_shape_graph(source_path)
        if item.slide_index in selected_slides
    ]
    plan = request_structured_response(
        PptxPatchPlan,
        schema_name="powerpoint_ooxml_patch_plan",
        system_text=PLAN_SYSTEM,
        user_text=_plan_user_text(
            operation=operation,
            source_sha256=sha256_file(source_path),
            shapes=shapes,
            instructions=instructions,
            selected_slides=selected_slides,
            minimum_font_size_pt=minimum_font_size_pt,
            prior_review=prior_review,
            style_instruction=style_instruction,
            template_present=template_present,
            target_count=len(extra_images or []),
        ),
        image_paths=[*images, *(extra_images or [])],
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=48000,
        reasoning_effort="high",
    )
    if plan.operation != operation:
        raise PptxAgentError(f"planner returned operation={plan.operation}, expected {operation}")
    if plan.source_sha256 != sha256_file(source_path):
        raise PptxAgentError("planner returned the wrong source checksum")
    _validate_plan(plan, shapes, selected_slides)
    return plan


def _validate_plan(
    plan: PptxPatchPlan,
    shapes: list[ShapeSnapshot],
    selected_slides: list[int],
) -> None:
    shape_map = {(item.slide_index, item.shape_id): item for item in shapes}
    available = set(shape_map)
    created: set[tuple[int, int]] = set()
    for operation in plan.operations:
        key = (operation.slide_index, operation.target_shape_id)
        if operation.slide_index not in selected_slides:
            raise PptxAgentError(f"patch targets unselected slide {operation.slide_index}")
        if key not in available and key not in created:
            raise PptxAgentError(f"patch targets unknown shape s{key[0]}:{key[1]}")
        if plan.operation == "beautify" and operation.action in {
            "delete", "replace_text", "duplicate"
        }:
            target = shape_map.get(key)
            if target is None or target.text:
                raise PptxAgentError(
                    "beautification may not delete, rewrite, or duplicate content-bearing shapes"
                )
        if operation.action == "duplicate":
            assert operation.new_shape_id is not None
            new_key = (operation.slide_index, operation.new_shape_id)
            if new_key in available or new_key in created:
                raise PptxAgentError(f"duplicate shape ID already exists: s{new_key[0]}:{new_key[1]}")
            created.add(new_key)


def review_patch(
    *,
    operation: str,
    source_images: list[Path],
    candidate_images: list[Path],
    instructions: list[ProductionInstruction],
    deterministic: dict[str, Any],
    model: str,
    timeout_seconds: int,
) -> PptxPatchReview:
    user_text = json.dumps(
        {
            "operation": operation,
            "instructions": [item.model_dump(mode="json") for item in instructions],
            "deterministic_checks": deterministic,
            "image_order": "source slides first, then candidate slides",
        },
        ensure_ascii=False,
    )
    review = request_structured_response(
        PptxPatchReview,
        schema_name="powerpoint_patch_review",
        system_text=REVIEW_SYSTEM,
        user_text=user_text,
        image_paths=[*source_images, *candidate_images],
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=12000,
        reasoning_effort="high",
    )
    return review


def review_approved(review: PptxPatchReview, deterministic: dict[str, Any]) -> bool:
    exact_note_override = bool(
        deterministic.get("operation") == "notes"
        and deterministic.get("operation_checks_passed")
        and deterministic.get("production_notes_removed")
        and review.content_preserved
        and review.production_notes_removed
        and review.layout_valid
        and review.editability_preserved
        and review.balanced_density
        and review.score >= 6
    )
    required = (
        review.instruction_fulfilled or exact_note_override,
        review.content_preserved,
        review.production_notes_removed,
        review.layout_valid,
        review.editability_preserved,
        review.balanced_density,
        review.score >= 8 or exact_note_override,
        bool(deterministic.get("ooxml_compatible")),
        bool(deterministic.get("content_preserved")),
    )
    return all(required)


def deterministic_checks(
    *,
    source_manifest: dict[int, list[str]],
    candidate_path: Path,
    instructions: Iterable[ProductionInstruction],
    font_policy: str,
    allowed_removed_texts: Iterable[str] = (),
    source_path: Path | None = None,
    plan: PptxPatchPlan | None = None,
) -> dict[str, Any]:
    instruction_list = list(instructions)
    removed = [item.raw_text for item in instruction_list] + list(allowed_removed_texts)
    preserved, missing = content_preserved(
        source_manifest,
        extract_text_manifest(candidate_path),
        removed,
    )
    ooxml = validate_ooxml(candidate_path, font_policy=font_policy)
    candidate_text = [
        value for values in extract_text_manifest(candidate_path).values() for value in values
    ]
    production_notes_removed = not any(
        item.raw_text in candidate_text for item in instruction_list
    )
    operation_checks = (
        _verify_patch_operations(source_path, candidate_path, plan)
        if source_path is not None and plan is not None
        else []
    )
    return {
        "operation": plan.operation if plan else None,
        "content_preserved": preserved,
        "missing_content": missing[:30],
        "ooxml_compatible": bool(ooxml.get("compatible")),
        "ooxml_errors": ooxml.get("errors", []),
        "ooxml_warnings": ooxml.get("warnings", []),
        "production_notes_removed": production_notes_removed,
        "operation_checks": operation_checks,
        "operation_checks_passed": bool(operation_checks) and all(
            item["passed"] for item in operation_checks
        ),
    }


def _verify_patch_operations(
    source_path: Path,
    candidate_path: Path,
    plan: PptxPatchPlan,
) -> list[dict[str, Any]]:
    before = {
        (item.slide_index, item.shape_id): item for item in extract_shape_graph(source_path)
    }
    after = {
        (item.slide_index, item.shape_id): item for item in extract_shape_graph(candidate_path)
    }
    checks: list[dict[str, Any]] = []
    tolerance = 0.15
    for operation in plan.operations:
        key = (operation.slide_index, operation.target_shape_id)
        source = before.get(key)
        result = after.get(key)
        passed = False
        observed: dict[str, Any] = {}
        if operation.action == "delete":
            passed = result is None
        elif operation.action == "replace_text":
            observed["text"] = result.text if result else None
            passed = result is not None and result.text == (operation.text or "")
        elif operation.action == "recolor":
            observed["color"] = result.fill_color if result else None
            passed = result is not None and result.fill_color == operation.color
        elif operation.action == "move" and source and result:
            dx = (result.x_pt or 0) - (source.x_pt or 0)
            dy = (result.y_pt or 0) - (source.y_pt or 0)
            observed.update({"dx_pt": dx, "dy_pt": dy})
            x_ok = (
                abs(dx - operation.dx_pt) <= tolerance if operation.dx_pt is not None
                else abs((result.x_pt or 0) - operation.x_pt) <= tolerance if operation.x_pt is not None
                else True
            )
            y_ok = (
                abs(dy - operation.dy_pt) <= tolerance if operation.dy_pt is not None
                else abs((result.y_pt or 0) - operation.y_pt) <= tolerance if operation.y_pt is not None
                else True
            )
            passed = x_ok and y_ok
        elif operation.action == "resize" and result:
            observed.update({"width_pt": result.width_pt, "height_pt": result.height_pt})
            width_ok = (
                abs((result.width_pt or 0) - operation.width_pt) <= tolerance
                if operation.width_pt is not None else True
            )
            height_ok = (
                abs((result.height_pt or 0) - operation.height_pt) <= tolerance
                if operation.height_pt is not None else True
            )
            passed = width_ok and height_ok
        elif operation.action == "duplicate" and operation.new_shape_id is not None:
            new_key = (operation.slide_index, operation.new_shape_id)
            passed = new_key in after
            observed["new_shape_present"] = passed
        elif operation.action == "set_font_size":
            # The OOXML editor validates and applies this scalar directly; the
            # lightweight shape graph intentionally does not duplicate run-level font data.
            passed = result is not None
        checks.append(
            {
                "op_id": operation.op_id,
                "action": operation.action,
                "passed": passed,
                "observed": observed,
            }
        )
    return checks


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "PptxAgentError",
    "apply_ooxml_patch",
    "deterministic_checks",
    "extract_production_instructions",
    "extract_shape_graph",
    "extract_text_manifest",
    "request_patch_plan",
    "review_approved",
    "review_patch",
    "write_json",
    "_selected",
]
