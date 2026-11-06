"""In-process fake of the Cyango external publish API and of S3 presigned PUTs."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "cyp_test-token"
PREFIX = "/external-publish/v1"


class FakeCyango:
    def __init__(self, *, single_max: int = 200, part_size: int = 256) -> None:
        self.single_max = single_max
        self.part_size = part_size
        self.lock = threading.Lock()
        self.uploads: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.put_count: dict[str, int] = {}
        # Failure injection: path suffix -> list of statuses to return before succeeding.
        self.fail: dict[str, list[int]] = {}
        self.completed: list[dict[str, Any]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{PREFIX}"

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> FakeCyango:
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _take_failure(self, key: str) -> int | None:
        with self.lock:
            queue = self.fail.get(key)
            return queue.pop(0) if queue else None

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _json(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._json(
                        401,
                        {"error": "unauthorized", "message": "The external publish token is not valid or was revoked."},
                    )
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                fake.calls.append(("GET", self.path))
                if not self._authorized():
                    return
                if self.path == f"{PREFIX}/me":
                    self._json(
                        200,
                        {
                            "user": {"id": "u1", "email": "maker@example.com"},
                            "workspace": {"id": "w1", "name": "Studio"},
                        },
                    )
                elif self.path == f"{PREFIX}/folders":
                    self._json(200, {"folders": [{"id": "f1", "name": "Scans"}]})
                else:
                    self._json(404, {"error": "not_found", "message": "No route."})

            def do_POST(self) -> None:  # noqa: N802
                fake.calls.append(("POST", self.path))
                if not self._authorized():
                    return
                body = self._body()
                route = self.path[len(PREFIX) :]
                failure = fake._take_failure(route)
                if failure:
                    self._json(failure, {"error": "injected", "message": f"Injected {failure} on {route}."})
                    return
                if route == "/uploads":
                    self._create(body)
                elif route == "/uploads/parts":
                    self._parts(body)
                elif route == "/uploads/complete":
                    self._complete(body)
                elif route == "/uploads/abort":
                    with fake.lock:
                        fake.uploads.pop(body.get("upload_id"), None)
                    self._json(200, {"status": "aborted"})
                else:
                    self._json(404, {"error": "not_found", "message": "No route."})

            def _create(self, body: dict[str, Any]) -> None:
                size = body["file_size"]
                upload_id = f"up-{uuid.uuid4().hex}"
                record = {"size": size, "title": body.get("title"), "folder_id": body.get("folder_id"), "body": body}
                if size <= fake.single_max:
                    record["mode"] = "single"
                    with fake.lock:
                        fake.uploads[upload_id] = record
                    self._json(
                        200,
                        {
                            "upload_id": upload_id,
                            "mode": "single",
                            "size": size,
                            "url": f"{fake.origin}/s3/{upload_id}/single",
                            "headers": {"Content-Type": "application/octet-stream"},
                        },
                    )
                    return
                record.update(mode="multipart", part_count=math.ceil(size / fake.part_size))
                with fake.lock:
                    fake.uploads[upload_id] = record
                self._json(
                    200,
                    {
                        "upload_id": upload_id,
                        "mode": "multipart",
                        "size": size,
                        "part_size": fake.part_size,
                        "part_count": record["part_count"],
                    },
                )

            def _parts(self, body: dict[str, Any]) -> None:
                upload_id = body.get("upload_id")
                if upload_id not in fake.uploads:
                    self._json(404, {"error": "not_found", "message": "Unknown or expired upload_id."})
                    return
                parts = [
                    {"part_number": n, "url": f"{fake.origin}/s3/{upload_id}/part/{n}", "size": 0}
                    for n in body["part_numbers"]
                ]
                self._json(200, {"parts": parts})

            def _complete(self, body: dict[str, Any]) -> None:
                upload_id = body.get("upload_id")
                record = fake.uploads.get(upload_id)
                if record is None:
                    self._json(404, {"error": "not_found", "message": "Unknown or expired upload_id."})
                    return
                if record["mode"] == "single":
                    data = fake.objects.get(f"{upload_id}/single")
                    if data is None or len(data) != record["size"]:
                        self._json(409, {"error": "upload_missing", "message": "Not in storage."})
                        return
                else:
                    parts = body.get("parts") or []
                    if [p["part_number"] for p in parts] != list(range(1, record["part_count"] + 1)):
                        self._json(400, {"error": "invalid_request", "message": "Every part is needed."})
                        return
                    chunks = []
                    for part in parts:
                        data = fake.objects.get(f"{upload_id}/part/{part['part_number']}")
                        if data is None or f'"{hashlib.md5(data).hexdigest()}"' != part["etag"]:
                            self._json(400, {"error": "invalid_request", "message": "Bad part."})
                            return
                        chunks.append(data)
                    data = b"".join(chunks)
                fake.completed.append({"upload_id": upload_id, "data": data, **record})
                self._json(
                    200,
                    {
                        "status": "completed",
                        "asset_id": "a1",
                        "story_id": "s1",
                        "story_url": "https://studio.example/editor/?storyId=s1",
                    },
                )

            def do_PUT(self) -> None:  # noqa: N802
                key = self.path[len("/s3/") :]
                fake.calls.append(("PUT", key))
                with fake.lock:
                    fake.put_count[key] = fake.put_count.get(key, 0) + 1
                length = int(self.headers.get("Content-Length") or -1)
                data = self.rfile.read(length) if length >= 0 else b""
                failure = fake._take_failure(key)
                if failure:
                    self.send_response(failure)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with fake.lock:
                    fake.objects[key] = data
                self.send_response(200)
                self.send_header("ETag", f'"{hashlib.md5(data).hexdigest()}"')
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler
