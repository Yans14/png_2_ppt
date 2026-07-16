from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Bounds(StrictModel):
    x: float
    y: float
    width: float
    height: float

    @field_validator("x", "y", "width", "height")
    @classmethod
    def finite_number(cls, value: float) -> float:
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("bounds values must be finite")
        return value

    @model_validator(mode="after")
    def positive_size(self) -> "Bounds":
        if self.width <= 0 or self.height <= 0:
            raise ValueError("bounds width and height must be positive")
        return self

    @property
    def area(self) -> float:
        return self.width * self.height


class GradientStop(StrictModel):
    position: float
    color: str
    opacity: float

    @field_validator("position", "opacity")
    @classmethod
    def unit_interval(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("gradient positions and opacity must be between 0 and 1")
        return value

    @field_validator("color")
    @classmethod
    def hex_color(cls, value: str) -> str:
        return normalize_hex(value)


class FillSpec(StrictModel):
    kind: Literal["none", "solid", "linear_gradient"]
    color: str | None
    opacity: float
    angle_deg: float | None
    stops: list[GradientStop]

    @field_validator("color")
    @classmethod
    def optional_hex_color(cls, value: str | None) -> str | None:
        return None if value is None else normalize_hex(value)

    @field_validator("opacity")
    @classmethod
    def opacity_range(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("fill opacity must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def validate_fill(self) -> "FillSpec":
        if self.kind == "solid" and self.color is None:
            raise ValueError("solid fill requires color")
        if self.kind == "linear_gradient" and len(self.stops) < 2:
            raise ValueError("linear gradient requires at least two stops")
        if self.kind == "linear_gradient" and self.angle_deg is None:
            raise ValueError("linear gradient requires angle_deg")
        return self


class StrokeSpec(StrictModel):
    color: str
    opacity: float
    width_px: float
    dash: Literal["solid", "dash", "dot", "dash_dot"]

    @field_validator("color")
    @classmethod
    def hex_color(cls, value: str) -> str:
        return normalize_hex(value)

    @field_validator("opacity")
    @classmethod
    def opacity_range(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("stroke opacity must be between 0 and 1")
        return value

    @field_validator("width_px")
    @classmethod
    def non_negative_width(cls, value: float) -> float:
        value = float(value)
        if value < 0:
            raise ValueError("stroke width cannot be negative")
        return value


class TextElement(StrictModel):
    kind: Literal["text"]
    id: str
    name: str
    layer: int
    group_id: str | None
    bounds: Bounds
    rotation_deg: float
    text: str
    font_family: str
    font_size_pt: float
    bold: bool
    italic: bool
    color: str
    opacity: float
    alignment: Literal["left", "center", "right", "justify"]
    vertical_alignment: Literal["top", "middle", "bottom"]
    line_spacing: float
    margin_px: float

    @field_validator("color")
    @classmethod
    def hex_color(cls, value: str) -> str:
        return normalize_hex(value)

    @field_validator("font_size_pt", "line_spacing")
    @classmethod
    def positive_value(cls, value: float) -> float:
        value = float(value)
        if value <= 0:
            raise ValueError("text size and line spacing must be positive")
        return value

    @field_validator("margin_px")
    @classmethod
    def non_negative_margin(cls, value: float) -> float:
        value = float(value)
        if value < 0:
            raise ValueError("margin cannot be negative")
        return value


PresetName = Literal[
    "rect",
    "roundRect",
    "ellipse",
    "triangle",
    "rtTriangle",
    "diamond",
    "pentagon",
    "hexagon",
    "octagon",
    "parallelogram",
    "trapezoid",
    "chevron",
    "rightArrow",
    "leftArrow",
    "upArrow",
    "downArrow",
    "curvedUpArrow",
    "curvedRightArrow",
    "curvedLeftArrow",
    "curvedDownArrow",
    "arc",
    "blockArc",
    "pie",
    "pieWedge",
    "star5",
    "star6",
    "star8",
    "donut",
    "cloud",
    "heart",
]


class ShapeElement(StrictModel):
    kind: Literal["shape"]
    id: str
    name: str
    layer: int
    group_id: str | None
    bounds: Bounds
    rotation_deg: float
    preset: PresetName
    fill: FillSpec
    stroke: StrokeSpec
    corner_radius: float | None


class LineElement(StrictModel):
    kind: Literal["line"]
    id: str
    name: str
    layer: int
    group_id: str | None
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: StrokeSpec
    arrow_start: Literal["none", "triangle", "stealth", "diamond", "oval"]
    arrow_end: Literal["none", "triangle", "stealth", "diamond", "oval"]


class PathCommand(StrictModel):
    op: Literal["M", "L", "C", "Q", "Z"]
    x: float | None
    y: float | None
    x1: float | None
    y1: float | None
    x2: float | None
    y2: float | None

    @model_validator(mode="after")
    def command_coordinates(self) -> "PathCommand":
        if self.op in {"M", "L", "C", "Q"} and (self.x is None or self.y is None):
            raise ValueError(f"{self.op} requires x and y")
        if self.op == "C" and None in (self.x1, self.y1, self.x2, self.y2):
            raise ValueError("C requires two control points")
        if self.op == "Q" and None in (self.x1, self.y1):
            raise ValueError("Q requires one control point")
        return self


class PathElement(StrictModel):
    kind: Literal["path"]
    id: str
    name: str
    layer: int
    group_id: str | None
    bounds: Bounds
    rotation_deg: float
    fill: FillSpec
    stroke: StrokeSpec
    commands: list[PathCommand]

    @field_validator("commands")
    @classmethod
    def path_is_not_empty(cls, value: list[PathCommand]) -> list[PathCommand]:
        if len(value) < 2:
            raise ValueError("path requires at least two commands")
        if value[0].op != "M":
            raise ValueError("path must start with M")
        return value


class ImageElement(StrictModel):
    kind: Literal["image"]
    id: str
    name: str
    layer: int
    group_id: str | None
    bounds: Bounds
    source_region: Bounds
    rotation_deg: float
    opacity: float
    preserve_aspect: bool
    alt_text: str
    content_type: Literal["photo", "texture", "raster_illustration"]

    @field_validator("opacity")
    @classmethod
    def opacity_range(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("image opacity must be between 0 and 1")
        return value


class ComponentInstanceElement(StrictModel):
    kind: Literal["component"]
    id: str
    name: str
    layer: int
    group_id: str | None
    bounds: Bounds
    rotation_deg: float
    opacity: float
    component_id: str

    @field_validator("opacity")
    @classmethod
    def opacity_range(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("component opacity must be between 0 and 1")
        return value


PrimitiveElement = Annotated[
    Union[TextElement, ShapeElement, LineElement, PathElement],
    Field(discriminator="kind"),
]

ElementSpec = Annotated[
    Union[
        TextElement,
        ShapeElement,
        LineElement,
        PathElement,
        ImageElement,
        ComponentInstanceElement,
    ],
    Field(discriminator="kind"),
]


class ComponentSpec(StrictModel):
    id: str
    name: str
    elements: list[PrimitiveElement]


class SlideSpec(StrictModel):
    version: Literal["1.0"]
    source_width: int
    source_height: int
    background: FillSpec
    components: list[ComponentSpec]
    # A component library without a placed top-level element renders a blank
    # slide.  Requiring one placed element also emits ``minItems: 1`` in the
    # structured-output schema, preventing the model from returning an
    # apparently valid but unusable reconstruction.
    elements: list[ElementSpec] = Field(min_length=1)
    reconstruction_notes: list[str]

    @model_validator(mode="after")
    def validate_graph(self) -> "SlideSpec":
        if self.source_width <= 0 or self.source_height <= 0:
            raise ValueError("source dimensions must be positive")

        component_ids = [component.id for component in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("component ids must be unique")
        component_set = set(component_ids)

        element_ids: list[str] = []
        for element in self.elements:
            element_ids.append(element.id)
            if isinstance(element, ComponentInstanceElement) and element.component_id not in component_set:
                raise ValueError(f"unknown component_id: {element.component_id}")
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("top-level element ids must be unique")

        for component in self.components:
            local_ids = [element.id for element in component.elements]
            if len(local_ids) != len(set(local_ids)):
                raise ValueError(f"component element ids must be unique in {component.id}")
        return self

    def count_objects(self) -> dict[str, int]:
        counts = {
            "text": 0,
            "shape": 0,
            "line": 0,
            "path": 0,
            "image": 0,
            "component": 0,
            "expanded_component_objects": 0,
        }
        components = {item.id: item for item in self.components}
        for element in self.elements:
            counts[element.kind] += 1
            if isinstance(element, ComponentInstanceElement):
                counts["expanded_component_objects"] += len(components[element.component_id].elements)
        return counts

    def full_slide_images(self, threshold: float = 0.72) -> list[str]:
        slide_area = self.source_width * self.source_height
        return [
            element.id
            for element in self.elements
            if isinstance(element, ImageElement) and element.bounds.area / slide_area >= threshold
        ]

    def suspicious_full_slide_images(self, threshold: float = 0.72) -> list[str]:
        """Full-slide raster artwork is suspicious; genuine photos/textures are allowed."""

        slide_area = self.source_width * self.source_height
        return [
            element.id
            for element in self.elements
            if isinstance(element, ImageElement)
            and element.bounds.area / slide_area >= threshold
            and element.content_type == "raster_illustration"
        ]


class SlidePatch(StrictModel):
    """Small refinement delta, avoiding regeneration of an already-correct slide graph."""

    background: FillSpec | None
    upsert_components: list[ComponentSpec]
    remove_component_ids: list[str]
    upsert_elements: list[ElementSpec]
    remove_element_ids: list[str]
    reconstruction_notes: list[str] | None

    @model_validator(mode="after")
    def validate_patch_ids(self) -> "SlidePatch":
        component_ids = [item.id for item in self.upsert_components]
        element_ids = [item.id for item in self.upsert_elements]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("upsert component ids must be unique")
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("upsert element ids must be unique")
        if set(component_ids) & set(self.remove_component_ids):
            raise ValueError("a component cannot be both upserted and removed")
        if set(element_ids) & set(self.remove_element_ids):
            raise ValueError("an element cannot be both upserted and removed")
        return self


def apply_slide_patch(spec: SlideSpec, patch: SlidePatch) -> SlideSpec:
    """Apply a stable-ID patch while preserving the order of untouched objects."""

    component_updates = {item.id: item for item in patch.upsert_components}
    removed_components = set(patch.remove_component_ids)
    components: list[ComponentSpec] = []
    for item in spec.components:
        if item.id in removed_components:
            continue
        components.append(component_updates.pop(item.id, item))
    components.extend(component_updates.values())

    element_updates = {item.id: item for item in patch.upsert_elements}
    removed_elements = set(patch.remove_element_ids)
    elements: list[ElementSpec] = []
    for item in spec.elements:
        if item.id in removed_elements:
            continue
        elements.append(element_updates.pop(item.id, item))
    elements.extend(element_updates.values())

    return SlideSpec(
        version=spec.version,
        source_width=spec.source_width,
        source_height=spec.source_height,
        background=patch.background or spec.background,
        components=components,
        elements=elements,
        reconstruction_notes=(
            patch.reconstruction_notes
            if patch.reconstruction_notes is not None
            else spec.reconstruction_notes
        ),
    )


def clamp_slide_spec(spec: SlideSpec) -> SlideSpec:
    """Keep generated object extents inside the PowerPoint slide canvas."""

    payload = spec.model_dump(mode="json")
    retained: list[dict[str, object]] = []
    changed_ids: list[str] = []
    for element in payload["elements"]:
        if element["kind"] == "line":
            original = (element["x1"], element["y1"], element["x2"], element["y2"])
            element["x1"] = min(spec.source_width, max(0.0, float(element["x1"])))
            element["x2"] = min(spec.source_width, max(0.0, float(element["x2"])))
            element["y1"] = min(spec.source_height, max(0.0, float(element["y1"])))
            element["y2"] = min(spec.source_height, max(0.0, float(element["y2"])))
            if original != (element["x1"], element["y1"], element["x2"], element["y2"]):
                changed_ids.append(str(element["id"]))
            retained.append(element)
            continue

        bounds = element.get("bounds")
        if not isinstance(bounds, dict):
            retained.append(element)
            continue
        old_x = float(bounds["x"])
        old_y = float(bounds["y"])
        old_width = float(bounds["width"])
        old_height = float(bounds["height"])
        new_x = max(0.0, old_x)
        new_y = max(0.0, old_y)
        new_right = min(float(spec.source_width), old_x + old_width)
        new_bottom = min(float(spec.source_height), old_y + old_height)
        if new_right <= new_x or new_bottom <= new_y:
            changed_ids.append(str(element["id"]))
            continue
        new_width = new_right - new_x
        new_height = new_bottom - new_y
        element_changed = False
        if (new_x, new_y, new_width, new_height) != (old_x, old_y, old_width, old_height):
            if element["kind"] == "image":
                source_region = element["source_region"]
                u0 = (new_x - old_x) / old_width
                v0 = (new_y - old_y) / old_height
                u1 = (new_right - old_x) / old_width
                v1 = (new_bottom - old_y) / old_height
                source_x = float(source_region["x"])
                source_y = float(source_region["y"])
                source_width = float(source_region["width"])
                source_height = float(source_region["height"])
                source_region.update(
                    {
                        "x": source_x + u0 * source_width,
                        "y": source_y + v0 * source_height,
                        "width": (u1 - u0) * source_width,
                        "height": (v1 - v0) * source_height,
                    }
                )
            bounds.update(
                {"x": new_x, "y": new_y, "width": new_width, "height": new_height}
            )
            element_changed = True

        rotation = float(element.get("rotation_deg", 0.0) or 0.0)
        if rotation:
            radians = math.radians(rotation)
            width = float(bounds["width"])
            height = float(bounds["height"])
            rotated_width = abs(width * math.cos(radians)) + abs(
                height * math.sin(radians)
            )
            rotated_height = abs(width * math.sin(radians)) + abs(
                height * math.cos(radians)
            )
            center_x = float(bounds["x"]) + width / 2
            center_y = float(bounds["y"]) + height / 2
            fit_scale = min(
                1.0,
                spec.source_width / rotated_width,
                spec.source_height / rotated_height,
            )
            if fit_scale < 1.0:
                width *= fit_scale
                height *= fit_scale
                bounds["width"] = width
                bounds["height"] = height
                bounds["x"] = center_x - width / 2
                bounds["y"] = center_y - height / 2
                rotated_width *= fit_scale
                rotated_height *= fit_scale
                element_changed = True
            left = center_x - rotated_width / 2
            right = center_x + rotated_width / 2
            top = center_y - rotated_height / 2
            bottom = center_y + rotated_height / 2
            shift_x = -left if left < 0 else min(0.0, spec.source_width - right)
            shift_y = -top if top < 0 else min(0.0, spec.source_height - bottom)
            if shift_x or shift_y:
                bounds["x"] = float(bounds["x"]) + shift_x
                bounds["y"] = float(bounds["y"]) + shift_y
                element_changed = True
        if element_changed:
            changed_ids.append(str(element["id"]))
        retained.append(element)
    payload["elements"] = retained
    if changed_ids:
        payload["reconstruction_notes"] = [
            *payload["reconstruction_notes"],
            "Clamped canvas overflow: " + ", ".join(changed_ids),
        ]
    return SlideSpec.model_validate(payload)


def normalize_hex(value: str) -> str:
    value = str(value).strip().lstrip("#").upper()
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6 or any(char not in "0123456789ABCDEF" for char in value):
        raise ValueError(f"invalid RGB color: {value!r}")
    return f"#{value}"


def openai_schema() -> dict:
    """Return strict JSON schema accepted by Responses structured outputs."""

    schema = SlideSpec.model_json_schema()

    def normalize(node: object) -> object:
        if isinstance(node, list):
            return [normalize(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, object] = {}
        for key, value in node.items():
            # OpenAI structured outputs supports anyOf but rejects discriminator-style oneOf.
            if key == "oneOf":
                result["anyOf"] = normalize(value)
            elif key == "discriminator":
                continue
            else:
                result[key] = normalize(value)
        return result

    return normalize(schema)  # type: ignore[return-value]
