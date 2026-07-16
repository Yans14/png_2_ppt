from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import ImageColor

from .figure_refinement import (
    FigureRefinementError,
    advise_figure_optimization,
    apply_figure_optimization_advice,
)
from .models import PathElement, SlideSpec
from .qa import (
    audit_pptx,
    compare_context_figure_style,
    compare_figure_geometry,
    compare_images,
    render_first_slide,
)
from .renderer import render_pptx


@dataclass(frozen=True)
class EvaluatedCandidate:
    name: str
    spec: SlideSpec
    pptx: Path
    png: Path
    geometry: dict[str, object]
    reference_style: dict[str, object]
    context_style: dict[str, object]
    objective: dict[str, object]
    audit: dict[str, object]


def _command_count(spec: SlideSpec) -> int:
    return sum(
        len(element.commands)
        for element in spec.elements
        if isinstance(element, PathElement)
    )


def _weighted_objective(
    geometry: dict[str, object],
    context_style: dict[str, object],
    *,
    command_count: int,
) -> dict[str, object]:
    terms = {
        "global_geometry": max(0.0, 1.0 - float(geometry["geometry_score"])),
        "local_geometry": max(0.0, 1.0 - float(geometry["worst_local_iou"])),
        "foreground_color": (
            0.70 * float(context_style["profile_mae"])
            + 0.30 * float(context_style["profile_p95"])
        ),
        "gradient_profile": float(context_style["gradient_profile_mae"]),
        "edit_complexity": max(0, command_count - 32) / 1000.0,
    }
    weights = {
        "global_geometry": 0.28,
        "local_geometry": 0.28,
        "foreground_color": 0.24,
        "gradient_profile": 0.20,
        "edit_complexity": 0.01,
    }
    weighted = {name: terms[name] * weights[name] for name in terms}
    loss = float(sum(weighted.values()))
    return {
        "loss": round(loss, 8),
        "terms": {name: round(value, 8) for name, value in terms.items()},
        "weights": weights,
        "weighted_terms": {name: round(value, 8) for name, value in weighted.items()},
    }


def _evaluate_candidate(
    name: str,
    spec: SlideSpec,
    directory: Path,
    *,
    clean_reference: Path,
    context_reference: Path,
    context_figure_bbox: tuple[float, float, float, float],
    background_color: str,
    timeout_seconds: int,
) -> EvaluatedCandidate:
    directory.mkdir(parents=True, exist_ok=True)
    pptx = render_pptx(
        spec,
        directory / "candidate.pptx",
        timeout_seconds=timeout_seconds,
    )
    png = render_first_slide(
        pptx,
        directory / "rendered.png",
        dpi=96,
        timeout_seconds=timeout_seconds,
    )
    geometry = compare_figure_geometry(
        clean_reference,
        png,
        background_color=background_color,
    )
    reference_style = compare_images(
        clean_reference,
        png,
        background_color=background_color,
    )
    context_style = compare_context_figure_style(
        context_reference,
        png,
        figure_bbox=context_figure_bbox,
        background_color=background_color,
    )
    objective = _weighted_objective(
        geometry,
        context_style,
        command_count=_command_count(spec),
    )
    return EvaluatedCandidate(
        name=name,
        spec=spec,
        pptx=pptx,
        png=png,
        geometry=geometry,
        reference_style=reference_style,
        context_style=context_style,
        objective=objective,
        audit=audit_pptx(pptx, spec),
    )


def _rgb(color: str) -> np.ndarray:
    return np.asarray(ImageColor.getrgb(color), dtype=np.float64)


def _hex(rgb: np.ndarray) -> str:
    values = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return "#" + "".join(f"{int(value):02X}" for value in values)


def _interpolate_profile(
    profile: list[dict[str, object]],
    progress: float,
    key: str,
) -> np.ndarray:
    ordered = sorted(profile, key=lambda row: float(row["progress"]))
    positions = np.asarray([float(row["progress"]) for row in ordered], dtype=np.float64)
    colors = np.asarray([row[key] for row in ordered], dtype=np.float64) * 255.0
    progress = float(np.clip(progress, positions[0], positions[-1]))
    return np.asarray(
        [np.interp(progress, positions, colors[:, channel]) for channel in range(3)],
        dtype=np.float64,
    )


def _gradient_profile_proposal(
    spec: SlideSpec,
    context_style: dict[str, object],
    *,
    learning_rate: float,
) -> SlideSpec | None:
    profile = context_style.get("profile")
    if not isinstance(profile, list) or not profile:
        return None
    payload = spec.model_dump(mode="json")
    changed = False
    for element in payload["elements"]:
        if element.get("kind") != "path":
            continue
        fill = element.get("fill", {})
        if fill.get("kind") != "linear_gradient":
            continue
        for stop in fill["stops"]:
            # PowerPoint's current arrow ramp runs from the dark head at position 0
            # to the pale tail at position 1, opposite the profile progress axis.
            progress = 1.0 - float(stop["position"])
            target = _interpolate_profile(profile, progress, "target_rgb")
            rendered = _interpolate_profile(profile, progress, "candidate_rgb")
            current = _rgb(stop["color"])
            stop["color"] = _hex(current + learning_rate * (target - rendered))
            changed = True
    if not changed:
        return None
    payload["reconstruction_notes"] = [
        *payload["reconstruction_notes"],
        f"Target-profile RGB descent step {learning_rate:.3f}",
    ]
    return SlideSpec.model_validate(payload)


def _gradient_angle_proposal(spec: SlideSpec, delta_degrees: float) -> SlideSpec | None:
    payload = spec.model_dump(mode="json")
    changed = False
    for element in payload["elements"]:
        fill = element.get("fill", {})
        if element.get("kind") != "path" or fill.get("kind") != "linear_gradient":
            continue
        fill["angle_deg"] = (float(fill["angle_deg"]) + delta_degrees) % 360.0
        changed = True
    if not changed:
        return None
    payload["reconstruction_notes"] = [
        *payload["reconstruction_notes"],
        f"Target gradient angle coordinate step {delta_degrees:+.3f} degrees",
    ]
    return SlideSpec.model_validate(payload)


def _gradient_position_proposal(spec: SlideSpec, delta: float) -> SlideSpec | None:
    payload = spec.model_dump(mode="json")
    changed = False
    for element in payload["elements"]:
        fill = element.get("fill", {})
        if element.get("kind") != "path" or fill.get("kind") != "linear_gradient":
            continue
        stops = sorted(fill["stops"], key=lambda row: float(row["position"]))
        if len(stops) <= 2:
            continue
        previous = 0.0
        for index, stop in enumerate(stops):
            if index in {0, len(stops) - 1}:
                previous = float(stop["position"])
                continue
            upper = float(stops[index + 1]["position"]) - 0.015
            position = min(upper, max(previous + 0.015, float(stop["position"]) + delta))
            stop["position"] = position
            previous = position
        fill["stops"] = stops
        changed = True
    if not changed:
        return None
    payload["reconstruction_notes"] = [
        *payload["reconstruction_notes"],
        f"Target gradient stop coordinate step {delta:+.4f}",
    ]
    return SlideSpec.model_validate(payload)


def _stochastic_geometry_proposals(
    spec: SlideSpec,
    geometry: dict[str, object],
    *,
    amplitude: float,
    seed: int,
) -> list[tuple[str, SlideSpec]]:
    regions = geometry.get("worst_local_regions")
    if not isinstance(regions, list) or not regions or amplitude <= 0:
        return []
    region = regions[0]
    if not isinstance(region, dict):
        return []
    rx0 = float(region["x"])
    ry0 = float(region["y"])
    rx1 = rx0 + float(region["width"])
    ry1 = ry0 + float(region["height"])
    rng = np.random.default_rng(seed)
    directions: dict[tuple[int, int, str], float] = {}
    base = spec.model_dump(mode="json")
    for element_index, element in enumerate(base["elements"]):
        if element.get("kind") != "path":
            continue
        bounds = element["bounds"]
        for command_index, command in enumerate(element["commands"]):
            for x_key, y_key in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
                if command.get(x_key) is None or command.get(y_key) is None:
                    continue
                actual_x = float(bounds["x"]) + float(command[x_key]) * float(bounds["width"])
                actual_y = float(bounds["y"]) + float(command[y_key]) * float(bounds["height"])
                if not (rx0 - 20 <= actual_x <= rx1 + 20 and ry0 - 20 <= actual_y <= ry1 + 20):
                    continue
                directions[(element_index, command_index, x_key)] = float(rng.choice([-1.0, 1.0]))
                directions[(element_index, command_index, y_key)] = float(rng.choice([-1.0, 1.0]))
    if not directions:
        return []

    proposals: list[tuple[str, SlideSpec]] = []
    for sign in (-1.0, 1.0):
        payload = spec.model_dump(mode="json")
        for (element_index, command_index, key), direction in directions.items():
            command = payload["elements"][element_index]["commands"][command_index]
            command[key] = float(np.clip(float(command[key]) + sign * direction * amplitude, -0.05, 1.05))
        payload["reconstruction_notes"] = [
            *payload["reconstruction_notes"],
            f"SPSA local geometry step {sign * amplitude:+.6f}",
        ]
        proposals.append((f"spsa-geometry-{sign:+.0f}", SlideSpec.model_validate(payload)))
    return proposals


def _candidate_summary(candidate: EvaluatedCandidate) -> dict[str, object]:
    return {
        "name": candidate.name,
        "loss": candidate.objective["loss"],
        "geometry_score": candidate.geometry["geometry_score"],
        "worst_local_iou": candidate.geometry["worst_local_iou"],
        "context_color_similarity": candidate.context_style["foreground_color_similarity"],
        "context_gradient_similarity": candidate.context_style[
            "gradient_profile_similarity"
        ],
        "command_count": _command_count(candidate.spec),
    }


def run_target_optimizer(
    clean_reference: str | Path,
    context_reference: str | Path,
    initial_spec: SlideSpec,
    *,
    context_figure_bbox: tuple[float, float, float, float],
    workspace: str | Path,
    background_color: str = "#FFFFFF",
    model: str = "gpt-5.5",
    api_key: str | None = None,
    timeout_seconds: int = 180,
    max_output_tokens: int = 12000,
    steps: int = 6,
    patience: int = 2,
    min_improvement: float = 0.00015,
    llm_interval: int = 2,
    target_geometry_score: float = 0.93,
    target_local_geometry_score: float = 0.90,
) -> dict[str, Any]:
    """Minimize target loss through LLM proposals and rendered coordinate descent.

    GPT-5.5 proposes complete native candidates. Python evaluates every proposal with a
    deterministic loss. JavaScript renders every candidate to editable PPTX before scoring.
    No LLM verdict can terminate or override the numerical optimization.
    """

    if steps < 1:
        raise ValueError("optimizer steps must be positive")
    if patience < 1:
        raise ValueError("optimizer patience must be positive")
    if llm_interval < 1:
        raise ValueError("optimizer llm_interval must be positive")
    clean = Path(clean_reference).resolve()
    context = Path(context_reference).resolve()
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)

    current = _evaluate_candidate(
        "initial",
        initial_spec,
        root / "iteration-0" / "current",
        clean_reference=clean,
        context_reference=context,
        context_figure_bbox=context_figure_bbox,
        background_color=background_color,
        timeout_seconds=timeout_seconds,
    )
    best = current
    history: list[dict[str, object]] = [_candidate_summary(current)]
    iteration_reports: list[dict[str, object]] = []
    color_rate = 1.0
    angle_step = 6.0
    position_step = 0.04
    geometry_step = 0.003
    plateau = 0
    converged = False
    stop_reason = "step_limit"

    for iteration in range(1, steps + 1):
        iteration_dir = root / f"iteration-{iteration}"
        proposal_specs: list[tuple[str, SlideSpec]] = []
        entry: dict[str, object] = {
            "iteration": iteration,
            "start": _candidate_summary(current),
            "step_sizes": {
                "color_learning_rate": round(color_rate, 6),
                "angle_degrees": round(angle_step, 6),
                "stop_position": round(position_step, 6),
                "geometry": round(geometry_step, 6),
            },
        }

        for rate in (0.35 * color_rate, 0.70 * color_rate, color_rate):
            proposal = _gradient_profile_proposal(
                current.spec,
                current.context_style,
                learning_rate=rate,
            )
            if proposal is not None:
                proposal_specs.append((f"rgb-descent-{rate:.4f}", proposal))
        for delta in (-angle_step, angle_step):
            proposal = _gradient_angle_proposal(current.spec, delta)
            if proposal is not None:
                proposal_specs.append((f"gradient-angle-{delta:+.3f}", proposal))
        for delta in (-position_step, position_step):
            proposal = _gradient_position_proposal(current.spec, delta)
            if proposal is not None:
                proposal_specs.append((f"gradient-stops-{delta:+.4f}", proposal))

        geometry_needs_work = (
            float(current.geometry["geometry_score"]) < target_geometry_score
            or float(current.geometry["worst_local_iou"]) < target_local_geometry_score
        )
        if geometry_needs_work:
            proposal_specs.extend(
                _stochastic_geometry_proposals(
                    current.spec,
                    current.geometry,
                    amplitude=geometry_step,
                    seed=iteration * 7919,
                )
            )

        if (iteration - 1) % llm_interval == 0 or plateau > 0:
            try:
                advice = advise_figure_optimization(
                    clean,
                    current.png,
                    current.spec,
                    geometry_metrics=current.geometry,
                    context_style_metrics=current.context_style,
                    objective=current.objective,
                    history=history,
                    iteration=iteration,
                    context_reference=context,
                    model=model,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                    max_output_tokens=max_output_tokens,
                )
                entry["llm_advice"] = advice.model_dump(
                    mode="json",
                    exclude={"corrected_paths"},
                )
                proposal_specs.append(
                    ("gpt-5.5-advice", apply_figure_optimization_advice(current.spec, advice))
                )
            except FigureRefinementError as error:
                entry["llm_advice_error"] = str(error)

        unique: list[tuple[str, SlideSpec]] = []
        seen: set[str] = set()
        for name, spec in proposal_specs:
            fingerprint = spec.model_dump_json()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append((name, spec))

        evaluated: list[EvaluatedCandidate] = []
        for index, (name, spec) in enumerate(unique):
            try:
                evaluated.append(
                    _evaluate_candidate(
                        name,
                        spec,
                        iteration_dir / f"proposal-{index:02d}",
                        clean_reference=clean,
                        context_reference=context,
                        context_figure_bbox=context_figure_bbox,
                        background_color=background_color,
                        timeout_seconds=timeout_seconds,
                    )
                )
            except Exception as error:
                entry.setdefault("proposal_errors", []).append(
                    {"name": name, "error": str(error)}
                )

        evaluated.sort(key=lambda candidate: float(candidate.objective["loss"]))
        entry["proposals"] = [_candidate_summary(candidate) for candidate in evaluated]
        previous_loss = float(current.objective["loss"])
        selected = evaluated[0] if evaluated else current
        improvement = previous_loss - float(selected.objective["loss"])

        if selected is not current and improvement > min_improvement:
            current = selected
            plateau = 0
            entry["selected"] = selected.name
            entry["improvement"] = round(improvement, 8)
            if float(current.objective["loss"]) < float(best.objective["loss"]):
                best = current
        else:
            plateau += 1
            color_rate *= 0.5
            angle_step *= 0.5
            position_step *= 0.5
            geometry_step *= 0.5
            entry["selected"] = "current"
            entry["improvement"] = 0.0
            entry["plateau"] = plateau

        history.append(_candidate_summary(current))
        entry["end"] = _candidate_summary(current)
        iteration_reports.append(entry)

        if (
            plateau >= patience
            and color_rate <= 0.125
            and angle_step <= 0.75
            and position_step <= 0.005
            and geometry_step <= 0.000375
        ):
            converged = True
            stop_reason = "loss_plateau_after_step_decay"
            break

    if not converged and len(iteration_reports) >= steps:
        stop_reason = "step_limit"

    return {
        "best_spec": best.spec,
        "best_pptx": best.pptx,
        "best_png": best.png,
        "best_geometry": best.geometry,
        "best_style": best.reference_style,
        "best_context_style": best.context_style,
        "best_objective": best.objective,
        "iterations": iteration_reports,
        "history": history,
        "converged": converged,
        "stop_reason": stop_reason,
        "context_reference": str(context),
        "context_figure_bbox": list(context_figure_bbox),
        "model": model,
        "renderer": "JavaScript editable PPTX renderer",
    }


__all__ = [
    "EvaluatedCandidate",
    "run_target_optimizer",
]
