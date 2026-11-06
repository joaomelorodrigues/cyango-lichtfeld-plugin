"""Upload job state, saved as JSON so an upload can resume after a restart."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    EXPORTING = "exporting"
    UPLOADING = "uploading"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class UploadedPart:
    part_number: int
    etag: str
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"part_number": self.part_number, "etag": self.etag, "size": self.size}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> UploadedPart:
        return cls(int(value.get("part_number", 0)), str(value.get("etag", "")), int(value.get("size", 0) or 0))


@dataclass
class UploadJob:
    id: str
    created_at: str
    updated_at: str
    status: JobStatus
    file_path: str
    source_format: str
    title: str
    description: str
    folder_id: str
    node_names: list[str]
    sh_degree: int
    base_url: str
    file_size: int = 0
    account_id: str = ""
    account_name: str = ""
    # The server session. upload_id is a signed ticket that expires after 48 hours.
    upload_id: str = ""
    upload_created_at: str = ""
    mode: str = ""
    part_size: int = 0
    part_count: int = 0
    uploaded_parts: list[UploadedPart] = field(default_factory=list)
    asset_id: str = ""
    story_id: str = ""
    story_url: str = ""
    error: str = ""

    def touch(self) -> None:
        self.updated_at = utc_now()

    def clear_session(self) -> None:
        self.upload_id = ""
        self.upload_created_at = ""
        self.mode = ""
        self.part_size = 0
        self.part_count = 0
        self.uploaded_parts = []

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["schema_version"] = SCHEMA_VERSION
        result["status"] = self.status.value
        result["uploaded_parts"] = [part.to_dict() for part in self.uploaded_parts]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> UploadJob:
        data = dict(value)
        if data.pop("schema_version", None) != SCHEMA_VERSION:
            raise ValueError("unsupported upload job schema_version")
        known = {item.name for item in fields(cls)}
        data = {key: item for key, item in data.items() if key in known}
        data["status"] = JobStatus(str(data["status"]))
        data["uploaded_parts"] = [UploadedPart.from_dict(part) for part in data.get("uploaded_parts", [])]
        return cls(**data)

    @property
    def resumable(self) -> bool:
        return self.file_size > 0 and self.status in {
            JobStatus.UPLOADING,
            JobStatus.FINALIZING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


@dataclass(frozen=True)
class UploadOutcome:
    asset_id: str
    story_id: str
    story_url: str


@dataclass(frozen=True)
class ProgressSnapshot:
    uploaded_bytes: int
    total_bytes: int
    fraction: float
    bytes_per_second: float
    eta_seconds: float | None
