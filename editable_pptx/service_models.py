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
    TEMPLATE_IMPORT = "template_import"
    TEMPLATE_REINDEX = "template_reindex"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_QUALITY = "failed_quality"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.FAILED_QUALITY,
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
    MANIFEST = "manifest"
    SCORECARD = "scorecard"
    TRACE = "trace"
    TEMPLATE_MATCH = "template_match"


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


class TemplateFamilyResource(ServiceModel):
    id: str
    name: str
    active: bool = True
    inferred: bool = True
    template_count: int = 0
    created_at: str
    updated_at: str


class TemplateResource(ServiceModel):
    id: str
    family_id: str
    name: str
    source_sha256: str
    perceptual_hash: str | None = None
    slide_count: int = Field(ge=1)
    active: bool = True
    deleted_at: str | None = None
    index_version: int = Field(ge=1)
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TemplateSlideResource(ServiceModel):
    id: str
    template_id: str
    family_id: str
    slide_index: int = Field(ge=1)
    archetype: str
    structural_features: dict[str, float | int | str] = Field(default_factory=dict)
    style_features: dict[str, Any] = Field(default_factory=dict)
    preview_path: str | None = None
    perceptual_hash: str | None = None


class TemplateMatch(ServiceModel):
    template_slide_id: str
    template_id: str
    family_id: str
    source_slide_index: int = Field(ge=1)
    slide_index: int = Field(ge=1)
    structural_score: float = Field(ge=0, le=1)
    style_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    selected: bool = False
    rationale: str = ""


class TemplateFamilyUpdate(ServiceModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


class TemplateFamilyMergeRequest(ServiceModel):
    target_family_id: str
    source_family_ids: list[str] = Field(min_length=1, max_length=64)


class TemplateFamilySplitRequest(ServiceModel):
    template_ids: list[str] = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=120)


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


class SlideInvariantSnapshot(ServiceModel):
    slide_index: int = Field(ge=1)
    lexical_tokens: list[str] = Field(default_factory=list)
    numeric_tokens: list[str] = Field(default_factory=list)
    table_data: list[list[str]] = Field(default_factory=list)
    chart_data: list[dict[str, Any]] = Field(default_factory=list)
    image_hashes: list[str] = Field(default_factory=list)
    logo_hashes: list[str] = Field(default_factory=list)
    logo_states: list[dict[str, Any]] = Field(default_factory=list)
    native_object_counts: dict[str, int] = Field(default_factory=dict)
    relationship_hash: str
    animation_hash: str


class DeckInvariantManifest(ServiceModel):
    source_sha256: str
    slide_count: int = Field(ge=1)
    slides: list[SlideInvariantSnapshot]
    package_assets: dict[str, str] = Field(default_factory=dict)
    protected_parts: dict[str, str] = Field(default_factory=dict)


class InvariantViolation(ServiceModel):
    code: str
    message: str
    slide_index: int | None = Field(default=None, ge=1)
    severity: Literal["blocker", "major", "minor"] = "blocker"


class InvariantReport(ServiceModel):
    passed: bool
    source_sha256: str
    candidate_sha256: str
    violations: list[InvariantViolation] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


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
        "align",
        "distribute",
        "set_typography",
        "set_paragraph",
        "set_fill",
        "set_border",
        "bring_to_front",
        "send_to_back",
        "crop_picture",
        "style_table",
        "style_chart",
        "group",
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
    font_family: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    text_color: str | None = None
    border_color: str | None = None
    border_width_pt: float | None = Field(default=None, ge=0)
    paragraph_alignment: Literal["left", "center", "right", "justify"] | None = None
    alignment: Literal["left", "center", "right", "top", "middle", "bottom"] | None = None
    distribution: Literal["horizontal", "vertical"] | None = None
    peer_shape_ids: list[int] = Field(default_factory=list, max_length=64)
    crop_left: float | None = Field(default=None, ge=0, le=1)
    crop_top: float | None = Field(default=None, ge=0, le=1)
    crop_right: float | None = Field(default=None, ge=0, le=1)
    crop_bottom: float | None = Field(default=None, ge=0, le=1)
    accent_colors: list[str] = Field(default_factory=list, max_length=12)
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
        if self.action == "group" and (
            self.new_shape_id is None or not self.peer_shape_ids
        ):
            raise ValueError("group requires new_shape_id and peer_shape_ids")
        if self.action == "align" and self.alignment is None:
            raise ValueError("align requires alignment")
        if self.action == "distribute" and self.distribution is None:
            raise ValueError("distribute requires distribution")
        if self.action == "set_border" and self.border_color is None:
            raise ValueError("set_border requires border_color")
        if self.action == "set_paragraph" and self.paragraph_alignment is None:
            raise ValueError("set_paragraph requires paragraph_alignment")
        if self.action == "crop_picture" and all(
            value is None
            for value in (self.crop_left, self.crop_top, self.crop_right, self.crop_bottom)
        ):
            raise ValueError("crop_picture requires at least one crop value")
        return self


class PptxSectionOperation(ServiceModel):
    op_id: str
    action: Literal["add", "remove", "rename"]
    name: str = Field(min_length=1, max_length=120)
    new_name: str | None = Field(default=None, min_length=1, max_length=120)
    slide_indices: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_section_operation(self) -> "PptxSectionOperation":
        if self.action == "add" and not self.slide_indices:
            raise ValueError("add section requires slide_indices")
        if self.action == "rename" and not self.new_name:
            raise ValueError("rename section requires new_name")
        if any(item < 1 for item in self.slide_indices):
            raise ValueError("section slide indices are 1-based")
        self.slide_indices = sorted(set(self.slide_indices))
        return self


class PptxPatchPlan(ServiceModel):
    operation: Literal["notes", "beautify"]
    source_sha256: str
    instruction_summary: str
    instructions: list[ProductionInstruction] = Field(default_factory=list, max_length=128)
    operations: list[PptxPatchOperation] = Field(default_factory=list, max_length=256)
    section_operations: list[PptxSectionOperation] = Field(
        default_factory=list, max_length=64
    )
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
    rubric_scores: dict[str, float] = Field(default_factory=dict)
    slide_scores: dict[int, float] = Field(default_factory=dict)
    relative_improvement: float | None = Field(default=None, ge=-10, le=10)


class BeautifyAnalysis(ServiceModel):
    source_sha256: str
    archetypes: dict[int, str]
    restyle_mode: Literal["conservative", "structural", "rebuild"]
    selected_family_id: str | None = None
    selected_layouts: dict[int, str] = Field(default_factory=dict)
    template_matches: list[TemplateMatch] = Field(default_factory=list)
    protected_shape_ids: list[str] = Field(default_factory=list)
    safe_zones: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    fallback_reasons: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class BeautifyCandidateScore(ServiceModel):
    candidate_index: int = Field(ge=1, le=3)
    strategy: str
    deterministic_passed: bool
    reviewer_score: float = Field(ge=0, le=10)
    reviewer_scores: dict[str, float] = Field(default_factory=dict)
    approved: bool
    issues: list[str] = Field(default_factory=list)
    repair_instruction: str | None = None
    artifact_name: str | None = None
    trace_id: str | None = None


class BeautifyTraceRecord(ServiceModel):
    trace_id: str
    job_id: str
    stage: str
    agent: str | None = None
    model: str
    candidate_index: int | None = Field(default=None, ge=1, le=3)
    started_at: str
    finished_at: str
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
