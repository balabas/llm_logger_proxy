from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import zlib
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .diffing import compact_text_diff, compact_token_diff, diff_manifests, diff_values
from .protocol import extract_model_response


log = logging.getLogger(__name__)

# Largest *differing region*, in characters, that may be delta-encoded.
#
# SequenceMatcher is quadratic, and the tracer sits in the request path: every
# second spent diffing is a second the caller waits for its model response.
# Measured on Cyrillic text (chars -> seconds for one get_opcodes):
#     5k -> 0.26   10k -> 1.1   20k -> 5.0   40k -> 18   80k -> 70
# The limit is applied to the region left after the shared prefix and suffix
# are trimmed, not to the whole blob, because the two are wildly different in
# practice: consecutive requests in a conversation share nearly everything and
# diverge in a few hundred characters, while an unrelated pair of 200k-char
# tool results shares nothing and costs minutes. Sizing by total length would
# refuse the first case to protect against the second.
_MAX_DELTA_CHARS = int(os.environ.get("INSEQUENT_MAX_DELTA_CHARS", "40000"))

# A delta slower than this means the size cap is set wrong for this content;
# say so rather than silently paying it on every call.
_SLOW_DELTA_SECONDS = float(os.environ.get("INSEQUENT_SLOW_DELTA_SECONDS", "1.0"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class TraceStore:
    """SQLite-backed trace storage with content-addressed, optionally delta-encoded blobs."""

    def __init__(self, path: str | Path, *, max_file_bytes: int | None = None):
        self.path = Path(path)
        self.max_file_bytes = max_file_bytes
        self._last_pruned_sessions: list[str] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()
        self._recover_interrupted_calls()
        # Reclaim any full-text index bloat left by earlier pruning before the
        # size check, so the file reflects its real content size.
        self._reclaim_search_index_if_bloated()
        if self.max_file_bytes:
            self.enforce_size_limit()

    def _init_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS blobs (
                    hash TEXT PRIMARY KEY,
                    storage TEXT NOT NULL,
                    base_hash TEXT REFERENCES blobs(hash),
                    chain_depth INTEGER NOT NULL DEFAULT 0,
                    codec TEXT NOT NULL,
                    data BLOB NOT NULL,
                    raw_size INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_hash TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    branch_root_id TEXT NOT NULL,
                    parent_state_id INTEGER REFERENCES states(id),
                    parent_source TEXT,
                    similarity REAL,
                    request_blob_hash TEXT NOT NULL REFERENCES blobs(hash),
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    branch_root_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    chronological_parent_id INTEGER REFERENCES calls(id),
                    request_state_id INTEGER NOT NULL REFERENCES states(id),
                    response_blob_hash TEXT REFERENCES blobs(hash),
                    thoughts_blob_hash TEXT REFERENCES blobs(hash),
                    raw_response_blob_hash TEXT REFERENCES blobs(hash),
                    status TEXT NOT NULL,
                    req_id TEXT,
                    prev_req_id TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_blob_hash TEXT NOT NULL REFERENCES blobs(hash)
                );

                CREATE TABLE IF NOT EXISTS event_field_heads (
                    session_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    field TEXT NOT NULL,
                    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
                    PRIMARY KEY(session_id, branch_id, kind, field)
                );

                CREATE TABLE IF NOT EXISTS stream_events (
                    call_id INTEGER NOT NULL REFERENCES calls(id),
                    sequence INTEGER NOT NULL,
                    relative_ms REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    data_blob_hash TEXT REFERENCES blobs(hash),
                    PRIMARY KEY(call_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS timeline (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS search_documents USING fts5(
                    owner_type UNINDEXED,
                    owner_id UNINDEXED,
                    field UNINDEXED,
                    text
                );

                CREATE INDEX IF NOT EXISTS idx_states_session
                    ON states(session_id, branch_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_calls_session
                    ON calls(session_id, branch_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_events_kind
                    ON events(session_id, branch_id, kind, id DESC);
                """
            )
            call_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(calls)")
            }
            state_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(states)")
            }
            if "branch_root_id" not in state_columns:
                self._db.execute("ALTER TABLE states ADD COLUMN branch_root_id TEXT")
                self._db.execute(
                    "UPDATE states SET branch_root_id=branch_id WHERE branch_root_id IS NULL"
                )
            if "branch_root_id" not in call_columns:
                self._db.execute("ALTER TABLE calls ADD COLUMN branch_root_id TEXT")
                self._db.execute(
                    "UPDATE calls SET branch_root_id=branch_id WHERE branch_root_id IS NULL"
                )
            if "raw_response_blob_hash" not in call_columns:
                self._db.execute(
                    "ALTER TABLE calls ADD COLUMN raw_response_blob_hash TEXT REFERENCES blobs(hash)"
                )
            if "thoughts_blob_hash" not in call_columns:
                self._db.execute(
                    "ALTER TABLE calls ADD COLUMN thoughts_blob_hash TEXT REFERENCES blobs(hash)"
                )
            # Caller-assigned request identity, so the notebook can declare its
            # own branch tree by pointing each request at its predecessor.
            if "req_id" not in call_columns:
                self._db.execute("ALTER TABLE calls ADD COLUMN req_id TEXT")
            if "prev_req_id" not in call_columns:
                self._db.execute("ALTER TABLE calls ADD COLUMN prev_req_id TEXT")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_calls_req_id "
                "ON calls(session_id, req_id)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_states_branch_root "
                "ON states(session_id, branch_root_id, id DESC)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_calls_branch_root "
                "ON calls(session_id, branch_root_id, id DESC)"
            )
        self._migrate_legacy_responses()
        self._migrate_response_parts()

    def _recover_interrupted_calls(self) -> None:
        """A new store process cannot own calls left running by an old one."""
        with self._db:
            self._db.execute(
                "UPDATE calls SET status='interrupted' WHERE status='running'"
            )

    def _migrate_legacy_responses(self) -> None:
        rows = self._db.execute(
            """
            SELECT id, response_blob_hash FROM calls
            WHERE response_blob_hash IS NOT NULL AND raw_response_blob_hash IS NULL
            """
        ).fetchall()
        for row in rows:
            raw = self.get_text(row["response_blob_hash"])
            streaming = any(
                line.lstrip().startswith("data:") for line in raw.splitlines()[:5]
            )
            response = extract_model_response(raw, streaming=streaming)
            normalized_hash = self.put_text(response.content)
            thoughts_hash = self.put_text(response.thoughts)
            with self._db:
                self._db.execute(
                    """
                    UPDATE calls
                    SET response_blob_hash=?, thoughts_blob_hash=?, raw_response_blob_hash=?
                    WHERE id=?
                    """,
                    (
                        normalized_hash,
                        thoughts_hash,
                        row["response_blob_hash"],
                        row["id"],
                    ),
                )
                self._db.execute(
                    "DELETE FROM search_documents "
                    "WHERE owner_type='call' AND owner_id=? AND field='output'",
                    (row["id"],),
                )
                self._db.execute(
                    "INSERT INTO search_documents VALUES ('call', ?, 'output', ?)",
                    (row["id"], response.content),
                )

    def _migrate_response_parts(self) -> None:
        rows = self._db.execute(
            """
            SELECT id, response_blob_hash, raw_response_blob_hash
            FROM calls
            WHERE response_blob_hash IS NOT NULL
              AND raw_response_blob_hash IS NOT NULL
              AND thoughts_blob_hash IS NULL
            """
        ).fetchall()
        for row in rows:
            raw = self.get_text(row["raw_response_blob_hash"])
            streaming = any(
                line.lstrip().startswith("data:") for line in raw.splitlines()[:5]
            )
            parsed = extract_model_response(raw, streaming=streaming)
            current = self.get_text(row["response_blob_hash"])
            provider_envelope = parsed.content != raw or bool(parsed.thoughts)
            content = parsed.content if provider_envelope else current
            content_hash = self.put_text(content)
            thoughts_hash = self.put_text(parsed.thoughts if provider_envelope else "")
            with self._db:
                self._db.execute(
                    """
                    UPDATE calls
                    SET response_blob_hash=?, thoughts_blob_hash=?
                    WHERE id=?
                    """,
                    (content_hash, thoughts_hash, row["id"]),
                )
                self._db.execute(
                    "DELETE FROM search_documents "
                    "WHERE owner_type='call' AND owner_id=? AND field IN ('output', 'thoughts')",
                    (row["id"],),
                )
                self._db.execute(
                    "INSERT INTO search_documents VALUES ('call', ?, 'output', ?)",
                    (row["id"], content),
                )
                if parsed.thoughts:
                    self._db.execute(
                        "INSERT INTO search_documents VALUES ('call', ?, 'thoughts', ?)",
                        (row["id"], parsed.thoughts),
                    )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------- blobs ----------

    def put_text(
        self,
        text: str,
        *,
        base_hash: str | None = None,
        delta_ratio: float = 0.60,
        max_chain: int = 20,
    ) -> str:
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            if self._db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone():
                return digest

            full = zlib.compress(raw, level=6)
            storage = "full"
            stored = full
            depth = 0
            chosen_base: str | None = None

            if base_hash:
                row = self._db.execute(
                    "SELECT chain_depth, raw_size FROM blobs WHERE hash=?", (base_hash,)
                ).fetchone()
                if row and row["chain_depth"] < max_chain:
                    base = self.get_text(base_hash)
                    started = time.monotonic()
                    ops = self._text_delta(base, text)
                    elapsed = time.monotonic() - started
                    if elapsed > _SLOW_DELTA_SECONDS:
                        log.warning(
                            "delta of %d chars against %d took %.1fs; lower "
                            "INSEQUENT_MAX_DELTA_CHARS (currently %d)",
                            len(text), len(base), elapsed, _MAX_DELTA_CHARS,
                        )
                    if ops is not None:
                        packed_delta = zlib.compress(_json(ops).encode("utf-8"), level=6)
                        if len(packed_delta) < len(full) * delta_ratio:
                            storage = "delta"
                            stored = packed_delta
                            depth = row["chain_depth"] + 1
                            chosen_base = base_hash

            with self._db:
                self._db.execute(
                    """
                    INSERT INTO blobs(hash, storage, base_hash, chain_depth, codec, data, raw_size)
                    VALUES (?, ?, ?, ?, 'zlib', ?, ?)
                    """,
                    (digest, storage, chosen_base, depth, stored, len(raw)),
                )
            return digest

    @staticmethod
    def _text_delta(base: str, current: str) -> list[list[Any]] | None:
        """Edits turning `base` into `current`, or None when diffing is too costly.

        Opcode positions are indices into `base`, which is what get_text()
        replays, so the shared head and tail are trimmed before matching and
        the offset added back afterwards. That keeps the quadratic work
        proportional to what actually changed rather than to the blob size.
        """
        head = 0
        limit = min(len(base), len(current))
        while head < limit and base[head] == current[head]:
            head += 1
        tail = 0
        limit -= head
        while tail < limit and base[-1 - tail] == current[-1 - tail]:
            tail += 1

        base_mid = base[head:len(base) - tail]
        current_mid = current[head:len(current) - tail]
        if len(base_mid) > _MAX_DELTA_CHARS or len(current_mid) > _MAX_DELTA_CHARS:
            return None

        matcher = SequenceMatcher(None, base_mid, current_mid, autojunk=False)
        return [
            [i1 + head, i2 + head, current_mid[j1:j2]]
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]

    def get_text(self, digest: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT * FROM blobs WHERE hash=?", (digest,)).fetchone()
            if not row:
                raise KeyError(f"unknown blob {digest}")
            payload = zlib.decompress(row["data"])
            if row["storage"] == "full":
                text = payload.decode("utf-8")
            else:
                base = self.get_text(row["base_hash"])
                ops = json.loads(payload)
                pieces: list[str] = []
                cursor = 0
                for i1, i2, replacement in ops:
                    pieces.append(base[cursor:i1])
                    pieces.append(replacement)
                    cursor = i2
                pieces.append(base[cursor:])
                text = "".join(pieces)
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                raise ValueError(f"blob reconstruction hash mismatch: {digest}")
            return text

    # ---------- request states and calls ----------

    def _externalize_message(
        self, message: dict[str, Any], base_message: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        role = str(message.get("role", ""))
        content = message.get("content", "")
        base_content_hash = None
        if base_message and isinstance(base_message.get("content"), dict):
            base_content_hash = base_message["content"].get("$blob")

        if isinstance(content, str):
            content_text = content
            content_format = "text"
        else:
            content_text = _json(content)
            content_format = "json"
        content_hash = self.put_text(content_text, base_hash=base_content_hash)

        extra = {key: value for key, value in message.items() if key not in ("role", "content")}
        result: dict[str, Any] = {
            "role": role,
            "content": {"$blob": content_hash, "format": content_format},
        }
        if extra:
            extra_text = _json(extra)
            result["extra"] = {"$blob": self.put_text(extra_text), "format": "json"}
        return result

    def _externalize_parameter(self, value: Any) -> Any:
        text = value if isinstance(value, str) else _json(value)
        if len(text) >= 256 or isinstance(value, (dict, list)):
            return {
                "$blob": self.put_text(text),
                "format": "text" if isinstance(value, str) else "json",
            }
        return value

    def _build_manifest(self, request: dict[str, Any]) -> dict[str, Any]:
        if isinstance(request.get("messages"), list):
            parameters = {
                key: self._externalize_parameter(value)
                for key, value in request.items()
                if key != "messages"
            }
            return {
                "kind": "chat",
                "messages": [self._externalize_message(item) for item in request["messages"]],
                "parameters": parameters,
            }
        prompt = request.get("prompt", "")
        prompt_text = prompt if isinstance(prompt, str) else _json(prompt)
        parameters = {
            key: self._externalize_parameter(value)
            for key, value in request.items()
            if key != "prompt"
        }
        return {
            "kind": "completion",
            "prompt": {"$blob": self.put_text(prompt_text), "format": "text"},
            "parameters": parameters,
        }

    @staticmethod
    def _manifest_signature(manifest: dict[str, Any]) -> list[str]:
        if manifest.get("kind") == "chat":
            return [
                f"{message.get('role')}:{message.get('content', {}).get('$blob', '')}"
                for message in manifest.get("messages", [])
            ]
        prompt = manifest.get("prompt", {})
        return [f"prompt:{prompt.get('$blob', '')}"]

    def _manifest_similarity(
        self, left: dict[str, Any], right: dict[str, Any]
    ) -> float | None:
        if left.get("kind") != right.get("kind"):
            return None
        if left.get("kind") == "completion":
            left_prompt = self.get_text(left["prompt"]["$blob"])
            right_prompt = self.get_text(right["prompt"]["$blob"])
            return SequenceMatcher(
                None,
                left_prompt.splitlines(),
                right_prompt.splitlines(),
                autojunk=False,
            ).ratio()
        return SequenceMatcher(
            None,
            self._manifest_signature(left),
            self._manifest_signature(right),
            autojunk=False,
        ).ratio()

    def _choose_parent(
        self,
        session_id: str,
        branch_root_id: str,
        manifest: dict[str, Any],
        explicit_parent: int | None,
    ) -> tuple[
        int | None,
        str | None,
        float | None,
        str | None,
        str | None,
        str | None,
        int | None,
    ]:
        if explicit_parent is not None:
            row = self._db.execute(
                """
                SELECT id, manifest_json, request_blob_hash, branch_id,
                       COALESCE(branch_root_id, branch_id) AS branch_root_id,
                       parent_state_id
                FROM states WHERE id=?
                """,
                (explicit_parent,),
            ).fetchone()
            if not row:
                raise ValueError(f"explicit parent state {explicit_parent} does not exist")
            return (
                row["id"],
                "explicit",
                1.0,
                row["request_blob_hash"],
                row["branch_id"],
                row["branch_root_id"],
                row["parent_state_id"],
            )

        rows = self._db.execute(
            """
            SELECT id, manifest_json, request_blob_hash, branch_id,
                   COALESCE(branch_root_id, branch_id) AS branch_root_id,
                   parent_state_id,
                   (
                       SELECT response_blob_hash FROM calls
                       WHERE request_state_id=states.id
                         AND response_blob_hash IS NOT NULL
                       ORDER BY id DESC LIMIT 1
                   ) AS response_blob_hash
            FROM states
            WHERE session_id=? AND COALESCE(branch_root_id, branch_id)=?
            ORDER BY id DESC LIMIT 50
            """,
            (session_id, branch_root_id),
        ).fetchall()
        current_signature = self._manifest_signature(manifest)
        current_prompt = (
            self.get_text(manifest["prompt"]["$blob"])
            if manifest.get("kind") == "completion"
            else None
        )
        best: tuple[float, sqlite3.Row] | None = None
        for row in rows:
            candidate = json.loads(row["manifest_json"])
            if candidate.get("kind") != manifest.get("kind"):
                continue
            if current_prompt is not None:
                candidate_prompt = self.get_text(candidate["prompt"]["$blob"])
                score = SequenceMatcher(
                    None,
                    candidate_prompt.splitlines(),
                    current_prompt.splitlines(),
                    autojunk=False,
                ).ratio()
            else:
                candidate_signature = self._manifest_signature(candidate)
                if row["response_blob_hash"]:
                    candidate_signature = [
                        *candidate_signature,
                        f"assistant:{row['response_blob_hash']}",
                    ]
                score = SequenceMatcher(
                    None,
                    candidate_signature,
                    current_signature,
                    autojunk=False,
                ).ratio()
            if best is None or score > best[0]:
                best = (score, row)
        if not best or best[0] < 0.20:
            return None, None, None, None, None, None, None
        row = best[1]
        return (
            row["id"],
            "inferred",
            best[0],
            row["request_blob_hash"],
            row["branch_id"],
            row["branch_root_id"],
            row["parent_state_id"],
        )

    def _active_branch_states(
        self, session_id: str, branch_root_id: str
    ) -> dict[str, int]:
        rows = self._db.execute(
            """
            SELECT branch_id, request_state_id FROM calls
            WHERE session_id=? AND branch_root_id=? AND status='running'
            ORDER BY id
            """,
            (session_id, branch_root_id),
        ).fetchall()
        return {row["branch_id"]: row["request_state_id"] for row in rows}

    @staticmethod
    def _available_parallel_branch(
        branch_root_id: str, active_branches: dict[str, int]
    ) -> str:
        if branch_root_id not in active_branches:
            return branch_root_id
        index = 2
        while f"{branch_root_id}~parallel-{index}" in active_branches:
            index += 1
        return f"{branch_root_id}~parallel-{index}"

    def start_call(
        self,
        request: dict[str, Any],
        *,
        session_id: str = "default",
        branch_id: str = "main",
        purpose: str = "chat",
        explicit_parent_state: int | None = None,
        req_id: str | None = None,
        prev_req_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        created = _now()
        with self._lock:
            # A caller-declared predecessor pins the parent exactly, the same way
            # an explicit parent state does, so the notebook's own branch tree is
            # honoured instead of being re-inferred by similarity.
            if explicit_parent_state is None and prev_req_id is not None:
                prev = self._db.execute(
                    """
                    SELECT request_state_id FROM calls
                    WHERE session_id=? AND req_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (session_id, prev_req_id),
                ).fetchone()
                if prev is None:
                    raise ValueError(
                        f"prev_req_id {prev_req_id!r} has no matching call in this session"
                    )
                explicit_parent_state = prev["request_state_id"]
            manifest = self._build_manifest(request)
            (
                parent_id,
                parent_source,
                similarity,
                base_request,
                parent_branch_id,
                parent_branch_root_id,
                inferred_parent_id,
            ) = self._choose_parent(
                session_id, branch_id, manifest, explicit_parent_state
            )
            resolved_branch_id = (
                parent_branch_id
                if parent_branch_id and parent_branch_root_id == branch_id
                else branch_id
            )
            active_branches = self._active_branch_states(session_id, branch_id)
            if not active_branches:
                resolved_branch_id = branch_id
            elif resolved_branch_id in active_branches:
                running_state_id = active_branches[resolved_branch_id]
                resolved_branch_id = self._available_parallel_branch(
                    branch_id, active_branches
                )
                if (
                    explicit_parent_state is None
                    and parent_branch_id
                    and parent_id == running_state_id
                ):
                    parent_id = inferred_parent_id
                    parent_source = "parallel"
                    if parent_id is None:
                        similarity = None
                        base_request = None
                    else:
                        parent = self._db.execute(
                            "SELECT request_blob_hash FROM states WHERE id=?",
                            (parent_id,),
                        ).fetchone()
                        base_request = parent["request_blob_hash"] if parent else None
            request_text = _json(request)
            request_blob = self.put_text(request_text, base_hash=base_request)
            state_hash = hashlib.sha256(
                f"{session_id}\0{resolved_branch_id}\0{_json(manifest)}".encode("utf-8")
            ).hexdigest()
            state_row = self._db.execute(
                "SELECT id FROM states WHERE state_hash=?", (state_hash,)
            ).fetchone()
            with self._db:
                if state_row:
                    state_id = state_row["id"]
                else:
                    cursor = self._db.execute(
                        """
                        INSERT INTO states(
                            state_hash, session_id, branch_id, branch_root_id,
                            parent_state_id, parent_source, similarity,
                            request_blob_hash, manifest_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            state_hash,
                            session_id,
                            resolved_branch_id,
                            branch_id,
                            parent_id,
                            parent_source,
                            similarity,
                            request_blob,
                            _json(manifest),
                            created,
                        ),
                    )
                    state_id = cursor.lastrowid
                previous = self._db.execute(
                    "SELECT id FROM calls WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                cursor = self._db.execute(
                    """
                    INSERT INTO calls(
                        created_at, session_id, branch_id, branch_root_id, purpose,
                        chronological_parent_id, request_state_id, status,
                        req_id, prev_req_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        created,
                        session_id,
                        resolved_branch_id,
                        branch_id,
                        purpose,
                        previous["id"] if previous else None,
                        state_id,
                        req_id,
                        prev_req_id,
                        _json(metadata or {}),
                    ),
                )
                call_id = cursor.lastrowid
                self._db.execute(
                    "INSERT INTO timeline(created_at, item_type, item_id) VALUES (?, 'call', ?)",
                    (created, call_id),
                )
                self._db.execute(
                    "INSERT INTO search_documents VALUES ('call', ?, 'input', ?)",
                    (call_id, request_text),
                )
            return int(call_id)

    def finish_call(
        self,
        call_id: int,
        response: str,
        *,
        thoughts: str = "",
        raw_response: str | None = None,
        status: str = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            digest = self.put_text(response)
            thoughts_digest = self.put_text(thoughts)
            raw_digest = (
                self.put_text(raw_response)
                if raw_response is not None and raw_response != response
                else digest
            )
            row = self._db.execute(
                "SELECT metadata_json FROM calls WHERE id=?", (call_id,)
            ).fetchone()
            if not row:
                raise KeyError(call_id)
            merged = json.loads(row["metadata_json"])
            merged.update(metadata or {})
            with self._db:
                self._db.execute(
                    """
                    UPDATE calls
                    SET response_blob_hash=?, thoughts_blob_hash=?,
                        raw_response_blob_hash=?, status=?, metadata_json=?
                    WHERE id=?
                    """,
                    (
                        digest,
                        thoughts_digest,
                        raw_digest,
                        status,
                        _json(merged),
                        call_id,
                    ),
                )
                self._db.execute(
                    "INSERT INTO search_documents VALUES ('call', ?, 'output', ?)",
                    (call_id, response),
                )
                if thoughts:
                    self._db.execute(
                        "INSERT INTO search_documents VALUES ('call', ?, 'thoughts', ?)",
                        (call_id, thoughts),
                    )
            self.enforce_size_limit()

    def update_call_title(self, req_id: str, title: str) -> dict[str, Any]:
        """Replace the explicit title of the newest call with a caller ID."""
        with self._lock:
            row = self._db.execute(
                """
                SELECT id, session_id, metadata_json FROM calls
                WHERE req_id=? ORDER BY id DESC LIMIT 1
                """,
                (req_id,),
            ).fetchone()
            if row is None:
                raise KeyError(req_id)
            metadata = json.loads(row["metadata_json"])
            metadata["title"] = title
            with self._db:
                self._db.execute(
                    "UPDATE calls SET metadata_json=? WHERE id=?",
                    (_json(metadata), row["id"]),
                )
            return {
                "call_id": int(row["id"]),
                "session": row["session_id"],
                "req_id": req_id,
                "title": title,
            }

    def add_stream_event(
        self, call_id: int, sequence: int, relative_ms: float, event_type: str, data: str
    ) -> None:
        digest = self.put_text(data) if data else None
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO stream_events VALUES (?, ?, ?, ?, ?)",
                (call_id, sequence, relative_ms, event_type, digest),
            )

    # ---------- generic application events ----------

    def record_event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        session_id: str = "default",
        branch_id: str = "main",
    ) -> int:
        created = _now()
        manifest: dict[str, Any] = {}
        indexed: list[tuple[str, str]] = []
        with self._lock:
            for field, value in payload.items():
                text = value if isinstance(value, str) else _json(value)
                if len(text) >= 256 or isinstance(value, (dict, list)):
                    head = self._db.execute(
                        """
                        SELECT blob_hash FROM event_field_heads
                        WHERE session_id=? AND branch_id=? AND kind=? AND field=?
                        """,
                        (session_id, branch_id, kind, field),
                    ).fetchone()
                    digest = self.put_text(text, base_hash=head["blob_hash"] if head else None)
                    manifest[field] = {
                        "$blob": digest,
                        "format": "text" if isinstance(value, str) else "json",
                    }
                    indexed.append((field, text))
                else:
                    manifest[field] = value
                    if isinstance(value, str):
                        indexed.append((field, value))

            payload_blob = self.put_text(_json(manifest))
            with self._db:
                cursor = self._db.execute(
                    """
                    INSERT INTO events(created_at, session_id, branch_id, kind, payload_blob_hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (created, session_id, branch_id, kind, payload_blob),
                )
                event_id = cursor.lastrowid
                self._db.execute(
                    "INSERT INTO timeline(created_at, item_type, item_id) VALUES (?, 'event', ?)",
                    (created, event_id),
                )
                for field, ref in manifest.items():
                    if isinstance(ref, dict) and "$blob" in ref:
                        self._db.execute(
                            """
                            INSERT INTO event_field_heads VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(session_id, branch_id, kind, field)
                            DO UPDATE SET blob_hash=excluded.blob_hash
                            """,
                            (session_id, branch_id, kind, field, ref["$blob"]),
                        )
                for field, text in indexed:
                    self._db.execute(
                        "INSERT INTO search_documents VALUES ('event', ?, ?, ?)",
                        (event_id, field, text),
                    )
            self.enforce_size_limit()
            return int(event_id)

    # ---------- retention ----------

    def _physical_bytes(self) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )

    def enforce_size_limit(self) -> list[str]:
        """Delete complete oldest sessions and compact until the file fits."""
        if not self.max_file_bytes:
            return []
        pruned: list[str] = []
        with self._lock:
            if not self._checkpoint_wal():
                return []
            while self._physical_bytes() > self.max_file_bytes:
                oldest = self._db.execute(
                    """
                    SELECT session_id, MIN(created_at) AS first_at
                    FROM (
                        SELECT session_id, created_at FROM calls
                        UNION ALL
                        SELECT session_id, created_at FROM events
                    )
                    WHERE session_id NOT IN (
                        SELECT DISTINCT session_id FROM calls WHERE status='running'
                    )
                    AND session_id != (
                        SELECT session_id
                        FROM (
                            SELECT session_id, created_at FROM calls
                            UNION ALL
                            SELECT session_id, created_at FROM events
                        )
                        GROUP BY session_id
                        ORDER BY MAX(created_at) DESC
                        LIMIT 1
                    )
                    GROUP BY session_id
                    ORDER BY first_at ASC
                    LIMIT 1
                    """
                ).fetchone()
                if not oldest:
                    break
                session_id = oldest["session_id"]
                self._delete_session(session_id)
                pruned.append(session_id)
                self._garbage_collect_blobs()
                # Merge the deleted session's full-text segments before VACUUM;
                # otherwise the dead index data keeps the file above the limit and
                # the loop deletes every remaining session for nothing.
                self._optimize_search_index()
                self._db.commit()  # close the optimize transaction before VACUUM
                self._db.execute("VACUUM")
                if not self._checkpoint_wal():
                    break
            self._last_pruned_sessions = pruned
        return pruned

    def _optimize_search_index(self) -> None:
        """Merge FTS5 segments so deleted documents' index data is reclaimed.

        Deleting rows only tombstones them in the full-text index; the segment
        data lingers until merged. After mass deletion (session pruning, cleaning
        history) an unmerged index can dwarf the actual content — tens of MB of
        dead segments for a handful of live documents — which VACUUM alone cannot
        reclaim because those are live pages, not free ones."""
        try:
            self._db.execute(
                "INSERT INTO search_documents(search_documents) VALUES('optimize')"
            )
        except sqlite3.OperationalError:
            pass

    def _search_index_is_bloated(self) -> bool:
        """A healthy FTS index has a few segments per document. Hundreds of
        segments for a few live documents means deleted content never merged."""
        try:
            segments = self._db.execute(
                "SELECT COUNT(*) FROM search_documents_data"
            ).fetchone()[0]
            documents = self._db.execute(
                "SELECT COUNT(*) FROM search_documents_docsize"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return False
        return segments > 256 and segments > max(documents, 1) * 32

    def _reclaim_search_index_if_bloated(self) -> bool:
        """One-shot repair for an already-bloated index (e.g. from earlier
        pruning). Runs at startup so the file shrinks to its real size."""
        with self._lock:
            if not self._search_index_is_bloated():
                return False
            self._optimize_search_index()
            self._db.commit()
            try:
                self._db.execute("VACUUM")
            except sqlite3.OperationalError:
                return False
            self._checkpoint_wal()
        return True

    def _checkpoint_wal(self) -> bool:
        """Try to compact the WAL without failing an in-flight request."""
        try:
            result = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                return False
            raise
        return result is None or result[0] == 0

    def _delete_session(self, session_id: str) -> None:
        state_ids = [
            row["id"]
            for row in self._db.execute(
                "SELECT id FROM states WHERE session_id=?", (session_id,)
            )
        ]
        call_ids = [
            row["id"]
            for row in self._db.execute(
                "SELECT id FROM calls WHERE session_id=?", (session_id,)
            )
        ]
        event_ids = [
            row["id"]
            for row in self._db.execute(
                "SELECT id FROM events WHERE session_id=?", (session_id,)
            )
        ]
        with self._db:
            if state_ids:
                placeholders = ",".join("?" for _ in state_ids)
                self._db.execute(
                    f"""
                    UPDATE states
                    SET parent_state_id=NULL, parent_source='pruned', similarity=NULL
                    WHERE parent_state_id IN ({placeholders})
                    """,
                    state_ids,
                )
            for owner_type, identifiers in (("call", call_ids), ("event", event_ids)):
                if identifiers:
                    placeholders = ",".join("?" for _ in identifiers)
                    self._db.execute(
                        f"DELETE FROM search_documents "
                        f"WHERE owner_type=? AND owner_id IN ({placeholders})",
                        (owner_type, *identifiers),
                    )
                    self._db.execute(
                        f"DELETE FROM timeline "
                        f"WHERE item_type=? AND item_id IN ({placeholders})",
                        (owner_type, *identifiers),
                    )
            if call_ids:
                placeholders = ",".join("?" for _ in call_ids)
                self._db.execute(
                    f"DELETE FROM stream_events WHERE call_id IN ({placeholders})",
                    call_ids,
                )
            self._db.execute(
                "DELETE FROM event_field_heads WHERE session_id=?", (session_id,)
            )
            self._db.execute("DELETE FROM calls WHERE session_id=?", (session_id,))
            self._db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self._db.execute("DELETE FROM states WHERE session_id=?", (session_id,))

    def history_reset_preview(self, call_id: int) -> dict[str, Any]:
        """Describe permanently deleting ``call_id`` and older calls in its session."""
        with self._lock:
            selected = self._db.execute(
                """
                SELECT c.id, c.session_id, timeline.sequence
                FROM calls c
                JOIN timeline
                  ON timeline.item_type='call' AND timeline.item_id=c.id
                WHERE c.id=?
                """,
                (call_id,),
            ).fetchone()
            if not selected:
                raise KeyError(call_id)
            # The selected call is removed together with everything older, so the
            # boundary is inclusive.
            older = self._db.execute(
                """
                SELECT c.id, c.status
                FROM calls c
                JOIN timeline
                  ON timeline.item_type='call' AND timeline.item_id=c.id
                WHERE c.session_id=? AND timeline.sequence<=?
                ORDER BY timeline.sequence
                """,
                (selected["session_id"], selected["sequence"]),
            ).fetchall()
            running = [row["id"] for row in older if row["status"] == "running"]
            return {
                "selected_call_id": selected["id"],
                "session_id": selected["session_id"],
                "delete_calls": len(older),
                "running_call_ids": running,
                "can_reset": not running,
            }

    def reset_history_before_call(self, call_id: int) -> dict[str, Any]:
        """Permanently delete older calls in the selected call's session.

        The selected call becomes the session's storage boundary. Surviving
        calls keep their complete request/response blobs, while references to
        deleted calls and orphan request states are detached.
        """
        with self._lock:
            preview = self.history_reset_preview(call_id)
            if preview["running_call_ids"]:
                identifiers = ", ".join(
                    f"#{identifier}" for identifier in preview["running_call_ids"]
                )
                raise ValueError(
                    f"cannot delete older history while calls {identifiers} are running"
                )
            selected = self._db.execute(
                """
                SELECT c.session_id, timeline.sequence
                FROM calls c
                JOIN timeline
                  ON timeline.item_type='call' AND timeline.item_id=c.id
                WHERE c.id=?
                """,
                (call_id,),
            ).fetchone()
            older_rows = self._db.execute(
                """
                SELECT c.id, c.req_id
                FROM calls c
                JOIN timeline
                  ON timeline.item_type='call' AND timeline.item_id=c.id
                WHERE c.session_id=? AND timeline.sequence<=?
                ORDER BY timeline.sequence
                """,
                (selected["session_id"], selected["sequence"]),
            ).fetchall()
            call_ids = [row["id"] for row in older_rows]
            if not call_ids:
                return {
                    **preview,
                    "deleted_calls": 0,
                    "remaining_calls": self._db.execute(
                        "SELECT COUNT(*) FROM calls WHERE session_id=?",
                        (selected["session_id"],),
                    ).fetchone()[0],
                }

            placeholders = ",".join("?" for _ in call_ids)
            session_state_ids = {
                row["id"]
                for row in self._db.execute(
                    "SELECT id FROM states WHERE session_id=?",
                    (selected["session_id"],),
                )
            }
            surviving_state_ids = {
                row["request_state_id"]
                for row in self._db.execute(
                    f"""
                    SELECT DISTINCT request_state_id FROM calls
                    WHERE id NOT IN ({placeholders})
                    """,
                    call_ids,
                )
            }
            orphan_state_ids = sorted(session_state_ids - surviving_state_ids)
            removed_req_ids = sorted({
                row["req_id"] for row in older_rows if row["req_id"] is not None
            })

            # The VACUUM below rebuilds the file from live pages only, so deleted
            # payloads never reach the new file — no need to also overwrite them
            # in place first. In-place secure_delete on a large bulk delete cost
            # ~20s and only duplicated what VACUUM already achieves.
            self._db.execute("PRAGMA secure_delete=OFF")
            with self._db:
                self._db.execute(
                    f"""
                    UPDATE calls SET chronological_parent_id=NULL
                    WHERE chronological_parent_id IN ({placeholders})
                    """,
                    call_ids,
                )
                if removed_req_ids:
                    req_placeholders = ",".join("?" for _ in removed_req_ids)
                    self._db.execute(
                        f"""
                        UPDATE calls SET prev_req_id=NULL
                        WHERE session_id=? AND id NOT IN ({placeholders})
                          AND prev_req_id IN ({req_placeholders})
                        """,
                        (selected["session_id"], *call_ids, *removed_req_ids),
                    )
                self._db.execute(
                    f"""
                    DELETE FROM search_documents
                    WHERE owner_type='call' AND owner_id IN ({placeholders})
                    """,
                    call_ids,
                )
                self._db.execute(
                    f"""
                    DELETE FROM timeline
                    WHERE item_type='call' AND item_id IN ({placeholders})
                    """,
                    call_ids,
                )
                self._db.execute(
                    f"DELETE FROM stream_events WHERE call_id IN ({placeholders})",
                    call_ids,
                )
                self._db.execute(
                    f"DELETE FROM calls WHERE id IN ({placeholders})",
                    call_ids,
                )
                if orphan_state_ids:
                    state_placeholders = ",".join("?" for _ in orphan_state_ids)
                    self._db.execute(
                        f"""
                        UPDATE states
                        SET parent_state_id=NULL, parent_source='history-reset',
                            similarity=NULL
                        WHERE parent_state_id IN ({state_placeholders})
                        """,
                        orphan_state_ids,
                    )
                    self._db.execute(
                        f"DELETE FROM states WHERE id IN ({state_placeholders})",
                        orphan_state_ids,
                    )

            self._garbage_collect_blobs()
            self._checkpoint_wal()
            self._db.execute("VACUUM")
            self._checkpoint_wal()
            return {
                **preview,
                "deleted_calls": len(call_ids),
                "remaining_calls": self._db.execute(
                    "SELECT COUNT(*) FROM calls WHERE session_id=?",
                    (selected["session_id"],),
                ).fetchone()[0],
            }

    @staticmethod
    def _blob_refs(value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, dict):
            if isinstance(value.get("$blob"), str):
                refs.add(value["$blob"])
            for item in value.values():
                refs.update(TraceStore._blob_refs(item))
        elif isinstance(value, list):
            for item in value:
                refs.update(TraceStore._blob_refs(item))
        return refs

    def _garbage_collect_blobs(self) -> None:
        reachable: set[str] = set()
        for table, column in (
            ("states", "request_blob_hash"),
            ("calls", "response_blob_hash"),
            ("calls", "thoughts_blob_hash"),
            ("calls", "raw_response_blob_hash"),
            ("events", "payload_blob_hash"),
            ("stream_events", "data_blob_hash"),
            ("event_field_heads", "blob_hash"),
        ):
            reachable.update(
                row[0]
                for row in self._db.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
                )
            )
        for row in self._db.execute("SELECT manifest_json FROM states"):
            reachable.update(self._blob_refs(json.loads(row["manifest_json"])))
        for row in self._db.execute("SELECT payload_blob_hash FROM events"):
            manifest = json.loads(self.get_text(row["payload_blob_hash"]))
            reachable.update(self._blob_refs(manifest))

        # A delta blob keeps its base alive. Resolve the whole chain from a
        # single in-memory map rather than one query per blob — on a large trace
        # the per-blob walk was thousands of round trips and dominated a reset.
        base_of = {
            row["hash"]: row["base_hash"]
            for row in self._db.execute("SELECT hash, base_hash FROM blobs")
        }
        pending = list(reachable)
        while pending:
            base = base_of.get(pending.pop())
            if base and base not in reachable:
                reachable.add(base)
                pending.append(base)

        # Every blob deleted here is already outside the reachable set, so no
        # surviving row references it — the foreign-key check SQLite would run
        # per deleted blob (a full scan of each referencing table, including the
        # large stream_events) is redundant and cost ~20s on a big trace.
        # Disable it for this delete only; the pragma is a no-op inside a
        # transaction, so it is set before the block begins one.
        self._db.execute("PRAGMA foreign_keys=OFF")
        try:
            with self._db:
                self._db.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS reachable_blobs(hash TEXT PRIMARY KEY)"
                )
                self._db.execute("DELETE FROM reachable_blobs")
                self._db.executemany(
                    "INSERT INTO reachable_blobs(hash) VALUES (?)",
                    ((digest,) for digest in reachable),
                )
                self._db.execute(
                    "DELETE FROM blobs WHERE NOT EXISTS "
                    "(SELECT 1 FROM reachable_blobs WHERE reachable_blobs.hash=blobs.hash)"
                )
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")

    # ---------- reads ----------

    def _resolve(self, value: Any) -> Any:
        # A blob reference is {"$blob": <hash string>}. Only a string hash is a
        # reference; a "$blob" whose value is anything else is ordinary data (or
        # a diff over the field named "$blob") and must be recursed, not fetched.
        if isinstance(value, dict) and isinstance(value.get("$blob"), str):
            text = self.get_text(value["$blob"])
            return json.loads(text) if value.get("format") == "json" else text
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        return value

    def get_call(self, call_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                """
                SELECT c.*, s.parent_state_id, s.parent_source, s.similarity,
                       s.request_blob_hash, s.manifest_json,
                       chronological.request_state_id AS chronological_parent_state_id
                FROM calls c
                JOIN states s ON s.id=c.request_state_id
                LEFT JOIN calls chronological ON chronological.id=c.chronological_parent_id
                WHERE c.id=?
                """,
                (call_id,),
            ).fetchone()
            if not row:
                raise KeyError(call_id)
            manifest = json.loads(row["manifest_json"])
            chronological_similarity = None
            if row["chronological_parent_state_id"]:
                chronological_state = self._db.execute(
                    "SELECT manifest_json FROM states WHERE id=?",
                    (row["chronological_parent_state_id"],),
                ).fetchone()
                if chronological_state:
                    chronological_similarity = self._manifest_similarity(
                        json.loads(chronological_state["manifest_json"]), manifest
                    )
            parent_manifest = None
            # Where the input diff's baseline came from, so the viewer can tell a
            # real ancestor ("state") from a chronological stand-in ("sibling"
            # for a concurrent lane, "previous" otherwise). A parallel lane forks
            # with parent_state_id NULL: it has no previous state at all.
            input_parent_call_id = None
            input_parent_source = None
            if row["parent_state_id"]:
                parent = self._db.execute(
                    "SELECT manifest_json FROM states WHERE id=?", (row["parent_state_id"],)
                ).fetchone()
                parent_manifest = json.loads(parent["manifest_json"]) if parent else None
                if parent_manifest is not None:
                    # The input diff compares against a state; name the call that
                    # last used it so an unchanged input can cite its ancestor.
                    parent_call = self._db.execute(
                        """
                        SELECT id FROM calls
                        WHERE id<? AND session_id=? AND request_state_id=?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (call_id, row["session_id"], row["parent_state_id"]),
                    ).fetchone()
                    input_parent_call_id = parent_call["id"] if parent_call else None
                    input_parent_source = "state"
            if parent_manifest is None:
                previous_same = self._db.execute(
                    """
                    SELECT s.manifest_json, s.id AS state_id, previous.id AS call_id,
                           previous.branch_id AS call_branch_id
                    FROM calls previous
                    JOIN states s ON s.id=previous.request_state_id
                    WHERE previous.id<? AND previous.session_id=?
                      AND previous.branch_root_id=? AND previous.purpose=?
                    ORDER BY
                      CASE WHEN previous.branch_id=? THEN 0 ELSE 1 END,
                      previous.id DESC
                    LIMIT 1
                    """,
                    (
                        call_id,
                        row["session_id"],
                        row["branch_root_id"],
                        row["purpose"],
                        row["branch_id"],
                    ),
                ).fetchone()
                if previous_same:
                    parent_manifest = json.loads(previous_same["manifest_json"])
                    input_parent_call_id = previous_same["call_id"]
                    input_parent_source = (
                        "sibling"
                        if previous_same["call_branch_id"] != row["branch_id"]
                        else "previous"
                    )
            call_diff = diff_manifests(parent_manifest, manifest)
            if (
                parent_manifest
                and manifest.get("kind") == "completion"
                and parent_manifest.get("kind") == "completion"
            ):
                old_prompt = self.get_text(parent_manifest["prompt"]["$blob"])
                new_prompt = self.get_text(manifest["prompt"]["$blob"])
                call_diff["prompt"] = compact_text_diff(old_prompt, new_prompt)
            response = self.get_text(row["response_blob_hash"]) if row["response_blob_hash"] else ""
            thoughts = (
                self.get_text(row["thoughts_blob_hash"])
                if row["thoughts_blob_hash"]
                else ""
            )
            raw_response = (
                self.get_text(row["raw_response_blob_hash"])
                if row["raw_response_blob_hash"]
                else response
            )
            metadata = json.loads(row["metadata_json"])
            # Calls stored before token accounting was introduced still retain
            # their exact provider envelope. Backfill usage lazily when the call
            # is opened instead of scanning every potentially huge raw response
            # in an existing trace during startup.
            if "usage" not in metadata and row["raw_response_blob_hash"]:
                streaming = any(
                    line.lstrip().startswith("data:")
                    for line in raw_response.splitlines()[:5]
                )
                parsed_usage = extract_model_response(
                    raw_response, streaming=streaming
                ).usage
                if parsed_usage:
                    metadata["usage"] = parsed_usage
                    with self._db:
                        self._db.execute(
                            "UPDATE calls SET metadata_json=? WHERE id=?",
                            (_json(metadata), call_id),
                        )
            output_parent = self._db.execute(
                """
                SELECT id, request_state_id, response_blob_hash, thoughts_blob_hash
                FROM calls
                WHERE id<? AND session_id=? AND branch_id=? AND purpose=?
                  AND response_blob_hash IS NOT NULL
                  AND (
                    request_state_id=?
                    OR (? IS NOT NULL AND request_state_id=?)
                  )
                ORDER BY
                  CASE WHEN request_state_id=? THEN 0 ELSE 1 END,
                  id DESC
                LIMIT 1
                """,
                (
                    call_id,
                    row["session_id"],
                    row["branch_id"],
                    row["purpose"],
                    row["request_state_id"],
                    row["parent_state_id"],
                    row["parent_state_id"],
                    row["request_state_id"],
                ),
            ).fetchone()
            if output_parent:
                previous_response = self.get_text(output_parent["response_blob_hash"])
                output_diff = compact_token_diff(previous_response, response)
                output_diff["base_call_id"] = output_parent["id"]
                previous_thoughts = (
                    self.get_text(output_parent["thoughts_blob_hash"])
                    if output_parent["thoughts_blob_hash"]
                    else ""
                )
                if not previous_thoughts and thoughts:
                    thoughts_diff = {
                        "mode": "snapshot",
                        "similarity": None,
                        "changes": [],
                    }
                else:
                    thoughts_diff = compact_token_diff(previous_thoughts, thoughts)
                thoughts_diff["base_call_id"] = output_parent["id"]
                output_parent_same_request = (
                    output_parent["request_state_id"] == row["request_state_id"]
                )
            else:
                # No earlier call shares this state (e.g. every FIT_TO_SCHEMA call
                # is the first at its freshly built message list). Carry the value
                # so the snapshot is self-describing — like the event path does at
                # {"mode": "snapshot", "value": payload} — rather than an empty diff
                # that says nothing on its own.
                output_diff = {
                    "mode": "snapshot",
                    "base_call_id": None,
                    "similarity": None,
                    "value": response,
                    "changes": [],
                }
                thoughts_diff = {
                    "mode": "snapshot",
                    "base_call_id": None,
                    "similarity": None,
                    "value": thoughts,
                    "changes": [],
                }
                output_parent_same_request = False
            return {
                "type": "call",
                "id": row["id"],
                "created_at": row["created_at"],
                "session_id": row["session_id"],
                "branch_id": row["branch_id"],
                "branch_root_id": row["branch_root_id"],
                "purpose": row["purpose"],
                "status": row["status"],
                "chronological_parent_id": row["chronological_parent_id"],
                "chronological_parent_state_id": row["chronological_parent_state_id"],
                "chronological_similarity": chronological_similarity,
                "request_state_id": row["request_state_id"],
                "parent_state_id": row["parent_state_id"],
                "parent_source": row["parent_source"],
                "similarity": row["similarity"],
                "req_id": row["req_id"],
                "prev_req_id": row["prev_req_id"],
                "diff": self._resolve(call_diff),
                "input_parent_call_id": input_parent_call_id,
                "input_parent_source": input_parent_source,
                "output_diff": output_diff,
                "output_parent_call_id": output_diff["base_call_id"],
                "output_parent_same_request": output_parent_same_request,
                "request": json.loads(self.get_text(row["request_blob_hash"])),
                "response": response,
                "thoughts": thoughts,
                "thoughts_diff": thoughts_diff,
                "raw_response": raw_response,
                "metadata": metadata,
            }

    def get_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError(event_id)
            manifest = json.loads(self.get_text(row["payload_blob_hash"]))
            payload = self._resolve(manifest)
            previous = self._db.execute(
                """
                SELECT payload_blob_hash FROM events
                WHERE session_id=? AND branch_id=? AND kind=? AND id<?
                ORDER BY id DESC LIMIT 1
                """,
                (row["session_id"], row["branch_id"], row["kind"], event_id),
            ).fetchone()
            previous_payload = None
            if previous:
                previous_manifest = json.loads(self.get_text(previous["payload_blob_hash"]))
                previous_payload = self._resolve(previous_manifest)
            event_diff = (
                diff_values(previous_payload, payload)
                if previous_payload is not None
                else {"mode": "snapshot", "value": payload}
            )
            if (
                previous_payload is not None
                and isinstance(previous_payload, dict)
                and isinstance(payload, dict)
                and event_diff.get("mode") == "diff"
            ):
                for field in set(previous_payload) & set(payload):
                    old_value = previous_payload[field]
                    new_value = payload[field]
                    if (
                        isinstance(old_value, str)
                        and isinstance(new_value, str)
                        and old_value != new_value
                        and max(len(old_value), len(new_value)) >= 256
                    ):
                        event_diff["fields"][field] = compact_text_diff(
                            old_value, new_value
                        )
            return {
                "type": "event",
                "id": row["id"],
                "created_at": row["created_at"],
                "session_id": row["session_id"],
                "branch_id": row["branch_id"],
                "kind": row["kind"],
                "diff": event_diff,
                "payload": payload,
            }

    def timeline(self, limit: int = 500, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if session_id is None:
                rows = self._db.execute(
                    "SELECT * FROM timeline ORDER BY sequence DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._db.execute(
                    """
                    SELECT timeline.*
                    FROM timeline
                    WHERE (
                        timeline.item_type='call'
                        AND EXISTS (
                            SELECT 1 FROM calls
                            WHERE calls.id=timeline.item_id AND calls.session_id=?
                        )
                    ) OR (
                        timeline.item_type='event'
                        AND EXISTS (
                            SELECT 1 FROM events
                            WHERE events.id=timeline.item_id AND events.session_id=?
                        )
                    )
                    ORDER BY timeline.sequence DESC
                    LIMIT ?
                    """,
                    (session_id, session_id, limit),
                ).fetchall()
            call_ids = [
                row["item_id"] for row in rows if row["item_type"] == "call"
            ]
            event_ids = [
                row["item_id"] for row in rows if row["item_type"] == "event"
            ]
            calls: dict[int, sqlite3.Row] = {}
            events: dict[int, sqlite3.Row] = {}
            # Stay below the conservative SQLite host-parameter limit used by
            # older distributions while still replacing the previous N+1
            # query pattern with a handful of batches.
            for offset in range(0, len(call_ids), 900):
                batch = call_ids[offset:offset + 900]
                placeholders = ",".join("?" for _ in batch)
                call_rows = self._db.execute(
                    f"""
                    SELECT c.id, c.session_id, c.branch_id, c.branch_root_id,
                           c.purpose AS label, c.status, c.req_id, c.prev_req_id,
                           c.request_state_id, s.parent_state_id,
                           c.metadata_json
                    FROM calls c JOIN states s ON s.id=c.request_state_id
                    WHERE c.id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                calls.update({item["id"]: item for item in call_rows})
            for offset in range(0, len(event_ids), 900):
                batch = event_ids[offset:offset + 900]
                placeholders = ",".join("?" for _ in batch)
                event_rows = self._db.execute(
                    f"""
                    SELECT id, session_id, branch_id, kind AS label,
                           'event' AS status
                    FROM events
                    WHERE id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                events.update({item["id"]: item for item in event_rows})
            items: list[dict[str, Any]] = []
            for row in reversed(rows):
                item = (
                    calls.get(row["item_id"])
                    if row["item_type"] == "call"
                    else events.get(row["item_id"])
                )
                if item:
                    item_data = dict(item)
                    metadata = json.loads(item_data.pop("metadata_json", "{}"))
                    if isinstance(metadata.get("duration_ms"), (int, float)):
                        item_data["duration_ms"] = metadata["duration_ms"]
                    if metadata.get("debug_label"):
                        item_data["debug_label"] = metadata["debug_label"]
                    if metadata.get("title"):
                        item_data["title"] = metadata["title"]
                    if isinstance(metadata.get("usage"), dict):
                        item_data["usage"] = metadata["usage"]
                    if metadata.get("request_id"):
                        item_data["request_id"] = metadata["request_id"]
                    if metadata.get("group"):
                        item_data["group"] = metadata["group"]
                    items.append(
                        {
                            "sequence": row["sequence"],
                            "created_at": row["created_at"],
                            "type": row["item_type"],
                            **item_data,
                        }
                    )
            return items

    def sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT session_id, MAX(created_at) AS last_at,
                       SUM(call_count) AS calls, SUM(event_count) AS events
                FROM (
                    SELECT session_id, created_at, 1 AS call_count, 0 AS event_count FROM calls
                    UNION ALL
                    SELECT session_id, created_at, 0 AS call_count, 1 AS event_count FROM events
                )
                GROUP BY session_id
                ORDER BY last_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int = 100,
        session_id: str | None = None,
        fields: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._search_unlocked(query, limit, session_id, fields)

    def _search_unlocked(
        self,
        query: str,
        limit: int,
        session_id: str | None,
        fields: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        candidate_limit = limit * 5 if session_id else limit
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        needle = query.casefold()
        rows: list[sqlite3.Row] = []
        seen: set[tuple[str, int, str]] = set()

        # Exact substring (infix) matches come first. A literal query — a table
        # row, text with punctuation — is what the user means to find verbatim,
        # but FTS's tokenizer discards the punctuation and can reduce such a query
        # to a single common token that floods the results. The trace is
        # size-bounded, so this case-folded scan is affordable, and it also gives
        # predictable infix matching (including Cyrillic) that the token index
        # cannot. Special characters therefore never break search.
        for row in self._db.execute(
            "SELECT owner_type, owner_id, field, text FROM search_documents"
        ):
            if fields is not None and row["field"] not in fields:
                continue
            if needle and needle in row["text"].casefold():
                key = (row["owner_type"], row["owner_id"], row["field"])
                if key not in seen:
                    rows.append(row)
                    seen.add(key)

        # Token-prefix matches supplement: FTS finds word-boundary matches across
        # formatting that a raw substring cannot. Each term is quoted (with its
        # own quotes doubled), so no query character reaches FTS as syntax.
        prefix_query = " AND ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms
        )
        if prefix_query:
            field_clause = ""
            field_params: list[Any] = []
            if fields:
                placeholders = ",".join("?" for _ in fields)
                field_clause = f" AND field IN ({placeholders})"
                field_params = sorted(fields)
            for row in self._db.execute(
                f"""
                SELECT owner_type, owner_id, field, text
                FROM search_documents
                WHERE search_documents MATCH ?
                {field_clause}
                LIMIT ?
                """,
                (prefix_query, *field_params, candidate_limit),
            ).fetchall():
                key = (row["owner_type"], row["owner_id"], row["field"])
                if key not in seen:
                    rows.append(row)
                    seen.add(key)

        results: list[dict[str, Any]] = []
        for row in rows:
            if fields is not None and row["field"] not in fields:
                continue
            result = {
                "owner_type": row["owner_type"],
                "owner_id": row["owner_id"],
                "field": row["field"],
                "snippet": self._search_snippet(row["text"], query, terms),
            }
            table = "calls" if result["owner_type"] == "call" else "events"
            owner = self._db.execute(
                f"SELECT session_id FROM {table} WHERE id=?", (result["owner_id"],)
            ).fetchone()
            if not owner or (session_id is not None and owner["session_id"] != session_id):
                continue
            result["session_id"] = owner["session_id"]
            results.append(result)
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _search_snippet(text: str, query: str, terms: list[str]) -> str:
        folded = text.casefold()
        # Centre the snippet on the most specific match: the full query phrase
        # wherever it occurs, else the longest (most distinctive) term present.
        # Picking the earliest match instead — as before — centred on a common
        # short word like "в" and showed unrelated surrounding text, which made
        # correct results look irrelevant.
        match_start = -1
        match_length = 0
        phrase_index = folded.find(query.casefold()) if query else -1
        if phrase_index >= 0:
            match_start = phrase_index
            match_length = len(query)
        else:
            for candidate in sorted(terms, key=len, reverse=True):
                index = folded.find(candidate.casefold())
                if index >= 0:
                    match_start = index
                    match_length = len(candidate)
                    break
        if match_start < 0:
            match_start = 0
            match_length = 0
        start = max(0, match_start - 80)
        end = min(len(text), match_start + max(match_length, 1) + 100)
        before = html.escape(text[start:match_start])
        matched = html.escape(text[match_start : match_start + match_length])
        after = html.escape(text[match_start + match_length : end])
        prefix = "… " if start else ""
        suffix = " …" if end < len(text) else ""
        marked = f"<mark>{matched}</mark>" if matched else ""
        return f"{prefix}{before}{marked}{after}{suffix}"

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                """
                SELECT COUNT(*) AS blobs, COALESCE(SUM(raw_size), 0) AS logical_bytes,
                       COALESCE(SUM(LENGTH(data)), 0) AS stored_bytes,
                       SUM(storage='delta') AS deltas
                FROM blobs
                """
            ).fetchone()
            return {
                **dict(row),
                "calls": self._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                "events": self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "file_bytes": self._physical_bytes(),
                "max_file_bytes": self.max_file_bytes,
                "last_pruned_sessions": list(self._last_pruned_sessions),
            }
