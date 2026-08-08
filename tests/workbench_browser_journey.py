from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sasori import SQLiteStore  # noqa: E402
from sasori.server import create_server  # noqa: E402
from workbench_browser_acceptance import (  # noqa: E402
    browser_candidates,
    browser_version,
    run_browser_process,
)


JOURNEY = Path(__file__).with_name("workbench_real_journey.js")
SCRIPT_MARKER = '<script src="/assets/event-reducer.0.1.0.js" defer></script>'
EXPECTED = "PASS:real-incident-lifecycle"
EXPECTED_INPUT = "browser lifecycle incident"
EXPECTED_ACTION = f"Operator review: diagnostic captured for {EXPECTED_INPUT}"
_HOP_BY_HOP = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class JourneyProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend: tuple[str, int]
    action_log: Path

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/workbench-real-journey.js":
            self._send(
                200,
                JOURNEY.read_bytes(),
                {"Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-store"},
            )
            return
        if self.path == "/__journey__/action-count":
            try:
                count = len(self.action_log.read_bytes().splitlines()) if self.action_log.exists() else 0
            except OSError:
                self.send_error(500, "action ledger is unreadable")
                return
            self._send(
                200,
                json.dumps({"count": count}, separators=(",", ":")).encode("ascii"),
                {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            )
            return
        self._forward()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._forward()

    def _forward(self) -> None:
        length_value = self.headers.get("Content-Length")
        try:
            length = int(length_value) if length_value is not None else 0
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        try:
            body = self.rfile.read(length) if length else None
        except OSError:
            self.close_connection = True
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP and name.lower() != "host"
        }
        headers["Host"] = f"{self.backend[0]}:{self.backend[1]}"
        if "Origin" in headers:
            headers["Origin"] = f"http://{self.backend[0]}:{self.backend[1]}"

        connection = http.client.HTTPConnection(*self.backend, timeout=15)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            response_headers = {
                name: value
                for name, value in response.getheaders()
                if name.lower() not in _HOP_BY_HOP
            }
        except (OSError, http.client.HTTPException):
            self.send_error(502, "real Sasori server proxy failed")
            return
        finally:
            connection.close()

        if self.command == "GET" and urllib.parse.urlsplit(self.path).path == "/":
            try:
                page = payload.decode("utf-8")
            except UnicodeDecodeError:
                self.send_error(502, "production Workbench is not UTF-8")
                return
            if page.count(SCRIPT_MARKER) != 1:
                self.send_error(502, "production reducer asset marker is missing or ambiguous")
                return
            payload = page.replace(
                SCRIPT_MARKER,
                '<script src="/workbench-real-journey.js"></script>\n  ' + SCRIPT_MARKER,
                1,
            ).encode("utf-8")

        self._send(response.status, payload, response_headers)

    def _send(self, status: int, payload: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            if name.lower() != "content-length":
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return


def create_proxy(backend: tuple[str, int], action_log: Path) -> http.server.ThreadingHTTPServer:
    handler = type(
        "BoundJourneyProxyHandler",
        (JourneyProxyHandler,),
        {"backend": backend, "action_log": action_log},
    )
    for port in range(18180, 18280):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        server.daemon_threads = True
        return server
    raise RuntimeError("no safe loopback port is available for the real Workbench journey")


def strict_action(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise AssertionError("Incident action ledger is missing or unreadable") from None
    if len(lines) != 1:
        raise AssertionError("real Workbench journey must write exactly one action")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate action key")
            value[key] = item
        return value

    try:
        decoded = json.loads(lines[0], object_pairs_hook=unique_object)
    except (ValueError, json.JSONDecodeError):
        raise AssertionError("Incident action ledger is not strict JSON") from None
    if decoded != {"summary": EXPECTED_ACTION}:
        raise AssertionError("Incident action ledger contains an unexpected effect")
    return {"summary": EXPECTED_ACTION}


def validate_store(database: Path, run_id: str) -> dict[str, object]:
    with SQLiteStore(database) as store:
        snapshot = store.load(run_id)
        events = store.stored_events(run_id)
    if snapshot.status != "completed":
        raise AssertionError("real Workbench journey did not durably complete")
    if snapshot.app_id != "incident":
        raise AssertionError("real Workbench journey lost its immutable app binding")
    if snapshot.final_message is None or EXPECTED_ACTION not in snapshot.final_message.content:
        raise AssertionError("real Workbench journey persisted an unexpected final answer")
    if len(events) != 16 or [event.seq for event in events] != list(range(1, 17)):
        raise AssertionError("real Workbench journey did not persist the exact event sequence")
    if [event.event.type for event in events].count("run.completed") != 1:
        raise AssertionError("real Workbench journey has an invalid terminal event count")
    return {
        "app_id": snapshot.app_id,
        "state": snapshot.status,
        "events": len(events),
        "latest_seq": events[-1].seq,
    }


def run_acceptance(binary: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="sasori-real-journey-") as directory:
        root = Path(directory)
        database = root / "runs.sqlite3"
        action_log = root / "actions.jsonl"
        previous_action_log = os.environ.get("SASORI_ACTION_LOG")
        os.environ["SASORI_ACTION_LOG"] = str(action_log)
        backend = None
        backend_thread = None
        proxy = None
        proxy_thread = None
        try:
            backend = create_server(
                "127.0.0.1",
                0,
                database=str(database),
                app="sasori_apps.incident:create_harness",
                trusted_loopback_no_auth=True,
            )
            backend_thread = threading.Thread(
                target=backend.serve_forever,
                name="sasori-real-journey-backend",
                daemon=True,
            )
            backend_thread.start()
            proxy = create_proxy(backend.server_address, action_log)
            proxy_thread = threading.Thread(
                target=proxy.serve_forever,
                name="sasori-real-journey-proxy",
                daemon=True,
            )
            proxy_thread.start()
            completed = run_browser_process(
                binary, proxy.server_address[1], virtual_time_budget=20000
            )
        finally:
            try:
                if proxy is not None:
                    if proxy_thread is not None and proxy_thread.is_alive():
                        proxy.shutdown()
                    proxy.server_close()
                if proxy_thread is not None:
                    proxy_thread.join(timeout=5)
            finally:
                try:
                    if backend is not None:
                        if backend_thread is not None and backend_thread.is_alive():
                            backend.shutdown()
                        backend.server_close()
                    if backend_thread is not None:
                        backend_thread.join(timeout=5)
                finally:
                    if previous_action_log is None:
                        os.environ.pop("SASORI_ACTION_LOG", None)
                    else:
                        os.environ["SASORI_ACTION_LOG"] = previous_action_log

        if completed.returncode != 0:
            raise RuntimeError(
                f"headless browser exited {completed.returncode}\n"
                f"stdout tail:\n{completed.stdout[-4000:]}\n"
                f"stderr tail:\n{completed.stderr[-4000:]}"
            )
        if EXPECTED not in completed.stdout or 'data-result="passed"' not in completed.stdout:
            raise AssertionError(
                "real Workbench browser journey did not reach the pass marker\n"
                f"stdout tail:\n{completed.stdout[-6000:]}\n"
                f"stderr tail:\n{completed.stderr[-4000:]}"
            )
        match = re.search(r'data-run-id="([A-Za-z0-9._-]+)"', completed.stdout)
        if match is None:
            raise AssertionError("real Workbench journey did not report its run ID")
        run_id = match.group(1)
        action = strict_action(action_log)
        durable = validate_store(database, run_id)
        return {
            "browser": browser_version(binary),
            "case": "real-incident-lifecycle",
            "run_id": run_id,
            "production_assets": ["event-reducer.0.1.0.js", "app.0.1.2.js"],
            "durable": durable,
            "effect": {"count": 1, "summary": action["summary"]},
            "cold_history_reopen": True,
            "permission_disclosure_visible": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the production Workbench against a real local Sasori Incident server."
    )
    parser.add_argument(
        "--require-browser",
        action="store_true",
        help="fail instead of skipping when Chrome/Edge is unavailable",
    )
    arguments = parser.parse_args()
    candidates = browser_candidates()
    if not candidates:
        if arguments.require_browser:
            raise SystemExit("Chrome, Chromium, or Edge is required for Workbench browser acceptance")
        print(json.dumps({"skipped": True, "reason": "browser_not_found"}))
        return 0
    evidence = run_acceptance(candidates[0])
    print(json.dumps(evidence, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
