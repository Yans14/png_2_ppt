#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageOps

from editable_pptx.api import create_app
from editable_pptx.job_store import JobStore
from editable_pptx.ooxml_edit import extract_shape_graph, extract_text_manifest
from editable_pptx.powerpoint import validate_ooxml
from editable_pptx.qa import audit_pptx, compare_images
from editable_pptx.service_config import ServiceSettings
from editable_pptx.service_models import ArtifactKind, JobResource
from editable_pptx.service_ops import render_all_slides
from editable_pptx.worker import Worker
from scripts.prepare_api_examples import EXAMPLE_ROOT, MANIFEST_PATH, REPO, prepare


ENDPOINT_KEYS = (
    "image_to_editable",
    "figure_to_editable",
    "notes",
    "beautify",
    "render",
    "validate",
)
PAID_ENDPOINTS = {"image_to_editable", "notes", "beautify"}


def _is_external_blocker(error: Any) -> bool:
    if not error:
        return False
    message = json.dumps(error, ensure_ascii=False).lower()
    return any(
        token in message
        for token in (
            "exceeded your current quota",
            "insufficient_quota",
            "billing details",
            "rate limit reached",
        )
    )


def _resolve(value: str) -> Path:
    relative = Path(value)
    if value.startswith(("cache/", "fixtures/")):
        return (EXAMPLE_ROOT / relative).resolve()
    return (REPO / relative).resolve()


def _file_tuple(path: Path) -> tuple[str, bytes, str]:
    media = {
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }.get(path.suffix.lower(), "application/octet-stream")
    return path.name, path.read_bytes(), media


def _run_job(
    client: TestClient,
    worker: Worker,
    endpoint: str,
    *,
    files: list[tuple[str, tuple[str, bytes, str]]],
    data: dict[str, str],
) -> JobResource:
    response = client.post(endpoint, files=files, data=data)
    if response.status_code != 202:
        raise RuntimeError(f"{endpoint} submission failed ({response.status_code}): {response.text}")
    job_id = response.json()["id"]
    if not worker.run_once():
        raise RuntimeError(f"worker did not claim job {job_id}")
    return client.app.state.store.get_job(job_id)


def _copy_artifacts(store: JobStore, job: JobResource, result_dir: Path) -> list[dict[str, Any]]:
    result_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for artifact in job.artifacts:
        source = store.artifact_path(artifact.id)
        target = result_dir / artifact.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        copied.append(
            {
                "id": artifact.id,
                "name": artifact.name,
                "kind": artifact.kind.value,
                "path": str(target),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
        )
    return copied


def _best_pptx(store: JobStore, job: JobResource) -> Path | None:
    if job.best_artifact_id:
        return store.artifact_path(job.best_artifact_id)
    for artifact in job.artifacts:
        if artifact.kind == ArtifactKind.PPTX:
            return store.artifact_path(artifact.id)
    return None


def _artifact_path(store: JobStore, job: JobResource, kind: ArtifactKind) -> Path | None:
    for artifact in job.artifacts:
        if artifact.kind == kind:
            return store.artifact_path(artifact.id)
    return None


def _render_preview(source: Path, output: Path) -> Path:
    rendered = render_all_slides(source, output.parent)
    image = next(path for path in rendered if path.suffix.lower() == ".png")
    if image != output:
        shutil.copy2(image, output)
    return output


def _nonblank(path: Path) -> bool:
    with Image.open(path) as image:
        gray = ImageOps.grayscale(image)
        extrema = gray.getextrema()
        return bool(extrema and extrema[1] - extrema[0] > 8)


def _shape_by_name(path: Path, token: str):
    token = token.lower()
    return next(
        (shape for shape in extract_shape_graph(path) if token in shape.name.lower()),
        None,
    )


def _evaluate_note(source: Path, output: Path, expectation: dict[str, Any]) -> dict[str, Any]:
    kind = expectation["kind"]
    result: dict[str, Any] = {"kind": kind, "fulfilled": False}
    if kind == "move":
        before = _shape_by_name(source, expectation["target"])
        after = _shape_by_name(output, expectation["target"])
        delta = None if not before or not after else (after.y_pt or 0) - (before.y_pt or 0)
        result.update({"observed_dy_pt": delta, "expected_dy_pt": expectation["dy_pt"]})
        result["fulfilled"] = delta is not None and abs(delta - expectation["dy_pt"]) <= 3
    elif kind == "replace_text":
        text = [value for values in extract_text_manifest(output).values() for value in values]
        result.update({"old_present": expectation["old"] in text, "new_present": expectation["new"] in text})
        result["fulfilled"] = expectation["new"] in text and expectation["old"] not in text
    elif kind == "recolor":
        after = _shape_by_name(output, expectation["target"])
        observed = after.fill_color if after else None
        result.update({"observed_color": observed, "expected_color": expectation["color"]})
        result["fulfilled"] = observed == expectation["color"]
    elif kind == "resize":
        after = _shape_by_name(output, expectation["target"])
        observed = after.width_pt if after else None
        result.update({"observed_width_pt": observed, "expected_width_pt": expectation["width_pt"]})
        result["fulfilled"] = observed is not None and abs(observed - expectation["width_pt"]) <= 3
    elif kind == "duplicate":
        text = [value for values in extract_text_manifest(output).values() for value in values]
        count = sum(value == expectation["new_text"] for value in text)
        result.update({"new_text_count": count})
        result["fulfilled"] = count >= 1
    all_text = [value for values in extract_text_manifest(output).values() for value in values]
    result["production_note_removed"] = not any("Please " in value for value in all_text)
    result["fulfilled"] = bool(result["fulfilled"] and result["production_note_removed"])
    return result


def _make_montage(paths: list[Path], output: Path, *, width: int = 480) -> Path | None:
    valid = [path for path in paths if path.exists()]
    if not valid:
        return None
    thumbs = []
    for path in valid:
        with Image.open(path) as image:
            image = image.convert("RGB")
            height = max(1, round(image.height * width / image.width))
            thumbs.append(image.resize((width, height), Image.Resampling.LANCZOS))
    gap = 16
    canvas = Image.new("RGB", (width, sum(item.height for item in thumbs) + gap * (len(thumbs) - 1)), "#DDDDDD")
    y = 0
    for image in thumbs:
        canvas.paste(image, (0, y))
        y += image.height + gap
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def _quality_image(target: Path, pptx: Path, result_dir: Path) -> dict[str, Any]:
    preview = _render_preview(pptx, result_dir / "output.png")
    metrics = compare_images(target, preview)
    audit = audit_pptx(pptx)
    ooxml = validate_ooxml(pptx)
    passed = bool(
        ooxml["compatible"]
        and not audit["flattened_slide"]
        and audit["native_shape_objects"] + audit["native_text_runs"] > 0
        and metrics["similarity_score"] >= 0.62
    )
    return {
        "passed": passed,
        "similarity_score": metrics["similarity_score"],
        "structural_score": metrics["structural_score"],
        "foreground_color_similarity": metrics["foreground_color_similarity"],
        "native_shapes": audit["native_shape_objects"],
        "native_text_runs": audit["native_text_runs"],
        "pictures": audit["picture_objects"],
        "flattened": audit["flattened_slide"],
        "ooxml_compatible": ooxml["compatible"],
        "output_preview": str(preview),
    }


def _process_case(
    endpoint: str,
    case: dict[str, Any],
    *,
    client: TestClient,
    worker: Worker,
    store: JobStore,
    result_root: Path,
    max_attempts: int,
) -> dict[str, Any]:
    case_id = case["id"]
    result_dir = result_root / endpoint / case_id
    result_dir.mkdir(parents=True, exist_ok=True)
    source = _resolve(case["input"])
    started = time.time()
    if endpoint == "image_to_editable":
        job = _run_job(
            client,
            worker,
            "/v1/image-to-editable",
            files=[("files", _file_tuple(source))],
            data={"mode": "apply", "iterations": "0", "parallel_slides": "1", "raster_policy": "photos-only"},
        )
    elif endpoint == "figure_to_editable":
        job = _run_job(
            client,
            worker,
            "/v1/figure-to-editable",
            files=[("file", _file_tuple(source))],
            data={"mode": "apply", "iterations": "0", "canvas_width": "960", "canvas_height": "540"},
        )
    elif endpoint == "notes":
        job = _run_job(
            client,
            worker,
            "/v1/notes",
            files=[("file", _file_tuple(source))],
            data={"mode": "apply", "slides": "1", "max_attempts": str(max_attempts), "minimum_font_size_pt": "7.5"},
        )
    elif endpoint == "beautify":
        target = _resolve(case["target"])
        job = _run_job(
            client,
            worker,
            "/v1/beautify",
            files=[("file", _file_tuple(source)), ("target_images", _file_tuple(target))],
            data={
                "mode": "apply",
                "slides": "1",
                "max_attempts": str(max_attempts),
                "minimum_font_size_pt": "7.5",
                "instruction": "Improve alignment, spacing, hierarchy, and color fidelity toward the attached target while preserving every business fact and native object.",
            },
        )
    elif endpoint == "render":
        job = _run_job(client, worker, "/v1/render", files=[("file", _file_tuple(source))], data={})
    elif endpoint == "validate":
        job = _run_job(client, worker, "/v1/validate", files=[("file", _file_tuple(source))], data={"font_policy": "portable"})
    else:
        raise ValueError(endpoint)

    artifacts = _copy_artifacts(store, job, result_dir)
    record: dict[str, Any] = {
        "endpoint": endpoint,
        "case_id": case_id,
        "category": case.get("category"),
        "status": job.status.value,
        "stage": job.stage,
        "duration_seconds": round(time.time() - started, 2),
        "job_id": job.id,
        "best_artifact_id": job.best_artifact_id,
        "error": job.error,
        "artifacts": artifacts,
        "input": str(source),
    }
    if _is_external_blocker(job.error):
        record["quality"] = {
            "passed": None,
            "evaluation_status": "blocked_external",
            "reason": "OpenAI quota or rate limit prevented quality evaluation",
        }
        return record
    pptx = _best_pptx(store, job)
    if endpoint == "image_to_editable" and pptx:
        record["quality"] = _quality_image(source, pptx, result_dir)
        record["input_preview"] = str(source)
    elif endpoint == "figure_to_editable" and pptx:
        report_path = _artifact_path(store, job, ArtifactKind.REPORT)
        report = json.loads(report_path.read_text()) if report_path else {}
        preview = _artifact_path(store, job, ArtifactKind.PREVIEW)
        audit = report.get("audit", {})
        contract = report.get("editable_contract", {})
        record["quality"] = {
            "passed": bool(
                job.status.value == "succeeded"
                and audit.get("native_shape_objects", 0) > 0
                and not audit.get("flattened_slide", True)
                and contract.get("no_embedded_raster")
                and preview
                and _nonblank(preview)
            ),
            "native_shapes": audit.get("native_shape_objects"),
            "custom_geometry_paths": audit.get("custom_geometry_paths"),
            "cubic_bezier_segments": audit.get("cubic_bezier_segments"),
            "warnings": report.get("warnings", []),
            "no_embedded_raster": contract.get("no_embedded_raster"),
            "output_preview": str(result_dir / preview.name) if preview else None,
        }
    elif endpoint == "notes" and pptx:
        output_preview = _render_preview(pptx, result_dir / "output.png")
        input_preview = _render_preview(source, result_dir / "input.png")
        expectation = _evaluate_note(source, pptx, case["expectation"])
        report_path = _artifact_path(store, job, ArtifactKind.REPORT)
        report = json.loads(report_path.read_text()) if report_path else {}
        deterministic = report.get("attempts", [{}])[-1].get("deterministic", {}) if report.get("attempts") else {}
        record["quality"] = {
            "passed": bool(expectation["fulfilled"] and deterministic.get("ooxml_compatible") and job.status.value == "succeeded"),
            "expectation": expectation,
            "review_approved": report.get("approved"),
            "review_score": report.get("best_score"),
            "content_preserved": deterministic.get("content_preserved"),
            "ooxml_compatible": deterministic.get("ooxml_compatible"),
            "input_preview": str(input_preview),
            "output_preview": str(output_preview),
        }
    elif endpoint == "beautify" and pptx:
        target = _resolve(case["target"])
        input_preview = _render_preview(source, result_dir / "input.png")
        output_preview = _render_preview(pptx, result_dir / "output.png")
        before = compare_images(target, input_preview)
        after = compare_images(target, output_preview)
        report_path = _artifact_path(store, job, ArtifactKind.REPORT)
        report = json.loads(report_path.read_text()) if report_path else {}
        attempts = report.get("attempts", [])
        deterministic = attempts[-1].get("deterministic", {}) if attempts else {}
        gain = after["similarity_score"] - before["similarity_score"]
        record["quality"] = {
            "passed": bool(
                deterministic.get("content_preserved")
                and deterministic.get("ooxml_compatible")
                and after["similarity_score"] >= 0.62
                and gain >= -0.01
            ),
            "review_approved": report.get("approved"),
            "review_score": report.get("best_score"),
            "before_similarity": before["similarity_score"],
            "after_similarity": after["similarity_score"],
            "similarity_gain": gain,
            "content_preserved": deterministic.get("content_preserved"),
            "ooxml_compatible": deterministic.get("ooxml_compatible"),
            "input_preview": str(input_preview),
            "target_preview": str(target),
            "output_preview": str(output_preview),
        }
    elif endpoint == "render":
        previews = [store.artifact_path(item.id) for item in job.artifacts if item.kind == ArtifactKind.PREVIEW]
        copied_previews = [result_dir / item.name for item in job.artifacts if item.kind == ArtifactKind.PREVIEW]
        record["quality"] = {
            "passed": bool(job.status.value == "succeeded" and previews and all(_nonblank(path) for path in previews)),
            "preview_count": len(previews),
            "all_nonblank": bool(previews and all(_nonblank(path) for path in previews)),
            "output_preview": str(copied_previews[0]) if copied_previews else None,
        }
    elif endpoint == "validate":
        expected_valid = case["expected"] == "valid"
        report_path = _artifact_path(store, job, ArtifactKind.REPORT)
        report = json.loads(report_path.read_text()) if report_path else {}
        detected_valid = job.status.value == "succeeded" and bool(report.get("ooxml", {}).get("compatible"))
        record["quality"] = {
            "passed": detected_valid == expected_valid,
            "expected_valid": expected_valid,
            "detected_valid": detected_valid,
            "ooxml_errors": report.get("ooxml", {}).get("errors", []) if report else (job.error or {}).get("message"),
        }
    else:
        record["quality"] = {"passed": False, "reason": "no output candidate"}
    return record


def _write_summary(root: Path, records: list[dict[str, Any]]) -> None:
    for record in records:
        if _is_external_blocker(record.get("error")):
            record["quality"] = {
                "passed": None,
                "evaluation_status": "blocked_external",
                "reason": "OpenAI quota or rate limit prevented quality evaluation",
            }
    aggregate: dict[str, Any] = {}
    for endpoint in ENDPOINT_KEYS:
        cases = [record for record in records if record["endpoint"] == endpoint]
        evaluated = [
            case for case in cases
            if case.get("quality", {}).get("passed") is not None
        ]
        passed = sum(case.get("quality", {}).get("passed") is True for case in evaluated)
        aggregate[endpoint] = {
            "cases": len(cases),
            "evaluated": len(evaluated),
            "blocked_external": len(cases) - len(evaluated),
            "passed": passed,
            "pass_rate": round(
                passed / len(evaluated), 4
            ) if evaluated else None,
        }
    payload = {
        "version": 1,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "aggregate": aggregate,
        "records": records,
    }
    (root / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["endpoint", "case_id", "status", "passed", "duration_seconds"])
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "endpoint": record["endpoint"],
                    "case_id": record["case_id"],
                    "status": record["status"],
                    "passed": record.get("quality", {}).get("passed"),
                    "duration_seconds": record["duration_seconds"],
                }
            )
    for endpoint in ENDPOINT_KEYS:
        endpoint_records = [record for record in records if record["endpoint"] == endpoint]
        inputs = []
        outputs = []
        for record in endpoint_records:
            quality = record.get("quality", {})
            for key in ("input_preview", "input"):
                if quality.get(key) or record.get(key):
                    candidate = Path(quality.get(key) or record.get(key))
                    if candidate.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                        inputs.append(candidate)
                    break
            if quality.get("output_preview"):
                outputs.append(Path(quality["output_preview"]))
        _make_montage(inputs, root / "montages" / f"{endpoint}-inputs.png")
        _make_montage(outputs, root / "montages" / f"{endpoint}-outputs.png")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="out/api-endpoint-benchmark")
    parser.add_argument("--endpoints", default=",".join(ENDPOINT_KEYS))
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--paid", action="store_true", help="Allow endpoints that call the OpenAI API")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    endpoints = [value.strip() for value in args.endpoints.split(",") if value.strip()]
    unknown = set(endpoints) - set(ENDPOINT_KEYS)
    if unknown:
        parser.error("unknown endpoints: " + ", ".join(sorted(unknown)))
    if set(endpoints) & PAID_ENDPOINTS and not args.paid:
        parser.error("--paid is required for image_to_editable, notes, or beautify")
    prepare(offline=args.offline)
    output = (REPO / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    summary_path = output / "summary.json"
    records: list[dict[str, Any]] = []
    if args.resume and summary_path.exists():
        records = json.loads(summary_path.read_text(encoding="utf-8")).get("records", [])
        # Successful cases are cached; failed cases in the requested endpoints are
        # deliberately removed so a service fix can be verified in place.
        records = [
            record
            for record in records
            if record["endpoint"] not in endpoints
            or bool(record.get("quality", {}).get("passed"))
        ]
    existing = {(record["endpoint"], record["case_id"]) for record in records}
    settings = ServiceSettings(home=output / "service", model=args.model)
    app = create_app(settings)
    client = TestClient(app)
    store: JobStore = app.state.store
    worker = Worker(store, model=args.model, artifact_ttl_days=settings.artifact_ttl_days)
    for endpoint in endpoints:
        for index, case in enumerate(manifest[endpoint], start=1):
            if (endpoint, case["id"]) in existing:
                continue
            print(f"[{endpoint}] {index}/5 {case['id']}", flush=True)
            try:
                record = _process_case(
                    endpoint,
                    case,
                    client=client,
                    worker=worker,
                    store=store,
                    result_root=output / "results",
                    max_attempts=max(1, min(args.max_attempts, 5)),
                )
            except Exception as error:  # benchmark boundary; preserve partial results
                record = {
                    "endpoint": endpoint,
                    "case_id": case["id"],
                    "status": "runner_failed",
                    "duration_seconds": 0,
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "quality": {"passed": False},
                    "input": str(_resolve(case["input"])),
                }
            records.append(record)
            _write_summary(output, records)
            print(
                json.dumps(
                    {
                        "status": record["status"],
                        "passed": record.get("quality", {}).get("passed"),
                        "duration_seconds": record.get("duration_seconds"),
                    }
                ),
                flush=True,
            )
    _write_summary(output, records)
    requested = [record for record in records if record["endpoint"] in endpoints]
    failed = [record for record in requested if record.get("quality", {}).get("passed") is False]
    blocked = [record for record in requested if record.get("quality", {}).get("passed") is None]
    print(
        json.dumps(
            {
                "summary": str(summary_path),
                "cases": len(records),
                "failed": len(failed),
                "blocked_external": len(blocked),
            },
            indent=2,
        )
    )
    return 1 if failed else 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
