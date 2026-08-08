import asyncio
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, ModelReply, ToolCall  # noqa: E402
from sasori.plugins import validate_registration  # noqa: E402
from sasori_plugins.web_fetch import (  # noqa: E402
    ALLOWED_HOSTS_ENV,
    _DNS_RESOLVER_LIMIT,
    _DNS_RESOLVER_SLOTS,
    WebFetchConfigurationError,
    WebFetchHTTPError,
    WebFetchProtocolError,
    WebFetchTimeoutError,
    WebFetchURLError,
    register,
    web_fetch_manifest,
    web_fetch_registration,
)
from test_providers import LocalJSONServer, _TLS_CERT, _TLS_KEY  # noqa: E402


class _FetchModel:
    def __init__(self, url: str) -> None:
        self.url = url
        self.tool_output = ""

    async def complete(self, messages, tools):
        if not any(message.role == "tool" for message in messages):
            return ModelReply(
                tool_calls=(ToolCall("fetch-1", "fetch_url", {"url": self.url}),)
            )
        self.tool_output = messages[-1].content
        return ModelReply(content="done")


class WebFetchPluginTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cert_path = Path(cls.temp.name) / "cert.pem"
        key_path = Path(cls.temp.name) / "key.pem"
        cert_path.write_text(_TLS_CERT, encoding="ascii")
        key_path.write_text(_TLS_KEY, encoding="ascii")
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cert_path, key_path)
        cls.client_context = ssl.create_default_context(cafile=str(cert_path))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.seen_sni = []
        self.server_context.set_servername_callback(
            lambda connection, name, context: self.seen_sni.append(name)
        )
        self.server = LocalJSONServer(self.server_context)
        self.origin = f"localhost:{self.server.server.server_port}"
        self.resolutions = []
        self.addresses = {"localhost": "127.0.0.1"}

    def tearDown(self):
        self.server.close()
        self.server_context.set_servername_callback(None)

    def resolver(self, host, port, family, socktype, protocol):
        self.resolutions.append((host, port))
        address = self.addresses.get(host, "127.0.0.1")
        resolved_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if resolved_family == socket.AF_INET6 else (address, port)
        return [(resolved_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    def registration(self, allowed=None, **options):
        configuration = {
            "_resolver": self.resolver,
            "_ssl_context": self.client_context,
            "_test_allowed_ips": ("127.0.0.1",),
        }
        configuration.update(options)
        return web_fetch_registration(
            (self.origin,) if allowed is None else allowed,
            **configuration,
        )

    def handler(self, allowed=None, **options):
        return self.registration(allowed, **options).tools[0].handler

    def url(self, path="/"):
        return f"https://{self.origin}{path}"

    def test_manifest_registration_and_entrypoint_default_deny(self):
        manifest = web_fetch_manifest()
        registration = self.registration()
        self.assertIs(validate_registration(manifest, registration), registration)
        self.assertEqual(manifest.execution.entry_point_value, "sasori_plugins.web_fetch:register")
        self.assertEqual(manifest.permissions.network_egress, ("https:SASORI_WEB_ALLOWED_HOSTS",))
        self.assertEqual(manifest.permissions.secrets, ())
        self.assertEqual(registration.tools[0].effect, "read_only")

        with patch.dict(os.environ, {ALLOWED_HOSTS_ENV: ""}):
            denied = register().tools[0].handler
        with self.assertRaisesRegex(WebFetchURLError, "not explicitly allowed"):
            denied("https://example.com/")

        with patch.dict(os.environ, {ALLOWED_HOSTS_ENV: self.origin}):
            configured = register()
        self.assertIs(validate_registration(manifest, configured), configured)

    def test_allowlist_and_url_boundary_fail_closed_before_dns(self):
        with self.assertRaises(WebFetchConfigurationError):
            web_fetch_registration(("*.example.com",))
        with self.assertRaises(WebFetchConfigurationError):
            web_fetch_registration(("xn--tst-bma.example",))
        with self.assertRaises(WebFetchConfigurationError):
            web_fetch_registration(("example.com", "EXAMPLE.COM"))

        def no_dns(*args):
            raise AssertionError("invalid URLs must fail before DNS")

        handler = web_fetch_registration(
            ("example.com", "example.com:8443"), _resolver=no_dns
        ).tools[0].handler
        cases = (
            "http://example.com/",
            "https://user@example.com/",
            "https://example.com/#fragment",
            "https://tést.example/",
            "https://xn--tst-bma.example/",
            "https://sub.example.com/",
            "https://example.com:444/",
            "https://93.184.216.34/",
            "https://example.com/%zz",
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(WebFetchURLError):
                    handler(value)

    def test_dns_rebinding_and_every_forbidden_address_are_rejected(self):
        forbidden = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
            "::ffff:8.8.8.8",
        )

        def resolver_for(address):
            def resolve(host, port, family, socktype, protocol):
                resolved_family = socket.AF_INET6 if ":" in address else socket.AF_INET
                sockaddr = (address, port, 0, 0) if resolved_family == socket.AF_INET6 else (address, port)
                return [(resolved_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

            return resolve

        for address in forbidden:
            handler = web_fetch_registration(
                ("example.test",), _resolver=resolver_for(address)
            ).tools[0].handler
            with self.subTest(address=address):
                with self.assertRaisesRegex(WebFetchURLError, "forbidden address"):
                    handler("https://example.test/")

        def mixed(host, port, family, socktype, protocol):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
            ]

        handler = web_fetch_registration(
            ("example.test",), _resolver=mixed
        ).tools[0].handler
        with self.assertRaisesRegex(WebFetchURLError, "forbidden address"):
            handler("https://example.test/")

    def test_dns_resolution_obeys_total_deadline_without_connecting(self):
        release = threading.Event()
        finished = threading.Event()

        def stalled(host, port, family, socktype, protocol):
            try:
                release.wait(1)
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("8.8.8.8", port),
                    )
                ]
            finally:
                finished.set()

        handler = web_fetch_registration(
            ("example.test",),
            _resolver=stalled,
            total_timeout=0.05,
        ).tools[0].handler
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(WebFetchTimeoutError, "total deadline"):
                handler("https://example.test/")
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_dns_slot_wait_does_not_reset_the_total_deadline(self):
        held = 0
        for _ in range(_DNS_RESOLVER_LIMIT):
            self.assertTrue(_DNS_RESOLVER_SLOTS.acquire(blocking=False))
            held += 1
        one_released = threading.Event()
        resolver_release = threading.Event()
        resolver_started = threading.Event()
        resolver_finished = threading.Event()

        def release_one_slot():
            _DNS_RESOLVER_SLOTS.release()
            one_released.set()

        def stalled(host, port, family, socktype, protocol):
            resolver_started.set()
            try:
                resolver_release.wait(1)
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("8.8.8.8", port),
                    )
                ]
            finally:
                resolver_finished.set()

        timer = threading.Timer(0.22, release_one_slot)
        timer.daemon = True
        timer.start()
        handler = web_fetch_registration(
            ("example.test",),
            _resolver=stalled,
            total_timeout=0.3,
        ).tools[0].handler
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(WebFetchTimeoutError, "total deadline"):
                handler("https://example.test/")
            self.assertLess(time.monotonic() - started, 0.42)
            self.assertTrue(one_released.is_set())
            self.assertTrue(resolver_started.is_set())
        finally:
            resolver_release.set()
            if resolver_started.is_set():
                self.assertTrue(resolver_finished.wait(1))
            timer.cancel()
            timer.join()
            for _ in range(held - int(one_released.is_set())):
                _DNS_RESOLVER_SLOTS.release()

    def test_stalled_dns_workers_are_process_bounded(self):
        release = threading.Event()
        all_slots_started = threading.Event()
        all_resolvers_finished = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def stalled(host, port, family, socktype, protocol):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == _DNS_RESOLVER_LIMIT:
                    all_slots_started.set()
            try:
                release.wait(2)
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("8.8.8.8", port),
                    )
                ]
            finally:
                with lock:
                    active -= 1
                    if active == 0:
                        all_resolvers_finished.set()

        handler = web_fetch_registration(
            ("example.test",),
            _resolver=stalled,
            total_timeout=0.2,
        ).tools[0].handler
        failures = []

        def invoke():
            try:
                handler("https://example.test/")
            except WebFetchTimeoutError:
                return
            except Exception as error:
                failures.append(error)
                return
            failures.append(AssertionError("stalled DNS unexpectedly completed"))

        callers = [
            threading.Thread(target=invoke)
            for _ in range(_DNS_RESOLVER_LIMIT)
        ]
        extras = []
        try:
            for caller in callers:
                caller.start()
            self.assertTrue(all_slots_started.wait(1))
            extras = [threading.Thread(target=invoke) for _ in range(3)]
            for caller in extras:
                caller.start()
            for caller in callers + extras:
                caller.join(1)
                self.assertFalse(caller.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(peak, _DNS_RESOLVER_LIMIT)
            with lock:
                self.assertEqual(active, _DNS_RESOLVER_LIMIT)
        finally:
            release.set()
            self.assertTrue(all_resolvers_finished.wait(1))

    def test_real_tls_uses_pinned_ip_original_host_and_no_proxy(self):
        self.server.queue(
            raw=b"hello",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        handler = self.handler()
        proxy_environment = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }
        with patch.dict(os.environ, proxy_environment), patch(
            "sasori_plugins.web_fetch.socket.getaddrinfo",
            side_effect=AssertionError("pinned connection must not resolve again"),
        ):
            output = handler(self.url("/page?q=1"))

        self.assertIn("[UNTRUSTED EXTERNAL CONTENT]", output)
        self.assertIn(f"Final URL: {self.url('/page?q=1')}", output)
        self.assertIn("Content-Type: text/plain; charset=utf-8", output)
        self.assertIn("Truncated: false", output)
        self.assertTrue(output.endswith("hello"))
        self.assertEqual(self.resolutions, [("localhost", self.server.server.server_port)])
        request = self.server.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/page?q=1")
        self.assertEqual(request["headers"]["Host"], self.origin)
        self.assertEqual(request["headers"]["Accept-Encoding"], "identity")
        self.assertNotIn("Cookie", request["headers"])
        self.assertEqual(self.seen_sni, ["localhost"])

    def test_redirect_revalidates_and_records_final_url(self):
        self.server.queue(raw=b"", status=302, headers={"Location": "/final"})
        self.server.queue(
            raw=b"done", headers={"Content-Type": "application/json; charset=utf-8"}
        )
        output = self.handler()(self.url("/start"))
        self.assertIn(f"Final URL: {self.url('/final')}", output)
        self.assertTrue(output.endswith("done"))
        self.assertEqual([request["path"] for request in self.server.requests], ["/start", "/final"])
        self.assertEqual(len(self.resolutions), 2)

    def test_redirect_limit_is_shared_with_the_original_deadline(self):
        self.server.queue(raw=b"", status=302, headers={"Location": "/second"})
        self.server.queue(raw=b"", status=302, headers={"Location": "/third"})
        with self.assertRaisesRegex(WebFetchProtocolError, "redirect limit"):
            self.handler(max_redirects=1)(self.url("/first"))
        self.assertEqual(
            [request["path"] for request in self.server.requests],
            ["/first", "/second"],
        )

    def test_redirect_to_unlisted_or_private_target_is_rejected(self):
        self.server.queue(
            raw=b"", status=302, headers={"Location": "https://unlisted.test/"}
        )
        with self.assertRaisesRegex(WebFetchURLError, "not explicitly allowed"):
            self.handler()(self.url("/unlisted"))

        self.addresses["metadata.test"] = "169.254.169.254"
        self.server.queue(
            raw=b"", status=302, headers={"Location": "https://metadata.test/"}
        )
        with self.assertRaisesRegex(WebFetchURLError, "forbidden address"):
            self.handler((self.origin, "metadata.test"))(self.url("/private"))
        self.assertEqual(len(self.server.requests), 2)

    def test_content_length_and_actual_body_limits_fail_closed(self):
        handler = self.handler(max_response_bytes=16)
        self.server.queue(
            raw=b"x",
            headers={"Content-Type": "text/plain", "Content-Length": "17"},
        )
        with self.assertRaisesRegex(WebFetchProtocolError, "byte limit"):
            handler(self.url("/declared"))

        self.server.queue(
            raw=b"x" * 17,
            headers={"Content-Type": "text/plain"},
            content_length=False,
        )
        with self.assertRaisesRegex(WebFetchProtocolError, "byte limit"):
            handler(self.url("/actual"))

        self.server.queue(
            raw=b"x",
            headers={"Content-Type": "text/plain", "Content-Length": "3"},
        )
        with self.assertRaisesRegex(WebFetchProtocolError, "before Content-Length"):
            handler(self.url("/short"))

    def test_slow_drip_obeys_monotonic_total_deadline(self):
        self.server.response_finished.clear()
        self.server.queue(
            raw=b"",
            headers={"Content-Type": "text/plain"},
            content_length=False,
            chunks=[b"x"] * 20,
            chunk_delay=0.04,
        )
        started = time.monotonic()
        with self.assertRaisesRegex(WebFetchTimeoutError, "total deadline"):
            self.handler(
                connect_timeout=1,
                read_timeout=1,
                total_timeout=0.12,
            )(self.url("/slow"))
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertTrue(self.server.response_finished.wait(1))

    def test_encoding_mime_and_utf8_are_strict(self):
        cases = (
            ({"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"text"),
            ({"Content-Type": "text/plain; charset=iso-8859-1"}, b"text"),
            ({"Content-Type": "application/octet-stream"}, b"text"),
            ({"Content-Type": "text/plain"}, b"\xff"),
        )
        handler = self.handler()
        for index, (headers, raw) in enumerate(cases):
            self.server.queue(raw=raw, headers=headers)
            with self.subTest(index=index):
                with self.assertRaises(WebFetchProtocolError):
                    handler(self.url(f"/invalid-{index}"))

    def test_http_429_has_no_hidden_retry(self):
        self.server.queue(raw=b"rate limited", status=429)
        self.server.queue(raw=b"would succeed")
        with self.assertRaises(WebFetchHTTPError) as raised:
            self.handler()(self.url("/limited"))
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(self.server.requests), 1)

    def test_output_is_bounded_and_reports_truncation(self):
        self.server.queue(raw=b"x" * 200, headers={"Content-Type": "text/plain"})
        output = self.handler(
            max_response_bytes=256,
            max_output_chars=160,
        )(self.url("/long"))
        self.assertEqual(len(output), 160)
        self.assertIn("Truncated: true", output)

    async def test_harness_executes_read_only_fetch_without_approval(self):
        self.server.queue(raw=b"tool text", headers={"Content-Type": "text/plain"})
        registration = self.registration()
        model = _FetchModel(self.url("/tool"))
        with Harness(model, registration.tools) as harness:
            result = await harness.run((Message("user", "Fetch the page."),))
        self.assertEqual(result.final_message.content, "done")
        self.assertIn("[UNTRUSTED EXTERNAL CONTENT]", model.tool_output)
        self.assertNotIn("approval.requested", [event.type for event in result.events])
        self.assertEqual(len(self.server.requests), 1)


if __name__ == "__main__":
    unittest.main()
