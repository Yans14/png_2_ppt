from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time
import traceback
import uuid

from .job_store import JobStore
from .service_config import ServiceSettings
from .service_models import ArtifactKind, JobStatus
from .service_ops import (
    OperationCancelled,
    OperationExecutor,
    OperationQualityError,
    OperationResult,
)


class Worker:
    def __init__(
        self,
        store: JobStore,
        *,
        model: str = "gpt-5.5",
        worker_id: str | None = None,
        poll_interval: float = 0.5,
        artifact_ttl_days: int = 7,
    ) -> None:
        self.store = store
        self.executor = OperationExecutor(store, model=model)
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.artifact_ttl_days = artifact_ttl_days
        self._next_cleanup_at = 0.0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_interval)

    def run_once(self) -> bool:
        now = time.time()
        if now >= self._next_cleanup_at:
            self.store.cleanup(
                older_than_epoch=now - max(1, self.artifact_ttl_days) * 86400
            )
            self._next_cleanup_at = now + 3600
        job = self.store.claim_next(self.worker_id)
        if job is None:
            return False

        def progress(value: float, stage: str, payload: dict | None) -> None:
            self.store.update_job(job.id, progress=value, stage=stage)
            if payload:
                self.store.add_event(job.id, "job.progress", payload)

        try:
            result = self.executor.execute(job, progress)
            path_to_id = self._register_result(job.id, result)
            if result.plan_path is not None:
                plan_artifact_id = path_to_id[str(result.plan_path.resolve())]
                self.store.create_plan(
                    job.id,
                    job.operation,
                    result.source_sha256 or "",
                    plan_artifact_id,
                )
            best_id = (
                path_to_id.get(str(result.best_path.resolve()))
                if result.best_path is not None
                else None
            )
            self.store.update_job(
                job.id,
                status=JobStatus.SUCCEEDED,
                progress=1,
                stage="completed",
                best_artifact_id=best_id,
            )
        except OperationQualityError as error:
            path_to_id = self._register_result(job.id, error.result)
            best_id = (
                path_to_id.get(str(error.result.best_path.resolve()))
                if error.result.best_path is not None
                else None
            )
            self.store.update_job(
                job.id,
                status=JobStatus.FAILED,
                stage="quality_gate_failed",
                best_artifact_id=best_id,
                error={"type": type(error).__name__, "message": str(error)},
            )
        except OperationCancelled as error:
            self.store.update_job(
                job.id,
                status=JobStatus.CANCELLED,
                stage="cancelled",
                error={"type": type(error).__name__, "message": str(error)},
            )
        except Exception as error:  # noqa: BLE001 - worker boundary must persist failure details
            self.store.add_event(
                job.id,
                "job.exception",
                {"type": type(error).__name__, "message": str(error)},
            )
            self.store.update_job(
                job.id,
                status=JobStatus.FAILED,
                stage="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(limit=12),
                },
            )
        return True

    def _register_result(self, job_id: str, result: OperationResult) -> dict[str, str]:
        path_to_id: dict[str, str] = {}
        for produced in result.artifacts:
            record = self.store.register_artifact(
                job_id,
                produced.path,
                kind=produced.kind,
                name=produced.name,
                parent_artifact_id=produced.parent_artifact_id,
                metadata=produced.metadata,
            )
            path_to_id[str(produced.path.resolve())] = record.id
        return path_to_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="editable-pptx-worker")
    parser.add_argument("--home")
    parser.add_argument("--model", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = ServiceSettings()
    if args.home:
        settings.home = __import__("pathlib").Path(args.home).expanduser()
    worker = Worker(
        JobStore(settings.home),
        model=args.model or settings.model,
        poll_interval=args.poll_interval or settings.poll_interval_seconds,
        artifact_ttl_days=settings.artifact_ttl_days,
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: worker.stop())
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
