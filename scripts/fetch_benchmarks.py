#!/usr/bin/env python3
"""Download public benchmark decks and render the selected slides to PNG."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=ROOT / "benchmarks" / "manifest.json", type=Path)
    parser.add_argument("--cache-dir", default=ROOT / "benchmarks" / "cache", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def find_pdftoppm() -> str:
    for candidate in (
        shutil.which("pdftoppm"),
        "/opt/homebrew/bin/pdftoppm",
        "/usr/local/bin/pdftoppm",
        "/usr/bin/pdftoppm",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SystemExit("pdftoppm is required (install Poppler)")


def download(url: str, destination: Path, *, force: bool) -> None:
    if destination.exists() and not force:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "editable-pptx-benchmark/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, tempfile.NamedTemporaryFile(
        dir=destination.parent, delete=False
    ) as temporary:
        shutil.copyfileobj(response, temporary)
        temporary_path = Path(temporary.name)
    temporary_path.replace(destination)


def contain_on_canvas(source: Path, destination: Path, width: int, height: int) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), "white")
        x = (width - image.width) // 2
        y = (height - image.height) // 2
        canvas.paste(image, (x, y))
        canvas.save(destination, optimize=True)


def main() -> int:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sources_dir = args.cache_dir / "sources"
    targets_dir = args.cache_dir / "targets"
    targets_dir.mkdir(parents=True, exist_ok=True)
    pdf_paths: dict[str, Path] = {}
    for source_id, source in manifest["sources"].items():
        pdf_path = sources_dir / f"{source_id}.pdf"
        download(source["url"], pdf_path, force=args.force)
        pdf_paths[source_id] = pdf_path

    pdftoppm = find_pdftoppm()
    width = int(manifest["canvas"]["width"])
    height = int(manifest["canvas"]["height"])
    for case in manifest["cases"]:
        destination = targets_dir / f"{case['id']}.png"
        if destination.exists() and not args.force:
            continue
        with tempfile.TemporaryDirectory(prefix="editable-pptx-benchmark-") as temp_dir:
            prefix = Path(temp_dir) / "page"
            page = str(case["page"])
            command = [
                pdftoppm,
                "-f",
                page,
                "-l",
                page,
                "-singlefile",
                "-png",
                "-r",
                "150",
                str(pdf_paths[case["source"]]),
                str(prefix),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
            contain_on_canvas(prefix.with_suffix(".png"), destination, width, height)
        print(f"rendered {case['id']} -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
