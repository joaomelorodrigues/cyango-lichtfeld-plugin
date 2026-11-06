"""Uploads a staged export to Cyango: one PUT for small files, parallel parts for large ones."""

from __future__ import annotations

import math
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .api import ApiClient
from .errors import ApiError, ConfigurationError, PartUploadError, ProtocolError, UploadCancelled
from .models import ProgressSnapshot, UploadedPart, UploadJob, UploadOutcome, utc_now
from .progress import ProgressTracker

SIGN_BATCH = 16
# The server ticket lasts 48 hours. Start again a little before that.
SESSION_MAX_AGE = timedelta(hours=46)
MAX_RETRY_DELAY = 60.0


def _ignore(*_args: Any) -> None:
    pass


@dataclass
class UploadCallbacks:
    on_stage: Callable[[str], None] = _ignore
    on_account: Callable[[dict[str, Any]], None] = _ignore
    on_progress: Callable[[ProgressSnapshot], None] = _ignore
    # The job changed and should be saved.
    on_checkpoint: Callable[[], None] = _ignore


class UploadEngine:
    def __init__(self, *, concurrency: int = 4, retries: int = 3, timeout: float = 60.0) -> None:
        self.concurrency = max(1, min(int(concurrency), 8))
        self.retries = max(0, int(retries))
        self.timeout = timeout

    def upload(
        self,
        job: UploadJob,
        token: str,
        cancel_event: threading.Event,
        callbacks: UploadCallbacks | None = None,
    ) -> UploadOutcome:
        callbacks = callbacks or UploadCallbacks()
        path = Path(job.file_path)
        size = _validate_file(path, job.file_size)
        client = ApiClient(token, job.base_url, timeout=self.timeout)

        callbacks.on_stage("Connecting to Cyango...")
        me = self._retry(client.get_me, cancel_event)
        account_id = str((me.get("user") or {}).get("id") or "")
        if not account_id:
            raise ProtocolError("Cyango returned an account without an id.")
        if job.account_id and job.account_id != account_id:
            raise ConfigurationError(
                "This upload belongs to another Cyango account. Discard it or connect that account."
            )
        job.account_id = account_id
        callbacks.on_account(me)

        for attempt in range(2):
            single_url, single_headers = "", {}
            if not job.upload_id or _expired(job) or (job.mode == "single" and not job.uploaded_parts):
                callbacks.on_stage("Creating upload...")
                job.clear_session()
                session = self._retry(lambda: client.create_upload(_create_body(job, size)), cancel_event)
                single_url, single_headers = _open_session(job, session, size)
                callbacks.on_checkpoint()
            try:
                parts: list[dict[str, Any]] | None = None
                if job.mode == "single":
                    if not job.uploaded_parts:
                        self._put_single(client, job, path, size, single_url, single_headers, cancel_event, callbacks)
                else:
                    self._put_parts(client, job, path, size, cancel_event, callbacks)
                    parts = [{"part_number": part.part_number, "etag": part.etag} for part in job.uploaded_parts]
                callbacks.on_stage("Finishing upload...")
                result = self._retry(lambda parts=parts: client.complete_upload(job.upload_id, parts), cancel_event)
                return _outcome(result)
            except PartUploadError:
                raise
            except ApiError as error:
                # 404: the ticket or the S3 upload is gone. 409: the file is not in storage.
                if attempt == 0 and error.status in (404, 409):
                    callbacks.on_stage("The upload expired. Starting it again...")
                    job.clear_session()
                    callbacks.on_checkpoint()
                    continue
                raise
        raise ProtocolError("Cyango did not accept the upload after a restart.")

    def _put_single(
        self,
        client: ApiClient,
        job: UploadJob,
        path: Path,
        size: int,
        url: str,
        headers: dict[str, str],
        cancel_event: threading.Event,
        callbacks: UploadCallbacks,
    ) -> None:
        tracker = ProgressTracker(size)
        callbacks.on_stage("Uploading...")
        for attempt in range(self.retries + 1):
            _check_cancel(cancel_event)
            try:
                etag = client.put_range(
                    url,
                    path,
                    0,
                    size,
                    cancel_event,
                    lambda count: callbacks.on_progress(tracker.add_bytes(1, count)),
                    headers,
                )
                callbacks.on_progress(tracker.commit(1, size))
                job.uploaded_parts = [UploadedPart(1, etag, size)]
                callbacks.on_checkpoint()
                return
            except UploadCancelled:
                raise
            except PartUploadError as error:
                callbacks.on_progress(tracker.reset(1))
                # A 403 here is a signature problem, not an expired URL: the URL is minutes old.
                if attempt >= self.retries or error.status == 403 or not error.transient:
                    raise
                self._wait(attempt, cancel_event, error.retry_after)

    def _put_parts(
        self,
        client: ApiClient,
        job: UploadJob,
        path: Path,
        size: int,
        cancel_event: threading.Event,
        callbacks: UploadCallbacks,
    ) -> None:
        part_size, part_count = job.part_size, job.part_count
        completed = {part.part_number: part for part in job.uploaded_parts if 1 <= part.part_number <= part_count}
        tracker = ProgressTracker(size, {number: part.size for number, part in completed.items()})
        callbacks.on_progress(tracker.snapshot())
        missing = [number for number in range(1, part_count + 1) if number not in completed]
        if not missing:
            return
        callbacks.on_stage(f"Uploading {len(missing)} of {part_count} parts...")

        # A failed part stops the others without touching the user's cancel flag.
        stop = _EitherEvent(cancel_event)
        urls: dict[int, str] = {}
        url_lock = threading.Lock()
        save_lock = threading.Lock()
        position = {number: index for index, number in enumerate(missing)}

        def url_for(number: int, fresh: bool) -> str:
            with url_lock:
                if fresh:
                    urls.pop(number, None)
                if number not in urls:
                    ahead = [n for n in missing[position[number] + 1 :] if n not in urls and n not in completed]
                    batch = [number, *ahead[: SIGN_BATCH - 1]]
                    urls.update(self._retry(lambda: client.sign_parts(job.upload_id, batch), stop))
                return urls.pop(number)

        def upload_one(number: int) -> None:
            start = (number - 1) * part_size
            length = min(part_size, size - start)
            for attempt in range(self.retries + 1):
                _check_cancel(stop)
                try:
                    etag = client.put_range(
                        url_for(number, fresh=attempt > 0),
                        path,
                        start,
                        length,
                        stop,
                        lambda count: callbacks.on_progress(tracker.add_bytes(number, count)),
                    )
                    if not etag:
                        raise ProtocolError(f"Storage returned no ETag for part {number}.")
                    callbacks.on_progress(tracker.commit(number, length))
                    with save_lock:
                        completed[number] = UploadedPart(number, etag, length)
                        job.uploaded_parts = [completed[key] for key in sorted(completed)]
                        callbacks.on_checkpoint()
                    return
                except UploadCancelled:
                    tracker.reset(number)
                    raise
                except PartUploadError as error:
                    callbacks.on_progress(tracker.reset(number))
                    if attempt >= self.retries or not error.transient:
                        raise
                    self._wait(attempt, stop, error.retry_after)

        with ThreadPoolExecutor(
            max_workers=min(self.concurrency, len(missing)), thread_name_prefix="cyango-part"
        ) as pool:
            futures = [pool.submit(upload_one, number) for number in missing]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Stop the other parts. The first error is the one to report.
                stop.stop()
                for future in futures:
                    future.cancel()
                raise

    def _retry(self, operation: Callable[[], Any], cancel_event: Any) -> Any:
        for attempt in range(self.retries + 1):
            _check_cancel(cancel_event)
            try:
                return operation()
            except ApiError as error:
                if attempt >= self.retries or not error.transient or isinstance(error, PartUploadError):
                    raise
                self._wait(attempt, cancel_event, error.retry_after)
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(attempt: int, cancel_event: Any, retry_after: float | None = None) -> None:
        delay = min(0.5 * (2**attempt) + random.random() * 0.25, 5.0)
        if retry_after is not None and retry_after >= 0:
            delay = max(delay, min(retry_after, MAX_RETRY_DELAY))
        if cancel_event.wait(delay):
            raise UploadCancelled("Upload cancelled.")


def upload_file_name(title: str, source_format: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-.")[:60] or "scene"
    return f"{stem}.{source_format}"


def _create_body(job: UploadJob, size: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file_name": upload_file_name(job.title, job.source_format),
        "file_size": size,
        "title": job.title,
    }
    if job.description:
        body["description"] = job.description
    if job.folder_id:
        body["folder_id"] = job.folder_id
    return body


def _open_session(job: UploadJob, session: dict[str, Any], size: int) -> tuple[str, dict[str, str]]:
    upload_id = session.get("upload_id")
    mode = session.get("mode")
    if not isinstance(upload_id, str) or not upload_id or mode not in ("single", "multipart"):
        raise ProtocolError("Cyango returned an invalid upload.")
    if session.get("size") != size:
        raise ProtocolError("Cyango planned the upload for a different file size.")
    job.upload_id = upload_id
    job.upload_created_at = utc_now()
    job.mode = mode
    job.uploaded_parts = []
    if mode == "multipart":
        part_size, part_count = session.get("part_size"), session.get("part_count")
        if not isinstance(part_size, int) or not isinstance(part_count, int) or part_size <= 0:
            raise ProtocolError("Cyango returned an invalid part size.")
        if math.ceil(size / part_size) != part_count:
            raise ProtocolError("Cyango returned a part count that does not match the file size.")
        job.part_size, job.part_count = part_size, part_count
        return "", {}
    url, headers = session.get("url"), session.get("headers")
    if not isinstance(url, str) or not url:
        raise ProtocolError("Cyango returned no upload URL.")
    clean_headers = {str(key): str(value) for key, value in headers.items()} if isinstance(headers, dict) else {}
    return url, clean_headers


def _outcome(result: dict[str, Any]) -> UploadOutcome:
    story_id = result.get("story_id")
    if result.get("status") != "completed" or not isinstance(story_id, str) or not story_id:
        raise ProtocolError("Cyango returned an invalid completed upload.")
    return UploadOutcome(
        asset_id=str(result.get("asset_id") or ""),
        story_id=story_id,
        story_url=str(result.get("story_url") or ""),
    )


def _expired(job: UploadJob) -> bool:
    try:
        created = datetime.fromisoformat(job.upload_created_at)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - created > SESSION_MAX_AGE


def _validate_file(path: Path, expected_size: int) -> int:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ConfigurationError(f"The staged export is missing: {path}") from error
    if size <= 0:
        raise ConfigurationError("The staged export is empty.")
    if expected_size and size != expected_size:
        raise ConfigurationError("The staged export changed after the upload started. Export it again.")
    return size


class _EitherEvent:
    """Set when the user cancels or when stop() is called. Has the Event methods the upload uses."""

    def __init__(self, cancel_event: threading.Event) -> None:
        self._cancel = cancel_event
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._cancel.is_set() or self._stop.is_set()

    def wait(self, timeout: float) -> bool:
        deadline = timeout
        while deadline > 0:
            if self.is_set():
                return True
            step = min(0.1, deadline)
            self._stop.wait(step)
            deadline -= step
        return self.is_set()


def _check_cancel(cancel_event: Any) -> None:
    if cancel_event.is_set():
        raise UploadCancelled("Upload cancelled.")
