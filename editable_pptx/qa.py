from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor, ImageFilter

from .models import LineElement, SlideSpec


class QualityCheckError(RuntimeError):
    pass


def _find_binary(name: str) -> str:
    binary = shutil.which(name)
    if binary:
        return binary
    candidates = {
        "soffice": [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/usr/bin/soffice",
        ],
        "pdftoppm": ["/usr/local/bin/pdftoppm", "/opt/homebrew/bin/pdftoppm", "/usr/bin/pdftoppm"],
    }
    for candidate in candidates.get(name, []):
        if Path(candidate).exists():
            return candidate
    raise QualityCheckError(f"Required renderer binary not found: {name}")


def render_first_slide(
    pptx_path: str | Path,
    output_png: str | Path,
    *,
    dpi: int = 96,
    timeout_seconds: int = 120,
) -> Path:
    source = Path(pptx_path).resolve()
    output = Path(output_png).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    soffice = _find_binary("soffice")
    pdftoppm = _find_binary("pdftoppm")

    with tempfile.TemporaryDirectory(prefix="editable-pptx-qa-") as temp_dir:
        temp = Path(temp_dir)
        profile_uri = (temp / "libreoffice-profile").resolve().as_uri()
        converted = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(temp),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        pdf = temp / f"{source.stem}.pdf"
        if converted.returncode != 0 or not pdf.exists():
            message = (converted.stderr or converted.stdout or "LibreOffice conversion failed").strip()
            raise QualityCheckError(message)
        rasterized = subprocess.run(
            [
                pdftoppm,
                "-f",
                "1",
                "-singlefile",
                "-png",
                "-r",
                str(dpi),
                str(pdf),
                str(temp / "slide"),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        rendered = temp / "slide.png"
        if rasterized.returncode != 0 or not rendered.exists():
            message = (rasterized.stderr or rasterized.stdout or "PDF rasterization failed").strip()
            raise QualityCheckError(message)
        shutil.copy2(rendered, output)
    return output


def compare_images(
    reference_path: str | Path,
    rendered_path: str | Path,
    *,
    background_color: str = "#FFFFFF",
    spec: SlideSpec | None = None,
) -> dict[str, object]:
    with Image.open(reference_path) as reference_image, Image.open(rendered_path) as rendered_image:
        reference = reference_image.convert("RGB")
        rendered = rendered_image.convert("RGB")
        original_render_size = rendered.size
        if rendered.size != reference.size:
            rendered = rendered.resize(reference.size, Image.Resampling.LANCZOS)

        reference_array = np.asarray(reference, dtype=np.float32) / 255.0
        rendered_array = np.asarray(rendered, dtype=np.float32) / 255.0
        difference = np.abs(reference_array - rendered_array)
        difference_map = difference.mean(axis=2)
        pixel_mae = float(difference.mean())
        pixel_p95 = float(np.percentile(difference, 95))

        reference_mask = _foreground_mask(reference.convert("RGBA"), background_color)
        rendered_mask = _foreground_mask(rendered.convert("RGBA"), background_color)
        common_foreground = np.logical_and(reference_mask, rendered_mask)
        eroded_common = np.asarray(
            Image.fromarray((common_foreground.astype(np.uint8) * 255), mode="L").filter(
                ImageFilter.MinFilter(5)
            ),
            dtype=np.uint8,
        ) > 0
        if not eroded_common.any():
            eroded_common = common_foreground

        if eroded_common.any():
            foreground_difference = difference[eroded_common]
            foreground_pixel_mae = float(foreground_difference.mean())
            foreground_pixel_p95 = float(np.percentile(foreground_difference, 95))
            reference_foreground = reference_array[eroded_common]
            rendered_foreground = rendered_array[eroded_common]
            reference_centered = reference_foreground - reference_foreground.mean(axis=0)
            rendered_centered = rendered_foreground - rendered_foreground.mean(axis=0)
            gradient_profile_mae = float(np.abs(reference_centered - rendered_centered).mean())
        else:
            foreground_pixel_mae = 1.0
            foreground_pixel_p95 = 1.0
            gradient_profile_mae = 1.0

        foreground_color_similarity = max(
            0.0,
            1.0 - foreground_pixel_mae * 2.0 - foreground_pixel_p95 * 0.5,
        )
        gradient_profile_similarity = max(0.0, 1.0 - gradient_profile_mae * 4.0)

        reference_edge = np.asarray(reference.convert("L").filter(ImageFilter.FIND_EDGES), dtype=np.float32) / 255.0
        rendered_edge = np.asarray(rendered.convert("L").filter(ImageFilter.FIND_EDGES), dtype=np.float32) / 255.0
        edge_mae = float(np.abs(reference_edge - rendered_edge).mean())

        reference_edge_mask = _edge_mask(reference)
        rendered_edge_mask = _edge_mask(rendered)
        edge_precision, edge_recall, edge_f1 = _tolerant_edge_f1(
            reference_edge_mask,
            rendered_edge_mask,
        )
        mask_union = int(np.logical_or(reference_mask, rendered_mask).sum())
        mask_iou = (
            float(np.logical_and(reference_mask, rendered_mask).sum() / mask_union)
            if mask_union
            else 1.0
        )

        preview_size = (min(320, reference.width), min(180, reference.height))
        reference_blur = np.asarray(
            reference.resize(preview_size, Image.Resampling.LANCZOS).filter(
                ImageFilter.GaussianBlur(1.5)
            ),
            dtype=np.float32,
        ) / 255.0
        rendered_blur = np.asarray(
            rendered.resize(preview_size, Image.Resampling.LANCZOS).filter(
                ImageFilter.GaussianBlur(1.5)
            ),
            dtype=np.float32,
        ) / 255.0
        blurred_mae = float(np.abs(reference_blur - rendered_blur).mean())

        color_score = max(0.0, 1.0 - pixel_mae * 2.4)
        blurred_score = max(0.0, 1.0 - blurred_mae * 2.2)
        structural_score = 0.52 * edge_f1 + 0.30 * blurred_score + 0.18 * mask_iou
        # Geometry and layout deliberately dominate exact pixels. The product reconstructs
        # editable objects rather than trying to hide discrepancies in a screenshot layer.
        similarity = 0.30 * color_score + 0.70 * structural_score

        height, width = difference_map.shape
        region_scores: list[dict[str, object]] = []
        for row in range(4):
            y0 = round(row * height / 4)
            y1 = round((row + 1) * height / 4)
            for column in range(6):
                x0 = round(column * width / 6)
                x1 = round((column + 1) * width / 6)
                region_scores.append(
                    {
                        "x": x0,
                        "y": y0,
                        "width": x1 - x0,
                        "height": y1 - y0,
                        "mae": round(float(difference_map[y0:y1, x0:x1].mean()), 6),
                    }
                )
        worst_regions = sorted(region_scores, key=lambda item: item["mae"], reverse=True)[:5]

        high_error = np.argwhere(difference_map >= 0.15)
        high_error_bbox: dict[str, int] | None = None
        if high_error.size:
            min_y, min_x = high_error.min(axis=0)
            max_y, max_x = high_error.max(axis=0)
            high_error_bbox = {
                "x": int(min_x),
                "y": int(min_y),
                "width": int(max_x - min_x + 1),
                "height": int(max_y - min_y + 1),
            }
        object_regions = _object_region_metrics(
            spec,
            reference_mask=reference_mask,
            rendered_mask=rendered_mask,
            reference_edge_mask=reference_edge_mask,
            rendered_edge_mask=rendered_edge_mask,
            difference_map=difference_map,
        )
        significant_object_regions = [
            item
            for item in object_regions
            if float(item["high_error_fraction"]) >= 0.08
            or float(item["pixel_mae"]) >= 0.06
        ]
        return {
            "reference_size": list(reference.size),
            "rendered_size": list(original_render_size),
            "pixel_mae": round(pixel_mae, 6),
            "pixel_p95": round(pixel_p95, 6),
            "edge_mae": round(edge_mae, 6),
            "edge_precision": round(edge_precision, 6),
            "edge_recall": round(edge_recall, 6),
            "edge_f1": round(edge_f1, 6),
            "foreground_mask_iou": round(mask_iou, 6),
            "blurred_mae": round(blurred_mae, 6),
            "structural_score": round(structural_score, 6),
            "similarity_score": round(similarity, 6),
            "foreground_pixel_mae": round(foreground_pixel_mae, 6),
            "foreground_pixel_p95": round(foreground_pixel_p95, 6),
            "foreground_color_similarity": round(foreground_color_similarity, 6),
            "gradient_profile_mae": round(gradient_profile_mae, 6),
            "gradient_profile_similarity": round(gradient_profile_similarity, 6),
            "foreground_comparison_pixels": int(eroded_common.sum()),
            "worst_regions": worst_regions,
            "high_error_bbox": high_error_bbox,
            "object_regions": object_regions,
            "worst_object_similarity": (
                object_regions[0]["similarity_score"] if object_regions else 1.0
            ),
            "worst_significant_object_similarity": (
                significant_object_regions[0]["similarity_score"]
                if significant_object_regions
                else 1.0
            ),
        }


def _object_region_metrics(
    spec: SlideSpec | None,
    *,
    reference_mask: np.ndarray,
    rendered_mask: np.ndarray,
    reference_edge_mask: np.ndarray,
    rendered_edge_mask: np.ndarray,
    difference_map: np.ndarray,
) -> list[dict[str, object]]:
    """Score each editable top-level object in its own target-image region.

    The global metric remains the release regression score.  These local scores identify
    which stable object IDs should be corrected, preventing a visually dominant background
    from hiding a malformed icon, connector, or text box.
    """

    if spec is None:
        return []
    height, width = difference_map.shape
    results: list[dict[str, object]] = []
    for element in spec.elements:
        if isinstance(element, LineElement):
            stroke_padding = max(3.0, float(element.stroke.width_px) * 2.0)
            x0 = min(element.x1, element.x2) - stroke_padding
            y0 = min(element.y1, element.y2) - stroke_padding
            x1 = max(element.x1, element.x2) + stroke_padding
            y1 = max(element.y1, element.y2) + stroke_padding
        else:
            x0 = element.bounds.x
            y0 = element.bounds.y
            x1 = x0 + element.bounds.width
            y1 = y0 + element.bounds.height
        left = max(0, min(width, int(math.floor(x0))))
        top = max(0, min(height, int(math.floor(y0))))
        right = max(left + 1, min(width, int(math.ceil(x1))))
        bottom = max(top + 1, min(height, int(math.ceil(y1))))
        if left >= width or top >= height:
            continue

        reference_region = reference_mask[top:bottom, left:right]
        rendered_region = rendered_mask[top:bottom, left:right]
        reference_edges = reference_edge_mask[top:bottom, left:right]
        rendered_edges = rendered_edge_mask[top:bottom, left:right]
        region_difference = difference_map[top:bottom, left:right]
        union = int(np.logical_or(reference_region, rendered_region).sum())
        mask_iou = (
            float(np.logical_and(reference_region, rendered_region).sum() / union)
            if union
            else 1.0
        )
        edge_precision, edge_recall, edge_f1 = _tolerant_edge_f1(
            reference_edges,
            rendered_edges,
        )
        mae = float(region_difference.mean())
        color_score = max(0.0, 1.0 - mae * 2.4)
        structural_score = 0.72 * edge_f1 + 0.28 * mask_iou
        similarity = 0.70 * structural_score + 0.30 * color_score
        results.append(
            {
                "id": element.id,
                "name": element.name,
                "kind": element.kind,
                "bounds": {
                    "x": left,
                    "y": top,
                    "width": right - left,
                    "height": bottom - top,
                },
                "pixel_mae": round(mae, 6),
                "edge_precision": round(edge_precision, 6),
                "edge_recall": round(edge_recall, 6),
                "edge_f1": round(edge_f1, 6),
                "foreground_mask_iou": round(mask_iou, 6),
                "structural_score": round(structural_score, 6),
                "similarity_score": round(similarity, 6),
                "high_error_fraction": round(float((region_difference >= 0.15).mean()), 6),
                "error_mass": round(
                    mae * float((right - left) * (bottom - top)),
                    3,
                ),
            }
        )
    results.sort(
        key=lambda item: (
            float(item["similarity_score"]),
            -float(item["high_error_fraction"]),
        )
    )
    return results


def _foreground_mask(
    image: Image.Image,
    background_color: str,
    *,
    color_tolerance: int = 12,
) -> np.ndarray:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.int16)
    background = np.asarray(ImageColor.getrgb(background_color), dtype=np.int16)
    color_distance = np.max(np.abs(rgba[:, :, :3] - background[None, None, :]), axis=2)
    alpha = rgba[:, :, 3]
    mask = (alpha >= 8) & (color_distance >= color_tolerance)
    # One-pixel dilation removes antialiasing differences without changing form.
    raster = Image.fromarray((mask.astype(np.uint8) * 255), mode="L").filter(ImageFilter.MaxFilter(3))
    return np.asarray(raster, dtype=np.uint8) > 0


def _edge_mask(image: Image.Image, threshold: int = 28) -> np.ndarray:
    # A slight pre-blur suppresses camera moiré and JPEG noise while retaining the
    # intended slide contours that the reconstruction should match.
    luminance = image.convert("L").filter(ImageFilter.GaussianBlur(0.8))
    edges = np.asarray(luminance.filter(ImageFilter.FIND_EDGES), dtype=np.uint8)
    mask = edges >= threshold
    if mask.shape[0] > 2 and mask.shape[1] > 2:
        mask[[0, -1], :] = False
        mask[:, [0, -1]] = False
    return mask


def _tolerant_edge_f1(
    reference: np.ndarray,
    rendered: np.ndarray,
    *,
    tolerance_pixels: int = 2,
) -> tuple[float, float, float]:
    filter_size = tolerance_pixels * 2 + 1
    reference_dilated = np.asarray(
        Image.fromarray(reference.astype(np.uint8) * 255, mode="L").filter(
            ImageFilter.MaxFilter(filter_size)
        ),
        dtype=np.uint8,
    ) > 0
    rendered_dilated = np.asarray(
        Image.fromarray(rendered.astype(np.uint8) * 255, mode="L").filter(
            ImageFilter.MaxFilter(filter_size)
        ),
        dtype=np.uint8,
    ) > 0
    rendered_count = int(rendered.sum())
    reference_count = int(reference.sum())
    precision = (
        float(np.logical_and(rendered, reference_dilated).sum() / rendered_count)
        if rendered_count
        else float(reference_count == 0)
    )
    recall = (
        float(np.logical_and(reference, rendered_dilated).sum() / reference_count)
        if reference_count
        else float(rendered_count == 0)
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    points = np.argwhere(mask)
    if points.size == 0:
        return None
    min_y, min_x = points.min(axis=0)
    max_y, max_x = points.max(axis=0)
    return int(min_x), int(min_y), int(max_x + 1), int(max_y + 1)


def _bbox_iou(
    first: tuple[int, int, int, int] | None,
    second: tuple[int, int, int, int] | None,
) -> float:
    if first is None and second is None:
        return 1.0
    if first is None or second is None:
        return 0.0
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return float(intersection / union) if union else 1.0


def compare_figure_geometry(
    reference_path: str | Path,
    rendered_path: str | Path,
    *,
    background_color: str = "#FFFFFF",
) -> dict[str, object]:
    """Compare silhouette and landmarks while ignoring fill-color pixel differences."""

    with Image.open(reference_path) as reference_image, Image.open(rendered_path) as rendered_image:
        reference = reference_image.convert("RGBA")
        rendered = rendered_image.convert("RGBA")
        original_render_size = rendered.size
        if rendered.size != reference.size:
            rendered = rendered.resize(reference.size, Image.Resampling.LANCZOS)

        reference_mask = _foreground_mask(reference, background_color)
        rendered_mask = _foreground_mask(rendered, background_color)
        intersection = int(np.logical_and(reference_mask, rendered_mask).sum())
        union = int(np.logical_or(reference_mask, rendered_mask).sum())
        silhouette_iou = float(intersection / union) if union else 1.0

        reference_area = int(reference_mask.sum())
        rendered_area = int(rendered_mask.sum())
        area_score = (
            min(reference_area, rendered_area) / max(reference_area, rendered_area)
            if max(reference_area, rendered_area)
            else 1.0
        )
        reference_bbox = _mask_bbox(reference_mask)
        rendered_bbox = _mask_bbox(rendered_mask)
        bbox_iou = _bbox_iou(reference_bbox, rendered_bbox)

        if reference_area and rendered_area:
            ref_y, ref_x = np.argwhere(reference_mask).mean(axis=0)
            out_y, out_x = np.argwhere(rendered_mask).mean(axis=0)
            diagonal = max(1.0, float(np.hypot(reference.width, reference.height)))
            centroid_distance = float(np.hypot(ref_x - out_x, ref_y - out_y) / diagonal)
            centroid_score = max(0.0, 1.0 - centroid_distance * 8.0)
        else:
            centroid_distance = 0.0 if reference_area == rendered_area else 1.0
            centroid_score = 1.0 if reference_area == rendered_area else 0.0

        mismatch = np.logical_xor(reference_mask, rendered_mask)
        mismatch_bbox_tuple = _mask_bbox(mismatch)
        mismatch_bbox = None
        if mismatch_bbox_tuple:
            mismatch_bbox = {
                "x": mismatch_bbox_tuple[0],
                "y": mismatch_bbox_tuple[1],
                "width": mismatch_bbox_tuple[2] - mismatch_bbox_tuple[0],
                "height": mismatch_bbox_tuple[3] - mismatch_bbox_tuple[1],
            }

        local_regions: list[dict[str, object]] = []
        if reference_bbox is not None:
            bx0, by0, bx1, by1 = reference_bbox
            minimum_reference_pixels = max(64, round(reference_area * 0.01))
            for row in range(4):
                y0 = round(by0 + row * (by1 - by0) / 4)
                y1 = round(by0 + (row + 1) * (by1 - by0) / 4)
                for column in range(4):
                    x0 = round(bx0 + column * (bx1 - bx0) / 4)
                    x1 = round(bx0 + (column + 1) * (bx1 - bx0) / 4)
                    reference_region = reference_mask[y0:y1, x0:x1]
                    rendered_region = rendered_mask[y0:y1, x0:x1]
                    reference_pixels = int(reference_region.sum())
                    if reference_pixels < minimum_reference_pixels:
                        continue
                    region_intersection = int(
                        np.logical_and(reference_region, rendered_region).sum()
                    )
                    region_union = int(np.logical_or(reference_region, rendered_region).sum())
                    local_regions.append(
                        {
                            "row": row,
                            "column": column,
                            "x": x0,
                            "y": y0,
                            "width": x1 - x0,
                            "height": y1 - y0,
                            "reference_foreground_pixels": reference_pixels,
                            "silhouette_iou": round(
                                region_intersection / region_union if region_union else 1.0,
                                6,
                            ),
                        }
                    )
        local_regions.sort(key=lambda item: float(item["silhouette_iou"]))
        local_scores = [float(item["silhouette_iou"]) for item in local_regions]
        worst_local_iou = min(local_scores, default=silhouette_iou)
        local_iou_p10 = float(np.percentile(local_scores, 10)) if local_scores else silhouette_iou

        geometry_score = (
            0.72 * silhouette_iou
            + 0.15 * bbox_iou
            + 0.08 * area_score
            + 0.05 * centroid_score
        )
        return {
            "reference_size": list(reference.size),
            "rendered_size": list(original_render_size),
            "silhouette_iou": round(silhouette_iou, 6),
            "bbox_iou": round(bbox_iou, 6),
            "area_ratio_score": round(float(area_score), 6),
            "centroid_distance": round(centroid_distance, 6),
            "geometry_score": round(float(geometry_score), 6),
            "worst_local_iou": round(worst_local_iou, 6),
            "local_iou_p10": round(local_iou_p10, 6),
            "worst_local_regions": local_regions[:4],
            "reference_foreground_pixels": reference_area,
            "rendered_foreground_pixels": rendered_area,
            "mismatch_bbox": mismatch_bbox,
        }


def compare_context_figure_style(
    context_path: str | Path,
    rendered_path: str | Path,
    *,
    figure_bbox: tuple[float, float, float, float],
    background_color: str = "#FFFFFF",
    bins: int = 12,
    sample_size: int = 256,
) -> dict[str, object]:
    """Compare a rendered figure's color ramp with its visible pixels in a full-slide target.

    ``figure_bbox`` is expressed in target-image pixels. The rendered figure's own foreground
    mask supplies the silhouette, so unrelated slide objects outside the figure cannot dilute
    the score. Neutral pixels and near-white occlusions inside the target (guide lines, icon
    circles, text) are rejected before robust per-bin medians are calculated.
    """

    if bins < 4:
        raise ValueError("bins must be at least 4")
    if sample_size < 64:
        raise ValueError("sample_size must be at least 64")
    x, y, width, height = (float(value) for value in figure_bbox)
    if width <= 0 or height <= 0:
        raise ValueError("figure_bbox width and height must be positive")

    with Image.open(context_path) as context_image, Image.open(rendered_path) as rendered_image:
        context = context_image.convert("RGB")
        rendered = rendered_image.convert("RGB")
        left = max(0, round(x))
        top = max(0, round(y))
        right = min(context.width, round(x + width))
        bottom = min(context.height, round(y + height))
        if right <= left or bottom <= top:
            raise ValueError("figure_bbox does not intersect the context image")

        rendered_mask = _foreground_mask(rendered.convert("RGBA"), background_color)
        rendered_bbox = _mask_bbox(rendered_mask)
        if rendered_bbox is None:
            raise ValueError("Rendered candidate contains no foreground figure")
        rx0, ry0, rx1, ry1 = rendered_bbox

        target_crop = context.crop((left, top, right, bottom)).resize(
            (sample_size, sample_size),
            Image.Resampling.LANCZOS,
        )
        candidate_crop = rendered.crop((rx0, ry0, rx1, ry1)).resize(
            (sample_size, sample_size),
            Image.Resampling.LANCZOS,
        )
        mask_crop = Image.fromarray(
            (rendered_mask[ry0:ry1, rx0:rx1].astype(np.uint8) * 255),
            mode="L",
        ).resize((sample_size, sample_size), Image.Resampling.NEAREST)

        target_array = np.asarray(target_crop, dtype=np.float32) / 255.0
        candidate_array = np.asarray(candidate_crop, dtype=np.float32) / 255.0
        candidate_mask = np.asarray(mask_crop, dtype=np.uint8) > 0

        target_rgb = target_array * 255.0
        chroma = target_rgb.max(axis=2) - target_rgb.min(axis=2)
        # The intended family is a cool blue/teal ramp. This excludes neutral guide lines,
        # black text, and white icon interiors while retaining the very pale blue tail.
        palette_mask = (
            (target_rgb[:, :, 2] - target_rgb[:, :, 0] >= 3.0)
            & (target_rgb[:, :, 1] - target_rgb[:, :, 0] >= 1.5)
            & (chroma >= 3.0)
            & (target_rgb.min(axis=2) < 250.0)
        )
        valid_target = candidate_mask & palette_mask

        grid_y, grid_x = np.mgrid[0:sample_size, 0:sample_size]
        # Normalized progression: 0 at the lower-left tail, 1 at the upper-right head.
        progress = (grid_x / max(1, sample_size - 1) + 1.0 - grid_y / max(1, sample_size - 1)) / 2.0

        profile: list[dict[str, object]] = []
        for index in range(bins):
            lower = index / bins
            upper = (index + 1) / bins
            region = (progress >= lower) & (progress < upper)
            target_region = valid_target & region
            candidate_region = candidate_mask & region
            if int(target_region.sum()) < 16 or int(candidate_region.sum()) < 16:
                continue
            target_median = np.median(target_array[target_region], axis=0)
            candidate_median = np.median(candidate_array[candidate_region], axis=0)
            profile.append(
                {
                    "progress": round((lower + upper) / 2.0, 6),
                    "target_rgb": [round(float(value), 6) for value in target_median],
                    "candidate_rgb": [round(float(value), 6) for value in candidate_median],
                    "target_pixels": int(target_region.sum()),
                    "candidate_pixels": int(candidate_region.sum()),
                }
            )

        if len(profile) < max(4, bins // 2):
            raise ValueError(
                "Too few visible target pixels inside figure_bbox; provide a tighter target region"
            )

        target_profile = np.asarray([row["target_rgb"] for row in profile], dtype=np.float32)
        candidate_profile = np.asarray(
            [row["candidate_rgb"] for row in profile], dtype=np.float32
        )
        weights = np.asarray([row["target_pixels"] for row in profile], dtype=np.float32)
        weights /= max(float(weights.sum()), 1.0)
        per_bin_mae = np.abs(target_profile - candidate_profile).mean(axis=1)
        profile_mae = float(np.sum(per_bin_mae * weights))
        profile_p95 = float(np.percentile(per_bin_mae, 95))
        target_centered = target_profile - np.average(target_profile, axis=0, weights=weights)
        candidate_centered = candidate_profile - np.average(
            candidate_profile,
            axis=0,
            weights=weights,
        )
        centered_per_bin = np.abs(target_centered - candidate_centered).mean(axis=1)
        gradient_mae = float(np.sum(centered_per_bin * weights))

        color_similarity = max(0.0, 1.0 - profile_mae * 2.0 - profile_p95 * 0.35)
        gradient_similarity = max(0.0, 1.0 - gradient_mae * 3.0)
        return {
            "context_size": [context.width, context.height],
            "rendered_size": [rendered.width, rendered.height],
            "figure_bbox": [left, top, right - left, bottom - top],
            "rendered_foreground_bbox": [rx0, ry0, rx1 - rx0, ry1 - ry0],
            "visible_target_pixels": int(valid_target.sum()),
            "profile_bins": len(profile),
            "profile_mae": round(profile_mae, 6),
            "profile_p95": round(profile_p95, 6),
            "gradient_profile_mae": round(gradient_mae, 6),
            "foreground_color_similarity": round(color_similarity, 6),
            "gradient_profile_similarity": round(gradient_similarity, 6),
            "profile": profile,
        }


def audit_pptx(pptx_path: str | Path, spec: SlideSpec | None = None) -> dict[str, object]:
    source = Path(pptx_path)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        slide_names = sorted(name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        slide_xml_values = [
            archive.read(name).decode("utf-8", errors="replace") for name in slide_names
        ]
        slide_xml = "\n".join(slide_xml_values)
        media = [name for name in names if name.startswith("ppt/media/") and not name.endswith("/")]
        presentation_xml = archive.read("ppt/presentation.xml").decode(
            "utf-8", errors="replace"
        )

    canvas_match = re.search(r"<p:sldSz[^>]*\bcx=\"(\d+)\"[^>]*\bcy=\"(\d+)\"", presentation_xml)
    canvas_width = int(canvas_match.group(1)) if canvas_match else 0
    canvas_height = int(canvas_match.group(2)) if canvas_match else 0
    overflow_objects = _canvas_overflow_objects(
        slide_xml_values,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )

    audit: dict[str, object] = {
        "slides": len(slide_names),
        "native_shape_objects": slide_xml.count("<p:sp>"),
        "native_text_runs": slide_xml.count("<a:t>"),
        "picture_objects": slide_xml.count("<p:pic>"),
        "media_files": len(media),
        "custom_geometry_paths": slide_xml.count("<a:custGeom>"),
        "cubic_bezier_segments": slide_xml.count("<a:cubicBezTo>"),
        "native_gradient_fills": slide_xml.count("<a:gradFill"),
        "canvas_size_emu": [canvas_width, canvas_height],
        "canvas_overflow_count": len(overflow_objects),
        "canvas_overflow_objects": overflow_objects,
        "flattened_slide": False,
    }
    if spec is not None:
        audit["spec_object_counts"] = spec.count_objects()
        audit["full_slide_raster_images"] = spec.full_slide_images()
        audit["suspicious_full_slide_raster_images"] = spec.suspicious_full_slide_images()
        audit["flattened_slide"] = bool(spec.suspicious_full_slide_images())
    return audit


def _canvas_overflow_objects(
    slide_xml_values: list[str],
    *,
    canvas_width: int,
    canvas_height: int,
    tolerance_emu: int = 1000,
) -> list[dict[str, object]]:
    if canvas_width <= 0 or canvas_height <= 0:
        return []
    namespaces = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }
    object_tags = (
        f"{{{namespaces['p']}}}sp",
        f"{{{namespaces['p']}}}pic",
        f"{{{namespaces['p']}}}graphicFrame",
        f"{{{namespaces['p']}}}cxnSp",
    )
    results: list[dict[str, object]] = []
    for slide_index, slide_xml in enumerate(slide_xml_values, start=1):
        root = ET.fromstring(slide_xml)
        for item in root.iter():
            if item.tag not in object_tags:
                continue
            transform = item.find(".//a:xfrm", namespaces)
            if transform is None:
                continue
            offset = transform.find("a:off", namespaces)
            extent = transform.find("a:ext", namespaces)
            if offset is None or extent is None:
                continue
            x = int(offset.attrib.get("x", "0"))
            y = int(offset.attrib.get("y", "0"))
            width = int(extent.attrib.get("cx", "0"))
            height = int(extent.attrib.get("cy", "0"))
            angle = int(transform.attrib.get("rot", "0")) / 60000.0
            if angle:
                radians = math.radians(angle)
                rotated_width = abs(width * math.cos(radians)) + abs(
                    height * math.sin(radians)
                )
                rotated_height = abs(width * math.sin(radians)) + abs(
                    height * math.cos(radians)
                )
                center_x = x + width / 2
                center_y = y + height / 2
                left = center_x - rotated_width / 2
                top = center_y - rotated_height / 2
                right = center_x + rotated_width / 2
                bottom = center_y + rotated_height / 2
            else:
                left, top, right, bottom = x, y, x + width, y + height
            if (
                left >= -tolerance_emu
                and top >= -tolerance_emu
                and right <= canvas_width + tolerance_emu
                and bottom <= canvas_height + tolerance_emu
            ):
                continue
            metadata = item.find(".//p:cNvPr", namespaces)
            results.append(
                {
                    "slide": slide_index,
                    "id": metadata.attrib.get("id") if metadata is not None else None,
                    "name": metadata.attrib.get("name") if metadata is not None else None,
                    "bbox_emu": [
                        round(left),
                        round(top),
                        round(right - left),
                        round(bottom - top),
                    ],
                }
            )
    return results


def write_report(report: dict[str, object], output_path: str | Path) -> Path:
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output
