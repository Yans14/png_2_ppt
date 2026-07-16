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


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=ROOT / "benchmarks" / "manifest.json", type=Path)
    parser.add_argument("--targets-dir", default=ROOT / "benchmarks" / "cache" / "targets", type=Path)
    parser.add_argument("--results-dir", default=ROOT / "benchmarks" / "results", type=Path)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--iterations", default=1, type=int)
    parser.add_argument("--target-score", default=0.93, type=float)
    parser.add_argument("--timeout", default=900, type=int)
    parser.add_argument("--max-output-tokens", default=64000, type=int)
    parser.add_argument("--jobs", default=1, type=int)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


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
    if report_path.exists() and not args.force:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return {"id": case_id, "status": "cached", "elapsed_seconds": 0, "report": report}

    command = [
        sys.executable,
        "-m",
        "editable_pptx",
        "--input",
        str(args.targets_dir / f"{case_id}.png"),
        "--output",
        str(case_dir / "reconstruction.pptx"),
        "--spec-out",
        str(case_dir / "spec.json"),
        "--report",
        str(report_path),
        "--workdir",
        str(case_dir / "work"),
        "--model",
        args.model,
        "--iterations",
        str(args.iterations),
        "--target-score",
        str(args.target_score),
        "--raster-policy",
        "photos-only",
        "--timeout",
        str(args.timeout),
        "--max-output-tokens",
        str(args.max_output_tokens),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=args.timeout * (args.iterations + 2),
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
    return {"id": case_id, "status": "success", "elapsed_seconds": elapsed, "report": report}


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
        "iterations": len(report.get("iterations", [])) if report else "",
    }


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
    summary = {
        "model": args.model,
        "iterations": args.iterations,
        "case_count": len(results),
        "success_count": sum(result["status"] in {"success", "cached"} for result in results),
        "mean_similarity_score": (
            round(sum(successful_scores) / len(successful_scores), 6) if successful_scores else None
        ),
        "all_native_editable": all(row["flattened_slide"] is False for row in rows if row["status"] != "failed"),
        "rows": rows,
        "results": results,
    }
    (args.results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (args.results_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["id", "status"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({key: summary[key] for key in summary if key not in {"rows", "results"}}, indent=2))
    return 0 if summary["success_count"] == summary["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
