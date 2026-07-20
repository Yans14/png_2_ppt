from __future__ import annotations

import json
import math
import shutil
import sqlite3
import threading
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

from .job_store import sha256_file
from .ooxml_edit import DML, PML, extract_shape_graph, slide_part_names
from .service_models import (
    TemplateFamilyResource,
    TemplateMatch,
    TemplateResource,
    TemplateSlideResource,
    utc_now,
)


INDEX_VERSION = 1
CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"


class TemplateCatalogError(RuntimeError):
    pass


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def perceptual_hash(path: str | Path) -> str:
    with Image.open(path) as source:
        image = source.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        values = np.asarray(image, dtype=np.int16)
    bits = values[:, 1:] >= values[:, :-1]
    integer = 0
    for bit in bits.flatten():
        integer = (integer << 1) | int(bit)
    return f"{integer:016x}"


def hash_distance(left: str | None, right: str | None) -> int:
    if not left or not right:
        return 64
    return (int(left, 16) ^ int(right, 16)).bit_count()


def aggregate_perceptual_hash(values: list[str]) -> str | None:
    if not values:
        return None
    integers = [int(value, 16) for value in values]
    combined = 0
    for bit in range(64):
        votes = sum((value >> bit) & 1 for value in integers)
        if votes * 2 >= len(integers):
            combined |= 1 << bit
    return f"{combined:016x}"


def _archetype(features: dict[str, float | int | str]) -> str:
    if int(features.get("chart_count", 0)):
        return "chart"
    if int(features.get("table_count", 0)):
        return "table"
    if int(features.get("picture_count", 0)) >= 2:
        return "visual"
    text_count = int(features.get("text_count", 0))
    shape_count = int(features.get("shape_count", 0))
    if text_count <= 2 and shape_count <= 4:
        return "title"
    if text_count >= 8:
        return "dense_content"
    if shape_count >= 12:
        return "process"
    return "content"


def _slide_features(pptx_path: Path) -> list[dict[str, Any]]:
    shapes = extract_shape_graph(pptx_path)
    grouped: dict[int, list[Any]] = {}
    for shape in shapes:
        grouped.setdefault(shape.slide_index, []).append(shape)
    features: list[dict[str, Any]] = []
    with zipfile.ZipFile(pptx_path) as archive:
        for slide_index, part_name in enumerate(slide_part_names(archive), start=1):
            root = ET.fromstring(archive.read(part_name))
            slide_shapes = grouped.get(slide_index, [])
            widths = [float(item.width_pt or 0) for item in slide_shapes]
            heights = [float(item.height_pt or 0) for item in slide_shapes]
            colors = [item.fill_color for item in slide_shapes if item.fill_color]
            structural: dict[str, float | int | str] = {
                "shape_count": len(root.findall(f".//{{{PML}}}sp")),
                "picture_count": len(root.findall(f".//{{{PML}}}pic")),
                "group_count": len(root.findall(f".//{{{PML}}}grpSp")),
                "text_count": sum(bool(item.text) for item in slide_shapes),
                "table_count": len(root.findall(f".//{{{DML}}}tbl")),
                "chart_count": len(root.findall(f".//{{{CHART}}}chart")),
                "mean_width_pt": round(sum(widths) / max(1, len(widths)), 3),
                "mean_height_pt": round(sum(heights) / max(1, len(heights)), 3),
            }
            features.append(
                {
                    "slide_index": slide_index,
                    "archetype": _archetype(structural),
                    "structural": structural,
                    "style": {"dominant_colors": [item for item, _ in Counter(colors).most_common(6)]},
                }
            )
    return features


def _numeric_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    keys = (
        "shape_count",
        "picture_count",
        "group_count",
        "text_count",
        "table_count",
        "chart_count",
        "mean_width_pt",
        "mean_height_pt",
    )
    scores = []
    for key in keys:
        a = float(left.get(key, 0))
        b = float(right.get(key, 0))
        scale = max(abs(a), abs(b), 1.0)
        scores.append(max(0.0, 1.0 - abs(a - b) / scale))
    return sum(scores) / len(scores)


def _style_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    a = set(left.get("dominant_colors", []))
    b = set(right.get("dominant_colors", []))
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


class TemplateCatalog:
    """Global local template library with immutable decks and SQLite metadata."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.decks_root = self.root / "decks"
        self.previews_root = self.root / "previews"
        self.decks_root.mkdir(parents=True, exist_ok=True)
        self.previews_root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "catalog.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS template_families (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    inferred INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS templates (
                    id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL REFERENCES template_families(id),
                    name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL UNIQUE,
                    perceptual_hash TEXT,
                    slide_count INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT,
                    index_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS template_slides (
                    id TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
                    family_id TEXT NOT NULL REFERENCES template_families(id),
                    slide_index INTEGER NOT NULL,
                    archetype TEXT NOT NULL,
                    structural_json TEXT NOT NULL,
                    style_json TEXT NOT NULL,
                    preview_path TEXT,
                    perceptual_hash TEXT,
                    UNIQUE(template_id, slide_index)
                );
                CREATE INDEX IF NOT EXISTS template_slides_family ON template_slides(family_id);
                CREATE INDEX IF NOT EXISTS template_slides_archetype ON template_slides(archetype);
                """
            )

    def import_deck(
        self,
        source_path: str | Path,
        *,
        preview_paths: Iterable[str | Path] = (),
        name: str | None = None,
    ) -> tuple[TemplateResource, bool]:
        source = Path(source_path).resolve()
        if source.suffix.lower() != ".pptx" or not zipfile.is_zipfile(source):
            raise TemplateCatalogError("template input must be a valid .pptx")
        digest = sha256_file(source)
        existing = self._template_by_sha(digest)
        if existing:
            return existing, True
        features = _slide_features(source)
        previews = [Path(item).resolve() for item in preview_paths]
        preview_hashes = [perceptual_hash(item) for item in previews if item.is_file()]
        representative_hash = aggregate_perceptual_hash(preview_hashes)
        near = self._near_duplicate(representative_hash, len(features))
        if near:
            return near, True
        family_id = self._infer_family(features)
        template_id = str(uuid.uuid4())
        timestamp = utc_now()
        destination = self.decks_root / f"{template_id}.pptx"
        shutil.copy2(source, destination)
        preview_dir = self.previews_root / template_id
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_records: list[tuple[str | None, str | None]] = []
        for index in range(len(features)):
            if index < len(previews) and previews[index].is_file():
                target = preview_dir / f"slide-{index + 1}.png"
                shutil.copy2(previews[index], target)
                preview_records.append((str(target.relative_to(self.root)), perceptual_hash(target)))
            else:
                preview_records.append((None, None))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO templates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    template_id,
                    family_id,
                    name or source.stem,
                    str(destination.relative_to(self.root)),
                    digest,
                    representative_hash,
                    len(features),
                    1,
                    None,
                    INDEX_VERSION,
                    timestamp,
                    timestamp,
                    json.dumps({}, ensure_ascii=False),
                ),
            )
            for feature, (preview_path, preview_digest) in zip(features, preview_records):
                connection.execute(
                    "INSERT INTO template_slides VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        template_id,
                        family_id,
                        feature["slide_index"],
                        feature["archetype"],
                        json.dumps(feature["structural"], ensure_ascii=False),
                        json.dumps(feature["style"], ensure_ascii=False),
                        preview_path,
                        preview_digest,
                    ),
                )
        return self.get_template(template_id), False

    def _template_by_sha(self, digest: str) -> TemplateResource | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM templates WHERE source_sha256=?", (digest,)
            ).fetchone()
        return self._template_from_row(row) if row else None

    def _near_duplicate(self, digest: str | None, slide_count: int) -> TemplateResource | None:
        if not digest:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM templates WHERE slide_count=? AND perceptual_hash IS NOT NULL",
                (slide_count,),
            ).fetchall()
        for row in rows:
            if hash_distance(digest, row["perceptual_hash"]) <= 4:
                return self._template_from_row(row)
        return None

    def _infer_family(self, features: list[dict[str, Any]]) -> str:
        signature = Counter(item["archetype"] for item in features)
        with self._connect() as connection:
            families = connection.execute(
                "SELECT id FROM template_families WHERE active=1"
            ).fetchall()
            best: tuple[float, str] | None = None
            for family in families:
                rows = connection.execute(
                    "SELECT archetype, COUNT(*) AS count FROM template_slides WHERE family_id=? GROUP BY archetype",
                    (family["id"],),
                ).fetchall()
                candidate = Counter({row["archetype"]: int(row["count"]) for row in rows})
                union = sum((signature | candidate).values())
                intersection = sum((signature & candidate).values())
                score = intersection / max(1, union)
                if best is None or score > best[0]:
                    best = (score, family["id"])
            if best and best[0] >= 0.72:
                return best[1]
            family_id = str(uuid.uuid4())
            timestamp = utc_now()
            connection.execute(
                "INSERT INTO template_families VALUES(?,?,?,?,?,?)",
                (family_id, f"Family {family_id[:8]}", 1, 1, timestamp, timestamp),
            )
            return family_id

    def list_templates(self, *, include_deleted: bool = False) -> list[TemplateResource]:
        query = "SELECT * FROM templates"
        if not include_deleted:
            query += " WHERE active=1 AND deleted_at IS NULL"
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._template_from_row(row) for row in rows]

    def get_template(self, template_id: str) -> TemplateResource:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise TemplateCatalogError(f"unknown template: {template_id}")
        return self._template_from_row(row)

    def template_path(self, template_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relative_path FROM templates WHERE id=?", (template_id,)
            ).fetchone()
        if row is None:
            raise TemplateCatalogError(f"unknown template: {template_id}")
        path = (self.root / row["relative_path"]).resolve()
        if self.root not in path.parents or not path.is_file():
            raise TemplateCatalogError("template deck is missing or escaped catalog root")
        return path

    def list_slides(self, *, active_only: bool = True) -> list[TemplateSlideResource]:
        query = """
            SELECT s.* FROM template_slides s
            JOIN templates t ON t.id=s.template_id
            JOIN template_families f ON f.id=s.family_id
        """
        if active_only:
            query += " WHERE t.active=1 AND t.deleted_at IS NULL AND f.active=1"
        query += " ORDER BY s.family_id, s.template_id, s.slide_index"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._slide_from_row(row) for row in rows]

    def get_slide(self, slide_id: str) -> TemplateSlideResource:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM template_slides WHERE id=?", (slide_id,)
            ).fetchone()
        if row is None:
            raise TemplateCatalogError(f"unknown template slide: {slide_id}")
        return self._slide_from_row(row)

    def preview_path(self, slide_id: str) -> Path | None:
        slide = self.get_slide(slide_id)
        if not slide.preview_path:
            return None
        path = (self.root / slide.preview_path).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        return path

    def soft_delete(self, template_id: str) -> TemplateResource:
        self.get_template(template_id)
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE templates SET active=0, deleted_at=?, updated_at=? WHERE id=?",
                (timestamp, timestamp, template_id),
            )
        return self.get_template(template_id)

    def restore(self, template_id: str) -> TemplateResource:
        self.get_template(template_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE templates SET active=1, deleted_at=NULL, updated_at=? WHERE id=?",
                (utc_now(), template_id),
            )
        return self.get_template(template_id)

    def list_families(self) -> list[TemplateFamilyResource]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*, COUNT(t.id) AS template_count
                FROM template_families f LEFT JOIN templates t
                ON t.family_id=f.id AND t.active=1 AND t.deleted_at IS NULL
                GROUP BY f.id ORDER BY f.name
                """
            ).fetchall()
        return [self._family_from_row(row) for row in rows]

    def update_family(
        self,
        family_id: str,
        *,
        name: str | None = None,
        active: bool | None = None,
    ) -> TemplateFamilyResource:
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if name is not None:
            fields["name"] = name.strip()
        if active is not None:
            fields["active"] = int(active)
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._connect() as connection:
            updated = connection.execute(
                f"UPDATE template_families SET {assignments} WHERE id=?",
                (*fields.values(), family_id),
            )
            if updated.rowcount != 1:
                raise TemplateCatalogError(f"unknown template family: {family_id}")
        return next(item for item in self.list_families() if item.id == family_id)

    def merge_families(self, target_family_id: str, source_family_ids: list[str]) -> TemplateFamilyResource:
        if target_family_id in source_family_ids:
            source_family_ids = [item for item in source_family_ids if item != target_family_id]
        known = {item.id for item in self.list_families()}
        if target_family_id not in known:
            raise TemplateCatalogError(f"unknown template family: {target_family_id}")
        missing = sorted(set(source_family_ids) - known)
        if missing:
            raise TemplateCatalogError("unknown source template families: " + ", ".join(missing))
        if not source_family_ids:
            raise TemplateCatalogError("at least one distinct source family is required")
        with self._lock, self._connect() as connection:
            for family_id in source_family_ids:
                connection.execute(
                    "UPDATE templates SET family_id=?, updated_at=? WHERE family_id=?",
                    (target_family_id, utc_now(), family_id),
                )
                connection.execute(
                    "UPDATE template_slides SET family_id=? WHERE family_id=?",
                    (target_family_id, family_id),
                )
                connection.execute(
                    "UPDATE template_families SET active=0, updated_at=? WHERE id=?",
                    (utc_now(), family_id),
                )
        return next(item for item in self.list_families() if item.id == target_family_id)

    def split_family(self, family_id: str, template_ids: list[str], name: str) -> TemplateFamilyResource:
        known = {item.id for item in self.list_families()}
        if family_id not in known:
            raise TemplateCatalogError(f"unknown template family: {family_id}")
        requested = list(dict.fromkeys(template_ids))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM templates WHERE family_id=?",
                (family_id,),
            ).fetchall()
        available = {row["id"] for row in rows}
        missing = sorted(set(requested) - available)
        if missing:
            raise TemplateCatalogError(
                "templates do not belong to the source family: " + ", ".join(missing)
            )
        new_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO template_families VALUES(?,?,?,?,?,?)",
                (new_id, name.strip(), 1, 0, timestamp, timestamp),
            )
            for template_id in requested:
                updated = connection.execute(
                    "UPDATE templates SET family_id=?, updated_at=? WHERE id=? AND family_id=?",
                    (new_id, timestamp, template_id, family_id),
                )
                if updated.rowcount:
                    connection.execute(
                        "UPDATE template_slides SET family_id=? WHERE template_id=?",
                        (new_id, template_id),
                    )
        return next(item for item in self.list_families() if item.id == new_id)

    def reindex(self) -> dict[str, int]:
        reindexed = 0
        skipped = 0
        for template in self.list_templates(include_deleted=True):
            if template.index_version >= INDEX_VERSION:
                skipped += 1
                continue
            features = _slide_features(self.template_path(template.id))
            with self._connect() as connection:
                for feature in features:
                    connection.execute(
                        """
                        UPDATE template_slides SET archetype=?, structural_json=?, style_json=?
                        WHERE template_id=? AND slide_index=?
                        """,
                        (
                            feature["archetype"],
                            json.dumps(feature["structural"], ensure_ascii=False),
                            json.dumps(feature["style"], ensure_ascii=False),
                            template.id,
                            feature["slide_index"],
                        ),
                    )
                connection.execute(
                    "UPDATE templates SET index_version=?, updated_at=? WHERE id=?",
                    (INDEX_VERSION, utc_now(), template.id),
                )
            reindexed += 1
        return {"reindexed": reindexed, "skipped": skipped, "index_version": INDEX_VERSION}

    def match_deck(
        self,
        source_path: str | Path,
        *,
        top_k: int = 5,
        forced_family_id: str | None = None,
    ) -> list[TemplateMatch]:
        source_features = _slide_features(Path(source_path).resolve())
        slides = self.list_slides()
        if forced_family_id:
            slides = [item for item in slides if item.family_id == forced_family_id]
        if not slides:
            return []
        by_family: dict[str, list[TemplateSlideResource]] = {}
        for slide in slides:
            by_family.setdefault(slide.family_id, []).append(slide)
        family_scores: list[tuple[float, str, list[TemplateMatch]]] = []
        for family_id, family_slides in by_family.items():
            matches: list[TemplateMatch] = []
            for feature in source_features:
                ranked: list[TemplateMatch] = []
                for candidate in family_slides:
                    structural = _numeric_similarity(
                        feature["structural"], candidate.structural_features
                    )
                    if feature["archetype"] == candidate.archetype:
                        structural = min(1.0, structural + 0.08)
                    style = _style_similarity(feature["style"], candidate.style_features)
                    confidence = 0.78 * structural + 0.22 * style
                    ranked.append(
                        TemplateMatch(
                            template_slide_id=candidate.id,
                            template_id=candidate.template_id,
                            family_id=family_id,
                            source_slide_index=int(feature["slide_index"]),
                            slide_index=candidate.slide_index,
                            structural_score=round(structural, 6),
                            style_score=round(style, 6),
                            confidence=round(confidence, 6),
                        )
                    )
                ranked.sort(key=lambda item: item.confidence, reverse=True)
                matches.extend(ranked[:top_k])
            best_by_source = [
                max(matches[index : index + top_k], key=lambda item: item.confidence)
                for index in range(0, len(matches), top_k)
                if matches[index : index + top_k]
            ]
            family_score = sum(item.confidence for item in best_by_source) / max(1, len(best_by_source))
            family_scores.append((family_score, family_id, matches))
        _, selected_family, candidates = max(family_scores, key=lambda item: item[0])
        selected: list[TemplateMatch] = []
        for index in range(0, len(candidates), top_k):
            group = candidates[index : index + top_k]
            if not group:
                continue
            winner = max(group, key=lambda item: item.confidence)
            selected.extend(
                item.model_copy(update={"selected": item.template_slide_id == winner.template_slide_id})
                for item in group
            )
        return [item for item in selected if item.family_id == selected_family]

    @staticmethod
    def _template_from_row(row: sqlite3.Row) -> TemplateResource:
        return TemplateResource(
            id=row["id"],
            family_id=row["family_id"],
            name=row["name"],
            source_sha256=row["source_sha256"],
            perceptual_hash=row["perceptual_hash"],
            slide_count=int(row["slide_count"]),
            active=bool(row["active"]),
            deleted_at=row["deleted_at"],
            index_version=int(row["index_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _slide_from_row(row: sqlite3.Row) -> TemplateSlideResource:
        return TemplateSlideResource(
            id=row["id"],
            template_id=row["template_id"],
            family_id=row["family_id"],
            slide_index=int(row["slide_index"]),
            archetype=row["archetype"],
            structural_features=json.loads(row["structural_json"]),
            style_features=json.loads(row["style_json"]),
            preview_path=row["preview_path"],
            perceptual_hash=row["perceptual_hash"],
        )

    @staticmethod
    def _family_from_row(row: sqlite3.Row) -> TemplateFamilyResource:
        return TemplateFamilyResource(
            id=row["id"],
            name=row["name"],
            active=bool(row["active"]),
            inferred=bool(row["inferred"]),
            template_count=int(row["template_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = [
    "INDEX_VERSION",
    "TemplateCatalog",
    "TemplateCatalogError",
    "aggregate_perceptual_hash",
    "hash_distance",
    "perceptual_hash",
]
