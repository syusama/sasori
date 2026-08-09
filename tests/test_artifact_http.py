import hashlib
import http.client
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, ModelReply  # noqa: E402
from sasori.server import create_server  # noqa: E402


class _Model:
    async def complete(self, messages, tools):
        return ModelReply(content=f"永恒:{messages[-1].content}")


class ArtifactHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = str(self.base / "server.sqlite3")
        self.artifact_root = self.base / "artifacts"
        self.module = types.ModuleType("sasori_artifact_http_test_app")
        self.module.create = lambda store: Harness(_Model(), store=store)
        sys.modules[self.module.__name__] = self.module
        self.running = []
        self.token = "artifact-test-token"

    def tearDown(self):
        failure = None
        for server, thread in reversed(self.running):
            try:
                server.shutdown()
                server.server_close()
                thread.join(5)
            except BaseException as error:
                failure = failure or error
        sys.modules.pop(self.module.__name__, None)
        self.temp.cleanup()
        if failure is not None:
            raise failure

    def start(self, *, publish_final_artifact=False):
        server = create_server(
            "127.0.0.1",
            0,
            database=self.database,
            artifact_root=self.artifact_root,
            app=f"{self.module.__name__}:create",
            token=self.token,
            publish_final_artifact=publish_final_artifact,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.running.append((server, thread))
        return server

    def stop(self, server):
        entry = next(item for item in self.running if item[0] is server)
        server.shutdown()
        server.server_close()
        entry[1].join(5)
        self.running.remove(entry)

    def request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, payload, dict(response.getheaders())
        connection.close()
        return result

    @staticmethod
    def json_body(payload):
        return json.loads(payload.decode("utf-8"))

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def create_run(self, server, run_id):
        status, payload, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": run_id, "input": run_id},
            self.auth,
        )
        self.assertEqual(status, 200)
        return self.json_body(payload)

    def test_list_content_head_range_auth_and_run_association(self):
        server = self.start(publish_final_artifact=True)
        completed = self.create_run(server, "artifact-http-1")

        status, _, _ = self.request(
            server, "GET", "/v1/runs/artifact-http-1/artifacts"
        )
        self.assertEqual(status, 401)
        status, payload, _ = self.request(
            server,
            "GET",
            "/v1/runs/artifact-http-1/artifacts",
            headers=self.auth,
        )
        self.assertEqual(status, 200)
        listed = self.json_body(payload)
        self.assertEqual(listed["run_id"], "artifact-http-1")
        self.assertEqual(len(listed["artifacts"]), 1)
        ref = listed["artifacts"][0]
        self.assertEqual(
            set(ref),
            {
                "version",
                "artifact_id",
                "run_id",
                "content_sha256",
                "size_bytes",
                "filename",
                "media_type",
                "created_seq",
            },
        )
        self.assertEqual(ref["created_seq"], completed["latest_seq"])
        self.assertNotIn(str(self.artifact_root), json.dumps(listed))

        content_path = (
            f"/v1/runs/artifact-http-1/artifacts/{ref['artifact_id']}/content"
        )
        status, content, headers = self.request(
            server, "GET", content_path, headers=self.auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(content))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(
            headers["ETag"], f'"sha256-{hashlib.sha256(content).hexdigest()}"'
        )
        self.assertEqual(hashlib.sha256(content).hexdigest(), ref["content_sha256"])

        status, head, head_headers = self.request(
            server, "HEAD", content_path, headers=self.auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(head, b"")
        self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])
        self.assertEqual(head_headers["ETag"], headers["ETag"])

        status, ranged, ranged_headers = self.request(
            server,
            "GET",
            content_path,
            headers={**self.auth, "Range": "bytes=0-3"},
        )
        self.assertEqual((status, ranged), (206, content[:4]))
        self.assertEqual(
            ranged_headers["Content-Range"], f"bytes 0-3/{len(content)}"
        )
        status, suffix, _ = self.request(
            server,
            "GET",
            content_path,
            headers={**self.auth, "Range": "bytes=-5"},
        )
        self.assertEqual((status, suffix), (206, content[-5:]))

        for value in ("items=0-1", "bytes=0-1,3-4", "bytes=999999-", "bytes=-0"):
            with self.subTest(value=value):
                status, error, invalid_headers = self.request(
                    server,
                    "GET",
                    content_path,
                    headers={**self.auth, "Range": value},
                )
                self.assertEqual(status, 416)
                self.assertEqual(
                    self.json_body(error)["error"]["code"], "range_not_satisfiable"
                )
                self.assertEqual(
                    invalid_headers["Content-Range"], f"bytes */{len(content)}"
                )

        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.putrequest("GET", content_path)
        connection.putheader("Authorization", f"Bearer {self.token}")
        connection.putheader("Range", "bytes=0-1")
        connection.putheader("Range", "bytes=2-3")
        connection.endheaders()
        repeated = connection.getresponse()
        repeated_body = repeated.read()
        repeated_headers = dict(repeated.getheaders())
        connection.close()
        self.assertEqual(repeated.status, 416)
        self.assertEqual(
            self.json_body(repeated_body)["error"]["code"],
            "range_not_satisfiable",
        )
        self.assertEqual(
            repeated_headers["Content-Range"], f"bytes */{len(content)}"
        )

        status, error, _ = self.request(
            server, "GET", content_path + "?download=1", headers=self.auth
        )
        self.assertEqual((status, self.json_body(error)["error"]["code"]), (422, "invalid_request"))

        self.create_run(server, "artifact-http-2")
        cross_path = (
            f"/v1/runs/artifact-http-2/artifacts/{ref['artifact_id']}/content"
        )
        missing_path = (
            "/v1/runs/artifact-http-2/artifacts/artifact-does-not-exist/content"
        )
        cross = self.request(server, "GET", cross_path, headers=self.auth)
        missing = self.request(server, "GET", missing_path, headers=self.auth)
        self.assertEqual(cross[0], 404)
        self.assertEqual(missing[0], 404)
        self.assertEqual(self.json_body(cross[1]), self.json_body(missing[1]))

        events_before = self.request(
            server,
            "GET",
            "/v1/runs/artifact-http-1/events?after_seq=0",
            headers=self.auth,
        )[1]
        list_before = self.request(
            server,
            "GET",
            "/v1/runs/artifact-http-1/artifacts",
            headers=self.auth,
        )[1]
        self.stop(server)
        restarted = self.start(publish_final_artifact=True)
        self.assertEqual(
            self.request(
                restarted,
                "GET",
                "/v1/runs/artifact-http-1/events?after_seq=0",
                headers=self.auth,
            )[1],
            events_before,
        )
        self.assertEqual(
            self.request(
                restarted,
                "GET",
                "/v1/runs/artifact-http-1/artifacts",
                headers=self.auth,
            )[1],
            list_before,
        )

    def test_opt_in_host_policy_restart_and_same_size_tamper(self):
        server = self.start(publish_final_artifact=False)
        completed = self.create_run(server, "manual-artifact")
        status, payload, _ = self.request(
            server,
            "GET",
            "/v1/runs/manual-artifact/artifacts",
            headers=self.auth,
        )
        self.assertEqual((status, self.json_body(payload)["artifacts"]), (200, []))
        original_latest = completed["latest_seq"]

        published = server.owner.call(
            server.owner.publish_artifact(
                "manual-artifact",
                b"",
                filename="empty.txt",
                declared_media_type="text/plain",
            ),
            5,
        )
        self.assertEqual(published["created_seq"], original_latest + 1)
        content_path = (
            f"/v1/runs/manual-artifact/artifacts/{published['artifact_id']}/content"
        )
        status, body, headers = self.request(
            server, "GET", content_path, headers=self.auth
        )
        self.assertEqual((status, body, headers["Content-Length"]), (200, b"", "0"))
        status, error, range_headers = self.request(
            server,
            "GET",
            content_path,
            headers={**self.auth, "Range": "bytes=0-0"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(range_headers["Content-Range"], "bytes */0")
        self.assertEqual(self.json_body(error)["error"]["code"], "range_not_satisfiable")

        events_before = self.request(
            server,
            "GET",
            "/v1/runs/manual-artifact/events?after_seq=0",
            headers=self.auth,
        )
        list_before = self.request(
            server,
            "GET",
            "/v1/runs/manual-artifact/artifacts",
            headers=self.auth,
        )
        self.stop(server)

        restarted = self.start(publish_final_artifact=False)
        events_after = self.request(
            restarted,
            "GET",
            "/v1/runs/manual-artifact/events?after_seq=0",
            headers=self.auth,
        )
        list_after = self.request(
            restarted,
            "GET",
            "/v1/runs/manual-artifact/artifacts",
            headers=self.auth,
        )
        self.assertEqual(events_after[1], events_before[1])
        self.assertEqual(list_after[1], list_before[1])

        nonempty = restarted.owner.call(
            restarted.owner.publish_artifact(
                "manual-artifact", b"integrity", filename="integrity.txt"
            ),
            5,
        )
        digest = nonempty["content_sha256"]
        blob = self.artifact_root / "blobs" / "sha256" / digest[:2] / digest
        blob.chmod(0o666)
        blob.write_bytes(b"xntegrity")
        tampered_path = (
            f"/v1/runs/manual-artifact/artifacts/{nonempty['artifact_id']}/content"
        )
        status, error, _ = self.request(
            restarted, "GET", tampered_path, headers=self.auth
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            self.json_body(error)["error"]["code"], "artifact_integrity_failed"
        )


if __name__ == "__main__":
    unittest.main()
