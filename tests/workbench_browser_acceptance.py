from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import locale
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "sasori_web"
FIXTURE = Path(__file__).with_name("workbench_browser_fixture.js")
SCRIPT_MARKER = '<script src="/assets/event-reducer.0.1.0.js" defer></script>'
EXPECTED = (
    "PASS:atelier-shell,unavailable-workflow,workflow-studio-preflight,workflow-studio-stale-edit,workflow-studio-contract,"
    "workflow-studio-malformed-rejection,workflow-studio-rejected,workflow-studio-transport,"
    "workflow-studio-invalid-unicode,"
    "workflow-catalog-save,workflow-catalog-stale-edit,workflow-catalog-conflict,"
    "workflow-catalog-unknown-recovery,workflow-catalog-malformed-success,"
    "workflow-catalog-pagination,workflow-catalog-digest-mismatch,workflow-catalog-record-switch,"
    "memory-skill-surface,workflow-surface,"
    "workflow-projection-contract,cancelled-recovery,workflow-refresh-burst,workflow-refresh-switch,"
    "stale-status,same-run-epoch,cold-events,late-sse,artifact-stale,create-run,approval"
)
BROWSER_TIMEOUT_SECONDS = 35
BROWSER_ATTEMPTS = 2
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_WIDTH = 1600
SCREENSHOT_HEIGHT = 1000
MIN_SCREENSHOT_BYTES = 1024


def browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("SASORI_BROWSER_BINARY")
    if explicit:
        candidates.append(Path(explicit))
    for command in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "msedge"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    for variable, relative in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / relative)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if candidate.is_file() and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/":
            page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
            if page.count(SCRIPT_MARKER) != 1:
                self.send_error(500, "production reducer asset marker is missing or ambiguous")
                return
            injected = page.replace(
                SCRIPT_MARKER,
                '<script src="/workbench-browser-fixture.js"></script>\n  ' + SCRIPT_MARKER,
                1,
            ).encode("utf-8")
            self._send(injected, "text/html; charset=utf-8")
            return
        if self.path == "/workbench-browser-fixture.js":
            self._send(FIXTURE.read_bytes(), "text/javascript; charset=utf-8")
            return
        if self.path.startswith("/assets/"):
            name = self.path.removeprefix("/assets/")
            if "/" in name or "\\" in name or name not in {
                "app.0.1.0.css",
                "app.0.2.0.css",
                "artifacts.0.1.0.css",
                "app.0.1.2.js",
                "app.0.1.3.js",
                "app.0.1.4.js",
                "app.0.2.0.js",
                "workflow.0.1.0.css",
                "workflow.0.1.0.js",
                "workflow.0.2.0.js",
                "workflow-manifest.0.1.0.js",
                "workflow-studio.0.1.0.css",
                "workflow-studio.0.1.0.js",
                "workflow-studio.0.2.0.css",
                "workflow-studio.0.2.0.js",
                "event-reducer.0.1.0.js",
                "mark.0.1.0.svg",
            }:
                self.send_error(404)
                return
            content_type = {
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }[Path(name).suffix]
            self._send((WEB_ROOT / name).read_bytes(), content_type)
            return
        self.send_error(404)

    def _send(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_fixture_server() -> http.server.ThreadingHTTPServer:
    # Chromium blocks several legacy service ports before making an HTTP
    # request. An OS-assigned port can therefore make a correct browser test
    # fail with ERR_UNSAFE_PORT. This bounded high-port search is deterministic
    # and still avoids assuming that one fixed developer port is available.
    for port in range(18080, 18180):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), FixtureHandler)
        except OSError:
            continue
        server.daemon_threads = True
        return server
    raise RuntimeError("no safe loopback port is available for Workbench browser acceptance")


def installed_directory_version(binary: Path) -> str | None:
    try:
        if not binary.parent.is_dir():
            return None
        installed = sorted(
            (
                child.name
                for child in binary.parent.iterdir()
                if child.is_dir()
                and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", child.name)
            ),
            key=lambda value: tuple(int(part) for part in value.split(".")),
        )
    except OSError:
        return None
    return installed[-1] if installed else None


def browser_version(binary: Path) -> str:
    installed = installed_directory_version(binary)
    if binary.suffix.lower() == ".exe" and installed is not None:
        return f"{binary.name} {installed}"

    completed = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    raw = completed.stdout or completed.stderr
    decoded = raw.decode(locale.getpreferredencoding(False), errors="replace")
    match = re.search(r"\d+\.\d+\.\d+\.\d+", decoded)
    version = match.group(0) if match else installed or "version-unavailable"
    return f"{binary.name} {version}"


def validate_screenshot(
    path: Path,
    expected_size: tuple[int, int] = (SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT),
) -> None:
    if not path.is_file():
        raise AssertionError("headless browser did not produce the requested screenshot")
    if path.stat().st_size < MIN_SCREENSHOT_BYTES:
        raise AssertionError("headless browser screenshot is unexpectedly small")
    with path.open("rb") as stream:
        header = stream.read(24)
    if (
        len(header) != 24
        or header[:8] != PNG_SIGNATURE
        or header[12:16] != b"IHDR"
    ):
        raise AssertionError("headless browser screenshot is not a canonical PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if (width, height) != expected_size:
        raise AssertionError(
            "headless browser screenshot dimensions are "
            f"{width}x{height}, expected {expected_size[0]}x{expected_size[1]}"
        )


class _DevToolsSocket:
    """Small standard-library WebSocket client for local Chrome DevTools."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str, timeout_seconds: float) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname is None or parsed.port is None:
            raise RuntimeError("Chrome returned an invalid local DevTools WebSocket URL")
        self._socket = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=timeout_seconds
        )
        self._socket.settimeout(timeout_seconds)
        self._buffer = bytearray()
        self._next_id = 0
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        header, remainder = response.split(b"\r\n\r\n", 1)
        if not header.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("Chrome rejected the local DevTools WebSocket upgrade")
        expected = base64.b64encode(
            hashlib.sha1(f"{key}{self._GUID}".encode("ascii")).digest()
        ).decode("ascii")
        accept = None
        for line in header.split(b"\r\n")[1:]:
            name, separator, value = line.partition(b":")
            if separator and name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip().decode("ascii", errors="replace")
                break
        if accept != expected:
            raise RuntimeError("Chrome returned an invalid DevTools WebSocket handshake")
        self._buffer.extend(remainder)

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise RuntimeError("Chrome closed the DevTools connection")
            self._buffer.extend(chunk)
        end = self._buffer.index(marker) + len(marker)
        value = bytes(self._buffer[:end])
        del self._buffer[:end]
        return value

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._socket.recv(max(65536, size - len(self._buffer)))
            if not chunk:
                raise RuntimeError("Chrome closed the DevTools connection")
            self._buffer.extend(chunk)
        value = bytes(self._buffer[:size])
        del self._buffer[:size]
        return value

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        size = len(payload)
        if size < 126:
            header = bytes((0x80 | opcode, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((0x80 | opcode, 0xFE)) + size.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0xFF)) + size.to_bytes(8, "big")
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _read_message(self) -> str:
        chunks: list[bytes] = []
        started = False
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = int.from_bytes(self._read_exact(2), "big")
            elif size == 127:
                size = int.from_bytes(self._read_exact(8), "big")
            masked = bool(second & 0x80)
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(size)
            if mask is not None:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise RuntimeError("Chrome closed the DevTools WebSocket")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x1:
                chunks = [payload]
                started = True
            elif opcode == 0x0 and started:
                chunks.append(payload)
            else:
                continue
            if final:
                return b"".join(chunks).decode("utf-8")

    def command(self, method: str, params: dict[str, object] | None = None) -> dict:
        self._next_id += 1
        message_id = self._next_id
        self._send_frame(
            json.dumps(
                {"id": message_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        while True:
            message = json.loads(self._read_message())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(
                    f"Chrome DevTools command {method} failed: {message['error']}"
                )
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError(f"Chrome DevTools command {method} was malformed")
            return result

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        self._socket.close()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _devtools_target(port: int, deadline: float) -> str:
    url = f"http://127.0.0.1:{port}/json/list"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.4) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except (OSError, ValueError) as error:
            last_error = error
        time.sleep(0.04)
    raise RuntimeError("Chrome DevTools target did not become available") from last_error


def _evaluated_value(client: _DevToolsSocket, expression: str) -> object:
    result = client.command(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    remote = result.get("result", {})
    if not isinstance(remote, dict) or "value" not in remote:
        return None
    return remote["value"]


def run_emulated_browser_process(
    binary: Path,
    port: int,
    *,
    viewport: tuple[int, int],
    extra_arguments: tuple[str, ...] = (),
    browser_path: str = "/",
    screenshot: Path | None = None,
    attempts: int = BROWSER_ATTEMPTS,
    timeout_seconds: int = BROWSER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run acceptance at an exact CSS viewport below Chromium's 500px floor."""

    last_error: Exception | None = None
    for _ in range(attempts):
        if screenshot is not None:
            screenshot.unlink(missing_ok=True)
        debug_port = _free_loopback_port()
        with tempfile.TemporaryDirectory(prefix="sasori-browser-emulated-") as profile:
            command = [
                str(binary),
                "--headless=new",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-sandbox",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ]
            command[1:1] = list(extra_arguments)
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            client: _DevToolsSocket | None = None
            deadline = time.monotonic() + timeout_seconds
            try:
                target = _devtools_target(debug_port, deadline)
                client = _DevToolsSocket(
                    target, timeout_seconds=max(1.0, deadline - time.monotonic())
                )
                client.command("Page.enable")
                client.command("Runtime.enable")
                client.command(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": viewport[0],
                        "height": viewport[1],
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                client.command(
                    "Page.navigate",
                    {"url": f"http://127.0.0.1:{port}{browser_path}"},
                )
                result = None
                while time.monotonic() < deadline:
                    try:
                        result = _evaluated_value(
                            client,
                            "document.querySelector('#sasori-browser-result')?.dataset.result || null",
                        )
                    except RuntimeError:
                        result = None
                    if result in {"passed", "failed"}:
                        break
                    time.sleep(0.025)
                if result not in {"passed", "failed"}:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                dom = _evaluated_value(client, "document.documentElement.outerHTML")
                if not isinstance(dom, str):
                    raise RuntimeError("Chrome DevTools did not return the accepted DOM")
                if screenshot is not None:
                    captured = client.command(
                        "Page.captureScreenshot",
                        {
                            "format": "png",
                            "fromSurface": True,
                            "captureBeyondViewport": False,
                        },
                    )
                    encoded = captured.get("data")
                    if not isinstance(encoded, str):
                        raise RuntimeError("Chrome DevTools screenshot was malformed")
                    screenshot.write_bytes(base64.b64decode(encoded, validate=True))
                    validate_screenshot(screenshot, viewport)
                return subprocess.CompletedProcess(command, 0, stdout=dom, stderr="")
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                last_error = error
            finally:
                if client is not None:
                    try:
                        client.command("Browser.close")
                    except (OSError, RuntimeError):
                        pass
                    client.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
    raise RuntimeError(
        f"emulated browser failed after {attempts} attempts at "
        f"{viewport[0]}x{viewport[1]}"
    ) from last_error


def run_browser_process(
    binary: Path,
    port: int,
    *,
    virtual_time_budget: int = 10000,
    screenshot: Path | None = None,
    attempts: int = BROWSER_ATTEMPTS,
    timeout_seconds: int = BROWSER_TIMEOUT_SECONDS,
    window_size: tuple[int, int] = (SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT),
    extra_arguments: tuple[str, ...] = (),
    browser_path: str = "/",
) -> subprocess.CompletedProcess[str]:
    last_timeout: subprocess.TimeoutExpired | None = None
    for _ in range(attempts):
        if screenshot is not None:
            screenshot.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="sasori-browser-") as profile:
            command = [
                str(binary),
                "--headless=new",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-sandbox",
                "--run-all-compositor-stages-before-draw",
                f"--window-size={window_size[0]},{window_size[1]}",
                f"--user-data-dir={profile}",
                f"--virtual-time-budget={virtual_time_budget}",
                "--dump-dom",
            ]
            command.extend(extra_arguments)
            if screenshot is not None:
                command.append(f"--screenshot={screenshot}")
            command.append(f"http://127.0.0.1:{port}{browser_path}")
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
                if completed.returncode == 0 and screenshot is not None:
                    validate_screenshot(screenshot, window_size)
                return completed
            except subprocess.TimeoutExpired as exc:
                last_timeout = exc
    raise RuntimeError(
        "headless browser process timed out after "
        f"{attempts} attempts of {timeout_seconds} seconds"
    ) from last_timeout


def run_acceptance(binary: Path) -> dict[str, object]:
    server = create_fixture_server()
    thread = threading.Thread(target=server.serve_forever, name="sasori-browser-fixture", daemon=True)
    thread.start()
    try:
        profiles = [
            (
                "desktop",
                run_browser_process(binary, server.server_address[1]),
            ),
            (
                "narrow-reduced",
                run_emulated_browser_process(
                    binary,
                    server.server_address[1],
                    viewport=(390, 844),
                    extra_arguments=("--force-prefers-reduced-motion",),
                    browser_path="/#profile=narrow-reduced",
                ),
            ),
            (
                "narrow-360-reduced",
                run_emulated_browser_process(
                    binary,
                    server.server_address[1],
                    viewport=(360, 800),
                    extra_arguments=("--force-prefers-reduced-motion",),
                    browser_path="/#profile=narrow-360-reduced",
                ),
            ),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    for profile, completed in profiles:
        if completed.returncode != 0:
            raise RuntimeError(
                f"headless browser profile {profile} exited {completed.returncode}\n"
                f"stdout tail:\n{completed.stdout[-4000:]}\n"
                f"stderr tail:\n{completed.stderr[-4000:]}"
            )
        if EXPECTED not in completed.stdout or 'data-result="passed"' not in completed.stdout:
            raise AssertionError(
                f"Workbench browser profile {profile} did not reach the pass marker\n"
                f"stdout tail:\n{completed.stdout[-6000:]}\n"
                f"stderr tail:\n{completed.stderr[-4000:]}"
            )
    return {
        "browser": browser_version(binary),
        "browser_profiles": [
            "desktop-1600x1000",
            "narrow-390x844-reduced-motion",
            "narrow-360x800-reduced-motion",
        ],
        "cases": EXPECTED.removeprefix("PASS:").split(","),
        "bundled_assets": [
            "event-reducer.0.1.0.js",
            "app.0.1.2.js",
            "app.0.1.3.js",
            "app.0.2.0.css",
            "app.0.2.0.js",
            "workflow.0.2.0.js",
            "workflow-manifest.0.1.0.js",
            "workflow-studio.0.1.0.css",
            "workflow-studio.0.1.0.js",
            "workflow-studio.0.2.0.css",
            "workflow-studio.0.2.0.js",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Sasori Workbench acceptance in a real browser.")
    parser.add_argument("--require-browser", action="store_true", help="fail instead of skipping when Chrome/Edge is unavailable")
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
