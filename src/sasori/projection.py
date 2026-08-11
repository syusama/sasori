from __future__ import annotations

import json

from sasori_core.projection import (
    ProjectionIntegrityError,
    event_projection,
    run_list_projection as _core_run_list_projection,
    run_projection,
    validate_run_id,
)

from .sqlite_store import SQLiteStore


MAX_PUBLIC_PROJECTION_EXTENSION_BYTES = 256 * 1024
_MISSING = object()


def _validated_projection_extension(
    extension: object, core: dict[str, object]
) -> dict[str, object]:
    """Freeze one product-owned extension and validate it at its boundary."""

    if type(extension) is not dict:
        raise ProjectionIntegrityError
    try:
        encoded = json.dumps(
            extension,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise ProjectionIntegrityError from None
    if len(encoded) > MAX_PUBLIC_PROJECTION_EXTENSION_BYTES:
        raise ProjectionIntegrityError
    decoded = json.loads(encoded)
    if type(decoded) is not dict:
        raise ProjectionIntegrityError

    # The batteries-included composer knows how to route its current Workflow
    # extension, but all Workflow fields and semantic rules live in sasori_flow.
    from sasori_flow.projection import validate_workflow_projection_extension

    validate_workflow_projection_extension(decoded, core)
    return decoded


def compose_run_projection(
    store: SQLiteStore, run_id: str, harness: object | None = None
) -> dict[str, object]:
    """Compose the immutable core run view with one validated public extension."""

    core = run_projection(store, run_id)
    if harness is None:
        return core
    try:
        projector = getattr(harness, "public_projection_extension", _MISSING)
        if projector is _MISSING:
            return core
        if not callable(projector):
            raise ProjectionIntegrityError
        extension = projector(run_id)
        validated = _validated_projection_extension(extension, core)
    except ProjectionIntegrityError:
        raise
    except Exception:
        raise ProjectionIntegrityError from None
    return {**core, **validated}


def run_list_projection(
    store: SQLiteStore,
    *,
    limit: int = 50,
    before: int | None = None,
    app_id: str | None = None,
) -> dict[str, object]:
    return _core_run_list_projection(
        store, limit=limit, before=before, app_id=app_id
    )


__all__ = [
    "MAX_PUBLIC_PROJECTION_EXTENSION_BYTES",
    "ProjectionIntegrityError",
    "compose_run_projection",
    "event_projection",
    "run_list_projection",
    "run_projection",
    "validate_run_id",
]
