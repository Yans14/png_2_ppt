from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import xml.etree.ElementTree as ElementTree
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageColor, ImageFilter

from .bezier_fit import fit_closed_curve
from .figure_refinement import (
    FigureRefinementError,
    FigureReview,
    apply_figure_review,
    review_figure,
)
from .models import FillSpec, PathElement, SlideSpec, StrokeSpec
from .qa import (
    audit_pptx,
    compare_figure_geometry,
    compare_images,
    render_first_slide,
    write_report,
)
from .raster_trace import RasterTraceResult, trace_raster
from .renderer import render_pptx

try:
    from svgelements import (
        Arc,
        Close,
        Color,
        CubicBezier,
        Line,
        Move,
        Path as SvgPath,
        QuadraticBezier,
        SVG,
        Shape,
    )
except ImportError as error:  # pragma: no cover - exercised by installation failures
    raise RuntimeError(
        "SVG figure conversion requires svgelements>=1.9.6,<2. "
        "Install the project dependencies before importing editable_pptx.figure."
    ) from error


SUPPORTED_VECTOR_SUFFIXES = {".svg"}
SUPPORTED_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_VECTOR_ELEMENTS = 1500
MAX_PATH_COMMANDS = 80_000


@dataclass(frozen=True)
class FigurePath:
    id: str
    name: str
    commands: list[dict[str, float | str | None]]
    bbox: tuple[float, float, float, float]
    fill: dict[str, Any]
    stroke: dict[str, Any]
    source_kind: str


@dataclass(frozen=True)
class FigureDocument:
    width: float
    height: float
    paths: list[FigurePath]
    input_type: str
    warnings: list[str] = field(default_factory=list)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        if not self.paths:
            raise ValueError("Figure contains no paths")
        return (
            min(item.bbox[0] for item in self.paths),
            min(item.bbox[1] for item in self.paths),
            max(item.bbox[2] for item in self.paths),
            max(item.bbox[3] for item in self.paths),
        )

    @property
    def command_count(self) -> int:
        return sum(len(item.commands) for item in self.paths)


@dataclass(frozen=True)
class FigureConversionOptions:
    canvas_width: int = 1280
    canvas_height: int = 720
    padding: float = 48
    fit: str = "contain"
    background_color: str = "#FFFFFF"
    max_colors: int = 1
    background_threshold: float = 6
    alpha_threshold: int = 8
    simplify: float = 1.25
    min_area: float = 12
    max_points: int = 5000
    curve_error: float = 1.75
    strict: bool = False
    timeout_seconds: int = 180
    render_preview: bool = True
    refine_mode: str = "none"
    model: str = "gpt-5.5"
    iterations: int = 2
    target_geometry_score: float = 0.93
    target_local_geometry_score: float = 0.90
    target_foreground_style_score: float = 0.96
    target_gradient_score: float = 0.95
    context_reference: str | None = None
    context_figure_bbox: tuple[float, float, float, float] | None = None
    seed_spec: str | None = None
    optimizer_steps: int = 6
    optimizer_patience: int = 2
    optimizer_min_improvement: float = 0.00015
    optimizer_llm_interval: int = 2
    max_output_tokens: int = 12000
    api_key: str | None = None

    def validate(self) -> None:
        if self.canvas_width <= 0 or self.canvas_height <= 0:
            raise ValueError("Canvas dimensions must be positive")
        if self.canvas_width / 96 > 56 or self.canvas_height / 96 > 56:
            raise ValueError("PowerPoint canvas dimensions cannot exceed 56 inches")
        if self.padding < 0:
            raise ValueError("Padding cannot be negative")
        if self.padding * 2 >= min(self.canvas_width, self.canvas_height):
            raise ValueError("Padding leaves no usable canvas")
        if self.fit not in {"contain", "stretch"}:
            raise ValueError("fit must be 'contain' or 'stretch'")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.curve_error < 0:
            raise ValueError("curve_error must be non-negative")
        if self.refine_mode not in {"none", "llm", "optimize"}:
            raise ValueError("refine_mode must be 'none', 'llm' or 'optimize'")
        if self.iterations < 0 or self.iterations > 8:
            raise ValueError("iterations must be between 0 and 8")
        if not 0 <= self.target_geometry_score <= 1:
            raise ValueError("target_geometry_score must be between 0 and 1")
        if not 0 <= self.target_local_geometry_score <= 1:
            raise ValueError("target_local_geometry_score must be between 0 and 1")
        if not 0 <= self.target_foreground_style_score <= 1:
            raise ValueError("target_foreground_style_score must be between 0 and 1")
        if not 0 <= self.target_gradient_score <= 1:
            raise ValueError("target_gradient_score must be between 0 and 1")
        if self.max_output_tokens < 1024:
            raise ValueError("max_output_tokens must be at least 1024")
        if self.context_figure_bbox is not None:
            if len(self.context_figure_bbox) != 4:
                raise ValueError("context_figure_bbox must contain x, y, width and height")
            if self.context_figure_bbox[2] <= 0 or self.context_figure_bbox[3] <= 0:
                raise ValueError("context_figure_bbox width and height must be positive")
        if self.optimizer_steps < 1 or self.optimizer_steps > 24:
            raise ValueError("optimizer_steps must be between 1 and 24")
        if self.optimizer_patience < 1 or self.optimizer_patience > 8:
            raise ValueError("optimizer_patience must be between 1 and 8")
        if self.optimizer_min_improvement < 0:
            raise ValueError("optimizer_min_improvement must be non-negative")
        if self.optimizer_llm_interval < 1 or self.optimizer_llm_interval > 8:
            raise ValueError("optimizer_llm_interval must be between 1 and 8")


def _hex_color(value: str, fallback: str = "#000000") -> str:
    try:
        rgb = ImageColor.getrgb(str(value).strip())
    except (ValueError, TypeError):
        return fallback.upper()
    return "#" + "".join(f"{int(channel):02X}" for channel in rgb[:3])


def _float(value: object, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return float(str(value).strip().rstrip("%")) / (100 if str(value).strip().endswith("%") else 1)
    except ValueError:
        return fallback


def _transparent_stroke() -> dict[str, Any]:
    return {"color": "#FFFFFF", "opacity": 0.0, "width_px": 0.0, "dash": "solid"}


def _none_fill() -> dict[str, Any]:
    return {"kind": "none", "color": None, "opacity": 0.0, "angle_deg": None, "stops": []}


def _solid_fill(color: str, opacity: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "solid",
        "color": _hex_color(color),
        "opacity": max(0.0, min(1.0, float(opacity))),
        "angle_deg": None,
        "stops": [],
    }


def _color_spec(color_value: Color | None, *, fallback: str) -> tuple[str, float] | None:
    if color_value is None:
        return None
    text = str(color_value).lower()
    if text in {"none", "transparent"}:
        return None
    return (
        str(color_value.hexrgb).upper(),
        max(0.0, min(1.0, float(color_value.alpha) / 255.0)),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_stop_style(element: ElementTree.Element) -> dict[str, str]:
    result = {key: value for key, value in element.attrib.items()}
    for declaration in element.attrib.get("style", "").split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _gradient_definitions(svg_path: Path, warnings: list[str]) -> dict[str, dict[str, Any]]:
    try:
        root = ElementTree.fromstring(svg_path.read_bytes())
    except ElementTree.ParseError as error:
        raise ValueError(f"Invalid SVG XML: {error}") from error

    gradients: dict[str, dict[str, Any]] = {}
    unsupported_tags = {
        "clipPath": "clip paths",
        "filter": "filters",
        "mask": "masks",
        "pattern": "pattern fills",
        "radialGradient": "radial gradients",
        "text": "live SVG text",
        "image": "embedded SVG images",
    }
    found_unsupported: set[str] = set()
    for element in root.iter():
        local = _local_name(element.tag)
        if local in unsupported_tags:
            found_unsupported.add(unsupported_tags[local])
        if local != "linearGradient":
            continue
        gradient_id = element.attrib.get("id")
        if not gradient_id:
            warnings.append("Ignored linearGradient without id")
            continue
        x1 = _float(element.attrib.get("x1"), 0.0)
        y1 = _float(element.attrib.get("y1"), 0.0)
        x2 = _float(element.attrib.get("x2"), 1.0)
        y2 = _float(element.attrib.get("y2"), 0.0)
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360
        stops: list[dict[str, Any]] = []
        for stop in element:
            if _local_name(stop.tag) != "stop":
                continue
            style = _parse_stop_style(stop)
            stops.append(
                {
                    "position": max(0.0, min(1.0, _float(style.get("offset"), 0.0))),
                    "color": _hex_color(style.get("stop-color", "#000000")),
                    "opacity": max(0.0, min(1.0, _float(style.get("stop-opacity"), 1.0))),
                }
            )
        if len(stops) < 2:
            warnings.append(f"Gradient #{gradient_id} has fewer than two stops and was approximated")
            continue
        stops.sort(key=lambda item: float(item["position"]))
        gradients[gradient_id] = {
            "kind": "linear_gradient",
            "color": None,
            "opacity": 1.0,
            "angle_deg": angle,
            "stops": stops,
        }
        if element.attrib.get("gradientTransform"):
            warnings.append(f"Gradient #{gradient_id}: gradientTransform is approximated by its base angle")
    for feature in sorted(found_unsupported):
        warnings.append(f"SVG {feature} are not natively represented and may be approximated or omitted")
    return gradients


def _shape_fill(
    shape: Shape,
    gradients: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    raw_fill = str(shape.values.get("fill", "")).strip()
    match = re.fullmatch(r"url\(\s*#([^\s)]+)\s*\)", raw_fill)
    if match:
        gradient = gradients.get(match.group(1))
        if gradient:
            result = {
                **gradient,
                "stops": [dict(stop) for stop in gradient["stops"]],
            }
            opacity = _float(shape.values.get("opacity"), 1.0) * _float(
                shape.values.get("fill-opacity"),
                1.0,
            )
            result["opacity"] = max(0.0, min(1.0, opacity))
            return result
        warnings.append(f"Unresolved or unsupported SVG paint server: {raw_fill}")
    solid = _color_spec(getattr(shape, "fill", None), fallback="#000000")
    if solid is None:
        return _none_fill()
    return _solid_fill(solid[0], solid[1] * _float(shape.values.get("opacity"), 1.0))


def _shape_stroke(shape: Shape) -> dict[str, Any]:
    solid = _color_spec(getattr(shape, "stroke", None), fallback="#000000")
    if solid is None:
        return _transparent_stroke()
    width = float(getattr(shape, "stroke_width", 1.0) or 1.0)
    dash = "solid"
    dash_array = str(shape.values.get("stroke-dasharray", "")).strip().lower()
    if dash_array and dash_array != "none":
        dash = "dash"
    return {
        "color": solid[0],
        "opacity": solid[1] * _float(shape.values.get("opacity"), 1.0),
        "width_px": max(0.0, width),
        "dash": dash,
    }


def _command(
    op: str,
    *,
    x: float | None = None,
    y: float | None = None,
    x1: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
    y2: float | None = None,
) -> dict[str, float | str | None]:
    return {"op": op, "x": x, "y": y, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _path_commands(path: SvgPath, *, arc_error: float = 0.05) -> list[dict[str, float | str | None]]:
    prepared = SvgPath(path)
    prepared.approximate_arcs_with_cubics(error=arc_error)
    commands: list[dict[str, float | str | None]] = []
    for segment in prepared:
        if isinstance(segment, Move):
            commands.append(_command("M", x=float(segment.end.x), y=float(segment.end.y)))
        elif isinstance(segment, Line):
            commands.append(_command("L", x=float(segment.end.x), y=float(segment.end.y)))
        elif isinstance(segment, CubicBezier):
            commands.append(
                _command(
                    "C",
                    x=float(segment.end.x),
                    y=float(segment.end.y),
                    x1=float(segment.control1.x),
                    y1=float(segment.control1.y),
                    x2=float(segment.control2.x),
                    y2=float(segment.control2.y),
                )
            )
        elif isinstance(segment, QuadraticBezier):
            start = segment.start
            control = segment.control
            end = segment.end
            commands.append(
                _command(
                    "C",
                    x=float(end.x),
                    y=float(end.y),
                    x1=float(start.x + (control.x - start.x) * 2 / 3),
                    y1=float(start.y + (control.y - start.y) * 2 / 3),
                    x2=float(end.x + (control.x - end.x) * 2 / 3),
                    y2=float(end.y + (control.y - end.y) * 2 / 3),
                )
            )
        elif isinstance(segment, Close):
            commands.append(_command("Z"))
        elif isinstance(segment, Arc):  # defensive: approximate_arcs_with_cubics should remove these
            raise ValueError("Arc conversion did not resolve all SVG arc segments")
        else:
            raise ValueError(f"Unsupported SVG segment: {type(segment).__name__}")
    return commands


def _svg_document(svg_path: Path) -> FigureDocument:
    if svg_path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"SVG exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MB input limit")
    warnings: list[str] = []
    gradients = _gradient_definitions(svg_path, warnings)
    try:
        svg = SVG.parse(
            str(svg_path),
            reify=True,
            ppi=96.0,
            parse_display_none=False,
            on_error="raise",
        )
    except Exception as error:  # svgelements exposes parser-specific errors
        raise ValueError(f"Unable to parse SVG geometry: {error}") from error

    paths: list[FigurePath] = []
    for shape_index, shape in enumerate(
        (element for element in svg.elements() if isinstance(element, Shape)),
        start=1,
    ):
        if len(paths) >= MAX_VECTOR_ELEMENTS:
            raise ValueError(f"SVG exceeds the {MAX_VECTOR_ELEMENTS} editable element limit")
        if str(shape.values.get("visibility", "")).lower() == "hidden":
            continue
        try:
            path = SvgPath(shape)
            bbox = path.bbox()
            if bbox is None:
                continue
            commands = _path_commands(path)
        except Exception as error:
            warnings.append(
                f"Skipped {type(shape).__name__} {getattr(shape, 'id', None) or shape_index}: {error}"
            )
            continue
        if len(commands) < 2:
            continue
        shape_id = str(getattr(shape, "id", None) or f"svg-shape-{shape_index}")
        paths.append(
            FigurePath(
                id=shape_id,
                name=str(shape.values.get("aria-label") or shape.values.get("title") or shape_id),
                commands=commands,
                bbox=tuple(float(value) for value in bbox),
                fill=_shape_fill(shape, gradients, warnings),
                stroke=_shape_stroke(shape),
                source_kind=type(shape).__name__,
            )
        )

    if not paths:
        raise ValueError("SVG contains no supported visible geometric shapes")
    command_count = sum(len(item.commands) for item in paths)
    if command_count > MAX_PATH_COMMANDS:
        raise ValueError(f"SVG exceeds the {MAX_PATH_COMMANDS} path-command limit")

    viewbox = getattr(svg, "viewbox", None)
    width = float(getattr(svg, "width", 0.0) or 0.0)
    height = float(getattr(svg, "height", 0.0) or 0.0)
    if width <= 0 or height <= 0:
        min_x = min(item.bbox[0] for item in paths)
        min_y = min(item.bbox[1] for item in paths)
        max_x = max(item.bbox[2] for item in paths)
        max_y = max(item.bbox[3] for item in paths)
        width = max(1.0, max_x - min_x)
        height = max(1.0, max_y - min_y)
    elif viewbox is not None:
        width = max(width, 1.0)
        height = max(height, 1.0)

    return FigureDocument(
        width=width,
        height=height,
        paths=paths,
        input_type="svg",
        warnings=warnings,
    )


def _contour_commands(
    contours: Iterable[list[tuple[float, float]]],
    *,
    curve_error: float,
) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for contour in contours:
        if len(contour) < 3:
            continue
        commands.append(_command("M", x=contour[0][0], y=contour[0][1]))
        if curve_error > 0:
            curves = fit_closed_curve(contour, curve_error)
            if curves:
                commands[-1] = _command("M", x=curves[0].start[0], y=curves[0].start[1])
                commands.extend(
                    _command(
                        "C",
                        x=curve.end[0],
                        y=curve.end[1],
                        x1=curve.control1[0],
                        y1=curve.control1[1],
                        x2=curve.control2[0],
                        y2=curve.control2[1],
                    )
                    for curve in curves
                )
            else:
                commands.extend(_command("L", x=x, y=y) for x, y in contour[1:])
        else:
            commands.extend(_command("L", x=x, y=y) for x, y in contour[1:])
        commands.append(_command("Z"))
    return commands


def _polygon_area(contour: list[tuple[float, float]]) -> float:
    return abs(
        sum(
            contour[index][0] * contour[(index + 1) % len(contour)][1]
            - contour[(index + 1) % len(contour)][0] * contour[index][1]
            for index in range(len(contour))
        )
        / 2
    )


def _remove_trace_specks(
    contours: list[list[tuple[float, float]]],
    *,
    relative_area: float = 0.002,
) -> tuple[list[list[tuple[float, float]]], int]:
    if len(contours) <= 1:
        return contours, 0
    areas = [_polygon_area(contour) for contour in contours]
    threshold = max(1.0, max(areas) * relative_area)
    kept = [contour for contour, area in zip(contours, areas, strict=True) if area >= threshold]
    return kept or [contours[areas.index(max(areas))]], len(contours) - len(kept)


def _raster_document(path: Path, options: FigureConversionOptions) -> FigureDocument:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"Raster input exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MB limit")
    traced: RasterTraceResult = trace_raster(
        path,
        max_colors=options.max_colors,
        background_threshold=options.background_threshold,
        alpha_threshold=options.alpha_threshold,
        simplify=options.simplify,
        min_area=options.min_area,
        max_points=options.max_points,
    )
    paths: list[FigurePath] = []
    warnings = list(traced.warnings)
    for index, layer in enumerate(traced.layers, start=1):
        contours, removed_specks = _remove_trace_specks(layer.contours)
        if removed_specks:
            warnings.append(
                f"Removed {removed_specks} sub-pixel raster contour speck(s) from layer {index}"
            )
        commands = _contour_commands(contours, curve_error=options.curve_error)
        xs = [point[0] for contour in contours for point in contour]
        ys = [point[1] for contour in contours for point in contour]
        if layer.gradient:
            fill: dict[str, Any] = {
                "kind": "linear_gradient",
                "color": None,
                "opacity": layer.opacity,
                "angle_deg": layer.gradient["angle_deg"],
                "stops": layer.gradient["stops"],
            }
        else:
            fill = _solid_fill(layer.color, layer.opacity)
        paths.append(
            FigurePath(
                id=f"raster-layer-{index}",
                name=f"Traced color layer {index}",
                commands=commands,
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                fill=fill,
                stroke=_transparent_stroke(),
                source_kind="raster-contour",
            )
        )
    return FigureDocument(
        width=float(traced.width),
        height=float(traced.height),
        paths=paths,
        input_type="raster",
        warnings=warnings,
    )


def load_figure(
    input_path: str | Path,
    options: FigureConversionOptions | None = None,
) -> FigureDocument:
    options = options or FigureConversionOptions()
    options.validate()
    source = Path(input_path).resolve()
    if not source.exists() or not source.is_file():
        raise ValueError(f"Figure input not found: {source}")
    suffix = source.suffix.lower()
    if suffix in SUPPORTED_VECTOR_SUFFIXES:
        document = _svg_document(source)
    elif suffix in SUPPORTED_RASTER_SUFFIXES:
        document = _raster_document(source, options)
    else:
        supported = ", ".join(sorted(SUPPORTED_VECTOR_SUFFIXES | SUPPORTED_RASTER_SUFFIXES))
        raise ValueError(f"Unsupported figure format {suffix or '<none>'}; supported: {supported}")
    if options.strict and document.warnings:
        raise ValueError("Strict conversion rejected warnings: " + "; ".join(document.warnings))
    return document


def _normalize_commands(
    commands: Iterable[dict[str, float | str | None]],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, float | str | None]]:
    min_x, min_y, max_x, max_y = bbox
    width = max(max_x - min_x, 1e-6)
    height = max(max_y - min_y, 1e-6)
    normalized: list[dict[str, float | str | None]] = []
    for source in commands:
        item = dict(source)
        for key, origin, span in (
            ("x", min_x, width),
            ("x1", min_x, width),
            ("x2", min_x, width),
            ("y", min_y, height),
            ("y1", min_y, height),
            ("y2", min_y, height),
        ):
            if item.get(key) is not None:
                item[key] = (float(item[key]) - origin) / span
        normalized.append(item)
    return normalized


def figure_to_slide_spec(
    document: FigureDocument,
    options: FigureConversionOptions | None = None,
) -> SlideSpec:
    options = options or FigureConversionOptions()
    options.validate()
    min_x, min_y, max_x, max_y = document.bbox
    source_width = max(max_x - min_x, 1e-6)
    source_height = max(max_y - min_y, 1e-6)
    usable_width = options.canvas_width - options.padding * 2
    usable_height = options.canvas_height - options.padding * 2
    if options.fit == "contain":
        scale = min(usable_width / source_width, usable_height / source_height)
        scale_x = scale_y = scale
    else:
        scale_x = usable_width / source_width
        scale_y = usable_height / source_height
    fitted_width = source_width * scale_x
    fitted_height = source_height * scale_y
    offset_x = (options.canvas_width - fitted_width) / 2 - min_x * scale_x
    offset_y = (options.canvas_height - fitted_height) / 2 - min_y * scale_y

    elements: list[PathElement] = []
    for layer, item in enumerate(document.paths, start=1):
        item_min_x, item_min_y, item_max_x, item_max_y = item.bbox
        item_width = max((item_max_x - item_min_x) * scale_x, 0.01)
        item_height = max((item_max_y - item_min_y) * scale_y, 0.01)
        stroke = dict(item.stroke)
        stroke["width_px"] = float(stroke.get("width_px", 0.0)) * (scale_x + scale_y) / 2
        elements.append(
            PathElement.model_validate(
                {
                    "kind": "path",
                    "id": item.id,
                    "name": item.name,
                    "layer": layer,
                    "group_id": "converted-figure",
                    "bounds": {
                        "x": item_min_x * scale_x + offset_x,
                        "y": item_min_y * scale_y + offset_y,
                        "width": item_width,
                        "height": item_height,
                    },
                    "rotation_deg": 0,
                    "fill": FillSpec.model_validate(item.fill).model_dump(mode="json"),
                    "stroke": StrokeSpec.model_validate(stroke).model_dump(mode="json"),
                    "commands": _normalize_commands(item.commands, item.bbox),
                }
            )
        )

    return SlideSpec.model_validate(
        {
            "version": "1.0",
            "source_width": options.canvas_width,
            "source_height": options.canvas_height,
            "background": {
                "kind": "solid",
                "color": _hex_color(options.background_color, "#FFFFFF"),
                "opacity": 1.0,
                "angle_deg": None,
                "stops": [],
            },
            "components": [],
            "elements": [element.model_dump(mode="json") for element in elements],
            "reconstruction_notes": [
                f"Deterministic {document.input_type} figure conversion",
                f"{len(elements)} native PowerPoint custom shape(s)",
                *document.warnings,
            ],
        }
    )


def _render_reference_png(
    source: Path,
    document: FigureDocument,
    initial_spec: SlideSpec,
    output_path: Path,
    *,
    options: FigureConversionOptions,
    workspace: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if document.input_type == "svg":
        reference_pptx = workspace / "reference-vector.pptx"
        render_pptx(initial_spec, reference_pptx, timeout_seconds=options.timeout_seconds)
        return render_first_slide(
            reference_pptx,
            output_path,
            dpi=96,
            timeout_seconds=options.timeout_seconds,
        )

    with Image.open(source) as image:
        original = image.convert("RGBA")
        pixels = np.asarray(original, dtype=np.int16)
        background_rgb = np.asarray(ImageColor.getrgb(options.background_color), dtype=np.int16)
        color_distance = np.max(
            np.abs(pixels[:, :, :3] - background_rgb[None, None, :]),
            axis=2,
        )
        foreground = (pixels[:, :, 3] >= options.alpha_threshold) & (color_distance >= 3)
        foreground_points = np.argwhere(foreground)
        if foreground_points.size == 0:
            raise ValueError("Raster reference contains no foreground geometry")
        min_y, min_x = foreground_points.min(axis=0)
        max_y, max_x = foreground_points.max(axis=0)
        crop_box = (
            max(0, int(min_x)),
            max(0, int(min_y)),
            min(original.width, int(max_x) + 1),
            min(original.height, int(max_y) + 1),
        )
        cropped = original.crop(crop_box)
        usable_width = options.canvas_width - options.padding * 2
        usable_height = options.canvas_height - options.padding * 2
        if options.fit == "contain":
            scale = min(usable_width / cropped.width, usable_height / cropped.height)
            target_width = max(1, int(round(cropped.width * scale)))
            target_height = max(1, int(round(cropped.height * scale)))
        else:
            target_width = max(1, int(round(usable_width)))
            target_height = max(1, int(round(usable_height)))
        target_x0 = (options.canvas_width - target_width) / 2
        target_y0 = (options.canvas_height - target_height) / 2
        resized = cropped.resize((target_width, target_height), Image.Resampling.LANCZOS)
        background = (*ImageColor.getrgb(options.background_color), 255)
        canvas = Image.new("RGBA", (options.canvas_width, options.canvas_height), background)
        canvas.alpha_composite(resized, (int(round(target_x0)), int(round(target_y0))))
        canvas.convert("RGB").save(output_path)
    return output_path


def _deterministic_trace_proposals(
    source: Path,
    document: FigureDocument,
    options: FigureConversionOptions,
) -> list[tuple[str, SlideSpec]]:
    if document.input_type != "raster":
        return []

    settings = [
        (
            max(0.6, options.simplify * 0.5),
            max(0.8, options.curve_error * 0.5) if options.curve_error > 0 else 0.0,
            options.background_threshold,
        ),
        (min(options.simplify, 2.0), min(options.curve_error, 4.0), min(options.background_threshold, 8.0)),
        (min(options.simplify, 2.0), min(options.curve_error, 4.0), min(options.background_threshold, 4.0)),
        (min(options.simplify, 1.25), min(options.curve_error, 1.75), min(options.background_threshold, 6.0)),
    ]
    current_setting = (
        round(options.simplify, 6),
        round(options.curve_error, 6),
        round(options.background_threshold, 6),
    )
    seen = {current_setting}
    expected_ids = [path.id for path in document.paths]
    proposals: list[tuple[str, SlideSpec]] = []
    for simplify, curve_error, background_threshold in settings:
        setting = (
            round(simplify, 6),
            round(curve_error, 6),
            round(background_threshold, 6),
        )
        if setting in seen:
            continue
        seen.add(setting)
        proposal_options = replace(
            options,
            simplify=simplify,
            curve_error=curve_error,
            background_threshold=background_threshold,
            refine_mode="none",
            render_preview=False,
            api_key=None,
        )
        proposal_document = load_figure(source, proposal_options)
        if [path.id for path in proposal_document.paths] != expected_ids:
            continue
        proposals.append(
            (
                (
                    f"retrace:simplify={simplify:.3g},curve_error={curve_error:.3g},"
                    f"background_threshold={background_threshold:.3g}"
                ),
                figure_to_slide_spec(proposal_document, proposal_options),
            )
        )
    return proposals


def _candidate_rank(
    geometry: dict[str, object],
    style: dict[str, object],
    *,
    use_reference_style: bool = True,
) -> tuple[float, float, float, float, float]:
    geometry_score = float(geometry["geometry_score"])
    local_score = float(geometry["worst_local_iou"])
    color_score = float(style["foreground_color_similarity"])
    gradient_score = float(style["gradient_profile_similarity"])
    critical_quality = (
        min(geometry_score, local_score, color_score, gradient_score)
        if use_reference_style
        else min(geometry_score, local_score)
    )
    return (
        critical_quality,
        local_score,
        geometry_score,
        color_score,
        gradient_score,
    )


def _gradient_calibration_proposal(
    current_spec: SlideSpec,
    reference_png: Path,
    candidate_png: Path,
    *,
    background_color: str,
) -> SlideSpec | None:
    gradient_paths = [
        element
        for element in current_spec.elements
        if isinstance(element, PathElement) and element.fill.kind == "linear_gradient"
    ]
    if len(gradient_paths) != 1 or len(gradient_paths[0].fill.stops) < 2:
        return None

    with Image.open(reference_png) as reference_image, Image.open(candidate_png) as candidate_image:
        reference = reference_image.convert("RGB")
        candidate = candidate_image.convert("RGB")
        if candidate.size != reference.size:
            candidate = candidate.resize(reference.size, Image.Resampling.LANCZOS)
        reference_array = np.asarray(reference, dtype=np.float64) / 255.0
        candidate_array = np.asarray(candidate, dtype=np.float64) / 255.0

    background = np.asarray(ImageColor.getrgb(background_color), dtype=np.float64) / 255.0
    reference_mask = np.max(np.abs(reference_array - background[None, None, :]), axis=2) >= 5 / 255
    candidate_mask = np.max(np.abs(candidate_array - background[None, None, :]), axis=2) >= 5 / 255
    common = np.logical_and(reference_mask, candidate_mask)
    interior = np.asarray(
        Image.fromarray((common.astype(np.uint8) * 255), mode="L").filter(
            ImageFilter.MinFilter(5)
        ),
        dtype=np.uint8,
    ) > 0
    if interior.sum() < 256:
        return None

    path = gradient_paths[0]
    stops = sorted(path.fill.stops, key=lambda stop: stop.position)
    positions = np.asarray([stop.position for stop in stops], dtype=np.float64)
    colors = np.asarray(
        [ImageColor.getrgb(stop.color) for stop in stops],
        dtype=np.float64,
    ) / 255.0
    palette_positions = np.linspace(0.0, 1.0, 257)
    palette = np.column_stack(
        [np.interp(palette_positions, positions, colors[:, channel]) for channel in range(3)]
    )
    candidate_values = candidate_array[interior]
    reference_values = reference_array[interior]
    inferred_indices: list[np.ndarray] = []
    batch_size = 4096
    for start in range(0, len(candidate_values), batch_size):
        batch = candidate_values[start : start + batch_size]
        distances = np.square(batch[:, None, :] - palette[None, :, :]).sum(axis=2)
        inferred_indices.append(np.argmin(distances, axis=1))
    inferred_positions = palette_positions[np.concatenate(inferred_indices)]

    calibrated_colors: list[str] = []
    selection_count = max(96, len(inferred_positions) // 24)
    for position in positions:
        distance = np.abs(inferred_positions - position)
        count = min(selection_count, len(distance))
        selected = np.argpartition(distance, count - 1)[:count]
        median = np.median(reference_values[selected], axis=0)
        calibrated_colors.append(
            "#" + "".join(f"{int(round(channel * 255)):02X}" for channel in np.clip(median, 0, 1))
        )

    if all(
        calibrated.lower() == stop.color.lower()
        for calibrated, stop in zip(calibrated_colors, stops, strict=True)
    ):
        return None

    payload = current_spec.model_dump(mode="json")
    for element in payload["elements"]:
        if element["id"] != path.id:
            continue
        ordered_payload_stops = sorted(
            element["fill"]["stops"],
            key=lambda stop: float(stop["position"]),
        )
        for stop, color in zip(ordered_payload_stops, calibrated_colors, strict=True):
            stop["color"] = color
        element["fill"]["stops"] = ordered_payload_stops
    payload["reconstruction_notes"] = [
        *payload["reconstruction_notes"],
        "Deterministic foreground-only PowerPoint gradient calibration",
    ]
    return SlideSpec.model_validate(payload)


def _acceptance_checks(
    geometry: dict[str, object],
    style: dict[str, object],
    review: FigureReview,
    *,
    options: FigureConversionOptions,
    use_reference_style: bool,
) -> dict[str, bool]:
    figure_review = review
    return {
        "global_geometry": float(geometry["geometry_score"]) >= options.target_geometry_score,
        "worst_local_geometry": float(geometry["worst_local_iou"])
        >= options.target_local_geometry_score,
        "foreground_color": (
            float(style["foreground_color_similarity"])
            >= options.target_foreground_style_score
            if use_reference_style
            else True
        ),
        "gradient_profile": (
            float(style["gradient_profile_similarity"]) >= options.target_gradient_score
            if use_reference_style
            else True
        ),
        "llm_geometry": float(figure_review.geometry_score)
        >= options.target_geometry_score,
        "llm_local_detail": float(figure_review.local_detail_score)
        >= options.target_local_geometry_score,
        "llm_style": float(figure_review.style_score)
        >= max(options.target_foreground_style_score, options.target_gradient_score),
        "llm_color": float(figure_review.color_score)
        >= options.target_foreground_style_score,
        "llm_gradient": float(figure_review.gradient_score)
        >= options.target_gradient_score,
        "no_major_issues": not any(issue.severity == "major" for issue in figure_review.issues),
    }


def _run_llm_refinement(
    source: Path,
    document: FigureDocument,
    initial_spec: SlideSpec,
    *,
    options: FigureConversionOptions,
    workspace: Path,
) -> dict[str, Any]:
    context_reference = Path(options.context_reference).resolve() if options.context_reference else None
    if context_reference is not None and not context_reference.exists():
        raise ValueError(f"Context reference does not exist: {context_reference}")
    use_reference_style = context_reference is None
    reference_png = _render_reference_png(
        source,
        document,
        initial_spec,
        workspace / "reference.png",
        options=options,
        workspace=workspace,
    )
    current_spec = initial_spec
    best_spec: SlideSpec | None = None
    best_pptx: Path | None = None
    best_geometry: dict[str, object] | None = None
    best_style: dict[str, object] | None = None
    iteration_reports: list[dict[str, Any]] = []
    accepted = False
    stop_reason = "iteration_limit"

    for index in range(options.iterations + 1):
        iteration_dir = workspace / f"iteration-{index}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        candidate_pptx = render_pptx(
            current_spec,
            iteration_dir / "candidate.pptx",
            timeout_seconds=options.timeout_seconds,
        )
        candidate_png = render_first_slide(
            candidate_pptx,
            iteration_dir / "rendered.png",
            dpi=96,
            timeout_seconds=options.timeout_seconds,
        )
        geometry = compare_figure_geometry(
            reference_png,
            candidate_png,
            background_color=options.background_color,
        )
        style = compare_images(
            reference_png,
            candidate_png,
            background_color=options.background_color,
        )
        audit = audit_pptx(candidate_pptx, current_spec)
        review = review_figure(
            reference_png,
            candidate_png,
            current_spec,
            geometry_metrics=geometry,
            style_metrics=style,
            target_geometry_score=options.target_geometry_score,
            target_local_geometry_score=options.target_local_geometry_score,
            target_foreground_style_score=options.target_foreground_style_score,
            target_gradient_score=options.target_gradient_score,
            iteration=index,
            context_reference=context_reference,
            model=options.model,
            api_key=options.api_key,
            timeout_seconds=options.timeout_seconds,
            max_output_tokens=options.max_output_tokens,
        )

        corrected_command_count = sum(len(path.commands) for path in review.corrected_paths)
        review_summary = review.model_dump(mode="json", exclude={"corrected_paths"})
        entry: dict[str, Any] = {
            "iteration": index,
            "geometry": geometry,
            "style": style,
            "audit": audit,
            "llm_review": review_summary,
            "corrected_path_count": len(review.corrected_paths),
            "corrected_command_count": corrected_command_count,
        }
        acceptance_checks = _acceptance_checks(
            geometry,
            style,
            review,
            options=options,
            use_reference_style=use_reference_style,
        )
        entry["acceptance_checks"] = acceptance_checks
        iteration_reports.append(entry)

        current_rank = _candidate_rank(
            geometry,
            style,
            use_reference_style=use_reference_style,
        )
        best_rank = (
            _candidate_rank(
                best_geometry,
                best_style,
                use_reference_style=use_reference_style,
            )
            if best_geometry is not None and best_style is not None
            else (-1.0, -1.0, -1.0, -1.0, -1.0)
        )
        if current_rank > best_rank:
            best_spec = current_spec
            best_pptx = candidate_pptx
            best_geometry = geometry
            best_style = style

        if review.verdict == "accept" and all(acceptance_checks.values()):
            best_spec = current_spec
            best_pptx = candidate_pptx
            best_geometry = geometry
            best_style = style
            accepted = True
            stop_reason = "llm_and_local_quality_guards_accepted"
            break
        if index >= options.iterations:
            stop_reason = "iteration_limit"
            break
        proposals: list[tuple[str, SlideSpec]] = []
        try:
            proposals.append(("llm-native-path", apply_figure_review(current_spec, review)))
        except FigureRefinementError as error:
            entry["llm_correction_rejected"] = str(error)
        if use_reference_style:
            gradient_proposal = _gradient_calibration_proposal(
                current_spec,
                reference_png,
                candidate_png,
                background_color=options.background_color,
            )
            if gradient_proposal is not None:
                proposals.append(("deterministic-gradient-calibration", gradient_proposal))
        proposals.extend(_deterministic_trace_proposals(source, document, options))

        proposal_reports: list[dict[str, Any]] = []
        proposal_candidates: list[
            tuple[str, SlideSpec, tuple[float, float, float, float, float], int]
        ] = []
        seen_specs: set[str] = set()
        for proposal_index, (proposal_name, proposal_spec) in enumerate(proposals):
            fingerprint = proposal_spec.model_dump_json()
            if fingerprint in seen_specs:
                continue
            seen_specs.add(fingerprint)
            proposal_dir = iteration_dir / f"proposal-{proposal_index}"
            proposal_dir.mkdir(parents=True, exist_ok=True)
            proposal_pptx = render_pptx(
                proposal_spec,
                proposal_dir / "candidate.pptx",
                timeout_seconds=options.timeout_seconds,
            )
            proposal_png = render_first_slide(
                proposal_pptx,
                proposal_dir / "rendered.png",
                dpi=96,
                timeout_seconds=options.timeout_seconds,
            )
            proposal_geometry = compare_figure_geometry(
                reference_png,
                proposal_png,
                background_color=options.background_color,
            )
            proposal_style = compare_images(
                reference_png,
                proposal_png,
                background_color=options.background_color,
            )
            proposal_rank = _candidate_rank(
                proposal_geometry,
                proposal_style,
                use_reference_style=use_reference_style,
            )
            command_count = sum(
                len(path.commands)
                for path in proposal_spec.elements
                if isinstance(path, PathElement)
            )
            proposal_reports.append(
                {
                    "name": proposal_name,
                    "geometry": proposal_geometry,
                    "style_similarity_score": proposal_style["similarity_score"],
                    "foreground_color_similarity": proposal_style[
                        "foreground_color_similarity"
                    ],
                    "gradient_profile_similarity": proposal_style[
                        "gradient_profile_similarity"
                    ],
                    "critical_quality_score": proposal_rank[0],
                    "command_count": command_count,
                    "selected": False,
                }
            )
            proposal_candidates.append((proposal_name, proposal_spec, proposal_rank, command_count))

        current_command_count = sum(
            len(path.commands)
            for path in current_spec.elements
            if isinstance(path, PathElement)
        )
        selection_pool = [
            ("current", current_spec, current_rank, current_command_count),
            *proposal_candidates,
        ]
        viable_context_corrections = [
            candidate
            for candidate in proposal_candidates
            if not use_reference_style
            and candidate[0] == "llm-native-path"
            and candidate[2][2] >= options.target_geometry_score
            and candidate[2][1] >= options.target_local_geometry_score
        ]
        if viable_context_corrections:
            selected_name, selected_spec, _, _ = max(
                viable_context_corrections,
                key=lambda candidate: candidate[2],
            )
            entry["selection_policy"] = (
                "original-context LLM style correction retained after global/local geometry guards"
            )
        else:
            best_proposal_quality = max(candidate[2][0] for candidate in selection_pool)
            near_best = [
                candidate
                for candidate in selection_pool
                if candidate[2][0] >= best_proposal_quality - 0.005
            ]
            selected_name, selected_spec, _, _ = min(
                near_best,
                key=lambda candidate: (
                    candidate[3],
                    -candidate[2][0],
                    -candidate[2][1],
                ),
            )
        if selected_name == "current":
            entry["proposals"] = proposal_reports
            entry["selection_policy"] = (
                "minimum edit points within 0.005 of best critical local/style quality"
            )
            stop_reason = "no_candidate_passed_or_improved_local_quality_guards"
            break
        for proposal_report in proposal_reports:
            proposal_report["selected"] = proposal_report["name"] == selected_name
        entry["proposals"] = proposal_reports
        entry.setdefault(
            "selection_policy",
            "minimum edit points within 0.005 of best critical local/style quality",
        )
        entry["selected_correction"] = selected_name
        current_spec = selected_spec

    if best_spec is None or best_pptx is None or best_geometry is None or best_style is None:
        raise ValueError("LLM refinement produced no valid editable candidate")
    return {
        "best_spec": best_spec,
        "best_pptx": best_pptx,
        "best_geometry": best_geometry,
        "best_style": best_style,
        "reference": reference_png,
        "iterations": iteration_reports,
        "accepted": accepted,
        "stop_reason": stop_reason,
        "context_reference": str(context_reference) if context_reference else None,
    }


def convert_figure(
    input_path: str | Path,
    output_path: str | Path,
    *,
    options: FigureConversionOptions | None = None,
    spec_path: str | Path | None = None,
    report_path: str | Path | None = None,
    preview_path: str | Path | None = None,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    options = options or FigureConversionOptions()
    options.validate()
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    if output.suffix.lower() != ".pptx":
        raise ValueError("Output path must end in .pptx")
    spec_output = Path(spec_path).resolve() if spec_path else output.with_suffix(".shape.json")
    report_output = Path(report_path).resolve() if report_path else output.with_suffix(".report.json")
    preview_output = Path(preview_path).resolve() if preview_path else output.with_suffix(".png")

    document = load_figure(source, options)
    if options.seed_spec:
        seed_path = Path(options.seed_spec).resolve()
        if not seed_path.exists():
            raise ValueError(f"Seed spec does not exist: {seed_path}")
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
        initial_spec = SlideSpec.model_validate(seed_payload.get("spec", seed_payload))
    else:
        initial_spec = figure_to_slide_spec(document, options)
    output.parent.mkdir(parents=True, exist_ok=True)

    workspace_context = (
        nullcontext(str(Path(workdir).resolve()))
        if workdir
        else tempfile.TemporaryDirectory(prefix="editable-figure-refine-")
    )
    with workspace_context as workspace_value:
        workspace = Path(workspace_value)
        workspace.mkdir(parents=True, exist_ok=True)
        refinement: dict[str, Any] | None = None
        if options.refine_mode == "llm":
            refinement = _run_llm_refinement(
                source,
                document,
                initial_spec,
                options=options,
                workspace=workspace,
            )
            spec = refinement["best_spec"]
            shutil.copy2(refinement["best_pptx"], output)
        elif options.refine_mode == "optimize":
            if not options.context_reference:
                raise ValueError("optimize mode requires context_reference")
            if options.context_figure_bbox is None:
                raise ValueError("optimize mode requires context_figure_bbox")
            from .figure_optimizer import run_target_optimizer

            reference_png = _render_reference_png(
                source,
                document,
                initial_spec,
                workspace / "reference.png",
                options=options,
                workspace=workspace,
            )
            refinement = run_target_optimizer(
                reference_png,
                options.context_reference,
                initial_spec,
                context_figure_bbox=options.context_figure_bbox,
                workspace=workspace / "optimizer",
                background_color=options.background_color,
                model=options.model,
                api_key=options.api_key,
                timeout_seconds=options.timeout_seconds,
                max_output_tokens=options.max_output_tokens,
                steps=options.optimizer_steps,
                patience=options.optimizer_patience,
                min_improvement=options.optimizer_min_improvement,
                llm_interval=options.optimizer_llm_interval,
                target_geometry_score=options.target_geometry_score,
                target_local_geometry_score=options.target_local_geometry_score,
            )
            spec = refinement["best_spec"]
            shutil.copy2(refinement["best_pptx"], output)
        else:
            spec = initial_spec
            render_pptx(spec, output, timeout_seconds=options.timeout_seconds)

        spec_output.parent.mkdir(parents=True, exist_ok=True)
        spec_output.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")

        render_error: str | None = None
        rendered_preview: str | None = None
        if options.render_preview:
            try:
                render_first_slide(
                    output,
                    preview_output,
                    dpi=96,
                    timeout_seconds=options.timeout_seconds,
                )
                rendered_preview = str(preview_output)
            except Exception as error:
                render_error = str(error)

        audit = audit_pptx(output, spec)
        expected_paths = len(document.paths)
        if audit["custom_geometry_paths"] < expected_paths:
            raise ValueError(
                f"Editable-shape audit failed: expected {expected_paths} custom geometries, "
                f"found {audit['custom_geometry_paths']}"
            )
        if audit["picture_objects"] or audit["media_files"]:
            raise ValueError("Editable-shape audit failed: raster picture objects were embedded")
        has_cubic_source = any(
            command["op"] == "C"
            for path in document.paths
            for command in path.commands
        )
        if has_cubic_source and audit["cubic_bezier_segments"] == 0:
            raise ValueError("Editable-shape audit failed: source curves were not exported as cubic Béziers")

        refinement_report = None
        if refinement is not None and options.refine_mode == "llm":
            refinement_report = {
                "mode": "llm",
                "model": options.model,
                "target_geometry_score": options.target_geometry_score,
                "target_local_geometry_score": options.target_local_geometry_score,
                "target_foreground_style_score": options.target_foreground_style_score,
                "target_gradient_score": options.target_gradient_score,
                "context_reference": refinement["context_reference"],
                "seed_spec": str(Path(options.seed_spec).resolve()) if options.seed_spec else None,
                "accepted": refinement["accepted"],
                "stop_reason": refinement["stop_reason"],
                "best_geometry": refinement["best_geometry"],
                "best_style": refinement["best_style"],
                "iterations": refinement["iterations"],
            }
        elif refinement is not None and options.refine_mode == "optimize":
            refinement_report = {
                "mode": "optimize",
                "model": options.model,
                "renderer": refinement["renderer"],
                "seed_spec": str(Path(options.seed_spec).resolve()) if options.seed_spec else None,
                "context_reference": refinement["context_reference"],
                "context_figure_bbox": refinement["context_figure_bbox"],
                "converged": refinement["converged"],
                "stop_reason": refinement["stop_reason"],
                "best_objective": refinement["best_objective"],
                "best_geometry": refinement["best_geometry"],
                "best_style": refinement["best_style"],
                "best_context_style": refinement["best_context_style"],
                "history": refinement["history"],
                "iterations": refinement["iterations"],
            }

        report: dict[str, Any] = {
            "input": str(source),
            "output": str(output),
            "input_type": document.input_type,
            "canvas": [options.canvas_width, options.canvas_height],
            "fit": options.fit,
            "padding": options.padding,
            "shape_count": len(document.paths),
            "path_command_count": sum(
                len(element.commands)
                for element in spec.elements
                if isinstance(element, PathElement)
            ),
            "warnings": document.warnings,
            "refinement": refinement_report,
            "audit": audit,
            "editable_contract": {
                "native_custom_geometry": audit["custom_geometry_paths"] >= expected_paths,
                "edit_points_available_in_powerpoint": audit["custom_geometry_paths"] >= expected_paths,
                "no_embedded_raster": audit["picture_objects"] == 0 and audit["media_files"] == 0,
                "native_cubic_beziers": audit["cubic_bezier_segments"],
            },
            "spec": str(spec_output),
            "preview": rendered_preview,
            "preview_error": render_error,
        }
        write_report(report, report_output)
        report["report"] = str(report_output)
        return report


__all__ = [
    "FigureConversionOptions",
    "FigureDocument",
    "FigurePath",
    "convert_figure",
    "figure_to_slide_spec",
    "load_figure",
]
