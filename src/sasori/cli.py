from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence

from .app import AppLoadError, load_harness
from .contracts import Message
from .projection import event_projection, run_projection, validate_run_id
from .runtime import RunCancelled, RunPaused, SasoriError
from .sqlite_store import (
    ApprovalConflict,
    ApprovalMismatch,
    RunAlreadyExists,
    RunNotFound,
    SQLiteStore,
    StoreError,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sasori")
    parser.add_argument("--db", default=os.environ.get("SASORI_DB", "sasori.sqlite3"))
    parser.add_argument("--app", default=os.environ.get("SASORI_APP"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start one user turn")
    run.add_argument("prompt", nargs="?")
    run.add_argument("--run-id")

    resume = commands.add_parser("resume", help="continue a durable run")
    resume.add_argument("run_id")

    status = commands.add_parser("status", help="show durable run state")
    status.add_argument("run_id")

    events = commands.add_parser("events", help="read durable events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--follow", action="store_true")
    events.add_argument("--poll-interval", type=float, default=0.5)

    approval = commands.add_parser("approval", help="resolve a tool approval")
    approval.add_argument("run_id")
    approval.add_argument("fingerprint")
    decision = approval.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--deny", action="store_true")

    effect = commands.add_parser("effect", help="resolve an unknown tool effect")
    effect.add_argument("run_id")
    effect.add_argument("fingerprint")
    effect.add_argument(
        "--action", choices=("record_result", "fail", "retry"), required=True
    )
    effect.add_argument("--reason", required=True)
    result = effect.add_mutually_exclusive_group()
    result.add_argument("--result-json")
    result.add_argument("--result-text")
    return parser


def _strict_json(value: str) -> object:
    def invalid_constant(token: str) -> object:
        raise ValueError(f"invalid JSON constant: {token}")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, parse_constant=invalid_constant, object_pairs_hook=pairs)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _print_projection(value: dict[str, object], json_output: bool) -> None:
    if json_output:
        _print_json(value)
        return
    print(
        f"RUN {value['run_id']}  {str(value['state']).upper()}"
        f" {value['pause_reason'] or ''}  step={value['step']} seq={value['latest_seq']}"
    )
    pending = value.get("pending")
    if isinstance(pending, dict):
        print(
            f"TOOL {pending['tool_name']}@{pending['tool_revision']}"
            f"  effect={pending['effect']}"
        )
        print("ARGS " + json.dumps(pending["arguments"], ensure_ascii=False))
    final = value.get("final_message")
    if isinstance(final, dict):
        print("FINAL " + str(final.get("content", "")))


def _print_error(code: str, message: str, json_output: bool) -> None:
    if json_output:
        _print_json({"ok": False, "error": {"code": code, "message": message}})
    else:
        print(f"ERROR {code}: {message}", file=sys.stderr)


def _need_app(args: argparse.Namespace, store: SQLiteStore):
    if not args.app:
        raise AppLoadError("--app or SASORI_APP is required for this command")
    return load_harness(args.app, store)


def _events(args: argparse.Namespace, store: SQLiteStore) -> int:
    run_id = validate_run_id(args.run_id)
    if args.after < 0:
        raise ValueError("--after must be non-negative")
    if args.follow and args.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than zero")
    store.load(run_id)
    cursor = args.after
    while True:
        batch = store.stored_events(run_id, cursor)
        for stored in batch:
            projected = event_projection(stored)
            if args.json_output:
                _print_json(projected)
            else:
                event = projected["event"]
                print(f"{stored.seq}\t{event['type']}\tstep={event['step']}")
            cursor = stored.seq
        if not args.follow:
            return 0
        state = store.load(run_id).status
        if state in {"completed", "failed", "cancelled"} and not batch:
            return 0
        time.sleep(args.poll_interval)


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store: SQLiteStore | None = None
    try:
        store = SQLiteStore(args.db)
        if args.command == "status":
            _print_projection(run_projection(store, args.run_id), args.json_output)
            return 0
        if args.command == "events":
            return _events(args, store)

        harness = _need_app(args, store)
        if args.command == "run":
            prompt = args.prompt
            if prompt is None and not sys.stdin.isatty():
                prompt = sys.stdin.read()
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("prompt is required as an argument or stdin")
            if len(prompt.encode("utf-8")) > 1024 * 1024:
                raise ValueError("prompt exceeds the 1 MiB limit")
            run_id = validate_run_id(args.run_id) if args.run_id else None
            try:
                result = asyncio.run(
                    harness.run((Message("user", prompt),), run_id=run_id)
                )
                run_id = result.run_id
            except RunPaused as paused:
                _print_projection(
                    run_projection(store, paused.run_id), args.json_output
                )
                return 3
            _print_projection(run_projection(store, run_id), args.json_output)
            return 0
        run_id = validate_run_id(args.run_id)
        if args.command == "resume":
            try:
                asyncio.run(harness.resume(run_id))
            except RunPaused:
                _print_projection(run_projection(store, run_id), args.json_output)
                return 3
            except RunCancelled:
                _print_projection(run_projection(store, run_id), args.json_output)
                return 4
            _print_projection(run_projection(store, run_id), args.json_output)
            return 0
        if args.command == "approval":
            harness.resolve_approval(run_id, args.fingerprint, args.approve)
            _print_projection(run_projection(store, run_id), args.json_output)
            return 0
        if args.command == "effect":
            result: object | None = args.result_text
            if args.result_json is not None:
                result = _strict_json(args.result_json)
            harness.resolve_effect(
                run_id,
                args.fingerprint,
                args.action,
                reason=args.reason,
                result=result,
            )
            _print_projection(run_projection(store, run_id), args.json_output)
            return 0
        raise AssertionError("unreachable command")
    except KeyboardInterrupt:
        _print_error("interrupted", "command interrupted", args.json_output)
        return 130
    except (ValueError, AppLoadError, json.JSONDecodeError) as exc:
        _print_error("invalid_input", str(exc), args.json_output)
        return 2
    except (RunNotFound, RunAlreadyExists, ApprovalMismatch, ApprovalConflict) as exc:
        _print_error(type(exc).__name__.lower(), str(exc), args.json_output)
        return 6
    except RunCancelled as exc:
        _print_error("run_cancelled", str(exc), args.json_output)
        return 4
    except RunPaused as exc:
        _print_projection(run_projection(store, exc.run_id), args.json_output)
        return 3
    except SasoriError as exc:
        _print_error(getattr(exc, "code", "run_failed"), str(exc), args.json_output)
        return 5
    except StoreError as exc:
        _print_error(type(exc).__name__.lower(), str(exc), args.json_output)
        return 7
    except Exception as exc:
        _print_error("adapter_error", f"{type(exc).__name__}: {exc}", args.json_output)
        return 7
    finally:
        if store is not None:
            store.close()


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
