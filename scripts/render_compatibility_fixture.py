#!/usr/bin/env python3
"""Render the deterministic cross-platform PowerPoint compatibility fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

from editable_pptx.models import SlideSpec
from editable_pptx.renderer import render_pptx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "test" / "fixtures" / "powerpoint-compatibility-spec.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    spec = SlideSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
    render_pptx(spec, args.output, timeout_seconds=args.timeout)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
