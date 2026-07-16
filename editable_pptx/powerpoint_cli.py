from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .powerpoint import (
    PowerPointCompatibilityError,
    validate_powerpoint,
    write_validation_report,
)
from .qa import QualityCheckError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate-editable-pptx",
        description="Validate OOXML and optionally round-trip a deck through Microsoft PowerPoint.",
    )
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--mode", choices=["off", "auto", "required"], default="required")
    parser.add_argument("--font-policy", choices=["portable", "exact"], default="portable")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate_powerpoint(
            args.pptx,
            mode=args.mode,
            font_policy=args.font_policy,
            reference_image=args.reference,
            workspace=args.workspace,
            timeout_seconds=args.timeout,
        )
    except (PowerPointCompatibilityError, QualityCheckError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.report:
        write_validation_report(report, args.report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("compatible") else 1


if __name__ == "__main__":
    raise SystemExit(main())
