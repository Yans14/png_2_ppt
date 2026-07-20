from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Iterable
from xml.etree import ElementTree as ET

from .job_store import sha256_file
from .ooxml_edit import DML, PKG_REL, PML, REL, slide_part_names
from .service_models import (
    DeckInvariantManifest,
    InvariantReport,
    InvariantViolation,
    SlideInvariantSnapshot,
)


CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"

_NUMBER = re.compile(r"(?<!\w)[+-]?(?:\d{1,3}(?:[ ,.']\d{3})+|\d+)(?:[.,]\d+)?%?(?!\w)")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
_LOGO_WORDS = re.compile(r"\b(logo|brand|wordmark|logotype|marque)\b", re.IGNORECASE)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_text(root: ET.Element) -> str:
    return " ".join(item.text or "" for item in root.findall(f".//{{{DML}}}t")).strip()


def _lexical_tokens(values: Iterable[str]) -> tuple[list[str], list[str]]:
    joined = "\n".join(values)
    numbers = [_normalize_number(item.group(0)) for item in _NUMBER.finditer(joined)]
    words = [item.group(0).casefold() for item in _WORD.finditer(joined)]
    return words, numbers


def _normalize_number(value: str) -> str:
    # Thousands separators and decimal punctuation are business content. Normalize
    # whitespace only so locale-specific 1,5 and 1.5 remain distinct.
    return value.replace("\u00a0", " ").replace(" ", "")


def _rels_name(part_name: str) -> str:
    part = PurePosixPath(part_name)
    return str(part.parent / "_rels" / f"{part.name}.rels")


def _resolve_target(source_part: str, target: str) -> str:
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    ).lstrip("/")


def _relationships(
    archive: zipfile.ZipFile,
    part_name: str,
) -> dict[str, tuple[str, str]]:
    rels_name = _rels_name(part_name)
    if rels_name not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rels_name))
    return {
        item.attrib.get("Id", ""): (
            item.attrib.get("Type", ""),
            _resolve_target(part_name, item.attrib.get("Target", "")),
        )
        for item in root.findall(f"{{{PKG_REL}}}Relationship")
        if item.attrib.get("Id") and item.attrib.get("TargetMode") != "External"
    }


def _table_data(root: ET.Element) -> list[list[str]]:
    tables: list[list[str]] = []
    for table in root.findall(f".//{{{DML}}}tbl"):
        cells: list[str] = []
        for row in table.findall(f"{{{DML}}}tr"):
            for cell in row.findall(f"{{{DML}}}tc"):
                cells.append(_normalized_text(cell))
        tables.append(cells)
    return tables


def _chart_data(
    archive: zipfile.ZipFile,
    relationships: dict[str, tuple[str, str]],
    root: ET.Element,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    chart_ids = [
        item.attrib.get(f"{{{REL}}}id", "")
        for item in root.findall(f".//{{{CHART}}}chart")
    ]
    for relationship_id in chart_ids:
        relation = relationships.get(relationship_id)
        if not relation or relation[1] not in archive.namelist():
            continue
        chart_root = ET.fromstring(archive.read(relation[1]))
        series: list[dict[str, list[str]]] = []
        for item in chart_root.findall(f".//{{{CHART}}}ser"):
            texts = [
                (value.text or "").strip()
                for value in item.findall(f".//{{{CHART}}}strCache//{{{CHART}}}v")
            ]
            numbers = [
                (value.text or "").strip()
                for value in item.findall(f".//{{{CHART}}}numCache//{{{CHART}}}v")
            ]
            formulas = [
                (value.text or "").strip()
                for value in item.findall(f".//{{{CHART}}}f")
            ]
            series.append({"text": texts, "numbers": numbers, "formulas": formulas})
        result.append({"part": relation[1], "series": series})
    return result


def _image_hashes(
    archive: zipfile.ZipFile,
    relationships: dict[str, tuple[str, str]],
    root: ET.Element,
) -> tuple[list[str], list[str], list[dict[str, object]]]:
    images: list[str] = []
    logos: list[str] = []
    logo_states: list[dict[str, object]] = []
    for picture in root.findall(f".//{{{PML}}}pic"):
        metadata = picture.find(f".//{{{PML}}}cNvPr")
        identity = ""
        if metadata is not None:
            identity = " ".join(
                metadata.attrib.get(key, "") for key in ("name", "descr", "title")
            )
        blip = picture.find(f".//{{{DML}}}blip")
        relationship_id = blip.attrib.get(f"{{{REL}}}embed", "") if blip is not None else ""
        relation = relationships.get(relationship_id)
        if not relation or relation[1] not in archive.namelist():
            continue
        digest = _sha256_bytes(archive.read(relation[1]))
        images.append(digest)
        if _LOGO_WORDS.search(identity):
            logos.append(digest)
            source_rect = picture.find(f"./{{{PML}}}blipFill/{{{DML}}}srcRect")
            blip = picture.find(f"./{{{PML}}}blipFill/{{{DML}}}blip")
            effects = []
            if blip is not None:
                effects = [
                    ET.tostring(child, encoding="unicode")
                    for child in list(blip)
                    if child.tag != f"{{{DML}}}extLst"
                ]
            transform = picture.find(f"./{{{PML}}}spPr/{{{DML}}}xfrm")
            extent = (
                transform.find(f"{{{DML}}}ext") if transform is not None else None
            )
            width = int(extent.attrib.get("cx", "0")) if extent is not None else 0
            height = int(extent.attrib.get("cy", "0")) if extent is not None else 0
            logo_states.append(
                {
                    "asset_sha256": digest,
                    "crop": dict(sorted((source_rect.attrib if source_rect is not None else {}).items())),
                    "effects_sha256": _sha256_bytes("".join(effects).encode("utf-8")),
                    "aspect_ratio": round(width / height, 8) if height else None,
                    "rotation": transform.attrib.get("rot") if transform is not None else None,
                    "flip_h": transform.attrib.get("flipH") if transform is not None else None,
                    "flip_v": transform.attrib.get("flipV") if transform is not None else None,
                }
            )
    logo_states.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return images, logos, logo_states


def _native_counts(root: ET.Element) -> dict[str, int]:
    return {
        "shape": len(root.findall(f".//{{{PML}}}sp")),
        "picture": len(root.findall(f".//{{{PML}}}pic")),
        "group": len(root.findall(f".//{{{PML}}}grpSp")),
        "graphic_frame": len(root.findall(f".//{{{PML}}}graphicFrame")),
        "table": len(root.findall(f".//{{{DML}}}tbl")),
        "chart": len(root.findall(f".//{{{CHART}}}chart")),
    }


def _animation_hash(root: ET.Element) -> str:
    payload = []
    for tag in (f"{{{PML}}}timing", f"{{{PML}}}transition"):
        for node in root.findall(f".//{tag}"):
            payload.append(ET.tostring(node, encoding="utf-8"))
    return _sha256_bytes(b"".join(payload))


def _protected_relationship_hash(
    archive: zipfile.ZipFile,
    relationships: dict[str, tuple[str, str]],
) -> str:
    protected = []
    protected_markers = ("/oleObject", "/audio", "/video", "/diagram", "/package")
    for relationship_id, (relation_type, target) in sorted(relationships.items()):
        if not any(marker in relation_type for marker in protected_markers):
            continue
        digest = _sha256_bytes(archive.read(target)) if target in archive.namelist() else "missing"
        protected.append((relationship_id, relation_type, target, digest))
    return _sha256_bytes(json.dumps(protected, sort_keys=True).encode("utf-8"))


def create_invariant_manifest(pptx_path: str | Path) -> DeckInvariantManifest:
    source = Path(pptx_path).resolve()
    slides: list[SlideInvariantSnapshot] = []
    package_assets: dict[str, str] = {}
    protected_parts: dict[str, str] = {}
    with zipfile.ZipFile(source) as archive:
        members = set(archive.namelist())
        for member in sorted(members):
            if member.startswith("ppt/media/") and not member.endswith("/"):
                package_assets[member] = _sha256_bytes(archive.read(member))
            if (
                (member.startswith("ppt/embeddings/") and not member.endswith("/"))
                or (member.startswith("ppt/diagrams/") and not member.endswith("/"))
                or member == "ppt/vbaProject.bin"
            ):
                protected_parts[member] = _sha256_bytes(archive.read(member))
        for slide_index, part_name in enumerate(slide_part_names(archive), start=1):
            root = ET.fromstring(archive.read(part_name))
            texts = [
                (node.text or "").strip()
                for node in root.findall(f".//{{{DML}}}t")
                if (node.text or "").strip()
            ]
            words, numbers = _lexical_tokens(texts)
            relationships = _relationships(archive, part_name)
            images, logos, logo_states = _image_hashes(archive, relationships, root)
            slides.append(
                SlideInvariantSnapshot(
                    slide_index=slide_index,
                    lexical_tokens=words,
                    numeric_tokens=numbers,
                    table_data=_table_data(root),
                    chart_data=_chart_data(archive, relationships, root),
                    image_hashes=images,
                    logo_hashes=logos,
                    logo_states=logo_states,
                    native_object_counts=_native_counts(root),
                    relationship_hash=_protected_relationship_hash(archive, relationships),
                    animation_hash=_animation_hash(root),
                )
            )
    return DeckInvariantManifest(
        source_sha256=sha256_file(source),
        slide_count=len(slides),
        slides=slides,
        package_assets=package_assets,
        protected_parts=protected_parts,
    )


def verify_invariants(
    source_manifest: DeckInvariantManifest,
    candidate_path: str | Path,
) -> InvariantReport:
    candidate = Path(candidate_path).resolve()
    candidate_manifest = create_invariant_manifest(candidate)
    violations: list[InvariantViolation] = []

    def violation(code: str, message: str, slide_index: int | None = None) -> None:
        violations.append(
            InvariantViolation(code=code, message=message, slide_index=slide_index)
        )

    if candidate_manifest.slide_count != source_manifest.slide_count:
        violation(
            "slide_count_changed",
            f"expected {source_manifest.slide_count}, found {candidate_manifest.slide_count}",
        )
    for before, after in zip(source_manifest.slides, candidate_manifest.slides):
        slide_index = before.slide_index
        if Counter(before.lexical_tokens) != Counter(after.lexical_tokens):
            violation("words_changed", "lexical word multiset changed", slide_index)
        if Counter(before.numeric_tokens) != Counter(after.numeric_tokens):
            violation("numbers_changed", "numeric token multiset changed", slide_index)
        if before.table_data != after.table_data:
            violation("table_data_changed", "native table cell data changed", slide_index)
        if before.chart_data != after.chart_data:
            violation("chart_data_changed", "native chart series data changed", slide_index)
        if Counter(before.image_hashes) != Counter(after.image_hashes):
            violation("images_changed", "embedded image assets changed", slide_index)
        if not set(before.logo_hashes).issubset(set(after.image_hashes)):
            violation("logos_changed", "logo assets changed", slide_index)
        if before.logo_states != after.logo_states:
            violation(
                "logo_geometry_or_effect_changed",
                "logo crop, effects, rotation, flip, or proportional geometry changed",
                slide_index,
            )
        for object_type in ("table", "chart"):
            if before.native_object_counts.get(object_type) != after.native_object_counts.get(object_type):
                violation(
                    f"native_{object_type}_count_changed",
                    f"native {object_type} count changed",
                    slide_index,
                )
        if before.relationship_hash != after.relationship_hash:
            violation("protected_relationship_changed", "OLE/media/diagram relationship changed", slide_index)
        if before.animation_hash != after.animation_hash:
            violation("animation_changed", "animation or transition XML changed", slide_index)
    missing_assets = set(source_manifest.package_assets.values()) - set(
        candidate_manifest.package_assets.values()
    )
    if missing_assets:
        violation(
            "package_assets_removed",
            "one or more original package media assets were removed or replaced",
        )
    if source_manifest.protected_parts != candidate_manifest.protected_parts:
        violation("protected_parts_changed", "embedded or diagram part changed")
    checks = {
        "slide_count": not any(item.code == "slide_count_changed" for item in violations),
        "words": not any(item.code == "words_changed" for item in violations),
        "numbers": not any(item.code == "numbers_changed" for item in violations),
        "tables": not any("table" in item.code for item in violations),
        "charts": not any("chart" in item.code for item in violations),
        "assets": not any("asset" in item.code or "image" in item.code or "logo" in item.code for item in violations),
        "relationships": not any("relationship" in item.code for item in violations),
        "animations": not any("animation" in item.code for item in violations),
    }
    return InvariantReport(
        passed=not violations,
        source_sha256=source_manifest.source_sha256,
        candidate_sha256=candidate_manifest.source_sha256,
        violations=violations,
        checks=checks,
    )


def has_unsupported_rebuild_objects(pptx_path: str | Path) -> list[str]:
    source = Path(pptx_path).resolve()
    reasons: set[str] = set()
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        if any(
            name.startswith("ppt/embeddings/") and not name.endswith("/")
            for name in names
        ):
            reasons.add("embedded_or_ole_object")
        if any(
            name.startswith("ppt/diagrams/") and not name.endswith("/")
            for name in names
        ):
            reasons.add("smartart_or_diagram")
        for part_name in slide_part_names(archive):
            root = ET.fromstring(archive.read(part_name))
            if root.find(f".//{{{PML}}}timing") is not None:
                reasons.add("animation_timeline")
    return sorted(reasons)


def font_size_report(
    pptx_path: str | Path,
    *,
    body_minimum_pt: float = 8.0,
    source_minimum_pt: float = 6.0,
) -> dict[str, object]:
    source = Path(pptx_path).resolve()
    violations: list[dict[str, object]] = []
    explicit_runs = 0
    with zipfile.ZipFile(source) as archive:
        for slide_index, part_name in enumerate(slide_part_names(archive), start=1):
            root = ET.fromstring(archive.read(part_name))
            for shape in root.findall(f".//{{{PML}}}sp"):
                metadata = shape.find(f".//{{{PML}}}cNvPr")
                name = metadata.attrib.get("name", "") if metadata is not None else ""
                text = _normalized_text(shape)
                source_like = bool(
                    re.search(r"\b(source|sources|note|footnote|methodology)\b", name, re.I)
                    or re.match(r"\s*(source|sources|note)\s*:", text, re.I)
                )
                minimum = source_minimum_pt if source_like else body_minimum_pt
                sizes = []
                for properties in (
                    shape.findall(f".//{{{DML}}}rPr")
                    + shape.findall(f".//{{{DML}}}defRPr")
                    + shape.findall(f".//{{{DML}}}endParaRPr")
                ):
                    raw_size = properties.attrib.get("sz")
                    if raw_size and raw_size.isdigit():
                        sizes.append(int(raw_size) / 100)
                explicit_runs += len(sizes)
                for size in sizes:
                    if size + 1e-6 < minimum:
                        violations.append(
                            {
                                "slide_index": slide_index,
                                "shape_id": metadata.attrib.get("id") if metadata is not None else None,
                                "shape_name": name,
                                "size_pt": size,
                                "minimum_pt": minimum,
                                "source_like": source_like,
                            }
                        )
    return {
        "passed": not violations,
        "body_minimum_pt": body_minimum_pt,
        "source_minimum_pt": source_minimum_pt,
        "explicit_runs_checked": explicit_runs,
        "violations": violations,
    }


__all__ = [
    "create_invariant_manifest",
    "font_size_report",
    "has_unsupported_rebuild_objects",
    "verify_invariants",
]
