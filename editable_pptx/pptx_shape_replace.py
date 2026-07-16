from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr


SHAPE_PATTERN = re.compile(r"<p:sp>(?:(?!<p:sp>).)*?</p:sp>", re.DOTALL)
SHAPE_PROPERTIES_PATTERN = re.compile(r"<p:spPr>.*?</p:spPr>", re.DOTALL)
TRANSFORM_PATTERN = re.compile(r"<a:xfrm\b.*?</a:xfrm>", re.DOTALL)


def _shape_xml_by_name(slide_xml: str, name: str) -> str:
    marker = f"name={quoteattr(name)}"
    matches = [shape for shape in SHAPE_PATTERN.findall(slide_xml) if marker in shape]
    if len(matches) != 1:
        raise ValueError(f"Expected one shape named {name!r}, found {len(matches)}")
    return matches[0]


def replace_shape_geometry(
    source_pptx: str | Path,
    donor_pptx: str | Path,
    output_pptx: str | Path,
    *,
    target_shape_name: str,
    donor_shape_name: str,
    slide_number: int = 1,
) -> Path:
    """Replace one native shape's geometry/fill while preserving its position and z-order."""

    source = Path(source_pptx).resolve()
    donor = Path(donor_pptx).resolve()
    output = Path(output_pptx).resolve()
    slide_name = f"ppt/slides/slide{slide_number}.xml"

    with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(donor) as donor_archive:
        source_slide = source_archive.read(slide_name).decode("utf-8")
        donor_slide = donor_archive.read(slide_name).decode("utf-8")
        target_shape = _shape_xml_by_name(source_slide, target_shape_name)
        donor_shape = _shape_xml_by_name(donor_slide, donor_shape_name)
        target_properties_match = SHAPE_PROPERTIES_PATTERN.search(target_shape)
        donor_properties_match = SHAPE_PROPERTIES_PATTERN.search(donor_shape)
        if target_properties_match is None or donor_properties_match is None:
            raise ValueError("Target and donor shapes must both contain p:spPr")
        target_properties = target_properties_match.group(0)
        donor_properties = donor_properties_match.group(0)
        target_transform_match = TRANSFORM_PATTERN.search(target_properties)
        donor_transform_match = TRANSFORM_PATTERN.search(donor_properties)
        if target_transform_match is None or donor_transform_match is None:
            raise ValueError("Target and donor shapes must both contain a:xfrm")
        replacement_properties = donor_properties.replace(
            donor_transform_match.group(0),
            target_transform_match.group(0),
            1,
        )
        # The donor slide can declare DrawingML's ``a`` namespace on its root,
        # while the source slide declares it locally on each original child.
        # Once the donor spPr is transplanted, make that namespace self-contained
        # so the source slide remains valid without reserializing the whole XML.
        replacement_properties = replacement_properties.replace(
            "<p:spPr>",
            '<p:spPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">',
            1,
        )
        replacement_shape = target_shape.replace(
            target_properties,
            replacement_properties,
            1,
        )
        patched_slide = source_slide.replace(target_shape, replacement_shape, 1).encode("utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}-",
            suffix=".pptx",
            dir=output.parent,
            delete=False,
        ) as temporary_file:
            temporary = Path(temporary_file.name)
        try:
            with zipfile.ZipFile(temporary, "w") as output_archive:
                for item in source_archive.infolist():
                    data = patched_slide if item.filename == slide_name else source_archive.read(item.filename)
                    output_archive.writestr(item, data)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return output


__all__ = ["replace_shape_geometry"]
