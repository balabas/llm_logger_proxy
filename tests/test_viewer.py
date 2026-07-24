from __future__ import annotations

import re
import threading

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
                {"role": "user", "content": "Remember cobalt blue."},
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
    store.finish_call(continuation, "continuing\nnext")
    continuation_state = store.get_call(continuation)["request_state_id"]
    summary = store.start_call(
        {
            "model": "local",
            "messages": [
                {
                    "role": "user",
                    "content": "Summarize the cobalt blue project conversation.",
                }
            ],
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
    assert page.evaluate(
        """() => {
          const entries = updateEntries({
            id: 3632,
            diff: {
              prompt: {hunks: [
                {"-": {lines: 1, preview: "removed source line"}},
                {"=": "1 unchanged lines"},
                {"+": "replacement source line"}
              ]}
            },
            output_diff: {mode: "unchanged"},
            response: ""
          });
          return entries.length === 1
            && entries[0].entryKey === "3632:0"
            && entries[0].operation === "~"
            && entries[0].oldText === "removed source line"
            && entries[0].newText === "replacement source line";
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
    expect(page.locator(".update-card")).to_have_count(3)
    expect(page.locator(".update-card.checkpoint .checkpoint-state")).to_have_count(2)
    expect(page.locator(".timeline-item.checkpoint-call")).to_have_count(2)
    expect(page.locator('.timeline-item[data-key="call:2"]')).to_have_class(
        re.compile("checkpoint-call")
    )
    expect(page.locator('.timeline-item[data-key="call:3"]')).not_to_have_class(
        re.compile("checkpoint-call")
    )
    expect(
        page.locator('.update-card[data-key="call:4"] .checkpoint-input')
    ).to_contain_text("Summarize the cobalt blue project conversation.")
    expect(
        page.locator('.update-card[data-key="call:4"] .checkpoint-output')
    ).to_contain_text("cobalt blue summary")
    expect(
        page.locator('.update-card[data-key="call:2"] .checkpoint-parameters')
    ).to_contain_text("temperature: 0.1")
    summary_checkpoint = page.locator('.update-card[data-key="call:4"]')
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
    summary_checkpoint.locator(".checkpoint-output").click()
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
    expect(summary_checkpoint.locator(".checkpoint-output")).to_have_class(
        re.compile("active")
    )
    expect(summary_checkpoint.locator(".checkpoint-output")).to_have_class(
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
    expect(page.locator(".update-card")).to_have_count(3)
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
    expect(page.locator(".update-card")).to_have_count(3)

    page.locator('.timeline-item[data-key="call:2"]').click()
    expect(
        page.locator('.update-card[data-key="call:2"] .checkpoint-input')
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
            '.update-card[data-key="call:3"] .update-jump[data-update-index="0"]'
        )
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(
        page.locator(
            '.update-card[data-key="call:3"] .update-jump[data-update-index="0"]'
        )
    ).to_have_class(re.compile("timeline-update-flash"))
    expect(
        page.locator('.update-card[data-key="call:2"] .checkpoint-input')
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
        page.locator('.update-card[data-key="call:4"] .checkpoint-input')
    ).to_have_class(re.compile("timeline-update-focus"))
    page.locator('.timeline-output[data-call-key="call:4"]').click()
    expect(
        page.locator('.update-card[data-key="call:4"] .checkpoint-output')
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
        page.locator('.update-card[data-key="call:3"] .output-update-card')
    ).to_have_class(re.compile("timeline-update-focus"))
    assert page.evaluate(
        """() => {
          const detail = {
            ...state.detail,
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
    expect(page.locator("#updates .timeline-update-focus")).to_have_count(1)
    expect(page.locator("#updates .timeline-update-flash")).to_have_count(1)
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
    expect(page.locator('.update-card[data-key="call:3"]')).to_have_class(
        re.compile("active")
    )
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
        '.update-card[data-key="call:3"] .update-jump',
        has_text="Added input · user",
    )
    expect(added_user).to_contain_text("Continue with the next section.")
    expect(added_user).to_have_class(re.compile("trace-kind-input"))
    expect(added_user).to_have_class(re.compile("trace-op-added"))
    expect(page.locator("#exact > .state-scope > .state-scope-label")).to_have_text(
        ["Input parameters", "Input", "Output"]
    )
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
    page.locator(
        '#mixed [data-state-scope="output"] > .state-scope-label'
    ).click()
    expect(page.locator('.timeline-output[data-call-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    expect(
        page.locator('.update-card[data-key="call:3"] .output-update-card')
    ).to_have_class(re.compile("timeline-update-focus"))
    expect(page.locator("#mixed .fragment-focus.output-update")).not_to_have_count(0)
    expect(page.locator("#exact .exact-focus.output-update")).not_to_have_count(0)
    page.locator(
        '#exact [data-state-scope="input-params"] > .state-scope-label'
    ).click()
    expect(
        page.locator("#updates .timeline-update-focus.trace-kind-input-params")
    ).not_to_have_count(0)
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
        '.update-card[data-key="call:3"] .parameter-update-card',
        has_text="Changed parameter · temperature",
    )
    expect(changed_temperature).to_contain_text("0.1 → 0.2")
    expect(changed_temperature).to_have_class(re.compile("trace-kind-input-params"))
    expect(changed_temperature).to_have_class(re.compile("trace-op-changed"))
    removed_parameter = page.locator(
        '.update-card[data-key="call:3"] .parameter-update-card',
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
        ["Input parameters", "Input", "Output"]
    )
    expect(
        page.locator("#exact > .state-scope.trace-kind-input-params")
    ).to_contain_text("temperature: 0.2")
    expect(page.locator('.update-card[data-key="call:3"]')).not_to_contain_text(
        "New current state"
    )
    output_fragment = page.locator(
        '.update-card[data-key="call:3"] .output-update-card .fragment-change',
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
    expect(page.locator("#exact .exact-focus")).to_have_text("Continue with the next section.")

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
            document.querySelector('.update-card[data-key="call:999999"]')?.remove();
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
    expect(page.locator(".update-card")).to_have_count(4)
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
    expect(page.locator('#mixed [data-update-entry="901:0"]')).to_contain_text(
        "first accumulated addition"
    )
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
