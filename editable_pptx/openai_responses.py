from __future__ import annotations

import json
import os
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .image_analysis import image_data_url


ModelT = TypeVar("ModelT", bound=BaseModel)


class OpenAIResponsesError(RuntimeError):
    pass


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    environment = os.environ.get("OPENAI_API_KEY", "").strip()
    if environment:
        return environment

    # Local project configuration only. Value stays in memory and is never logged.
    env_file = Path.cwd() / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "OPENAI_API_KEY":
            continue
        try:
            parsed = shlex.split(value, comments=True, posix=True)
        except ValueError as error:
            raise OpenAIResponsesError("Invalid OPENAI_API_KEY entry in .env") from error
        return parsed[0].strip() if parsed else ""
    return ""


def normalize_structured_output_schema(model_type: type[BaseModel]) -> dict[str, Any]:
    """Return strict JSON Schema accepted by Responses structured outputs."""

    schema = model_type.model_json_schema()

    def normalize(node: object) -> object:
        if isinstance(node, list):
            return [normalize(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, object] = {}
        for key, value in node.items():
            if key == "oneOf":
                result["anyOf"] = normalize(value)
            elif key == "discriminator":
                continue
            else:
                result[key] = normalize(value)
        return result

    return normalize(schema)  # type: ignore[return-value]


def _response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                return part["text"]
    return ""


def _detail_for_model(model: str) -> str:
    unsupported_original = ("mini", "nano", "gpt-5-mini", "gpt-5-nano")
    return "high" if any(token in model for token in unsupported_original) else "original"


def request_structured_response(
    response_type: type[ModelT],
    *,
    schema_name: str,
    system_text: str,
    user_text: str,
    image_paths: list[str | Path],
    model: str,
    api_key: str | None = None,
    timeout_seconds: int = 240,
    max_output_tokens: int = 32000,
    reasoning_effort: str = "high",
    max_retries: int = 2,
    max_validation_retries: int = 1,
    background: bool = True,
    poll_interval_seconds: float = 2.0,
) -> ModelT:
    resolved_key = resolve_api_key(api_key)
    if not resolved_key:
        raise OpenAIResponsesError("OPENAI_API_KEY is missing")

    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
    detail = _detail_for_model(model)
    for image_path in image_paths:
        content.append(
            {
                "type": "input_image",
                "image_url": image_data_url(image_path),
                "detail": detail,
            }
        )

    body = {
        "model": model,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_text}],
            },
            {
                "role": "user",
                "content": content,
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": normalize_structured_output_schema(response_type),
            }
        },
        "max_output_tokens": max_output_tokens,
        "background": background,
    }

    deadline = time.monotonic() + timeout_seconds
    validation_feedback = ""
    for validation_attempt in range(max_validation_retries + 1):
        if validation_feedback:
            body["input"][-1]["content"][0]["text"] = (
                user_text
                + "\n\nYour previous response failed local schema validation. Regenerate the "
                "complete object and correct every reported issue:\n"
                + validation_feedback[:2000]
            )
        payload = _request_json_with_retries(
            "https://api.openai.com/v1/responses",
            api_key=resolved_key,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            deadline=deadline,
            max_retries=max_retries,
        )
        while background and payload.get("status") in {"queued", "in_progress"}:
            response_id = payload.get("id")
            if not isinstance(response_id, str) or not response_id:
                raise OpenAIResponsesError("Background response did not include an id")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenAIResponsesError(
                    f"OpenAI background response timed out after {timeout_seconds}s"
                )
            time.sleep(min(max(0.1, poll_interval_seconds), remaining))
            payload = _request_json_with_retries(
                "https://api.openai.com/v1/responses/"
                + urllib.parse.quote(response_id, safe=""),
                api_key=resolved_key,
                data=None,
                method="GET",
                deadline=deadline,
                max_retries=max_retries,
            )

        if payload.get("status") in {"failed", "cancelled"}:
            message = (
                payload.get("error", {}).get("message")
                if isinstance(payload.get("error"), dict)
                else None
            )
            raise OpenAIResponsesError(
                f"OpenAI response {payload.get('status')}: {message or 'unknown error'}"
            )
        if payload.get("status") == "incomplete":
            reason = payload.get("incomplete_details", {}).get("reason", "unknown reason")
            raise OpenAIResponsesError(
                f"OpenAI response was incomplete ({reason}); increase max output tokens"
            )

        text = _response_text(payload)
        if not text:
            raise OpenAIResponsesError("OpenAI response contained no structured output text")
        try:
            return response_type.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as error:
            if validation_attempt >= max_validation_retries:
                label = "structured output" if isinstance(error, ValidationError) else "JSON"
                raise OpenAIResponsesError(f"OpenAI returned invalid {label}: {error}") from error
            validation_feedback = str(error)

    raise OpenAIResponsesError("OpenAI response failed structured-output validation")


def _request_json_with_retries(
    url: str,
    *,
    api_key: str,
    data: bytes | None,
    method: str,
    deadline: float,
    max_retries: int,
) -> dict[str, Any]:
    """Execute one Responses API operation within a shared wall-clock deadline."""

    for attempt in range(max_retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenAIResponsesError("OpenAI request exceeded its wall-clock deadline")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=min(120.0, remaining)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise OpenAIResponsesError("OpenAI returned a non-object JSON response")
            return payload
        except urllib.error.HTTPError as error:
            error_code = None
            try:
                error_payload = json.loads(error.read().decode("utf-8"))
                error_details = error_payload.get("error", {})
                message = error_details.get("message") or f"HTTP {error.code}"
                error_code = error_details.get("code") or error_details.get("type")
            except Exception:
                message = f"HTTP {error.code}"
            retryable = (
                error.code in {408, 409, 429, 500, 502, 503, 504}
                and error_code not in {"insufficient_quota", "billing_hard_limit_reached"}
            )
            if not retryable or attempt >= max_retries:
                suffix = f" [{error_code}]" if error_code else ""
                raise OpenAIResponsesError(f"OpenAI request failed{suffix}: {message}") from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            delay = (
                float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else 1.5 * (attempt + 1)
            )
        except urllib.error.URLError as error:
            if attempt >= max_retries:
                raise OpenAIResponsesError(f"OpenAI request failed: {error.reason}") from error
            delay = 1.5 * (attempt + 1)
        except TimeoutError as error:
            if attempt >= max_retries:
                raise OpenAIResponsesError("OpenAI request operation timed out") from error
            delay = 1.5 * (attempt + 1)
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

    raise OpenAIResponsesError("OpenAI request produced no response")


__all__ = [
    "OpenAIResponsesError",
    "normalize_structured_output_schema",
    "request_structured_response",
    "resolve_api_key",
]
