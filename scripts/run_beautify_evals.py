#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "failed_quality", "cancelled"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the 20-slide beautify v1 API evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "benchmarks" / "beautify_v1" / "cases.json"
    )
    parser.add_argument(
        "--corpus-dir", type=Path, default=ROOT / "benchmarks" / "cache" / "beautify-v1"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "benchmarks" / "results" / "beautify-v1-summary.json"
    )
    parser.add_argument(
        "--output-decks-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "beautify-v1-decks",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument(
        "--powerpoint-validation",
        choices=("off", "auto", "required"),
        default="auto",
    )
    return parser


def _wait(client: httpx.Client, job_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(1)
    raise TimeoutError(f"job {job_id} exceeded {timeout}s")


def _scorecards(client: httpx.Client, job: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for artifact in job.get("artifacts", []):
        if artifact.get("kind") != "scorecard":
            continue
        response = client.get(artifact["download_url"])
        response.raise_for_status()
        result.append(response.json())
    return result


def _download_best_pptx(
    client: httpx.Client,
    job: dict[str, Any],
    output_dir: Path,
    case_id: str,
) -> Path | None:
    best_id = job.get("best_artifact_id")
    if not best_id:
        return None
    artifact = next(
        (item for item in job.get("artifacts", []) if item.get("id") == best_id),
        None,
    )
    if artifact is None or artifact.get("kind") != "pptx":
        return None
    response = client.get(artifact["download_url"])
    response.raise_for_status()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{case_id}.pptx"
    destination.write_bytes(response.content)
    return destination


def main() -> int:
    args = _parser().parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    missing = [item["id"] for item in cases if not (args.corpus_dir / f"{item['id']}.pptx").is_file()]
    if missing:
        raise SystemExit(
            "Missing generated fixtures: " + ", ".join(missing) + ". Run npm run benchmark:beautify:fixtures."
        )
    results = []
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        for item in cases:
            source = args.corpus_dir / f"{item['id']}.pptx"
            with source.open("rb") as handle:
                response = client.post(
                    "/v1/beautify",
                    files={
                        "file": (
                            source.name,
                            handle,
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        )
                    },
                    data={
                        "mode": "apply",
                        "restyle_mode": "auto",
                        "max_candidates": str(args.max_candidates),
                        "trace_level": "metadata",
                        "powerpoint_validation": args.powerpoint_validation,
                    },
                )
            response.raise_for_status()
            job = _wait(client, response.json()["id"], args.timeout)
            scorecards = _scorecards(client, job)
            valid = [card for card in scorecards if card.get("hard_gate_passed")]
            best = max(valid, key=lambda card: float(card["review"]["score"]), default=None)
            output_pptx = _download_best_pptx(
                client,
                job,
                args.output_decks_dir,
                item["id"],
            )
            results.append(
                {
                    "id": item["id"],
                    "category": item["category"],
                    "job_id": job["id"],
                    "status": job["status"],
                    "hard_gate_passed": bool(best),
                    "reviewer_score": float(best["review"]["score"]) if best else 0.0,
                    "relative_improvement": (
                        best["review"].get("relative_improvement") if best else None
                    ),
                    "accepted": bool(best and best.get("approved")),
                    "output_pptx": str(output_pptx) if output_pptx else None,
                }
            )
    accepted = sum(item["accepted"] for item in results)
    hard_gate_rate = sum(item["hard_gate_passed"] for item in results) / max(1, len(results))
    accepted_scores = [
        float(item["reviewer_score"]) for item in results if item["accepted"]
    ]
    minimum_reviewer_score = min(accepted_scores, default=0.0)
    improvements = [
        float(item["relative_improvement"])
        for item in results
        if item["relative_improvement"] is not None
    ]
    average_improvement = sum(improvements) / max(1, len(improvements))
    acceptance = manifest["acceptance"]
    passed = (
        accepted >= int(acceptance["minimum_accepted"])
        and minimum_reviewer_score >= float(acceptance["minimum_reviewer_score"])
        and hard_gate_rate >= float(acceptance["hard_gate_pass_rate"])
        and average_improvement >= float(acceptance["minimum_average_improvement"])
    )
    summary = {
        "passed": passed,
        "accepted": accepted,
        "total": len(results),
        "hard_gate_rate": round(hard_gate_rate, 4),
        "minimum_reviewer_score": round(minimum_reviewer_score, 4),
        "average_improvement": round(average_improvement, 4),
        "acceptance": acceptance,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
