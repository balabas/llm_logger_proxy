from __future__ import annotations

import json
from pathlib import Path

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from insequent_logger.config import load_config
from insequent_logger.notebook import NotebookRecorder
from insequent_logger.protocol import extract_model_output, extract_model_response
from insequent_logger.server import RERANK_PATHS, TraceServer
from insequent_logger.store import TraceStore


class LlamaStub(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        raw = json.dumps(
            {"prompt": "|system|@@@@@|user|@@@@@|assistant|@@@@@", "received": body}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class RerankStub(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        raw = json.dumps({
            "model": body.get("model", "reranker"),
            "results": [{"index": 1, "relevance_score": 0.97}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class SlowStreamingStub(BaseHTTPRequestHandler):
    release = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        first = (
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            + b":" + (b"x" * 4096) + b"\n\n"
        )
        self.wfile.write(first)
        self.wfile.flush()
        self.release.wait(timeout=5)
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class SlowResponseHeadersStub(BaseHTTPRequestHandler):
    entered = threading.Event()
    release = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.entered.set()
        self.release.wait(timeout=5)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def test_config_and_llama_specific_proxy(tmp_path):
    config_path = tmp_path / "trace.toml"
    config_path.write_text(
        """
[server]
port = 9911
[storage]
path = "custom.llmtrace"
[defaults]
session_id = "fallback"

[reranker]
enabled = true

[reranker.llama_cpp]
host = "127.0.0.1"
port = 8012

[reranker.listen]
host = "127.0.0.1"
port = 9912
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config["server"]["port"] == 9911
    assert config["server"]["host"] == "127.0.0.1"
    assert config["storage"]["path"] == "custom.llmtrace"
    assert config["defaults"]["branch_id"] == "main"
    assert config["reranker"]["enabled"] is True
    assert config["reranker"]["llama_cpp"] == {
        "host": "127.0.0.1",
        "port": 8012,
    }
    assert config["reranker"]["listen"] == {
        "host": "127.0.0.1",
        "port": 9912,
    }

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), LlamaStub)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    store = TraceStore(tmp_path / "proxy.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0),
        store,
        f"http://127.0.0.1:{upstream.server_port}",
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        response = requests.post(
            f"http://127.0.0.1:{proxy.server_port}/apply-template",
            json={"messages": [{"role": "user", "content": "@@@@@"}]},
            timeout=10,
        )
        response.raise_for_status()
        assert response.json()["prompt"].startswith("|system|")
    finally:
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()


def test_reranker_listener_merges_calls_into_main_timeline(tmp_path):
    llm_upstream = ThreadingHTTPServer(("127.0.0.1", 0), LlamaStub)
    rerank_upstream = ThreadingHTTPServer(("127.0.0.1", 0), RerankStub)
    threading.Thread(target=llm_upstream.serve_forever, daemon=True).start()
    threading.Thread(target=rerank_upstream.serve_forever, daemon=True).start()

    store = TraceStore(tmp_path / "merged.llmtrace")
    main_proxy = TraceServer(
        ("127.0.0.1", 0), store, f"http://127.0.0.1:{llm_upstream.server_port}"
    )
    rerank_proxy = TraceServer(
        ("127.0.0.1", 0),
        store,
        f"http://127.0.0.1:{rerank_upstream.server_port}",
        traced_paths=RERANK_PATHS,
        default_purpose="rerank",
        default_debug_label="rerank",
    )
    threading.Thread(target=main_proxy.serve_forever, daemon=True).start()
    threading.Thread(target=rerank_proxy.serve_forever, daemon=True).start()
    headers = {"X-LLMTrace-Session": "merged-session"}
    try:
        llm = requests.post(
            f"http://127.0.0.1:{main_proxy.server_port}/v1/completions",
            headers=headers,
            json={"model": "llm", "prompt": "answer this"},
            timeout=5,
        )
        llm.raise_for_status()
        reranked = requests.post(
            f"http://127.0.0.1:{rerank_proxy.server_port}/v1/rerank",
            headers=headers,
            json={
                "model": "reranker",
                "query": "relevant building",
                "documents": ["noise", "building Alpha"],
                "top_n": 1,
            },
            timeout=5,
        )
        reranked.raise_for_status()
        assert reranked.json()["results"][0]["index"] == 1

        rows = store.timeline(session_id="merged-session")
        assert [row["id"] for row in rows] == [
            int(llm.headers["X-LLMTrace-Call"]),
            int(reranked.headers["X-LLMTrace-Call"]),
        ]
        assert [row["label"] for row in rows] == ["chat", "rerank"]
        rerank_detail = store.get_call(rows[-1]["id"])
        assert rerank_detail["request"]["query"] == "relevant building"
        assert rerank_detail["request"]["documents"] == ["noise", "building Alpha"]
        assert '"relevance_score": 0.97' in rerank_detail["response"]
        assert rerank_detail["metadata"]["endpoint"] == "/v1/rerank"
        assert rerank_detail["metadata"]["debug_label"] == "rerank"
    finally:
        main_proxy.shutdown()
        rerank_proxy.shutdown()
        main_proxy.server_close()
        rerank_proxy.server_close()
        store.close()
        llm_upstream.shutdown()
        rerank_upstream.shutdown()
        llm_upstream.server_close()
        rerank_upstream.server_close()


def test_aborted_stream_cancels_upstream_without_waiting_for_next_chunk(tmp_path):
    SlowStreamingStub.release = threading.Event()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowStreamingStub)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = TraceStore(tmp_path / "aborted.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0),
        store,
        f"http://127.0.0.1:{upstream.server_port}",
        default_session="abort-test",
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    client = socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5)
    try:
        body = json.dumps({
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "abort me"}],
        }).encode()
        client.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        received = b""
        while b'"first"' not in received:
            received += client.recv(8192)
        client.close()

        deadline = time.monotonic() + 2
        status = "running"
        while time.monotonic() < deadline:
            rows = store.timeline(session_id="abort-test")
            status = rows[0]["status"] if rows else "missing"
            if status == "cancelled":
                break
            time.sleep(0.05)
        assert status == "cancelled"
    finally:
        client.close()
        SlowStreamingStub.release.set()
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()


def test_abort_before_upstream_headers_does_not_leave_call_running(tmp_path):
    SlowResponseHeadersStub.entered = threading.Event()
    SlowResponseHeadersStub.release = threading.Event()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowResponseHeadersStub)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = TraceStore(tmp_path / "abort-before-headers.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0),
        store,
        f"http://127.0.0.1:{upstream.server_port}",
        default_session="abort-before-headers",
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    client = socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5)
    try:
        body = json.dumps({
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "abort before headers"}],
        }).encode()
        client.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        assert SlowResponseHeadersStub.entered.wait(timeout=2)
        client.close()

        deadline = time.monotonic() + 2
        detail = None
        while time.monotonic() < deadline:
            rows = store.timeline(session_id="abort-before-headers")
            if rows and rows[0]["status"] == "cancelled":
                detail = store.get_call(rows[0]["id"])
                break
            time.sleep(0.05)
        assert detail is not None
        assert detail["status"] == "cancelled"
        assert detail["metadata"]["client_disconnected"] is True
    finally:
        client.close()
        SlowResponseHeadersStub.release.set()
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()


def test_notebook_has_stable_session_and_separate_run():
    recorder = NotebookRecorder(
        "http://127.0.0.1:8081",
        run_id="run-002",
        session_id="guided-doc-v18",
        branch_id="document-rewrite",
    )
    assert recorder.openai_headers("rewrite") == {
        "X-LLMTrace-Session": "guided-doc-v18",
        "X-LLMTrace-Branch": "document-rewrite",
        "X-LLMTrace-Run": "run-002",
        "X-LLMTrace-Purpose": "rewrite",
    }
    assert recorder.openai_headers(
        "rewrite", debug_label="select next line | W1 | START"
    )["X-LLMTrace-Debug-Label"] == "1 select next line | W1 | START"
    assert recorder.openai_headers(
        "table-discard-vote", debug_label="parent vote L12 #1"
    )["X-LLMTrace-Debug-Label"] == "2 parent vote L12 #1"
    with pytest.raises(ValueError, match="ASCII HTTP header"):
        recorder.openai_headers("rewrite", debug_label="select · next")


def test_provider_envelopes_are_normalized_to_model_text():
    raw_sse = "\n\n".join(
        [
            'data: {"choices":[{"text":"T|Dec","finish_reason":null}]}',
            'data: {"choices":[{"text":"ide\\nB|line","finish_reason":null}]}',
            "data: [DONE]",
        ]
    )
    assert extract_model_output(raw_sse, streaming=True) == "T|Decide\nB|line"

    raw_json = json.dumps(
        {"choices": [{"message": {"content": "actual answer"}, "finish_reason": "stop"}]}
    )
    assert extract_model_output(raw_json, streaming=False) == "actual answer"

    raw_reasoning = json.dumps(
        {
            "choices": [{
                "message": {
                    "reasoning_content": "private reasoning",
                    "content": "final answer",
                },
                "finish_reason": "stop",
            }]
        }
    )
    separated = extract_model_response(raw_reasoning, streaming=False)
    assert separated.thoughts == "private reasoning"
    assert separated.content == "final answer"
    assert extract_model_output(raw_reasoning, streaming=False) == "final answer"


def test_streamed_tool_call_deltas_are_assembled_as_readable_json():
    deltas = [
        [
            {
                "index": 0,
                "id": "call-a",
                "type": "function",
                "function": {"name": "get_toc_headings", "arguments": "{"},
            },
            {
                "index": 1,
                "id": "call-b",
                "type": "function",
                "function": {"name": "get_toc_headings", "arguments": "{"},
            },
        ],
        [
            {"index": 0, "function": {"arguments": '"doc_id":"СМЛ*Раздел ПД №3'}},
            {"index": 1, "function": {"arguments": '"doc_id":"СМЛ*Раздел ПД №4'}},
        ],
        [
            {"index": 0, "function": {"arguments": '*V0"}'}},
            {"index": 1, "function": {"arguments": '*V0"}'}},
        ],
    ]
    raw_sse = "\n\n".join(
        [
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": tool_calls}}]},
                ensure_ascii=False,
            )
            for tool_calls in deltas
        ]
        + ["data: [DONE]"]
    )

    output = extract_model_output(raw_sse, streaming=True)
    assert "][{" not in output
    assert json.loads(output) == [
        {
            "index": 0,
            "type": "function",
            "function": {
                "name": "get_toc_headings",
                "arguments": {"doc_id": "СМЛ*Раздел ПД №3*V0"},
            },
            "id": "call-a",
        },
        {
            "index": 1,
            "type": "function",
            "function": {
                "name": "get_toc_headings",
                "arguments": {"doc_id": "СМЛ*Раздел ПД №4*V0"},
            },
            "id": "call-b",
        },
    ]


def test_provider_token_usage_is_normalized_when_available():
    streamed = "\n\n".join([
        'data: {"choices":[{"delta":{"content":"done"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":1200,'
        '"completion_tokens":34,"total_tokens":1234}}',
        "data: [DONE]",
    ])
    assert extract_model_response(streamed, streaming=True).usage == {
        "input_tokens": 1200,
        "output_tokens": 34,
        "total_tokens": 1234,
    }

    native = json.dumps({
        "content": "done",
        "timings": {"prompt_n": 90, "predicted_n": 10},
    })
    assert extract_model_response(native, streaming=False).usage == {
        "input_tokens": 90,
        "output_tokens": 10,
        "total_tokens": 100,
    }


def test_generation_speed_is_captured_from_timings():
    # The server's timings carry the real decode rate; capture it so the stored
    # call records the generation speed, not just token counts.
    streamed = "\n\n".join([
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":7906,"completion_tokens":70,'
        '"total_tokens":7976},"timings":{"predicted_n":70,"predicted_ms":2179.39,'
        '"predicted_per_second":31.66,"prompt_per_second":760.77}}',
        "data: [DONE]",
    ])
    assert extract_model_response(streamed, streaming=True).usage == {
        "input_tokens": 7906,
        "output_tokens": 70,
        "total_tokens": 7976,
        "output_per_second": 31.66,
        "input_per_second": 760.77,
    }

    # Falls back to predicted_n / predicted_ms when the rate is not given.
    computed = json.dumps({
        "content": "hi",
        "timings": {"prompt_n": 90, "predicted_n": 100, "predicted_ms": 2000},
    })
    usage = extract_model_response(computed, streaming=False).usage
    assert usage["output_per_second"] == 50.0  # 100 tokens / 2.0 s


def test_copied_notebook_uses_session_headers_without_remote_event_logging():
    notebook_path = (
        Path(__file__).parents[1]
        / "guided_doc_indexing_thinking_stages_v18_insequent.ipynb"
    )
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    source = "\n".join(
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    )
    assert "X-LLMTrace-Session" in source
    assert "MODEL_CALL_PURPOSES" in source
    assert "TRACE_ITEM_NUMBERS = itertools.count(1)" in source
    assert 'debug_label = f"{next(TRACE_ITEM_NUMBERS)} {step}"' in source
    assert "requires a real step name" in source
    assert "trace step names must be ASCII HTTP header values" in source
    assert 'step = step or "select next source line"' in source
    assert 'f"select next line | W{_rw_window} | {cursor_label}"' in source
    assert 'step=f"w{_rw_window} · {cursor_label}"' not in source
    assert "NotebookRecorder" not in source
    assert "trace.log_event" not in source
    assert "/api/events" not in source


class ChatStub(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        raw = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 21,
                    "completion_tokens": 3,
                    "total_tokens": 24,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_debug_label_header_is_stored_and_surfaced(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChatStub)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = TraceStore(tmp_path / "debug.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0), store, f"http://127.0.0.1:{upstream.server_port}"
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        response = requests.post(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            headers={
                "X-LLMTrace-Session": "notebook",
                "X-LLMTrace-Debug-Label": "07-rewrite-attempt-2",
            },
            json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
        assert response.status_code == 200
        call_id = int(response.headers["X-LLMTrace-Call"])

        # The label rides in metadata and is surfaced on the timeline row so the
        # UI can show it without loading the full call.
        assert store.get_call(call_id)["metadata"]["debug_label"] == "07-rewrite-attempt-2"
        assert store.get_call(call_id)["metadata"]["usage"] == {
            "input_tokens": 21,
            "output_tokens": 3,
            "total_tokens": 24,
        }
        row = next(item for item in store.timeline() if item.get("id") == call_id)
        assert row["debug_label"] == "07-rewrite-attempt-2"
        assert row["usage"]["total_tokens"] == 24

        # A request without the header carries no label.
        plain = requests.post(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            headers={"X-LLMTrace-Session": "notebook"},
            json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
        plain_id = int(plain.headers["X-LLMTrace-Call"])
        assert "debug_label" not in store.get_call(plain_id)["metadata"]
        plain_row = next(item for item in store.timeline() if item.get("id") == plain_id)
        assert "debug_label" not in plain_row

        dynamic = requests.post(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            headers={
                "X-LLMTrace-Session": "notebook",
                "X-LLMTrace-Req-Id": "dynamic-title-1",
                "X-LLMTrace-Debug-Label": "EXECUTE/%D0%BF%D0%BE%D0%B8%D1%81%D0%BA******",
                "X-LLMTrace-Debug-Label-Encoding": "percent",
                "X-LLMTrace-Title": "EXECUTE/find_documents",
            },
            json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
        dynamic_id = int(dynamic.headers["X-LLMTrace-Call"])
        assert store.get_call(dynamic_id)["metadata"]["debug_label"] == "EXECUTE/поиск******"
        assert store.get_call(dynamic_id)["metadata"]["title"] == "EXECUTE/find_documents"

        updated = requests.post(
            f"http://127.0.0.1:{proxy.server_port}/_llmtrace/update-title",
            json={
                "req_id": "dynamic-title-1",
                "title": "EXECUTE/поиск:TOOL_CALLS/get_toc_headings",
            },
            timeout=10,
        )
        assert updated.status_code == 204
        assert (
            store.get_call(dynamic_id)["metadata"]["title"]
            == "EXECUTE/поиск:TOOL_CALLS/get_toc_headings"
        )
        dynamic_row = next(
            item for item in store.timeline() if item.get("id") == dynamic_id
        )
        assert dynamic_row["title"] == "EXECUTE/поиск:TOOL_CALLS/get_toc_headings"
        assert dynamic_row["debug_label"] == "EXECUTE/поиск******"
    finally:
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()


class NativeCompletionStub(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        raw = json.dumps({"content": "native answer", "stop": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_headerless_and_native_completion_requests_are_traced(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), NativeCompletionStub)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = TraceStore(tmp_path / "catch.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0), store, f"http://127.0.0.1:{upstream.server_port}"
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{proxy.server_port}"
    try:
        # No X-LLMTrace headers at all, across every completion endpoint a client
        # might use — OpenAI-compatible and llama.cpp native, with/without /v1.
        for path in (
            "/v1/completions",
            "/v1/chat/completions",
            "/completions",
            "/chat/completions",
            "/completion",
        ):
            response = requests.post(
                f"{base}{path}", json={"prompt": "hi"}, timeout=10
            )
            assert response.status_code == 200, path
            assert response.headers.get("X-LLMTrace-Call"), f"{path} not traced"

        # A non-completion route is still forwarded without being recorded.
        template = requests.post(
            f"{base}/apply-template", json={"messages": []}, timeout=10
        )
        assert "X-LLMTrace-Call" not in template.headers

        assert store.stats()["calls"] == 5
        # The native /completion response ({"content": ...}, no choices) is parsed
        # to its text, not stored as the raw envelope.
        native = requests.post(f"{base}/completion", json={"prompt": "x"}, timeout=10)
        native_id = int(native.headers["X-LLMTrace-Call"])
        assert store.get_call(native_id)["response"] == "native answer"
    finally:
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()
