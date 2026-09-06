from __future__ import annotations

import hashlib
import json
import re

import pytest

from insequent_logger import TraceStore


def test_search_with_special_characters_finds_the_literal_row(tmp_path):
    # A literal query full of punctuation (a copied table row) must find its exact
    # row, not degrade to matching a lone common token and flood the results —
    # FTS tokenizing "|  6 |  …" down to "6" is what made search seem to fail.
    store = TraceStore(tmp_path / "special.llmtrace")
    target = store.start_call(
        {"model": "local", "messages": [
            {"role": "user", "content": "header\n|  6 |  Насосная станция  |  готово |\nfooter"},
        ]},
        session_id="s",
    )
    store.finish_call(target, "ok")
    for i in range(25):  # noise that all contain "6"
        call = store.start_call(
            {"model": "local", "messages": [
                {"role": "user", "content": f"random text with 6 and number {i}6 more"},
            ]},
            session_id="s",
        )
        store.finish_call(call, f"result 6 item {i}")

    results = store.search("|  6 |  Насосная станция  |  готово |", limit=10)
    assert results, "a literal query must find its row"
    assert results[0]["owner_id"] == target, results[0]
    assert all(r["owner_id"] == target for r in results), results

    # A query that is only punctuation and one common token must never raise.
    assert isinstance(store.search("|  6 |        |           ", limit=5), list)
    assert isinstance(store.search('| "unbalanced |', limit=5), list)
    store.close()


def test_search_snippet_centres_on_the_phrase_not_a_stray_letter(tmp_path):
    # A long doc that contains the phrase also contains its common short words
    # (e.g. "в") much earlier. The snippet must centre on the phrase, not on the
    # first stray letter — otherwise a correct result looks irrelevant.
    store = TraceStore(tmp_path / "snippet.llmtrace")
    phrase = "В состав проектируемых гидротехнических сооружений"
    text = (
        "в начале документа много общего текста и Нагорные канавы №1-№3 и прочее, "
        "далее по тексту: " + phrase + " входят насосные станции и трубопроводы."
    )
    call = store.start_call(
        {"model": "local", "messages": [{"role": "user", "content": text}]},
        session_id="s",
    )
    store.finish_call(call, "ok")

    results = store.search(phrase, limit=5, fields={"input"})
    assert results, "phrase must be found"
    snippet = results[0]["snippet"]
    marks = re.findall(r"<mark[^>]*>(.*?)</mark>", snippet)
    assert marks == [phrase], marks  # the whole phrase is highlighted, not "в"
    assert phrase in re.sub(r"<[^>]+>", "", snippet)  # and it is in the shown window
    store.close()


def test_pruning_reclaims_full_text_index_segments(tmp_path):
    # Deleting rows only tombstones them in the FTS index; the segment data
    # lingers and VACUUM cannot reclaim it. Without merging, a churned index grows
    # to dwarf the actual content — the bug where a 2-call trace occupied ~57 MB.
    path = tmp_path / "bloat.llmtrace"
    store = TraceStore(path)
    for index in range(60):
        call = store.start_call(
            {"model": "local", "messages": [
                {"role": "user", "content": f"уникальный документ номер {index} с содержимым"},
            ]},
            session_id=f"s{index}",
        )
        store.finish_call(call, f"результат выполнения шага номер {index}")
        store._delete_session(f"s{index}")
    store._db.commit()
    before = store._db.execute("SELECT COUNT(*) FROM search_documents_data").fetchone()[0]
    store._optimize_search_index()
    store._db.commit()
    after = store._db.execute("SELECT COUNT(*) FROM search_documents_data").fetchone()[0]
    # A live call remains searchable after the merge.
    live = store.start_call(
        {"model": "local", "messages": [{"role": "user", "content": "финальный запрос"}]},
        session_id="live",
    )
    store.finish_call(live, "финальный ответ")
    hits = store._db.execute(
        "SELECT COUNT(*) FROM search_documents WHERE search_documents MATCH 'финальный'"
    ).fetchone()[0]
    store.close()
    assert after < before, (before, after)
    assert hits >= 1


def test_startup_reclaims_a_bloated_search_index(tmp_path):
    # A store that opens onto an already-bloated index (from earlier pruning)
    # repairs it once at startup, so the file shrinks to its real size.
    path = tmp_path / "startup.llmtrace"
    store = TraceStore(path)
    for index in range(120):
        call = store.start_call(
            {"model": "local", "messages": [
                {"role": "user", "content": f"документ {index} " * 8},
            ]},
            session_id=f"s{index}",
        )
        store.finish_call(call, f"вывод {index} " * 8)
        store._delete_session(f"s{index}")
    store._db.commit()
    segments_before = store._db.execute(
        "SELECT COUNT(*) FROM search_documents_data"
    ).fetchone()[0]
    was_bloated = store._search_index_is_bloated()
    store.close()

    reopened = TraceStore(path)  # __init__ runs the reclaim
    segments_after = reopened._db.execute(
        "SELECT COUNT(*) FROM search_documents_data"
    ).fetchone()[0]
    reopened.close()

    if was_bloated:  # only assert reclamation when the churn actually bloated it
        assert segments_after < segments_before, (segments_before, segments_after)
    assert not TraceStore(path)._search_index_is_bloated()


def test_base_less_output_snapshot_carries_its_value(tmp_path):
    # A call with no earlier call at its state (e.g. every FIT_TO_SCHEMA call, the
    # first at a freshly built message list) gets a base-less snapshot. It must
    # carry the value so the snapshot is self-describing, not an empty diff that
    # says nothing on its own — mirroring the event path's {"mode": "snapshot",
    # "value": payload}.
    store = TraceStore(tmp_path / "snap.llmtrace")
    call_id = store.start_call(
        {"model": "local", "messages": [{"role": "user", "content": "go"}]},
        session_id="s",
        purpose="FIT_TO_SCHEMA",
    )
    store.finish_call(call_id, '{"step_result":"the produced answer"}', thoughts="reasoning")
    call = store.get_call(call_id)

    assert call["output_parent_call_id"] is None
    assert call["output_diff"]["mode"] == "snapshot"
    assert call["output_diff"]["changes"] == []
    assert call["output_diff"]["value"] == '{"step_result":"the produced answer"}'
    assert call["thoughts_diff"]["mode"] == "snapshot"
    assert call["thoughts_diff"]["value"] == "reasoning"


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

    # The removed side carries the full removed text (so Mixed can show what was
    # removed in full, attributed to the removing call) alongside a compact
    # preview label. The full text never repeats the unchanged shared lines.
    removed_hunk = next(
        hunk["-"]
        for hunk in detail["diff"]["prompt"]["hunks"]
        if isinstance(hunk, dict) and "-" in hunk
    )
    assert removed_hunk["preview"].startswith("WINDOW: one")
    assert removed_hunk["text"] == "WINDOW: one\ncandidate A"
    assert "shared instruction line 050" not in removed_hunk["text"]
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


def test_existing_raw_response_usage_is_backfilled_when_call_is_opened(tmp_path):
    store = TraceStore(tmp_path / "usage-backfill.llmtrace")
    call = store.start_call(
        {"model": "local", "prompt": "count", "stream": True},
        session_id="usage",
    )
    raw = "\n\n".join([
        'data: {"choices":[{"delta":{"content":"done"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":253,'
        '"completion_tokens":1188,"total_tokens":1441}}',
        "data: [DONE]",
    ])
    store.finish_call(call, "done", raw_response=raw)
    assert "usage" not in next(
        item for item in store.timeline() if item.get("id") == call
    )

    assert store.get_call(call)["metadata"]["usage"] == {
        "input_tokens": 253,
        "output_tokens": 1188,
        "total_tokens": 1441,
    }
    assert next(
        item for item in store.timeline() if item.get("id") == call
    )["usage"]["total_tokens"] == 1441
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
    store.finish_call(first, "alpha output", thoughts="alpha private reasoning")
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
    assert {
        item["field"]
        for item in store.search(
            "alpha", session_id="session-a", fields={"thoughts"}
        )
    } == {"thoughts"}
    assert store.search(
        "private reasoning", session_id="session-a", fields={"input"}
    ) == []
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


def test_reset_history_permanently_deletes_calls_before_selected_boundary(tmp_path):
    store = TraceStore(tmp_path / "reset-history.llmtrace")

    def completed(prompt, response, *, session="main", parent=None, req_id=None, prev=None):
        call_id = store.start_call(
            {"model": "local", "prompt": prompt},
            session_id=session,
            explicit_parent_state=parent,
            req_id=req_id,
            prev_req_id=prev,
        )
        store.finish_call(call_id, response)
        return call_id, store.get_call(call_id)["request_state_id"]

    first, first_state = completed("old first", "old response", req_id="A")
    second, second_state = completed(
        "old second", "second response", parent=first_state, req_id="B", prev="A"
    )
    boundary, boundary_state = completed(
        "keep boundary", "boundary response", parent=second_state, req_id="C", prev="B"
    )
    newest, _ = completed(
        "keep newest", "newest response", parent=boundary_state, req_id="D", prev="C"
    )
    other, _ = completed("other session", "other response", session="other")

    # Reset is inclusive of the selected call: selecting `second` removes it and
    # everything older (first), leaving the boundary and newer calls.
    preview = store.history_reset_preview(second)
    assert preview == {
        "selected_call_id": second,
        "session_id": "main",
        "delete_calls": 2,
        "running_call_ids": [],
        "can_reset": True,
    }

    result = store.reset_history_before_call(second)
    assert result["deleted_calls"] == 2
    assert result["remaining_calls"] == 2
    for deleted in (first, second):
        with pytest.raises(KeyError):
            store.get_call(deleted)
    assert store.search("old first", session_id="main") == []
    assert [item["id"] for item in store.timeline(session_id="main")] == [
        boundary,
        newest,
    ]
    assert store.get_call(other)["response"] == "other response"

    boundary_detail = store.get_call(boundary)
    assert boundary_detail["request"]["prompt"] == "keep boundary"
    assert boundary_detail["response"] == "boundary response"
    assert boundary_detail["chronological_parent_id"] is None
    assert boundary_detail["parent_state_id"] is None
    assert boundary_detail["parent_source"] == "history-reset"
    assert boundary_detail["prev_req_id"] is None
    assert boundary_detail["diff"]["mode"] == "snapshot"
    newest_detail = store.get_call(newest)
    assert newest_detail["request"]["prompt"] == "keep newest"
    assert newest_detail["response"] == "newest response"
    assert newest_detail["chronological_parent_id"] == boundary
    assert newest_detail["output_parent_call_id"] == boundary

    # Blob garbage collection deletes with foreign-key enforcement disabled (it
    # only removes blobs the reachability walk proved orphaned); the store must
    # still be referentially intact and leave enforcement back on.
    assert store._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    dangling = store._db.execute(
        """
        SELECT COUNT(*) FROM calls
        WHERE response_blob_hash IS NOT NULL
          AND response_blob_hash NOT IN (SELECT hash FROM blobs)
        """
    ).fetchone()[0]
    assert dangling == 0
    store.close()


def test_reset_history_rejects_deleting_a_running_call(tmp_path):
    store = TraceStore(tmp_path / "reset-running.llmtrace")
    running = store.start_call({"prompt": "still running"}, session_id="main")
    boundary = store.start_call({"prompt": "boundary"}, session_id="main")
    store.finish_call(boundary, "keep")

    preview = store.history_reset_preview(boundary)
    assert preview["running_call_ids"] == [running]
    assert preview["can_reset"] is False
    with pytest.raises(ValueError, match="running"):
        store.reset_history_before_call(boundary)
    assert store.get_call(running)["status"] == "running"
    assert store.get_call(boundary)["response"] == "keep"
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


def test_req_id_declares_explicit_branch_lineage(tmp_path):
    store = TraceStore(tmp_path / "reqid.llmtrace")

    def call(req_id, prev_req_id, content):
        made = store.start_call(
            {"model": "local", "messages": [{"role": "user", "content": content}]},
            session_id="nb",
            branch_id="main",
            req_id=req_id,
            prev_req_id=prev_req_id,
        )
        store.finish_call(made, "ok")
        return made

    # A tree the caller declares itself: B and C both continue A, so they branch.
    a = call("A", None, "root")
    b = call("B", "A", "continue B")
    c = call("C", "A", "branch C")
    d = call("D", "C", "continue D")

    state_a = store.get_call(a)["request_state_id"]
    state_c = store.get_call(c)["request_state_id"]
    detail_b = store.get_call(b)
    detail_c = store.get_call(c)
    detail_d = store.get_call(d)

    assert detail_b["parent_state_id"] == state_a
    assert detail_c["parent_state_id"] == state_a  # sibling branch off A
    assert detail_d["parent_state_id"] == state_c
    assert detail_b["parent_source"] == "explicit"
    assert (detail_b["req_id"], detail_b["prev_req_id"]) == ("B", "A")

    # The identity is surfaced on the timeline row, not only in the full call.
    row = next(item for item in store.timeline() if item.get("id") == d)
    assert row["req_id"] == "D"
    assert row["prev_req_id"] == "C"

    # An unknown predecessor is a caller error, not a silent fallback.
    import pytest

    with pytest.raises(ValueError):
        call("E", "does-not-exist", "x")
    store.close()


def test_timeline_loads_rows_in_batches_instead_of_one_query_per_item(tmp_path):
    store = TraceStore(tmp_path / "batched-timeline.llmtrace")
    for index in range(40):
        call_id = store.start_call(
            {"model": "local", "prompt": f"request {index}"},
            session_id="large-session",
        )
        store.finish_call(call_id, f"response {index}")
    for index in range(4):
        store.record_event(
            "checkpoint",
            {"index": index},
            session_id="large-session",
        )

    statements = []
    store._db.set_trace_callback(statements.append)
    timeline = store.timeline(limit=100, session_id="large-session")
    store._db.set_trace_callback(None)

    selects = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(timeline) == 44
    assert len(selects) == 3, selects
    store.close()


def test_changed_externalized_parameter_resolves_without_crash(tmp_path):
    # Large or nested parameters (e.g. a tool list) are externalized to a blob
    # reference {"$blob": hash}. When such a parameter changes, the diff must not
    # descend into the reference — doing so produced a nested "$blob" key that the
    # reader tried to fetch as a blob, crashing get_call.
    store = TraceStore(tmp_path / "params.llmtrace")
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "d" * 40,
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
        for i in range(6)
    ]
    request = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 80000,
        "stream_options": {"include_usage": True},
        "tools": tools,
    }
    first = store.start_call(request, session_id="p")
    store.finish_call(first, "ok")
    parent_state = store.get_call(first)["request_state_id"]

    changed = [dict(tool) for tool in tools]
    changed[0] = {"type": "function", "function": {"name": "tool_RENAMED"}}
    second = store.start_call(
        {**request, "tools": changed, "messages": [{"role": "user", "content": "next"}]},
        session_id="p",
        explicit_parent_state=parent_state,
    )
    store.finish_call(second, "ok")

    detail = store.get_call(second)  # must not raise
    tools_diff = detail["diff"]["parameters"]["tools"]
    assert tools_diff["op"] == "~"
    # The change resolves to the real tool lists, not blob hashes.
    assert isinstance(tools_diff["old"], list)
    assert isinstance(tools_diff["new"], list)
    assert tools_diff["new"][0]["function"]["name"] == "tool_RENAMED"
    assert detail["request"]["tools"][0]["function"]["name"] == "tool_RENAMED"
    store.close()
