from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobOperation(str, Enum):
    IMAGE_TO_EDITABLE = "image_to_editable"
    NOTES = "notes"
    BEAUTIFY = "beautify"
    FIGURE_TO_EDITABLE = "figure_to_editable"
    RENDER = "render"
    VALIDATE = "validate"
    APPLY_PLAN = "apply_plan"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


class ArtifactKind(str, Enum):
    INPUT = "input"
    PPTX = "pptx"
    SPEC = "spec"
    REPORT = "report"
    PREVIEW = "preview"
    PDF = "pdf"
    PLAN = "plan"
    BUNDLE = "bundle"
    LOG = "log"


class SlideSelector(ServiceModel):
    all: bool = True
    indices: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_selector(self) -> "SlideSelector":
        if self.all and self.indices:
            raise ValueError("slide selector cannot combine all=true with explicit indices")
        if not self.all and not self.indices:
            raise ValueError("slide selector requires at least one 1-based slide index")
        if any(index <= 0 for index in self.indices):
            raise ValueError("slide indices are 1-based and must be positive")
        self.indices = sorted(set(self.indices))
        return self


class ArtifactRecord(ServiceModel):
    id: str
    job_id: str
    name: str
    kind: ArtifactKind
    size_bytes: int
    sha256: str
    media_type: str
    created_at: str
    parent_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    download_url: str | None = None


class JobEvent(ServiceModel):
    sequence: int
    job_id: str
    event_type: str
    created_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobResource(ServiceModel):
    id: str
    operation: JobOperation
    status: JobStatus
    mode: Literal["plan", "apply"]
    progress: float = Field(ge=0, le=1)
    stage: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    source_job_id: str | None = None
    parent_job_id: str | None = None
    plan_id: str | None = None
    best_artifact_id: str | None = None
    error: dict[str, Any] | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)


class PlanResource(ServiceModel):
    id: str
    job_id: str
    operation: JobOperation
    source_sha256: str
    status: Literal["ready", "applied", "invalidated"]
    created_at: str
    applied_job_id: str | None = None
    artifact_id: str


class CapabilityReport(ServiceModel):
    python: str
    node: str | None
    libreoffice: str | None
    pdftoppm: str | None
    powerpoint_adapter: str | None
    openai_key_configured: bool
    supported_input_formats: list[str]
    operations: list[JobOperation]


class BrandProfile(ServiceModel):
    name: str
    fonts: dict[str, str] = Field(default_factory=dict)
    palette: dict[str, str] = Field(default_factory=dict)
    spacing: dict[str, float] = Field(default_factory=dict)
    protected_zones: list[dict[str, float]] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


class ProductionInstruction(ServiceModel):
    id: str
    slide_index: int = Field(ge=1)
    source: Literal["api", "comment", "speaker_note", "visible_callout"]
    raw_text: str
    author: str | None = None
    timestamp: str | None = None
    priority: int = Field(ge=0, le=100)
    conflict_resolution: str | None = None
    object_id: str | None = None
    related_object_ids: list[str] = Field(default_factory=list, max_length=16)
    part_name: str | None = None


class ContentManifest(ServiceModel):
    slide_index: int = Field(ge=1)
    required_text: list[str] = Field(default_factory=list)
    required_assets: list[str] = Field(default_factory=list)
    protected_object_ids: list[str] = Field(default_factory=list)
    source_sha256: str


class ShapeSnapshot(ServiceModel):
    stable_id: str
    slide_index: int = Field(ge=1)
    shape_id: int = Field(ge=1)
    name: str
    kind: str
    text: str
    x_pt: float | None = None
    y_pt: float | None = None
    width_pt: float | None = None
    height_pt: float | None = None
    fill_color: str | None = None


class PptxPatchOperation(ServiceModel):
    op_id: str
    action: Literal[
        "delete",
        "replace_text",
        "move",
        "resize",
        "recolor",
        "set_font_size",
        "duplicate",
    ]
    slide_index: int = Field(ge=1)
    target_shape_id: int = Field(ge=1)
    new_shape_id: int | None = Field(default=None, ge=1)
    new_name: str | None = None
    text: str | None = None
    x_pt: float | None = None
    y_pt: float | None = None
    width_pt: float | None = Field(default=None, gt=0)
    height_pt: float | None = Field(default=None, gt=0)
    dx_pt: float | None = None
    dy_pt: float | None = None
    color: str | None = None
    font_size_pt: float | None = Field(default=None, gt=0)
    source_instruction_ids: list[str] = Field(default_factory=list, max_length=16)
    rationale: str
    expected_effect: str

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "PptxPatchOperation":
        if self.action == "replace_text" and self.text is None:
            raise ValueError("replace_text requires text")
        if self.action == "recolor" and self.color is None:
            raise ValueError("recolor requires color")
        if self.action == "set_font_size" and self.font_size_pt is None:
            raise ValueError("set_font_size requires font_size_pt")
        if self.action == "duplicate" and self.new_shape_id is None:
            raise ValueError("duplicate requires new_shape_id")
        return self


class PptxPatchPlan(ServiceModel):
    operation: Literal["notes", "beautify"]
    source_sha256: str
    instruction_summary: str
    instructions: list[ProductionInstruction] = Field(default_factory=list, max_length=128)
    operations: list[PptxPatchOperation] = Field(default_factory=list, max_length=256)
    protected_shape_ids: list[str] = Field(default_factory=list, max_length=512)
    content_invariant: bool = True
    cleanup_executed_instructions: bool = True
    layout_strategy: Literal["preserve", "local_reflow", "global_reflow", "rebuild"]
    minimum_font_size_pt: float = Field(default=7.5, gt=0)


class PptxPatchReview(ServiceModel):
    instruction_fulfilled: bool
    content_preserved: bool
    production_notes_removed: bool
    layout_valid: bool
    editability_preserved: bool
    balanced_density: bool
    issues: list[str] = Field(default_factory=list, max_length=32)
    repair_instruction: str | None = None
    score: float = Field(ge=0, le=10)
