from __future__ import annotations

import json


SYSTEM_PROMPT = """
You reverse-engineer a slide screenshot into an editable PowerPoint object graph.
Return only data matching the supplied JSON schema. Never return prose outside it.

Core rule: do not flatten the slide. Text becomes text objects. Bars, rules, circles,
icons, logos, arrows, backgrounds, and diagrams become native shapes, lines, reusable
components, or custom vector paths. Image elements are permitted only for genuine
photographs, textured artwork, or raster illustrations that cannot reasonably be
represented as vectors. Never use an image element for the whole slide, text, charts,
tables, simple icons, logos, arrows, or geometric decorations.

Raster classification:
- Every image element declares content_type as photo, texture, or raster_illustration.
- Never crop the complete slide screenshot and label it as raster_illustration.
- A full-bleed image is acceptable only when the actual source content is genuinely a
  full-bleed photo or texture; all overlaid text and graphics must still be separate.
- When a photo region contains screenshot text, logos, or diagrams, still describe those
  overlays as native objects. The asset extractor will remove matching rasterized pixels
  from the photo crop before the native objects are placed on top.

Coordinates:
- source_width/source_height exactly equal supplied pixel dimensions.
- top-level bounds and line coordinates use source-image pixels.
- Keep top-level bounds and line endpoints inside 0..source_width and 0..source_height.
  Recreate visibly clipped edge decorations inside canvas instead of placing objects beyond it.
- path command coordinates are normalized inside the path bounds: 0 is left/top,
  1 is right/bottom. Coordinates may slightly exceed 0..1 only when source requires it.
- component primitive bounds and line coordinates are normalized inside component
  instances. Components should contain icons or repeated symbols, not page-level text.

Fidelity:
- Transcribe every visible text string exactly, including punctuation and footnotes.
- Match font family category, size, weight, alignment, line breaks, colors, strokes,
  opacity, dash style, rotation, and z-order.
- Use reusable components for repeated pictograms.
- Use separate native objects for repeated bars and rules so users can edit each value.
- Keep element names concise and descriptive.
- Treat horizontal_rectangles from local pixel analysis as measured evidence: preserve
  their count, bounding boxes, repeated row spacing, and sampled colors unless the hint
  is clearly a false positive.

Curves and arrows:
- Use path elements for freeform arrows, ribbons, logos, and organic contours.
- Use C cubic commands for smooth curves. Avoid faceted polygon approximations.
- A filled curved arrow should normally be one closed path containing outer curve,
  arrowhead, and inner return curve. Close it with Z.
- Use linear_gradient fill when source visibly fades along arrow or ribbon.

Layering:
- Background and large washes first.
- Connectors/guides before nodes and icons.
- Foreground labels/icons last.

Schema mechanics:
- Every nullable field remains present; use null when irrelevant.
- Every fill includes kind, color, opacity, angle_deg, stops.
- For none fill: color null, angle_deg null, stops empty.
- For solid fill: color set, angle_deg null, stops empty.
- For linear_gradient: color null, angle_deg set, at least two stops.
- Invisible stroke uses width_px 0 and opacity 0.
- Path commands include every coordinate field; use null for unused values.
""".strip()


def initial_user_prompt(image_facts: dict[str, object], raster_policy: str) -> str:
    policy_text = {
        "none": "No raster image elements allowed. Approximate all artwork with editable vectors.",
        "photos-only": "Raster image elements allowed only for genuine photographic/textured regions.",
        "allow": "Raster regions allowed when truly needed, but full-slide flattening remains forbidden.",
    }[raster_policy]
    return (
        "Reconstruct this single slide screenshot as a fully editable PowerPoint object graph.\n"
        f"Raster policy: {policy_text}\n"
        "Local pixel analysis:\n"
        f"{json.dumps(image_facts, ensure_ascii=False)}\n"
        "Prioritize exact geometry. Inspect complex arrows and logos at high detail."
    )


def refinement_patch_prompt(
    image_facts: dict[str, object],
    metrics: dict[str, object],
    current_spec: dict[str, object],
    raster_policy: str,
) -> str:
    return (
        "Improve current editable reconstruction. First image is source. Second image is rendered PPTX.\n"
        "Return a minimal patch, not a complete spec. Upsert only objects that must change; "
        "keep all other lists empty. Reuse existing IDs when correcting objects. Use new IDs only "
        "for genuinely missing objects. Set background and reconstruction_notes to null when unchanged.\n"
        f"Raster policy: {raster_policy}.\n"
        f"Pixel facts: {json.dumps(image_facts, ensure_ascii=False)}\n"
        f"Measured differences: {json.dumps(metrics, ensure_ascii=False)}\n"
        "Focus on the five worst regions and the largest structural errors: paths, text boxes, "
        "spacing, colors, and z-order. Do not churn IDs or rewrite already-correct objects.\n"
        f"Current spec: {json.dumps(current_spec, ensure_ascii=False)}"
    )
