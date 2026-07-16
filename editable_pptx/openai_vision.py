from __future__ import annotations

from pathlib import Path

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
        patch = request_structured_response(
            SlidePatch,
            schema_name="editable_slide_patch",
            system_text=SYSTEM_PROMPT,
            user_text=refinement_patch_prompt(
                image_facts,
                metrics,
                current_spec.model_dump(mode="json"),
                raster_policy,
            ),
            image_paths=[source_image, rendered_image],
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
        return apply_slide_patch(current_spec, patch)
    except (OpenAIResponsesError, ValueError) as error:
        raise OpenAIReconstructionError(str(error)) from error
