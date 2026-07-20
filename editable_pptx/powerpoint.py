from __future__ import annotations

import json
import platform
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from .qa import audit_pptx, compare_images, render_first_slide


class PowerPointCompatibilityError(RuntimeError):
    pass


PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

PORTABLE_OFFICE_FONTS = {
    "aptos",
    "aptos display",
    "arial",
    "arial narrow",
    "calibri",
    "cambria",
    "courier new",
    "georgia",
    "tahoma",
    "times new roman",
    "trebuchet ms",
    "verdana",
}


def validate_ooxml(
    pptx_path: str | Path,
    *,
    font_policy: str = "portable",
) -> dict[str, object]:
    """Validate the package features most likely to trigger PowerPoint repair."""

    source = Path(pptx_path).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    fonts: set[str] = set()
    external_relationships: list[str] = []
    checked_xml_parts = 0
    duplicate_shape_ids: list[dict[str, object]] = []
    duplicate_shape_names: list[dict[str, object]] = []
    invalid_extents: list[dict[str, object]] = []
    invalid_autofit: list[dict[str, object]] = []
    invalid_gradients: list[dict[str, object]] = []

    if not source.exists() or not zipfile.is_zipfile(source):
        return {
            "compatible": False,
            "errors": ["File is not a readable Open XML package"],
            "warnings": [],
        }

    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            corrupt_member = archive.testzip()
            if corrupt_member:
                errors.append(f"CRC failure in package member: {corrupt_member}")
            for required in (
                "[Content_Types].xml",
                "_rels/.rels",
                "ppt/presentation.xml",
                "ppt/_rels/presentation.xml.rels",
            ):
                if required not in names:
                    errors.append(f"Missing required package part: {required}")

            if "[Content_Types].xml" in names:
                try:
                    content_types = ET.fromstring(archive.read("[Content_Types].xml"))
                    presentation_override = any(
                        item.attrib.get("PartName") == "/ppt/presentation.xml"
                        for item in content_types.findall(
                            f"{{{CONTENT_TYPES_NS}}}Override"
                        )
                    )
                    if not presentation_override:
                        errors.append("Missing presentation content-type override")
                except ET.ParseError as error:
                    errors.append(f"Invalid [Content_Types].xml: {error}")

            for relationship_name in sorted(
                name for name in names if name.endswith(".rels")
            ):
                try:
                    root = ET.fromstring(archive.read(relationship_name))
                except ET.ParseError as error:
                    errors.append(f"Invalid relationship XML {relationship_name}: {error}")
                    continue
                base_part = _relationship_source_part(relationship_name)
                for relationship in root.findall(f"{{{REL_NS}}}Relationship"):
                    target = relationship.attrib.get("Target", "")
                    if relationship.attrib.get("TargetMode") == "External":
                        external_relationships.append(target)
                        continue
                    resolved = _resolve_relationship_target(base_part, target)
                    if resolved and resolved not in names:
                        errors.append(
                            f"Broken relationship in {relationship_name}: {target}"
                        )

            for member in sorted(name for name in names if name.endswith(".xml")):
                try:
                    root = ET.fromstring(archive.read(member))
                except ET.ParseError as error:
                    errors.append(f"Invalid XML {member}: {error}")
                    continue
                checked_xml_parts += 1
                for font in root.findall(f".//{{{DML_NS}}}latin"):
                    typeface = font.attrib.get("typeface", "").strip()
                    if typeface and not typeface.startswith("+"):
                        fonts.add(typeface)
                if not re.fullmatch(r"ppt/slides/slide\d+\.xml", member):
                    continue
                _inspect_slide_xml(
                    member,
                    root,
                    duplicate_shape_ids=duplicate_shape_ids,
                    duplicate_shape_names=duplicate_shape_names,
                    invalid_extents=invalid_extents,
                    invalid_autofit=invalid_autofit,
                    invalid_gradients=invalid_gradients,
                )
    except (OSError, zipfile.BadZipFile) as error:
        errors.append(str(error))

    if duplicate_shape_ids:
        errors.append("Duplicate non-visual shape IDs detected")
    if duplicate_shape_names:
        warnings.append("Duplicate shape names detected; stable-ID patching may be ambiguous")
    if invalid_extents:
        errors.append("Negative object extents detected")
    if invalid_autofit:
        errors.append("Invalid PowerPoint text auto-fit values detected")
    if invalid_gradients:
        errors.append("Invalid gradient stop or alpha values detected")
    nonportable_fonts = sorted(
        font for font in fonts if font.lower() not in PORTABLE_OFFICE_FONTS
    )
    if font_policy == "portable" and nonportable_fonts:
        warnings.append(
            "Non-portable fonts remain in the package: " + ", ".join(nonportable_fonts)
        )

    return {
        "compatible": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_xml_parts": checked_xml_parts,
        "fonts": sorted(fonts),
        "nonportable_fonts": nonportable_fonts,
        "external_relationships": external_relationships,
        "duplicate_shape_ids": duplicate_shape_ids,
        "duplicate_shape_names": duplicate_shape_names,
        "invalid_extents": invalid_extents,
        "invalid_autofit": invalid_autofit,
        "invalid_gradients": invalid_gradients,
    }


def _relationship_source_part(relationship_name: str) -> str:
    if relationship_name == "_rels/.rels":
        return ""
    path = PurePosixPath(relationship_name)
    if path.parent.name != "_rels" or not path.name.endswith(".rels"):
        return ""
    return str(path.parent.parent / path.name.removesuffix(".rels"))


def _resolve_relationship_target(source_part: str, target: str) -> str:
    if not target or target.startswith("/"):
        return target.lstrip("/")
    base = posixpath.dirname(source_part)
    return posixpath.normpath(posixpath.join(base, target)).lstrip("/")


def _inspect_slide_xml(
    member: str,
    root: ET.Element,
    *,
    duplicate_shape_ids: list[dict[str, object]],
    duplicate_shape_names: list[dict[str, object]],
    invalid_extents: list[dict[str, object]],
    invalid_autofit: list[dict[str, object]],
    invalid_gradients: list[dict[str, object]],
) -> None:
    identifiers: dict[str, int] = {}
    names: dict[str, int] = {}
    for metadata in root.findall(f".//{{{PML_NS}}}cNvPr"):
        identifier = metadata.attrib.get("id", "")
        name = metadata.attrib.get("name", "")
        identifiers[identifier] = identifiers.get(identifier, 0) + 1
        if name:
            names[name] = names.get(name, 0) + 1
    duplicate_shape_ids.extend(
        {"part": member, "id": identifier, "count": count}
        for identifier, count in identifiers.items()
        if identifier and count > 1
    )
    duplicate_shape_names.extend(
        {"part": member, "name": name, "count": count}
        for name, count in names.items()
        if count > 1
    )
    for extent in root.findall(f".//{{{DML_NS}}}ext"):
        # a:ext is also used by extension lists, where it carries a URI rather
        # than geometry. Only transform extents have cx/cy coordinates.
        if "cx" not in extent.attrib and "cy" not in extent.attrib:
            continue
        cx = _integer_or_none(extent.attrib.get("cx"))
        cy = _integer_or_none(extent.attrib.get("cy"))
        if cx is None or cy is None or cx < 0 or cy < 0:
            invalid_extents.append(
                {"part": member, "cx": extent.attrib.get("cx"), "cy": extent.attrib.get("cy")}
            )
    for autofit in root.findall(f".//{{{DML_NS}}}normAutofit"):
        for attribute in ("fontScale", "lnSpcReduction"):
            if attribute not in autofit.attrib:
                continue
            value = _integer_or_none(autofit.attrib.get(attribute))
            if value is None or not 0 <= value <= 100000:
                invalid_autofit.append(
                    {"part": member, "attribute": attribute, "value": autofit.attrib.get(attribute)}
                )
    for gradient in root.findall(f".//{{{DML_NS}}}gradFill"):
        positions: list[int] = []
        for stop in gradient.findall(f".//{{{DML_NS}}}gs"):
            position = _integer_or_none(stop.attrib.get("pos"))
            if position is None or not 0 <= position <= 100000:
                invalid_gradients.append(
                    {"part": member, "kind": "position", "value": stop.attrib.get("pos")}
                )
            else:
                positions.append(position)
            for alpha in stop.findall(f".//{{{DML_NS}}}alpha"):
                value = _integer_or_none(alpha.attrib.get("val"))
                if value is None or not 0 <= value <= 100000:
                    invalid_gradients.append(
                        {"part": member, "kind": "alpha", "value": alpha.attrib.get("val")}
                    )
        if positions != sorted(positions):
            invalid_gradients.append(
                {"part": member, "kind": "stop-order", "value": positions}
            )


def _integer_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def available_powerpoint_adapter() -> str | None:
    system = platform.system().lower()
    if system == "windows" and (shutil.which("powershell") or shutil.which("pwsh")):
        return "windows-com"
    if system == "darwin":
        app = Path("/Applications/Microsoft PowerPoint.app")
        if app.exists() and shutil.which("osascript"):
            return "macos-applescript"
    return None


def validate_powerpoint(
    pptx_path: str | Path,
    *,
    mode: str = "auto",
    font_policy: str = "portable",
    reference_image: str | Path | None = None,
    workspace: str | Path | None = None,
    timeout_seconds: int = 180,
) -> dict[str, object]:
    if mode not in {"off", "auto", "required"}:
        raise ValueError("PowerPoint validation mode must be off, auto, or required")
    source = Path(pptx_path).resolve()
    ooxml = validate_ooxml(source, font_policy=font_policy)
    if mode == "off":
        return {
            "status": "not_run",
            "mode": mode,
            "adapter": None,
            "ooxml": ooxml,
            "compatible": bool(ooxml["compatible"]),
        }
    adapter = available_powerpoint_adapter()
    if not adapter:
        if mode == "required":
            raise PowerPointCompatibilityError(
                "Microsoft PowerPoint is required but no supported local installation was found"
            )
        return {
            "status": "unavailable",
            "mode": mode,
            "adapter": None,
            "ooxml": ooxml,
            "compatible": bool(ooxml["compatible"]),
            "message": "Real PowerPoint round-trip was skipped because PowerPoint is unavailable",
        }

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="editable-pptx-powerpoint-")
        root = Path(temporary.name)
    else:
        root = Path(workspace).resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        roundtrip = root / "roundtrip.pptx"
        original_exported = root / "powerpoint-original-slide-1.png"
        exported = root / "powerpoint-roundtrip-slide-1.png"
        execution = (
            _roundtrip_windows(
                source,
                roundtrip,
                original_exported,
                exported,
                timeout_seconds,
            )
            if adapter == "windows-com"
            else _roundtrip_macos(
                source,
                roundtrip,
                original_exported,
                exported,
                timeout_seconds,
            )
        )
        if not execution["success"] or not roundtrip.exists():
            result = {
                "status": "failed",
                "mode": mode,
                "adapter": adapter,
                "ooxml": ooxml,
                "compatible": False,
                "repair_detected": bool(execution.get("timed_out")),
                "execution": execution,
            }
            if mode == "required":
                raise PowerPointCompatibilityError(
                    "PowerPoint could not open/save/export the presentation: "
                    + str(execution.get("message", "unknown failure"))
                )
            return result

        before_audit = audit_pptx(source)
        after_audit = audit_pptx(roundtrip)
        roundtrip_ooxml = validate_ooxml(roundtrip, font_policy=font_policy)
        counts = {
            "native_shape_objects": _count_delta(before_audit, after_audit, "native_shape_objects"),
            "native_text_runs": _count_delta(before_audit, after_audit, "native_text_runs"),
            "picture_objects": _count_delta(before_audit, after_audit, "picture_objects"),
            "custom_geometry_paths": _count_delta(before_audit, after_audit, "custom_geometry_paths"),
        }
        object_count_stable = (
            counts["native_shape_objects"]["delta"] == 0
            and counts["picture_objects"]["delta"] == 0
            and counts["custom_geometry_paths"]["delta"] == 0
        )
        text_preserved = (
            int(after_audit.get("native_text_runs", 0)) > 0
            if int(before_audit.get("native_text_runs", 0)) > 0
            else True
        )
        drift: dict[str, object] | None = None
        if original_exported.exists() and exported.exists():
            drift = compare_images(original_exported, exported)
        else:
            with tempfile.TemporaryDirectory(prefix="editable-pptx-roundtrip-drift-") as drift_dir:
                drift_root = Path(drift_dir)
                original_render = render_first_slide(
                    source,
                    drift_root / "original.png",
                    timeout_seconds=timeout_seconds,
                )
                roundtrip_render = render_first_slide(
                    roundtrip,
                    drift_root / "roundtrip.png",
                    timeout_seconds=timeout_seconds,
                )
                drift = compare_images(original_render, roundtrip_render)
        powerpoint_metrics = None
        if reference_image is not None and exported.exists():
            powerpoint_metrics = compare_images(reference_image, exported)
        compatible = (
            bool(ooxml["compatible"])
            and bool(roundtrip_ooxml["compatible"])
            and object_count_stable
            and text_preserved
            and int(after_audit.get("canvas_overflow_count", 0)) == 0
            and float(drift.get("structural_score", 0.0)) >= 0.98
        )
        return {
            "status": "passed" if compatible else "failed",
            "mode": mode,
            "adapter": adapter,
            "compatible": compatible,
            "repair_detected": False,
            "execution": execution,
            "ooxml": ooxml,
            "roundtrip_ooxml": roundtrip_ooxml,
            "before_audit": before_audit,
            "after_audit": after_audit,
            "count_deltas": counts,
            "object_count_stable": object_count_stable,
            "text_preserved": text_preserved,
            "roundtrip_drift": drift,
            "powerpoint_render_metrics": powerpoint_metrics,
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def _count_delta(
    before: dict[str, object],
    after: dict[str, object],
    key: str,
) -> dict[str, int]:
    first = int(before.get(key, 0))
    second = int(after.get(key, 0))
    return {"before": first, "after": second, "delta": second - first}


def _roundtrip_windows(
    source: Path,
    roundtrip: Path,
    original_exported: Path,
    roundtrip_exported: Path,
    timeout_seconds: int,
) -> dict[str, object]:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return {"success": False, "message": "PowerShell is unavailable"}
    script = r"""
param(
  [string]$InputPath,
  [string]$RoundtripPath,
  [string]$OriginalExportPath,
  [string]$RoundtripExportPath
)
$ErrorActionPreference = "Stop"
$powerpoint = $null
$presentation = $null
try {
  $powerpoint = New-Object -ComObject PowerPoint.Application
  $powerpoint.DisplayAlerts = 1
  $presentation = $powerpoint.Presentations.Open($InputPath, -1, 0, 0)
  $presentation.Slides.Item(1).Export($OriginalExportPath, "PNG", 1920, 1080)
  $presentation.SaveCopyAs($RoundtripPath, 24)
  $presentation.Close()
  $presentation = $powerpoint.Presentations.Open($RoundtripPath, -1, 0, 0)
  $presentation.Slides.Item(1).Export($RoundtripExportPath, "PNG", 1920, 1080)
} finally {
  if ($presentation -ne $null) { $presentation.Close() }
  if ($powerpoint -ne $null) { $powerpoint.Quit() }
}
""".strip()
    script_path = roundtrip.parent / "powerpoint-roundtrip.ps1"
    script_path.write_text(script + "\n", encoding="utf-8")
    return _run_adapter(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            str(source),
            str(roundtrip),
            str(original_exported),
            str(roundtrip_exported),
        ],
        timeout_seconds,
    )


def _roundtrip_macos(
    source: Path,
    roundtrip: Path,
    original_exported: Path,
    roundtrip_exported: Path,
    timeout_seconds: int,
) -> dict[str, object]:
    osascript = shutil.which("osascript")
    if not osascript:
        return {"success": False, "message": "osascript is unavailable"}
    original_pdf = roundtrip.parent / "powerpoint-original.pdf"
    roundtrip_pdf = roundtrip.parent / "powerpoint-roundtrip.pdf"
    script = r"""
on run argv
  set inputPath to item 1 of argv
  set roundtripPath to item 2 of argv
  set originalPdfPath to item 3 of argv
  set roundtripPdfPath to item 4 of argv
  tell application "Microsoft PowerPoint"
    launch
    open POSIX file inputPath
    set deck to active presentation
    save deck in POSIX file originalPdfPath as save as PDF
    save deck in POSIX file roundtripPath as save as Open XML presentation
    close deck saving no
    open POSIX file roundtripPath
    set roundtripDeck to active presentation
    save roundtripDeck in POSIX file roundtripPdfPath as save as PDF
    close roundtripDeck saving no
  end tell
end run
""".strip()
    script_path = roundtrip.parent / "powerpoint-roundtrip.applescript"
    script_path.write_text(script + "\n", encoding="utf-8")
    execution = _run_adapter(
        [
            osascript,
            str(script_path),
            str(source),
            str(roundtrip),
            str(original_pdf),
            str(roundtrip_pdf),
        ],
        timeout_seconds,
    )
    if execution["success"] and original_pdf.exists() and roundtrip_pdf.exists():
        pdftoppm = shutil.which("pdftoppm")
        if pdftoppm:
            for pdf_path, png_path in (
                (original_pdf, original_exported),
                (roundtrip_pdf, roundtrip_exported),
            ):
                raster = _run_adapter(
                    [
                        pdftoppm,
                        "-f",
                        "1",
                        "-singlefile",
                        "-png",
                        "-r",
                        "144",
                        str(pdf_path),
                        str(png_path.with_suffix("")),
                    ],
                    timeout_seconds,
                )
                if not raster["success"]:
                    execution["export_warning"] = raster.get("message")
    return execution


def _run_adapter(command: list[str], timeout_seconds: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "timed_out": True,
            "message": f"PowerPoint automation timed out after {timeout_seconds}s",
        }
    message = (completed.stderr or completed.stdout).strip()
    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "timed_out": False,
        "message": message,
    }


def write_validation_report(report: dict[str, object], output: str | Path) -> Path:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
