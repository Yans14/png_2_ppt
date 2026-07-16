from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageColor

from .models import SlideSpec, clamp_slide_spec


QualityProfileName = Literal["budget", "balanced", "max"]
FontPolicy = Literal["portable", "exact"]


@dataclass(frozen=True)
class QualityProfile:
    name: QualityProfileName
    initial_model: str
    refinement_model: str
    escalation_model: str | None
    local_candidate_limit: int
    local_object_limit: int


QUALITY_PROFILES: dict[QualityProfileName, QualityProfile] = {
    "budget": QualityProfile(
        name="budget",
        initial_model="gpt-5.6-luna",
        refinement_model="gpt-5.6-luna",
        escalation_model=None,
        local_candidate_limit=0,
        local_object_limit=0,
    ),
    "balanced": QualityProfile(
        name="balanced",
        initial_model="gpt-5.6-luna",
        refinement_model="gpt-5.6-luna",
        escalation_model="gpt-5.6-terra",
        local_candidate_limit=9,
        local_object_limit=1,
    ),
    "max": QualityProfile(
        name="max",
        initial_model="gpt-5.5",
        refinement_model="gpt-5.5",
        escalation_model=None,
        local_candidate_limit=18,
        local_object_limit=2,
    ),
}


PORTABLE_OFFICE_FONTS = {
    "aptos",
    "aptos display",
    "arial",
    "arial narrow",
    "calibri",
    "cambria",
    "courier new",
    "georgia",
    "tahoma",
    "times new roman",
    "trebuchet ms",
    "verdana",
}


def profile_for(name: str) -> QualityProfile:
    try:
        return QUALITY_PROFILES[name]  # type: ignore[index]
    except KeyError as error:
        raise ValueError(f"Unknown quality profile: {name}") from error


def choose_refinement_model(
    profile: QualityProfile,
    *,
    correction_index: int,
    metrics: dict[str, object],
    model_override: str | None = None,
) -> tuple[str, str]:
    if model_override:
        return model_override, "explicit model override"
    if (
        profile.escalation_model
        and correction_index >= 2
        and _needs_topology_escalation(metrics)
    ):
        return profile.escalation_model, "persistent structural or object-region error"
    return profile.refinement_model, "profile refinement model"


def _needs_topology_escalation(metrics: dict[str, object]) -> bool:
    structural = float(metrics.get("structural_score", 0.0))
    worst_object = float(metrics.get("worst_significant_object_similarity", 1.0))
    object_regions = metrics.get("object_regions", [])
    severe_object = any(
        float(item.get("structural_score", 1.0)) < 0.72
        and (
            float(item.get("high_error_fraction", 0.0)) >= 0.08
            or float(item.get("pixel_mae", 0.0)) >= 0.06
        )
        and item.get("kind") in {"shape", "path", "line", "component"}
        for item in object_regions
        if isinstance(item, dict)
    )
    return structural < 0.86 or worst_object < 0.68 or severe_object


def audit_is_editable(audit: dict[str, object]) -> bool:
    native_objects = int(audit.get("native_shape_objects", 0)) + int(
        audit.get("picture_objects", 0)
    )
    return (
        native_objects > 0
        and not bool(audit.get("flattened_slide", False))
        and int(audit.get("canvas_overflow_count", 0)) == 0
    )


def candidate_decision(
    candidate_metrics: dict[str, object],
    candidate_audit: dict[str, object],
    best_metrics: dict[str, object] | None,
    changed_object_ids: list[str] | None = None,
) -> tuple[bool, str]:
    """Accept global gains or meaningful local structural gains without visual regression."""

    if not audit_is_editable(candidate_audit):
        return False, "candidate failed native editability audit"
    if best_metrics is None:
        return True, "first valid native candidate"

    candidate_score = float(candidate_metrics.get("similarity_score", 0.0))
    best_score = float(best_metrics.get("similarity_score", 0.0))
    # Renderer noise and sub-pixel antialiasing can move the global score by roughly 1e-4.
    # Require a visible margin before allowing a broad patch to replace the best graph.
    if candidate_score >= best_score + 0.0005:
        return True, "global similarity improved"

    candidate_structure = float(candidate_metrics.get("structural_score", 0.0))
    best_structure = float(best_metrics.get("structural_score", 0.0))
    candidate_worst = float(
        candidate_metrics.get("worst_significant_object_similarity", 1.0)
    )
    best_worst = float(best_metrics.get("worst_significant_object_similarity", 1.0))
    targeted_gain = (
        candidate_structure >= best_structure + 0.01
        or candidate_worst >= best_worst + 0.02
    )
    if targeted_gain and candidate_score >= best_score - 0.005:
        return True, "targeted structural gain within global regression guard"
    if changed_object_ids and candidate_score >= best_score - 0.005:
        candidate_regions = {
            item.get("id"): item
            for item in candidate_metrics.get("object_regions", [])
            if isinstance(item, dict)
        }
        best_regions = {
            item.get("id"): item
            for item in best_metrics.get("object_regions", [])
            if isinstance(item, dict)
        }
        comparable = [
            object_id
            for object_id in changed_object_ids
            if object_id in candidate_regions and object_id in best_regions
        ]
        if comparable:
            structural_gain = sum(
                float(candidate_regions[object_id].get("structural_score", 0.0))
                - float(best_regions[object_id].get("structural_score", 0.0))
                for object_id in comparable
            ) / len(comparable)
            similarity_gain = sum(
                float(candidate_regions[object_id].get("similarity_score", 0.0))
                - float(best_regions[object_id].get("similarity_score", 0.0))
                for object_id in comparable
            ) / len(comparable)
            worst_regression = min(
                float(candidate_regions[object_id].get("structural_score", 0.0))
                - float(best_regions[object_id].get("structural_score", 0.0))
                for object_id in comparable
            )
            if (
                structural_gain >= 0.015 or similarity_gain >= 0.02
            ) and worst_regression >= -0.03:
                return True, "changed objects improved within global regression guard"
    return False, "no measured gain under the quality acceptance contract"


def apply_font_policy(
    spec: SlideSpec,
    policy: FontPolicy,
) -> tuple[SlideSpec, list[dict[str, str]]]:
    if policy == "exact":
        return spec, []
    payload = spec.model_dump(mode="json")
    substitutions: list[dict[str, str]] = []

    def normalize(items: list[dict[str, object]], scope: str) -> None:
        for item in items:
            if item.get("kind") != "text":
                continue
            original = str(item.get("font_family", "Arial"))
            if original.strip().lower() in PORTABLE_OFFICE_FONTS:
                continue
            replacement = _portable_font_for(original)
            item["font_family"] = replacement
            substitutions.append(
                {
                    "object_id": f"{scope}{item.get('id', '')}",
                    "requested": original,
                    "replacement": replacement,
                }
            )

    normalize(payload["elements"], "")
    for component in payload["components"]:
        normalize(component["elements"], f"{component['id']}/")
    return SlideSpec.model_validate(payload), substitutions


def _portable_font_for(font_family: str) -> str:
    normalized = font_family.lower()
    if any(token in normalized for token in ("serif", "times", "georgia", "cambria")):
        return "Times New Roman"
    if any(token in normalized for token in ("mono", "code", "courier")):
        return "Courier New"
    return "Arial"


def local_adjustment_proposals(
    spec: SlideSpec,
    metrics: dict[str, object],
    reference_path: str | Path,
    *,
    background_color: str,
    candidate_limit: int,
    object_limit: int,
) -> list[tuple[str, list[str], SlideSpec]]:
    """Create stable-ID coordinate/color proposals for the worst editable objects."""

    if candidate_limit <= 0 or object_limit <= 0:
        return []
    regions = [
        item
        for item in metrics.get("object_regions", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    significant = [
        item
        for item in regions
        if float(item.get("high_error_fraction", 0.0)) >= 0.08
        or float(item.get("pixel_mae", 0.0)) >= 0.06
    ]
    regions = significant or regions
    element_ids = {element.id for element in spec.elements}
    target_ids = [item["id"] for item in regions if item["id"] in element_ids][
        :object_limit
    ]
    proposals: list[tuple[str, list[str], SlideSpec]] = []
    for element_id in target_ids:
        element = next(item for item in spec.elements if item.id == element_id)
        if element.kind == "line":
            span = max(abs(element.x2 - element.x1), abs(element.y2 - element.y1), 20.0)
        else:
            span = max(min(element.bounds.width, element.bounds.height), 20.0)
        step = min(12.0, max(2.0, round(span * 0.025)))
        for label, mutation in _geometry_mutations(element.kind, step):
            candidate = _mutate_element(spec, element_id, mutation)
            if candidate is not None:
                proposals.append((f"{element_id}:{label}", [element_id], candidate))
            if len(proposals) >= candidate_limit:
                return proposals
        color = _sample_object_color(
            reference_path,
            element,
            background_color=background_color,
        )
        if color:
            candidate = _mutate_element(spec, element_id, ("color", color))
            if candidate is not None:
                proposals.append((f"{element_id}:sample-color", [element_id], candidate))
        if len(proposals) >= candidate_limit:
            return proposals[:candidate_limit]
    return proposals[:candidate_limit]


def _geometry_mutations(kind: str, step: float) -> list[tuple[str, tuple[str, float]]]:
    if kind == "line":
        return [
            ("left", ("dx", -step)),
            ("right", ("dx", step)),
            ("up", ("dy", -step)),
            ("down", ("dy", step)),
        ]
    return [
        ("left", ("dx", -step)),
        ("right", ("dx", step)),
        ("up", ("dy", -step)),
        ("down", ("dy", step)),
        ("wider", ("dw", step)),
        ("narrower", ("dw", -step)),
        ("taller", ("dh", step)),
        ("shorter", ("dh", -step)),
    ]


def _mutate_element(
    spec: SlideSpec,
    element_id: str,
    mutation: tuple[str, float | str],
) -> SlideSpec | None:
    payload = copy.deepcopy(spec.model_dump(mode="json"))
    item = next((entry for entry in payload["elements"] if entry["id"] == element_id), None)
    if item is None:
        return None
    axis, value = mutation
    if axis == "color":
        if item["kind"] == "text":
            item["color"] = str(value)
        elif item["kind"] in {"shape", "path"} and item["fill"]["kind"] == "solid":
            item["fill"]["color"] = str(value)
        else:
            return None
    elif item["kind"] == "line":
        delta = float(value)
        if axis == "dx":
            item["x1"] += delta
            item["x2"] += delta
        elif axis == "dy":
            item["y1"] += delta
            item["y2"] += delta
        else:
            return None
    else:
        delta = float(value)
        bounds = item["bounds"]
        if axis == "dx":
            bounds["x"] += delta
        elif axis == "dy":
            bounds["y"] += delta
        elif axis == "dw":
            bounds["width"] += delta
        elif axis == "dh":
            bounds["height"] += delta
        if bounds["width"] <= 1 or bounds["height"] <= 1:
            return None
    try:
        return clamp_slide_spec(SlideSpec.model_validate(payload))
    except ValueError:
        return None


def _sample_object_color(
    reference_path: str | Path,
    element: object,
    *,
    background_color: str,
) -> str | None:
    if getattr(element, "kind", None) not in {"text", "shape", "path"}:
        return None
    if getattr(element, "kind", None) in {"shape", "path"}:
        fill = getattr(element, "fill", None)
        if fill is None or fill.kind != "solid":
            return None
    bounds = getattr(element, "bounds", None)
    if bounds is None:
        return None
    with Image.open(reference_path) as image:
        rgb = image.convert("RGB")
        inset_x = bounds.width * 0.08
        inset_y = bounds.height * 0.08
        crop = rgb.crop(
            (
                max(0, round(bounds.x + inset_x)),
                max(0, round(bounds.y + inset_y)),
                min(rgb.width, round(bounds.x + bounds.width - inset_x)),
                min(rgb.height, round(bounds.y + bounds.height - inset_y)),
            )
        )
        if crop.width <= 0 or crop.height <= 0:
            return None
        quantized = crop.resize((min(64, crop.width), min(64, crop.height))).quantize(
            colors=24
        ).convert("RGB")
        colors = quantized.getcolors(maxcolors=4096) or []
    background = np.asarray(ImageColor.getrgb(background_color), dtype=np.int16)
    candidates: list[tuple[int, tuple[int, int, int]]] = []
    for count, color in colors:
        distance = int(np.max(np.abs(np.asarray(color, dtype=np.int16) - background)))
        if distance >= 18:
            candidates.append((count, color))
    if not candidates:
        return None
    if getattr(element, "kind", None) == "text":
        candidates.sort(
            key=lambda entry: (
                0.2126 * entry[1][0] + 0.7152 * entry[1][1] + 0.0722 * entry[1][2],
                -entry[0],
            )
        )
        selected = candidates[0][1]
    else:
        selected = max(candidates, key=lambda entry: entry[0])[1]
    return "#" + "".join(f"{channel:02X}" for channel in selected)
