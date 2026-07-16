from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image

from .models import SlidePatch, SlideSpec, apply_slide_patch
from .openai_responses import OpenAIResponsesError, request_structured_response
from .prompts import SYSTEM_PROMPT, initial_user_prompt, refinement_patch_prompt


class OpenAIReconstructionError(RuntimeError):
    pass


def _request(
    *,
    api_key: str | None,
    model: str,
    user_text: str,
    image_paths: list[str | Path],
    timeout_seconds: int,
    max_output_tokens: int,
) -> SlideSpec:
    try:
        return request_structured_response(
            SlideSpec,
            schema_name="editable_slide_spec",
            system_text=SYSTEM_PROMPT,
            user_text=user_text,
            image_paths=image_paths,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
    except OpenAIResponsesError as error:
        raise OpenAIReconstructionError(str(error)) from error


def reconstruct_slide(
    image_path: str | Path,
    *,
    image_facts: dict[str, object],
    raster_policy: str = "photos-only",
    model: str = "gpt-5.5",
    api_key: str | None = None,
    timeout_seconds: int = 240,
    max_output_tokens: int = 64000,
) -> SlideSpec:
    return _request(
        api_key=api_key,
        model=model,
        user_text=initial_user_prompt(image_facts, raster_policy),
        image_paths=[image_path],
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


def refine_slide(
    source_image: str | Path,
    rendered_image: str | Path,
    current_spec: SlideSpec,
    *,
    image_facts: dict[str, object],
    metrics: dict[str, object],
    raster_policy: str = "photos-only",
    model: str = "gpt-5.5",
    api_key: str | None = None,
    timeout_seconds: int = 240,
    max_output_tokens: int = 64000,
) -> SlideSpec:
    try:
        with tempfile.TemporaryDirectory(prefix="editable-pptx-focus-") as temp_dir:
            focus_paths, focus_regions = _focus_crops(
                source_image,
                rendered_image,
                metrics,
                Path(temp_dir),
            )
            patch = request_structured_response(
                SlidePatch,
                schema_name="editable_slide_patch",
                system_text=SYSTEM_PROMPT,
                user_text=refinement_patch_prompt(
                    image_facts,
                    metrics,
                    current_spec.model_dump(mode="json"),
                    raster_policy,
                    focus_regions,
                ),
                image_paths=[source_image, rendered_image, *focus_paths],
                model=model,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                max_output_tokens=max_output_tokens,
            )
        return apply_slide_patch(current_spec, patch)
    except (OpenAIResponsesError, ValueError) as error:
        raise OpenAIReconstructionError(str(error)) from error


def _focus_crops(
    source_image: str | Path,
    rendered_image: str | Path,
    metrics: dict[str, object],
    output_dir: Path,
    *,
    maximum_regions: int = 2,
) -> tuple[list[Path], list[dict[str, object]]]:
    reference_size = metrics.get("reference_size", [0, 0])
    canvas_area = (
        float(reference_size[0]) * float(reference_size[1])
        if isinstance(reference_size, list) and len(reference_size) == 2
        else 0.0
    )
    regions = [
        item
        for item in metrics.get("object_regions", [])
        if isinstance(item, dict)
        and item.get("kind") in {"shape", "path", "line", "component"}
        and isinstance(item.get("bounds"), dict)
        and (
            canvas_area <= 0
            or float(item["bounds"].get("width", 0))
            * float(item["bounds"].get("height", 0))
            <= canvas_area * 0.18
        )
        and (
            float(item.get("high_error_fraction", 0.0)) >= 0.08
            or float(item.get("pixel_mae", 0.0)) >= 0.06
        )
    ]
    regions.sort(
        key=lambda item: (
            float(item.get("error_mass", 0.0)),
            -float(item.get("similarity_score", 1.0)),
        ),
        reverse=True,
    )
    selected = regions[:maximum_regions]
    if not selected:
        return [], []
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    metadata: list[dict[str, object]] = []
    with Image.open(source_image) as source_file, Image.open(rendered_image) as rendered_file:
        source = source_file.convert("RGB")
        rendered = rendered_file.convert("RGB")
        if rendered.size != source.size:
            rendered = rendered.resize(source.size, Image.Resampling.LANCZOS)
        for index, region in enumerate(selected, start=1):
            bounds = region["bounds"]
            x = int(bounds["x"])
            y = int(bounds["y"])
            width = int(bounds["width"])
            height = int(bounds["height"])
            padding = max(12, round(max(width, height) * 0.18))
            box = (
                max(0, x - padding),
                max(0, y - padding),
                min(source.width, x + width + padding),
                min(source.height, y + height + padding),
            )
            source_crop = source.crop(box)
            rendered_crop = rendered.crop(box)
            source_path = output_dir / f"focus-{index}-source.png"
            rendered_path = output_dir / f"focus-{index}-rendered.png"
            source_crop.save(source_path)
            rendered_crop.save(rendered_path)
            paths.extend([source_path, rendered_path])
            metadata.append(
                {
                    "pair": index,
                    "object_id": region.get("id"),
                    "kind": region.get("kind"),
                    "crop_box": list(box),
                    "source_image_position": 3 + (index - 1) * 2,
                    "rendered_image_position": 4 + (index - 1) * 2,
                }
            )
    return paths, metadata
