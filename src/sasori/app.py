from __future__ import annotations

import importlib
import inspect
import re

from .runtime import Harness
from .sqlite_store import SQLiteStore


_APP_SPEC = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z"
)


class AppLoadError(Exception):
    pass


def load_harness(
    spec: object,
    store: SQLiteStore,
    *,
    app_id: str | None = None,
) -> Harness:
    """Load trusted installed Python code using ``module:factory``."""
    if not isinstance(spec, str) or _APP_SPEC.fullmatch(spec) is None:
        raise AppLoadError("app must use module:factory syntax")
    module_name, factory_name = spec.split(":", 1)
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError) as exc:
        raise AppLoadError("app factory could not be imported") from exc
    if not callable(factory):
        raise AppLoadError("app factory must be callable")
    try:
        parameters = inspect.signature(factory).parameters.values()
        accepts_app_id = any(
            (
                parameter.name == "app_id"
                and parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            )
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_app_id = False
    try:
        harness = (
            factory(store, app_id=app_id)
            if app_id is not None and accepts_app_id
            else factory(store)
        )
    except Exception as exc:
        raise AppLoadError(f"app factory failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(harness, Harness):
        raise AppLoadError("app factory must return Harness")
    if harness.store is not store:
        raise AppLoadError("app factory must bind the supplied SQLiteStore")
    return harness


__all__ = ["AppLoadError", "load_harness"]
