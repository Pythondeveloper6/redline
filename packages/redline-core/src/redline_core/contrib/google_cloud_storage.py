"""Google Cloud Storage blob uploads and downloads as spans.

Call :func:`instrument_google_cloud_storage` once at startup. Each ``Blob`` upload or
download call becomes one ``custom`` span with ``client="gcs"``, ``op`` ``upload`` or
``download`` and the **bucket** as target. Object names, contents, URLs and credentials are
never captured; the size is recorded when the uploaded data or downloaded result is
already bytes or text. The library is imported only when this function runs.
"""

from __future__ import annotations

from typing import Any

from redline_core.extending import instrument_client_method, payload_size

__all__ = ["instrument_google_cloud_storage", "uninstrument_google_cloud_storage"]

_originals: dict[str, Any] = {}
_UPLOADS = ("upload_from_string", "upload_from_file", "upload_from_filename")
_DOWNLOADS = (
    "download_as_bytes",
    "download_as_string",
    "download_as_text",
    "download_to_file",
    "download_to_filename",
)


def _bucket(self: Any, args: Any, kwargs: Any) -> str | None:
    name = getattr(getattr(self, "bucket", None), "name", None)
    return name if isinstance(name, str) else None


def _upload_bytes(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    return payload_size(args[0] if args else kwargs.get("data"))


def instrument_google_cloud_storage() -> bool:
    """Patch ``google.cloud.storage.Blob``; ``False`` if the library is missing."""
    if _originals:
        return True
    try:
        from google.cloud.storage import Blob
    except ImportError:
        return False
    for name in _UPLOADS:
        if hasattr(Blob, name):
            _originals[name] = instrument_client_method(
                Blob,
                name,
                client="gcs",
                operation="upload",
                target=_bucket,
                payload_bytes=_upload_bytes if name == "upload_from_string" else None,
            )
    for name in _DOWNLOADS:
        if hasattr(Blob, name):
            _originals[name] = instrument_client_method(
                Blob,
                name,
                client="gcs",
                operation="download",
                target=_bucket,
                result_bytes=payload_size,
            )
    return True


def uninstrument_google_cloud_storage() -> None:
    """Undo :func:`instrument_google_cloud_storage`."""
    for restore in list(_originals.values()):
        restore()
    _originals.clear()
