from __future__ import annotations

import argparse
import http.server
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "sasori_web"
FIXTURE = Path(__file__).with_name("workbench_browser_fixture.js")
SCRIPT_MARKER = '<script src="/assets/event-reducer.0.1.0.js" defer></script>'
EXPECTED = "PASS:stale-status,same-run-epoch,cold-events,late-sse,create-run,approval"
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
                "app.0.1.2.js",
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


def validate_screenshot(path: Path) -> None:
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
    if (width, height) != (SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT):
        raise AssertionError(
            "headless browser screenshot dimensions are "
            f"{width}x{height}, expected {SCREENSHOT_WIDTH}x{SCREENSHOT_HEIGHT}"
        )


def run_browser_process(
    binary: Path,
    port: int,
    *,
    virtual_time_budget: int = 10000,
    screenshot: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    last_timeout: subprocess.TimeoutExpired | None = None
    for _ in range(BROWSER_ATTEMPTS):
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
                "--window-size=1600,1000",
                f"--user-data-dir={profile}",
                f"--virtual-time-budget={virtual_time_budget}",
                "--dump-dom",
            ]
            if screenshot is not None:
                command.append(f"--screenshot={screenshot}")
            command.append(f"http://127.0.0.1:{port}/")
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=BROWSER_TIMEOUT_SECONDS,
                    check=False,
                )
                if completed.returncode == 0 and screenshot is not None:
                    validate_screenshot(screenshot)
                return completed
            except subprocess.TimeoutExpired as exc:
                last_timeout = exc
    raise RuntimeError(
        "headless browser process timed out after "
        f"{BROWSER_ATTEMPTS} attempts of {BROWSER_TIMEOUT_SECONDS} seconds"
    ) from last_timeout


def run_acceptance(binary: Path) -> dict[str, object]:
    server = create_fixture_server()
    thread = threading.Thread(target=server.serve_forever, name="sasori-browser-fixture", daemon=True)
    thread.start()
    try:
        completed = run_browser_process(binary, server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if completed.returncode != 0:
        raise RuntimeError(
            f"headless browser exited {completed.returncode}\n"
            f"stdout tail:\n{completed.stdout[-4000:]}\n"
            f"stderr tail:\n{completed.stderr[-4000:]}"
        )
    if EXPECTED not in completed.stdout or 'data-result="passed"' not in completed.stdout:
        raise AssertionError(
            "Workbench browser acceptance did not reach the pass marker\n"
            f"stdout tail:\n{completed.stdout[-6000:]}\n"
            f"stderr tail:\n{completed.stderr[-4000:]}"
        )
    return {
        "browser": browser_version(binary),
        "cases": EXPECTED.removeprefix("PASS:").split(","),
        "production_assets": ["event-reducer.0.1.0.js", "app.0.1.2.js"],
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
