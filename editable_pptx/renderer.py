from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .models import SlideSpec


class RenderError(RuntimeError):
    pass


_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_SHAPE_XML = re.compile(r"<p:sp>.*?</p:sp>", re.DOTALL)
_SHAPE_NAME_XML = re.compile(r'<p:cNvPr\b[^>]*\bname="([^"]*)"')
_AUTOFIT_XML = re.compile(r"<a:normAutofit(?:\s[^>]*)?/>")


def _roundtrip_autofit_scales(pptx_path: Path, timeout_seconds: int) -> dict[str, dict[str, dict[str, str]]]:
    """Ask LibreOffice to materialize dynamic PowerPoint text-fit scales."""

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return {}
    with tempfile.TemporaryDirectory(prefix="editable-pptx-autofit-") as temp_dir:
        root = Path(temp_dir)
        profile_uri = (root / "profile").resolve().as_uri()
        try:
            result = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--convert-to",
                    "pptx",
                    "--outdir",
                    str(root),
                    str(pptx_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {}
        roundtripped = root / pptx_path.name
        if result.returncode != 0 or not roundtripped.exists():
            return {}

        scales: dict[str, dict[str, dict[str, str]]] = {}
        with zipfile.ZipFile(roundtripped) as archive:
            for member in archive.namelist():
                if not re.fullmatch(r"ppt/slides/slide\d+\.xml", member):
                    continue
                tree = ElementTree.fromstring(archive.read(member))
                slide_scales: dict[str, dict[str, str]] = {}
                for shape in tree.findall(f".//{{{_PML_NS}}}sp"):
                    metadata = shape.find(
                        f"{{{_PML_NS}}}nvSpPr/{{{_PML_NS}}}cNvPr"
                    )
                    autofit = shape.find(
                        f"{{{_PML_NS}}}txBody/{{{_DML_NS}}}bodyPr/{{{_DML_NS}}}normAutofit"
                    )
                    if metadata is None or autofit is None:
                        continue
                    font_scale = autofit.attrib.get("fontScale")
                    if not font_scale or not font_scale.isdigit():
                        continue
                    attributes = {"fontScale": font_scale}
                    line_reduction = autofit.attrib.get("lnSpcReduction")
                    if line_reduction and line_reduction.isdigit():
                        attributes["lnSpcReduction"] = line_reduction
                    slide_scales[metadata.attrib.get("name", "")] = attributes
                if slide_scales:
                    scales[member] = slide_scales
        return scales


def _inject_autofit_scales_in_xml(
    xml_text: str,
    scales: dict[str, dict[str, str]],
) -> str:
    def replace_shape(match: re.Match[str]) -> str:
        block = match.group(0)
        name_match = _SHAPE_NAME_XML.search(block)
        if not name_match:
            return block
        attributes = scales.get(html.unescape(name_match.group(1)))
        if not attributes:
            return block
        serialized = " ".join(f'{key}="{value}"' for key, value in attributes.items())
        return _AUTOFIT_XML.sub(f"<a:normAutofit {serialized}/>", block, count=1)

    return _SHAPE_XML.sub(replace_shape, xml_text)


def _stabilize_text_autofit(pptx_path: Path, timeout_seconds: int) -> None:
    scales = _roundtrip_autofit_scales(pptx_path, timeout_seconds)
    if not scales:
        return
    temporary = pptx_path.with_name(f".{pptx_path.name}.autofit.tmp")
    try:
        with zipfile.ZipFile(pptx_path, "r") as source, zipfile.ZipFile(
            temporary, "w"
        ) as destination:
            for member in source.infolist():
                data = source.read(member.filename)
                slide_scales = scales.get(member.filename)
                if slide_scales:
                    data = _inject_autofit_scales_in_xml(
                        data.decode("utf-8"), slide_scales
                    ).encode("utf-8")
                destination.writestr(member, data)
        os.replace(temporary, pptx_path)
    finally:
        temporary.unlink(missing_ok=True)


def render_pptx(
    spec: SlideSpec,
    output_path: str | Path,
    *,
    assets: dict[str, str] | None = None,
    node_binary: str = "node",
    renderer_script: str | Path | None = None,
    timeout_seconds: int = 120,
) -> Path:
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    node = shutil.which(node_binary)
    if not node:
        raise RenderError(f"Node.js binary not found: {node_binary}")

    if renderer_script is None:
        packaged_renderer = Path(__file__).resolve().parent / "js" / "index.js"
        source_renderer = Path(__file__).resolve().parents[1] / "src" / "render-image-spec.js"
        renderer_script = packaged_renderer if packaged_renderer.exists() else source_renderer
    script = Path(renderer_script).resolve()
    if not script.exists():
        raise RenderError(f"Renderer script not found: {script}")

    envelope = {
        "spec": spec.model_dump(mode="json"),
        "assets": assets or {},
    }
    with tempfile.TemporaryDirectory(prefix="editable-pptx-render-") as temp_dir:
        spec_path = Path(temp_dir) / "render-spec.json"
        spec_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        try:
            result = subprocess.run(
                [node, str(script), str(spec_path), str(output)],
                cwd=script.parent,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RenderError(f"PPTX rendering timed out after {timeout_seconds}s") from error

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown renderer error").strip()
        raise RenderError(f"PPTX rendering failed: {message}")
    if not output.exists() or output.stat().st_size == 0:
        raise RenderError("PPTX renderer produced no output")
    _stabilize_text_autofit(output, timeout_seconds)
    return output
