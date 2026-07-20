from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

from .image_analysis import image_data_url
from .openai_responses import resolve_api_key
from .service_models import (
    BeautifyAnalysis,
    BeautifyTraceRecord,
    PptxPatchPlan,
    PptxPatchReview,
    utc_now,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


ANALYST_INSTRUCTIONS = """
You are the AnalystAgent for a native-editable PowerPoint beautification pipeline.
Return the requested structured analysis only. Business words, numbers, table data,
chart data, logos, media, animations, and slide count are immutable. Structure is
more important than visual style when selecting a template. Keep one template family
for the whole deck. A candidate below the deterministic 0.75 confidence threshold
must not be selected. If SmartArt, OLE, diagrams, or animation timelines are reported,
never choose rebuild. Uploaded templates are mandatory when identified as forced. Return
protected_shape_ids and safe_zones in slide points for titles, body content and footers.
""".strip()


DESIGNER_INSTRUCTIONS = """
You are the DesignerAgent. Produce a strict PptxPatchPlan for the deterministic OOXML
executor. You do not edit files. Use only supplied slide and shape IDs. Preserve every
word and number; punctuation, case and whitespace may change only when explicitly
needed, but prefer no text replacement. Charts and tables stay native and their data
never changes. Photos may be moved, resized, or cropped. Logos may move and scale
proportionally but may not be cropped or recolored. Preserve animations, transitions,
OLE, SmartArt, relationships, footers and sources. Prefer compound align/distribute,
typography, paragraph, fill, border, crop, z-order, table-style and chart-style actions.
Use section_operations for native PowerPoint section add/remove/rename requests.
Body text must be at least 8pt and source text at least 6pt. Return only the structured
plan and use the exact source_sha256 provided.
""".strip()


REVIEWER_INSTRUCTIONS = """
You are the ReviewerAgent and final aesthetic judge. Compare images in this order:
source slides, candidate slides, then template or target references. Deterministic
hard-gate results are authoritative and cannot be overridden. Score seven criteria
from 0 to 10 in rubric_scores using exactly these keys: hierarchy, alignment,
readability, coherence, density, template_fidelity, polish. The overall score is the
weighted visual judgment. Reject clipping, overlap, weak hierarchy, inconsistent
spacing, unreadable sources, obvious template drift or any hard-gate failure. Set
relative_improvement to candidate score minus source score. A score >=8 is possible
only when every boolean gate is true. Populate slide_scores for every selected slide;
one slide below 8 rejects the whole deck. Give a concrete repair_instruction on rejection.
""".strip()


class AgentsSdkUnavailable(RuntimeError):
    pass


class AgentRunFailed(RuntimeError):
    pass


@dataclass
class AgentResult:
    output: BaseModel
    trace: BeautifyTraceRecord


class ModelProviderFactory(Protocol):
    """Provider boundary kept intentionally small for a future Azure adapter."""

    name: str

    def create(self, *, api_key: str, timeout_seconds: int) -> Any: ...


class OpenAIModelProviderFactory:
    name = "openai"

    def create(self, *, api_key: str, timeout_seconds: int) -> Any:
        from agents import OpenAIProvider
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            timeout=max(1, timeout_seconds),
            # Retry policy is owned by this module so quota errors can never be retried.
            max_retries=0,
        )
        return OpenAIProvider(openai_client=client, use_responses=True)


class CostBudget:
    INPUT_USD_PER_MILLION = 5.0
    OUTPUT_USD_PER_MILLION = 30.0

    def __init__(self, maximum_usd: float) -> None:
        self.maximum_usd = max(0.0, float(maximum_usd))
        self.spent_usd = 0.0
        self._lock = threading.Lock()

    def add(self, input_tokens: int, output_tokens: int) -> float:
        cost = (
            input_tokens * self.INPUT_USD_PER_MILLION
            + output_tokens * self.OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        with self._lock:
            self.spent_usd += cost
            if self.maximum_usd and self.spent_usd > self.maximum_usd:
                raise AgentRunFailed(
                    f"LLM cost budget exceeded: ${self.spent_usd:.4f} > ${self.maximum_usd:.2f}"
                )
        return cost


class AgentsSdkRuntime:
    """Thin, optional Agents SDK adapter with isolated per-job structured runs."""

    def __init__(
        self,
        *,
        model: str,
        job_id: str,
        trace_level: str = "metadata",
        max_cost_usd: float = 5.0,
        timeout_seconds: int = 900,
        transient_retries: int = 2,
        provider_factory: ModelProviderFactory | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> None:
        self.model = model
        self.job_id = job_id
        self.trace_level = trace_level
        self.budget = CostBudget(max_cost_usd)
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.transient_retries = max(0, int(transient_retries))
        self.provider_factory = provider_factory or OpenAIModelProviderFactory()
        self.cancellation_check = cancellation_check

    @staticmethod
    def available() -> bool:
        try:
            import agents  # noqa: F401
        except ImportError:
            return False
        return True

    def analyze(
        self,
        payload: dict[str, Any],
        *,
        image_paths: list[Path],
    ) -> AgentResult:
        return self._run(
            agent_name="AnalystAgent",
            instructions=ANALYST_INSTRUCTIONS,
            output_type=BeautifyAnalysis,
            payload=payload,
            image_paths=image_paths,
            stage="analysis",
        )

    def design(
        self,
        payload: dict[str, Any],
        *,
        image_paths: list[Path],
        candidate_index: int,
    ) -> AgentResult:
        return self._run(
            agent_name="DesignerAgent",
            instructions=DESIGNER_INSTRUCTIONS,
            output_type=PptxPatchPlan,
            payload=payload,
            image_paths=image_paths,
            stage="design",
            candidate_index=candidate_index,
        )

    def review(
        self,
        payload: dict[str, Any],
        *,
        image_paths: list[Path],
        candidate_index: int,
    ) -> AgentResult:
        return self._run(
            agent_name="ReviewerAgent",
            instructions=REVIEWER_INSTRUCTIONS,
            output_type=PptxPatchReview,
            payload=payload,
            image_paths=image_paths,
            stage="review",
            candidate_index=candidate_index,
        )

    def _run(
        self,
        *,
        agent_name: str,
        instructions: str,
        output_type: type[ModelT],
        payload: dict[str, Any],
        image_paths: list[Path],
        stage: str,
        candidate_index: int | None = None,
    ) -> AgentResult:
        try:
            from agents import Agent, AgentOutputSchema, RunConfig, Runner
        except ImportError as error:
            raise AgentsSdkUnavailable(
                "Beautify quality v1 requires the optional 'agents' dependency: "
                "pip install -e '.[api,agents]'"
            ) from error

        trace_id = uuid.uuid4().hex
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
        for path in image_paths:
            content.append(
                {
                    "type": "input_image",
                    "image_url": image_data_url(path),
                    "detail": "original",
                }
            )
        agent = Agent(
            name=agent_name,
            instructions=instructions,
            model=self.model,
            output_type=AgentOutputSchema(output_type, strict_json_schema=False),
        )
        api_key = resolve_api_key()
        if not api_key:
            raise AgentRunFailed("OPENAI_API_KEY is missing")
        run_config = RunConfig(
            model_provider=self.provider_factory.create(
                api_key=api_key,
                timeout_seconds=self.timeout_seconds,
            ),
            workflow_name="editable-pptx-beautify",
            group_id=self.job_id,
            tracing_disabled=self.trace_level == "none",
            trace_include_sensitive_data=self.trace_level == "full",
            trace_metadata={
                "job_id": self.job_id,
                "stage": stage,
                "candidate_index": candidate_index,
                "local_trace_id": trace_id,
                "provider": self.provider_factory.name,
            },
        )
        started_iso = utc_now()
        started = time.monotonic()
        output: ModelT | None = None
        last_error: Exception | None = None
        for repair_attempt in range(2):
            current_content = list(content)
            if repair_attempt:
                current_content[0] = {
                    "type": "input_text",
                    "text": json.dumps(
                        {
                            **payload,
                            "structured_output_repair": (
                                "The previous final output failed local structured validation. "
                                "Return a complete valid object matching the output schema."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            for transient_attempt in range(self.transient_retries + 1):
                if self.cancellation_check is not None:
                    self.cancellation_check()
                try:
                    result = Runner.run_sync(
                        agent,
                        [{"role": "user", "content": current_content}],
                        max_turns=4,
                        run_config=run_config,
                    )
                    output = result.final_output
                    if not isinstance(output, output_type):
                        output = output_type.model_validate(output)
                    break
                except Exception as error:  # SDK errors are an optional dependency boundary.
                    last_error = error
                    if type(error).__name__ in {"ModelBehaviorError", "ValidationError"}:
                        break
                    if not _is_transient_error(error) or transient_attempt >= self.transient_retries:
                        raise AgentRunFailed(f"{agent_name} API run failed: {error}") from error
                    time.sleep(min(2**transient_attempt, 8))
            if output is not None:
                break
        if output is None:
            raise AgentRunFailed(
                f"{agent_name} failed after one structured-output repair: {last_error}"
            ) from last_error
        input_tokens, output_tokens = _usage_tokens(result)
        estimated_cost = self.budget.add(input_tokens, output_tokens)
        finished = utc_now()
        trace_metadata: dict[str, Any] = {
            "trace_level": self.trace_level,
            "provider": self.provider_factory.name,
        }
        if self.trace_level == "full":
            trace_metadata.update(
                {
                    "input_payload": payload,
                    "image_inputs": [str(path) for path in image_paths],
                }
            )
        trace = BeautifyTraceRecord(
            trace_id=trace_id,
            job_id=self.job_id,
            stage=stage,
            agent=agent_name,
            model=self.model,
            candidate_index=candidate_index,
            started_at=started_iso,
            finished_at=finished,
            latency_ms=round((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=round(estimated_cost, 6),
            metadata=trace_metadata,
        )
        return AgentResult(output=output, trace=trace)


def _is_transient_error(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".casefold()
    if "insufficient_quota" in text or "billing" in text or "quota" in text:
        return False
    return type(error).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    } or any(
        marker in text
        for marker in ("temporarily unavailable", "connection reset", "timeout", "status 500", "status 502", "status 503", "status 504")
    )


def _usage_tokens(result: Any) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        input_tokens += int(
            getattr(usage, "input_tokens", None)
            or getattr(usage, "requests_input_tokens", None)
            or 0
        )
        output_tokens += int(
            getattr(usage, "output_tokens", None)
            or getattr(usage, "requests_output_tokens", None)
            or 0
        )
    return input_tokens, output_tokens


__all__ = [
    "AgentRunFailed",
    "AgentsSdkRuntime",
    "AgentsSdkUnavailable",
    "CostBudget",
    "ModelProviderFactory",
    "OpenAIModelProviderFactory",
]
