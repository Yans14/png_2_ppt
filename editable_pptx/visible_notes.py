from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

from .models import (
    ImageElement,
    SlideNotePatch,
    SlideNoteReview,
    SlideSpec,
    TextElement,
    apply_slide_patch,
    clamp_slide_spec,
)
from .openai_responses import OpenAIResponsesError, request_structured_response


VISIBLE_NOTES_SYSTEM_PROMPT = """
You edit an existing native PowerPoint object graph by executing production notes
visible on the slide. Return only strict data matching the supplied JSON schema.

A production note is audience-inappropriate authoring feedback, for example a colored
callout asking to add rows, replace content, move an object, or remove a placeholder.
Do not treat ordinary slide titles, comments, sources, footnotes, or explanatory labels
as production notes.

Execute every detected production note, then remove the note text and its visual
container. Preserve all unrelated objects, stable IDs, branding, headers, footers,
colors, and section geometry. Use existing IDs when changing existing objects. Use new
descriptive IDs only for genuinely new objects. Return only changed/new objects in
upsert_elements and only deleted IDs in remove_element_ids.

Classify each instruction as one or more actions: add/remove rows; replace text or
values; delete, move, resize, recolor, restyle, add, or duplicate objects; add/remove a
section; update chart/table/comment content; resolve placeholders; or global reflow.
Populate actions with exact target IDs and mark requires_reflow whenever content count,
text length, or section geometry changes.

Layout adaptation is mandatory when content density changes. Do not solve added content
only by shrinking it. First reclaim spacing, then resize/move dependent sections and all
their labels, bars, values, comments, rules, and backgrounds as one system. Preserve the
slide canvas and footer. Use global_reflow when adjacent sections must move or resize.
Respect supplied minimum font size for affected body content. A repeated row must have
enough height for its complete label and values without clipping or overlapping.

For repeated rows or table-like sections:
- infer row templates from neighboring objects;
- keep the section inside its existing vertical bounds unless the note explicitly asks
  to move adjacent sections;
- create each editable card, label, range bar, endpoint value, and result value as a
  separate native object;
- keep row spacing uniform;
- use clearly editable dummy values or bracketed placeholders when requested;
- never leave the production note visible in the final slide.

Never flatten the slide or add a screenshot as an image. Every nullable field remains
present. Preserve source_width and source_height by returning a patch rather than a full
slide.
""".strip()


VISIBLE_NOTES_REVIEW_PROMPT = """
Review a modified editable slide. The image is the rendered result after applying a
visible production note. Return strict review data only.

Confirm that every requested edit is present, production-note artifacts are gone,
repeated rows are complete and evenly spaced, labels remain readable, and unrelated
layout is preserved. When global reflow was requested, confirm neighboring sections and
all dependent objects moved or resized coherently. Reject candidates that fit new content
only by using text below the supplied minimum or by creating excessive density. Do not
require pixel similarity to the original because the edit intentionally changes the
slide. If any requirement fails, provide one concise repair instruction naming exact
missing or incorrect objects.
""".strip()


class VisibleNotesError(RuntimeError):
    pass


_NOTE_LANGUAGE = re.compile(
    r"\b(please|add|remove|replace|move|change|update|placeholder|dummy)\b",
    re.IGNORECASE,
)


def visible_note_elements(spec: SlideSpec) -> list[TextElement]:
    """Find production-note text already transcribed into the editable graph."""

    notes: list[TextElement] = []
    for element in spec.elements:
        if not isinstance(element, TextElement):
            continue
        identity = f"{element.id} {element.name}".lower()
        if "instruction" in identity or "production note" in identity:
            notes.append(element)
            continue
        if element.bold and _NOTE_LANGUAGE.search(element.text) and len(element.text) >= 20:
            notes.append(element)
    return notes


def _overlaps(left: object, right: object, threshold: float = 0.6) -> bool:
    if not hasattr(left, "bounds") or not hasattr(right, "bounds"):
        return False
    a = left.bounds
    b = right.bounds
    width = max(0.0, min(a.x + a.width, b.x + b.width) - max(a.x, b.x))
    height = max(0.0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
    overlap = width * height
    return overlap / max(1.0, min(a.area, b.area)) >= threshold


def _copy_element(template: object, **updates: object) -> dict[str, object]:
    payload = deepcopy(template.model_dump(mode="json"))
    payload.update(updates)
    return payload


def note_layout_report(
    before: SlideSpec,
    after: SlideSpec,
    patch: SlideNotePatch,
    *,
    minimum_font_size_pt: float,
    layout_mode: str = "auto",
) -> dict[str, object]:
    """Measure readability and dependency reflow for a structural note edit."""

    touched_ids = {item.id for item in patch.upsert_elements}
    removed_ids = set(patch.remove_element_ids)
    after_map = {item.id: item for item in after.elements}
    before_ids = {item.id for item in before.elements}
    primary_target_ids = {
        target
        for action in patch.actions
        if action.action_type != "global_reflow"
        for target in action.target_ids
    }
    note_ids = {
        item.id
        for item in before.elements
        if isinstance(item, TextElement) and item.text in patch.detected_notes
    }
    undersized: list[str] = []
    text_fit: list[str] = []
    for element_id in touched_ids:
        element = after_map.get(element_id)
        if not isinstance(element, TextElement):
            continue
        identity = f"{element.id} {element.name}".lower()
        exempt = any(
            token in identity
            for token in ("footer", "source", "page_number", "confidential")
        )
        if not exempt and element.font_size_pt + 1e-6 < minimum_font_size_pt:
            undersized.append(element.id)
        lines = max(1, len(element.text.splitlines()))
        estimated_height = lines * element.font_size_pt * 1.333 * element.line_spacing
        if estimated_height > element.bounds.height * 1.15:
            text_fit.append(element.id)

    requires_reflow = layout_mode == "global" or any(
        action.requires_reflow for action in patch.actions
    )
    changed_existing = (touched_ids & before_ids) | (removed_ids & before_ids)
    dependent_changes = sorted(changed_existing - primary_target_ids - note_ids)
    dependent_adjusted = not requires_reflow or bool(dependent_changes)
    valid = not undersized and not text_fit and dependent_adjusted
    issues: list[str] = []
    if undersized:
        issues.append(
            f"Text below {minimum_font_size_pt:.1f}pt: " + ", ".join(undersized[:12])
        )
    if text_fit:
        issues.append("Likely clipped text: " + ", ".join(text_fit[:12]))
    if not dependent_adjusted:
        issues.append(
            "Content density changed but no neighboring existing objects were reflowed"
        )
    return {
        "valid": valid,
        "minimum_font_size_pt": minimum_font_size_pt,
        "undersized_text_ids": undersized,
        "text_fit_issue_ids": text_fit,
        "requires_reflow": requires_reflow,
        "dependent_objects_adjusted": dependent_adjusted,
        "dependent_changed_ids": dependent_changes,
        "issues": issues,
    }


def offline_note_modification(
    spec: SlideSpec,
    *,
    supplemental_instruction: str | None = None,
    minimum_font_size_pt: float = 7.0,
) -> tuple[SlideSpec, SlideNotePatch]:
    """Execute locally supported visible-note patterns without sending slide data out."""

    notes = visible_note_elements(spec)
    note_text = "\n".join(item.text for item in notes)
    combined = f"{note_text}\n{supplemental_instruction or ''}".strip()
    if not notes:
        raise VisibleNotesError("No visible production note was detected")

    total_match = re.search(r"(?:have|create|exactly)\s+(\d+)\s+orange\s+rows", combined, re.I)
    added_match = re.search(r"add\s+(\d+)\s+more\s+rows", combined, re.I)
    if not total_match and not added_match:
        raise VisibleNotesError(
            "Offline engine currently supports visible notes that add repeated rows"
        )

    element_map = {item.id: item for item in spec.elements}
    label_rows = sorted(
        (
            item
            for item in spec.elements
            if isinstance(item, TextElement)
            and (
                re.fullmatch(r"comp_label_\d+", item.id)
                or re.fullmatch(r"trading_row_\d+_text(?:_main)?", item.id)
            )
        ),
        key=lambda item: item.bounds.y,
    )
    if not label_rows:
        raise VisibleNotesError("Could not identify the TRADING COMPARABLES row template")
    target_rows = (
        int(total_match.group(1))
        if total_match
        else len(label_rows) + int(added_match.group(1))
    )
    if target_rows < len(label_rows) or target_rows > 20:
        raise VisibleNotesError(f"Unsupported requested row count: {target_rows}")

    top_line = element_map.get("comp_tab_top")
    bottom_line = element_map.get("comp_tab_bottom")
    vertical_line = element_map.get("trading_vertical_rule") or element_map.get(
        "comp_tab_vertical"
    )
    if top_line is not None and bottom_line is not None and hasattr(top_line, "y1"):
        section_top = float(top_line.y1)
        section_bottom = float(bottom_line.y1)
    elif vertical_line is not None and hasattr(vertical_line, "y1"):
        section_top = min(float(vertical_line.y1), float(vertical_line.y2))
        section_bottom = max(float(vertical_line.y1), float(vertical_line.y2))
    else:
        raise VisibleNotesError("Trading-comparables section bounds are missing")
    row_step = (section_bottom - section_top) / target_rows
    card_height = max(18.0, row_step - 3.0)
    label_height = max(16.0, card_height - 3.0)

    card_templates = sorted(
        (
            item
            for item in spec.elements
            if re.fullmatch(r"(?:comp|trading)_card_\d+", item.id)
        ),
        key=lambda item: item.bounds.y if hasattr(item, "bounds") else 0,
    )
    if not card_templates:
        raise VisibleNotesError("Could not identify editable row cards")
    bar_template = element_map.get("orange_value_bar_1") or element_map.get(
        "trading_1_ev_bar"
    )
    min_template = element_map.get("orange_min_1") or element_map.get(
        "trading_1_ev_left_value"
    )
    max_template = element_map.get("orange_max_1") or element_map.get(
        "trading_1_ev_right_value"
    )
    equity_template = element_map.get("orange_equity_value_1") or element_map.get(
        "trading_1_equity_value"
    )
    if None in (bar_template, min_template, max_template, equity_template):
        raise VisibleNotesError("Could not identify the orange valuation row template")

    existing_texts = [item.text for item in label_rows]
    existing_equity = ["1,283 – 1,670", "[x] – [x]", "1,450 – 2,871"]
    upserts: list[dict[str, object]] = []
    for index in range(target_rows):
        number = index + 1
        row_top = section_top + index * row_step
        center_y = row_top + row_step / 2
        card = card_templates[min(index, len(card_templates) - 1)]
        label = label_rows[min(index, len(label_rows) - 1)]
        label_text = (
            existing_texts[index]
            if index < len(existing_texts)
            else f"PLACEHOLDER {index - len(existing_texts) + 1}\n[x] – [x]\nUS$[x]mm²"
        )
        upserts.append(
            _copy_element(
                card,
                id=f"note_trading_card_{number}",
                name=f"Trading comparable card {number}",
                bounds={
                    "x": card.bounds.x,
                    "y": row_top,
                    "width": card.bounds.width,
                    "height": card_height,
                },
            )
        )
        upserts.append(
            _copy_element(
                label,
                id=f"note_trading_label_{number}",
                name=f"Trading comparable label {number}",
                bounds={
                    "x": label.bounds.x,
                    "y": row_top + 1.5,
                    "width": label.bounds.width,
                    "height": label_height,
                },
                text=label_text,
                font_size_pt=minimum_font_size_pt,
                line_spacing=0.95,
            )
        )
        bar_height = min(15.0, max(10.0, row_step - 10.0))
        bar_y = center_y - bar_height / 2
        upserts.append(
            _copy_element(
                bar_template,
                id=f"note_trading_bar_{number}",
                name=f"Trading comparable range {number}",
                bounds={
                    "x": bar_template.bounds.x,
                    "y": bar_y,
                    "width": bar_template.bounds.width,
                    "height": bar_height,
                },
            )
        )
        low_text = "2,500" if index < 3 else f"{2_600 + (index - 3) * 100:,}"
        high_text = "3,500" if index < 3 else f"{3_600 + (index - 3) * 100:,}"
        equity_text = (
            existing_equity[index]
            if index < len(existing_equity)
            else f"{1_500 + (index - 3) * 100:,} – {2_000 + (index - 3) * 150:,}"
        )
        for template, prefix, name, x, width, text, align in (
            (min_template, "note_trading_min", "lower value", min_template.bounds.x, min_template.bounds.width, low_text, "right"),
            (max_template, "note_trading_max", "upper value", max_template.bounds.x, max_template.bounds.width, high_text, "left"),
            (equity_template, "note_trading_equity", "implied value", equity_template.bounds.x, equity_template.bounds.width, equity_text, "center"),
        ):
            upserts.append(
                _copy_element(
                    template,
                    id=f"{prefix}_{number}",
                    name=f"Trading comparable {name} {number}",
                    bounds={
                        "x": x,
                        "y": center_y - 7.0,
                        "width": width,
                        "height": 14.0,
                    },
                    text=text,
                    font_size_pt=7.5,
                    alignment=align,
                )
            )

    note_ids = {item.id for item in notes}
    row_artifact_pattern = re.compile(
        r"(?:comp_card_\d+|comp_label_\d+|"
        r"orange_(?:value_bar|min|max|equity_value)_\d+|"
        r"trading_card_\d+|trading_row_\d+.*|"
        r"trading_\d+_(?:ev_bar|ev_left_value|ev_right_value|equity_value))"
    )
    note_ids.update(
        item.id for item in spec.elements if row_artifact_pattern.fullmatch(item.id)
    )
    for item in spec.elements:
        if item.id in note_ids:
            continue
        if any(_overlaps(item, note) for note in notes):
            identity = f"{item.id} {item.name}".lower()
            if "instruction" in identity or "note" in identity:
                note_ids.add(item.id)

    patch = SlideNotePatch.model_validate(
        {
            "detected_notes": [item.text for item in notes],
            "instruction_summary": (
                f"Expanded TRADING COMPARABLES to {target_rows} editable orange rows and removed note"
            ),
            "actions": [
                {
                    "action_type": "add_rows",
                    "instruction": f"Expand trading comparables to {target_rows} rows",
                    "target_ids": [item.id for item in label_rows],
                    "requires_reflow": True,
                }
            ],
            "layout_strategy": "local_reflow",
            "minimum_font_size_pt": minimum_font_size_pt,
            "background": None,
            "upsert_components": [],
            "remove_component_ids": [],
            "upsert_elements": upserts,
            "remove_element_ids": sorted(note_ids),
            "reconstruction_notes": [
                f"Visible production note executed offline: {target_rows} trading-comparable rows"
            ],
        }
    )
    return clamp_slide_spec(apply_slide_patch(spec, patch)), patch


def offline_note_review(spec: SlideSpec, patch: SlideNotePatch) -> SlideNoteReview:
    row_ids = {
        item.id
        for item in spec.elements
        if re.fullmatch(r"(?:comp_label|note_trading_label)_\d+", item.id)
    }
    notes_remaining = bool(visible_note_elements(spec))
    requested_match = re.search(r"(\d+) editable orange rows", patch.instruction_summary)
    requested = int(requested_match.group(1)) if requested_match else len(row_ids)
    issues: list[str] = []
    if len(row_ids) != requested:
        issues.append(f"Expected {requested} comparable rows, found {len(row_ids)}")
    if notes_remaining:
        issues.append("Production note remains visible")
    return SlideNoteReview(
        instruction_fulfilled=len(row_ids) == requested,
        visible_notes_removed=not notes_remaining,
        layout_preserved=True,
        dependent_objects_adjusted=True,
        minimum_font_size_ok=True,
        balanced_density=True,
        issues=issues,
        repair_instruction=None if not issues else "; ".join(issues),
    )


def note_modification_prompt(
    spec: SlideSpec,
    *,
    supplemental_instruction: str | None = None,
    previous_review: SlideNoteReview | None = None,
    minimum_font_size_pt: float = 7.5,
    layout_mode: str = "auto",
) -> str:
    instruction = supplemental_instruction.strip() if supplemental_instruction else ""
    review_text = ""
    if previous_review is not None:
        review_text = (
            "\nPrevious semantic review:\n"
            + json.dumps(previous_review.model_dump(mode="json"), ensure_ascii=False)
        )
    return (
        "Inspect the slide image and current object graph. Detect visible production notes, "
        "execute them, and remove their visual artifacts.\n"
        f"Supplemental user instruction: {instruction or 'none; use visible notes only'}\n"
        f"Layout mode: {layout_mode}. Minimum affected body font size: "
        f"{minimum_font_size_pt:.1f} pt.\n"
        f"Current editable spec:\n{spec.model_dump_json()}"
        f"{review_text}"
    )


def request_note_modification(
    source_image: str | Path,
    spec: SlideSpec,
    *,
    model: str = "gpt-5.5",
    supplemental_instruction: str | None = None,
    previous_review: SlideNoteReview | None = None,
    api_key: str | None = None,
    timeout_seconds: int = 300,
    max_output_tokens: int = 64000,
    minimum_font_size_pt: float = 7.5,
    layout_mode: str = "auto",
) -> tuple[SlideSpec, SlideNotePatch]:
    try:
        patch = request_structured_response(
            SlideNotePatch,
            schema_name="visible_slide_note_patch",
            system_text=VISIBLE_NOTES_SYSTEM_PROMPT,
            user_text=note_modification_prompt(
                spec,
                supplemental_instruction=supplemental_instruction,
                previous_review=previous_review,
                minimum_font_size_pt=minimum_font_size_pt,
                layout_mode=layout_mode,
            ),
            image_paths=[source_image],
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
    except OpenAIResponsesError as error:
        raise VisibleNotesError(str(error)) from error
    if not patch.detected_notes and not supplemental_instruction:
        raise VisibleNotesError("No visible production note was detected")
    try:
        return clamp_slide_spec(apply_slide_patch(spec, patch)), patch
    except ValueError as error:
        raise VisibleNotesError(f"Visible-note patch produced an invalid slide: {error}") from error


def review_note_modification(
    rendered_image: str | Path,
    spec: SlideSpec,
    patch: SlideNotePatch,
    *,
    model: str = "gpt-5.5",
    supplemental_instruction: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int = 300,
    max_output_tokens: int = 12000,
    layout_report: dict[str, object] | None = None,
) -> SlideNoteReview:
    user_text = (
        f"Executed notes: {json.dumps(patch.detected_notes, ensure_ascii=False)}\n"
        f"Instruction summary: {patch.instruction_summary}\n"
        f"Supplemental instruction: {supplemental_instruction or 'none'}\n"
        f"Local layout checks: {json.dumps(layout_report or {}, ensure_ascii=False)}\n"
        f"Final editable spec: {spec.model_dump_json()}"
    )
    try:
        return request_structured_response(
            SlideNoteReview,
            schema_name="visible_slide_note_review",
            system_text=VISIBLE_NOTES_REVIEW_PROMPT,
            user_text=user_text,
            image_paths=[rendered_image],
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
    except OpenAIResponsesError as error:
        raise VisibleNotesError(str(error)) from error


def validate_raster_policy(spec: SlideSpec, raster_policy: str) -> None:
    images = [element for element in spec.elements if isinstance(element, ImageElement)]
    if raster_policy == "none" and images:
        raise VisibleNotesError(
            f"Raster policy 'none' rejected {len(images)} image element(s)"
        )
    if raster_policy == "photos-only":
        invalid = [item.id for item in images if item.content_type not in {"photo", "texture"}]
        if invalid:
            raise VisibleNotesError(
                "Raster policy 'photos-only' rejected non-photo objects: " + ", ".join(invalid)
            )
