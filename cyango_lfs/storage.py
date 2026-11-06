"""Staged exports and upload manifests on disk."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import UploadJob


def default_cache_root() -> Path:
    override = os.environ.get("CYANGO_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "LichtFeld" / "Cyango"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "lichtfeld-cyango"
    return Path.home() / ".cache" / "lichtfeld-cyango"


class JobStore:
    """One JSON manifest per upload, plus its staged export file."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.jobs_dir = self.root / "jobs"
        self.staging_dir = self.root / "staging"
        self._lock = threading.RLock()

    def ensure(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def staging_path(self, job_id: str, source_format: str) -> Path:
        self.ensure()
        return self.staging_dir / f"{job_id}.{source_format}"

    def save(self, job: UploadJob) -> None:
        self.ensure()
        job.touch()
        target = self.jobs_dir / f"{job.id}.json"
        payload = json.dumps(job.to_dict(), indent=2, sort_keys=True)
        with self._lock:
            handle, temporary = tempfile.mkstemp(prefix=f".{job.id}.", suffix=".tmp", dir=self.jobs_dir)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def load(self, job_id: str) -> UploadJob | None:
        path = self.jobs_dir / f"{job_id}.json"
        if not path.is_file():
            return None
        with self._lock:
            try:
                return UploadJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                return None

    def list_jobs(self) -> list[UploadJob]:
        if not self.jobs_dir.is_dir():
            return []
        jobs = [job for job in (self.load(path.stem) for path in self.jobs_dir.glob("*.json")) if job is not None]
        return sorted(jobs, key=lambda item: item.updated_at, reverse=True)

    def latest_resumable(self) -> UploadJob | None:
        for job in self.list_jobs():
            try:
                if job.resumable and Path(job.file_path).stat().st_size == job.file_size:
                    return job
            except OSError:
                continue
        return None

    def cleanup(self) -> None:
        """Deletes jobs that cannot resume: finished ones and exports a closed session left behind."""
        for job in self.list_jobs():
            if not job.resumable:
                self.delete(job)

    def delete(self, job: UploadJob) -> None:
        with self._lock:
            staged = Path(job.file_path)
            try:
                # Never delete a file outside the staging folder.
                staged.resolve().relative_to(self.staging_dir.resolve())
                staged.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            try:
                (self.jobs_dir / f"{job.id}.json").unlink(missing_ok=True)
            except OSError:
                pass
