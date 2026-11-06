"""Connects the LichtFeld main thread to the background Cyango upload."""

from __future__ import annotations

import copy
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import DEFAULT_BASE_URL, ApiClient
from .credentials import clear_token as clear_stored_token
from .credentials import load_token, save_token
from .errors import ConfigurationError, UploadCancelled
from .lfs_export import LfsExportAdapter, SceneSelection
from .models import JobStatus, ProgressSnapshot, UploadJob, utc_now
from .progress import format_bytes, format_duration
from .storage import JobStore
from .uploader import UploadCallbacks, UploadEngine

SETTINGS_NAME = "cyango"
STORED_SOURCE = "secure credential store"
SESSION_SOURCE = "this LichtFeld session"


@dataclass
class ControllerState:
    active: bool = False
    phase: str = "idle"
    status_text: str = "Ready to publish."
    progress: float = 0.0
    progress_text: str = ""
    account_text: str = ""
    token_source: str = ""
    token_configured: bool = False
    error: str = ""
    story_id: str = ""
    story_url: str = ""
    folders: tuple[tuple[str, str], ...] = ()
    resume_available: bool = False
    resumable_title: str = ""
    resumable_size: int = 0


class PluginController:
    def __init__(
        self, lf: Any, runtime_state: Any, store: JobStore | None = None, *, auto_connect: bool = True
    ) -> None:
        self.lf = lf
        self.adapter = LfsExportAdapter(lf, runtime_state)
        self.store = store or JobStore()
        self.settings = lf.plugins.settings(SETTINGS_NAME)
        self._lock = threading.RLock()
        self._state = ControllerState()
        self._job: UploadJob | None = None
        self._job_token = ""
        self._concurrency = 4
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._export_seen_active = False
        self._remember_token = bool(self.settings.get("remember_token", False))
        self._token = load_token() if self._remember_token else ""
        self._update_token_state()
        self.store.cleanup()
        self._refresh_resume()
        self._unsubscribe = runtime_state.export_progress_state.subscribe(self._on_export_state)
        if self._token and auto_connect:
            self.test_connection()

    # -- Queries --------------------------------------------------------

    def snapshot(self) -> ControllerState:
        with self._lock:
            return copy.copy(self._state)

    def preference(self, key: str, default: Any) -> Any:
        return self.settings.get(key, default)

    def save_preferences(self, values: dict[str, Any]) -> None:
        self.settings.update(values)

    @property
    def base_url(self) -> str:
        default = os.environ.get("CYANGO_API_URL", DEFAULT_BASE_URL)
        return str(self.preference("base_url", default) or default).rstrip("/")

    def inspect_scope(self, scope: str) -> SceneSelection:
        return self.adapter.resolve(scope)

    def suggested_title(self, scope: str) -> str:
        return self.adapter.suggested_title(scope)

    # -- Token ----------------------------------------------------------

    def set_token(self, value: str, *, remember: bool | None = None) -> None:
        with self._lock:
            self._token = value.strip()
            if remember is not None:
                self._remember_token = bool(remember)
                self.settings.set("remember_token", self._remember_token)
            if self._remember_token and self._token:
                if not save_token(self._token):
                    self._remember_token = False
                    self.settings.set("remember_token", False)
            else:
                clear_stored_token()
            self._state.error = ""
            self._state.account_text = ""
            self._state.folders = ()
            self._update_token_state()

    def clear_token(self) -> None:
        self.set_token("", remember=False)

    def set_remember_token(self, enabled: bool) -> None:
        with self._lock:
            self._remember_token = bool(enabled)
            if self._remember_token and self._token and not save_token(self._token):
                self._remember_token = False
                self._state.error = "No secure credential store is available. The token is kept for this session only."
            if not self._remember_token:
                clear_stored_token()
            self.settings.set("remember_token", self._remember_token)
            self._update_token_state()

    def test_connection(self) -> None:
        token = self._require_token()
        with self._lock:
            if self._state.active:
                return
            self._state.active = True
            self._state.phase = "auth"
            self._state.status_text = "Checking the Cyango account..."
            self._state.error = ""

        def run() -> None:
            try:
                client = ApiClient(token, self.base_url)
                me = client.get_me()
                folders = client.list_folders()
                with self._lock:
                    self._state.account_text = _account_label(me)
                    self._state.folders = tuple((folder["id"], folder["name"]) for folder in folders)
                    self._state.status_text = f"Connected as {self._state.account_text}."
            except Exception as error:
                self._set_error(error)
            finally:
                with self._lock:
                    self._state.active = False
                    self._state.phase = "idle"
                self.adapter.request_redraw()

        threading.Thread(target=run, daemon=True, name="cyango-auth").start()

    # -- Upload ---------------------------------------------------------

    def start_upload(
        self,
        *,
        scope: str,
        source_format: str,
        title: str,
        description: str,
        folder_id: str,
        sh_degree: int,
        concurrency: int,
    ) -> None:
        token = self._require_token()
        with self._lock:
            if self._state.active:
                raise ConfigurationError("An export or upload is already running.")
        selection = self.adapter.resolve(scope)
        job_id = uuid.uuid4().hex
        path = self.store.staging_path(job_id, source_format)
        now = utc_now()
        job = UploadJob(
            id=job_id,
            created_at=now,
            updated_at=now,
            status=JobStatus.EXPORTING,
            file_path=str(path),
            source_format=source_format,
            title=title.strip() or self.adapter.suggested_title(scope),
            description=description.strip(),
            folder_id=folder_id.strip(),
            node_names=selection.node_names,
            sh_degree=max(0, min(int(sh_degree), selection.maximum_sh_degree, 3)),
            base_url=self.base_url,
        )
        self.store.save(job)
        with self._lock:
            self._job = job
            self._job_token = token
            self._concurrency = max(1, min(int(concurrency), 8))
            self._cancel_event = threading.Event()
            self._export_seen_active = False
            self._state.active = True
            self._state.phase = "export"
            self._state.status_text = (
                f"Exporting {len(selection.node_names)} splat node(s) to {source_format.upper()}..."
            )
            self._state.progress = 0.0
            self._state.progress_text = "Starting the export"
            self._state.error = ""
            self._state.story_id = ""
            self._state.story_url = ""
        try:
            self.adapter.start_export(source_format, path, selection.node_names, job.sh_degree)
        except Exception as error:
            self._finish_failure(error)
            raise

    def resume_latest(self, concurrency: int) -> None:
        token = self._require_token()
        job = self.store.latest_resumable()
        if job is None:
            raise ConfigurationError("No staged upload to resume.")
        with self._lock:
            if self._state.active:
                raise ConfigurationError("An export or upload is already running.")
            self._job = job
            self._job_token = token
            self._concurrency = max(1, min(int(concurrency), 8))
            self._cancel_event = threading.Event()
            self._state.active = True
            self._state.phase = "upload"
            self._state.error = ""
            self._state.status_text = "Resuming the upload..."
        self._start_upload_worker()

    def discard_resume(self) -> None:
        job = self.store.latest_resumable()
        if job is None:
            return
        token = self._token
        if job.upload_id and token:
            # Best effort: drop the parts already in storage.
            def run() -> None:
                try:
                    ApiClient(token, job.base_url, timeout=15).abort_upload(job.upload_id)
                except Exception:
                    pass

            threading.Thread(target=run, daemon=True, name="cyango-abort").start()
        self.store.delete(job)
        self._refresh_resume()

    def cancel(self) -> None:
        with self._lock:
            phase = self._state.phase
            self._state.status_text = "Cancelling..."
            self._cancel_event.set()
        if phase == "export":
            try:
                self.adapter.cancel_export()
            except Exception:
                pass

    def poll_export_state(self) -> None:
        with self._lock:
            exporting = self._state.active and self._state.phase == "export"
        if exporting:
            self._on_export_state(self.adapter.export_state())

    def open_url(self, url: str) -> None:
        self.adapter.open_url(url)

    def copy_text(self, text: str) -> None:
        self.adapter.copy_text(text)

    def report_error(self, error: Exception) -> None:
        self._set_error(error)
        self.adapter.request_redraw()

    def shutdown(self) -> None:
        self.cancel()
        try:
            self._unsubscribe()
        except Exception:
            pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    # -- Internals ------------------------------------------------------

    def _on_export_state(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        with self._lock:
            job = self._job
            if not self._state.active or self._state.phase != "export" or job is None:
                return
            path = str(value.get("path", ""))
            if path and Path(path) != Path(job.file_path):
                return
            stage = str(value.get("stage", ""))
            if value.get("active", False):
                self._export_seen_active = True
                self._state.progress = max(0.0, min(float(value.get("progress", 0.0) or 0.0), 1.0))
                self._state.progress_text = stage or "Exporting"
                self.adapter.request_redraw()
                return
            if not self._export_seen_active and stage != "Complete":
                return
            # The export ended. Take the upload step only once.
            self._state.phase = "upload"
            error_text = str(value.get("error", "") or "")

        if self._cancel_event.is_set():
            self._finish_cancelled()
        elif error_text or stage != "Complete":
            self._finish_failure(ConfigurationError(error_text or "The LichtFeld export failed."))
        else:
            try:
                size = Path(job.file_path).stat().st_size
                if size <= 0:
                    raise ConfigurationError("LichtFeld wrote an empty export.")
                job.file_size = size
                job.status = JobStatus.UPLOADING
                self.store.save(job)
                self._start_upload_worker()
            except Exception as error:
                self._finish_failure(error)

    def _start_upload_worker(self) -> None:
        with self._lock:
            job = self._job
            if job is None:
                return
            token = self._job_token
            engine = UploadEngine(concurrency=self._concurrency)
            self._state.phase = "upload"
            self._state.progress = 0.0
            self._state.progress_text = ""
            self._state.status_text = "Preparing the upload..."

        def stage(message: str) -> None:
            with self._lock:
                self._state.status_text = message
                job.status = JobStatus.FINALIZING if message.startswith("Finishing") else JobStatus.UPLOADING
            self.store.save(job)
            self.adapter.request_redraw()

        def account(me: dict[str, Any]) -> None:
            job.account_name = _account_label(me)
            with self._lock:
                self._state.account_text = job.account_name
            self.store.save(job)

        def progress(value: ProgressSnapshot) -> None:
            with self._lock:
                self._state.progress = value.fraction
                self._state.progress_text = (
                    f"{format_bytes(value.uploaded_bytes)} / {format_bytes(value.total_bytes)}"
                    f"  •  {format_bytes(value.bytes_per_second)}/s  •  ETA {format_duration(value.eta_seconds)}"
                )
            self.adapter.request_redraw()

        def checkpoint() -> None:
            self.store.save(job)

        def run() -> None:
            try:
                outcome = engine.upload(
                    job, token, self._cancel_event, UploadCallbacks(stage, account, progress, checkpoint)
                )
                job.status = JobStatus.COMPLETE
                job.asset_id, job.story_id, job.story_url = outcome.asset_id, outcome.story_id, outcome.story_url
                job.error = ""
                self.store.delete(job)
                self._refresh_resume()
                with self._lock:
                    self._state.active = False
                    self._state.phase = "complete"
                    self._state.status_text = "Published to Cyango."
                    self._state.progress = 1.0
                    self._state.story_id = outcome.story_id
                    self._state.story_url = outcome.story_url
                    self._state.error = ""
            except UploadCancelled:
                self._finish_cancelled()
            except Exception as error:
                if self._cancel_event.is_set():
                    self._finish_cancelled()
                else:
                    self._finish_failure(error)
            finally:
                self._refresh_resume()
                self.adapter.request_redraw()

        self._worker = threading.Thread(target=run, daemon=True, name="cyango-upload")
        self._worker.start()

    def _finish_cancelled(self) -> None:
        job = self._job
        if job is not None:
            job.status = JobStatus.CANCELLED
            job.error = "Upload cancelled."
            self.store.save(job)
        # Resume state first, so a reader that sees the new phase also sees it.
        self._refresh_resume()
        with self._lock:
            self._state.active = False
            self._state.phase = "cancelled"
            staged = job is not None and job.file_size > 0
            self._state.status_text = "Cancelled. You can resume the upload." if staged else "Export cancelled."
            self._state.error = ""
        self.adapter.request_redraw()

    def _finish_failure(self, error: Exception) -> None:
        message = str(error) or error.__class__.__name__
        job = self._job
        if job is not None:
            job.status = JobStatus.FAILED
            job.error = message
            self.store.save(job)
        self._refresh_resume()
        with self._lock:
            self._state.active = False
            self._state.phase = "failed"
            self._state.status_text = "The upload stopped."
            self._state.error = message
        self.adapter.request_redraw()

    def _set_error(self, error: Exception) -> None:
        with self._lock:
            self._state.error = str(error) or error.__class__.__name__
            self._state.status_text = "Could not connect."

    def _refresh_resume(self) -> None:
        job = self.store.latest_resumable()
        with self._lock:
            self._state.resume_available = job is not None
            self._state.resumable_title = job.title if job else ""
            self._state.resumable_size = job.file_size if job else 0

    def _require_token(self) -> str:
        with self._lock:
            token = self._token
        if not token:
            raise ConfigurationError("Paste a Cyango external publish token first.")
        return token

    def _update_token_state(self) -> None:
        self._state.token_configured = bool(self._token)
        if not self._token:
            self._state.token_source = ""
        else:
            self._state.token_source = STORED_SOURCE if self._remember_token else SESSION_SOURCE


def _account_label(me: dict[str, Any]) -> str:
    user = me.get("user") if isinstance(me.get("user"), dict) else {}
    workspace = me.get("workspace") if isinstance(me.get("workspace"), dict) else {}
    name = str(user.get("email") or user.get("name") or user.get("id") or "Cyango account")
    workspace_name = str(workspace.get("name") or "")
    return f"{name} · {workspace_name}" if workspace_name else name
