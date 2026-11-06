"""Client for the Cyango external publish API. Standard library only."""

from __future__ import annotations

import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import Any
from collections.abc import Callable, Iterator

from .errors import ApiError, ConfigurationError, PartUploadError, ProtocolError, UploadCancelled
from .package_info import USER_AGENT

DEFAULT_BASE_URL = "https://api.cyango.com/external-publish/v1"
TOKEN_PREFIX = "cyp_"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would send the token to another URL. Fail instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _RangedBody:
    """Streams one byte range of a file and reports progress."""

    def __init__(
        self,
        path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.path = path
        self.start = start
        self.size = size
        self.cancel_event = cancel_event
        self.on_bytes = on_bytes
        self.chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        remaining = self.size
        with self.path.open("rb") as stream:
            stream.seek(self.start)
            while remaining:
                if self.cancel_event.is_set():
                    raise UploadCancelled("Upload cancelled.")
                data = stream.read(min(self.chunk_size, remaining))
                if not data:
                    raise ProtocolError(f"The export ended before the expected {self.size} bytes.")
                remaining -= len(data)
                self.on_bytes(len(data))
                yield data


class ApiClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 60.0) -> None:
        token = token.strip()
        if not token:
            raise ConfigurationError("Paste a Cyango external publish token first.")
        if not token.startswith(TOKEN_PREFIX):
            raise ConfigurationError(f"A Cyango external publish token starts with {TOKEN_PREFIX}.")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        _check_url(self.base_url, "API URL")
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def get_me(self) -> dict[str, Any]:
        return _object(self._request("GET", "/me"), "account")

    def list_folders(self) -> list[dict[str, str]]:
        value = _object(self._request("GET", "/folders"), "folder list").get("folders")
        if not isinstance(value, list):
            raise ProtocolError("Cyango returned an invalid folder list.")
        return [
            {"id": str(item["id"]), "name": str(item.get("name") or "")}
            for item in value
            if isinstance(item, dict) and item.get("id")
        ]

    def create_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        return _object(self._request("POST", "/uploads", body), "upload")

    def sign_parts(self, upload_id: str, part_numbers: list[int]) -> dict[int, str]:
        value = _object(
            self._request("POST", "/uploads/parts", {"upload_id": upload_id, "part_numbers": part_numbers}),
            "part signature",
        )
        parts = value.get("parts")
        if not isinstance(parts, list):
            raise ProtocolError("Cyango returned no part URLs.")
        urls: dict[int, str] = {}
        for item in parts:
            if isinstance(item, dict) and isinstance(item.get("part_number"), int) and isinstance(item.get("url"), str):
                urls[item["part_number"]] = item["url"]
        missing = sorted(set(part_numbers) - urls.keys())
        if missing:
            raise ProtocolError(f"Cyango returned no URL for parts {missing}.")
        return urls

    def complete_upload(self, upload_id: str, parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"upload_id": upload_id}
        if parts is not None:
            body["parts"] = parts
        return _object(self._request("POST", "/uploads/complete", body), "completed upload")

    def abort_upload(self, upload_id: str) -> None:
        self._request("POST", "/uploads/abort", {"upload_id": upload_id})

    def put_range(
        self,
        url: str,
        path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
        headers: dict[str, str] | None = None,
    ) -> str:
        """PUTs bytes [start, start + size) to a presigned URL and returns the ETag."""
        _check_url(url, "upload URL")
        request = urllib.request.Request(
            url,
            data=_RangedBody(path, start, size, cancel_event, on_bytes),
            method="PUT",
            headers={**(headers or {}), "Content-Length": str(size), "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return str(response.headers.get("ETag") or "")
        except UploadCancelled:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            raise PartUploadError(
                f"Storage refused the upload ({error.code} {error.reason}).",
                status=error.code,
                url=url,
                retry_after=_retry_after(error.headers),
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PartUploadError(f"Could not reach storage: {getattr(error, 'reason', error)}", url=url) from error

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            payload = _read_json(error)
            message = payload.get("message") if isinstance(payload, dict) else None
            code = payload.get("error") if isinstance(payload, dict) else None
            raise ApiError(
                str(message or f"{error.code} {error.reason}"),
                status=error.code,
                code=str(code or ""),
                url=url,
                retry_after=_retry_after(error.headers),
            ) from error
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
            raise ApiError(f"Could not reach Cyango: {getattr(error, 'reason', error)}", url=url) from error

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(f"Cyango returned invalid JSON for {method} {path}.") from error


def _check_url(value: str, label: str) -> None:
    parsed = urllib.parse.urlparse(value)
    local = parsed.hostname in _LOCAL_HOSTS
    if not parsed.netloc or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
        raise ConfigurationError(f"The {label} must be an HTTPS URL.")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"Cyango returned an invalid {label} response.")
    return value


def _read_json(error: urllib.error.HTTPError) -> Any:
    try:
        raw = error.read()
        return json.loads(raw.decode("utf-8")) if raw else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        error.close()


def _retry_after(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isdigit():
        delay = float(value)
        return delay if math.isfinite(delay) else None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
