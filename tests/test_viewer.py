from __future__ import annotations

import re
import threading
import time

import pytest
import requests
from playwright.sync_api import Page, expect

from insequent_logger.server import TraceServer
from insequent_logger.store import TraceStore


@pytest.fixture
def viewer_url(tmp_path):
    store = TraceStore(tmp_path / "viewer.llmtrace")
    old_call = store.start_call(
        {
            "model": "local",
            "messages": [{"role": "user", "content": "Previous unrelated session."}],
        },
        session_id="previous-session",
    )
    store.finish_call(old_call, '{"answer":"old"}')
    shared_context = (
        "You are inspecting a technical project. Preserve exact facts, identifiers, "
        "measurements, relationships, and prior decisions. " * 8
    )
    first = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": shared_context},
                {"role": "user", "content": "Remember cobalt blue precisely."},
            ],
            "temperature": 0.1,
            "obsolete": True,
        },
        session_id="viewer",
        branch_id="main",
    )
    store.finish_call(first, '{"answer":"remembered"}')
    state = store.get_call(first)["request_state_id"]
    continuation = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": shared_context},
                {"role": "user", "content": "Remember cobalt blue."},
                {"role": "assistant", "content": "Remembered."},
                {"role": "user", "content": "Continue with the next section."},
            ],
            "temperature": 0.2,
        },
        session_id="viewer",
        branch_id="main",
        explicit_parent_state=state,
    )
    store.finish_call(
        continuation,
        "continuing\nnext",
        thoughts="I should preserve the conversation and continue carefully.",
    )
    continuation_state = store.get_call(continuation)["request_state_id"]
    summary = store.start_call(
        {
            "model": "local",
            "prompt": "Summarize the cobalt blue project conversation.",
        },
        session_id="viewer",
        branch_id="summary-side-call",
        purpose="summarize",
        explicit_parent_state=continuation_state,
    )
    store.finish_call(summary, '{"answer":"cobalt blue summary"}')
    store.record_event(
        "rewrite_response",
        {
            "prompt": shared_context * 4,
            "response": "large duplicated response " * 80,
            "attempt": 1,
        },
        session_id="viewer",
    )
    store.record_event(
        "resolved_snapshot",
        {"decisions": {"10": {"kind": "B", "window": 1}}},
        session_id="viewer",
    )

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_four_pane_current_state_and_append_only_updates(page: Page, viewer_url: str):
    page.goto(f"{viewer_url}/")
    expect(page.get_by_label("Session")).to_have_value("viewer")
    expect(page.get_by_label("Session").locator("option")).to_have_count(2)
    expect(page.get_by_role("heading", name="Timeline")).to_be_visible()
    expect(page.get_by_label("Follow")).not_to_be_checked()
    expect(page.get_by_role("heading", name="Mixed trace")).to_be_visible()
    expect(page.get_by_role("heading", name="Exact state")).to_be_visible()
    expect(page.get_by_role("heading", name="Updates")).to_be_visible()
    expect(page.locator(".timeline-item")).not_to_have_count(0)
    expect(page.locator("#waiting-calls")).to_have_text("0 waiting / running")
    page.evaluate(
        """renderWaitingCalls([{
          type: "call",
          id: 3,
          status: "running",
          label: "rewrite",
          branch_id: "main"
        }])"""
    )
    expect(page.locator("#waiting-calls")).to_contain_text("1 waiting / running")
    expect(page.locator("#waiting-calls .waiting-calls-head")).to_have_count(1)
    one_waiting_height = page.locator("#waiting-calls").bounding_box()["height"]
    page.evaluate(
        """renderWaitingCalls([
          {type: "call", id: 3, status: "running"},
          {type: "call", id: 4, status: "running"},
          {type: "call", id: 5, status: "running"}
        ])"""
    )
    expect(page.locator("#waiting-calls")).to_have_text("3 waiting / running")
    assert page.locator("#waiting-calls").bounding_box()["height"] == one_waiting_height
    page.evaluate("renderWaitingCalls(state.timelineItems)")
    expect(page.locator("#waiting-calls")).to_have_text("0 waiting / running")
    assert page.locator("#waiting-calls").bounding_box()["height"] == one_waiting_height
    assert page.evaluate(
        """isCheckpoint({
          diff: {mode: "diff", prompt: {hunks: [{"=": "20 unchanged lines"}]}},
          request_state_id: 12,
          parent_state_id: 10,
          chronological_parent_state_id: 11,
          chronological_similarity: 0.98
        })"""
    ) is False
    assert page.evaluate(
        """isCheckpoint({
          diff: {mode: "diff", prompt: {hunks: [{"=": "20 unchanged lines"}]}},
          request_state_id: 12,
          parent_state_id: 10,
          chronological_parent_state_id: 11,
          chronological_similarity: 0.10
        })"""
    ) is True
    assert page.evaluate(
        """isCheckpoint({
          diff: {mode: "diff", prompt: {hunks: [{"=": "20 unchanged lines"}]}},
          request_state_id: 11,
          parent_state_id: 10,
          chronological_parent_state_id: 11
        })"""
    ) is False
    # A removal and a later insertion are two changes, however close they sit:
    # pairing them would invent a transition and bind both to one identity.
    assert page.evaluate(
        """() => {
          const entries = updateEntries({
            id: 3632,
            diff: {
              prompt: {hunks: [
                {at_old: 30, at_new: 30, "-": {lines: 1, preview: "removed source line"}},
                {"=": "1 unchanged lines"},
                {at_old: 32, at_new: 31, "+": "replacement source line"}
              ]}
            },
            output_diff: {mode: "unchanged"},
            response: ""
          });
          return entries.length === 2
            && entries[0].operation === "-"
            && entries[0].oldText === "removed source line"
            && entries[0].location === "old 30"
            && entries[1].operation === "+"
            && entries[1].newText === "replacement source line"
            && entries[1].location === "new 31"
            && entries[1].promptLine === 31;
        }"""
    ) is True
    # A hunk the differ itself recorded as a replacement still reads as one.
    assert page.evaluate(
        """() => {
          const entries = updateEntries({
            id: 3633,
            diff: {
              prompt: {hunks: [
                {
                  at_old: 12,
                  at_new: 12,
                  "-": {lines: 1, preview: "old source line"},
                  "+": "new source line"
                }
              ]}
            },
            output_diff: {mode: "unchanged"},
            response: ""
          });
          return entries.length === 1
            && entries[0].operation === "~"
            && entries[0].oldText === "old source line"
            && entries[0].newText === "new source line"
            && entries[0].location === "line 12";
        }"""
    ) is True
    assert page.evaluate(
        """() => {
          const saved = {
            detail: state.detail,
            mixedSegmentDetails: state.mixedSegmentDetails,
          };
          const stableDiff = {
            mode: "diff",
            prompt: {
              op: "=",
              hunks: [{"=": "100 unchanged lines"}],
            },
          };
          const prior = {
            id: 9100,
            request: {prompt: "current request"},
            response: "retained historical output",
            diff: stableDiff,
            output_diff: {
              mode: "diff",
              changes: [{
                op: "+",
                old: "",
                new: "retained historical output",
                old_line: 1,
                new_line: 1,
                new_start: 0,
                new_end: 20,
              }],
              base_call_id: 9099,
            },
          };
          const selected = {
            id: 9101,
            request: {prompt: "current request"},
            response: "selected current output",
            diff: stableDiff,
            output_diff: {
              mode: "diff",
              changes: [{
                op: "~",
                old: "previous output",
                new: "selected current output",
                old_line: 1,
                new_line: 1,
                new_start: 0,
                new_end: 23,
              }],
              base_call_id: 9100,
            },
          };
          state.detail = selected;
          state.mixedSegmentDetails = [prior, selected];
          renderMixed();
          const mixedText = document.querySelector("#mixed").textContent;
          const retainedHistoricalRemoved = [
            ...document.querySelectorAll("#mixed .removed-part"),
          ].some(node => node.textContent.includes("retained historical output"));
          const retainedHistoricalPresent = [
            ...document.querySelectorAll("#mixed .added-part"),
          ].some(node => node.textContent.includes("retained historical output"));
          const fragmentTemplate = document.createElement("template");
          fragmentTemplate.innerHTML = updateEntryHtml({
            category: "output",
            fragments: [{
              op: "~",
              old: "old token",
              new: "new token",
              old_line: 1,
              new_line: 1,
            }],
          });
          const removedText = fragmentTemplate.content
            .querySelector(".removed-part").textContent;
          const addedText = fragmentTemplate.content
            .querySelector(".added-part").textContent;
          state.detail = saved.detail;
          state.mixedSegmentDetails = saved.mixedSegmentDetails;
          renderMixed();
          return mixedText.includes("selected current output")
            && mixedText.includes("retained historical output")
            && retainedHistoricalRemoved
            && !retainedHistoricalPresent
            && removedText === "old token"
            && addedText === "new token";
        }"""
    ) is True
    expect(page.locator(".timeline-item .item-label")).to_have_text(
        [
            "→ new state input",
            "← output",
            "→ input",
            "← output",
            "→ new state input",
            "← output",
        ]
    )
    expect(page.locator(".timeline-input.trace-kind-input")).to_have_count(3)
    expect(page.locator(".timeline-output.trace-kind-output")).to_have_count(3)
    assert page.evaluate(
        """() => {
          const input = document.querySelector(".timeline-input");
          const output = document.querySelector(".timeline-output");
          const inputStyle = getComputedStyle(input);
          const outputStyle = getComputedStyle(output);
          const inputMarker = getComputedStyle(input, "::before");
          const outputMarker = getComputedStyle(output, "::before");
          return inputStyle.backgroundColor !== outputStyle.backgroundColor
            && inputMarker.backgroundColor !== outputMarker.backgroundColor;
        }"""
    ) is True
    assert page.evaluate(
        """() => {
          const saved = state.timelineItems;
          renderTimelineEvents([
            {
              type: "call", id: 10, sequence: 10, branch_id: "main",
              branch_root_id: "main", status: "ok",
              created_at: "2026-07-24T12:00:00.000Z", duration_ms: 1000
            },
            {
              type: "call", id: 11, sequence: 11, branch_id: "main~parallel-2",
              branch_root_id: "main", status: "ok",
              created_at: "2026-07-24T12:00:00.200Z", duration_ms: 300
            },
            {
              type: "call", id: 12, sequence: 12, branch_id: "main~parallel-3",
              branch_root_id: "main", status: "running",
              created_at: "2026-07-24T12:00:00.300Z"
            }
          ]);
          const order = [...document.querySelectorAll(".timeline-item")].map(node => (
            `${node.dataset.key || node.dataset.callKey}:${node.dataset.phase}`
          ));
          const dividers = [...document.querySelectorAll(
            ".timeline-parallel-divider"
          )].map(node => node.textContent.trim().replace(/\\s+/g, " "));
          renderTimelineEvents(saved);
          return JSON.stringify(order) === JSON.stringify([
              "call:10:input",
              "call:11:input",
              "call:12:input",
              "call:11:output",
              "call:10:output"
            ])
            && JSON.stringify(dividers) === JSON.stringify([
              "parallel start 3 branches"
            ]);
        }"""
    ) is True
    assert page.evaluate(
        """() => {
          const saved = state.timelineItems;
          const baseTime = Date.parse("2026-07-24T12:00:00.000Z");
          const items = Array.from({length: 40}, (_, index) => ({
            type: "call",
            id: 1000 + index,
            sequence: 1000 + index,
            branch_id: "main",
            branch_root_id: "main",
            status: index === 0 ? "running" : "ok",
            created_at: new Date(baseTime + index * 1000).toISOString(),
            duration_ms: 100,
          }));
          state.timelineItems = items;
          renderTimelineEvents(items);
          const container = document.querySelector("#timeline");
          const target = container.querySelector(
            '.timeline-input[data-key="call:1020"]'
          );
          container.scrollTop = target.offsetTop - 30;
          const before = target.getBoundingClientRect().top
            - container.getBoundingClientRect().top;
          const anchor = captureTimelineViewport();
          items[0] = {...items[0], status: "ok", duration_ms: 100};
          renderTimelineEvents(items);
          restoreTimelineViewport(anchor);
          const restored = container.querySelector(
            '.timeline-input[data-key="call:1020"]'
          );
          const after = restored.getBoundingClientRect().top
            - container.getBoundingClientRect().top;
          state.timelineItems = saved;
          renderTimelineEvents(saved);
          return Math.abs(after - before) < 1;
        }"""
    ) is True
    expect(page.locator(".update-card")).to_have_count(6)
    expect(page.locator(".update-card.checkpoint .checkpoint-state")).to_have_count(4)
    expect(page.locator(".timeline-item.checkpoint-call")).to_have_count(2)
    expect(page.locator('.timeline-item[data-key="call:2"]')).to_have_class(
        re.compile("checkpoint-call")
    )
    expect(page.locator('.timeline-item[data-key="call:3"]')).not_to_have_class(
        re.compile("checkpoint-call")
    )
    expect(
        page.locator('.update-card[data-key="call:4"][data-phase="input"] .checkpoint-input')
    ).to_contain_text("Summarize the cobalt blue project conversation.")
    expect(
        page.locator(
            '.update-card[data-key="call:4"][data-phase="input"] '
            '[data-input-field="prompt"] > .state-field-label'
        )
    ).to_have_text("Prompt")
    expect(
        page.locator(
            '.update-card[data-key="call:4"][data-phase="input"] '
            '[data-input-field="prompt"] > .state-field-content'
        )
    ).not_to_contain_text("prompt:")
    expect(
        page.locator('.update-card[data-key="call:4"][data-phase="output"] .checkpoint-output')
    ).to_contain_text("cobalt blue summary")
    expect(
        page.locator('.update-card[data-key="call:2"][data-phase="input"] .checkpoint-parameters')
    ).to_contain_text("temperature: 0.1")
    summary_checkpoint = page.locator(
        '.update-card[data-key="call:4"][data-phase="input"]'
    )
    summary_checkpoint_output = page.locator(
        '.update-card[data-key="call:4"][data-phase="output"]'
    )
    page.locator(
        '.timeline-input[data-key="call:4"] .item-label',
        has_text="new state input",
    ).click()
    expect(summary_checkpoint.locator(".checkpoint-input")).to_have_class(
        re.compile("timeline-update-focus")
    )
    summary_checkpoint.locator(".checkpoint-input").click()
    expect(page.locator('.timeline-item[data-key="call:4"]')).to_have_class(
        re.compile("active")
    )
    expect(page.locator("#mixed .checkpoint-pane-focus.trace-kind-input")).to_be_visible()
    expect(page.locator("#exact .checkpoint-pane-focus.trace-kind-input")).to_be_visible()
    summary_checkpoint_output.locator(".checkpoint-output").click()
    expect(page.locator("#mixed .checkpoint-pane-focus.trace-kind-output")).to_be_visible()
    expect(page.locator("#exact .checkpoint-pane-focus.trace-kind-output")).to_be_visible()
    summary_checkpoint.locator(".checkpoint-parameters").click()
    expect(page.get_by_role("button", name="Params")).to_have_class(re.compile("active"))
    expect(
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-input-params")
    ).not_to_have_count(0)
    expect(
        page.locator("#exact .checkpoint-pane-focus.trace-kind-input-params")
    ).to_be_visible()
    summary_checkpoint.locator(".checkpoint-input").click()
    expect(page.locator(".mixed-card")).to_have_count(0)
    expect(page.locator("#mixed")).not_to_contain_text('mode: "diff"')
    expect(page.locator("#mixed")).not_to_contain_text('op: "="')
    expect(page.locator("#mixed")).not_to_contain_text("unchanged lines")
    expect(page.locator(".timeline-item.event")).to_have_count(0)
    expect(page.locator("#mixed")).to_contain_text("Summarize")
    expect(page.locator("#mixed")).to_contain_text("cobalt blue summary")
    expect(page.locator("#exact")).to_contain_text("Summarize")
    expect(page.locator("#exact")).to_contain_text("cobalt blue summary")
    expect(page.locator("#mixed-status")).to_contain_text("new current state")
    page.locator('#exact [data-state-scope="output"]').click()
    expect(summary_checkpoint_output.locator(".checkpoint-output")).to_have_class(
        re.compile("active")
    )
    expect(summary_checkpoint_output.locator(".checkpoint-output")).to_have_class(
        re.compile("update-back-focus")
    )

    retained_card = page.locator(".update-card").first
    retained_card.evaluate("(element) => element.dataset.retentionSentinel = 'keep'")
    page.route(
        "**/api/timeline?**",
        lambda route: route.fulfill(json=[]),
        times=1,
    )
    page.evaluate("loadTimeline()")
    expect(page.locator(".update-card")).to_have_count(6)
    expect(retained_card).to_have_attribute("data-retention-sentinel", "keep")
    page.route(
        "**/api/sessions",
        lambda route: route.fulfill(json=[]),
        times=1,
    )
    page.evaluate("loadSessions()")
    expect(page.get_by_label("Session")).to_have_value("viewer")
    expect(page.get_by_label("Session")).to_contain_text("retained in viewer")
    page.evaluate("loadSessions()")
    page.evaluate("loadTimeline()")
    expect(page.locator(".update-card")).to_have_count(6)

    page.locator('.timeline-item[data-key="call:2"]').click()
    expect(
        page.locator('.update-card[data-key="call:2"][data-phase="input"] .checkpoint-input')
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(
        page.locator('.timeline-item[data-key="call:3"]')
    ).to_have_attribute("data-branch-lane", "0")
    expect(
        page.locator('.timeline-item[data-key="call:4"]')
    ).to_have_attribute("data-branch-lane", "0")
    assert page.evaluate(
        """() => {
          const saved = state.timelineItems;
          state.timelineItems = saved.map((item, index) => ({
            ...item,
            branch_id: index === 0 ? "main" : `main~parallel-${index + 1}`,
            created_at: `2026-07-24T12:00:00.${index}00Z`,
            duration_ms: 1000,
          }));
          applyBranchIndentation();
          const lanes = state.timelineItems.map(item => Number(
            document.querySelector(
              `.timeline-item[data-key="${itemKey(item)}"]`
            ).dataset.branchLane
          ));
          state.timelineItems = saved;
          applyBranchIndentation();
          return JSON.stringify(lanes) === JSON.stringify([0, 1, 2]);
        }"""
    ) is True
    assert page.evaluate(
        """() => {
          const saved = state.timelineItems;
          state.timelineItems = [
            {
              type: "call", id: 4491, sequence: 4491, branch_id: "main",
              branch_root_id: "main", status: "cancelled",
              created_at: "2026-07-24T08:49:53.317Z", duration_ms: 1271
            },
            {
              type: "call", id: 4492, sequence: 4492,
              branch_id: "main~parallel-2", branch_root_id: "main",
              status: "cancelled",
              created_at: "2026-07-24T08:49:54.586Z", duration_ms: 675
            },
            {
              type: "call", id: 4493, sequence: 4493, branch_id: "main",
              branch_root_id: "main", status: "cancelled",
              created_at: "2026-07-24T08:49:54.920Z", duration_ms: 393
            },
            {
              type: "call", id: 4494, sequence: 4494, branch_id: "main",
              branch_root_id: "main", status: "cancelled",
              created_at: "2026-07-24T08:49:55.869Z", duration_ms: 361
            }
          ];
          renderTimelineEvents(state.timelineItems);
          const result = state.timelineItems.map(item => {
            const node = document.querySelector(
              `.timeline-input[data-key="${itemKey(item)}"]`
            );
            return [
              Number(node.dataset.branchLane),
              Number(node.dataset.branchDepth),
              node.classList.contains("parallel-block"),
            ];
          });
          const chronological = [...document.querySelectorAll(
            ".timeline-item, .timeline-parallel-divider"
          )].map(node => (
            node.classList.contains("parallel-start") ? "start"
              : node.classList.contains("parallel-end") ? "end"
                : `${node.dataset.key || node.dataset.callKey}:${node.dataset.phase}`
          ));
          state.timelineItems = saved;
          renderTimelineEvents(saved);
          return JSON.stringify(result) === JSON.stringify([
              [0, 1, true],
              [1, 2, true],
              [0, 1, true],
              [0, 0, false],
            ])
            && JSON.stringify(chronological) === JSON.stringify([
              "start",
              "call:4491:input",
              "call:4492:input",
              "call:4491:output",
              "call:4493:input",
              "call:4492:output",
              "call:4493:output",
              "end",
              "call:4494:input",
              "call:4494:output",
            ]);
        }"""
    ) is True
    page.locator('.timeline-item[data-key="call:3"]').click()
    expect(
        page.locator('#mixed [data-update-entry^="3:"]').first
    ).to_be_in_viewport()
    expect(
        page.locator('#exact [data-update-entry^="3:"]').first
    ).to_be_in_viewport()
    expect(
        page.locator(
            '.update-card[data-key="call:3"][data-phase="input"] '
            '.update-jump[data-update-index="0"]'
        )
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(
        page.locator(
            '.update-card[data-key="call:3"][data-phase="input"] '
            '.update-jump[data-update-index="0"]'
        )
    ).to_have_class(re.compile("timeline-update-flash"))
    expect(
        page.locator('.update-card[data-key="call:2"][data-phase="input"] .checkpoint-input')
    ).not_to_have_class(re.compile("timeline-update-focus"))
    expect(
        page.locator('.update-card[data-key="call:3"] .update-jump.active')
    ).to_have_count(0)
    expect(
        page.locator('#exact [data-state-scope="input-params"]')
    ).to_have_class(re.compile("timeline-scope-focus"))
    page.locator('.timeline-item[data-key="call:4"]').click()
    assert page.locator("#mixed").evaluate("(element) => element.scrollTop") == 0
    expect(page.locator("#mixed .checkpoint-pane-focus")).not_to_have_count(0)
    expect(page.locator("#exact .checkpoint-pane-focus")).not_to_have_count(0)
    expect(
        page.locator('.update-card[data-key="call:4"][data-phase="input"] .checkpoint-input')
    ).to_have_class(re.compile("timeline-update-focus"))
    page.locator('.timeline-output[data-call-key="call:4"]').click()
    expect(
        page.locator('.update-card[data-key="call:4"][data-phase="output"] .checkpoint-output')
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-output")
    ).to_be_visible()
    expect(
        page.locator("#exact .checkpoint-pane-focus.trace-kind-output")
    ).to_be_visible()

    page.locator('.timeline-output[data-call-key="call:3"]').click()
    expect(page.locator("#mixed .fragment-focus.input-update")).to_have_count(0)
    expect(page.locator("#exact .exact-focus.input-update")).to_have_count(0)
    expect(page.locator("#mixed .fragment-focus.output-update")).not_to_have_count(0)
    expect(page.locator("#exact .exact-focus.output-update")).not_to_have_count(0)
    expect(
        page.locator('.update-card[data-key="call:3"][data-phase="output"] .output-update-card')
    ).to_have_class(re.compile("timeline-update-focus"))
    assert page.evaluate(
        """() => {
          const detail = {
            ...state.detail,
            thoughts: "",
            thoughts_diff: {
              mode: "unchanged",
              similarity: 1,
              changes: [],
              base_call_id: 2,
            },
            output_diff: {
              mode: "unchanged",
              similarity: 1,
              changes: [],
              base_call_id: 2,
            },
          };
          state.detail = detail;
          state.mixedSegmentDetails = [detail];
          renderMixed();
          renderExact();
          const entryKey = focusTimelineSelection(detail, "output");
          const card = document.querySelector(
            '.update-card[data-key="call:3"]'
          );
          card.querySelector('[data-update-scope="output"]')?.remove();
          card.insertAdjacentHTML(
            "beforeend",
            unchangedOutputNoticeHtml(detail),
          );
          focusTimelineUpdateCard(
            card,
            entryKey,
            "output",
          );
          const unchangedOutput = card.querySelector(
            '[data-update-scope="output"]'
          );
          return entryKey === null
            && document.querySelector(
              '#mixed [data-state-scope="output"]'
            ).classList.contains("timeline-scope-focus")
            && document.querySelector(
              '#mixed [data-state-scope="output"]'
            ).classList.contains("flash")
            && document.querySelector(
              '#exact [data-state-scope="output"]'
            ).classList.contains("timeline-scope-focus")
            && document.querySelector(
              '#exact [data-state-scope="output"]'
            ).classList.contains("flash")
            && getComputedStyle(
              document.querySelector('#exact [data-state-scope="output"]')
            ).animationName === "timeline-scope-flash"
            && !document.querySelector("#mixed .fragment-focus.input-update")
            && !document.querySelector("#exact .exact-focus.input-update")
            && !document.querySelector("#mixed .fragment-focus.output-update")
            && !document.querySelector("#exact .exact-focus.output-update")
            && unchangedOutput.textContent.includes(
              "Same output as call #2"
            )
            && unchangedOutput.classList.contains("timeline-update-focus")
            && unchangedOutput.classList.contains("timeline-update-flash")
            && !card.classList.contains("timeline-update-focus");
        }"""
    ) is True
    page.locator('.timeline-input[data-key="call:3"]').click()
    page.wait_for_timeout(250)
    page.evaluate(
        """() => {
          document.querySelectorAll(
            ".fragment-focus, .exact-focus, .timeline-scope-focus, "
              + ".checkpoint-pane-focus, .timeline-update-focus, "
              + ".timeline-update-flash"
          ).forEach(node => node.classList.remove(
            "fragment-focus",
            "exact-focus",
            "timeline-scope-focus",
            "checkpoint-pane-focus",
            "timeline-update-focus",
            "timeline-update-flash",
          ));
        }"""
    )
    page.evaluate(
        """document.querySelector(
          '.timeline-input[data-key="call:3"]'
        ).click()"""
    )
    expect(page.locator("#mixed .fragment-focus")).not_to_have_count(0)
    expect(
        page.locator("#exact .exact-focus, #exact .timeline-scope-focus")
    ).not_to_have_count(0)
    # A phase click focuses every entry of that phase, not only the first, and
    # each of them pulses once.
    input_updates = page.locator(
        '.update-card[data-key="call:3"][data-phase="input"] .update-jump'
    )
    input_focus = page.locator(
        '.update-card[data-key="call:3"][data-phase="input"] '
        ".update-jump.timeline-update-focus"
    )
    expect(input_focus).not_to_have_count(0)
    expect(input_focus).to_have_count(input_updates.count())
    expect(page.locator("#updates .timeline-update-focus")).to_have_count(
        input_updates.count()
    )
    expect(page.locator("#updates .timeline-update-flash")).to_have_count(
        input_updates.count()
    )
    page.wait_for_timeout(250)
    focused_scrolls = page.evaluate(
        """() => [
          document.querySelector("#timeline").scrollTop,
          document.querySelector("#mixed").scrollTop,
          document.querySelector("#exact").scrollTop,
          document.querySelector("#updates").scrollTop,
        ]"""
    )
    page.evaluate(
        """() => {
          window.__updateFlashStarts = 0;
          window.__preliminaryUpdateScrolls = 0;
          window.__originalKeepFollowedUpdateVisible = keepFollowedUpdateVisible;
          keepFollowedUpdateVisible = () => {
            window.__preliminaryUpdateScrolls += 1;
          };
          document.querySelector("#updates").addEventListener(
            "animationstart",
            event => {
              if (event.animationName === "timeline-update-flash") {
                window.__updateFlashStarts += 1;
              }
            },
            {once: true},
          );
          document.querySelector(
            '.timeline-input[data-key="call:3"]'
          ).click();
        }"""
    )
    page.wait_for_function("window.__updateFlashStarts === 1")
    page.wait_for_timeout(250)
    assert page.evaluate(
        """() => {
          keepFollowedUpdateVisible = window.__originalKeepFollowedUpdateVisible;
          return window.__preliminaryUpdateScrolls;
        }"""
    ) == 0
    assert page.evaluate(
        """() => [
          document.querySelector("#timeline").scrollTop,
          document.querySelector("#mixed").scrollTop,
          document.querySelector("#exact").scrollTop,
          document.querySelector("#updates").scrollTop,
        ]"""
    ) == focused_scrolls

    page.evaluate(
        """() => {
          const originalDetailFor = detailFor;
          let releaseSlowSelection;
          const gate = new Promise(resolve => { releaseSlowSelection = resolve; });
          window.__slowSelectionStarted = false;
          detailFor = async (type, id) => {
            if (Number(id) === 4) {
              window.__slowSelectionStarted = true;
              await gate;
            }
            return originalDetailFor(type, id);
          };
          window.__releaseSlowSelection = releaseSlowSelection;
          window.__slowSelection = selectItem(
            "call",
            4,
            document.querySelector('.timeline-item[data-key="call:4"]')
          ).finally(() => { detailFor = originalDetailFor; });
        }"""
    )
    page.wait_for_function("window.__slowSelectionStarted")
    page.locator('.timeline-item[data-key="call:3"]').click()
    page.wait_for_function("state.detail?.id === 3")
    page.evaluate("window.__releaseSlowSelection()")
    page.wait_for_function(
        "window.__slowSelection",
    )
    page.evaluate("window.__slowSelection")
    assert page.evaluate(
        """() => state.selected.id === 3
          && state.detail.id === 3
          && state.mixedSegmentDetails.at(-1).id === 3"""
    )

    page.get_by_placeholder("Search inputs, outputs, state…").fill("obal")
    page.get_by_role("button", name="Search").click()
    expect(page.locator(".result").first).to_be_visible()
    page.get_by_role("button", name="Hide search results").click()
    expect(page.locator("#search-results")).to_have_class(re.compile("hidden"))
    expect(page.get_by_placeholder("Search inputs, outputs, state…")).to_have_value("obal")
    page.get_by_role("button", name="Search").click()
    page.get_by_placeholder("Search inputs, outputs, state…").fill("")
    expect(page.locator("#search-results")).to_have_class(re.compile("hidden"))

    summary_item = page.locator('.timeline-item[data-key="call:4"]')
    summary_item.click()
    expect(page.locator("#lineage")).to_contain_text("state S")
    expect(page.locator("#exact")).to_contain_text("Summarize")
    expect(page.get_by_role("button", name="I/O")).to_have_class(re.compile("active"))
    expect(page.locator("#exact")).to_contain_text("cobalt blue summary")

    page.get_by_role("button", name="Params").click()
    expect(page.locator("#exact")).to_contain_text('model: "local"')
    expect(page.locator("#exact")).not_to_contain_text("Summarize")

    page.locator('.timeline-item[data-key="call:3"]').click()
    expect(page.get_by_role("button", name="I/O")).to_have_class(re.compile("active"))
    expect(
        page.locator('.update-card[data-key="call:3"][data-phase="input"]')
    ).to_have_class(re.compile("active"))
    expect(
        page.locator('#mixed [data-update-entry^="3:"].fragment-focus')
    ).not_to_have_count(0)
    expect(
        page.locator('#exact [data-update-entry^="3:"].exact-focus')
    ).not_to_have_count(0)
    expect(page.locator("#mixed-status")).to_contain_text("accumulated updates")
    expect(page.locator("#mixed .inline-update")).not_to_have_count(0)
    expect(page.locator("#mixed .inline-update").first).to_have_class(re.compile("flash"))
    expect(page.locator("#mixed")).to_contain_text("Continue with the next section.")
    expect(page.locator("#mixed")).to_contain_text("continuing")
    expect(page.locator("#mixed")).not_to_contain_text("output: |")
    added_user = page.locator(
        '.update-card[data-key="call:3"][data-phase="input"] .update-jump',
        has_text="Added input · user",
    )
    expect(added_user).to_contain_text("Continue with the next section.")
    expect(added_user).to_have_class(re.compile("trace-kind-input"))
    expect(added_user).to_have_class(re.compile("trace-op-added"))
    expect(page.locator("#exact > .state-scope > .state-scope-label")).to_have_text(
        ["Input parameters", "Input", "Thoughts", "Output"]
    )
    expect(
        page.locator('#exact [data-state-scope="thoughts"]')
    ).to_contain_text("I should preserve the conversation")
    expect(
        page.locator('#exact [data-state-scope="output"]')
    ).not_to_contain_text("I should preserve the conversation")
    expect(
        page.locator('.update-card[data-key="call:3"][data-phase="output"] .thoughts-update-card')
    ).to_contain_text("Added thoughts")
    expect(
        page.locator(
            '.update-card[data-key="call:3"][data-phase="output"] .thoughts-update-card .added-part'
        )
    ).to_have_text("I should preserve the conversation and continue carefully.")
    expect(page.locator("#exact .message-list-label")).to_have_text("Messages")
    expect(page.locator("#exact .message-field-label", has_text="Content")).not_to_have_count(0)
    expect(page.locator("#exact .message-field-label", has_text="Role")).not_to_have_count(0)
    expect(page.locator("#exact .message-list")).not_to_contain_text("messages:")
    expect(page.locator("#exact .message-list")).not_to_contain_text("content:")
    expect(page.locator("#exact .message-list")).not_to_contain_text("role:")
    assert page.locator("#exact .state-scope-content").evaluate_all(
        """nodes => nodes.every(node => (
          !/^\\s*(input_params|input|output):/.test(node.textContent)
        ))"""
    )
    page.locator(
        '#exact [data-state-scope="input"] > .state-scope-label'
    ).click()
    expect(page.locator('.timeline-input[data-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    expect(added_user).to_have_class(re.compile("timeline-update-focus"))
    expect(page.locator("#mixed .fragment-focus.input-update")).not_to_have_count(0)
    expect(page.locator("#exact .exact-focus.input-update")).not_to_have_count(0)
    changed_input_state = page.locator(
        "#exact .state-scope-content "
        ".exact-update.input-update.trace-kind-input.trace-op-changed",
        has_text="Remember cobalt blue.",
    )
    expect(changed_input_state).to_be_visible()
    changed_input_state.click()
    expect(changed_input_state).to_have_class(re.compile("exact-focus"))
    expect(changed_input_state).to_have_class(re.compile("flash"))
    expect(
        page.locator(
            "#mixed .state-scope-content "
            ".trace-part.input-update.fragment-focus",
            has_text="Remember cobalt blue.",
        )
    ).not_to_have_count(0)
    expect(
        page.locator(
            '.update-card[data-key="call:3"][data-phase="input"] '
            ".input-update-card.trace-op-changed"
        )
    ).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator('.timeline-input[data-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    page.locator(
        '#mixed [data-state-scope="output"] > .state-scope-label'
    ).click()
    expect(page.locator('.timeline-output[data-call-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    expect(
        page.locator('.update-card[data-key="call:3"][data-phase="output"] .output-update-card')
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(page.locator("#mixed .fragment-focus.output-update")).not_to_have_count(0)
    expect(page.locator("#exact .exact-focus.output-update")).not_to_have_count(0)
    page.locator(
        '#exact [data-state-scope="input-params"] > .state-scope-label'
    ).click()
    expect(
        page.locator("#updates .timeline-update-focus.trace-kind-input-params")
    ).not_to_have_count(0)
    page.evaluate(
        """() => {
          document.querySelectorAll(
            ".fragment-focus, .exact-focus, .timeline-scope-focus, "
              + ".checkpoint-pane-focus, .timeline-update-focus, "
              + ".timeline-update-flash"
          ).forEach(node => node.classList.remove(
            "fragment-focus",
            "exact-focus",
            "timeline-scope-focus",
            "checkpoint-pane-focus",
            "timeline-update-focus",
            "timeline-update-flash",
          ));
          document.querySelector(
            '#exact [data-state-scope="input"] > .state-scope-content'
          ).dispatchEvent(new MouseEvent("click", {bubbles: true}));
        }"""
    )
    expect(page.locator("#mixed .fragment-focus")).to_have_count(0)
    expect(page.locator("#exact .exact-focus")).to_have_count(0)
    expect(page.locator("#updates .timeline-update-focus")).to_have_count(0)
    page.locator("#exact .state-scope.trace-kind-input").select_text()
    assert "Continue with the next section." in page.evaluate(
        "window.getSelection().toString()"
    )
    page.evaluate("window.getSelection().removeAllRanges()")
    page.locator("#mixed .state-scope.trace-kind-output").select_text()
    assert "continuing" in page.evaluate("window.getSelection().toString()")
    page.evaluate("window.getSelection().removeAllRanges()")
    added_user.select_text()
    assert "Continue with the next section." in page.evaluate(
        "window.getSelection().toString()"
    )
    expect(added_user).not_to_have_class(re.compile(r"\bactive\b"))
    page.evaluate("window.getSelection().removeAllRanges()")
    page.locator(
        '#mixed [data-update-entry]',
        has_text="Continue with the next section.",
    ).click()
    expect(added_user).to_have_class(re.compile("active"))
    expect(added_user).to_have_class(re.compile("update-back-focus"))
    page.locator(
        '#exact [data-update-entry]',
        has_text="Continue with the next section.",
    ).click()
    expect(added_user).to_have_class(re.compile("active"))
    changed_temperature = page.locator(
        '.update-card[data-key="call:3"][data-phase="input"] .parameter-update-card',
        has_text="Changed parameter · temperature",
    )
    expect(changed_temperature).to_contain_text("0.1 → 0.2")
    expect(changed_temperature).to_have_class(re.compile("trace-kind-input-params"))
    expect(changed_temperature).to_have_class(re.compile("trace-op-changed"))
    removed_parameter = page.locator(
        '.update-card[data-key="call:3"][data-phase="input"] .parameter-update-card',
        has_text="Removed parameter · obsolete",
    )
    expect(removed_parameter.locator(".removed-part")).to_have_text("true")
    expect(removed_parameter).to_have_class(re.compile("trace-op-removed"))
    expect(page.locator("#mixed .parameter-update")).not_to_have_count(0)
    expect(
        page.locator("#mixed .removed-part.parameter-update", has_text="obsolete: true")
    ).to_be_visible()
    expect(
        page.locator("#mixed .removed-part.parameter-update", has_text="0.1")
    ).to_be_visible()
    expect(
        page.locator("#mixed .added-part.parameter-update", has_text="0.2")
    ).to_be_visible()
    expect(page.locator("#exact")).not_to_contain_text("obsolete")
    expect(page.locator("#exact")).not_to_contain_text("0.1")
    expect(page.locator("#exact > .state-scope > .state-scope-label")).to_have_text(
        ["Input parameters", "Input", "Thoughts", "Output"]
    )
    expect(
        page.locator("#exact > .state-scope.trace-kind-input-params")
    ).to_contain_text("temperature: 0.2")
    expect(
        page.locator('.update-card[data-key="call:3"][data-phase="input"]')
    ).not_to_contain_text("New current state")
    output_fragment = page.locator(
        '.update-card[data-key="call:3"][data-phase="output"] .output-update-card .fragment-change',
        has=page.locator(".added-part"),
    ).first
    expect(
        output_fragment.locator("xpath=ancestor::*[contains(@class,'output-update-card')]")
    ).to_have_class(re.compile("trace-kind-output"))
    page.locator("#mixed [data-output-fragment]").first.click()
    expect(output_fragment).to_have_class(re.compile("active"))
    expect(output_fragment).to_have_class(re.compile("update-back-focus"))
    page.locator("#exact [data-output-fragment]").first.click()
    expect(output_fragment).to_have_class(re.compile("active"))
    output_fragment.click()
    expect(output_fragment).to_have_class(re.compile("active"))
    expect(page.locator("#mixed .fragment-focus")).not_to_have_count(0)
    expect(page.locator("#exact .exact-focus.output-update")).not_to_have_count(0)
    changed_temperature.click()
    expect(page.locator("#exact .parameter-update")).to_have_text("0.2")
    page.screenshot(path="/tmp/insequent_updates.png", full_page=True)
    added_user.click()
    expect(page.locator('.timeline-item[data-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    expect(page.locator("#mixed .fragment-focus.input-update")).not_to_have_count(0)
    expect(page.get_by_role("button", name="I/O")).to_have_class(re.compile("active"))
    # A message change is recorded at message granularity, so the mark covers
    # the whole message the hunk names, not a line found by searching for text.
    expect(page.locator("#exact .exact-focus")).to_contain_text(
        "Continue with the next section."
    )
    expect(page.locator("#exact .exact-focus")).not_to_contain_text('"')

    page.evaluate(
        """() => {
          state.followedUpdateKey = "call:3";
          state.followedUpdateTimer = window.setTimeout(() => {}, 5000);
        }"""
    )
    page.locator("#updates").hover()
    page.mouse.wheel(0, 20)
    page.wait_for_function("state.followedUpdateKey === null")

    page.evaluate(
        """() => {
          const saved = {
            fetchJson,
            selectItem,
            items: state.timelineItems,
            signature: state.timelineSignature,
            lastKey: state.lastTimelineKey,
            selected: state.selected,
            liveBusy: state.liveBusy,
          };
          const appended = {type: "call", id: 999999, status: "ok"};
          const records = [...state.timelineItems, appended];
          state.selected = state.timelineItems[state.timelineItems.length - 1];
          state.followNewItems = true;
          document.querySelector("#follow-new-items").checked = true;
          state.liveBusy = true;
          document.querySelector("#updates").scrollTop = 0;
          fetchJson = async url => url.startsWith("/api/timeline?")
            ? records
            : saved.fetchJson(url);
          selectItem = async () => new Promise(resolve => {
            window.__releaseDelayedSelection = resolve;
          });
          window.__delayedAppendDone = false;
          void loadTimeline().finally(() => {
            fetchJson = saved.fetchJson;
            selectItem = saved.selectItem;
            state.timelineItems = saved.items;
            state.timelineSignature = saved.signature;
            state.lastTimelineKey = saved.lastKey;
            state.selected = saved.selected;
            state.followNewItems = false;
            document.querySelector("#follow-new-items").checked = false;
            state.liveBusy = saved.liveBusy;
                document.querySelector('.timeline-input[data-key="call:999999"]')?.remove();
                document.querySelector('.timeline-output[data-call-key="call:999999"]')?.remove();
            document.querySelectorAll(
              '.update-card[data-key="call:999999"]'
            ).forEach(node => node.remove());
            window.__delayedAppendDone = true;
          });
        }"""
    )
    page.wait_for_function("window.__releaseDelayedSelection != null")
    delayed_user_scroll = page.locator("#updates").evaluate(
        """element => {
          element.scrollTop = Math.min(180, element.scrollHeight - element.clientHeight);
          return element.scrollTop;
        }"""
    )
    assert delayed_user_scroll > 0
    page.evaluate("window.__releaseDelayedSelection()")
    page.wait_for_function("window.__delayedAppendDone === true")
    assert page.locator("#updates").evaluate(
        "element => element.scrollTop"
    ) == delayed_user_scroll

    first_card = page.locator(".update-card").first
    first_card.evaluate("(element) => element.dataset.liveSentinel = 'preserve-me'")
    page.locator("#updates").evaluate("(element) => { element.scrollTop = 180; }")
    scroll_before = page.locator("#updates").evaluate("(element) => element.scrollTop")
    before = page.locator(".timeline-item").count()
    selected_before_live = page.evaluate("itemKey(state.selected)")
    response = requests.post(
        f"{viewer_url}/v1/completions",
        headers={
            "X-LLMTrace-Session": "viewer",
            "X-LLMTrace-Branch": "main",
        },
        json={"model": "local", "prompt": "live update probe", "stream": False},
        timeout=10,
    )
    assert response.status_code == 502
    expect(page.locator(".timeline-item")).to_have_count(before + 2, timeout=4000)
    expect(page.locator(".timeline-input").last).to_contain_text("input")
    expect(page.locator(".timeline-output").last).to_contain_text("← output")
    expect(page.locator(".update-card")).to_have_count(8)
    assert page.evaluate("itemKey(state.selected)") == selected_before_live
    expect(first_card).to_have_attribute("data-live-sentinel", "preserve-me")
    scroll_after = page.locator("#updates").evaluate("(element) => element.scrollTop")
    if scroll_before:
        assert abs(scroll_after - scroll_before) <= 2
    else:
        assert scroll_after >= 0

    page.screenshot(path="/tmp/insequent_viewer.png", full_page=True)

    page.evaluate(
        """async () => {
          const regression = {
            id: 898,
            request: {
              model: "local",
              temperature: 0.2,
              prompt: "retained prompt line one\\nretained prompt line two\\nchanged prompt line"
            },
            response: "same response",
            diff: {
              mode: "diff",
              prompt: {
                op: "~",
                hunks: [
                  {"=": "2 unchanged lines"},
                  {"at_old": 3, "at_new": 3, "-": {"lines": 1, "preview": "old prompt line"}, "+": "changed prompt line"}
                ]
              },
              parameters: {}
            },
            output_diff: {mode: "unchanged", base_call_id: 896, changes: []},
            output_parent_call_id: 896
          };
          state.detail = regression;
          renderMixed();
          renderExact();

          const identical = {
            ...regression,
            id: 896,
            output_diff: {mode: "unchanged", base_call_id: 895, changes: []},
            output_parent_call_id: 895
          };
          state.details.set("call:895", Promise.resolve({...regression, id: 895}));
          const card = document.createElement("article");
          card.className = "update-card loading";
          card.dataset.key = "call:896";
          card.dataset.type = "call";
          card.dataset.id = "896";
          document.querySelector("#updates").appendChild(card);
          state.details.set("call:896", Promise.resolve(identical));
          await loadUpdateCard(card);
        }"""
    )
    expect(page.locator("#mixed")).to_contain_text("retained prompt line one")
    expect(page.locator("#mixed")).to_contain_text("retained prompt line two")
    expect(page.locator("#mixed")).to_contain_text("changed prompt line")
    expect(page.locator("#exact")).to_contain_text("retained prompt line one")
    expect(page.locator("#exact > .state-scope > .state-scope-label")).to_have_text(
        ["Input parameters", "Input", "Output"]
    )
    expect(
        page.locator('#mixed [data-input-field="prompt"] > .state-field-label')
    ).to_have_text("Prompt")
    expect(
        page.locator('#exact [data-input-field="prompt"] > .state-field-label')
    ).to_have_text("Prompt")
    expect(
        page.locator('#exact [data-input-field="prompt"] > .state-field-content')
    ).to_contain_text("retained prompt line one")
    assert not page.locator(
        '#exact [data-input-field="prompt"] > .state-field-content'
    ).text_content().lstrip().startswith("prompt:")
    expect(
        page.locator(
            '#mixed [data-state-scope="input-params"] .state-scope-content'
        )
    ).to_contain_text('model: "local"')
    expect(page.locator("#mixed .state-scope.trace-kind-input")).to_be_visible()
    expect(page.locator("#mixed .state-scope.trace-kind-output")).to_be_visible()
    expect(page.locator("#exact .state-scope.trace-kind-input")).to_be_visible()
    expect(page.locator("#exact .state-scope.trace-kind-output")).to_be_visible()
    expect(page.locator('.update-card[data-key="call:896"]')).to_contain_text(
        "Identical call"
    )
    expect(page.locator('.update-card[data-key="call:896"]')).to_contain_text(
        "call #896 = call #895"
    )
    page.evaluate(
        """() => {
          const checkpoint = {
            id: 900,
            request: {
              model: "local",
              messages: [{role: "user", content: "base message"}]
            },
            response: "stable output",
            diff: {mode: "snapshot"},
            output_diff: {mode: "snapshot", changes: []}
          };
          const firstDelta = {
            id: 901,
            request: {
              model: "local",
              messages: [
                {role: "user", content: "base message"},
                {role: "user", content: "first accumulated addition"}
              ]
            },
            response: "stable output",
            diff: {
              mode: "diff",
              messages: [
                {op: "=", old: [0, 1], new: [0, 1]},
                {
                  op: "+",
                  old: [1, 1],
                  new: [1, 2],
                  new_messages: [{role: "user", content: "first accumulated addition"}]
                }
              ],
              parameters: {}
            },
            output_diff: {mode: "unchanged", changes: []}
          };
          const secondDelta = {
            id: 902,
            request: {
              model: "local",
              messages: [
                {role: "user", content: "base message"},
                {role: "user", content: "second accumulated addition"}
              ]
            },
            response: "stable output",
            diff: {
              mode: "diff",
              messages: [
                {op: "=", old: [0, 1], new: [0, 1]},
                {
                  op: "~",
                  old: [1, 2],
                  new: [1, 2],
                  old_messages: [{role: "user", content: "first accumulated addition"}],
                  new_messages: [{role: "user", content: "second accumulated addition"}]
                }
              ],
              parameters: {}
            },
            output_diff: {mode: "unchanged", changes: []}
          };
          state.detail = secondDelta;
          state.mixedSegmentDetails = [checkpoint, firstDelta, secondDelta];
          renderMixed();
        }"""
    )
    # 901 added this text; 902 removed it. Mixed shows it as removed history
    # owned by the removing call (902), so a click lands on a removed side — not
    # the call that added it. It is still present (grow-never-lose), just red.
    expect(
        page.locator('#mixed [data-update-entry="902:0"].removed-part')
    ).to_contain_text("first accumulated addition")
    expect(page.locator("#mixed")).to_contain_text("first accumulated addition")
    expect(
        page.locator('#mixed [data-update-entry="902:0"].added-part')
    ).to_contain_text("second accumulated addition")
    expect(page.locator("#mixed-status")).to_contain_text("2 accumulated updates")
    page.evaluate(
        """() => {
          const checkpoint = {
            id: 903,
            request: {
              model: "local",
              messages: [{role: "user", content: "replacement checkpoint"}]
            },
            response: "replacement output",
            diff: {mode: "snapshot"},
            output_diff: {mode: "snapshot", changes: []}
          };
          state.detail = checkpoint;
          state.mixedSegmentDetails = [checkpoint];
          renderMixed();
        }"""
    )
    expect(page.locator("#mixed")).to_contain_text("replacement checkpoint")
    expect(page.locator("#mixed")).not_to_contain_text("first accumulated addition")
    expect(page.locator("#mixed-status")).to_contain_text("new current state")
    page.evaluate(
        """async () => {
          const card = document.createElement("article");
          card.className = "update-card loading";
          card.dataset.key = "call:1278";
          card.dataset.type = "call";
          card.dataset.id = "1278";
          card.innerHTML = '<div class="update-card-head">LLM call #1278</div>';
          document.querySelector("#updates").appendChild(card);
          await loadUpdateCard(card);
        }"""
    )
    unavailable = page.locator('.update-card[data-key="call:1278"]')
    expect(unavailable).to_have_class(re.compile("load-error"))
    expect(unavailable).to_contain_text("Call unavailable")
    expect(unavailable).to_contain_text("no longer present in trace storage")
    expect(unavailable).not_to_have_attribute("data-loaded", "true")
    expect(unavailable).not_to_have_class(re.compile(r"\bloading\b"))
    unavailable.scroll_into_view_if_needed()
    page.screenshot(path="/tmp/insequent_regression_898.png", full_page=True)


@pytest.fixture
def parallel_viewer_url(tmp_path):
    """A branch-root call plus two parallel lanes that share its exact input.

    The lanes differ only in their output, so their input event has no input
    update of its own to focus. The later lane answers first, so the history
    interleaves: both requests leave before either response lands.
    """
    store = TraceStore(tmp_path / "parallel.llmtrace")
    shared_request = {
        "model": "local",
        "messages": [
            {"role": "user", "content": "Outline the mining report."},
            {"role": "user", "content": "Expand every section."},
        ],
    }
    root = store.start_call(
        dict(shared_request), session_id="parallel", branch_id="main"
    )
    store.finish_call(root, "root outline", metadata={"duration_ms": 10})
    # Separate the root from the lanes in wall-clock time, then let the lanes
    # overlap: both requests leave together and the second one answers first.
    time.sleep(0.05)
    lane_one = store.start_call(
        dict(shared_request), session_id="parallel", branch_id="main"
    )
    lane_two = store.start_call(
        dict(shared_request), session_id="parallel", branch_id="main"
    )
    store.finish_call(lane_two, "lane two expansion", metadata={"duration_ms": 100})
    store.finish_call(lane_one, "lane one expansion", metadata={"duration_ms": 400})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", root, lane_one, lane_two
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_input_click_never_focuses_output_update_when_input_is_unchanged(
    page: Page, parallel_viewer_url
):
    url, root, lane_one, lane_two = parallel_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".timeline-item")).not_to_have_count(0)

    card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="input"]'
    )
    output_card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="output"]'
    )
    page.locator(f'.timeline-input[data-key="call:{lane_two}"]').click()
    lane_input = card.locator('[data-update-scope="input"]')
    # A parallel lane forks with no parent state, so its baseline is the
    # concurrent sibling, not an ancestor it could have changed from.
    expect(lane_input).to_contain_text("Parallel lane input")
    expect(lane_input).to_contain_text("No previous state to compare")
    expect(lane_input).not_to_contain_text("Unchanged input")
    # With no previous state there is nothing to point at: naming a call would
    # contradict the absence the row reports.
    expect(lane_input).not_to_contain_text("#")
    expect(lane_input).to_have_class(re.compile("timeline-update-focus"))
    expect(lane_input).to_have_class(re.compile("timeline-update-flash"))
    # Nothing it sent is a change, so the card shows the request itself.
    expect(card.locator('.lane-snapshot-section[data-lane-scope="input"]')).to_contain_text(
        "Expand every section."
    )
    expect(
        card.locator('.lane-snapshot-section[data-lane-scope="input-params"]')
    ).to_contain_text('model: "local"')
    # The call's only update is an output one, on its own card; the input event
    # must not borrow it, directly or by focusing a whole card.
    expect(output_card.locator(".output-update-card")).not_to_have_class(
        re.compile("timeline-update-focus")
    )
    expect(card).not_to_have_class(re.compile("timeline-update-focus"))

    page.locator(f'.timeline-output[data-call-key="call:{lane_two}"]').click()
    expect(output_card.locator(".output-update-card")).to_have_class(
        re.compile("timeline-update-focus")
    )
    expect(lane_input).not_to_have_class(re.compile("timeline-update-focus"))

    # A call that does continue its own branch keeps the unchanged wording.
    sequential_card = page.locator(
        f'.update-card[data-key="call:{lane_one}"][data-phase="input"]'
    )
    page.locator(f'.timeline-input[data-key="call:{lane_one}"]').click()
    sequential_input = sequential_card.locator('[data-update-scope="input"]')
    expect(sequential_input).to_contain_text("Unchanged input")
    expect(sequential_input).to_contain_text(f"Same input as call #{root}")
    expect(sequential_input).to_have_class(re.compile("timeline-update-focus"))


def test_updates_separate_input_and_output_in_timeline_order(
    page: Page, parallel_viewer_url
):
    url, root, lane_one, lane_two = parallel_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".update-card")).to_have_count(6)

    timeline_order = page.evaluate(
        """() => [...document.querySelectorAll("#timeline .timeline-item")].map(
          node => `${node.dataset.key || node.dataset.callKey}:${node.dataset.phase}`
        )"""
    )
    updates_order = page.evaluate(
        """() => [...document.querySelectorAll("#updates .update-card")].map(
          node => `${node.dataset.key}:${node.dataset.phase}`
        )"""
    )
    # Both requests leave before either response lands, and the later lane
    # answers first, so the sequence interleaves rather than pairing per call.
    assert updates_order == [
        f"call:{root}:input",
        f"call:{root}:output",
        f"call:{lane_one}:input",
        f"call:{lane_two}:input",
        f"call:{lane_two}:output",
        f"call:{lane_one}:output",
    ]
    assert updates_order == timeline_order

    input_card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="input"]'
    )
    output_card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="output"]'
    )
    expect(input_card).to_contain_text("→ input")
    expect(output_card).to_contain_text("← output")
    # Neither phase shows the other's updates.
    expect(input_card.locator(".output-update-card")).to_have_count(0)
    expect(output_card.locator(".output-update-card")).to_have_count(1)
    expect(output_card.locator('[data-update-scope="input"]')).to_have_count(0)


def test_update_card_click_activates_its_timeline_event(page: Page, parallel_viewer_url):
    url, root, lane_one, lane_two = parallel_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".update-card")).to_have_count(6)

    # A card whose body is only a scope notice still navigates: it has no
    # update entry to click, but it is the input moment of its call.
    lane_card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="input"]'
    )
    lane_card.locator(".update-unchanged-input").click()
    expect(
        page.locator(f'.timeline-input[data-key="call:{lane_two}"]')
    ).to_have_class(re.compile("active"))
    expect(lane_card).to_have_class(re.compile("active"))
    expect(lane_card.locator(".update-unchanged-input")).to_have_class(
        re.compile("timeline-update-focus")
    )

    # The head is part of the same control.
    output_card = page.locator(
        f'.update-card[data-key="call:{root}"][data-phase="output"]'
    )
    output_card.locator(".update-card-head").click()
    expect(
        page.locator(f'.timeline-output[data-call-key="call:{root}"]')
    ).to_have_class(re.compile("active"))
    expect(output_card).to_have_class(re.compile("active"))
    expect(
        page.locator(f'.timeline-input[data-key="call:{lane_two}"]')
    ).not_to_have_class(re.compile("active"))

    # Keyboard reaches the same target.
    lane_card.locator(".update-unchanged-input").press("Enter")
    expect(
        page.locator(f'.timeline-input[data-key="call:{lane_two}"]')
    ).to_have_class(re.compile("active"))

    # An update entry activates its own phase's event, not the call's input:
    # only input events carry data-key, so the phase must be handed over.
    output_card = page.locator(
        f'.update-card[data-key="call:{lane_two}"][data-phase="output"]'
    )
    output_card.locator(".update-jump").first.click()
    expect(
        page.locator(f'.timeline-output[data-call-key="call:{lane_two}"]')
    ).to_have_class(re.compile("active"))
    expect(
        page.locator(f'.timeline-input[data-key="call:{lane_two}"]')
    ).not_to_have_class(re.compile("active"))
    assert page.evaluate("state.selectedPhase") == "output"



@pytest.fixture
def live_viewer_url(tmp_path):
    """A timeline long enough to scroll, with the store kept open for appends."""
    store = TraceStore(tmp_path / "live.llmtrace")
    messages = [{"role": "user", "content": "Step 0"}]
    for step in range(1, 15):
        call = store.start_call(
            {"model": "local", "messages": list(messages)},
            session_id="live",
            branch_id="main",
        )
        store.finish_call(call, f"answer {step}", metadata={"duration_ms": 5})
        messages = messages + [
            {"role": "assistant", "content": f"answer {step}"},
            {"role": "user", "content": f"Step {step}"},
        ]

    def append(step):
        call = store.start_call(
            {"model": "local", "messages": list(messages) + [
                {"role": "user", "content": f"Appended {step}"},
            ]},
            session_id="live",
            branch_id="main",
        )
        store.finish_call(call, f"appended answer {step}", metadata={"duration_ms": 5})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", append
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_timeline_follows_new_events_only_when_follow_is_enabled(
    page: Page, live_viewer_url
):
    url, append_call = live_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".timeline-item")).to_have_count(28)
    expect(page.get_by_label("Follow")).not_to_be_checked()

    # Sitting at the newest end is not a request to be dragged along by it.
    page.locator("#timeline").evaluate(
        "element => { element.scrollTop = element.scrollHeight; }"
    )
    page.wait_for_timeout(100)
    resting_scroll = page.locator("#timeline").evaluate("element => element.scrollTop")
    assert resting_scroll > 0

    append_call(1)
    page.evaluate("loadTimeline()")
    expect(page.locator(".timeline-item")).to_have_count(30)
    page.wait_for_timeout(300)
    assert page.locator("#timeline").evaluate("element => element.scrollTop") == (
        resting_scroll
    )
    assert page.locator("#timeline").evaluate(
        "element => element.scrollHeight - element.scrollTop - element.clientHeight"
    ) > 80

    # With Follow on, the newest event is what the pane is for.
    page.get_by_label("Follow").check()
    append_call(2)
    page.evaluate("loadTimeline()")
    expect(page.locator(".timeline-item")).to_have_count(32)
    page.wait_for_timeout(300)
    assert page.locator("#timeline").evaluate(
        "element => element.scrollHeight - element.scrollTop - element.clientHeight"
    ) < 80


@pytest.fixture
def repeated_prompt_viewer_url(tmp_path):
    """A prompt that gains the same line twice, in two different places.

    Identical text at different positions is exactly what text matching cannot
    tell apart, so each entry must be placed by its recorded line.
    """
    store = TraceStore(tmp_path / "prompt.llmtrace")
    repeated = "[L000322] The section was prepared under regulation 87."
    old_lines = [f"[L{index:06d}] Source paragraph {index}." for index in range(1, 40)]
    old_lines[20] = repeated
    first = store.start_call(
        {"model": "local", "prompt": "\n".join(old_lines)},
        session_id="prompt",
        branch_id="main",
    )
    store.finish_call(first, "first answer", metadata={"duration_ms": 5})
    new_lines = list(old_lines)
    new_lines[4] = repeated          # replaces a line: one recorded replacement
    new_lines.insert(30, repeated)   # a second, separate insertion
    second = store.start_call(
        {"model": "local", "prompt": "\n".join(new_lines)},
        session_id="prompt",
        branch_id="main",
    )
    store.finish_call(second, "second answer", metadata={"duration_ms": 5})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", second, repeated
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_repeated_prompt_lines_are_placed_by_recorded_position(
    page: Page, repeated_prompt_viewer_url
):
    url, call_id, repeated = repeated_prompt_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{call_id}"]').click()
    card = page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="input"]')

    # Every prompt entry says where it is, so identical text is distinguishable.
    entries = card.locator(".update-jump")
    expect(entries).not_to_have_count(0)
    locations = card.locator(".update-jump .fragment-location")
    assert locations.count() == entries.count()
    assert len(set(locations.all_inner_texts())) == entries.count()

    repeating = [
        index for index in range(entries.count())
        if repeated in entries.nth(index).inner_text()
    ]
    assert len(repeating) >= 2

    # Each of them focuses its own fragment; none is left unfocusable, and none
    # borrows another entry's text.
    seen = set()
    for index in repeating:
        entries.nth(index).click()
        page.wait_for_timeout(200)
        focused = page.evaluate(
            """() => [...document.querySelectorAll('#mixed .fragment-focus')]
                 .map(node => node.dataset.updateEntry)"""
        )
        # A recorded replacement legitimately lights both of its halves, but
        # never a second entry's text, and never nothing at all.
        keys = set(focused)
        assert len(keys) == 1, focused
        key = keys.pop()
        assert key not in seen
        seen.add(key)


@pytest.fixture
def positional_viewer_url(tmp_path):
    """Identical payload text at several recorded positions.

    Repeated messages, repeated response fragments, and a later call that shifts
    everything an earlier call added.
    """
    store = TraceStore(tmp_path / "positions.llmtrace")
    same = "IDENTICAL PARAGRAPH"
    base = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first ask"},
    ]
    first = store.start_call(
        {"model": "local", "messages": list(base)},
        session_id="pos",
        branch_id="main",
    )
    store.finish_call(first, "line a\nline b", metadata={"duration_ms": 5})

    # The same message added twice, at index 1 and index 3; and the response
    # gains the same line twice, at two different offsets.
    repeated = [
        base[0],
        {"role": "user", "content": same},
        base[1],
        {"role": "user", "content": same},
    ]
    second = store.start_call(
        {"model": "local", "messages": list(repeated)},
        session_id="pos",
        branch_id="main",
    )
    store.finish_call(
        second,
        "line a\nREPEATED CLAUSE\nline b\nREPEATED CLAUSE",
        metadata={"duration_ms": 5},
    )

    # A wedge inserted at index 1 shifts every position the second call recorded.
    wedged = [
        base[0],
        {"role": "user", "content": "WEDGE"},
        repeated[1],
        base[1],
        repeated[3],
    ]
    third = store.start_call(
        {"model": "local", "messages": list(wedged)},
        session_id="pos",
        branch_id="main",
    )
    store.finish_call(third, "line a\nline b\nline c", metadata={"duration_ms": 5})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", first, second, third
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def marked_entries(page, pane):
    return page.evaluate(
        """(id) => [...document.querySelectorAll('#' + id + ' [data-update-entry]')].map(
             node => node.dataset.updateEntry
               + (node.dataset.outputFragment ? '/f' + node.dataset.outputFragment : ''))""",
        pane,
    )


def test_repeated_payload_is_placed_by_recorded_position(
    page: Page, positional_viewer_url
):
    url, _first, second, third = positional_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".timeline-item")).to_have_count(6)
    page.locator(f'.timeline-input[data-key="call:{second}"]').click()
    page.wait_for_timeout(400)

    # Two identical messages were added at two recorded indexes, and the same
    # response line at two recorded offsets: each keeps its own mark.
    assert page.evaluate(
        f"""async () => {{
          const entries = updateEntries(await detailFor('call', {second}));
          const messages = entries.filter(entry => entry.messageIndex != null);
          return messages.length === 2
            && messages[0].messageIndex === 1
            && messages[1].messageIndex === 3;
        }}"""
    ) is True
    for pane in ("exact", "mixed"):
        marks = marked_entries(page, pane)
        assert marks.count(f"{second}:0") == 1, (pane, marks)
        assert marks.count(f"{second}:1") == 1, (pane, marks)
        assert f"{second}:2/f0" in marks and f"{second}:2/f1" in marks, (pane, marks)

    # Each message entry marks its own message, not the first matching text.
    order = page.evaluate(
        """() => [...document.querySelectorAll('#exact [data-update-entry]')]
             .map(node => node.dataset.updateEntry)"""
    )
    assert order.index(f"{second}:0") < order.index(f"{second}:1")


def test_earlier_call_history_survives_shifted_positions(
    page: Page, positional_viewer_url
):
    url, _first, second, third = positional_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".timeline-item")).to_have_count(6)
    page.locator(f'.timeline-input[data-key="call:{third}"]').click()
    page.wait_for_timeout(400)

    # The wedge moved every index the second call recorded, so its entries are
    # resolved by content in Mixed — and still both of them, not one collapsed.
    mixed = marked_entries(page, "mixed")
    assert f"{third}:0" in mixed
    assert mixed.count(f"{second}:0") == 1, mixed
    assert mixed.count(f"{second}:1") == 1, mixed

    # The later call's own addition is marked as its own, never attributed to
    # whatever now occupies the earlier call's recorded index.
    assert page.evaluate(
        f"""() => {{
          const node = document.querySelector('#exact [data-update-entry="{third}:0"]');
          return node ? node.textContent.includes("WEDGE") : false;
        }}"""
    ) is True
    assert page.evaluate(
        f"""() => [...document.querySelectorAll('#mixed [data-update-entry="{second}:0"]')]
             .every(node => node.textContent.includes("IDENTICAL PARAGRAPH"))"""
    ) is True


@pytest.fixture
def swallowing_segment_viewer_url(tmp_path):
    """A segment where an earlier call's retained span covers the whole output.

    The later call's fragments sit inside that span, so a flat text cannot mark
    both — and the selected call's own change is the one that must win.
    """
    store = TraceStore(tmp_path / "swallow.llmtrace")
    whole = "if line is meaningful and body contains substantive content, keep it"

    def call(prompt, response):
        made = store.start_call(
            {"model": "local", "prompt": prompt}, session_id="swallow", branch_id="main"
        )
        store.finish_call(made, response, metadata={"duration_ms": 5})
        return made

    # A checkpoint opens the segment. A parallel lane then leaves the next call
    # without an output comparison, so that call records its whole output as
    # added — and the last call changes fragments inside that very text.
    call("root prompt\nline", "root output")
    lane_one = store.start_call(
        {"model": "local", "prompt": "root prompt\nline two"},
        session_id="swallow",
        branch_id="main",
    )
    lane_two = store.start_call(
        {"model": "local", "prompt": "root prompt\nline three"},
        session_id="swallow",
        branch_id="main",
    )
    store.finish_call(lane_one, "lane one output", metadata={"duration_ms": 5})
    store.finish_call(lane_two, whole, metadata={"duration_ms": 5})
    second = call("root prompt\nline four", whole)
    third = call("root prompt\nline five", whole.replace("if line is ", "when "))

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", second, third
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_mixed_never_loses_a_change_to_an_overlapping_span(
    page: Page, swallowing_segment_viewer_url
):
    url, _second, third = swallowing_segment_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-output[data-call-key="call:{third}"]').click()
    page.wait_for_timeout(300)

    card = page.locator(f'.update-card[data-key="call:{third}"][data-phase="output"]')
    fragments = card.locator(".fragment-change")
    count = fragments.count()
    assert count > 0

    # The situation this guards against must actually be present: an earlier
    # call in the segment claims the whole output text.
    assert page.evaluate(
        f"""() => {{
          const parts = stateDisplayParts(state.detail);
          const earlier = state.mixedSegmentDetails
            .slice(0, -1)
            .filter(detail => !isCheckpoint(detail))
            .flatMap(detail => updateEntries(detail))
            .filter(entry => entry.scope === "output" && entry.wholeScope);
          return earlier.some(entry => {{
            const range = entryRanges(parts.outputText, entry, parts.outputAnchors)[0];
            return range && range[1] - range[0] > parts.outputText.length / 2;
          }});
        }}"""
    ) is True

    # Every fragment of the selected call is present in Mixed, whatever an
    # earlier call's retained span covers.
    missing = page.evaluate(
        f"""async () => {{
          const entries = updateEntries(await detailFor('call', {third}));
          const entry = entries.find(item => item.fragments);
          return entry.fragments
            .map((fragment, index) => index)
            .filter(index => !document.querySelector(
              `#mixed [data-update-entry="${{entry.entryKey}}"][data-output-fragment="${{index}}"]`,
            ));
        }}"""
    )
    assert missing == [], missing

    # And every one of them focuses when clicked, including a pure removal.
    for index in range(count):
        fragments.nth(index).click()
        page.wait_for_timeout(150)
        focused = page.evaluate(
            """() => [...document.querySelectorAll('#mixed .fragment-focus')]
                 .map(node => node.dataset.outputFragment)"""
        )
        assert focused, f"fragment {index} focused nothing in Mixed"


@pytest.fixture
def transition_viewer_url(tmp_path):
    """A response whose text is replaced, so an entry has both halves."""
    store = TraceStore(tmp_path / "transition.llmtrace")
    first = store.start_call(
        {"model": "local", "prompt": "decide\nnow"},
        session_id="transition",
        branch_id="main",
    )
    store.finish_call(
        first, "Consider next source line and keep it", metadata={"duration_ms": 5}
    )
    second = store.start_call(
        {"model": "local", "prompt": "decide\nnow please"},
        session_id="transition",
        branch_id="main",
    )
    store.finish_call(
        second, "Decide if source line and keep it", metadata={"duration_ms": 5}
    )

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", second
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_clicking_one_half_of_a_transition_focuses_only_that_half(
    page: Page, transition_viewer_url
):
    url, call_id = transition_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-output[data-call-key="call:{call_id}"]').click()
    card = page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="output"]')
    pair = card.locator(".fragment-change").filter(has=page.locator("del")).first
    expect(pair.locator("del")).not_to_have_count(0)
    expect(pair.locator("ins")).not_to_have_count(0)

    def focused_tags():
        return page.evaluate(
            """() => [...document.querySelectorAll('#mixed .fragment-focus')]
                 .map(node => node.tagName)"""
        )

    # Clicking the removed half asks about removed text, and Exact State holds
    # no removed text, so nothing is flashed there either.
    pair.locator("del").first.click()
    page.wait_for_timeout(250)
    assert set(focused_tags()) == {"DEL"}, focused_tags()
    assert page.locator("#exact .exact-focus").count() == 0

    # Focus is the last style layer: it has to be visible on top of the kind and
    # operation decoration, which carry their own outline.
    decoration = page.evaluate(
        """() => {
          const node = document.querySelector('#mixed .fragment-focus');
          const style = getComputedStyle(node);
          return [style.outlineWidth, style.outlineColor, style.animationName];
        }"""
    )
    assert decoration[0] == "2px", decoration
    assert decoration[1] == "rgb(255, 240, 166)", decoration
    assert decoration[2] == "removed-flash", decoration

    pair.locator("ins").first.click()
    page.wait_for_timeout(250)
    assert set(focused_tags()) == {"INS"}, focused_tags()

    # Clicking the entry itself, away from either half, still focuses both.
    pair.locator(".fragment-location").click()
    page.wait_for_timeout(250)
    assert set(focused_tags()) == {"DEL", "INS"}, focused_tags()

    # Kind outlines on stacked inline marks must sit inside their own box, or a
    # dense stack of marks bleeds its outlines into the neighbouring lines.
    offsets = page.evaluate(
        """() => [...document.querySelectorAll(
             '#mixed .trace-part[class*=\"trace-kind-\"]'
           )]
             .filter(node => !node.classList.contains('fragment-focus')
               && !node.classList.contains('exact-focus'))
             .map(node => getComputedStyle(node).outlineOffset)"""
    )
    assert offsets, "expected inline kind marks in Mixed"
    assert all(value.startswith("-") or value == "0px" for value in offsets), offsets


def test_second_click_joins_the_first_selection_instead_of_cancelling_it(
    page: Page, repeated_prompt_viewer_url
):
    url, call_id, _repeated = repeated_prompt_viewer_url
    page.goto(f"{url}/")
    card = page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="input"]')
    entries = card.locator(".update-jump")
    expect(entries).not_to_have_count(0)
    assert entries.count() >= 2

    # Two clicks in a row on the same call, with no wait between them: the
    # second must not cancel the load the first started, or the focus it was
    # waiting to apply never arrives.
    page.evaluate(
        """(count) => {
          const jumps = document.querySelectorAll(
            '.update-card[data-phase="input"] .update-jump'
          );
          jumps[0].click();
          jumps[count - 1].click();
        }""",
        arg=entries.count(),
    )
    last_index = entries.nth(entries.count() - 1).get_attribute("data-update-index")
    page.wait_for_function(
        """(key) => [...document.querySelectorAll('#mixed .fragment-focus')]
             .some(node => node.dataset.updateEntry === key)""",
        arg=f"{call_id}:{last_index}",
        timeout=5000,
    )


def test_switching_phase_on_one_call_clears_the_other_phase_focus(
    page: Page, transition_viewer_url
):
    url, call_id = transition_viewer_url
    page.goto(f"{url}/")

    # This call changed both its input and its output.
    page.locator(f'.timeline-input[data-key="call:{call_id}"]').click()
    page.wait_for_timeout(300)
    assert page.evaluate(
        """() => document.querySelectorAll(
             '#mixed [data-state-scope="input"] .fragment-focus'
           ).length"""
    ) > 0

    # Clicking the output event must move focus to output, not add to it: the
    # input marks left behind would otherwise be what the pane rests on, so the
    # output click reads as if nothing happened.
    page.locator(f'.timeline-output[data-call-key="call:{call_id}"]').click()
    page.wait_for_timeout(400)
    state = page.evaluate(
        """() => {
          const inputFocus = document.querySelectorAll(
            '#mixed [data-state-scope="input"] .fragment-focus'
          ).length;
          const outputFocus = document.querySelectorAll(
            '#mixed [data-state-scope="output"] .fragment-focus'
          );
          const pane = document.querySelector('#mixed').getBoundingClientRect();
          const rect = outputFocus[0] && outputFocus[0].getBoundingClientRect();
          return {
            inputFocus,
            outputFocus: outputFocus.length,
            outputInView: rect ? (rect.top < pane.bottom && rect.bottom > pane.top) : false,
            exactStale: document.querySelectorAll(
              '#exact [data-state-scope="input"] .exact-focus'
            ).length,
          };
        }"""
    )
    assert state["inputFocus"] == 0, state
    assert state["outputFocus"] > 0, state
    assert state["outputInView"] is True, state
    assert state["exactStale"] == 0, state


@pytest.fixture
def add_then_remove_viewer_url(tmp_path):
    """A call adds a distinctive line; a later call removes it."""
    store = TraceStore(tmp_path / "addremove.llmtrace")
    marker = "DISTINCTIVE REMOVED PARAGRAPH ABOUT ENVIRONMENTAL PROTECTION"
    base = [f"line {i}" for i in range(1, 20)]
    c1 = store.start_call(
        {"model": "local", "prompt": "\n".join(base)},
        session_id="ar",
        branch_id="main",
    )
    store.finish_call(c1, "out one", metadata={"duration_ms": 5})
    added = base[:5] + [marker] + base[5:]
    c2 = store.start_call(
        {"model": "local", "prompt": "\n".join(added)},
        session_id="ar",
        branch_id="main",
    )
    store.finish_call(c2, "out two", metadata={"duration_ms": 5})
    # c3 removes the marker again
    c3 = store.start_call(
        {"model": "local", "prompt": "\n".join(base)},
        session_id="ar",
        branch_id="main",
    )
    store.finish_call(c3, "out three", metadata={"duration_ms": 5})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", c2, c3, marker
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_removed_text_in_mixed_references_the_call_that_removed_it(
    page: Page, add_then_remove_viewer_url
):
    url, adder, remover, marker = add_then_remove_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{remover}"]').click()
    page.wait_for_timeout(500)

    # The removed marker is shown as removed history, in full, and every removed
    # copy references the remover — never the call that added it.
    refs = page.evaluate(
        """(marker) => [...document.querySelectorAll('#mixed del.removed-part')]
             .filter(node => node.textContent.includes(marker))
             .map(node => ({entry: node.dataset.updateEntry, len: node.textContent.length}))""",
        marker,
    )
    assert refs, "removed marker should appear in Mixed"
    assert all(ref["entry"].startswith(f"{remover}:") for ref in refs), refs
    assert all(f"{adder}:" not in ref["entry"] for ref in refs), refs
    assert all(ref["len"] >= len(marker) for ref in refs), refs  # full text, not truncated

    # Clicking it lands on the removing call's entry, on its REMOVED side — so
    # the reference matches: removed text points to removed text.
    page.locator("#mixed del.removed-part", has_text=marker).first.click()
    page.wait_for_timeout(500)
    landed = page.evaluate(
        """(marker) => {
          const active = document.querySelector('.update-jump.active, .update-back-focus');
          if (!active) return {none: true};
          const del = active.querySelector('del.removed-part, .removed-part');
          return {
            card: active.closest('.update-card')?.dataset.key,
            removedHasMarker: !!del && del.textContent.includes(marker),
          };
        }""",
        marker,
    )
    assert landed.get("card") == f"call:{remover}", landed
    assert landed.get("removedHasMarker") is True, landed


@pytest.fixture
def multi_change_viewer_url(tmp_path):
    """A call that changes both a parameter and its prompt in one step."""
    store = TraceStore(tmp_path / "multi.llmtrace")
    first = store.start_call(
        {"model": "local", "prompt": "line one\nline two", "temperature": 0.1},
        session_id="multi",
        branch_id="main",
    )
    store.finish_call(first, "answer one", metadata={"duration_ms": 5})
    second = store.start_call(
        {"model": "local", "prompt": "line one\nline two changed", "temperature": 0.2},
        session_id="multi",
        branch_id="main",
    )
    store.finish_call(second, "answer two", metadata={"duration_ms": 5})

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", second
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_timeline_input_click_focuses_every_input_entry(
    page: Page, multi_change_viewer_url
):
    url, call_id = multi_change_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{call_id}"]').click()
    page.wait_for_timeout(400)

    card = page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="input"]')
    updates = card.locator(".update-jump")
    focused = card.locator(".update-jump.timeline-update-focus")
    # The call changed a parameter and the prompt: both entries are input, so
    # both are focused — not just the first.
    assert updates.count() >= 2
    expect(focused).to_have_count(updates.count())
    labels = focused.locator("strong").all_inner_texts()
    assert any("parameter" in label.lower() for label in labels), labels
    assert any("prompt" in label.lower() for label in labels), labels


def test_update_focus_flash_does_not_replay_on_live_refresh(
    page: Page, multi_change_viewer_url
):
    url, call_id = multi_change_viewer_url
    page.goto(f"{url}/")
    page.evaluate(
        """() => {
          window.__flashStarts = 0;
          document.querySelector("#updates").addEventListener(
            "animationstart",
            event => {
              if (event.animationName === "timeline-update-flash") {
                window.__flashStarts += 1;
              }
            },
            true,
          );
        }"""
    )
    page.locator(f'.timeline-input[data-key="call:{call_id}"]').click()
    page.wait_for_timeout(400)
    starts_after_click = page.evaluate("() => window.__flashStarts")
    assert starts_after_click > 0

    # A refresh re-runs renderUpdateCards to reconcile card order. Reconciling
    # must not re-insert a card that has not moved, or every refresh replays the
    # focus pulse. Drive the reconcile several times with unchanged order.
    for _ in range(4):
        page.evaluate("() => renderUpdateCards(state.timelineItems)")
        page.wait_for_timeout(150)
    page.wait_for_timeout(1500)  # let any lingering animation finish
    assert page.evaluate("() => window.__flashStarts") == starts_after_click, (
        "focus flash replayed on live refresh"
    )
    # Focus itself persists.
    expect(
        page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="input"] '
                     ".update-jump.timeline-update-focus")
    ).not_to_have_count(0)
