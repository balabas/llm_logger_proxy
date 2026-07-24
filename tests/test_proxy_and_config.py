from __future__ import annotations

import json
from pathlib import Path

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from insequent_logger.config import load_config
from insequent_logger.notebook import NotebookRecorder
from insequent_logger.protocol import extract_model_output, extract_model_response
from insequent_logger.server import TraceServer
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
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config["server"]["port"] == 9911
    assert config["server"]["host"] == "127.0.0.1"
    assert config["storage"]["path"] == "custom.llmtrace"
    assert config["defaults"]["branch_id"] == "main"

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
    assert "NotebookRecorder" not in source
    assert "trace.log_event" not in source
    assert "/api/events" not in source
