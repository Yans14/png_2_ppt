from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .invariants import has_unsupported_rebuild_objects
from .ooxml_edit import PML
from .powerpoint import validate_ooxml


EMU_PER_INCH = 914400


class NativeRebuildError(RuntimeError):
    pass


def _slide_size_inches(source: Path) -> tuple[float, float]:
    with zipfile.ZipFile(source) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
    size = presentation.find(f"{{{PML}}}sldSz")
    if size is None:
        raise NativeRebuildError("presentation has no native slide size")
    return (
        int(size.attrib["cx"]) / EMU_PER_INCH,
        int(size.attrib["cy"]) / EMU_PER_INCH,
    )


def native_rebuild_deck(
    source_path: str | Path,
    output_path: str | Path,
    *,
    report_path: str | Path,
    minimum_font_size_pt: float,
    timeout_seconds: int,
) -> Path:
    """Re-render a simple deck as native PptxGenJS objects.

    The caller must still run invariant gates. Complex objects are rejected before
    invoking the renderer because the converter intentionally does not flatten them.
    """

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    report = Path(report_path).resolve()
    unsupported = has_unsupported_rebuild_objects(source)
    if unsupported:
        raise NativeRebuildError(
            "native rebuild is blocked by protected objects: " + ", ".join(unsupported)
        )
    node = shutil.which("node")
    converter = Path(__file__).resolve().parents[1] / "src" / "convert.js"
    if not node or not converter.is_file():
        raise NativeRebuildError("Node.js native rebuild renderer is unavailable")
    width, height = _slide_size_inches(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            node,
            str(converter),
            "--input",
            str(source),
            "--output",
            str(output),
            "--report",
            str(report),
            "--target-width",
            str(width),
            "--target-height",
            str(height),
            "--allow-slide-split",
            "false",
            "--allow-element-deletion",
            "false",
            "--max-slides-growth-pct",
            "0",
            "--readability-min-font-pt",
            str(minimum_font_size_pt),
            "--strict-review",
            "false",
            "--planner",
            "heuristic",
            "--reflow-policy",
            "disabled",
        ],
        capture_output=True,
        text=True,
        timeout=max(1, int(timeout_seconds)),
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise NativeRebuildError(
            "native rebuild renderer failed: "
            + (completed.stderr or completed.stdout or "no output").strip()
        )
    validation = validate_ooxml(output)
    if not validation["compatible"]:
        raise NativeRebuildError(
            "native rebuild output failed OOXML validation: "
            + "; ".join(validation.get("errors", []))
        )
    if report.is_file():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload["native_editable_rebuild"] = True
        payload["protected_objects_restored"] = []
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


__all__ = ["NativeRebuildError", "native_rebuild_deck"]
