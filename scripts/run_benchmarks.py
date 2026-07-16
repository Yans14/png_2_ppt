#!/usr/bin/env python3
"""Run reproducible image-to-editable-PPTX evaluations and aggregate their reports."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PIL import Image

from editable_pptx.version import METRIC_VERSION, __version__


ROOT = Path(__file__).resolve().parents[1]
COMPLETED_STATUSES = {"success", "cached", "rescored"}


def report_meets_editability_contract(report: dict[str, Any]) -> bool:
    """Reject blank, flattened, overflowing, or otherwise unusable decks."""

    audit = report.get("audit")
    ooxml = report.get("ooxml_validation")
    if not isinstance(audit, dict) or not isinstance(ooxml, dict):
        return False
    native_objects = int(audit.get("native_shape_objects", 0)) + int(
        audit.get("picture_objects", 0)
    )
    return (
        native_objects > 0
        and audit.get("flattened_slide") is False
        and int(audit.get("canvas_overflow_count", 0)) == 0
        and ooxml.get("compatible") is True
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=ROOT / "benchmarks" / "manifest.json", type=Path)
    parser.add_argument("--targets-dir", default=ROOT / "benchmarks" / "cache" / "targets", type=Path)
    parser.add_argument("--results-dir", default=ROOT / "benchmarks" / "results", type=Path)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument(
        "--quality-profile",
        choices=["budget", "balanced", "max"],
        default="balanced",
    )
    parser.add_argument(
        "--local-optimization",
        choices=["auto", "on", "off"],
        default="auto",
    )
    parser.add_argument(
        "--powerpoint-validation",
        choices=["off", "auto", "required"],
        default="auto",
    )
    parser.add_argument(
        "--font-policy",
        choices=["portable", "exact"],
        default="portable",
    )
    parser.add_argument("--iterations", default=1, type=int)
    parser.add_argument("--target-score", default=0.93, type=float)
    parser.add_argument("--timeout", default=900, type=int)
    parser.add_argument("--max-output-tokens", default=64000, type=int)
    parser.add_argument("--jobs", default=1, type=int)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--rescore-existing",
        action="store_true",
        help="Render existing specs with the current engine without making API calls",
    )
    parser.add_argument(
        "--reuse-existing-spec",
        action="store_true",
        help="Start paid correction passes from an existing spec instead of reconstructing from scratch",
    )
    return parser.parse_args()


def cache_matches(report: dict[str, Any], args: argparse.Namespace) -> bool:
    return (
        report.get("engine_version") == __version__
        and report.get("metric_version") == METRIC_VERSION
        and report.get("model") == args.model
        and report.get("quality_profile") == getattr(args, "quality_profile", "balanced")
        and report.get("local_optimization")
        == getattr(args, "local_optimization", "auto")
        and report.get("powerpoint_validation_mode")
        == getattr(args, "powerpoint_validation", "auto")
        and report.get("font_policy") == getattr(args, "font_policy", "portable")
        and report.get("raster_policy") == "photos-only"
        and report.get("requested_iterations") == args.iterations
        and report.get("target_score") == args.target_score
        and report_meets_editability_contract(report)
    )


def effective_iterations(args: argparse.Namespace) -> int:
    """Offline rescoring renders exactly once and can never trigger LLM refinement."""

    return 0 if args.rescore_existing else int(args.iterations)


def run_case(
    case: dict[str, Any],
    args: argparse.Namespace,
    quota_exhausted: threading.Event,
) -> dict[str, Any]:
    case_id = case["id"]
    if quota_exhausted.is_set():
        return {
            "id": case_id,
            "status": "skipped",
            "elapsed_seconds": 0,
            "error": "Skipped because a previous case exhausted the OpenAI API quota.",
        }
    case_dir = args.results_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    report_path = case_dir / "report.json"
    spec_path = case_dir / "spec.json"
    if (args.rescore_existing or args.reuse_existing_spec) and not spec_path.exists():
        return {
            "id": case_id,
            "status": "missing_spec",
            "elapsed_seconds": 0,
            "error": "No existing spec is available for the requested resumed run.",
        }
    if report_path.exists() and not args.force and not args.rescore_existing:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if cache_matches(report, args):
            return {
                "id": case_id,
                "status": "cached",
                "elapsed_seconds": 0,
                "report": report,
            }

    iterations = effective_iterations(args)
    command = [
        sys.executable,
        "-m",
        "editable_pptx",
        "--input",
        str(args.targets_dir / f"{case_id}.png"),
        "--output",
        str(case_dir / "reconstruction.pptx"),
        "--spec-out",
        str(spec_path),
        "--report",
        str(report_path),
        "--workdir",
        str(case_dir / "work"),
        "--model",
        args.model,
        "--quality-profile",
        args.quality_profile,
        "--local-optimization",
        args.local_optimization,
        "--powerpoint-validation",
        args.powerpoint_validation,
        "--font-policy",
        args.font_policy,
        "--iterations",
        str(iterations),
        "--target-score",
        str(args.target_score),
        "--raster-policy",
        "photos-only",
        "--timeout",
        str(args.timeout),
        "--max-output-tokens",
        str(args.max_output_tokens),
    ]
    if args.rescore_existing or args.reuse_existing_spec:
        command.extend(["--spec-in", str(spec_path)])
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=args.timeout * (iterations + 2),
        check=False,
    )
    elapsed = round(time.monotonic() - started, 3)
    (case_dir / "run.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (case_dir / "run.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not report_path.exists():
        normalized_error = completed.stderr.lower()
        if "insufficient_quota" in normalized_error or "exceeded your current quota" in normalized_error:
            quota_exhausted.set()
        return {
            "id": case_id,
            "status": "failed",
            "elapsed_seconds": elapsed,
            "returncode": completed.returncode,
            "error": completed.stderr.strip()[-2000:],
        }
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report_meets_editability_contract(report):
        return {
            "id": case_id,
            "status": "invalid_output",
            "elapsed_seconds": elapsed,
            "error": "Generated deck failed the native editability contract.",
            "report": report,
        }
    status = "rescored" if args.rescore_existing else "success"
    return {"id": case_id, "status": status, "elapsed_seconds": elapsed, "report": report}


def compact_row(result: dict[str, Any]) -> dict[str, Any]:
    report = result.get("report", {})
    metrics = report.get("best_metrics", {})
    audit = report.get("audit", {})
    return {
        "id": result["id"],
        "status": result["status"],
        "elapsed_seconds": result.get("elapsed_seconds", ""),
        "similarity_score": metrics.get("similarity_score", ""),
        "pixel_mae": metrics.get("pixel_mae", ""),
        "edge_mae": metrics.get("edge_mae", ""),
        "foreground_color_similarity": metrics.get("foreground_color_similarity", ""),
        "objects": (
            int(audit.get("native_shape_objects", 0)) + int(audit.get("picture_objects", 0))
            if audit
            else ""
        ),
        "native_text_runs": audit.get("native_text_runs", ""),
        "picture_objects": audit.get("picture_objects", ""),
        "flattened_slide": audit.get("flattened_slide", ""),
        "canvas_overflow_count": audit.get("canvas_overflow_count", ""),
        "iterations": len(report.get("iterations", [])) if report else "",
    }


def summary_stem(
    selected_cases: list[str] | None,
    *,
    rescore_existing: bool = False,
) -> str:
    prefix = "summary-rescore" if rescore_existing else "summary"
    if not selected_cases:
        return prefix
    normalized = "-".join(sorted(selected_cases))
    return prefix + "-" + normalized


def validate_targets(
    cases: list[dict[str, Any]],
    *,
    targets_dir: Path,
    expected_width: int,
    expected_height: int,
) -> None:
    errors: list[str] = []
    for case in cases:
        target = targets_dir / f"{case['id']}.png"
        if not target.exists():
            errors.append(f"{case['id']}: missing target {target}")
            continue
        try:
            with Image.open(target) as image:
                if image.size != (expected_width, expected_height):
                    errors.append(
                        f"{case['id']}: expected {expected_width}x{expected_height}, "
                        f"found {image.width}x{image.height}"
                    )
        except OSError as error:
            errors.append(f"{case['id']}: unreadable target ({error})")
    if errors:
        raise SystemExit("Benchmark target validation failed:\n" + "\n".join(errors))


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = set(args.cases or [])
    cases = [case for case in manifest["cases"] if not selected or case["id"] in selected]
    missing = selected - {case["id"] for case in cases}
    if missing:
        raise SystemExit(f"unknown benchmark case(s): {', '.join(sorted(missing))}")
    validate_targets(
        cases,
        targets_dir=args.targets_dir,
        expected_width=int(manifest["canvas"]["width"]),
        expected_height=int(manifest["canvas"]["height"]),
    )
    args.results_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    quota_exhausted = threading.Event()
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(run_case, case, args, quota_exhausted): case["id"]
            for case in cases
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result['id']}: {result['status']} ({result.get('elapsed_seconds', 0)}s)")
    results.sort(key=lambda item: item["id"])
    rows = [compact_row(result) for result in results]
    successful_scores = [float(row["similarity_score"]) for row in rows if row["similarity_score"] != ""]
    completed_rows = [row for row in rows if row["status"] in COMPLETED_STATUSES]
    summary = {
        "model": args.model,
        "quality_profile": args.quality_profile,
        "local_optimization": args.local_optimization,
        "powerpoint_validation": args.powerpoint_validation,
        "font_policy": args.font_policy,
        "reuse_existing_spec": args.reuse_existing_spec,
        "iterations": effective_iterations(args),
        "case_count": len(results),
        "success_count": sum(result["status"] in COMPLETED_STATUSES for result in results),
        "mean_similarity_score": (
            round(sum(successful_scores) / len(successful_scores), 6) if successful_scores else None
        ),
        "all_native_editable": bool(completed_rows)
        and all(
            report_meets_editability_contract(result.get("report", {}))
            for result in results
            if result["status"] in COMPLETED_STATUSES
        ),
        "rows": rows,
        "results": results,
    }
    output_stem = summary_stem(args.cases, rescore_existing=args.rescore_existing)
    (args.results_dir / f"{output_stem}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (args.results_dir / f"{output_stem}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["id", "status"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({key: summary[key] for key in summary if key not in {"rows", "results"}}, indent=2))
    return 0 if summary["success_count"] == summary["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
