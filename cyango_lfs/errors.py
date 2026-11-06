"""Errors the panel shows to the user."""

from __future__ import annotations


class CyangoError(Exception):
    """Base class for user-facing plugin errors."""


class ConfigurationError(CyangoError):
    """The plugin or the upload request is not set up correctly."""


class ProtocolError(CyangoError):
    """Cyango returned a response the plugin does not understand."""


class UploadCancelled(CyangoError):
    """The user cancelled the export or the upload."""


class ApiError(CyangoError):
    """An HTTP request failed."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        url: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.url = url
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status is None or self.status in {408, 425, 429} or 500 <= self.status <= 599


class PartUploadError(ApiError):
    """A presigned PUT to storage failed."""

    @property
    def transient(self) -> bool:
        # 403: the presigned URL expired. A new URL fixes it.
        return super().transient or self.status == 403
