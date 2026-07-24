from __future__ import annotations

import hashlib
import json

from insequent_logger import TraceStore


def test_blob_delta_round_trip_and_dedup(tmp_path):
    store = TraceStore(tmp_path / "test.llmtrace")
    base = "".join(
        f"line {index:04d}: value-{index * 7919:09d} with distinct payload\n"
        for index in range(500)
    )
    changed = base.replace("line 0002:", "line 0002: CHANGED", 1) + "new line\n"
    first = store.put_text(base)
    second = store.put_text(changed, base_hash=first)
    assert store.put_text(base) == first
    assert store.get_text(second) == changed
    assert hashlib.sha256(changed.encode()).hexdigest() == second
    row = store._db.execute("SELECT storage FROM blobs WHERE hash=?", (second,)).fetchone()
    assert row["storage"] == "delta"
    store.close()


def test_chat_states_diff_and_exact_reconstruction(tmp_path):
    store = TraceStore(tmp_path / "test.llmtrace")
    first_request = {
        "model": "local",
        "messages": [
            {"role": "system", "content": "Be exact."},
            {"role": "user", "content": "Remember cobalt blue."},
        ],
        "temperature": 0.1,
    }
    first_call = store.start_call(first_request, session_id="s")
    store.finish_call(first_call, '{"answer":"remembered"}')
    first = store.get_call(first_call)

    second_request = {
        **first_request,
        "messages": [
            *first_request["messages"],
            {"role": "assistant", "content": "Remembered."},
            {"role": "user", "content": "What color?"},
        ],
        "temperature": 0.2,
    }
    second_call = store.start_call(
        second_request,
        session_id="s",
        explicit_parent_state=first["request_state_id"],
    )
    store.finish_call(second_call, '{"answer":"cobalt blue"}')
    second = store.get_call(second_call)

    assert second["request"] == second_request
    assert second["chronological_parent_state_id"] == first["request_state_id"]
    assert second["parent_source"] == "explicit"
    assert second["diff"]["mode"] == "diff"
    assert second["diff"]["parameters"]["temperature"]["new"] == 0.2
    assert second["diff"]["messages"][-1]["op"] == "+"
    assert second["diff"]["messages"][-1]["messages"][0]["content"] == "Remembered."
    assert second["diff"]["messages"][-1]["old_messages"] == []
    assert second["diff"]["messages"][-1]["new_messages"][0]["content"] == "Remembered."
    assert second["output_parent_call_id"] == first_call
    assert second["output_diff"]["mode"] == "diff"
    assert second["output_diff"]["changes"]
    assert all("old_line" in change for change in second["output_diff"]["changes"])
    assert all("new_line" in change for change in second["output_diff"]["changes"])
    assert '{"answer":"' not in str(second["output_diff"]["changes"])

    branch_call = store.start_call(
        {
            **first_request,
            "messages": [
                *first_request["messages"],
                {"role": "user", "content": "Start a different branch."},
            ],
        },
        session_id="s",
        explicit_parent_state=first["request_state_id"],
    )
    branch = store.get_call(branch_call)
    assert branch["parent_state_id"] == first["request_state_id"]
    assert branch["chronological_parent_state_id"] == second["request_state_id"]
    store.close()


def test_completion_prompts_use_compact_sequence_diff(tmp_path):
    store = TraceStore(tmp_path / "completion.llmtrace")
    shared = "\n".join(f"shared instruction line {index:03d}" for index in range(120))
    first_request = {
        "model": "local",
        "prompt": f"{shared}\nWINDOW: one\ncandidate A",
        "temperature": 0.1,
    }
    first = store.start_call(first_request, session_id="notebook", purpose="rewrite")
    store.finish_call(first, "first", raw_response='data: {"choices":[{"text":"first"}]}')

    second_request = {
        "model": "local",
        "prompt": f"{shared}\nWINDOW: two\ncandidate B",
        "temperature": 0.2,
    }
    second = store.start_call(second_request, session_id="notebook", purpose="rewrite")
    store.finish_call(second, "second")
    detail = store.get_call(second)
    rendered_diff = str(detail["diff"])

    assert detail["parent_source"] == "inferred"
    assert detail["similarity"] > 0.95
    assert detail["diff"]["prompt"]["op"] == "~"
    assert "120 unchanged lines" in rendered_diff
    assert "WINDOW: two" in rendered_diff
    assert "shared instruction line 050" not in rendered_diff
    assert detail["raw_response"] == "second"
    store.close()


def test_legacy_stream_envelope_is_migrated_on_open(tmp_path):
    path = tmp_path / "legacy.llmtrace"
    store = TraceStore(path)
    call = store.start_call(
        {"model": "local", "prompt": "test", "stream": True},
        session_id="legacy",
    )
    raw = "\n\n".join(
        [
            'data: {"choices":[{"text":"actual ","finish_reason":null}]}',
            'data: {"choices":[{"text":"text","finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    raw_hash = store.put_text(raw)
    with store._db:
        store._db.execute(
            """
            UPDATE calls
            SET response_blob_hash=?, raw_response_blob_hash=NULL, status='ok'
            WHERE id=?
            """,
            (raw_hash, call),
        )
    store.close()

    reopened = TraceStore(path)
    detail = reopened.get_call(call)
    assert detail["response"] == "actual text"
    assert detail["raw_response"] == raw
    reopened.close()


def test_thoughts_are_stored_separately_from_final_output(tmp_path):
    store = TraceStore(tmp_path / "thoughts.llmtrace")
    call = store.start_call(
        {"model": "local", "messages": [{"role": "user", "content": "answer"}]},
        session_id="thoughts",
    )
    raw = json.dumps(
        {
            "choices": [{
                "message": {
                    "reasoning_content": "consider the evidence",
                    "content": "the final answer",
                }
            }]
        }
    )
    store.finish_call(
        call,
        "the final answer",
        thoughts="consider the evidence",
        raw_response=raw,
    )

    detail = store.get_call(call)
    assert detail["thoughts"] == "consider the evidence"
    assert detail["response"] == "the final answer"
    assert detail["thoughts_diff"]["mode"] == "snapshot"
    assert detail["output_diff"]["mode"] == "snapshot"
    store.close()


def test_legacy_combined_reasoning_is_split_from_raw_response(tmp_path):
    path = tmp_path / "legacy-reasoning.llmtrace"
    store = TraceStore(path)
    call = store.start_call(
        {"model": "local", "messages": [{"role": "user", "content": "answer"}]},
        session_id="legacy-thoughts",
    )
    raw = json.dumps(
        {
            "choices": [{
                "message": {
                    "reasoning_content": "old thoughts",
                    "content": "old answer",
                }
            }]
        }
    )
    response_hash = store.put_text("old thoughtsold answer")
    raw_hash = store.put_text(raw)
    with store._db:
        store._db.execute(
            """
            UPDATE calls
            SET response_blob_hash=?, raw_response_blob_hash=?,
                thoughts_blob_hash=NULL, status='ok'
            WHERE id=?
            """,
            (response_hash, raw_hash, call),
        )
    store.close()

    reopened = TraceStore(path)
    detail = reopened.get_call(call)
    assert detail["thoughts"] == "old thoughts"
    assert detail["response"] == "old answer"
    reopened.close()


def test_summary_side_branch_returns_to_main_parent(tmp_path):
    store = TraceStore(tmp_path / "test.llmtrace")
    main_call = store.start_call(
        {"messages": [{"role": "user", "content": "Fact A"}], "model": "local"},
        session_id="s",
        branch_id="main",
    )
    store.finish_call(main_call, "A")
    main_state = store.get_call(main_call)["request_state_id"]

    summary_call = store.start_call(
        {"messages": [{"role": "user", "content": "Summarize Fact A"}], "model": "local"},
        session_id="s",
        branch_id="summary",
        purpose="summarize",
        explicit_parent_state=main_state,
    )
    store.finish_call(summary_call, "Summary A")

    resumed_call = store.start_call(
        {
            "messages": [
                {"role": "user", "content": "[COMPRESSED]\nSummary A"},
                {"role": "user", "content": "Continue"},
            ],
            "model": "local",
        },
        session_id="s",
        branch_id="main",
        purpose="chat-after-compression",
        explicit_parent_state=main_state,
    )
    store.finish_call(resumed_call, "Continued")
    resumed = store.get_call(resumed_call)
    assert resumed["parent_state_id"] == main_state
    assert resumed["chronological_parent_id"] == summary_call
    store.close()


def test_parallel_calls_with_identical_clocks_keep_distinct_outputs(
    tmp_path, monkeypatch
):
    timestamp = "2026-07-24T12:00:00.000000+00:00"
    monkeypatch.setattr("insequent_logger.store._now", lambda: timestamp)
    store = TraceStore(tmp_path / "parallel.llmtrace")
    request = {
        "messages": [{"role": "user", "content": "Generate an alternative."}],
        "model": "local",
        "temperature": 0.8,
    }

    first = store.start_call(request, session_id="parallel", purpose="alternative")
    second = store.start_call(request, session_id="parallel", purpose="alternative")
    third = store.start_call(request, session_id="parallel", purpose="alternative")
    store.finish_call(first, "Alternative A")
    store.finish_call(second, "Alternative B")
    store.finish_call(third, "Alternative B")

    timeline = store.timeline(session_id="parallel")
    assert [(item["type"], item["id"]) for item in timeline] == [
        ("call", first),
        ("call", second),
        ("call", third),
    ]
    assert [item["created_at"] for item in timeline] == [
        timestamp,
        timestamp,
        timestamp,
    ]
    assert timeline[0]["sequence"] < timeline[1]["sequence"]
    assert timeline[0]["sequence"] != 0

    first_detail = store.get_call(first)
    second_detail = store.get_call(second)
    third_detail = store.get_call(third)
    assert first_detail["branch_id"] == "main"
    assert second_detail["branch_id"] == "main~parallel-2"
    assert third_detail["branch_id"] == "main~parallel-3"
    assert len(
        {
            first_detail["request_state_id"],
            second_detail["request_state_id"],
            third_detail["request_state_id"],
        }
    ) == 3
    assert first_detail["response"] == "Alternative A"
    assert second_detail["response"] == "Alternative B"
    assert third_detail["response"] == "Alternative B"
    assert second_detail["output_parent_call_id"] is None
    assert third_detail["output_parent_call_id"] is None
    store.close()


def test_parallel_completion_uses_nearest_root_call_as_diff_base(tmp_path):
    store = TraceStore(tmp_path / "parallel-diff-base.llmtrace")
    shared = "\n".join(f"shared prompt line {index}" for index in range(100))
    first = store.start_call(
        {"model": "local", "prompt": f"{shared}\nvariant A"},
        session_id="parallel-diff",
        purpose="rewrite",
    )
    second = store.start_call(
        {"model": "local", "prompt": f"{shared}\nvariant B"},
        session_id="parallel-diff",
        purpose="rewrite",
    )

    second_detail = store.get_call(second)
    assert second_detail["branch_id"] == "main~parallel-2"
    assert second_detail["parent_state_id"] is None
    assert second_detail["diff"]["mode"] == "diff"
    assert second_detail["diff"]["prompt"]["op"] == "~"
    assert second_detail["diff"]["prompt"]["hunks"][0] == {
        "=": "100 unchanged lines"
    }

    store.finish_call(first, "A")
    store.finish_call(second, "B")
    store.close()


def test_overlapping_calls_create_and_continue_automatic_branches(tmp_path):
    store = TraceStore(tmp_path / "automatic-branches.llmtrace")
    common = [{"role": "system", "content": "Work independently."}]
    base_call = store.start_call(
        {"messages": [*common, {"role": "user", "content": "Prepare options."}]},
        session_id="parallel",
    )
    store.finish_call(base_call, "Ready")
    base_state = store.get_call(base_call)["request_state_id"]

    first = store.start_call(
        {
            "messages": [
                *common,
                {"role": "user", "content": "Prepare options."},
                {"role": "assistant", "content": "Ready"},
                {"role": "user", "content": "Develop option A."},
            ]
        },
        session_id="parallel",
        explicit_parent_state=base_state,
    )
    second = store.start_call(
        {
            "messages": [
                *common,
                {"role": "user", "content": "Prepare options."},
                {"role": "assistant", "content": "Ready"},
                {"role": "user", "content": "Develop option B."},
            ]
        },
        session_id="parallel",
    )

    first_detail = store.get_call(first)
    second_detail = store.get_call(second)
    assert first_detail["branch_id"] == "main"
    assert second_detail["branch_id"] == "main~parallel-2"
    assert second_detail["branch_root_id"] == "main"
    assert second_detail["parent_state_id"] == base_state

    store.finish_call(second, "Option B result")
    second_followup = store.start_call(
        {
            "messages": [
                *common,
                {"role": "user", "content": "Prepare options."},
                {"role": "assistant", "content": "Ready"},
                {"role": "user", "content": "Develop option B."},
                {"role": "assistant", "content": "Option B result"},
                {"role": "user", "content": "Continue B."},
            ]
        },
        session_id="parallel",
    )
    assert store.get_call(second_followup)["branch_id"] == "main~parallel-2"

    store.finish_call(first, "Option A result")
    store.finish_call(second_followup, "Option B continued")
    first_followup = store.start_call(
        {
            "messages": [
                *common,
                {"role": "user", "content": "Prepare options."},
                {"role": "assistant", "content": "Ready"},
                {"role": "user", "content": "Develop option A."},
                {"role": "assistant", "content": "Option A result"},
                {"role": "user", "content": "Continue A."},
            ]
        },
        session_id="parallel",
    )

    assert store.get_call(first_followup)["branch_id"] == "main"
    store.close()


def test_application_event_delta_and_search(tmp_path):
    store = TraceStore(tmp_path / "test.llmtrace")
    decisions = {str(i): {"kind": "B", "window": 1} for i in range(100)}
    first = store.record_event("resolved_snapshot", {"decisions": decisions}, session_id="n")
    decisions["100"] = {"kind": "H", "window": 2}
    second = store.record_event("resolved_snapshot", {"decisions": decisions}, session_id="n")
    detail = store.get_event(second)
    assert detail["payload"]["decisions"]["100"]["kind"] == "H"
    assert detail["diff"]["mode"] == "diff"
    assert any(result["owner_id"] == second for result in store.search("window"))
    stats = store.stats()
    assert stats["deltas"] >= 1
    assert store.get_event(first)["diff"]["mode"] == "snapshot"
    store.close()


def test_large_event_text_diff_does_not_repeat_shared_content(tmp_path):
    store = TraceStore(tmp_path / "event-text.llmtrace")
    shared = "\n".join(f"unchanged document line {index}" for index in range(100))
    store.record_event(
        "rendered_output",
        {"markdown": f"{shared}\nold ending"},
        session_id="n",
    )
    second = store.record_event(
        "rendered_output",
        {"markdown": f"{shared}\nnew ending"},
        session_id="n",
    )
    detail = store.get_event(second)
    rendered = str(detail["diff"])
    assert "100 unchanged lines" in rendered
    assert "new ending" in rendered
    assert "unchanged document line 050" not in rendered
    store.close()


def test_sessions_and_session_filtered_search(tmp_path):
    store = TraceStore(tmp_path / "sessions.llmtrace")
    first = store.start_call(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "alpha private term; Концентрация загрязняющих веществ",
                }
            ]
        },
        session_id="session-a",
    )
    store.finish_call(first, "alpha output")
    second = store.start_call(
        {"messages": [{"role": "user", "content": "beta private term"}]},
        session_id="session-b",
    )
    store.finish_call(second, "beta output")

    assert [item["session_id"] for item in store.sessions()] == [
        "session-b",
        "session-a",
    ]
    assert all(
        item["session_id"] == "session-a"
        for item in store.timeline(session_id="session-a")
    )
    assert store.search("private", session_id="session-a")
    assert {
        item["session_id"] for item in store.search("private", session_id="session-a")
    } == {"session-a"}
    assert store.search("priv", session_id="session-a")
    assert store.search("ivat", session_id="session-a")
    cyrillic = store.search("ЦЕНТРАЦ", session_id="session-a")
    assert cyrillic
    assert "<mark>центрац</mark>" in cyrillic[0]["snippet"].casefold()
    assert store.search('" punctuation', session_id="session-a") == []
    store.close()


def test_disk_limit_prunes_complete_oldest_sessions(tmp_path):
    limit = 3 * 1024 * 1024
    store = TraceStore(tmp_path / "retained.llmtrace", max_file_bytes=limit)

    def noisy_text(seed: str) -> str:
        return "".join(
            hashlib.sha256(f"{seed}-{index}".encode()).hexdigest()
            for index in range(2000)
        )

    for index in range(12):
        call = store.start_call(
            {
                "messages": [
                    {"role": "user", "content": noisy_text(f"input-{index}")}
                ],
                "model": "local",
            },
            session_id=f"session-{index}",
        )
        store.finish_call(call, noisy_text(f"output-{index}"))

    sessions = [item["session_id"] for item in store.sessions()]
    assert "session-11" in sessions
    assert "session-0" not in sessions
    assert store.stats()["file_bytes"] <= limit
    assert store.stats()["max_file_bytes"] == limit
    store.close()


def test_restart_marks_persisted_running_calls_interrupted(tmp_path):
    path = tmp_path / "interrupted.llmtrace"
    store = TraceStore(path)
    call = store.start_call(
        {"prompt": "unfinished", "model": "local"},
        session_id="old-run",
    )
    store.close()

    reopened = TraceStore(path)
    assert reopened.get_call(call)["status"] == "interrupted"
    reopened.close()


def test_retention_never_deletes_newest_session_while_older_is_running(tmp_path):
    store = TraceStore(tmp_path / "newest.llmtrace")
    store.max_file_bytes = 1
    store._physical_bytes = lambda: 2

    old_call = store.start_call(
        {"prompt": "old active", "model": "local"},
        session_id="old-active",
    )
    newest_call = store.start_call(
        {"prompt": "new complete", "model": "local"},
        session_id="newest",
    )
    store.finish_call(newest_call, "new result")

    assert {session["session_id"] for session in store.sessions()} == {
        "old-active",
        "newest",
    }

    store.finish_call(old_call, "old result")
    assert [session["session_id"] for session in store.sessions()] == ["newest"]
    assert store.get_call(newest_call)["response"] == "new result"
    store.close()


def test_retention_defers_when_wal_checkpoint_is_locked(tmp_path):
    store = TraceStore(tmp_path / "busy-checkpoint.llmtrace")
    call = store.start_call(
        {"prompt": "active transaction", "model": "local"},
        session_id="busy",
    )
    store.max_file_bytes = 1
    store._physical_bytes = lambda: 2

    store._db.execute("BEGIN IMMEDIATE")
    store._db.execute(
        "UPDATE calls SET metadata_json=? WHERE id=?",
        ('{"pending":true}', call),
    )
    assert store.enforce_size_limit() == []
    store._db.rollback()

    store.finish_call(call, "completed")
    store.close()
