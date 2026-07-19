#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from editable_pptx.models import SlideSpec
from editable_pptx.renderer import render_pptx


REPO = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO / "benchmarks" / "api_examples" / "manifest.json"
EXAMPLE_ROOT = MANIFEST_PATH.parent
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _fill(color: str) -> dict[str, object]:
    return {
        "kind": "solid",
        "color": color,
        "opacity": 1,
        "angle_deg": None,
        "stops": [],
    }


def _shape(
    element_id: str,
    name: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    color: str,
    layer: int,
) -> dict[str, object]:
    return {
        "kind": "shape",
        "id": element_id,
        "name": name,
        "layer": layer,
        "group_id": None,
        "bounds": {"x": x, "y": y, "width": width, "height": height},
        "rotation_deg": 0,
        "preset": "rect",
        "fill": _fill(color),
        "stroke": {"color": color, "opacity": 0, "width_px": 0, "dash": "solid"},
        "corner_radius": None,
    }


def _text(
    element_id: str,
    name: str,
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float,
    color: str = "#17212B",
    bold: bool = False,
    layer: int = 2,
    align: str = "left",
) -> dict[str, object]:
    return {
        "kind": "text",
        "id": element_id,
        "name": name,
        "layer": layer,
        "group_id": None,
        "bounds": {"x": x, "y": y, "width": width, "height": height},
        "rotation_deg": 0,
        "text": text,
        "font_family": "Arial",
        "font_size_pt": size,
        "bold": bold,
        "italic": False,
        "color": color,
        "opacity": 1,
        "alignment": align,
        "vertical_alignment": "middle",
        "line_spacing": 1,
        "margin_px": 0,
    }


def _note_spec(case_id: str) -> SlideSpec:
    elements: list[dict[str, object]] = [
        _text(
            "title",
            "Slide title",
            "Controlled production-note fixture",
            x=48,
            y=34,
            width=620,
            height=54,
            size=26,
            bold=True,
        ),
        _text(
            "body",
            "Body copy",
            "This business content must remain editable and unchanged unless the note explicitly targets it.",
            x=48,
            y=92,
            width=700,
            height=48,
            size=14,
            color="#4E5B66",
        ),
    ]
    instructions = {
        "move-card": "Please move the blue target card and its white label down by exactly 28 points.",
        "replace-value": "Please replace 'Revenue 2025: US$400m' with 'Revenue 2026: US$500m'.",
        "recolor-accent": "Please recolor the target accent rectangle to #0A8F5A and change nothing else.",
        "resize-panel": "Please resize the target panel to exactly 360 points wide while preserving its height and position.",
        "duplicate-row": "Please duplicate the ROW A rectangle and its label once, place the copy 42 points below, and change the copied label to ROW B.",
    }
    if case_id == "move-card":
        elements.extend(
            [
                _shape("target_card", "Target card", x=70, y=180, width=300, height=80, color="#176B87", layer=2),
                _text("target_card_label", "Target card label", "MOVE THIS CARD", x=90, y=197, width=260, height=42, size=18, color="#FFFFFF", bold=True, layer=3, align="center"),
            ]
        )
    elif case_id == "replace-value":
        elements.append(
            _text("target_value", "Target value", "Revenue 2025: US$400m", x=70, y=180, width=420, height=65, size=24, bold=True)
        )
    elif case_id == "recolor-accent":
        elements.extend(
            [
                _shape("target_accent", "Target accent", x=70, y=180, width=340, height=42, color="#D95B2B", layer=2),
                _text("accent_label", "Accent label", "TARGET ACCENT", x=80, y=185, width=320, height=30, size=15, color="#FFFFFF", bold=True, layer=3, align="center"),
            ]
        )
    elif case_id == "resize-panel":
        elements.extend(
            [
                _shape("target_panel", "Target panel", x=70, y=180, width=300, height=105, color="#D8E9F0", layer=2),
                _text("panel_label", "Panel label", "RESIZE THIS PANEL", x=90, y=210, width=260, height=38, size=17, bold=True, layer=3, align="center"),
            ]
        )
    elif case_id == "duplicate-row":
        elements.extend(
            [
                _shape("target_row", "Target row", x=70, y=170, width=360, height=44, color="#E9EEF2", layer=2),
                _text("target_row_label", "Target row label", "ROW A", x=90, y=177, width=320, height=28, size=16, bold=True, layer=3),
            ]
        )
    else:
        raise ValueError(f"unknown note fixture: {case_id}")

    elements.extend(
        [
            _shape("production_note_box", "Production note box", x=485, y=310, width=420, height=135, color="#C70000", layer=10),
            _text("production_note", "Production instruction", instructions[case_id], x=510, y=330, width=370, height=95, size=16, color="#FFFFFF", bold=True, layer=11, align="center"),
            _text("footer", "Footer", "Synthetic QA fixture · editable objects only", x=48, y=495, width=600, height=24, size=9, color="#64717B", layer=20),
        ]
    )
    return SlideSpec.model_validate(
        {
            "version": "1.0",
            "source_width": 960,
            "source_height": 540,
            "background": _fill("#FFFFFF"),
            "components": [],
            "elements": elements,
            "reconstruction_notes": [],
        }
    )


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "editable-pptx-benchmark/2.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    if b"<svg" not in payload[:2000].lower():
        raise RuntimeError(f"download was not SVG: {url}")
    target.write_bytes(payload)


def _broken_relationship(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as input_zip, zipfile.ZipFile(target, "w") as output_zip:
        changed = False
        for info in input_zip.infolist():
            data = input_zip.read(info.filename)
            if info.filename == "ppt/slides/_rels/slide1.xml.rels":
                root = ET.fromstring(data)
                for relationship in root.findall(f"{{{PKG_REL}}}Relationship"):
                    if relationship.attrib.get("Type", "").endswith("/slideLayout"):
                        relationship.attrib["Target"] = "../slideLayouts/missing.xml"
                        changed = True
                        break
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output_zip.writestr(info, data)
    if not changed:
        target.unlink(missing_ok=True)
        raise RuntimeError("could not create broken-relationship fixture")


def prepare(*, offline: bool = False) -> dict[str, list[str]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    prepared: dict[str, list[str]] = {"figures": [], "notes": [], "validate": []}
    for case in manifest["figure_to_editable"]:
        if not case.get("url"):
            continue
        target = EXAMPLE_ROOT / case["input"]
        if not target.exists():
            if offline:
                raise RuntimeError(f"missing cached public figure: {target}")
            _download(case["url"], target)
        prepared["figures"].append(str(target))
    for case in manifest["notes"]:
        target = EXAMPLE_ROOT / case["input"]
        target.parent.mkdir(parents=True, exist_ok=True)
        render_pptx(_note_spec(case["id"]), target)
        prepared["notes"].append(str(target))
    invalid = next(item for item in manifest["validate"] if item["expected"] == "invalid")
    invalid_target = EXAMPLE_ROOT / invalid["input"]
    valid_source = REPO / manifest["validate"][0]["input"]
    _broken_relationship(valid_source, invalid_target)
    prepared["validate"].append(str(invalid_target))
    return prepared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(prepare(offline=args.offline), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
