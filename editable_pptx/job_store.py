from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from .service_models import (
    ArtifactKind,
    ArtifactRecord,
    JobEvent,
    JobOperation,
    JobResource,
    JobStatus,
    PlanResource,
    TERMINAL_JOB_STATUSES,
    utc_now,
)


class JobStoreError(RuntimeError):
    pass


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then always release the handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class JobStore:
    """SQLite metadata plus immutable, checksum-addressed job artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=30,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    source_job_id TEXT,
                    parent_job_id TEXT,
                    plan_id TEXT,
                    best_artifact_id TEXT,
                    worker_id TEXT,
                    error_json TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_artifact_id TEXT,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(job_id, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job_sequence
                    ON events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_job_id TEXT,
                    artifact_id TEXT NOT NULL
                );
                """
            )

    def job_dir(self, job_id: str) -> Path:
        if not job_id or any(character not in "0123456789abcdef-" for character in job_id):
            raise JobStoreError("invalid job id")
        directory = (self.jobs_root / job_id).resolve()
        if self.jobs_root not in directory.parents:
            raise JobStoreError("job path escaped storage root")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def create_job(
        self,
        operation: JobOperation,
        request: dict[str, Any],
        *,
        mode: str = "apply",
        source_job_id: str | None = None,
        parent_job_id: str | None = None,
        plan_id: str | None = None,
    ) -> JobResource:
        if mode not in {"plan", "apply"}:
            raise JobStoreError("job mode must be plan or apply")
        job_id = str(uuid.uuid4())
        timestamp = utc_now()
        self.job_dir(job_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, operation, status, mode, progress, stage, request_json,
                    created_at, updated_at, source_job_id, parent_job_id, plan_id
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    operation.value,
                    JobStatus.QUEUED.value,
                    mode,
                    "queued",
                    json.dumps(request, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    source_job_id,
                    parent_job_id,
                    plan_id,
                ),
            )
            self._insert_event(connection, job_id, "job.created", {"operation": operation.value})
        return self.get_job(job_id)

    def claim_next(self, worker_id: str) -> JobResource | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE status=? ORDER BY created_at LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            timestamp = utc_now()
            updated = connection.execute(
                """
                UPDATE jobs SET status=?, stage=?, started_at=?, updated_at=?, worker_id=?
                WHERE id=? AND status=?
                """,
                (
                    JobStatus.RUNNING.value,
                    "starting",
                    timestamp,
                    timestamp,
                    worker_id,
                    row["id"],
                    JobStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            self._insert_event(connection, row["id"], "job.started", {"worker_id": worker_id})
            connection.commit()
        return self.get_job(row["id"])

    def replace_request(self, job_id: str, request: dict[str, Any]) -> JobResource:
        self.get_job(job_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET request_json=?, updated_at=? WHERE id=?",
                (json.dumps(request, ensure_ascii=False), utc_now(), job_id),
            )
            self._insert_event(connection, job_id, "job.request_ready", {})
        return self.get_job(job_id)

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        stage: str | None = None,
        best_artifact_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> JobResource:
        job = self.get_job(job_id)
        next_status = status or job.status
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status.value
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, float(progress)))
        if stage is not None:
            values["stage"] = stage
        if best_artifact_id is not None:
            values["best_artifact_id"] = best_artifact_id
        if error is not None:
            values["error_json"] = json.dumps(error, ensure_ascii=False)
        if next_status in TERMINAL_JOB_STATUSES:
            values["finished_at"] = utc_now()
            if next_status == JobStatus.SUCCEEDED:
                values["progress"] = 1.0
        assignments = ", ".join(f"{name}=?" for name in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*values.values(), job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "job.updated",
                {
                    "status": next_status.value,
                    "progress": values.get("progress", job.progress),
                    "stage": values.get("stage", job.stage),
                },
            )
        return self.get_job(job_id)

    def request_cancel(self, job_id: str) -> JobResource:
        job = self.get_job(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )
            self._insert_event(connection, job_id, "job.cancel_requested", {})
        return self.get_job(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobStoreError(f"unknown job: {job_id}")
        return bool(row["cancel_requested"])

    def add_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> JobEvent:
        with self._connect() as connection:
            sequence = self._insert_event(connection, job_id, event_type, payload)
        return self.events_after(job_id, sequence - 1)[0]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO events(job_id,event_type,created_at,payload_json) VALUES(?,?,?,?)",
            (job_id, event_type, utc_now(), json.dumps(payload, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def events_after(self, job_id: str, sequence: int = 0) -> list[JobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence",
                (job_id, sequence),
            ).fetchall()
        return [
            JobEvent(
                sequence=row["sequence"],
                job_id=row["job_id"],
                event_type=row["event_type"],
                created_at=row["created_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def ingest_input(
        self,
        job_id: str,
        source: str | Path,
        *,
        name: str | None = None,
        parent_artifact_id: str | None = None,
    ) -> ArtifactRecord:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise JobStoreError(f"input file does not exist: {source_path}")
        safe_name = self._safe_name(name or source_path.name)
        destination = self.job_dir(job_id) / "inputs" / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        return self.register_artifact(
            job_id,
            destination,
            kind=ArtifactKind.INPUT,
            name=safe_name,
            parent_artifact_id=parent_artifact_id,
        )

    def ingest_artifact(
        self,
        job_id: str,
        artifact_id: str,
        *,
        name: str | None = None,
    ) -> ArtifactRecord:
        source = self.get_artifact(artifact_id)
        return self.ingest_input(
            job_id,
            self.artifact_path(artifact_id),
            name=name or source.name,
            parent_artifact_id=artifact_id,
        )

    def register_artifact(
        self,
        job_id: str,
        path: str | Path,
        *,
        kind: ArtifactKind,
        name: str | None = None,
        media_type: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        artifact_path = Path(path).resolve()
        job_root = self.job_dir(job_id)
        if not artifact_path.is_file() or job_root not in artifact_path.parents:
            raise JobStoreError("artifact must be a file inside its job workspace")
        artifact_id = str(uuid.uuid4())
        artifact_name = self._safe_name(name or artifact_path.name)
        relative = artifact_path.relative_to(job_root).as_posix()
        timestamp = utc_now()
        guessed = mimetypes.guess_type(artifact_name)[0] or "application/octet-stream"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id,job_id,name,kind,relative_path,size_bytes,sha256,media_type,
                    created_at,parent_artifact_id,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact_id,
                    job_id,
                    artifact_name,
                    kind.value,
                    relative,
                    artifact_path.stat().st_size,
                    sha256_file(artifact_path),
                    media_type or guessed,
                    timestamp,
                    parent_artifact_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            self._insert_event(
                connection,
                job_id,
                "artifact.created",
                {"artifact_id": artifact_id, "name": artifact_name, "kind": kind.value},
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise JobStoreError(f"unknown artifact: {artifact_id}")
        return self._artifact_from_row(row)

    def artifact_path(self, artifact_id: str) -> Path:
        artifact = self.get_artifact(artifact_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relative_path FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        assert row is not None
        path = (self.job_dir(artifact.job_id) / row["relative_path"]).resolve()
        if self.job_dir(artifact.job_id) not in path.parents or not path.is_file():
            raise JobStoreError("artifact file is missing or escaped its job directory")
        return path

    def list_artifacts(self, job_id: str) -> list[ArtifactRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at, name", (job_id,)
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def create_plan(
        self,
        job_id: str,
        operation: JobOperation,
        source_sha256: str,
        artifact_id: str,
    ) -> PlanResource:
        plan_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, job_id, operation.value, source_sha256, "ready", utc_now(), None, artifact_id),
            )
            connection.execute(
                "UPDATE jobs SET plan_id=?, updated_at=? WHERE id=?",
                (plan_id, utc_now(), job_id),
            )
            self._insert_event(connection, job_id, "plan.ready", {"plan_id": plan_id})
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> PlanResource:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise JobStoreError(f"unknown plan: {plan_id}")
        return PlanResource(
            id=row["id"],
            job_id=row["job_id"],
            operation=JobOperation(row["operation"]),
            source_sha256=row["source_sha256"],
            status=row["status"],
            created_at=row["created_at"],
            applied_job_id=row["applied_job_id"],
            artifact_id=row["artifact_id"],
        )

    def mark_plan_applied(self, plan_id: str, applied_job_id: str) -> PlanResource:
        with self._connect() as connection:
            connection.execute(
                "UPDATE plans SET status='applied', applied_job_id=? WHERE id=? AND status='ready'",
                (applied_job_id, plan_id),
            )
        return self.get_plan(plan_id)

    def retry_job(self, job_id: str) -> JobResource:
        source = self.get_job(job_id)
        request = dict(source.request)
        if source.best_artifact_id:
            request["source_artifact_id"] = source.best_artifact_id
            request.pop("source_paths", None)
        return self.create_job(
            source.operation,
            request,
            mode=source.mode,
            source_job_id=source.id,
            parent_job_id=source.id,
        )

    def get_job(self, job_id: str) -> JobResource:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobStoreError(f"unknown job: {job_id}")
        return JobResource(
            id=row["id"],
            operation=JobOperation(row["operation"]),
            status=JobStatus(row["status"]),
            mode=row["mode"],
            progress=float(row["progress"]),
            stage=row["stage"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cancel_requested=bool(row["cancel_requested"]),
            source_job_id=row["source_job_id"],
            parent_job_id=row["parent_job_id"],
            plan_id=row["plan_id"],
            best_artifact_id=row["best_artifact_id"],
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            request=json.loads(row["request_json"]),
            artifacts=self.list_artifacts(job_id),
        )

    def list_jobs(self, *, statuses: Iterable[JobStatus] | None = None) -> list[JobResource]:
        query = "SELECT id FROM jobs"
        parameters: list[Any] = []
        if statuses:
            values = [status.value for status in statuses]
            query += " WHERE status IN (" + ",".join("?" for _ in values) + ")"
            parameters.extend(values)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self.get_job(row["id"]) for row in rows]

    def cleanup(self, *, older_than_epoch: float) -> list[str]:
        removed: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, finished_at FROM jobs WHERE finished_at IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    timestamp = __import__("datetime").datetime.fromisoformat(
                        row["finished_at"]
                    ).timestamp()
                except (TypeError, ValueError):
                    continue
                if timestamp >= older_than_epoch:
                    continue
                shutil.rmtree(self.jobs_root / row["id"], ignore_errors=True)
                connection.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                removed.append(row["id"])
        return removed

    @staticmethod
    def _safe_name(value: str) -> str:
        normalized = Path(value).name.replace("\x00", "")
        safe = "".join(character for character in normalized if character.isalnum() or character in "._- ")
        safe = safe.strip(" .")[:180]
        if not safe:
            raise JobStoreError("empty or unsafe artifact name")
        return safe

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            job_id=row["job_id"],
            name=row["name"],
            kind=ArtifactKind(row["kind"]),
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            media_type=row["media_type"],
            created_at=row["created_at"],
            parent_artifact_id=row["parent_artifact_id"],
            metadata=json.loads(row["metadata_json"]),
            download_url=f"/v1/artifacts/{row['id']}",
        )
