from __future__ import annotations

import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .protocol import extract_model_response
from .store import TraceStore


STATIC_DIR = Path(__file__).with_name("static")


class TraceServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: TraceStore,
        upstream: str,
        *,
        default_session: str = "unassigned",
        default_branch: str = "main",
    ):
        super().__init__(address, TraceHandler)
        self.store = store
        self.upstream = upstream.rstrip("/")
        self.default_session = default_session
        self.default_branch = default_branch


class TraceHandler(BaseHTTPRequestHandler):
    server: TraceServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[insequent] {self.address_string()} {fmt % args}")

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/timeline":
            params = parse_qs(parsed.query)
            limit = min(int(params.get("limit", ["500"])[0]), 5000)
            session = params.get("session", [None])[0]
            self._json(self.server.store.timeline(limit=limit, session_id=session))
            return
        if parsed.path == "/api/sessions":
            self._json(self.server.store.sessions())
            return
        if parsed.path.startswith("/api/calls/"):
            self._detail("call", parsed.path.rsplit("/", 1)[-1])
            return
        if parsed.path.startswith("/api/events/"):
            self._detail("event", parsed.path.rsplit("/", 1)[-1])
            return
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0].strip()
            session = params.get("session", [None])[0]
            if not query:
                self._json([])
                return
            try:
                self._json(self.server.store.search(query, session_id=session))
            except Exception as exc:
                self._error(400, f"invalid search: {exc}")
            return
        if parsed.path == "/api/stats":
            self._json(self.server.store.stats())
            return
        if parsed.path.startswith("/v1/") or parsed.path in (
            "/models",
            "/health",
            "/props",
            "/slots",
            "/metrics",
        ):
            self._proxy_get(parsed.path, parsed.query)
            return
        if parsed.path in ("/", "/index.html", "/styles.css", "/app.js"):
            self._static(parsed.path)
            return
        self._proxy_get(parsed.path, parsed.query)

    def _detail(self, item_type: str, raw_id: str) -> None:
        try:
            item_id = int(raw_id)
            value = (
                self.server.store.get_call(item_id)
                if item_type == "call"
                else self.server.store.get_event(item_id)
            )
            self._json(value)
        except (ValueError, KeyError) as exc:
            self._error(404, str(exc))

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._error(403, "forbidden")
            return
        if not target.is_file():
            self._error(404, "not found")
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_get(self, path: str, query: str) -> None:
        target = f"{self.server.upstream}{path}"
        if query:
            target += f"?{query}"
        try:
            response = requests.get(target, timeout=30)
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        except requests.RequestException as exc:
            self._error(502, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            self._record_event()
            return
        if parsed.path in ("/v1/chat/completions", "/v1/completions"):
            self._proxy_completion(parsed.path)
            return
        self._proxy_generic_post(parsed.path, parsed.query)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _record_event(self) -> None:
        try:
            body = self._read_json_body()
            event_id = self.server.store.record_event(
                str(body.pop("event")),
                body.pop("payload", body),
                session_id=self.headers.get(
                    "X-LLMTrace-Session", self.server.default_session
                ),
                branch_id=self.headers.get(
                    "X-LLMTrace-Branch", self.server.default_branch
                ),
            )
            self._json({"id": event_id}, 201)
        except Exception as exc:
            self._error(400, str(exc))

    def _proxy_completion(self, path: str) -> None:
        try:
            request_body = self._read_json_body()
        except Exception as exc:
            self._error(400, str(exc))
            return

        session = self.headers.get("X-LLMTrace-Session", self.server.default_session)
        branch = self.headers.get("X-LLMTrace-Branch", self.server.default_branch)
        purpose = self.headers.get("X-LLMTrace-Purpose", "chat")
        run_id = self.headers.get("X-LLMTrace-Run")
        debug_label = self.headers.get("X-LLMTrace-Debug-Label")
        group = self.headers.get("X-LLMTrace-Group")
        req_id = self.headers.get("X-LLMTrace-Req-Id")
        prev_req_id = self.headers.get("X-LLMTrace-Prev-Req-Id")
        raw_parent = self.headers.get("X-LLMTrace-Base-State")
        try:
            explicit_parent = int(raw_parent) if raw_parent else None
        except ValueError:
            self._error(400, "X-LLMTrace-Base-State must be an integer")
            return

        try:
            call_id = self.server.store.start_call(
                request_body,
                session_id=session,
                branch_id=branch,
                purpose=purpose,
                explicit_parent_state=explicit_parent,
                req_id=req_id,
                prev_req_id=prev_req_id,
                metadata={
                    "endpoint": path,
                    **({"run_id": run_id} if run_id else {}),
                    **({"debug_label": debug_label} if debug_label else {}),
                    **({"group": group} if group else {}),
                },
            )
        except ValueError as exc:
            self._error(400, str(exc))
            return
        target = f"{self.server.upstream}{path}"
        forward_headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "*/*"),
        }
        if self.headers.get("Authorization"):
            forward_headers["Authorization"] = self.headers["Authorization"]

        started = time.monotonic()
        try:
            response = requests.post(
                target,
                json=request_body,
                headers=forward_headers,
                timeout=(30, 600),
                stream=bool(request_body.get("stream")),
            )
            if request_body.get("stream"):
                self._relay_stream(call_id, response, started)
            else:
                raw = response.content
                raw_text = raw.decode("utf-8", errors="replace")
                model_response = extract_model_response(raw_text, streaming=False)
                self.server.store.finish_call(
                    call_id,
                    model_response.content,
                    thoughts=model_response.thoughts,
                    raw_response=raw_text,
                    status="ok" if response.ok else "error",
                    metadata={
                        "http_status": response.status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
                self.send_response(response.status_code)
                self.send_header(
                    "Content-Type", response.headers.get("Content-Type", "application/json")
                )
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-LLMTrace-Call", str(call_id))
                self.end_headers()
                self.wfile.write(raw)
        except requests.RequestException as exc:
            self.server.store.finish_call(
                call_id,
                str(exc),
                status="error",
                metadata={"duration_ms": round((time.monotonic() - started) * 1000, 3)},
            )
            self._error(502, str(exc))

    def _proxy_generic_post(self, path: str, query: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        target = f"{self.server.upstream}{path}"
        if query:
            target += f"?{query}"
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {
                "host",
                "content-length",
                "connection",
                "x-llmtrace-session",
                "x-llmtrace-branch",
                "x-llmtrace-purpose",
                "x-llmtrace-base-state",
                "x-llmtrace-run",
            }
        }
        try:
            response = requests.post(
                target,
                data=body,
                headers=headers,
                timeout=(30, 600),
                stream=True,
            )
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/octet-stream"),
            )
            self.send_header("Cache-Control", response.headers.get("Cache-Control", "no-cache"))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in response.iter_content(chunk_size=4096):
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                response.close()
                self.close_connection = True
        except requests.RequestException as exc:
            self._error(502, str(exc))

    def _relay_stream(
        self, call_id: int, response: requests.Response, started: float
    ) -> None:
        self.send_response(response.status_code)
        self.send_header(
            "Content-Type", response.headers.get("Content-Type", "text/event-stream")
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-LLMTrace-Call", str(call_id))
        self.end_headers()
        captured: list[bytes] = []
        sequence = 0
        status = "ok" if response.ok else "error"
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                captured.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                self.server.store.add_stream_event(
                    call_id,
                    sequence,
                    round((time.monotonic() - started) * 1000, 3),
                    "chunk",
                    chunk.decode("utf-8", errors="replace"),
                )
                sequence += 1
        except (BrokenPipeError, ConnectionResetError):
            status = "cancelled"
        finally:
            self.close_connection = True
            raw_text = b"".join(captured).decode("utf-8", errors="replace")
            model_response = extract_model_response(raw_text, streaming=True)
            self.server.store.finish_call(
                call_id,
                model_response.content,
                thoughts=model_response.thoughts,
                raw_response=raw_text,
                status=status,
                metadata={
                    "http_status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "stream_chunks": sequence,
                },
            )


def serve(
    db_path: str | Path = "trace.llmtrace",
    *,
    host: str = "127.0.0.1",
    port: int = 8081,
    upstream: str = "http://127.0.0.1:8080",
    default_session: str = "unassigned",
    default_branch: str = "main",
    max_file_bytes: int | None = None,
) -> None:
    store = TraceStore(db_path, max_file_bytes=max_file_bytes)
    server = TraceServer(
        (host, port),
        store,
        upstream,
        default_session=default_session,
        default_branch=default_branch,
    )
    print(f"Insequent viewer: http://{host}:{port}/")
    print(f"OpenAI-compatible proxy: http://{host}:{port}/v1 -> {upstream}/v1")
    print(f"Trace file: {Path(db_path).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
