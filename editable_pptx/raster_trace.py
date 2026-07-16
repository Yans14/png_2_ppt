from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import hypot
from pathlib import Path

import numpy as np
from PIL import Image


Point = tuple[float, float]


@dataclass(frozen=True)
class TracedLayer:
    color: str
    opacity: float
    contours: list[list[Point]]
    source_pixels: int
    gradient: dict[str, object] | None = None


@dataclass(frozen=True)
class RasterTraceResult:
    width: int
    height: int
    layers: list[TracedLayer]
    background_color: str
    warnings: list[str]


def _hex(rgb: np.ndarray | tuple[int, int, int] | list[int]) -> str:
    return "#" + "".join(f"{int(channel):02X}" for channel in rgb[:3])


def _signed_area(points: list[Point]) -> float:
    return sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(points, points[1:] + points[:1], strict=True)
    ) / 2


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return hypot(point[0] - start[0], point[1] - start[1])
    numerator = abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0])
    return numerator / hypot(dx, dy)


def _rdp(points: list[Point], epsilon: float) -> list[Point]:
    if len(points) <= 2:
        return points
    start = points[0]
    end = points[-1]
    best_distance = -1.0
    best_index = 0
    for index, point in enumerate(points[1:-1], start=1):
        distance = _point_line_distance(point, start, end)
        if distance > best_distance:
            best_distance = distance
            best_index = index
    if best_distance <= epsilon:
        return [start, end]
    left = _rdp(points[: best_index + 1], epsilon)
    right = _rdp(points[best_index:], epsilon)
    return left[:-1] + right


def _remove_collinear(points: list[Point]) -> list[Point]:
    if len(points) < 4:
        return points
    result: list[Point] = []
    for index, point in enumerate(points):
        previous = points[index - 1]
        following = points[(index + 1) % len(points)]
        cross = (
            (point[0] - previous[0]) * (following[1] - point[1])
            - (point[1] - previous[1]) * (following[0] - point[0])
        )
        if abs(cross) > 1e-9:
            result.append(point)
    return result if len(result) >= 3 else points


def _simplify_closed(points: list[Point], epsilon: float) -> list[Point]:
    points = _remove_collinear(points)
    if epsilon <= 0 or len(points) <= 4:
        return points

    first = points[0]
    far_index = max(
        range(1, len(points)),
        key=lambda index: (points[index][0] - first[0]) ** 2 + (points[index][1] - first[1]) ** 2,
    )
    opposite = points[far_index]
    second_index = max(
        range(len(points)),
        key=lambda index: (points[index][0] - opposite[0]) ** 2 + (points[index][1] - opposite[1]) ** 2,
    )
    start_index, end_index = sorted((far_index, second_index))
    first_half = points[start_index : end_index + 1]
    second_half = points[end_index:] + points[: start_index + 1]
    simplified = _rdp(first_half, epsilon)[:-1] + _rdp(second_half, epsilon)[:-1]
    return _remove_collinear(simplified)


def _trace_loops(mask: np.ndarray) -> list[list[Point]]:
    height, width = mask.shape
    padded = np.pad(mask, 1, constant_values=False)
    top = mask & ~padded[:-2, 1:-1]
    right = mask & ~padded[1:-1, 2:]
    bottom = mask & ~padded[2:, 1:-1]
    left = mask & ~padded[1:-1, :-2]

    adjacency: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for y, x in zip(*np.nonzero(top), strict=True):
        adjacency[(int(x), int(y))].append((int(x + 1), int(y)))
    for y, x in zip(*np.nonzero(right), strict=True):
        adjacency[(int(x + 1), int(y))].append((int(x + 1), int(y + 1)))
    for y, x in zip(*np.nonzero(bottom), strict=True):
        adjacency[(int(x + 1), int(y + 1))].append((int(x), int(y + 1)))
    for y, x in zip(*np.nonzero(left), strict=True):
        adjacency[(int(x), int(y + 1))].append((int(x), int(y)))

    edge_count = sum(len(items) for items in adjacency.values())
    loops: list[list[Point]] = []
    direction_index = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}

    def pop_edge(start: tuple[int, int], previous_direction: int | None) -> tuple[int, int]:
        choices = adjacency[start]
        if previous_direction is None or len(choices) == 1:
            return choices.pop()
        preference = [
            (previous_direction + 1) % 4,
            previous_direction,
            (previous_direction + 3) % 4,
            (previous_direction + 2) % 4,
        ]
        ranked = {
            direction: rank
            for rank, direction in enumerate(preference)
        }
        best_index = min(
            range(len(choices)),
            key=lambda index: ranked.get(
                direction_index[
                    (choices[index][0] - start[0], choices[index][1] - start[1])
                ],
                99,
            ),
        )
        return choices.pop(best_index)

    consumed = 0
    while consumed < edge_count:
        start = next((point for point, targets in adjacency.items() if targets), None)
        if start is None:
            break
        loop: list[Point] = [start]
        current = start
        previous_direction: int | None = None
        for _ in range(edge_count + 1):
            if not adjacency[current]:
                break
            following = pop_edge(current, previous_direction)
            consumed += 1
            previous_direction = direction_index[(following[0] - current[0], following[1] - current[1])]
            current = following
            if current == start:
                break
            loop.append(current)
        if current == start and len(loop) >= 3:
            loops.append([(float(x), float(y)) for x, y in loop])
    return loops


def _fit_linear_gradient(
    rgb: np.ndarray,
    mask: np.ndarray,
    *,
    max_samples: int = 120_000,
) -> dict[str, object] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) < 64:
        return None
    step = max(1, len(xs) // max_samples)
    xs = xs[::step].astype(np.float64)
    ys = ys[::step].astype(np.float64)
    colors = rgb[np.nonzero(mask)][::step].astype(np.float64)
    width = max(1.0, float(rgb.shape[1] - 1))
    height = max(1.0, float(rgb.shape[0] - 1))
    coordinates = np.column_stack((xs / width, ys / height, np.ones_like(xs)))
    coefficients, _, _, _ = np.linalg.lstsq(coordinates, colors, rcond=None)
    predicted = coordinates @ coefficients
    centered = colors - colors.mean(axis=0)
    denominator = float(np.square(centered).sum())
    if denominator <= 1e-9:
        return None
    r_squared = 1.0 - float(np.square(colors - predicted).sum()) / denominator
    spatial = coefficients[:2, :]
    left, singular, _ = np.linalg.svd(spatial, full_matrices=False)
    if not len(singular) or singular[0] < 8 or r_squared < 0.45:
        return None
    direction = left[:, 0]
    projection = coordinates[:, :2] @ direction
    low = float(np.percentile(projection, 1))
    high = float(np.percentile(projection, 99))
    if high - low <= 1e-6:
        return None
    normalized = np.clip((projection - low) / (high - low), 0, 1)
    stops: list[dict[str, object]] = []
    for position in (0.0, 0.25, 0.5, 0.75, 1.0):
        distance = np.abs(normalized - position)
        count = max(16, min(len(distance), len(distance) // 20))
        selection = np.argpartition(distance, count - 1)[:count]
        median = np.median(colors[selection], axis=0).round().clip(0, 255).astype(np.uint8)
        stops.append(
            {
                "position": position,
                "color": _hex(median),
                "opacity": 1.0,
            }
        )
    first = np.asarray(_hex_to_rgb(stops[0]["color"]), dtype=np.float64)
    last = np.asarray(_hex_to_rgb(stops[-1]["color"]), dtype=np.float64)
    if float(np.linalg.norm(first - last)) < 24:
        return None
    return {
        "angle_deg": _angle_degrees(direction[1], direction[0]),
        "stops": stops,
        "r_squared": round(r_squared, 6),
    }


def _hex_to_rgb(value: object) -> tuple[int, int, int]:
    text = str(value).lstrip("#")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))


def _angle_degrees(y: float, x: float) -> float:
    return float(np.degrees(np.arctan2(y, x)) % 360)


def trace_raster(
    image_path: str | Path,
    *,
    max_colors: int = 1,
    background_threshold: float = 24,
    alpha_threshold: int = 8,
    simplify: float = 1.25,
    min_area: float = 12,
    max_points: int = 5000,
    max_dimension: int = 2048,
) -> RasterTraceResult:
    if max_colors < 1 or max_colors > 32:
        raise ValueError("max_colors must be between 1 and 32")
    if simplify < 0:
        raise ValueError("simplify must be non-negative")
    if max_points < 16:
        raise ValueError("max_points must be at least 16")

    source_path = Path(image_path)
    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        original_width, original_height = rgba.size
        scale_back = 1.0
        if max(rgba.size) > max_dimension:
            scale = max_dimension / max(rgba.size)
            resized = (
                max(1, round(rgba.width * scale)),
                max(1, round(rgba.height * scale)),
            )
            rgba = rgba.resize(resized, Image.Resampling.LANCZOS)
            scale_back = original_width / rgba.width

        array = np.asarray(rgba, dtype=np.uint8)
        rgb = array[:, :, :3]
        alpha = array[:, :, 3]
        border = np.concatenate(
            [rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]],
            axis=0,
        )
        background = np.median(border, axis=0).round().astype(np.uint8)
        transparent_border = np.concatenate(
            [alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]],
            axis=0,
        )
        uses_alpha = bool(np.percentile(transparent_border, 25) < 250 or alpha.min() < 128)
        if uses_alpha:
            foreground = alpha >= alpha_threshold
        else:
            delta = rgb.astype(np.int32) - background.astype(np.int32)
            foreground = np.sqrt(np.square(delta).sum(axis=2)) >= background_threshold

        foreground_pixels = int(foreground.sum())
        if foreground_pixels == 0:
            raise ValueError("No foreground geometry detected in raster input")

        warnings: list[str] = []
        if scale_back != 1:
            warnings.append(
                f"Raster tracing used a {rgba.width}x{rgba.height} working copy; coordinates were scaled back."
            )

        layers: list[TracedLayer] = []
        if max_colors == 1:
            masks = [(foreground, np.median(rgb[foreground], axis=0).round().astype(np.uint8))]
        else:
            quantized_source = Image.fromarray(rgb, mode="RGB").quantize(
                colors=max_colors + 1,
                method=Image.Quantize.MEDIANCUT,
            )
            indices = np.asarray(quantized_source, dtype=np.uint8)
            palette = quantized_source.getpalette() or []
            masks = []
            for index, count in sorted(
                (
                    (int(index), int(((indices == index) & foreground).sum()))
                    for index in np.unique(indices[foreground])
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:max_colors]:
                if count < min_area:
                    continue
                offset = index * 3
                color_value = np.asarray(palette[offset : offset + 3], dtype=np.uint8)
                masks.append(((indices == index) & foreground, color_value))

        for layer_index, (mask, color_value) in enumerate(masks):
            loops = [
                loop
                for loop in _trace_loops(mask)
                if abs(_signed_area(loop)) >= min_area
            ]
            if not loops:
                continue

            epsilon = simplify
            simplified = [_simplify_closed(loop, epsilon) for loop in loops]
            point_count = sum(len(loop) for loop in simplified)
            while point_count > max_points and epsilon < max(rgba.size):
                epsilon = max(0.5, epsilon * 1.35)
                simplified = [_simplify_closed(loop, epsilon) for loop in loops]
                point_count = sum(len(loop) for loop in simplified)
            if epsilon > simplify:
                warnings.append(
                    f"Layer {layer_index + 1} simplification increased to {epsilon:.2f}px to respect max_points."
                )

            scaled = [
                [(x * scale_back, y * scale_back) for x, y in loop]
                for loop in simplified
                if len(loop) >= 3
            ]
            if not scaled:
                continue
            layer_alpha = float(np.median(alpha[mask]) / 255.0) if uses_alpha else 1.0
            gradient = _fit_linear_gradient(rgb, mask)
            layers.append(
                TracedLayer(
                    color=_hex(color_value),
                    opacity=round(layer_alpha, 6),
                    contours=scaled,
                    source_pixels=int(mask.sum()),
                    gradient=gradient,
                )
            )

    if not layers:
        raise ValueError("Raster input produced no traceable contour")
    return RasterTraceResult(
        width=original_width,
        height=original_height,
        layers=layers,
        background_color=_hex(background),
        warnings=warnings,
    )
