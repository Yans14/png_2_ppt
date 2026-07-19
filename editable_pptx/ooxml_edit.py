from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Iterable
from xml.etree import ElementTree as ET

from .job_store import sha256_file
from .powerpoint import validate_ooxml
from .service_models import (
    PptxPatchOperation,
    PptxPatchPlan,
    ProductionInstruction,
    ShapeSnapshot,
)


PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
EMU_PER_POINT = 12700

ET.register_namespace("a", DML)
ET.register_namespace("p", PML)
ET.register_namespace("r", REL)


class OoxmlEditError(RuntimeError):
    pass


_INSTRUCTION_WORDS = re.compile(
    r"\b(please|add|remove|replace|move|change|update|delete|insert|duplicate|"
    r"merci|ajoute[rz]?|supprime[rz]?|remplace[rz]?|d[ée]place[rz]?|modifie[rz]?|"
    r"placeholder|dummy|production note|instruction)\b",
    re.IGNORECASE,
)


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split()).strip()


def _relationship_source_part(relationship_name: str) -> str:
    path = PurePosixPath(relationship_name)
    if path.parent.name != "_rels" or not path.name.endswith(".rels"):
        return ""
    return str(path.parent.parent / path.name.removesuffix(".rels"))


def _resolve_target(source_part: str, target: str) -> str:
    import posixpath

    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target)).lstrip("/")


def slide_part_names(archive: zipfile.ZipFile) -> list[str]:
    presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
    rels = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
    targets = {
        item.attrib["Id"]: _resolve_target("ppt/presentation.xml", item.attrib["Target"])
        for item in rels.findall(f"{{{PKG_REL}}}Relationship")
    }
    names = []
    for slide_id in presentation.findall(f".//{{{PML}}}sldId"):
        relationship_id = slide_id.attrib.get(f"{{{REL}}}id")
        if relationship_id in targets:
            names.append(targets[relationship_id])
    return names


def _shape_metadata(node: ET.Element) -> ET.Element | None:
    return node.find(f".//{{{PML}}}cNvPr")


def _shape_text(node: ET.Element) -> str:
    return _normalize_text(" ".join(item.text or "" for item in node.findall(f".//{{{DML}}}t")))


def _shape_transform(node: ET.Element) -> tuple[float | None, float | None, float | None, float | None]:
    offset = node.find(f".//{{{DML}}}xfrm/{{{DML}}}off")
    extent = node.find(f".//{{{DML}}}xfrm/{{{DML}}}ext")
    if offset is None:
        offset = node.find(f".//{{{PML}}}xfrm/{{{DML}}}off")
    if extent is None:
        extent = node.find(f".//{{{PML}}}xfrm/{{{DML}}}ext")
    def point(element: ET.Element | None, key: str) -> float | None:
        if element is None or key not in element.attrib:
            return None
        return round(int(element.attrib[key]) / EMU_PER_POINT, 4)
    return point(offset, "x"), point(offset, "y"), point(extent, "cx"), point(extent, "cy")


def _fill_color(node: ET.Element) -> str | None:
    color = node.find(f".//{{{DML}}}solidFill/{{{DML}}}srgbClr")
    return f"#{color.attrib['val'].upper()}" if color is not None and color.attrib.get("val") else None


def extract_shape_graph(pptx_path: str | Path) -> list[ShapeSnapshot]:
    source = Path(pptx_path).resolve()
    shapes: list[ShapeSnapshot] = []
    with zipfile.ZipFile(source) as archive:
        for slide_index, part in enumerate(slide_part_names(archive), start=1):
            root = ET.fromstring(archive.read(part))
            for node in root.findall(f".//{{{PML}}}sp") + root.findall(f".//{{{PML}}}pic") + root.findall(f".//{{{PML}}}graphicFrame") + root.findall(f".//{{{PML}}}grpSp"):
                metadata = _shape_metadata(node)
                if metadata is None or not metadata.attrib.get("id", "").isdigit():
                    continue
                shape_id = int(metadata.attrib["id"])
                x, y, width, height = _shape_transform(node)
                shapes.append(
                    ShapeSnapshot(
                        stable_id=f"s{slide_index}:{shape_id}",
                        slide_index=slide_index,
                        shape_id=shape_id,
                        name=metadata.attrib.get("name", f"Shape {shape_id}"),
                        kind=node.tag.rsplit("}", 1)[-1],
                        text=_shape_text(node),
                        x_pt=x,
                        y_pt=y,
                        width_pt=width,
                        height_pt=height,
                        fill_color=_fill_color(node),
                    )
                )
    return shapes


def extract_text_manifest(pptx_path: str | Path) -> dict[int, list[str]]:
    manifest: dict[int, list[str]] = {}
    for shape in extract_shape_graph(pptx_path):
        if shape.text:
            manifest.setdefault(shape.slide_index, []).append(shape.text)
    return manifest


def extract_production_instructions(
    pptx_path: str | Path,
    *,
    api_instruction: str | None = None,
    selected_slides: Iterable[int] | None = None,
) -> list[ProductionInstruction]:
    source = Path(pptx_path).resolve()
    selected = set(selected_slides or [])
    instructions: list[ProductionInstruction] = []
    with zipfile.ZipFile(source) as archive:
        slides = slide_part_names(archive)
        target_slides = selected or set(range(1, len(slides) + 1))
        if api_instruction and api_instruction.strip():
            for slide_index in sorted(target_slides):
                instructions.append(
                    ProductionInstruction(
                        id=f"api-{slide_index}",
                        slide_index=slide_index,
                        source="api",
                        raw_text=api_instruction.strip(),
                        priority=100,
                    )
                )

        for slide_index, part in enumerate(slides, start=1):
            if slide_index not in target_slides:
                continue
            root = ET.fromstring(archive.read(part))
            slide_shapes = [
                node for node in root.findall(f".//{{{PML}}}sp")
                if _shape_metadata(node) is not None
            ]
            for shape in slide_shapes:
                metadata = _shape_metadata(shape)
                text = _shape_text(shape)
                if not text or len(text) < 15 or metadata is None:
                    continue
                identity = f"{metadata.attrib.get('name', '')} {text}".lower()
                if "instruction" not in identity and "production note" not in identity and not _INSTRUCTION_WORDS.search(text):
                    continue
                bold = any(
                    item.attrib.get("b") in {"1", "true"}
                    for item in shape.findall(f".//{{{DML}}}rPr")
                )
                if not bold and "instruction" not in identity and "production note" not in identity:
                    continue
                shape_id = metadata.attrib.get("id")
                related = _overlapping_container_ids(shape, slide_shapes, slide_index)
                instructions.append(
                    ProductionInstruction(
                        id=f"callout-{slide_index}-{shape_id}",
                        slide_index=slide_index,
                        source="visible_callout",
                        raw_text=text,
                        priority=40,
                        object_id=f"s{slide_index}:{shape_id}",
                        related_object_ids=related,
                        part_name=part,
                    )
                )

        notes_by_slide = _notes_parts_by_slide(archive)
        for slide_index, notes_part in notes_by_slide.items():
            if slide_index not in target_slides:
                continue
            root = ET.fromstring(archive.read(notes_part))
            for shape in root.findall(f".//{{{PML}}}sp"):
                text = _shape_text(shape)
                if len(text) >= 15 and _INSTRUCTION_WORDS.search(text):
                    metadata = _shape_metadata(shape)
                    shape_id = metadata.attrib.get("id") if metadata is not None else "unknown"
                    instructions.append(
                        ProductionInstruction(
                            id=f"speaker-{slide_index}-{shape_id}",
                            slide_index=slide_index,
                            source="speaker_note",
                            raw_text=text,
                            priority=60,
                            object_id=shape_id,
                            part_name=notes_part,
                        )
                    )

        comment_slides = _comment_parts_by_slide(archive)
        for member in sorted(name for name in archive.namelist() if name.startswith("ppt/comments/") and name.endswith(".xml")):
            try:
                root = ET.fromstring(archive.read(member))
            except ET.ParseError:
                continue
            candidates = [
                item for item in root.iter()
                if item.tag.rsplit("}", 1)[-1].lower() in {"cm", "comment"}
            ] or [root]
            for index, comment in enumerate(candidates):
                text = _normalize_text(
                    " ".join(
                        item.text or ""
                        for item in comment.iter()
                        if item.tag.rsplit("}", 1)[-1].lower() in {"t", "text"}
                    )
                )
                if len(text) < 3:
                    continue
                slide_index = comment_slides.get(member, 1)
                if slide_index not in target_slides:
                    continue
                instructions.append(
                    ProductionInstruction(
                        id=f"comment-{slide_index}-{index}",
                        slide_index=slide_index,
                        source="comment",
                        raw_text=text,
                        author=comment.attrib.get("authorId"),
                        timestamp=comment.attrib.get("dt"),
                        priority=80,
                        object_id=comment.attrib.get("idx"),
                        part_name=member,
                    )
                )
    unique: dict[tuple[int, str, str], ProductionInstruction] = {}
    for instruction in instructions:
        key = (instruction.slide_index, instruction.source, _normalize_text(instruction.raw_text).lower())
        unique.setdefault(key, instruction)
    return sorted(unique.values(), key=lambda item: (item.slide_index, -item.priority, item.id))


def _overlapping_container_ids(
    note_shape: ET.Element,
    shapes: list[ET.Element],
    slide_index: int,
) -> list[str]:
    x, y, width, height = _shape_transform(note_shape)
    if None in {x, y, width, height}:
        return []
    assert x is not None and y is not None and width is not None and height is not None
    note_area = max(1.0, width * height)
    related: list[str] = []
    for candidate in shapes:
        if candidate is note_shape or _shape_text(candidate):
            continue
        cx, cy, cw, ch = _shape_transform(candidate)
        if None in {cx, cy, cw, ch}:
            continue
        assert cx is not None and cy is not None and cw is not None and ch is not None
        overlap_w = max(0.0, min(x + width, cx + cw) - max(x, cx))
        overlap_h = max(0.0, min(y + height, cy + ch) - max(y, cy))
        overlap = overlap_w * overlap_h
        if overlap / min(note_area, max(1.0, cw * ch)) < 0.75:
            continue
        metadata = _shape_metadata(candidate)
        if metadata is not None and metadata.attrib.get("id", "").isdigit():
            related.append(f"s{slide_index}:{metadata.attrib['id']}")
    return related


def _comment_parts_by_slide(archive: zipfile.ZipFile) -> dict[str, int]:
    slides = slide_part_names(archive)
    mapping: dict[str, int] = {}
    for slide_index, slide_part in enumerate(slides, start=1):
        slide_path = PurePosixPath(slide_part)
        rels_name = str(slide_path.parent / "_rels" / f"{slide_path.name}.rels")
        if rels_name not in archive.namelist():
            continue
        root = ET.fromstring(archive.read(rels_name))
        for relationship in root.findall(f"{{{PKG_REL}}}Relationship"):
            if "comment" not in relationship.attrib.get("Type", "").lower():
                continue
            target = _resolve_target(slide_part, relationship.attrib.get("Target", ""))
            mapping[target] = slide_index
    return mapping


def _notes_parts_by_slide(archive: zipfile.ZipFile) -> dict[int, str]:
    slides = slide_part_names(archive)
    part_to_index = {part: index for index, part in enumerate(slides, start=1)}
    mapping: dict[int, str] = {}
    for rels_name in archive.namelist():
        if not re.fullmatch(r"ppt/notesSlides/_rels/notesSlide\d+\.xml\.rels", rels_name):
            continue
        root = ET.fromstring(archive.read(rels_name))
        source_part = _relationship_source_part(rels_name)
        for relationship in root.findall(f"{{{PKG_REL}}}Relationship"):
            if relationship.attrib.get("Type", "").endswith("/slide"):
                target = _resolve_target(source_part, relationship.attrib["Target"])
                if target in part_to_index:
                    mapping[part_to_index[target]] = source_part
    return mapping


def apply_ooxml_patch(
    source_path: str | Path,
    output_path: str | Path,
    plan: PptxPatchPlan,
) -> Path:
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if sha256_file(source) != plan.source_sha256:
        raise OoxmlEditError("patch source checksum does not match the PowerPoint file")
    output.parent.mkdir(parents=True, exist_ok=True)
    operations_by_slide: dict[int, list[PptxPatchOperation]] = {}
    for operation in plan.operations:
        operations_by_slide.setdefault(operation.slide_index, []).append(operation)
    callout_ids: dict[int, set[int]] = {}
    note_texts: set[str] = set()
    comment_texts: set[str] = set()
    if plan.cleanup_executed_instructions:
        for instruction in plan.instructions:
            note_texts.add(_normalize_text(instruction.raw_text))
            if instruction.source == "comment":
                comment_texts.add(_normalize_text(instruction.raw_text))
            if instruction.source == "visible_callout" and instruction.object_id:
                try:
                    slide, shape = instruction.object_id.removeprefix("s").split(":", 1)
                    callout_ids.setdefault(int(slide), set()).add(int(shape))
                except ValueError:
                    pass
            for related in instruction.related_object_ids:
                try:
                    slide, shape = related.removeprefix("s").split(":", 1)
                    callout_ids.setdefault(int(slide), set()).add(int(shape))
                except ValueError:
                    pass

    with zipfile.ZipFile(source, "r") as input_zip:
        slides = slide_part_names(input_zip)
        with tempfile.NamedTemporaryFile(
            prefix="editable-pptx-patch-", suffix=".pptx", delete=False, dir=output.parent
        ) as temporary_handle:
            temporary = Path(temporary_handle.name)
        try:
            with zipfile.ZipFile(temporary, "w") as output_zip:
                for info in input_zip.infolist():
                    member = info.filename
                    data = input_zip.read(member)
                    if member in slides:
                        slide_index = slides.index(member) + 1
                        data = _patch_slide_xml(
                            data,
                            operations_by_slide.get(slide_index, []),
                            callout_ids.get(slide_index, set()),
                        )
                    elif member.startswith("ppt/notesSlides/notesSlide") and member.endswith(".xml") and note_texts:
                        data = _remove_note_texts(data, note_texts)
                    elif member.startswith("ppt/comments/") and member.endswith(".xml") and comment_texts:
                        data = _remove_comment_texts(data, comment_texts)
                    output_zip.writestr(info, data)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    validation = validate_ooxml(output)
    if not validation["compatible"]:
        output.unlink(missing_ok=True)
        raise OoxmlEditError("patched PowerPoint failed OOXML validation: " + "; ".join(validation["errors"]))
    return output


def _patch_slide_xml(data: bytes, operations: list[PptxPatchOperation], remove_ids: set[int]) -> bytes:
    root = ET.fromstring(data)
    for shape_id in sorted(remove_ids):
        _delete_shape(root, shape_id)
    existing_ids = {
        int(item.attrib["id"])
        for item in root.findall(f".//{{{PML}}}cNvPr")
        if item.attrib.get("id", "").isdigit()
    }
    for operation in operations:
        node = _find_shape(root, operation.target_shape_id)
        if node is None:
            # Cleanup may have already removed a note/callout that the LLM also
            # represented explicitly as a delete operation. Deletion is idempotent.
            if operation.action == "delete":
                continue
            raise OoxmlEditError(
                f"shape {operation.target_shape_id} not found on slide {operation.slide_index}"
            )
        if operation.action == "delete":
            _delete_shape(root, operation.target_shape_id)
        elif operation.action == "replace_text":
            texts = node.findall(f".//{{{DML}}}t")
            if not texts:
                raise OoxmlEditError(f"shape {operation.target_shape_id} has no editable text")
            texts[0].text = operation.text or ""
            for text in texts[1:]:
                text.text = ""
        elif operation.action in {"move", "resize"}:
            _apply_transform(node, operation)
        elif operation.action == "recolor":
            _apply_color(node, operation.color or "#000000")
        elif operation.action == "set_font_size":
            _apply_font_size(node, operation.font_size_pt or 7.5)
        elif operation.action == "duplicate":
            assert operation.new_shape_id is not None
            if operation.new_shape_id in existing_ids:
                raise OoxmlEditError(f"duplicate shape id {operation.new_shape_id} already exists")
            duplicate = copy.deepcopy(node)
            metadata = _shape_metadata(duplicate)
            if metadata is None:
                raise OoxmlEditError("duplicate source has no non-visual metadata")
            metadata.attrib["id"] = str(operation.new_shape_id)
            metadata.attrib["name"] = operation.new_name or f"{metadata.attrib.get('name', 'Shape')} copy"
            _apply_transform(duplicate, operation)
            parent = _parent_map(root).get(node)
            if parent is None:
                raise OoxmlEditError("duplicate source has no parent")
            parent.insert(list(parent).index(node) + 1, duplicate)
            existing_ids.add(operation.new_shape_id)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _find_shape(root: ET.Element, shape_id: int) -> ET.Element | None:
    parents = _parent_map(root)
    for metadata in root.findall(f".//{{{PML}}}cNvPr"):
        if metadata.attrib.get("id") != str(shape_id):
            continue
        node = metadata
        while node in parents:
            node = parents[node]
            if node.tag in {
                f"{{{PML}}}sp",
                f"{{{PML}}}pic",
                f"{{{PML}}}graphicFrame",
                f"{{{PML}}}grpSp",
                f"{{{PML}}}cxnSp",
            }:
                return node
    return None


def _delete_shape(root: ET.Element, shape_id: int) -> None:
    node = _find_shape(root, shape_id)
    if node is None:
        return
    parent = _parent_map(root).get(node)
    if parent is not None:
        parent.remove(node)


def _transform_nodes(node: ET.Element) -> tuple[ET.Element | None, ET.Element | None]:
    offset = node.find(f".//{{{DML}}}xfrm/{{{DML}}}off")
    extent = node.find(f".//{{{DML}}}xfrm/{{{DML}}}ext")
    if offset is None:
        offset = node.find(f".//{{{PML}}}xfrm/{{{DML}}}off")
    if extent is None:
        extent = node.find(f".//{{{PML}}}xfrm/{{{DML}}}ext")
    return offset, extent


def _apply_transform(node: ET.Element, operation: PptxPatchOperation) -> None:
    offset, extent = _transform_nodes(node)
    if offset is None or extent is None:
        raise OoxmlEditError(f"shape {operation.target_shape_id} has no editable transform")
    if operation.x_pt is not None:
        offset.attrib["x"] = str(round(operation.x_pt * EMU_PER_POINT))
    elif operation.dx_pt is not None:
        offset.attrib["x"] = str(int(offset.attrib.get("x", "0")) + round(operation.dx_pt * EMU_PER_POINT))
    if operation.y_pt is not None:
        offset.attrib["y"] = str(round(operation.y_pt * EMU_PER_POINT))
    elif operation.dy_pt is not None:
        offset.attrib["y"] = str(int(offset.attrib.get("y", "0")) + round(operation.dy_pt * EMU_PER_POINT))
    if operation.width_pt is not None:
        extent.attrib["cx"] = str(round(operation.width_pt * EMU_PER_POINT))
    if operation.height_pt is not None:
        extent.attrib["cy"] = str(round(operation.height_pt * EMU_PER_POINT))


def _apply_color(node: ET.Element, color: str) -> None:
    normalized = color.removeprefix("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", normalized):
        raise OoxmlEditError(f"invalid RGB color: {color}")
    existing = node.find(f".//{{{DML}}}solidFill/{{{DML}}}srgbClr")
    if existing is not None:
        existing.attrib["val"] = normalized
        return
    properties = node.find(f"{{{PML}}}spPr") or node.find(f"{{{PML}}}grpSpPr")
    if properties is None:
        raise OoxmlEditError("shape has no fill properties")
    solid = ET.SubElement(properties, f"{{{DML}}}solidFill")
    ET.SubElement(solid, f"{{{DML}}}srgbClr", {"val": normalized})


def _apply_font_size(node: ET.Element, size_pt: float) -> None:
    text_properties = node.findall(f".//{{{DML}}}rPr") + node.findall(f".//{{{DML}}}defRPr")
    if not text_properties:
        raise OoxmlEditError("shape has no editable text formatting")
    for properties in text_properties:
        properties.attrib["sz"] = str(round(size_pt * 100))


def _remove_note_texts(data: bytes, note_texts: set[str]) -> bytes:
    root = ET.fromstring(data)
    for shape in root.findall(f".//{{{PML}}}sp"):
        text = _shape_text(shape)
        if any(note and (note == text or note in text) for note in note_texts):
            for item in shape.findall(f".//{{{DML}}}t"):
                item.text = ""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _remove_comment_texts(data: bytes, comment_texts: set[str]) -> bytes:
    root = ET.fromstring(data)
    parents = _parent_map(root)
    candidates = [
        item for item in root.iter()
        if item.tag.rsplit("}", 1)[-1].lower() in {"cm", "comment"}
    ]
    for comment in candidates:
        text = _normalize_text(
            " ".join(
                item.text or ""
                for item in comment.iter()
                if item.tag.rsplit("}", 1)[-1].lower() in {"t", "text"}
            )
        )
        if text in comment_texts and comment in parents:
            parents[comment].remove(comment)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _remove_comment_relationships(data: bytes) -> bytes:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data
    for relationship in list(root.findall(f"{{{PKG_REL}}}Relationship")):
        if "comment" in relationship.attrib.get("Type", "").lower():
            root.remove(relationship)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _remove_comment_content_types(data: bytes) -> bytes:
    root = ET.fromstring(data)
    for child in list(root):
        if "comment" in child.attrib.get("ContentType", "").lower():
            root.remove(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def content_preserved(
    before: dict[int, list[str]],
    after: dict[int, list[str]],
    removed_instruction_texts: Iterable[str],
) -> tuple[bool, list[str]]:
    removed = {_normalize_text(value) for value in removed_instruction_texts}
    missing: list[str] = []
    for slide_index, texts in before.items():
        expected = Counter(text for text in texts if _normalize_text(text) not in removed)
        actual = Counter(after.get(slide_index, []))
        for text, count in (expected - actual).items():
            missing.extend([f"slide {slide_index}: {text}"] * count)
    return not missing, missing
