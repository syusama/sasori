from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Coroutine, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from sasori_artifacts import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactError,
    ArtifactInvalid,
    ArtifactNotFound,
    ArtifactPayload,
    ArtifactStore,
    artifact_projection,
    validate_artifact_id,
)
from sasori_flow import (
    SavedWorkflowCatalog,
    SavedWorkflowDetail,
    WorkflowCatalogConfigurationError,
    WorkflowCatalogError,
    WorkflowCatalogExists,
    WorkflowCatalogIntegrityError,
    WorkflowCatalogNotFound,
    WorkflowCatalogRevisionMismatch,
    WorkflowCatalogStore,
    WorkflowCatalogValidationError,
    catalog_etag,
    validate_catalog_id,
)

from .app import AppLoadError, load_harness
from .contracts import Message, Tool, is_valid_app_id
from .projection import (
    compose_run_projection,
    event_projection,
    run_list_projection,
    validate_run_id,
)
from .runtime import (
    DuplicateToolCallError,
    Harness,
    MaxStepsExceeded,
    ModelCallError,
    ModelTimeoutError,
    RunCancelled,
    RunPaused,
    SasoriError,
)
from .sqlite_store import (
    ApprovalConflict,
    ApprovalMismatch,
    RunAlreadyExists,
    RunNotFound,
    SQLiteStore,
    StoreError,
)


_MAX_BODY = 1024 * 1024
_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
_RUN_PATH = re.compile(r"/v1/runs/([^/]+)(?:/(resume|approval|effect|events))?\Z")
_ARTIFACT_PATH = re.compile(
    r"/v1/runs/([^/]+)/artifacts(?:/([^/]+)/content)?\Z"
)
_WORKFLOW_CATALOG_PATH = re.compile(r"/v1/workflows/(wfcat_[0-9a-f]{32})\Z")
_WORKFLOW_ETAG = re.compile(
    r'"sasori-wfcat-([0-9a-f]{32})-r([1-9][0-9]*)-([0-9a-f]{64})"\Z'
)
_WORKBENCH_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/assets/app.0.1.0.css": ("app.0.1.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.2.0.css": ("app.0.2.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/artifacts.0.1.0.css": ("artifacts.0.1.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.1.1.js": ("app.0.1.1.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/event-reducer.0.1.0.js": ("event-reducer.0.1.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.1.2.js": ("app.0.1.2.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.1.3.js": ("app.0.1.3.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.1.4.js": ("app.0.1.4.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/app.0.2.0.js": ("app.0.2.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow.0.1.0.css": ("workflow.0.1.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow.0.1.0.js": ("workflow.0.1.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow.0.2.0.js": ("workflow.0.2.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow-manifest.0.1.0.js": ("workflow-manifest.0.1.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow-studio.0.1.0.css": ("workflow-studio.0.1.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow-studio.0.1.0.js": ("workflow-studio.0.1.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow-studio.0.2.0.css": ("workflow-studio.0.2.0.css", "text/css; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/workflow-studio.0.2.0.js": ("workflow-studio.0.2.0.js", "text/javascript; charset=utf-8", "public, max-age=31536000, immutable"),
    "/assets/mark.0.1.0.svg": ("mark.0.1.0.svg", "image/svg+xml", "public, max-age=31536000, immutable"),
}
_WORKBENCH_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class ServerConfigurationError(Exception):
    pass


class RuntimeBusy(Exception):
    pass


class WorkflowCatalogOutcomeUnknown(Exception):
    def __init__(self, catalog_id: str) -> None:
        self.catalog_id = catalog_id
        super().__init__(
            "saved Workflow mutation outcome is unknown; "
            "reconcile with a read-only GET"
        )


class ServerShuttingDown(Exception):
    pass


class ServerShutdownIncomplete(ServerConfigurationError):
    pass


class InvalidTransition(Exception):
    pass


class InvalidRequest(Exception):
    pass


class WorkflowPreflightRejected(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class WorkflowCatalogPreconditionRequired(Exception):
    pass


class InvalidArtifactRange(Exception):
    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__("artifact byte range is invalid or unsatisfiable")


class AppNotFound(Exception):
    pass


class AppUnavailable(Exception):
    def __init__(self, app_id: str, reason_code: str) -> None:
        self.app_id = app_id
        self.reason_code = reason_code
        super().__init__(f"application {app_id} is unavailable")


class AppBindingMissing(Exception):
    pass


def _strict_json(value: bytes) -> object:
    def invalid_constant(token: str) -> object:
        raise ValueError(f"invalid JSON constant: {token}")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(
        value.decode("utf-8"),
        parse_constant=invalid_constant,
        object_pairs_hook=pairs,
    )


def _bounded_workflow_error(exc: Exception, fallback: str) -> str:
    try:
        message = str(exc)
        encoded = message.encode("utf-8", "strict")
    except (UnicodeEncodeError, UnicodeError):
        return fallback
    if not message or len(encoded) > 512:
        return fallback
    return message


def _workflow_database_path(database: str, configured: str | Path | None) -> str:
    if configured is not None:
        if not isinstance(configured, (str, Path)) or not str(configured):
            raise ServerConfigurationError("Workflow catalog database path is invalid")
        value = str(configured)
    elif database == ":memory:":
        value = ":memory:"
    else:
        source = Path(database)
        suffix = source.suffix or ".sqlite3"
        value = str(source.with_name(f"{source.stem}.workflows{suffix}"))
    if database != ":memory:" and value != ":memory:" and (
        Path(database).resolve() == Path(value).resolve()
    ):
        raise ServerConfigurationError(
            "run and Workflow catalog databases must be different files"
        )
    return value


def _loopback(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_origin(origin: object) -> bool:
    if not isinstance(origin, str) or origin == "*" or "\r" in origin or "\n" in origin:
        return False
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    )


class _Owner:
    def __init__(
        self,
        database: str,
        app: str | Mapping[str, str],
        artifact_root: str | Path | None = None,
        publish_final_artifact: bool = False,
        workflow_database: str | Path | None = None,
    ) -> None:
        self.database = database
        self.workflow_database = _workflow_database_path(database, workflow_database)
        if artifact_root is None:
            if database == ":memory:":
                raise ServerConfigurationError(
                    "artifact_root is required with an in-memory run database"
                )
            artifact_root = Path(database).resolve().with_suffix(".artifacts")
        self.artifact_root = Path(artifact_root)
        self.publish_final_artifact = publish_final_artifact
        if isinstance(app, str):
            from sasori_apps.registry import app_id_for_spec

            self.apps = {app_id_for_spec(app) or "default": app}
        elif isinstance(app, Mapping) and app:
            self.apps = dict(app)
        else:
            raise ServerConfigurationError("at least one application is required")
        if any(
            not isinstance(app_id, str)
            or not is_valid_app_id(app_id)
            or not isinstance(spec, str)
            or not spec
            for app_id, spec in self.apps.items()
        ):
            raise ServerConfigurationError("application IDs or factories are invalid")
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._store: SQLiteStore | None = None
        self._workflow_store: WorkflowCatalogStore | None = None
        self._saved_workflows: SavedWorkflowCatalog | None = None
        self._artifacts: ArtifactStore | None = None
        self._harnesses: dict[str, Harness] = {}
        self._unavailable: dict[str, str] = {}
        self._workflow_tools: tuple[Tool, ...] = ()
        self._gate: asyncio.Lock | None = None
        self._error: BaseException | None = None
        self._state = "OPEN"
        self._state_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._running = threading.Event()
        self._closed = threading.Event()
        self._active_drive: asyncio.Task[object] | None = None
        self._calls: set[concurrent.futures.Future[Any]] = set()
        self._shutdown_future: concurrent.futures.Future[None] | None = None

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def start(self, timeout: float = 15.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            self._mark_closing()
            if self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except RuntimeError:
                    pass
            raise ServerConfigurationError("runtime owner did not start")
        if self._error is not None:
            raise ServerConfigurationError(
                f"runtime owner failed: {type(self._error).__name__}: {self._error}"
            ) from self._error
        if self.state != "OPEN":
            raise ServerConfigurationError("runtime owner stopped during startup")

    def _mark_closing(self) -> None:
        with self._state_lock:
            if self._state == "OPEN":
                self._state = "CLOSING"
            self._stop_requested.set()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            try:
                self._workflow_store = WorkflowCatalogStore(self.workflow_database)
                self._store = SQLiteStore(self.database)
                self._artifacts = ArtifactStore(self._store, self.artifact_root)
                first_error: BaseException | None = None
                for app_id, spec in self.apps.items():
                    try:
                        self._harnesses[app_id] = load_harness(
                            spec, self._store, app_id=app_id
                        )
                    except BaseException as exc:
                        first_error = first_error or exc
                        self._unavailable[app_id] = "app_factory_failed"
                if not self._harnesses:
                    if len(self.apps) == 1 and first_error is not None:
                        raise first_error
                    raise ServerConfigurationError("no configured application could start")
                from sasori_apps.registry import workflow_preflight_tools

                self._workflow_tools = workflow_preflight_tools(self._harnesses)
                self._saved_workflows = SavedWorkflowCatalog(
                    self._workflow_store, self._workflow_tools
                )
                if self.publish_final_artifact:
                    self._reconcile_artifacts()
                self._gate = asyncio.Lock()
            except BaseException as exc:
                self._error = exc
                return
            if self._stop_requested.is_set():
                return
            self._running.set()
            self._ready.set()
            if self._stop_requested.is_set():
                return
            loop.run_forever()
        finally:
            self._running.clear()
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                try:
                    if self._artifacts is not None:
                        self._artifacts.close()
                finally:
                    try:
                        if self._store is not None:
                            self._store.close()
                    finally:
                        try:
                            if self._workflow_store is not None:
                                self._workflow_store.close()
                        finally:
                            loop.close()
                            with self._state_lock:
                                self._state = "CLOSED"
                            self._ready.set()
                            self._closed.set()

    def call(self, operation: Coroutine[Any, Any, Any], timeout: float | None = None):
        with self._state_lock:
            if self._state != "OPEN":
                operation.close()
                raise ServerShuttingDown("runtime owner is shutting down")
            if (
                self._loop is None
                or self._error is not None
                or not self._thread.is_alive()
                or not self._running.is_set()
            ):
                operation.close()
                raise ServerConfigurationError("runtime owner is unavailable")
            try:
                future = asyncio.run_coroutine_threadsafe(operation, self._loop)
            except RuntimeError:
                operation.close()
                raise ServerShuttingDown("runtime owner is shutting down") from None
            self._calls.add(future)
        future.add_done_callback(self._call_finished)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RuntimeBusy("runtime owner did not respond") from None
        except concurrent.futures.CancelledError:
            if self.state != "OPEN":
                raise ServerShuttingDown("runtime owner is shutting down") from None
            raise RuntimeBusy("runtime operation was cancelled") from None

    def _call_finished(self, future: concurrent.futures.Future[Any]) -> None:
        with self._state_lock:
            self._calls.discard(future)

    async def _exclusive(self, operation):
        assert self._gate is not None
        if self.state != "OPEN":
            raise ServerShuttingDown("runtime owner is shutting down")
        if self._gate.locked():
            raise RuntimeBusy("another runtime mutation is active")
        await self._gate.acquire()
        current = asyncio.current_task()
        self._active_drive = current
        try:
            return await operation()
        finally:
            if self._active_drive is current:
                self._active_drive = None
            self._gate.release()

    def _selected(self, app_id: str | None) -> tuple[str, Harness]:
        if app_id is None:
            if len(self.apps) != 1:
                raise InvalidRequest("app_id is required when multiple applications are configured")
            app_id = next(iter(self.apps))
        if app_id not in self.apps:
            raise AppNotFound(app_id)
        harness = self._harnesses.get(app_id)
        if harness is None:
            raise AppUnavailable(app_id, self._unavailable.get(app_id, "app_factory_failed"))
        return app_id, harness

    def _for_run(self, run_id: str) -> tuple[str, Harness]:
        assert self._store is not None
        snapshot = self._store.load(run_id)
        app_id = snapshot.app_id
        if app_id is None:
            if len(self.apps) != 1:
                raise AppBindingMissing("run has no immutable application binding")
            app_id = next(iter(self.apps))
        return self._selected(app_id)

    def _run_projection(
        self, run_id: str, harness: Harness | None = None
    ) -> dict[str, object]:
        assert self._store is not None
        if harness is None:
            snapshot = self._store.load(run_id)
            if snapshot.app_id is not None:
                harness = self._harnesses.get(snapshot.app_id)
        return compose_run_projection(self._store, run_id, harness)

    def _materialize_final_artifact(self, run_id: str) -> None:
        assert self._store is not None and self._artifacts is not None
        if not self.publish_final_artifact:
            return
        snapshot = self._store.load(run_id)
        if snapshot.status != "completed" or snapshot.final_message is None:
            return
        stored = self._store.stored_events(run_id)
        terminal_seq = next(
            (
                item.seq
                for item in reversed(stored)
                if item.event.type == "run.completed"
            ),
            0,
        )
        if terminal_seq == 0:
            raise StoreError("completed run has no durable run.completed event")
        app_id = snapshot.app_id or "unbound"
        content = (
            "# Sasori run result\n\n"
            f"- Run: `{run_id}`\n"
            f"- Application: `{app_id}`\n"
            f"- Loop terminal event cursor: `{terminal_seq}`\n\n"
            "## Final message\n\n"
            f"{snapshot.final_message.content}\n"
        ).encode("utf-8")
        artifact_id = "result-" + hashlib.sha256(
            f"sasori-final-result-v1\0{run_id}".encode("utf-8")
        ).hexdigest()[:32]
        self._artifacts.put(
            run_id,
            content,
            artifact_id=artifact_id,
            declared_filename=f"{run_id}-result.md",
            declared_media_type="text/markdown",
        )

    def _reconcile_artifacts(self) -> None:
        assert self._store is not None and self._artifacts is not None
        before = None
        while True:
            rows = self._store.list_runs(limit=100, before=before)
            for _, snapshot in rows:
                if snapshot.status == "completed":
                    self._materialize_final_artifact(snapshot.run_id)
            if len(rows) < 100:
                return
            before = rows[-1][0]

    async def run(
        self, prompt: str, run_id: str | None, app_id: str | None = None
    ) -> tuple[int, dict[str, object]]:
        assert self._store is not None
        selected_id, harness = self._selected(app_id)

        async def drive():
            try:
                result = await harness.run(
                    (Message("user", prompt),), run_id=run_id, app_id=selected_id
                )
                self._materialize_final_artifact(result.run_id)
                return 200, self._run_projection(result.run_id, harness)
            except RunPaused as paused:
                return 202, self._run_projection(paused.run_id, harness)

        return await self._exclusive(drive)

    async def resume(self, run_id: str) -> tuple[int, dict[str, object]]:
        assert self._store is not None
        _, harness = self._for_run(run_id)

        async def drive():
            state = self._store.load(run_id).status
            if state in {"completed", "failed", "cancelled"}:
                raise InvalidTransition(f"run is {state} and cannot resume")
            try:
                await harness.resume(run_id)
                self._materialize_final_artifact(run_id)
                return 200, self._run_projection(run_id, harness)
            except RunPaused:
                return 202, self._run_projection(run_id, harness)
            except RunCancelled as exc:
                raise InvalidTransition(str(exc)) from None

        return await self._exclusive(drive)

    async def approval(
        self, run_id: str, fingerprint: str, approved: bool
    ) -> dict[str, object]:
        assert self._store is not None
        _, harness = self._for_run(run_id)

        async def mutate():
            harness.resolve_approval(run_id, fingerprint, approved)
            return self._run_projection(run_id, harness)

        return await self._exclusive(mutate)

    async def effect(
        self,
        run_id: str,
        fingerprint: str,
        action: str,
        reason: str,
        result: object,
    ) -> dict[str, object]:
        assert self._store is not None
        _, harness = self._for_run(run_id)

        async def mutate():
            try:
                harness.resolve_effect(
                    run_id, fingerprint, action, reason=reason, result=result
                )
            except ValueError as exc:
                raise InvalidTransition(str(exc)) from None
            return self._run_projection(run_id, harness)

        return await self._exclusive(mutate)

    async def status(self, run_id: str) -> dict[str, object]:
        assert self._store is not None
        return self._run_projection(run_id)

    async def events(self, run_id: str, after: int) -> list[dict[str, object]]:
        assert self._store is not None
        self._store.load(run_id)
        return [
            event_projection(item) for item in self._store.stored_events(run_id, after)
        ]

    async def catalog(self) -> dict[str, object]:
        from sasori_apps.registry import application_surface_catalog

        return application_surface_catalog(self._harnesses, self._unavailable)

    async def workflow_preflight(
        self, definition: dict[str, object]
    ) -> dict[str, object]:
        from sasori_apps.registry import (
            WorkflowPreflightFailure,
            workflow_preflight_definition,
        )

        try:
            manifest = workflow_preflight_definition(
                definition, self._workflow_tools
            )
        except WorkflowPreflightFailure as exc:
            fallback = {
                "invalid_definition": "workflow definition was rejected",
                "tool_contract_mismatch": "workflow Tool contract was rejected",
                "manifest_rejected": "workflow manifest was rejected",
            }[exc.reason_code]
            raise WorkflowPreflightRejected(
                exc.reason_code,
                _bounded_workflow_error(exc, fallback),
            ) from None
        return {"ok": True, "schema_version": 1, "manifest": manifest}

    @staticmethod
    def _workflow_record(detail: SavedWorkflowDetail) -> dict[str, object]:
        return {
            "ok": True,
            "schema_version": 1,
            "record": detail.as_data(),
        }

    async def saved_workflow_list(
        self, limit: int, before: int | None
    ) -> dict[str, object]:
        assert self._saved_workflows is not None
        return self._saved_workflows.list(limit, before).as_data()

    async def saved_workflow_get(
        self, catalog_id: str, revision: int | None
    ) -> tuple[dict[str, object], str]:
        assert self._saved_workflows is not None
        detail = self._saved_workflows.get(catalog_id, revision)
        return self._workflow_record(detail), catalog_etag(detail.record)

    async def saved_workflow_put(
        self,
        catalog_id: str,
        definition: dict[str, object],
        *,
        create: bool,
        expected_revision: int | None = None,
        expected_definition_sha256: str | None = None,
    ) -> tuple[int, dict[str, object], str]:
        assert self._saved_workflows is not None
        try:
            if create:
                record = self._saved_workflows.create(catalog_id, definition)
                status = 201
            else:
                assert expected_revision is not None
                assert expected_definition_sha256 is not None
                record, _changed = self._saved_workflows.update(
                    catalog_id,
                    expected_revision,
                    expected_definition_sha256,
                    definition,
                )
                status = 200
        except WorkflowCatalogValidationError as exc:
            raise WorkflowPreflightRejected(
                exc.reason_code,
                _bounded_workflow_error(exc, "workflow definition was rejected"),
            ) from None
        detail = SavedWorkflowDetail(record, record.catalog_revision, "compatible", None)
        return status, self._workflow_record(detail), catalog_etag(record)

    async def history(
        self, limit: int, before: int | None, app_id: str | None
    ) -> dict[str, object]:
        assert self._store is not None
        if app_id is not None and app_id not in self.apps:
            raise AppNotFound(app_id)
        return run_list_projection(
            self._store, limit=limit, before=before, app_id=app_id
        )

    async def artifacts(self, run_id: str) -> dict[str, object]:
        assert self._store is not None and self._artifacts is not None
        self._store.load(run_id)
        return {
            "run_id": run_id,
            "artifacts": [
                artifact_projection(ref) for ref in self._artifacts.list(run_id)
            ],
        }

    async def artifact(self, run_id: str, artifact_id: str) -> ArtifactPayload:
        assert self._store is not None and self._artifacts is not None
        self._store.load(run_id)
        return self._artifacts.get(run_id, artifact_id)

    async def publish_artifact(
        self,
        run_id: str,
        content: bytes,
        *,
        filename: str,
        declared_media_type: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, object]:
        """Trusted host hook; HTTP clients cannot publish arbitrary bytes."""
        assert self._store is not None and self._artifacts is not None

        async def mutate():
            self._store.load(run_id)
            ref = self._artifacts.put(
                run_id,
                content,
                declared_filename=filename,
                declared_media_type=declared_media_type,
                artifact_id=artifact_id,
            )
            return artifact_projection(ref)

        return await self._exclusive(mutate)

    async def _settle(self, grace: float) -> None:
        current = asyncio.current_task()
        active = self._active_drive
        if active is not None and active is not current and not active.done():
            done, _ = await asyncio.wait((active,), timeout=grace)
            if not done:
                active.cancel()
                try:
                    await active
                except BaseException:
                    pass
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)

    def _settled(self, future: concurrent.futures.Future[None]) -> None:
        if future.cancelled():
            return
        try:
            error = future.exception()
        except BaseException:
            return
        if error is not None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass

    def close(self, grace: float = 15.0, hard_timeout: float | None = None) -> None:
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("shutdown grace must be finite and non-negative")
        hard = max(grace * 2, grace + 1.0) if hard_timeout is None else hard_timeout
        if not math.isfinite(hard) or hard <= 0:
            raise ValueError("shutdown hard timeout must be finite and positive")
        deadline = time.monotonic() + hard
        self._mark_closing()
        if self.state == "CLOSED" or not self._thread.is_alive():
            return
        loop = self._loop
        if loop is None or not self._running.is_set():
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise ServerShutdownIncomplete(
                    "runtime owner startup did not settle before the hard deadline"
                )
            return

        with self._state_lock:
            future = self._shutdown_future
            if future is None:
                operation = self._settle(grace)
                try:
                    future = asyncio.run_coroutine_threadsafe(operation, loop)
                except RuntimeError:
                    operation.close()
                    future = None
                else:
                    self._shutdown_future = future
                    future.add_done_callback(self._settled)
        if future is None:
            raise ServerShutdownIncomplete("runtime owner shutdown could not be scheduled")
        try:
            future.result(max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            raise ServerShutdownIncomplete(
                "runtime owner did not settle before the hard deadline; work may still be running"
            ) from None
        except concurrent.futures.CancelledError:
            raise ServerShutdownIncomplete("runtime owner shutdown was cancelled") from None
        except BaseException as exc:
            raise ServerShutdownIncomplete(
                f"runtime owner shutdown failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._thread.join(max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive() or not self._closed.is_set():
            raise ServerShutdownIncomplete(
                "runtime owner thread did not close before the hard deadline"
            )


class SasoriHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        owner: _Owner,
        *,
        token: str | None,
        cors_origins: Sequence[str],
        sse_max_seconds: float,
        sse_keepalive_seconds: float,
        max_connections: int = 32,
    ) -> None:
        self.owner = owner
        self.token = token
        self.cors_origins = frozenset(cors_origins)
        self.sse_max_seconds = sse_max_seconds
        self.sse_keepalive_seconds = sse_keepalive_seconds
        self._connections = threading.BoundedSemaphore(max_connections)
        self._closing = threading.Event()
        self._handlers = threading.Condition()
        self._active_handlers = 0
        self.handler_drain_seconds = 15.0
        self.request_timeout_seconds = 10.0
        super().__init__(address, SasoriRequestHandler)

    def process_request(self, request, client_address) -> None:
        if self._closing.is_set():
            self._reject_closing(request)
            return
        if not self._connections.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        with self._handlers:
            if self._closing.is_set():
                self._connections.release()
                self._reject_closing(request)
                return
            self._active_handlers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._handlers:
                self._active_handlers -= 1
                self._handlers.notify_all()
            self._connections.release()
            raise

    def _reject_closing(self, request) -> None:
        payload = (
            b'{"ok":false,"error":{"code":"server_shutting_down",'
            b'"message":"server is shutting down","retryable":true}}'
        )
        try:
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()
            with self._handlers:
                self._active_handlers -= 1
                self._handlers.notify_all()

    def shutdown(self) -> None:
        self._closing.set()
        super().shutdown()

    def server_close(self) -> None:
        self._closing.set()
        failure: BaseException | None = None
        try:
            super().server_close()
        except BaseException as exc:
            failure = exc
        try:
            self.owner.close()
        except BaseException as exc:
            failure = failure or exc
        deadline = time.monotonic() + self.handler_drain_seconds
        with self._handlers:
            while self._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = failure or ServerShutdownIncomplete(
                        "HTTP handlers did not drain before the hard deadline"
                    )
                    break
                self._handlers.wait(remaining)
        if failure is not None:
            raise failure


class SasoriRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Sasori/0.1"

    @property
    def sasori(self) -> SasoriHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self._response_started = False
        self.connection.settimeout(self.sasori.request_timeout_seconds)

    def _cors_origin(self) -> str | None:
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return None
        if len(origins) != 1:
            return ""
        origin = origins[0]
        return origin if origin in self.sasori.cors_origins else ""

    def _same_origin(self, origin: str) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or "\r" in hosts[0] or "\n" in hosts[0]:
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and parsed.netloc == hosts[0]
        )

    def _send_headers(
        self,
        status: int,
        content_type: str,
        length: int | None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if not extra or "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        if self.close_connection and (not extra or "Connection" not in extra):
            self.send_header("Connection", "close")
        self.end_headers()

    def _json(self, status: int, value: object, extra: Mapping[str, str] | None = None) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(payload), extra)
        self.wfile.write(payload)

    def _workbench_asset(self, path: str, query: str) -> bool:
        asset = _WORKBENCH_ASSETS.get(path)
        if asset is None or query:
            return False
        filename, content_type, cache_control = asset
        try:
            payload = files("sasori_web").joinpath(filename).read_bytes()
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            self._error(503, "workbench_unavailable", "Workbench assets are unavailable")
            return True
        headers = {
            **_WORKBENCH_SECURITY_HEADERS,
            "Cache-Control": cache_control,
        }
        self._send_headers(200, content_type, len(payload), headers)
        self.wfile.write(payload)
        return True

    def _error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        catalog_id: str | None = None,
        retryable: bool = False,
        reason_code: str | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        error: dict[str, object] = {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
        if run_id is not None:
            error["run_id"] = run_id
        if catalog_id is not None:
            error["catalog_id"] = catalog_id
        if reason_code is not None:
            error["reason_code"] = reason_code
        self._json(status, {"ok": False, "error": error}, extra)

    def _close_error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        self.close_connection = True
        headers = dict(extra or {})
        headers["Connection"] = "close"
        self._error(
            status,
            code,
            message,
            retryable=retryable,
            extra=headers,
        )

    def _server_closing(self) -> bool:
        if not self.sasori._closing.is_set():
            return False
        self._close_error(
            503,
            "server_shutting_down",
            "server is shutting down",
            retryable=True,
        )
        return True

    def _authorized(self) -> bool:
        if self._server_closing():
            return False
        try:
            path = urllib.parse.urlsplit(self.path).path
        except ValueError:
            self._close_error(422, "invalid_request", "request path is invalid")
            return False
        if not path.startswith("/v1/"):
            return True
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self._close_error(400, "invalid_header", "Origin must not be repeated")
            return False
        if origins and (
            origins[0] not in self.sasori.cors_origins
            and not self._same_origin(origins[0])
        ):
            self._close_error(
                403, "origin_forbidden", "request origin is not allowed"
            )
            return False
        token = self.sasori.token
        if token is None:
            return True
        values = self.headers.get_all("Authorization", [])
        if len(values) > 1:
            self._close_error(
                400, "invalid_header", "Authorization must not be repeated"
            )
            return False
        value = values[0] if values else ""
        expected = f"Bearer {token}"
        if not hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
            self._close_error(
                401,
                "unauthorized",
                "bearer authentication is required",
                extra={"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def _body(self) -> object | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._close_error(
                411, "length_required", "chunked request bodies are unsupported"
            )
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._close_error(411, "length_required", "Content-Length is required")
            return None
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            self._close_error(400, "invalid_length", "Content-Length is invalid")
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            self._close_error(400, "invalid_length", "Content-Length is invalid")
            return None
        if length > _MAX_BODY:
            self._close_error(413, "body_too_large", "request body exceeds 1 MiB")
            return None
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) > 1:
            self._close_error(400, "invalid_header", "Content-Type must not be repeated")
            return None
        content_type = content_types[0] if content_types else ""
        parts = [part.strip().lower() for part in content_type.split(";")]
        if not parts or parts[0] != "application/json" or any(
            part and part != "charset=utf-8" for part in parts[1:]
        ):
            self._close_error(
                415,
                "unsupported_media_type",
                "application/json utf-8 is required",
            )
            return None
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self._close_error(408, "request_timeout", "request body timed out")
            return None
        except OSError:
            self.close_connection = True
            return None
        if len(raw) != length:
            self._close_error(
                400, "incomplete_body", "request body ended before Content-Length"
            )
            return None
        try:
            value = _strict_json(raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
            self._error(400, "malformed_json", "request body is not valid JSON")
            return None
        if not isinstance(value, dict):
            self._error(422, "invalid_body", "request body must be a JSON object")
            return None
        return value

    @staticmethod
    def _fields(body: dict[str, object], allowed: set[str]) -> None:
        unknown = set(body).difference(allowed)
        if unknown:
            raise InvalidRequest("request contains unknown fields")

    def _path(self) -> tuple[str, str | None] | None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError as exc:
            raise InvalidRequest("request path is invalid") from exc
        match = _RUN_PATH.fullmatch(parsed.path)
        if match is None:
            return None
        try:
            run_id = validate_run_id(urllib.parse.unquote(match.group(1)))
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        return run_id, match.group(2)

    def _artifact_path(self) -> tuple[str, str | None] | None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError as exc:
            raise InvalidRequest("request path is invalid") from exc
        match = _ARTIFACT_PATH.fullmatch(parsed.path)
        if match is None:
            return None
        try:
            run_id = validate_run_id(urllib.parse.unquote(match.group(1)))
            artifact_id = (
                validate_artifact_id(urllib.parse.unquote(match.group(2)))
                if match.group(2) is not None
                else None
            )
        except (ValueError, ArtifactInvalid) as exc:
            raise InvalidRequest(str(exc)) from None
        return run_id, artifact_id

    def _workflow_catalog_path(self) -> str | None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError as exc:
            raise InvalidRequest("request path is invalid") from exc
        match = _WORKFLOW_CATALOG_PATH.fullmatch(parsed.path)
        if match is None:
            if parsed.path.startswith("/v1/workflows/"):
                raise InvalidRequest("saved Workflow catalog path is invalid")
            return None
        try:
            return validate_catalog_id(urllib.parse.unquote(match.group(1)))
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None

    @staticmethod
    def _workflow_list_query(query: str) -> tuple[int, int | None]:
        try:
            values = urllib.parse.parse_qs(query, keep_blank_values=True)
        except ValueError as exc:
            raise InvalidRequest("Workflow catalog query is invalid") from exc
        if set(values).difference({"limit", "before"}) or any(
            len(items) != 1 for items in values.values()
        ):
            raise InvalidRequest("Workflow catalog query is invalid")
        try:
            limit = int(values.get("limit", ["50"])[0])
            before = int(values["before"][0]) if "before" in values else None
        except ValueError as exc:
            raise InvalidRequest("Workflow catalog cursors must be integers") from exc
        if not 1 <= limit <= 100 or before is not None and not (
            1 <= before <= 2**63 - 1
        ):
            raise InvalidRequest("Workflow catalog cursor or limit is out of range")
        return limit, before

    @staticmethod
    def _workflow_revision_query(query: str) -> int | None:
        if not query:
            return None
        try:
            values = urllib.parse.parse_qs(query, keep_blank_values=True)
        except ValueError as exc:
            raise InvalidRequest("Workflow revision query is invalid") from exc
        if set(values) != {"revision"} or len(values["revision"]) != 1:
            raise InvalidRequest("Workflow revision query is invalid")
        try:
            revision = int(values["revision"][0])
        except ValueError as exc:
            raise InvalidRequest("Workflow revision must be an integer") from exc
        if not 1 <= revision <= 2**63 - 1:
            raise InvalidRequest("Workflow revision is out of range")
        return revision

    def _workflow_precondition(
        self, catalog_id: str
    ) -> tuple[bool, int | None, str | None]:
        matches = self.headers.get_all("If-Match", [])
        none_matches = self.headers.get_all("If-None-Match", [])
        if len(matches) > 1 or len(none_matches) > 1:
            raise InvalidRequest("Workflow conditional headers must not be repeated")
        if matches and none_matches:
            raise InvalidRequest("Workflow conditional headers are mutually exclusive")
        if none_matches:
            if none_matches[0] != "*":
                raise InvalidRequest("Workflow create requires If-None-Match: *")
            return True, None, None
        if not matches:
            raise WorkflowCatalogPreconditionRequired(
                "saved Workflow mutation requires a conditional request"
            )
        matched = _WORKFLOW_ETAG.fullmatch(matches[0])
        if matched is None or f"wfcat_{matched.group(1)}" != catalog_id:
            raise InvalidRequest("If-Match must contain the exact current Workflow ETag")
        revision = int(matched.group(2))
        if revision > 2**63 - 1:
            raise InvalidRequest("Workflow ETag revision is out of range")
        return False, revision, matched.group(3)

    def _handle_exception(self, exc: Exception, run_id: str | None = None) -> None:
        if isinstance(exc, ServerShuttingDown):
            self._close_error(
                503,
                "server_shutting_down",
                str(exc),
                retryable=True,
                extra={"Retry-After": "1"},
            )
        elif isinstance(exc, WorkflowCatalogOutcomeUnknown):
            self._error(
                504,
                "workflow_catalog_outcome_unknown",
                str(exc),
                catalog_id=exc.catalog_id,
                extra={"Cache-Control": "private, no-store"},
            )
        elif isinstance(exc, RuntimeBusy):
            self._error(
                503,
                "runtime_busy",
                str(exc),
                run_id=run_id,
                retryable=True,
                extra={"Retry-After": "1"},
            )
        elif isinstance(exc, RunNotFound):
            self._error(404, "run_not_found", "run was not found", run_id=run_id)
        elif isinstance(exc, ArtifactNotFound):
            self._error(
                404, "artifact_not_found", "artifact was not found", run_id=run_id
            )
        elif isinstance(exc, ArtifactCorrupted):
            self._error(
                503,
                "artifact_integrity_failed",
                "artifact bytes failed durable integrity verification",
                run_id=run_id,
            )
        elif isinstance(exc, ArtifactConflict):
            self._error(409, "artifact_conflict", str(exc), run_id=run_id)
        elif isinstance(exc, ArtifactInvalid):
            self._error(422, "invalid_artifact", str(exc), run_id=run_id)
        elif isinstance(exc, InvalidArtifactRange):
            self._error(
                416,
                "range_not_satisfiable",
                str(exc),
                run_id=run_id,
                extra={"Content-Range": f"bytes */{exc.size}"},
            )
        elif isinstance(exc, AppNotFound):
            self._error(404, "app_not_found", "application was not found")
        elif isinstance(exc, AppUnavailable):
            self._error(
                409,
                "app_unavailable",
                str(exc),
                reason_code=exc.reason_code,
            )
        elif isinstance(exc, AppBindingMissing):
            self._error(409, "app_binding_missing", str(exc), run_id=run_id)
        elif isinstance(exc, RunAlreadyExists):
            self._error(409, "run_already_exists", str(exc), run_id=run_id)
        elif isinstance(exc, ApprovalMismatch):
            self._error(409, "approval_mismatch", str(exc), run_id=run_id)
        elif isinstance(exc, ApprovalConflict):
            self._error(409, "approval_conflict", str(exc), run_id=run_id)
        elif isinstance(exc, (InvalidTransition, RunPaused, RunCancelled)):
            self._error(409, "invalid_transition", str(exc), run_id=run_id)
        elif isinstance(exc, InvalidRequest):
            self._error(422, "invalid_request", str(exc), run_id=run_id)
        elif isinstance(exc, WorkflowPreflightRejected):
            self._error(
                422,
                "workflow_preflight_rejected",
                str(exc),
                reason_code=exc.reason_code,
            )
        elif isinstance(exc, WorkflowCatalogPreconditionRequired):
            self._error(
                428,
                "workflow_catalog_precondition_required",
                str(exc),
            )
        elif isinstance(exc, (WorkflowCatalogExists, WorkflowCatalogRevisionMismatch)):
            self._error(
                412,
                "workflow_catalog_revision_mismatch",
                "saved Workflow precondition did not match durable state",
            )
        elif isinstance(exc, WorkflowCatalogNotFound):
            self._error(
                404,
                "workflow_catalog_not_found",
                "saved Workflow or revision was not found",
            )
        elif isinstance(exc, WorkflowCatalogIntegrityError):
            self._error(
                503,
                "workflow_catalog_integrity_failed",
                "saved Workflow catalog integrity verification failed",
            )
        elif isinstance(exc, (WorkflowCatalogConfigurationError, WorkflowCatalogError)):
            self._error(
                503,
                "workflow_catalog_store_unavailable",
                "saved Workflow catalog is unavailable",
                retryable=True,
            )
        elif isinstance(exc, ModelTimeoutError):
            self._error(504, exc.code, str(exc), run_id=run_id, retryable=True)
        elif isinstance(exc, (ModelCallError, DuplicateToolCallError)):
            self._error(502, exc.code, str(exc), run_id=run_id)
        elif isinstance(exc, MaxStepsExceeded):
            self._error(422, exc.code, str(exc), run_id=run_id)
        elif isinstance(exc, StoreError):
            self._error(503, "store_unavailable", str(exc), run_id=run_id, retryable=True)
        elif isinstance(exc, ArtifactError):
            self._error(
                503,
                "artifact_store_unavailable",
                "artifact store is unavailable",
                run_id=run_id,
                retryable=True,
            )
        elif isinstance(exc, SasoriError):
            self._error(502, exc.code, str(exc), run_id=run_id)
        else:
            self._error(500, "internal_error", "internal adapter error", run_id=run_id)

    def do_OPTIONS(self) -> None:
        self._response_started = False
        if self._server_closing():
            return
        try:
            path = urllib.parse.urlsplit(self.path).path
        except ValueError:
            self._close_error(422, "invalid_request", "request path is invalid")
            return
        origins = self.headers.get_all("Origin", [])
        if len(origins) != 1:
            self._close_error(400, "invalid_header", "one Origin header is required")
            return
        origin = origins[0]
        if not path.startswith("/v1/") or origin not in self.sasori.cors_origins:
            self._close_error(
                403, "origin_forbidden", "request origin is not allowed"
            )
            return
        self._send_headers(
            204,
            "text/plain; charset=utf-8",
            0,
            {
                "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Authorization, Content-Type, If-Match, If-None-Match, "
                    "Last-Event-ID, Range"
                ),
                "Access-Control-Expose-Headers": "ETag, Location",
                "Access-Control-Max-Age": "600",
            },
        )

    def do_GET(self) -> None:
        self._response_started = False
        if self._server_closing():
            return
        if self.path == "/healthz":
            self._json(200, {"ok": True, "status": "alive"})
            return
        if self.path == "/readyz":
            try:
                self.sasori.owner.call(asyncio.sleep(0, result=True), 2)
                self._json(200, {"ok": True, "status": "ready"})
            except Exception:
                self._error(503, "not_ready", "runtime owner is unavailable", retryable=True)
            return
        try:
            public_path = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._close_error(422, "invalid_request", "request path is invalid")
            return
        if self._workbench_asset(public_path.path, public_path.query):
            return
        if not self._authorized():
            return
        run_id: str | None = None
        try:
            try:
                parsed = urllib.parse.urlsplit(self.path)
            except ValueError as exc:
                raise InvalidRequest("request path is invalid") from exc
            if parsed.path == "/v1/apps":
                if parsed.query:
                    raise InvalidRequest("app catalog does not accept query parameters")
                value = self.sasori.owner.call(self.sasori.owner.catalog(), 5)
                self._json(200, value)
                return
            if parsed.path == "/v1/workflows":
                limit, before = self._workflow_list_query(parsed.query)
                value = self.sasori.owner.call(
                    self.sasori.owner.saved_workflow_list(limit, before), 5
                )
                self._json(200, value)
                return
            if parsed.path == "/v1/workflows/preflight":
                self._error(405, "method_not_allowed", "GET is not allowed")
                return
            workflow_catalog_id = self._workflow_catalog_path()
            if workflow_catalog_id is not None:
                revision = self._workflow_revision_query(parsed.query)
                value, etag = self.sasori.owner.call(
                    self.sasori.owner.saved_workflow_get(
                        workflow_catalog_id, revision
                    ),
                    5,
                )
                self._json(
                    200,
                    value,
                    {
                        "Cache-Control": "private, no-store",
                        "ETag": etag,
                        "Access-Control-Expose-Headers": "ETag, Location",
                    },
                )
                return
            if parsed.path == "/v1/runs":
                limit, before, app_id = self._history_query(parsed.query)
                value = self.sasori.owner.call(
                    self.sasori.owner.history(limit, before, app_id), 5
                )
                self._json(200, value)
                return
            artifact_path = self._artifact_path()
            if artifact_path is not None:
                run_id, artifact_id = artifact_path
                if artifact_id is None:
                    if parsed.query:
                        raise InvalidRequest(
                            "artifact collection does not accept query parameters"
                        )
                    value = self.sasori.owner.call(
                        self.sasori.owner.artifacts(run_id), 5
                    )
                    self._json(200, value)
                    return
                self._get_artifact(run_id, artifact_id, parsed.query)
                return
            path = self._path()
            if path is None:
                self._error(404, "not_found", "endpoint was not found")
                return
            run_id, action = path
            if action is None:
                value = self.sasori.owner.call(self.sasori.owner.status(run_id), 5)
                self._json(200, value)
                return
            if action != "events":
                self._error(405, "method_not_allowed", "GET is not allowed")
                return
            self._get_events(run_id)
        except Exception as exc:
            if self._response_started:
                self.close_connection = True
                return
            self._handle_exception(exc, run_id)

    def _artifact_range(self, size: int) -> tuple[int, int] | None:
        values = self.headers.get_all("Range", [])
        if not values:
            return None
        if len(values) != 1:
            raise InvalidArtifactRange(size)
        match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", values[0])
        if match is None or size == 0:
            raise InvalidArtifactRange(size)
        first, last = match.groups()
        if not first and not last:
            raise InvalidArtifactRange(size)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if start >= size or end < start:
                raise InvalidArtifactRange(size)
            return start, min(end, size - 1)
        suffix = int(last)
        if suffix <= 0:
            raise InvalidArtifactRange(size)
        return max(0, size - suffix), size - 1

    def _get_artifact(
        self, run_id: str, artifact_id: str, query: str, *, head: bool = False
    ) -> None:
        if query:
            raise InvalidRequest("artifact content does not accept query parameters")
        payload = self.sasori.owner.call(
            self.sasori.owner.artifact(run_id, artifact_id), 10
        )
        if not isinstance(payload, ArtifactPayload):
            raise ArtifactCorrupted("artifact adapter returned an invalid payload")
        ref = payload.ref
        byte_range = self._artifact_range(ref.size_bytes)
        if byte_range is None:
            status = 200
            content = payload.content
            range_header = None
        else:
            start, end = byte_range
            status = 206
            content = payload.content[start : end + 1]
            range_header = f"bytes {start}-{end}/{ref.size_bytes}"
        encoded_filename = urllib.parse.quote(ref.filename, safe="")
        content_type = (
            ref.media_type if ref.can_preview_text else "application/octet-stream"
        )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": (
                "attachment; filename=\"artifact\"; "
                f"filename*=UTF-8''{encoded_filename}"
            ),
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cross-Origin-Resource-Policy": "same-origin",
            "ETag": f'"sha256-{ref.content_sha256}"',
            "X-Sasori-Content-SHA256": ref.content_sha256,
        }
        if range_header is not None:
            headers["Content-Range"] = range_header
        self._send_headers(status, content_type, len(content), headers)
        if not head:
            self.wfile.write(content)

    def do_HEAD(self) -> None:
        self._response_started = False
        if self._server_closing():
            return
        if not self._authorized():
            return
        run_id: str | None = None
        try:
            parsed = urllib.parse.urlsplit(self.path)
            artifact_path = self._artifact_path()
            if artifact_path is None or artifact_path[1] is None:
                self._error(405, "method_not_allowed", "HEAD is not allowed")
                return
            run_id, artifact_id = artifact_path
            self._get_artifact(run_id, artifact_id, parsed.query, head=True)
        except Exception as exc:
            if self._response_started:
                self.close_connection = True
                return
            self._handle_exception(exc, run_id)

    @staticmethod
    def _history_query(query: str) -> tuple[int, int | None, str | None]:
        try:
            values = urllib.parse.parse_qs(query, keep_blank_values=True)
        except ValueError as exc:
            raise InvalidRequest("history query is invalid") from exc
        if set(values).difference({"limit", "before", "app_id"}) or any(
            len(items) != 1 for items in values.values()
        ):
            raise InvalidRequest("history query is invalid")
        try:
            limit = int(values.get("limit", ["50"])[0])
            before = (
                int(values["before"][0]) if "before" in values else None
            )
        except ValueError as exc:
            raise InvalidRequest("history cursors must be integers") from exc
        if not 1 <= limit <= 100 or before is not None and before < 1:
            raise InvalidRequest("history cursor or limit is out of range")
        app_id = values.get("app_id", [None])[0]
        if app_id is not None and not is_valid_app_id(app_id):
            raise InvalidRequest("app_id is invalid")
        return limit, before, app_id

    def _cursor(self) -> int:
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError as exc:
            raise InvalidRequest("event query is invalid") from exc
        try:
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        except ValueError as exc:
            raise InvalidRequest("event query is invalid") from exc
        if set(values).difference({"after_seq"}) or len(values.get("after_seq", [])) > 1:
            raise InvalidRequest("event query is invalid")
        query = values.get("after_seq", [None])[0]
        headers = self.headers.get_all("Last-Event-ID", [])
        if len(headers) > 1:
            raise InvalidRequest("Last-Event-ID must not be repeated")
        header = headers[0] if headers else None
        if query is not None and header is not None and query != header:
            raise InvalidRequest("event cursor header and query conflict")
        raw = header if header is not None else query
        if raw is None:
            return 0
        try:
            cursor = int(raw)
        except ValueError as exc:
            raise InvalidRequest("event cursor must be an integer") from exc
        if cursor < 0:
            raise InvalidRequest("event cursor must be non-negative")
        return cursor

    def _get_events(self, run_id: str) -> None:
        cursor = self._cursor()
        status = self.sasori.owner.call(self.sasori.owner.status(run_id), 5)
        if cursor > status["latest_seq"]:
            self._error(409, "cursor_ahead", "event cursor is ahead of durable history")
            return
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept.lower():
            events = self.sasori.owner.call(
                self.sasori.owner.events(run_id, cursor), 5
            )
            self._json(
                200,
                {
                    "run_id": run_id,
                    "after_seq": cursor,
                    "latest_seq": status["latest_seq"],
                    "events": events,
                },
            )
            return
        self._sse(run_id, cursor)

    def _sse(self, run_id: str, cursor: int) -> None:
        if self._server_closing():
            return
        self.close_connection = True
        self._send_headers(
            200,
            "text/event-stream; charset=utf-8",
            None,
            {
                "Cache-Control": "no-cache",
                "Connection": "close",
                "X-Accel-Buffering": "no",
            },
        )
        started = time.monotonic()
        keepalive = started
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while time.monotonic() - started < self.sasori.sse_max_seconds:
                if self.sasori._closing.is_set():
                    return
                batch = self.sasori.owner.call(
                    self.sasori.owner.events(run_id, cursor), 5
                )
                terminal = False
                for projected in batch:
                    sequence = projected["seq"]
                    event = projected["event"]
                    payload = json.dumps(
                        projected,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    self.wfile.write(
                        f"id: {sequence}\nevent: {event['type']}\ndata: {payload}\n\n".encode(
                            "utf-8"
                        )
                    )
                    cursor = sequence
                    terminal = event["type"] in _TERMINAL_EVENTS
                now = time.monotonic()
                if batch:
                    self.wfile.flush()
                    keepalive = now
                if terminal:
                    return
                status = self.sasori.owner.call(self.sasori.owner.status(run_id), 5)
                if status["state"] in {"completed", "failed", "cancelled"}:
                    return
                if now - keepalive >= self.sasori.sse_keepalive_seconds:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    keepalive = now
                time.sleep(min(0.25, self.sasori.sse_keepalive_seconds))
        except OSError:
            return

    def do_POST(self) -> None:
        self._response_started = False
        if not self._authorized():
            return
        path = None
        run_id: str | None = None
        try:
            try:
                parsed = urllib.parse.urlsplit(self.path)
            except ValueError as exc:
                raise InvalidRequest("request path is invalid") from exc
            if parsed.path in {"/v1/runs", "/v1/workflows/preflight"}:
                if parsed.query:
                    raise InvalidRequest(
                        (
                            "run endpoint does not accept query parameters"
                            if parsed.path == "/v1/runs"
                            else "workflow preflight does not accept query parameters"
                        )
                    )
            else:
                path = self._path()
                if path is None:
                    self._error(404, "not_found", "endpoint was not found")
                    return
                run_id = path[0]
            body = self._body()
            if body is None:
                return
            assert isinstance(body, dict)
            if parsed.path == "/v1/workflows/preflight":
                value = self.sasori.owner.call(
                    self.sasori.owner.workflow_preflight(body), 5
                )
                self._json(200, value)
                return
            if parsed.path == "/v1/runs":
                self._fields(body, {"run_id", "app_id", "input"})
                prompt = body.get("input")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise InvalidRequest("input must be a non-empty string")
                requested = body.get("run_id")
                if requested is not None:
                    try:
                        requested = validate_run_id(requested)
                    except ValueError as exc:
                        raise InvalidRequest(str(exc)) from None
                app_id = body.get("app_id")
                if app_id is not None and (
                    not isinstance(app_id, str)
                    or not is_valid_app_id(app_id)
                ):
                    raise InvalidRequest("app_id is invalid")
                status, value = self.sasori.owner.call(
                    self.sasori.owner.run(prompt, requested, app_id)
                )
                self._json(status, value)
                return
            assert path is not None and run_id is not None
            action = path[1]
            if action == "resume":
                self._fields(body, set())
                status, value = self.sasori.owner.call(
                    self.sasori.owner.resume(run_id)
                )
                self._json(status, value)
                return
            if action == "approval":
                self._fields(body, {"fingerprint", "approved"})
                fingerprint = body.get("fingerprint")
                approved = body.get("approved")
                if not isinstance(fingerprint, str) or not fingerprint:
                    raise InvalidRequest("fingerprint must be a non-empty string")
                if not isinstance(approved, bool):
                    raise InvalidRequest("approved must be a boolean")
                value = self.sasori.owner.call(
                    self.sasori.owner.approval(run_id, fingerprint, approved)
                )
                self._json(200, value)
                return
            if action == "effect":
                self._fields(body, {"fingerprint", "action", "reason", "result"})
                fingerprint = body.get("fingerprint")
                effect_action = body.get("action")
                reason = body.get("reason")
                if not isinstance(fingerprint, str) or not fingerprint:
                    raise InvalidRequest("fingerprint must be a non-empty string")
                if effect_action not in {"record_result", "fail", "retry"}:
                    raise InvalidRequest("action is invalid")
                if not isinstance(reason, str) or not reason.strip():
                    raise InvalidRequest("reason must be a non-empty string")
                value = self.sasori.owner.call(
                    self.sasori.owner.effect(
                        run_id,
                        fingerprint,
                        effect_action,
                        reason,
                        body.get("result"),
                    )
                )
                self._json(200, value)
                return
            self._error(405, "method_not_allowed", "POST is not allowed")
        except Exception as exc:
            if self._response_started:
                self.close_connection = True
                return
            self._handle_exception(exc, run_id)

    def do_PUT(self) -> None:
        self._response_started = False
        if not self._authorized():
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
            catalog_id = self._workflow_catalog_path()
            if catalog_id is None:
                self._error(404, "not_found", "endpoint was not found")
                return
            if parsed.query:
                raise InvalidRequest(
                    "saved Workflow mutation does not accept query parameters"
                )
            create, expected_revision, expected_digest = self._workflow_precondition(
                catalog_id
            )
            body = self._body()
            if body is None:
                return
            assert isinstance(body, dict)
            try:
                status, value, etag = self.sasori.owner.call(
                    self.sasori.owner.saved_workflow_put(
                        catalog_id,
                        body,
                        create=create,
                        expected_revision=expected_revision,
                        expected_definition_sha256=expected_digest,
                    ),
                    5,
                )
            except RuntimeBusy:
                raise WorkflowCatalogOutcomeUnknown(catalog_id) from None
            headers = {
                "Cache-Control": "private, no-store",
                "ETag": etag,
                "Access-Control-Expose-Headers": "ETag, Location",
            }
            if status == 201:
                headers["Location"] = f"/v1/workflows/{catalog_id}"
            self._json(status, value, headers)
        except Exception as exc:
            if self._response_started:
                self.close_connection = True
                return
            self._handle_exception(exc)


def create_server(
    host: str,
    port: int,
    *,
    database: str,
    workflow_database: str | Path | None = None,
    artifact_root: str | Path | None = None,
    app: str | None = None,
    apps: Mapping[str, str] | None = None,
    token: str | None = None,
    trusted_loopback_no_auth: bool = False,
    cors_origins: Sequence[str] = (),
    sse_max_seconds: float = 300.0,
    sse_keepalive_seconds: float = 15.0,
    publish_final_artifact: bool = False,
) -> SasoriHTTPServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ServerConfigurationError("port must be between 0 and 65535")
    if not isinstance(host, str) or not host:
        raise ServerConfigurationError("host is required")
    if token is not None and (
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or "\r" in token
        or "\n" in token
    ):
        raise ServerConfigurationError("server token is malformed")
    if token is None and not (trusted_loopback_no_auth and _loopback(host)):
        raise ServerConfigurationError(
            "bearer token is required unless loopback no-auth is explicitly trusted"
        )
    if any(not _valid_origin(origin) for origin in cors_origins):
        raise ServerConfigurationError("CORS origins must be exact http(s) origins")
    if not isinstance(publish_final_artifact, bool):
        raise ServerConfigurationError("publish_final_artifact must be a boolean")
    if (
        isinstance(sse_max_seconds, bool)
        or not isinstance(sse_max_seconds, (int, float))
        or not math.isfinite(sse_max_seconds)
        or sse_max_seconds <= 0
        or isinstance(sse_keepalive_seconds, bool)
        or not isinstance(sse_keepalive_seconds, (int, float))
        or not math.isfinite(sse_keepalive_seconds)
        or sse_keepalive_seconds <= 0
    ):
        raise ServerConfigurationError("SSE timing values must be positive")
    if (app is None) == (apps is None):
        raise ServerConfigurationError("configure either app or apps")
    owner = _Owner(
        database,
        app if app is not None else apps or {},
        artifact_root,
        publish_final_artifact,
        workflow_database,
    )
    owner.start()
    try:
        return SasoriHTTPServer(
            (host, port),
            owner,
            token=token,
            cors_origins=cors_origins,
            sse_max_seconds=sse_max_seconds,
            sse_keepalive_seconds=sse_keepalive_seconds,
        )
    except BaseException:
        owner.close()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sasori-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.environ.get("SASORI_DB", "sasori.sqlite3"))
    parser.add_argument(
        "--workflow-db", default=os.environ.get("SASORI_WORKFLOW_DB")
    )
    parser.add_argument(
        "--artifact-root", default=os.environ.get("SASORI_ARTIFACT_ROOT")
    )
    parser.add_argument("--app", action="append", default=[])
    parser.add_argument("--token-file")
    parser.add_argument("--trusted-loopback-no-auth", action="store_true")
    parser.add_argument("--cors-origin", action="append", default=[])
    parser.add_argument("--publish-final-artifact", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    publish_environment = os.environ.get("SASORI_PUBLISH_FINAL_ARTIFACT")
    if publish_environment not in (None, "0", "1"):
        print(
            "sasori-server: SASORI_PUBLISH_FINAL_ARTIFACT must be 0 or 1",
            file=os.sys.stderr,
        )
        return 2
    publish_final_artifact = (
        args.publish_final_artifact or publish_environment == "1"
    )
    configured = list(args.app)
    if not configured and os.environ.get("SASORI_APP"):
        configured.append(os.environ["SASORI_APP"])
    if not configured:
        raise SystemExit("--app or SASORI_APP is required")
    from sasori_apps.registry import app_id_for_spec

    apps: dict[str, str] = {}
    for value in configured:
        if "=" in value:
            app_id, spec = value.split("=", 1)
        else:
            spec = value
            app_id = app_id_for_spec(spec) or "default"
        if not is_valid_app_id(app_id) or not spec or app_id in apps:
            raise SystemExit("--app must use unique [id=]module:factory values")
        apps[app_id] = spec
    token = None
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    elif os.environ.get("SASORI_SERVER_TOKEN"):
        token = os.environ["SASORI_SERVER_TOKEN"]
    try:
        server = create_server(
            args.host,
            args.port,
            database=args.db,
            workflow_database=args.workflow_db,
            artifact_root=args.artifact_root,
            apps=apps,
            token=token,
            trusted_loopback_no_auth=args.trusted_loopback_no_auth,
            cors_origins=args.cors_origin,
            publish_final_artifact=publish_final_artifact,
        )
    except (
        ServerConfigurationError,
        AppLoadError,
        StoreError,
        WorkflowCatalogError,
    ) as exc:
        print(f"sasori-server: {exc}", file=os.sys.stderr)
        return 2
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AppBindingMissing",
    "AppNotFound",
    "AppUnavailable",
    "InvalidRequest",
    "InvalidTransition",
    "RuntimeBusy",
    "SasoriHTTPServer",
    "ServerConfigurationError",
    "ServerShutdownIncomplete",
    "ServerShuttingDown",
    "create_server",
]
