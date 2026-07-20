from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from .ooxml_edit import CONTENT_TYPES, PKG_REL, PML, REL, slide_part_names
from .powerpoint import validate_ooxml


PRESENTATION_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class TemplateApplyError(RuntimeError):
    pass


def _rels_name(part_name: str) -> str:
    part = PurePosixPath(part_name)
    return str(part.parent / "_rels" / f"{part.name}.rels")


def _source_part(rels_name: str) -> str:
    path = PurePosixPath(rels_name)
    return str(path.parent.parent / path.name.removesuffix(".rels"))


def _resolve(source_part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target)).lstrip("/")


def _relative(source_part: str, target_part: str) -> str:
    return posixpath.relpath(target_part, posixpath.dirname(source_part))


def _next_part(names: set[str], prefix: str, suffix: str = ".xml") -> str:
    pattern = re.compile(re.escape(prefix) + r"(\d+)" + re.escape(suffix) + r"$")
    numbers = [
        int(match.group(1))
        for name in names
        if (match := pattern.fullmatch(name))
    ]
    return f"{prefix}{max(numbers, default=0) + 1}{suffix}"


def _next_relationship_id(root: ET.Element) -> str:
    used = {
        int(match.group(1))
        for item in root.findall(f"{{{PKG_REL}}}Relationship")
        if (match := re.fullmatch(r"rId(\d+)", item.attrib.get("Id", "")))
    }
    return f"rId{max(used, default=0) + 1}"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _template_layout_parts(
    archive: zipfile.ZipFile,
    template_slide_index: int,
) -> tuple[str, str, str, str]:
    slides = slide_part_names(archive)
    if template_slide_index < 1 or template_slide_index > len(slides):
        raise TemplateApplyError("template slide index is outside the deck")
    slide_part = slides[template_slide_index - 1]
    slide_rels = ET.fromstring(archive.read(_rels_name(slide_part)))
    layout_relation = next(
        (
            item
            for item in slide_rels.findall(f"{{{PKG_REL}}}Relationship")
            if item.attrib.get("Type", "").endswith("/slideLayout")
        ),
        None,
    )
    if layout_relation is None:
        raise TemplateApplyError("template slide has no slideLayout relationship")
    layout_part = _resolve(slide_part, layout_relation.attrib["Target"])
    layout_rels_name = _rels_name(layout_part)
    layout_rels = ET.fromstring(archive.read(layout_rels_name))
    master_relation = next(
        (
            item
            for item in layout_rels.findall(f"{{{PKG_REL}}}Relationship")
            if item.attrib.get("Type", "").endswith("/slideMaster")
        ),
        None,
    )
    if master_relation is None:
        raise TemplateApplyError("template layout has no slideMaster relationship")
    master_part = _resolve(layout_part, master_relation.attrib["Target"])
    return slide_part, layout_part, layout_rels_name, master_part


def import_template_layout(
    source_path: str | Path,
    template_path: str | Path,
    output_path: str | Path,
    *,
    template_slide_index: int,
    source_slide_indices: list[int],
) -> Path:
    source = Path(source_path).resolve()
    template = Path(template_path).resolve()
    output = Path(output_path).resolve()
    if source.suffix.lower() != ".pptx" or template.suffix.lower() != ".pptx":
        raise TemplateApplyError("layout import accepts PPTX files only")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as source_zip, zipfile.ZipFile(template) as template_zip:
        source_names = set(source_zip.namelist())
        _, template_layout, template_layout_rels, template_master = _template_layout_parts(
            template_zip, template_slide_index
        )
        template_master_rels = _rels_name(template_master)
        new_layout = _next_part(source_names, "ppt/slideLayouts/slideLayout")
        source_names.add(new_layout)
        new_master = _next_part(source_names, "ppt/slideMasters/slideMaster")
        source_names.add(new_master)
        new_theme = _next_part(source_names, "ppt/theme/theme")
        source_names.add(new_theme)
        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}

        layout_xml = template_zip.read(template_layout)
        layout_rels_root = ET.fromstring(template_zip.read(template_layout_rels))
        master_xml_root = ET.fromstring(template_zip.read(template_master))
        master_rels_root = ET.fromstring(template_zip.read(template_master_rels))

        layout_master_relation = next(
            item
            for item in layout_rels_root.findall(f"{{{PKG_REL}}}Relationship")
            if item.attrib.get("Type", "").endswith("/slideMaster")
        )
        layout_master_relation.attrib["Target"] = _relative(new_layout, new_master)

        selected_layout_relation_id = None
        for item in list(master_rels_root.findall(f"{{{PKG_REL}}}Relationship")):
            relation_type = item.attrib.get("Type", "")
            target = _resolve(template_master, item.attrib.get("Target", ""))
            if relation_type.endswith("/slideLayout"):
                if target == template_layout:
                    selected_layout_relation_id = item.attrib.get("Id")
                    item.attrib["Target"] = _relative(new_master, new_layout)
                else:
                    master_rels_root.remove(item)
            elif relation_type.endswith("/theme"):
                if target not in template_zip.namelist():
                    raise TemplateApplyError("template master theme is missing")
                theme_bytes = template_zip.read(target)
                existing_theme = next(
                    (
                        name
                        for name in source_names
                        if name.startswith("ppt/theme/")
                        and name in source_zip.namelist()
                        and _digest(source_zip.read(name)) == _digest(theme_bytes)
                    ),
                    None,
                )
                theme_part = existing_theme or new_theme
                if existing_theme is None:
                    additions[theme_part] = theme_bytes
                item.attrib["Target"] = _relative(new_master, theme_part)
            elif relation_type.endswith("/image") and target in template_zip.namelist():
                target_bytes = template_zip.read(target)
                existing = next(
                    (
                        name
                        for name in source_names
                        if name.startswith("ppt/media/")
                        and name in source_zip.namelist()
                        and _digest(source_zip.read(name)) == _digest(target_bytes)
                    ),
                    None,
                )
                if existing is None:
                    extension = PurePosixPath(target).suffix
                    existing = _next_part(source_names, "ppt/media/image", extension)
                    source_names.add(existing)
                    additions[existing] = target_bytes
                item.attrib["Target"] = _relative(new_master, existing)
            elif relation_type.endswith(("/hyperlink", "/oleObject", "/audio", "/video")):
                master_rels_root.remove(item)

        if selected_layout_relation_id is None:
            raise TemplateApplyError("template master did not reference the selected layout")
        layout_ids = master_xml_root.find(f"{{{PML}}}sldLayoutIdLst")
        if layout_ids is not None:
            for child in list(layout_ids):
                if child.attrib.get(f"{{{REL}}}id") != selected_layout_relation_id:
                    layout_ids.remove(child)

        additions[new_layout] = layout_xml
        additions[_rels_name(new_layout)] = ET.tostring(
            layout_rels_root, encoding="utf-8", xml_declaration=True
        )
        additions[new_master] = ET.tostring(
            master_xml_root, encoding="utf-8", xml_declaration=True
        )
        additions[_rels_name(new_master)] = ET.tostring(
            master_rels_root, encoding="utf-8", xml_declaration=True
        )

        presentation_rels = ET.fromstring(source_zip.read("ppt/_rels/presentation.xml.rels"))
        new_master_rel_id = _next_relationship_id(presentation_rels)
        ET.SubElement(
            presentation_rels,
            f"{{{PKG_REL}}}Relationship",
            {
                "Id": new_master_rel_id,
                "Type": f"{PRESENTATION_REL}/slideMaster",
                "Target": _relative("ppt/presentation.xml", new_master),
            },
        )
        replacements["ppt/_rels/presentation.xml.rels"] = ET.tostring(
            presentation_rels, encoding="utf-8", xml_declaration=True
        )
        presentation = ET.fromstring(source_zip.read("ppt/presentation.xml"))
        master_list = presentation.find(f"{{{PML}}}sldMasterIdLst")
        if master_list is None:
            master_list = ET.Element(f"{{{PML}}}sldMasterIdLst")
            presentation.insert(0, master_list)
        existing_master_ids = [
            int(item.attrib["id"])
            for item in master_list
            if item.attrib.get("id", "").isdigit()
        ]
        ET.SubElement(
            master_list,
            f"{{{PML}}}sldMasterId",
            {
                "id": str(max(existing_master_ids, default=2147483647) + 1),
                f"{{{REL}}}id": new_master_rel_id,
            },
        )
        replacements["ppt/presentation.xml"] = ET.tostring(
            presentation, encoding="utf-8", xml_declaration=True
        )

        source_slides = slide_part_names(source_zip)
        for slide_index in source_slide_indices:
            if slide_index < 1 or slide_index > len(source_slides):
                raise TemplateApplyError("source slide index is outside the deck")
            part_name = source_slides[slide_index - 1]
            rels_name = _rels_name(part_name)
            rels = ET.fromstring(source_zip.read(rels_name))
            relation = next(
                (
                    item
                    for item in rels.findall(f"{{{PKG_REL}}}Relationship")
                    if item.attrib.get("Type", "").endswith("/slideLayout")
                ),
                None,
            )
            if relation is None:
                relation = ET.SubElement(
                    rels,
                    f"{{{PKG_REL}}}Relationship",
                    {
                        "Id": _next_relationship_id(rels),
                        "Type": f"{PRESENTATION_REL}/slideLayout",
                    },
                )
            relation.attrib["Target"] = _relative(part_name, new_layout)
            replacements[rels_name] = ET.tostring(
                rels, encoding="utf-8", xml_declaration=True
            )

        content_types = ET.fromstring(source_zip.read("[Content_Types].xml"))
        template_content_types = ET.fromstring(template_zip.read("[Content_Types].xml"))
        type_by_part = {
            item.attrib.get("PartName", "").lstrip("/"): item.attrib.get("ContentType", "")
            for item in template_content_types.findall(f"{{{CONTENT_TYPES}}}Override")
        }
        for source_part, new_part in (
            (template_layout, new_layout),
            (template_master, new_master),
        ):
            content_type = type_by_part.get(source_part)
            if content_type:
                ET.SubElement(
                    content_types,
                    f"{{{CONTENT_TYPES}}}Override",
                    {"PartName": f"/{new_part}", "ContentType": content_type},
                )
        replacements["[Content_Types].xml"] = ET.tostring(
            content_types, encoding="utf-8", xml_declaration=True
        )

        with tempfile.NamedTemporaryFile(
            prefix="editable-pptx-template-", suffix=".pptx", delete=False, dir=output.parent
        ) as handle:
            temporary = Path(handle.name)
        try:
            with zipfile.ZipFile(temporary, "w") as output_zip:
                for info in source_zip.infolist():
                    output_zip.writestr(
                        info,
                        replacements.get(info.filename, source_zip.read(info.filename)),
                    )
                for name, data in additions.items():
                    if name not in source_zip.namelist():
                        output_zip.writestr(name, data)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    validation = validate_ooxml(output)
    if not validation["compatible"]:
        output.unlink(missing_ok=True)
        raise TemplateApplyError(
            "imported template layout failed OOXML validation: "
            + "; ".join(validation.get("errors", []))
        )
    return output


__all__ = ["TemplateApplyError", "import_template_layout"]
