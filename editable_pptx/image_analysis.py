from __future__ import annotations

import base64
import mimetypes
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from .models import (
    ComponentInstanceElement,
    FillSpec,
    ImageElement,
    LineElement,
    SlideSpec,
    StrokeSpec,
)


@dataclass(frozen=True)
class ImageFacts:
    width: int
    height: int
    aspect_ratio: float
    background_color: str
    dominant_colors: list[dict[str, object]]
    horizontal_rectangles: list[dict[str, object]]
    has_alpha: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _hex(rgb: tuple[int, int, int] | list[int] | np.ndarray) -> str:
    return "#" + "".join(f"{int(channel):02X}" for channel in rgb[:3])


def analyze_image(image_path: str | Path, palette_size: int = 12) -> ImageFacts:
    path = Path(image_path)
    with Image.open(path) as source:
        has_alpha = source.mode in {"RGBA", "LA"} or "transparency" in source.info
        image = source.convert("RGB")
        width, height = image.size

        array = np.asarray(image, dtype=np.uint8)
        border = np.concatenate(
            [
                array[0, :, :],
                array[-1, :, :],
                array[:, 0, :],
                array[:, -1, :],
            ],
            axis=0,
        )
        background = np.median(border, axis=0).round().astype(np.uint8)

        sample = image.copy()
        sample.thumbnail((512, 512), Image.Resampling.LANCZOS)
        quantized = sample.quantize(colors=palette_size, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        counts = quantized.getcolors(maxcolors=palette_size * 4) or []
        total = max(1, sum(count for count, _ in counts))
        dominant: list[dict[str, object]] = []
        for count, index in sorted(counts, reverse=True):
            offset = index * 3
            rgb = palette[offset : offset + 3]
            if len(rgb) != 3:
                continue
            dominant.append(
                {
                    "color": _hex(rgb),
                    "share": round(count / total, 4),
                }
            )

        rectangle_hints = _detect_horizontal_rectangles(array, dominant)

    return ImageFacts(
        width=width,
        height=height,
        aspect_ratio=round(width / height, 6),
        background_color=_hex(background),
        dominant_colors=dominant,
        horizontal_rectangles=rectangle_hints,
        has_alpha=has_alpha,
    )


def _detect_horizontal_rectangles(
    array: np.ndarray,
    dominant_colors: list[dict[str, object]],
    *,
    max_results: int = 100,
) -> list[dict[str, object]]:
    """Find repeated filled bars without OCR or a heavyweight CV dependency."""

    height, width, _ = array.shape
    min_width = max(20, round(width * 0.025))
    candidates: list[dict[str, int]] = []
    pixels = array.astype(np.int32)

    color_values = {str(entry["color"]).upper() for entry in dominant_colors}
    flat = array.reshape(-1, 3)
    saturated = flat[
        (np.ptp(flat.astype(np.int16), axis=1) >= 24)
        & (flat.astype(np.float32).mean(axis=1) < 240)
    ]
    if saturated.size:
        step = max(1, len(saturated) // 100_000)
        strip = Image.fromarray(saturated[::step].reshape(-1, 1, 3), mode="RGB")
        quantized = strip.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        for _, index in quantized.getcolors(maxcolors=32) or []:
            offset = index * 3
            rgb_value = palette[offset : offset + 3]
            if len(rgb_value) == 3:
                color_values.add(_hex(rgb_value))

    for color_value in color_values:
        value = color_value.lstrip("#")
        rgb = np.array([int(value[index : index + 2], 16) for index in (0, 2, 4)], dtype=np.int32)
        if int(rgb.max() - rgb.min()) < 24 or float(rgb.mean()) > 235:
            continue
        distance = np.sqrt(np.square(pixels - rgb).sum(axis=2))
        mask = distance <= 48

        active: list[dict[str, int]] = []
        finished: list[dict[str, int]] = []
        for y in range(height):
            xs = np.flatnonzero(mask[y])
            runs: list[tuple[int, int]] = []
            if xs.size:
                breaks = np.flatnonzero(np.diff(xs) > 1)
                starts = np.r_[0, breaks + 1]
                ends = np.r_[breaks, xs.size - 1]
                runs = [
                    (int(xs[start]), int(xs[end]))
                    for start, end in zip(starts, ends, strict=True)
                    if int(xs[end] - xs[start] + 1) >= min_width
                ]

            next_active: list[dict[str, int]] = []
            used: set[int] = set()
            for x0, x1 in runs:
                best_index = None
                best_delta = 10**9
                for index, rectangle in enumerate(active):
                    if index in used:
                        continue
                    delta = abs(rectangle["last_x0"] - x0) + abs(rectangle["last_x1"] - x1)
                    if delta <= 8 and delta < best_delta:
                        best_index = index
                        best_delta = delta
                if best_index is None:
                    next_active.append(
                        {"x0": x0, "x1": x1, "y0": y, "y1": y, "last_x0": x0, "last_x1": x1}
                    )
                else:
                    rectangle = active[best_index]
                    used.add(best_index)
                    rectangle["x0"] = min(rectangle["x0"], x0)
                    rectangle["x1"] = max(rectangle["x1"], x1)
                    rectangle["y1"] = y
                    rectangle["last_x0"] = x0
                    rectangle["last_x1"] = x1
                    next_active.append(rectangle)
            finished.extend(rectangle for index, rectangle in enumerate(active) if index not in used)
            active = next_active
        finished.extend(active)

        for rectangle in finished:
            rect_width = rectangle["x1"] - rectangle["x0"] + 1
            rect_height = rectangle["y1"] - rectangle["y0"] + 1
            if rect_height < 3 or rect_width / rect_height < 2.5:
                continue
            candidates.append(rectangle)

    def overlap_ratio(left: dict[str, int], right: dict[str, int]) -> float:
        x0 = max(left["x0"], right["x0"])
        y0 = max(left["y0"], right["y0"])
        x1 = min(left["x1"], right["x1"])
        y1 = min(left["y1"], right["y1"])
        if x1 < x0 or y1 < y0:
            return 0.0
        intersection = (x1 - x0 + 1) * (y1 - y0 + 1)
        left_area = (left["x1"] - left["x0"] + 1) * (left["y1"] - left["y0"] + 1)
        right_area = (right["x1"] - right["x0"] + 1) * (right["y1"] - right["y0"] + 1)
        return intersection / min(left_area, right_area)

    deduplicated: list[dict[str, int]] = []
    for rectangle in sorted(
        candidates,
        key=lambda item: (item["x1"] - item["x0"] + 1) * (item["y1"] - item["y0"] + 1),
        reverse=True,
    ):
        if any(overlap_ratio(rectangle, existing) >= 0.75 for existing in deduplicated):
            continue
        deduplicated.append(rectangle)

    results: list[dict[str, object]] = []
    for rectangle in sorted(deduplicated, key=lambda item: (item["y0"], item["x0"]))[:max_results]:
        crop = array[rectangle["y0"] : rectangle["y1"] + 1, rectangle["x0"] : rectangle["x1"] + 1]
        median = np.median(crop.reshape(-1, 3), axis=0).round().astype(np.uint8)
        results.append(
            {
                "x": rectangle["x0"],
                "y": rectangle["y0"],
                "width": rectangle["x1"] - rectangle["x0"] + 1,
                "height": rectangle["y1"] - rectangle["y0"] + 1,
                "color": _hex(median),
            }
        )
    return results


def image_data_url(image_path: str | Path) -> str:
    path = Path(image_path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def extract_image_assets(
    spec: SlideSpec,
    image_path: str | Path,
    asset_dir: str | Path,
) -> dict[str, str]:
    """Crop true raster regions into independent editable picture objects."""

    output_dir = Path(asset_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    assets: dict[str, str] = {}

    with Image.open(image_path) as source:
        source = source.convert("RGBA")
        max_w, max_h = source.size
        for element in spec.elements:
            if not isinstance(element, ImageElement):
                continue
            region = element.source_region
            left = max(0, min(max_w - 1, int(round(region.x))))
            top = max(0, min(max_h - 1, int(round(region.y))))
            right = max(left + 1, min(max_w, int(round(region.x + region.width))))
            bottom = max(top + 1, min(max_h, int(round(region.y + region.height))))
            crop = source.crop((left, top, right, bottom))
            crop = _remove_rasterized_overlays(crop, spec, element)
            output_path = output_dir / f"{safe_filename(element.id)}.png"
            crop.save(output_path, format="PNG")
            assets[element.id] = str(output_path.resolve())

    return assets


def _hex_rgb(value: str) -> np.ndarray:
    normalized = value.lstrip("#")
    return np.asarray(
        [int(normalized[index : index + 2], 16) for index in (0, 2, 4)],
        dtype=np.int16,
    )


def _visible_fill_colors(fill: FillSpec) -> list[str]:
    if fill.opacity <= 0 or fill.kind == "none":
        return []
    if fill.kind == "solid" and fill.color:
        return [fill.color]
    return [stop.color for stop in fill.stops if stop.opacity * fill.opacity > 0]


def _visible_stroke_colors(stroke: StrokeSpec) -> list[str]:
    return [stroke.color] if stroke.opacity > 0 and stroke.width_px > 0 else []


def _element_colors(spec: SlideSpec, element: object) -> list[str]:
    colors: list[str] = []
    color = getattr(element, "color", None)
    opacity = float(getattr(element, "opacity", 1))
    if isinstance(color, str) and opacity > 0:
        colors.append(color)
    fill = getattr(element, "fill", None)
    # Opaque native fills completely cover the same screenshot pixels. Only translucent
    # fills need cleaning; masking large opaque diagrams would be wasteful and less stable.
    if isinstance(fill, FillSpec) and fill.opacity < 0.98:
        colors.extend(_visible_fill_colors(fill))
    stroke = getattr(element, "stroke", None)
    if isinstance(stroke, StrokeSpec):
        colors.extend(_visible_stroke_colors(stroke))
    if isinstance(element, ComponentInstanceElement):
        component = next(
            (item for item in spec.components if item.id == element.component_id),
            None,
        )
        if component:
            for primitive in component.elements:
                colors.extend(_element_colors(spec, primitive))
    return list(dict.fromkeys(colors))


def _element_bbox(element: object) -> tuple[float, float, float, float] | None:
    bounds = getattr(element, "bounds", None)
    if bounds is not None:
        return bounds.x, bounds.y, bounds.x + bounds.width, bounds.y + bounds.height
    if isinstance(element, LineElement):
        pad = max(2.0, element.stroke.width_px * 2.0)
        return (
            min(element.x1, element.x2) - pad,
            min(element.y1, element.y2) - pad,
            max(element.x1, element.x2) + pad,
            max(element.y1, element.y2) + pad,
        )
    return None


def _inpaint_nearest_neighbors(array: np.ndarray, mask: np.ndarray) -> np.ndarray:
    result = array.astype(np.float32)
    unknown = mask.copy()
    height, width = unknown.shape
    for _ in range(height + width):
        if not unknown.any():
            break
        valid = ~unknown
        sums = np.zeros_like(result)
        counts = np.zeros((height, width), dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            target_y = slice(max(0, dy), min(height, height + dy))
            target_x = slice(max(0, dx), min(width, width + dx))
            source_y = slice(max(0, -dy), min(height, height - dy))
            source_x = slice(max(0, -dx), min(width, width - dx))
            neighbor_valid = valid[source_y, source_x]
            sums[target_y, target_x] += result[source_y, source_x] * neighbor_valid[..., None]
            counts[target_y, target_x] += neighbor_valid
        fillable = unknown & (counts > 0)
        if not fillable.any():
            break
        result[fillable] = sums[fillable] / counts[fillable][:, None]
        unknown[fillable] = False
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def _remove_rasterized_overlays(
    crop: Image.Image,
    spec: SlideSpec,
    image_element: ImageElement,
) -> Image.Image:
    """Remove screenshot text/vector pixels duplicated by higher native objects.

    The operation masks only pixels close to the declared overlay colors, then fills the
    thin masked strokes from adjacent photo pixels. Opaque native shapes subsequently hide
    their own source regions; this primarily prevents duplicated text and logos.
    """

    image_box = image_element.bounds
    image_x1 = image_box.x + image_box.width
    image_y1 = image_box.y + image_box.height
    array = np.asarray(crop.convert("RGB"), dtype=np.uint8).copy()
    combined_mask = np.zeros(array.shape[:2], dtype=bool)
    for overlay in spec.elements:
        if overlay.id == image_element.id or overlay.layer <= image_element.layer:
            continue
        if overlay.kind not in {"text", "component", "line"}:
            # Opaque paths and shapes cover their screenshot versions. Cleaning their
            # entire geometric bounds risks removing unrelated photographic detail.
            continue
        bbox = _element_bbox(overlay)
        colors = _element_colors(spec, overlay)
        if bbox is None or not colors:
            continue
        x0 = max(image_box.x, bbox[0])
        y0 = max(image_box.y, bbox[1])
        x1 = min(image_x1, bbox[2])
        y1 = min(image_y1, bbox[3])
        if x1 <= x0 or y1 <= y0:
            continue
        left = max(0, int((x0 - image_box.x) / image_box.width * array.shape[1]) - 2)
        top = max(0, int((y0 - image_box.y) / image_box.height * array.shape[0]) - 2)
        right = min(array.shape[1], int(np.ceil((x1 - image_box.x) / image_box.width * array.shape[1])) + 2)
        bottom = min(array.shape[0], int(np.ceil((y1 - image_box.y) / image_box.height * array.shape[0])) + 2)
        region = array[top:bottom, left:right].astype(np.int16)
        local_mask = np.zeros(region.shape[:2], dtype=bool)
        for overlay_color in colors:
            delta = region.astype(np.int32) - _hex_rgb(overlay_color).astype(np.int32)
            distance = np.sqrt(np.square(delta).sum(axis=2))
            local_mask |= distance <= 52
        combined_mask[top:bottom, left:right] |= local_mask

    if not combined_mask.any():
        return crop
    expanded = np.asarray(
        Image.fromarray(combined_mask.astype(np.uint8) * 255, mode="L").filter(
            ImageFilter.MaxFilter(3)
        ),
        dtype=np.uint8,
    ) > 0
    cleaned = _inpaint_nearest_neighbors(array, expanded)
    return Image.fromarray(cleaned, mode="RGB").convert("RGBA")


def safe_filename(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return normalized.strip("_") or "asset"
