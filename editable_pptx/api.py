from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .job_store import JobStore, JobStoreError, sha256_file
from .openai_responses import resolve_api_key
from .powerpoint import available_powerpoint_adapter
from .service_config import ServiceSettings
from .service_models import (
    CapabilityReport,
    ArtifactKind,
    ArtifactRecord,
    JobOperation,
    JobResource,
    JobStatus,
    TERMINAL_JOB_STATUSES,
    TemplateFamilyMergeRequest,
    TemplateFamilyResource,
    TemplateFamilySplitRequest,
    TemplateFamilyUpdate,
    TemplateResource,
)
from .template_catalog import TemplateCatalog, TemplateCatalogError
from .version import __version__
from .worker import Worker


def create_app(
    settings: ServiceSettings | None = None,
    *,
    start_embedded_worker: bool = False,
) -> FastAPI:
    settings = settings or ServiceSettings()
    settings.validate_network_binding()
    store = JobStore(settings.home)
    app = FastAPI(
        title="Editable PPTX Service",
        version=__version__,
        description="Asynchronous native-editable PowerPoint reconstruction and editing API.",
    )
    app.state.settings = settings
    app.state.store = store
    catalog = TemplateCatalog(settings.template_catalog_home)
    app.state.catalog = catalog
    app.state.worker = None

    bearer_token = os.environ.get("EDITABLE_PPTX_BEARER_TOKEN", "").strip()
    if bearer_token:
        @app.middleware("http")
        async def require_bearer(request: Request, call_next):
            if request.url.path in {"/v1/health", "/v1/version", "/docs", "/openapi.json"}:
                return await call_next(request)
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {bearer_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(status_code=401, content={"detail": "invalid bearer token"})
            return await call_next(request)

    if start_embedded_worker:
        worker = Worker(
            store,
            model=settings.model,
            poll_interval=settings.poll_interval_seconds,
            artifact_ttl_days=settings.artifact_ttl_days,
            slide_concurrency=settings.slide_concurrency,
            template_match_threshold=settings.template_match_threshold,
            default_max_cost_usd=settings.default_max_cost_usd,
            default_timeout_seconds=settings.default_timeout_seconds,
        )
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        app.state.worker = worker
        app.state.worker_thread = thread

        @app.on_event("shutdown")
        def stop_worker() -> None:
            worker.stop()
            thread.join(timeout=5)

    def job_or_404(job_id: str) -> JobResource:
        try:
            return store.get_job(job_id)
        except JobStoreError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    async def create_upload_job(
        operation: JobOperation,
        files: list[UploadFile],
        *,
        mode: str,
        request_payload: dict,
    ) -> JobResource:
        if mode not in {"plan", "apply"}:
            raise HTTPException(status_code=422, detail="mode must be plan or apply")
        if not files:
            raise HTTPException(status_code=422, detail="at least one file is required")
        if len(files) > settings.max_images_per_job:
            raise HTTPException(status_code=413, detail="too many files in one job")
        job = store.create_job(operation, request_payload, mode=mode)
        artifact_ids: list[str] = []
        try:
            artifact_ids = await save_uploads(job.id, files)
        except Exception:
            store.update_job(job.id, status=JobStatus.FAILED, stage="upload_failed")
            raise
        request_payload = {**request_payload, "input_artifact_ids": artifact_ids}
        return store.replace_request(job.id, request_payload)

    async def save_uploads(job_id: str, files: list[UploadFile]) -> list[str]:
        artifact_ids: list[str] = []
        existing = len(store.list_artifacts(job_id))
        for index, upload in enumerate(files, start=existing + 1):
            original = upload.filename or f"upload-{index}"
            if Path(original).suffix.lower() == ".pptm":
                raise HTTPException(status_code=415, detail="PPTM is not supported")
            safe_name = f"{index:03d}-{Path(original).name}"
            target = store.job_dir(job_id) / "incoming" / safe_name
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload exceeds {settings.max_upload_bytes} bytes",
                        )
                    handle.write(chunk)
            artifact = store.register_artifact(
                job_id,
                target,
                kind=ArtifactKind.INPUT,
                name=safe_name,
                media_type=upload.content_type,
            )
            artifact_ids.append(artifact.id)
        return artifact_ids

    async def create_artifact_job(
        operation: JobOperation,
        artifact_id: str,
        *,
        mode: str,
        request_payload: dict,
    ) -> JobResource:
        try:
            store.get_artifact(artifact_id)
        except JobStoreError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return store.create_job(
            operation,
            {**request_payload, "source_artifact_id": artifact_id},
            mode=mode,
        )

    @app.get("/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/version")
    def version() -> dict[str, str]:
        return {"version": __version__}

    @app.get("/v1/capabilities", response_model=CapabilityReport)
    def capabilities() -> CapabilityReport:
        return CapabilityReport(
            python=sys.version.split()[0],
            node=shutil.which("node"),
            libreoffice=shutil.which("soffice") or shutil.which("libreoffice"),
            pdftoppm=shutil.which("pdftoppm"),
            powerpoint_adapter=available_powerpoint_adapter(),
            openai_key_configured=bool(resolve_api_key()),
            supported_input_formats=["pptx", "png", "jpeg", "webp", "svg"],
            operations=list(JobOperation),
        )

    @app.post("/v1/image-to-editable", response_model=JobResource, status_code=202)
    async def image_to_editable(
        files: Annotated[list[UploadFile], File()],
        mode: Annotated[str, Form()] = "apply",
        iterations: Annotated[int, Form(ge=0, le=5)] = 2,
        parallel_slides: Annotated[int, Form(ge=1, le=8)] = 4,
        raster_policy: Annotated[str, Form()] = "photos-only",
        font_policy: Annotated[str, Form()] = "portable",
    ) -> JobResource:
        return await create_upload_job(
            JobOperation.IMAGE_TO_EDITABLE,
            files,
            mode=mode,
            request_payload={
                "iterations": iterations,
                "parallel_slides": parallel_slides,
                "raster_policy": raster_policy,
                "font_policy": font_policy,
            },
        )

    @app.post("/v1/figure-to-editable", response_model=JobResource, status_code=202)
    async def figure_to_editable(
        file: Annotated[UploadFile, File()],
        mode: Annotated[str, Form()] = "apply",
        canvas_width: Annotated[int, Form(gt=0)] = 1280,
        canvas_height: Annotated[int, Form(gt=0)] = 720,
        iterations: Annotated[int, Form(ge=0, le=5)] = 2,
        refine: Annotated[str, Form(pattern="^(none|llm|optimize)$")] = "none",
    ) -> JobResource:
        return await create_upload_job(
            JobOperation.FIGURE_TO_EDITABLE,
            [file],
            mode=mode,
            request_payload={
                "canvas": [canvas_width, canvas_height],
                "iterations": iterations,
                "refine": refine,
            },
        )

    @app.post("/v1/render", response_model=JobResource, status_code=202)
    async def render(
        file: Annotated[UploadFile | None, File()] = None,
        source_artifact_id: Annotated[str | None, Form()] = None,
    ) -> JobResource:
        if bool(file) == bool(source_artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one file or source_artifact_id")
        if source_artifact_id:
            return await create_artifact_job(JobOperation.RENDER, source_artifact_id, mode="apply", request_payload={})
        assert file is not None
        return await create_upload_job(JobOperation.RENDER, [file], mode="apply", request_payload={})

    @app.post("/v1/validate", response_model=JobResource, status_code=202)
    async def validate(
        file: Annotated[UploadFile | None, File()] = None,
        source_artifact_id: Annotated[str | None, Form()] = None,
        font_policy: Annotated[str, Form()] = "portable",
    ) -> JobResource:
        if bool(file) == bool(source_artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one file or source_artifact_id")
        payload = {"font_policy": font_policy, "powerpoint_validation": "auto"}
        if source_artifact_id:
            return await create_artifact_job(JobOperation.VALIDATE, source_artifact_id, mode="apply", request_payload=payload)
        assert file is not None
        return await create_upload_job(JobOperation.VALIDATE, [file], mode="apply", request_payload=payload)

    @app.post("/v1/notes", response_model=JobResource, status_code=202)
    async def notes(
        file: Annotated[UploadFile | None, File()] = None,
        source_artifact_id: Annotated[str | None, Form()] = None,
        instruction: Annotated[str | None, Form()] = None,
        mode: Annotated[str, Form()] = "apply",
        slides: Annotated[str, Form()] = "all",
        max_attempts: Annotated[int, Form(ge=1, le=5)] = 3,
        minimum_font_size_pt: Annotated[float, Form(gt=0)] = 7.5,
    ) -> JobResource:
        if bool(file) == bool(source_artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one file or source_artifact_id")
        payload = {
            "instruction": instruction,
            "cleanup_notes": True,
            "slides": _parse_slides(slides),
            "max_attempts": max_attempts,
            "minimum_font_size_pt": minimum_font_size_pt,
        }
        if source_artifact_id:
            return await create_artifact_job(JobOperation.NOTES, source_artifact_id, mode=mode, request_payload=payload)
        assert file is not None
        return await create_upload_job(JobOperation.NOTES, [file], mode=mode, request_payload=payload)

    @app.post("/v1/beautify", response_model=JobResource, status_code=202)
    async def beautify(
        file: Annotated[UploadFile | None, File()] = None,
        source_artifact_id: Annotated[str | None, Form()] = None,
        template: Annotated[UploadFile | None, File()] = None,
        target_images: Annotated[list[UploadFile] | None, File()] = None,
        instruction: Annotated[str | None, Form()] = None,
        mode: Annotated[str, Form()] = "apply",
        slides: Annotated[str, Form()] = "all",
        max_attempts: Annotated[int, Form(ge=1, le=5)] = 3,
        max_candidates: Annotated[int | None, Form(ge=1, le=3)] = None,
        minimum_font_size_pt: Annotated[float | None, Form(gt=0)] = None,
        body_min_font_size_pt: Annotated[float, Form(ge=8)] = 8.0,
        source_min_font_size_pt: Annotated[float, Form(ge=6)] = 6.0,
        restyle_mode: Annotated[
            str, Form(pattern="^(auto|conservative|structural|rebuild)$")
        ] = "auto",
        catalog_enabled: Annotated[bool, Form()] = True,
        max_cost_usd: Annotated[float | None, Form(gt=0)] = None,
        timeout_seconds: Annotated[int | None, Form(ge=60, le=7200)] = None,
        trace_level: Annotated[str, Form(pattern="^(none|metadata|full)$")] = "metadata",
        powerpoint_validation: Annotated[
            str, Form(pattern="^(off|auto|required)$")
        ] = "auto",
    ) -> JobResource:
        if bool(file) == bool(source_artifact_id):
            raise HTTPException(status_code=422, detail="provide exactly one file or source_artifact_id")
        resolved_candidates = max_candidates if max_candidates is not None else min(max_attempts, 3)
        resolved_body_minimum = (
            minimum_font_size_pt
            if minimum_font_size_pt is not None
            else body_min_font_size_pt
        )
        payload = {
            "instruction": instruction,
            "content_invariant": True,
            "slides": _parse_slides(slides),
            "max_attempts": max_attempts,
            "max_candidates": resolved_candidates,
            "legacy_max_attempts_clamped": max_candidates is None and max_attempts > 3,
            "minimum_font_size_pt": resolved_body_minimum,
            "body_min_font_size_pt": resolved_body_minimum,
            "source_min_font_size_pt": source_min_font_size_pt,
            "restyle_mode": restyle_mode,
            "catalog_enabled": catalog_enabled,
            "max_cost_usd": max_cost_usd or settings.default_max_cost_usd,
            "timeout_seconds": timeout_seconds or settings.default_timeout_seconds,
            "trace_level": trace_level,
            "powerpoint_validation": powerpoint_validation,
        }

        def finalize_request(job: JobResource) -> JobResource:
            if payload["legacy_max_attempts_clamped"]:
                store.add_event(
                    job.id,
                    "request.warning",
                    {
                        "code": "max_attempts_clamped",
                        "message": "legacy max_attempts was capped at three candidates",
                        "requested": max_attempts,
                        "effective": resolved_candidates,
                    },
                )
            return job

        auxiliary = [*([template] if template else []), *(target_images or [])]
        if source_artifact_id:
            job = await create_artifact_job(JobOperation.BEAUTIFY, source_artifact_id, mode=mode, request_payload=payload)
            auxiliary_ids = await save_uploads(job.id, auxiliary) if auxiliary else []
            request_payload = dict(job.request)
            if template:
                request_payload["template_artifact_id"] = auxiliary_ids[0]
                auxiliary_ids = auxiliary_ids[1:]
            request_payload["target_artifact_ids"] = auxiliary_ids
            return finalize_request(store.replace_request(job.id, request_payload))
        assert file is not None
        uploads = [file, *auxiliary]
        result = await create_upload_job(JobOperation.BEAUTIFY, uploads, mode=mode, request_payload=payload)
        request_payload = dict(result.request)
        ids = list(request_payload["input_artifact_ids"])
        request_payload["input_artifact_ids"] = ids[:1]
        if template:
            request_payload["template_artifact_id"] = ids[1]
            ids = [ids[0], *ids[2:]]
        request_payload["target_artifact_ids"] = ids[1:]
        return finalize_request(store.replace_request(result.id, request_payload))

    @app.post("/v1/templates/import", response_model=JobResource, status_code=202)
    async def import_template(
        file: Annotated[UploadFile, File()],
        name: Annotated[str | None, Form()] = None,
        timeout_seconds: Annotated[int, Form(ge=60, le=7200)] = 900,
    ) -> JobResource:
        if Path(file.filename or "").suffix.lower() != ".pptx":
            raise HTTPException(status_code=415, detail="template import accepts PPTX only")
        return await create_upload_job(
            JobOperation.TEMPLATE_IMPORT,
            [file],
            mode="apply",
            request_payload={"name": name, "timeout_seconds": timeout_seconds},
        )

    @app.get("/v1/templates", response_model=list[TemplateResource])
    def list_templates(include_deleted: bool = False) -> list[TemplateResource]:
        return catalog.list_templates(include_deleted=include_deleted)

    @app.get("/v1/templates/{template_id}", response_model=TemplateResource)
    def get_template(template_id: str) -> TemplateResource:
        try:
            return catalog.get_template(template_id)
        except TemplateCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/v1/templates/{template_id}", response_model=TemplateResource)
    def delete_template(template_id: str) -> TemplateResource:
        try:
            return catalog.soft_delete(template_id)
        except TemplateCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/templates/{template_id}/restore", response_model=TemplateResource)
    def restore_template(template_id: str) -> TemplateResource:
        try:
            return catalog.restore(template_id)
        except TemplateCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/templates/reindex", response_model=JobResource, status_code=202)
    def reindex_templates() -> JobResource:
        return store.create_job(
            JobOperation.TEMPLATE_REINDEX,
            {"index_mode": "incremental"},
            mode="apply",
        )

    @app.get("/v1/template-families", response_model=list[TemplateFamilyResource])
    def list_template_families() -> list[TemplateFamilyResource]:
        return catalog.list_families()

    @app.patch(
        "/v1/template-families/{family_id}",
        response_model=TemplateFamilyResource,
    )
    def update_template_family(
        family_id: str,
        payload: TemplateFamilyUpdate,
    ) -> TemplateFamilyResource:
        try:
            return catalog.update_family(
                family_id,
                name=payload.name,
                active=payload.active,
            )
        except TemplateCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/v1/template-families/merge",
        response_model=TemplateFamilyResource,
    )
    def merge_template_families(
        payload: TemplateFamilyMergeRequest,
    ) -> TemplateFamilyResource:
        try:
            return catalog.merge_families(
                payload.target_family_id,
                payload.source_family_ids,
            )
        except (TemplateCatalogError, StopIteration) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/v1/template-families/{family_id}/split",
        response_model=TemplateFamilyResource,
    )
    def split_template_family(
        family_id: str,
        payload: TemplateFamilySplitRequest,
    ) -> TemplateFamilyResource:
        try:
            return catalog.split_family(family_id, payload.template_ids, payload.name)
        except (TemplateCatalogError, StopIteration) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/v1/jobs", response_model=list[JobResource])
    def list_jobs() -> list[JobResource]:
        return store.list_jobs()

    @app.get("/v1/jobs/{job_id}", response_model=JobResource)
    def get_job(job_id: str) -> JobResource:
        return job_or_404(job_id)

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobResource, status_code=202)
    def cancel(job_id: str) -> JobResource:
        job_or_404(job_id)
        return store.request_cancel(job_id)

    @app.post("/v1/jobs/{job_id}/retry", response_model=JobResource, status_code=202)
    def retry(job_id: str) -> JobResource:
        job_or_404(job_id)
        return store.retry_job(job_id)

    @app.get("/v1/jobs/{job_id}/artifacts")
    def artifacts(job_id: str) -> list[dict]:
        job_or_404(job_id)
        return [item.model_dump(mode="json") for item in store.list_artifacts(job_id)]

    @app.get("/v1/jobs/{job_id}/candidates")
    def candidates(job_id: str) -> list[dict]:
        job_or_404(job_id)
        return [
            item.model_dump(mode="json")
            for item in store.list_artifacts(job_id)
            if item.metadata.get("candidate_index") is not None
        ]

    @app.get("/v1/jobs/{job_id}/trace")
    def trace(job_id: str) -> dict:
        job = job_or_404(job_id)
        if job.request.get("trace_level") == "none":
            return {"job_id": job_id, "status": "disabled", "events": []}
        trace_artifact = next(
            (
                item
                for item in store.list_artifacts(job_id)
                if item.kind == ArtifactKind.TRACE
            ),
            None,
        )
        if trace_artifact is None:
            return {
                "job_id": job_id,
                "status": "pending",
                "events": [
                    item.model_dump(mode="json")
                    for item in store.events_after(job_id)
                    if item.event_type.startswith("agent.")
                ],
            }
        return json.loads(store.artifact_path(trace_artifact.id).read_text(encoding="utf-8"))

    @app.post(
        "/v1/jobs/{job_id}/bundle",
        response_model=ArtifactRecord,
        status_code=201,
    )
    def bundle(job_id: str) -> ArtifactRecord:
        job = job_or_404(job_id)
        for artifact in job.artifacts:
            if artifact.kind == ArtifactKind.BUNDLE:
                return artifact
        target = store.job_dir(job_id) / "artifacts" / "job-bundle.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        artifacts = [item for item in job.artifacts if item.kind != ArtifactKind.BUNDLE]
        manifest = {
            "job": job.model_dump(mode="json", exclude={"artifacts"}),
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            for artifact in artifacts:
                archive.write(
                    store.artifact_path(artifact.id),
                    f"artifacts/{artifact.name}",
                )
        return store.register_artifact(
            job_id,
            target,
            kind=ArtifactKind.BUNDLE,
            media_type="application/zip",
        )

    @app.get("/v1/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
        job_or_404(job_id)

        async def stream() -> AsyncIterator[str]:
            sequence = after
            while True:
                if await request.is_disconnected():
                    return
                pending = store.events_after(job_id, sequence)
                for event in pending:
                    sequence = event.sequence
                    yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
                job = store.get_job(job_id)
                if job.status in TERMINAL_JOB_STATUSES and not pending:
                    return
                await asyncio.sleep(settings.poll_interval_seconds)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/v1/artifacts/{artifact_id}")
    def download(artifact_id: str) -> FileResponse:
        try:
            artifact = store.get_artifact(artifact_id)
            path = store.artifact_path(artifact_id)
        except JobStoreError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path, media_type=artifact.media_type, filename=artifact.name)

    @app.post("/v1/plans/{plan_id}/apply", response_model=JobResource, status_code=202)
    def apply_plan(plan_id: str) -> JobResource:
        try:
            plan = store.get_plan(plan_id)
            source_job = store.get_job(plan.job_id)
            source_ids = source_job.request.get("input_artifact_ids") or []
            if not source_ids and source_job.request.get("source_artifact_id"):
                source_ids = [source_job.request["source_artifact_id"]]
            if not source_ids:
                raise JobStoreError("plan source artifact is missing")
            if sha256_file(store.artifact_path(source_ids[0])) != plan.source_sha256:
                raise JobStoreError("plan source checksum no longer matches")
            job = store.create_job(
                plan.operation,
                {**source_job.request, "plan_artifact_id": plan.artifact_id},
                mode="apply",
                source_job_id=source_job.id,
                parent_job_id=source_job.id,
                plan_id=plan.id,
            )
            store.mark_plan_applied(plan.id, job.id)
            return job
        except JobStoreError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


def _parse_slides(value: str) -> dict[str, object]:
    normalized = value.strip().lower()
    if normalized in {"", "all", "*"}:
        return {"all": True, "indices": []}
    try:
        indices = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as error:
        raise HTTPException(status_code=422, detail="slides must be 'all' or comma-separated 1-based indices") from error
    if not indices or indices[0] < 1:
        raise HTTPException(status_code=422, detail="slide indices are 1-based")
    return {"all": False, "indices": indices}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="editable-pptx-api")
    parser.add_argument("--home")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--with-worker", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = ServiceSettings()
    if args.home:
        settings.home = Path(args.home).expanduser()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    settings.validate_network_binding()
    import uvicorn

    uvicorn.run(
        create_app(settings, start_embedded_worker=args.with_worker),
        host=settings.host,
        port=settings.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
