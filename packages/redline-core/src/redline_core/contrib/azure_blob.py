"""Azure Blob Storage ``BlobClient`` uploads, downloads and deletes as spans.

Call :func:`instrument_azure_blob` once at startup. Each ``upload_blob``,
``download_blob`` and ``delete_blob`` call (sync, and ``azure.storage.blob.aio`` when it is
importable) becomes one ``custom`` span with ``client="azure_blob"``, ``op`` ``upload``,
``download`` or ``delete`` and the **container** as target. Blob names, contents, URLs,
SAS tokens and credentials are never captured; the upload size is recorded when the data
is already bytes or text. For ``download_blob`` the span covers starting the download; the
returned stream is read later by the caller. The library is imported only when this
function runs.
"""

from __future__ import annotations

from typing import Any

from redline_core.extending import instrument_client_method, payload_size

__all__ = ["instrument_azure_blob", "uninstrument_azure_blob"]

_originals: dict[str, Any] = {}
_OPS = {"upload_blob": "upload", "download_blob": "download", "delete_blob": "delete"}


def _container(self: Any, args: Any, kwargs: Any) -> str | None:
    name = getattr(self, "container_name", None)
    return name if isinstance(name, str) else None


def _upload_bytes(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    return payload_size(args[0] if args else kwargs.get("data"))


def _patch(cls: Any, label: str) -> None:
    for name, op in _OPS.items():
        if hasattr(cls, name):
            _originals[f"{label}.{name}"] = instrument_client_method(
                cls,
                name,
                client="azure_blob",
                operation=op,
                target=_container,
                payload_bytes=_upload_bytes if op == "upload" else None,
            )


def instrument_azure_blob() -> bool:
    """Patch ``azure.storage.blob.BlobClient``; ``False`` if the library is missing."""
    if _originals:
        return True
    try:
        from azure.storage.blob import BlobClient
    except ImportError:
        return False
    _patch(BlobClient, "sync")
    try:
        from azure.storage.blob.aio import BlobClient as AsyncBlobClient
    except ImportError:  # the aio extra (aiohttp) is not installed
        pass
    else:
        _patch(AsyncBlobClient, "aio")
    return True


def uninstrument_azure_blob() -> None:
    """Undo :func:`instrument_azure_blob`."""
    for restore in list(_originals.values()):
        restore()
    _originals.clear()
