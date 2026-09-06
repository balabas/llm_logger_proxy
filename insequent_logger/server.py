from __future__ import annotations

import itertools
import json
import logging
import mimetypes
import queue
import select
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .protocol import extract_model_response
from .store import TraceStore


STATIC_DIR = Path(__file__).with_name("static")

# Every completion endpoint a client might use is traced, headers or not — the
# OpenAI-compatible chat/text routes and llama.cpp's native `/completion`, with
# or without a `/v1` prefix. Non-completion routes (templates, tokenize,
# embeddings, health) pass through untraced.
COMPLETION_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/completions",
    "/chat/completions",
    "/completions",
    "/completion",
})
RERANK_PATHS = frozenset({"/v1/rerank", "/rerank"})


class LiveHub:
    """In-memory pub/sub for in-flight streaming output.

    Lets the viewer watch a call's raw output as it streams — a live side-channel
    that is entirely separate from durable storage. The relay thread pushes each
    chunk here as it forwards it to the client; the background log worker still
    writes the persistent record after the stream ends. Nothing here touches the
    store, so watching a stream never couples logging to the response path.

    A stream is identified by a transient ``live_id`` (not the durable call id,
    which does not exist until the log worker runs). Slow subscribers are dropped
    silently rather than allowed to stall the relay.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._active: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def next_id(self) -> int:
        return next(self._ids)

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            # Catch a new subscriber up on streams already in flight, so a viewer
            # that connects mid-stream still sees the output so far.
            backlog = [("snapshot", dict(record)) for record in self._active.values()]
            self._subscribers.add(subscriber)
        for event in backlog:
            subscriber.put_nowait(event)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def _broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait((kind, payload))
            except queue.Full:
                # A stalled subscriber must never back up the relay; it will fall
                # behind and can reconnect for a fresh snapshot.
                pass

    def open(self, live_id: int, meta: dict[str, Any]) -> None:
        record = {"live_id": live_id, "text": "", "status": "streaming", **meta}
        with self._lock:
            self._active[live_id] = record
        self._broadcast("start", dict(record))

    def update(self, live_id: int, **changes: Any) -> None:
        """Update an already-announced request without changing its identity."""
        with self._lock:
            record = self._active.get(live_id)
            if record is None:
                return
            record.update(changes)
            payload = dict(record)
        self._broadcast("update", payload)

    def push(self, live_id: int, text: str) -> None:
        with self._lock:
            record = self._active.get(live_id)
            if record is None:
                return
            record["text"] += text
            session = record.get("session")
        self._broadcast("delta", {"live_id": live_id, "session": session, "text": text})

    def close(self, live_id: int, status: str) -> None:
        with self._lock:
            record = self._active.get(live_id)
            if record is not None:
                record["status"] = status
                record["ended_at_ms"] = round(time.time() * 1000)
            session = record.get("session") if record else None
        self._broadcast(
            "end", {"live_id": live_id, "session": session, "status": status}
        )

    def stored(self, live_id: int, call_id: int | None) -> None:
        """Retire a live item only after its durable replacement is queryable."""
        with self._lock:
            record = self._active.pop(live_id, None)
            session = record.get("session") if record else None
        self._broadcast(
            "stored",
            {"live_id": live_id, "call_id": call_id, "session": session},
        )

    def update_title(self, update: dict[str, Any]) -> None:
        """Publish an explicit caller-provided title update."""
        with self._lock:
            record = next(
                (
                    item for item in self._active.values()
                    if item.get("req_id") == update.get("req_id")
                ),
                None,
            )
            if record is not None:
                record["title"] = update["title"]
                payload = dict(record)
            else:
                payload = None
        if payload is not None:
            self._broadcast("update", payload)
        else:
            self._broadcast("title", update)

    def shutdown(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(("__stop__", {}))
            except queue.Full:
                pass


class TraceServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: TraceStore,
        upstream: str,
        *,
        default_session: str = "unassigned",
        default_branch: str = "main",
        traced_paths: frozenset[str] = COMPLETION_PATHS,
        default_purpose: str = "chat",
        default_debug_label: str | None = None,
    ):
        super().__init__(address, TraceHandler)
        self.store = store
        self.upstream = upstream.rstrip("/")
        self.default_session = default_session
        self.default_branch = default_branch
        self.traced_paths = traced_paths
        self.default_purpose = default_purpose
        self.default_debug_label = default_debug_label
        # Logging/diffing runs off the request path so it never delays the model
        # response. A single worker preserves call order, keeping delta chains and
        # diffs identical to the old synchronous behaviour.
        self.log_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llmtrace-log"
        )
        # Live streaming side-channel for the viewer; see LiveHub.
        self.live = LiveHub()

    def is_traced_path(self, path: str) -> bool:
        return path.rstrip("/") in self.traced_paths

    def server_close(self) -> None:  # noqa: D401 - flush pending logs on shutdown
        # Release any open SSE subscribers before tearing the socket down so their
        # handler threads unblock and exit instead of parking on an empty queue.
        self.live.shutdown()
        super().server_close()
        self.log_executor.shutdown(wait=True)


_log = logging.getLogger("insequent_logger")


def _connection_closed(connection: socket.socket) -> bool:
    """Return true when the downstream HTTP client has closed its socket."""
    try:
        readable, _, exceptional = select.select(
            [connection], [], [connection], 0,
        )
        if exceptional:
            return True
        if not readable:
            return False
        return connection.recv(
            1, socket.MSG_PEEK | socket.MSG_DONTWAIT,
        ) == b""
    except BlockingIOError:
        return False
    except (OSError, ValueError):
        return True


def _abort_upstream_response(response: requests.Response) -> None:
    """Close the upstream TCP stream, including a currently blocking read."""
    raw_response = getattr(response.raw, "_fp", None)
    buffered_reader = getattr(raw_response, "fp", None)
    socket_io = getattr(buffered_reader, "raw", None)
    upstream_socket = getattr(socket_io, "_sock", None)
    if upstream_socket is not None:
        try:
            upstream_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    response.close()


def _store_completion(
    store: TraceStore,
    *,
    call_id: int,
    events: list[tuple[int, float, str, str]],
    response_text: str,
    thoughts: str,
    status: str,
    finish_metadata: dict[str, Any],
    raw_response: str | None = None,
) -> int | None:
    """Persist the response for an existing call.

    Runs in the background log executor; all expensive diffing happens here,
    entirely off the client's response path.
    """
    try:
        for sequence, relative_ms, event_type, data in events:
            store.add_stream_event(call_id, sequence, relative_ms, event_type, data)
        store.finish_call(
            call_id,
            response_text,
            thoughts=thoughts,
            # Keep the genuine upstream bytes: on a context-exceeded/cancelled call
            # the parsed content is empty but the raw body (error envelope or bare
            # finish_reason frames) is the only record of what came back. Fall back
            # to response_text only when no raw body was captured (e.g. the upstream
            # never responded and we stored the exception text).
            raw_response=raw_response if raw_response is not None else (response_text or None),
            status=status,
            metadata=finish_metadata,
        )
        return call_id
    except Exception:  # never let logging crash the proxy
        _log.exception("background trace logging failed")
        return None


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

    def _live_stream(self, session: str | None) -> None:
        """Server-Sent-Events feed of in-flight streaming output.

        Long-lived: holds the connection open and forwards live events from the
        hub until the client disconnects or the server shuts down. Runs on its own
        handler thread (ThreadingHTTPServer), so it never blocks other requests.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(kind: str, payload: dict[str, Any] | None = None) -> bool:
            if payload is None:
                frame = f": {kind}\n\n"  # comment/heartbeat keeps the socket alive
            else:
                data = json.dumps(payload, ensure_ascii=False)
                frame = f"event: {kind}\ndata: {data}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ValueError):
                return False

        subscriber = self.server.live.subscribe()
        try:
            if not emit("ping"):
                return
            while True:
                try:
                    kind, payload = subscriber.get(timeout=15)
                except queue.Empty:
                    if not emit("ping"):  # detect a dead client between events
                        return
                    continue
                if kind == "__stop__":
                    return
                # Scope to the requested session when one is given. Events without
                # a session (older snapshots) always pass through.
                if session and payload.get("session") not in (None, session):
                    continue
                if not emit(kind, payload):
                    return
        finally:
            self.server.live.unsubscribe(subscriber)

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
        if parsed.path == "/api/live":
            params = parse_qs(parsed.query)
            self._live_stream(params.get("session", [None])[0])
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
            allowed_fields = {"input", "thoughts", "output"}
            raw_fields = params.get("fields", [""])[0]
            fields = {
                field.strip() for field in raw_fields.split(",") if field.strip()
            }
            if fields and not fields <= allowed_fields:
                self._error(400, "fields must contain only input, thoughts, output")
                return
            if not query:
                self._json([])
                return
            try:
                self._json(
                    self.server.store.search(
                        query,
                        session_id=session,
                        fields=fields or None,
                    )
                )
            except Exception as exc:
                self._error(400, f"invalid search: {exc}")
            return
        if parsed.path == "/api/stats":
            self._json(self.server.store.stats())
            return
        if parsed.path == "/api/history/reset":
            params = parse_qs(parsed.query)
            try:
                call_id = int(params.get("call_id", [""])[0])
                self._json(self.server.store.history_reset_preview(call_id))
            except (ValueError, KeyError) as exc:
                self._error(404, str(exc))
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
        if parsed.path == "/_llmtrace/update-title":
            self._update_title()
            return
        if parsed.path == "/api/events":
            self._record_event()
            return
        if parsed.path == "/api/history/reset":
            self._reset_history()
            return
        if self.server.is_traced_path(parsed.path):
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

    def _update_title(self) -> None:
        try:
            body = self._read_json_body()
            req_id = str(body.get("req_id", "")).strip()
            title = str(body.get("title", "")).strip()
            if not req_id:
                raise ValueError("req_id is required")
            if not title:
                raise ValueError("title is required")
            update = self.server.store.update_call_title(req_id, title)
            self.server.live.update_title(update)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except KeyError as exc:
            self._error(404, f"unknown req_id: {exc.args[0]}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc))

    def _reset_history(self) -> None:
        try:
            body = self._read_json_body()
            call_id = int(body["call_id"])
            self._json(self.server.store.reset_history_before_call(call_id))
        except KeyError as exc:
            self._error(404, str(exc))
        except (TypeError, ValueError) as exc:
            self._error(409, str(exc))

    def _post_upstream_while_client_connected(
        self,
        target: str,
        *,
        request_body: dict[str, Any],
        headers: dict[str, str],
        stream: bool,
        call_id: int,
    ) -> requests.Response | None:
        """Wait for upstream headers only while the downstream client exists.

        ``requests.post`` can block for minutes before an inference server sends
        response headers. Run that wait in a daemon thread so an already-aborted
        client does not leave this handler and its durable call marked running.
        The worker closes a late response instead of handing it back after the
        client has gone away.
        """
        outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        abandoned = threading.Event()
        response_lock = threading.Lock()
        response_holder: dict[str, requests.Response] = {}

        def request_upstream() -> None:
            try:
                response = requests.post(
                    target,
                    json=request_body,
                    headers=headers,
                    timeout=(30, 600),
                    stream=stream,
                )
            except requests.RequestException as exc:
                if not abandoned.is_set(): outcome.put(("error", exc))
                return
            with response_lock:
                if abandoned.is_set():
                    _abort_upstream_response(response)
                    return
                response_holder["response"] = response
                outcome.put(("response", response))

        worker = threading.Thread(
            target=request_upstream,
            name=f"llmtrace-upstream-headers-{call_id}",
            daemon=True,
        )
        worker.start()
        while True:
            try:
                kind, value = outcome.get(timeout=0.05)
            except queue.Empty:
                if not _connection_closed(self.connection):
                    continue
                with response_lock:
                    abandoned.set()
                    late_response = response_holder.get("response")
                    if late_response is not None:
                        _abort_upstream_response(late_response)
                self.close_connection = True
                return None
            if kind == "error":
                raise value
            return value

    def _proxy_completion(self, path: str) -> None:
        try:
            request_body = self._read_json_body()
        except Exception as exc:
            self._error(400, str(exc))
            return

        session = self.headers.get("X-LLMTrace-Session", self.server.default_session)
        branch = self.headers.get("X-LLMTrace-Branch", self.server.default_branch)
        purpose = self.headers.get("X-LLMTrace-Purpose", self.server.default_purpose)
        run_id = self.headers.get("X-LLMTrace-Run")
        debug_label = (
            self.headers.get("X-LLMTrace-Debug-Label")
            or self.server.default_debug_label
        )
        if (
            debug_label
            and self.headers.get("X-LLMTrace-Debug-Label-Encoding") == "percent"
        ):
            debug_label = unquote(debug_label)
        title = self.headers.get("X-LLMTrace-Title")
        group = self.headers.get("X-LLMTrace-Group")
        req_id = self.headers.get("X-LLMTrace-Req-Id")
        client_request_id = (
            self.headers.get("X-Request-ID")
            or self.headers.get("Request-ID")
        )
        prev_req_id = self.headers.get("X-LLMTrace-Prev-Req-Id")
        raw_parent = self.headers.get("X-LLMTrace-Base-State")
        try:
            explicit_parent = int(raw_parent) if raw_parent else None
        except ValueError:
            self._error(400, "X-LLMTrace-Base-State must be an integer")
            return

        start_kwargs = {
            "session_id": session,
            "branch_id": branch,
            "purpose": purpose,
            "explicit_parent_state": explicit_parent,
            "req_id": req_id,
            "prev_req_id": prev_req_id,
            "metadata": {
                "endpoint": path,
                **({"run_id": run_id} if run_id else {}),
                **({"debug_label": debug_label} if debug_label else {}),
                **({"title": title} if title else {}),
                **({"group": group} if group else {}),
            },
        }

        # Create the durable call at request start. Its numeric id is the request
        # number shown by the timeline and returned to the client; live streaming
        # uses a separate internal transport id only for pub/sub routing.
        try:
            call_id = self.server.store.start_call(request_body, **start_kwargs)
        except (KeyError, ValueError) as exc:
            self._error(400, str(exc))
            return
        except Exception as exc:
            self._error(500, str(exc))
            return

        target = f"{self.server.upstream}{path}"
        forward_headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "*/*"),
        }
        if self.headers.get("Authorization"):
            forward_headers["Authorization"] = self.headers["Authorization"]

        started = time.monotonic()
        live_id: int | None = None
        if request_body.get("stream"):
            # The caller's request identity and full input are known before the
            # upstream responds. Announce that request now; response headers will
            # update this same item instead of creating a second timeline item.
            live_id = self.server.live.next_id()
            self.server.live.open(
                live_id,
                {
                    "session": session,
                    "label": debug_label,
                    "title": title,
                    "endpoint": path,
                    "started_ms": round(time.time() * 1000),
                    "request": request_body,
                    "call_id": call_id,
                    "req_id": req_id,
                    "request_id": call_id,
                    "status": "running",
                },
            )
        try:
            response = self._post_upstream_while_client_connected(
                target,
                request_body=request_body,
                headers=forward_headers,
                stream=bool(request_body.get("stream")),
                call_id=call_id,
            )
            if response is None:
                if live_id is not None:
                    self.server.live.close(live_id, "cancelled")
                future = self.server.log_executor.submit(
                    _store_completion,
                    self.server.store,
                    call_id=call_id,
                    events=[],
                    response_text="",
                    thoughts="",
                    status="cancelled",
                    finish_metadata={
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        "client_disconnected": True,
                    },
                )
                if live_id is not None:
                    future.add_done_callback(
                        lambda completed, current_live_id=live_id: self.server.live.stored(
                            current_live_id, completed.result()
                        )
                    )
                return
            upstream_request_id = (
                response.headers.get("X-Request-ID")
                or response.headers.get("Request-ID")
            )
            provider_request_id = client_request_id or upstream_request_id
            if request_body.get("stream"):
                assert live_id is not None
                self.server.live.update(
                    live_id,
                    status="streaming",
                    request_id=call_id,
                )
                events, raw_text, status = self._relay_stream(
                    response, started, live_id, call_id
                )
                self.server.live.close(live_id, status)
                parsed = extract_model_response(raw_text, streaming=True)
                future = self.server.log_executor.submit(
                    _store_completion,
                    self.server.store,
                    call_id=call_id,
                    events=events,
                    response_text=parsed.content,
                    thoughts=parsed.thoughts,
                    raw_response=raw_text,
                    status=status,
                    finish_metadata={
                        "http_status": response.status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        "stream_chunks": len(events),
                        **({"usage": parsed.usage} if parsed.usage else {}),
                        **(
                            {"provider_request_id": provider_request_id}
                            if provider_request_id else {}
                        ),
                    },
                )
                future.add_done_callback(
                    lambda completed, current_live_id=live_id: self.server.live.stored(
                        current_live_id, completed.result()
                    )
                )
            else:
                raw = response.content
                raw_text = raw.decode("utf-8", errors="replace")
                parsed = extract_model_response(raw_text, streaming=False)
                _store_completion(
                    self.server.store,
                    call_id=call_id,
                    events=[],
                    response_text=parsed.content,
                    thoughts=parsed.thoughts,
                    raw_response=raw_text,
                    status="ok" if response.ok else "error",
                    finish_metadata={
                        "http_status": response.status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        **({"usage": parsed.usage} if parsed.usage else {}),
                        **(
                            {"provider_request_id": provider_request_id}
                            if provider_request_id else {}
                        ),
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
            if live_id is not None:
                self.server.live.close(live_id, "error")
            future = self.server.log_executor.submit(
                _store_completion,
                self.server.store,
                call_id=call_id,
                events=[],
                response_text=str(exc),
                thoughts="",
                status="error",
                finish_metadata={
                    "duration_ms": round((time.monotonic() - started) * 1000, 3)
                },
            )
            if live_id is not None:
                future.add_done_callback(
                    lambda completed, current_live_id=live_id: self.server.live.stored(
                        current_live_id, completed.result()
                    )
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
        self,
        response: requests.Response,
        started: float,
        live_id: int | None = None,
        call_id: int | None = None,
    ) -> tuple[list[tuple[int, float, str, str]], str, str]:
        """Relay the upstream stream to the client, buffering events for later.

        Does NO store work — chunks go to the client immediately and the buffered
        events are persisted afterwards by the background log worker. When a
        ``live_id`` is given, each chunk is also published to the live side-channel
        so the viewer can watch the output in flight. Returns (events, raw_text,
        status).
        """
        self.send_response(response.status_code)
        self.send_header(
            "Content-Type", response.headers.get("Content-Type", "text/event-stream")
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if call_id is not None:
            self.send_header("X-LLMTrace-Call", str(call_id))
        self.end_headers()
        captured: list[bytes] = []
        events: list[tuple[int, float, str, str]] = []
        sequence = 0
        status = "ok" if response.ok else "error"
        downstream_closed = threading.Event()
        stop_watching = threading.Event()

        def watch_downstream() -> None:
            while not stop_watching.wait(0.05):
                if not _connection_closed(self.connection):
                    continue
                downstream_closed.set()
                # iter_content may be blocked waiting for the model's next
                # token. Closing only the browser socket would leave this
                # handler (and the model request) alive until the 600s read
                # timeout, so interrupt the upstream read from this watcher.
                _abort_upstream_response(response)
                return

        watcher = threading.Thread(
            target=watch_downstream,
            name=f"llmtrace-client-watch-{call_id or 'stream'}",
            daemon=True,
        )
        watcher.start()
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                captured.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                text = chunk.decode("utf-8", errors="replace")
                events.append((
                    sequence,
                    round((time.monotonic() - started) * 1000, 3),
                    "chunk",
                    text,
                ))
                if live_id is not None:
                    self.server.live.push(live_id, text)
                sequence += 1
        except (BrokenPipeError, ConnectionResetError):
            downstream_closed.set()
            status = "cancelled"
            _abort_upstream_response(response)
        except Exception:
            if downstream_closed.is_set():
                status = "cancelled"
            else:
                raise
        finally:
            stop_watching.set()
            response.close()
            watcher.join(timeout=0.2)
            if downstream_closed.is_set():
                status = "cancelled"
            self.close_connection = True
        raw_text = b"".join(captured).decode("utf-8", errors="replace")
        return events, raw_text, status


def serve(
    db_path: str | Path = "trace.llmtrace",
    *,
    host: str = "127.0.0.1",
    port: int = 8081,
    upstream: str = "http://127.0.0.1:8080",
    default_session: str = "unassigned",
    default_branch: str = "main",
    max_file_bytes: int | None = None,
    reranker_listen_host: str | None = None,
    reranker_listen_port: int | None = None,
    reranker_llama_cpp_host: str | None = None,
    reranker_llama_cpp_port: int | None = None,
) -> None:
    store = TraceStore(db_path, max_file_bytes=max_file_bytes)
    server: TraceServer | None = None
    reranker_listener: TraceServer | None = None
    reranker_listener_thread: threading.Thread | None = None
    try:
        server = TraceServer(
            (host, port),
            store,
            upstream,
            default_session=default_session,
            default_branch=default_branch,
        )
        reranker_configured = any(value is not None for value in (
            reranker_listen_host,
            reranker_listen_port,
            reranker_llama_cpp_host,
            reranker_llama_cpp_port,
        ))
        if reranker_configured:
            if any(value is None for value in (
                reranker_listen_host,
                reranker_listen_port,
                reranker_llama_cpp_host,
                reranker_llama_cpp_port,
            )):
                raise ValueError(
                    "reranker requires llama_cpp host/port and listen host/port"
                )
            if reranker_listen_host == host and reranker_listen_port == port:
                raise ValueError("reranker listener must use a different host/port")
            reranker_llama_cpp_url = (
                f"http://{reranker_llama_cpp_host}:{reranker_llama_cpp_port}"
            )
            reranker_listener = TraceServer(
                (reranker_listen_host, reranker_listen_port),
                store,
                reranker_llama_cpp_url,
                default_session=default_session,
                default_branch=default_branch,
                traced_paths=RERANK_PATHS,
                default_purpose="rerank",
                default_debug_label="rerank",
            )
            reranker_listener_thread = threading.Thread(
                target=reranker_listener.serve_forever,
                name="insequent-reranker-listener",
                daemon=True,
            )
            reranker_listener_thread.start()
    except Exception:
        if reranker_listener is not None:
            reranker_listener.server_close()
        if server is not None:
            server.server_close()
        store.close()
        raise
    print(f"Logger viewer: http://{host}:{port}/")
    print(f"OpenAI-compatible proxy: http://{host}:{port}/v1 -> {upstream}/v1")
    if reranker_listener is not None:
        print(
            f"Reranker proxy: http://{reranker_listen_host}:"
            f"{reranker_listen_port}/v1/rerank "
            f"-> {reranker_llama_cpp_url.rstrip('/')}/v1/rerank"
        )
    print(f"Trace file: {Path(db_path).resolve()}")
    try:
        assert server is not None
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if reranker_listener is not None:
            reranker_listener.shutdown()
            reranker_listener.server_close()
        if reranker_listener_thread is not None:
            reranker_listener_thread.join(timeout=2)
        assert server is not None
        server.server_close()
        store.close()
