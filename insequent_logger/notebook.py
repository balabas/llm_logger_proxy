from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import requests


class NotebookRecorder:
    """Small adapter for sending notebook domain events to an Insequent server."""

    def __init__(
        self,
        base_url: str,
        *,
        run_id: str,
        session_id: str | None = None,
        branch_id: str = "main",
        timeout: float = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.session_id = session_id or run_id
        self.branch_id = branch_id
        self.timeout = timeout
        self._sequence = 0
        self._item_number = 0
        self._lock = threading.Lock()

    def openai_headers(
        self,
        purpose: str | None = None,
        debug_label: str | None = None,
        req_id: str | None = None,
        prev_req_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-LLMTrace-Session": self.session_id,
            "X-LLMTrace-Branch": self.branch_id,
            "X-LLMTrace-Run": self.run_id,
        }
        if purpose:
            headers["X-LLMTrace-Purpose"] = purpose
        # A numbered per-step label the viewer shows on the call's timeline and
        # update rows. Set it per request — an OpenAI client can carry it via
        # client._custom_headers["X-LLMTrace-Debug-Label"].
        if debug_label:
            try:
                debug_label.encode("ascii")
            except UnicodeEncodeError as error:
                raise ValueError(
                    "debug_label must be an ASCII HTTP header value"
                ) from error
            with self._lock:
                self._item_number += 1
                item_number = self._item_number
            headers["X-LLMTrace-Debug-Label"] = f"{item_number} {debug_label}"
        # Caller-declared request identity. Give each request a req_id and point
        # it at its predecessor with prev_req_id to declare the branch tree
        # explicitly, so lineage follows the notebook's own structure instead of
        # similarity inference.
        if req_id:
            headers["X-LLMTrace-Req-Id"] = req_id
        if prev_req_id:
            headers["X-LLMTrace-Prev-Req-Id"] = prev_req_id
        return headers

    def log_event(self, event_type: str, **payload: Any) -> int:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        record = {
            "run_id": self.run_id,
            "seq": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        response = requests.post(
            f"{self.base_url}/api/events",
            headers=self.openai_headers(),
            json={"event": event_type, "payload": record},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return int(response.json()["id"])
