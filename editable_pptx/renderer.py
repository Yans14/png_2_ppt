from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import SlideSpec


class RenderError(RuntimeError):
    pass


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
    return output
