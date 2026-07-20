from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "EDITABLE_PPTX_HOME",
                str(Path.home() / ".local" / "share" / "editable-pptx-service"),
            )
        ).expanduser()
    )
    host: str = os.environ.get("EDITABLE_PPTX_HOST", "127.0.0.1")
    port: int = int(os.environ.get("EDITABLE_PPTX_PORT", "8765"))
    max_upload_bytes: int = int(os.environ.get("EDITABLE_PPTX_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    max_slides: int = int(os.environ.get("EDITABLE_PPTX_MAX_SLIDES", "100"))
    max_images_per_job: int = int(os.environ.get("EDITABLE_PPTX_MAX_IMAGES", "50"))
    max_attempts_per_slide: int = int(os.environ.get("EDITABLE_PPTX_MAX_ATTEMPTS", "3"))
    artifact_ttl_days: int = int(os.environ.get("EDITABLE_PPTX_ARTIFACT_TTL_DAYS", "30"))
    poll_interval_seconds: float = float(os.environ.get("EDITABLE_PPTX_POLL_INTERVAL", "0.5"))
    model: str = os.environ.get("EDITABLE_PPTX_MODEL", "gpt-5.5")
    default_max_cost_usd: float = float(os.environ.get("EDITABLE_PPTX_MAX_COST_USD", "5"))
    default_timeout_seconds: int = int(os.environ.get("EDITABLE_PPTX_TIMEOUT_SECONDS", "900"))
    slide_concurrency: int = int(os.environ.get("EDITABLE_PPTX_SLIDE_CONCURRENCY", "2"))
    template_match_threshold: float = float(
        os.environ.get("EDITABLE_PPTX_TEMPLATE_MATCH_THRESHOLD", "0.75")
    )

    @property
    def template_catalog_home(self) -> Path:
        return self.home / "template-catalog"

    def validate_network_binding(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get(
            "EDITABLE_PPTX_BEARER_TOKEN"
        ):
            raise ValueError(
                "Refusing a non-loopback API binding without EDITABLE_PPTX_BEARER_TOKEN"
            )
