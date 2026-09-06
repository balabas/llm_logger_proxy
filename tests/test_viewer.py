from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from playwright.sync_api import Page, expect

from insequent_logger.protocol import extract_model_response
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


@pytest.fixture
def tall_updates_url(tmp_path):
    """A session whose Updates pane overflows: several calls, each with a long
    message body, so scrolling to a selected call actually has to move."""
    store = TraceStore(tmp_path / "tall.llmtrace")
    body = "\n".join(
        f"instruction line {index:03d} about buildings and structures"
        for index in range(40)
    )
    parent_state = None
    for revision in range(8):
        request = {
            "model": "local",
            "max_tokens": 80000,
            "messages": [
                {"role": "system", "content": f"{body}\nrevision {revision}"},
                {"role": "user", "content": f"request number {revision} " * 6},
            ],
        }
        keywords = {"session_id": "tall"}
        if parent_state is not None:
            keywords["explicit_parent_state"] = parent_state
        call = store.start_call(request, **keywords)
        store.finish_call(call, f"response for call {revision} " * 20)
        parent_state = store.get_call(call)["request_state_id"]

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


@pytest.fixture
def input_and_output_diff_url(tmp_path):
    """Two chained calls that change BOTH their input (user message) and their
    output — so call 2 has an input update entry (index 0) and an output update
    entry (index 1), which is what a cross-pane focus needs to disambiguate."""
    store = TraceStore(tmp_path / "io-diff.llmtrace")
    system = "You are a helpful assistant."
    first = store.start_call(
        {"model": "m", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "AAA original question about cats"},
        ]},
        session_id="s",
    )
    store.finish_call(first, "The original answer mentions cats and dogs.")
    state = store.get_call(first)["request_state_id"]
    second = store.start_call(
        {"model": "m", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "BBB changed question about buildings"},
        ]},
        session_id="s",
        explicit_parent_state=state,
    )
    store.finish_call(second, "The changed answer mentions buildings and rooms.")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_switching_focus_never_leaves_a_stale_update_entry_highlighted(
    page: Page, input_and_output_diff_url: str
):
    # Focusing one update entry, then another of a DIFFERENT scope, must leave only
    # the second highlighted in the Updates pane. Regression: the mark-click path
    # cleared its highlight only within one card and in one class-style, so the
    # previous entry (e.g. an output change) stayed lit after clicking an input
    # mark — the "second click focuses the wrong item".
    page.goto(f"{input_and_output_diff_url}/")
    page.locator('.timeline-input[data-key="call:2"]').click()

    # Focus the OUTPUT change first (via the output timeline item).
    page.locator('.timeline-output[data-call-key="call:2"]').click()
    focused = page.locator("#updates .timeline-update-focus, #updates .update-jump.active")
    expect(focused).to_have_count(1)
    expect(focused).to_contain_text("output")

    # Now click the INPUT mark in Exact. Only the input entry may stay highlighted.
    page.locator("#exact mark.exact-update.input-update").first.click()
    still = page.locator("#updates .timeline-update-focus, #updates .update-jump.active")
    expect(still).to_have_count(1)
    expect(still).to_contain_text("input")
    expect(still).not_to_contain_text("output")


@pytest.fixture
def exact_scroll_url(tmp_path):
    """Two calls sharing a long unchanged body, differing only in the last user
    message. The change sits near the bottom of the Exact pane's input scope, so
    revealing it requires scrolling — the setup needed to tell "scrolled" from
    "held still"."""
    store = TraceStore(tmp_path / "exact-scroll.llmtrace")
    body = "\n".join(
        f"line {index:03d}: shared unchanged content about buildings"
        for index in range(60)
    )
    first = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": body},
                {"role": "user", "content": "AAA original bottom message"},
            ],
        },
        session_id="exact",
    )
    store.finish_call(first, "r1")
    state = store.get_call(first)["request_state_id"]
    second = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": body},
                {"role": "user", "content": "BBB changed bottom message different"},
            ],
        },
        session_id="exact",
        explicit_parent_state=state,
    )
    store.finish_call(second, "r2")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_clicking_a_scope_in_exact_does_not_scroll_the_exact_pane(
    page: Page, exact_scroll_url: str
):
    # Clicking a reference inside a pane focuses it across every pane, but the
    # pane the click came from must not move — the user is already looking at it.
    # Only the other pane scrolls to the match. Regression: the scope-navigation
    # path scrolled *both* panes, jerking the Exact pane out from under the click.
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:2"]').first.click()
    input_label = page.locator('#exact [data-state-scope="input"] .state-scope-label')
    expect(input_label).to_be_visible()

    # Selecting the call legitimately scrolls Exact to the change; let that settle
    # so the reset below is not overwritten by a late selection scroll (which
    # would masquerade as a click-driven one).
    def exact_scroll_top():
        return page.evaluate("() => Math.round(document.querySelector('#exact').scrollTop)")

    previous = None
    for _ in range(30):
        page.wait_for_timeout(60)
        current = exact_scroll_top()
        if current == previous:
            break
        previous = current

    # pinPaneScroll is a short timing-based restore that happens to mask this for
    # fast card loads; neutralize it so the test checks the actual contract (the
    # source pane is never told to scroll) rather than winning a 260ms race.
    page.evaluate(
        "() => { window.pinPaneScroll = () => {};"
        " document.querySelector('#exact').scrollTop = 0; }"
    )
    page.wait_for_timeout(150)
    before = page.evaluate("() => Math.round(document.querySelector('#exact').scrollTop)")

    input_label.first.click()

    max_delta = 0
    for _ in range(16):
        page.wait_for_timeout(60)
        now = page.evaluate("() => Math.round(document.querySelector('#exact').scrollTop)")
        max_delta = max(max_delta, abs(now - before))
    assert max_delta <= 3, f"exact pane scrolled on its own click: Δ={max_delta}px"


def test_clicking_plain_scope_text_restarts_timeline_focus_pulse(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:2"]').click()
    page.wait_for_function("state.detail?.id === 2")
    page.evaluate(
        """() => {
          window.__timelinePlainTextPulses = [];
          document.querySelectorAll(".timeline-item").forEach(item => {
            item.addEventListener("animationstart", event => {
              if (event.animationName === "timeline-item-focus-flash") {
                window.__timelinePlainTextPulses.push(
                  item.dataset.key || item.dataset.callKey
                );
              }
            });
          });
          const content = document.querySelector(
            '#exact [data-state-scope="input"] > .state-scope-content'
          );
          // The "input:" prefix is a plain text node, not an update mark or the
          // scope label. This is the click that previously produced no feedback.
          content.click();
        }"""
    )
    # This unchanged text is inherited from checkpoint call #1, so ownership
    # navigation correctly points there rather than to reconstructed call #2.
    page.wait_for_function("window.__timelinePlainTextPulses.length === 1")
    timeline_input = page.locator('.timeline-input[data-key="call:1"]')
    expect(timeline_input).to_have_class(re.compile(r"\bactive\b"))
    expect(timeline_input).to_have_class(re.compile(r"\btimeline-focus-flash\b"))
    expect(page.locator(
        '.update-card[data-key="call:1"][data-phase="input"] '
        '.checkpoint-input.active'
    )).to_have_count(1)
    assert page.evaluate("state.timelineFocus") == {
        "key": "call:1",
        "phase": "input",
    }

    # The same already-active target must pulse again, making a successful
    # repeat focus distinguishable from a dead click.
    page.wait_for_timeout(1200)
    page.locator(
        '#exact [data-state-scope="input"] > .state-scope-content'
    ).click(position={"x": 4, "y": 4})
    page.wait_for_function("window.__timelinePlainTextPulses.length === 2")
    assert page.evaluate("window.__timelinePlainTextPulses") == ["call:1", "call:1"]


def test_clicking_a_mixed_mark_restores_the_pre_mousedown_scroll(
    page: Page, exact_scroll_url: str
):
    # CodeMirror handles mousedown before the pane's click handler. If that
    # handling moves the editor, the click handler must restore the position
    # from before mousedown rather than pinning the already-moved position.
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:2"]').first.click()
    page.wait_for_selector("#mixed .cm-scroller")
    entry_key = page.evaluate(
        """() => {
          const mark = mixedCodeMirrorModel.marks.find(
            candidate => candidate.attributes["data-update-entry"]
          );
          return mark.attributes["data-update-entry"];
        }"""
    )
    # Let the timeline selection's legitimate reveal finish before establishing
    # the click baseline; it is unrelated to the source-pane click under test.
    page.wait_for_timeout(400)
    # Put a semantic mark at a stable visible coordinate. The actual decoration
    # is virtualized when its long input change is outside CodeMirror's viewport;
    # this harness exercises the same delegated pane handlers without depending
    # on which document ranges CodeMirror has mounted.
    positions = page.evaluate(
        """entryKey => {
          const pane = document.querySelector("#mixed");
          const scroller = pane.querySelector(".cm-scroller");
          const target = document.createElement("button");
          target.id = "mixed-mousedown-regression-target";
          target.dataset.updateEntry = entryKey;
          target.textContent = "mixed update mark";
          target.style.cssText = "position:absolute;left:12px;top:48px;z-index:20";
          pane.appendChild(target);
          scroller.scrollTop = (scroller.scrollHeight - scroller.clientHeight) / 2;
          const before = scroller.scrollTop;
          const maximum = scroller.scrollHeight - scroller.clientHeight;
          const shifted = before >= 60
            ? before - 50
            : Math.min(maximum, before + 50);
          window.__mixedClickScrollFrames = [];
          let sampledFrames = 0;
          const sampleFrame = () => {
            window.__mixedClickScrollFrames.push(scroller.scrollTop);
            sampledFrames += 1;
            if (sampledFrames < 180) requestAnimationFrame(sampleFrame);
          };
          requestAnimationFrame(sampleFrame);
          target.addEventListener("mousedown", () => {
            // Reproduce a slow CodeMirror measurement on a very large virtual
            // document, well after the old 300ms source-pane guard expired.
            setTimeout(() => { scroller.scrollTop = shifted; }, 2200);
          }, { once: true });
          return { before, shifted };
        }""",
        entry_key,
    )
    assert positions["shifted"] != positions["before"], positions

    target = page.locator("#mixed-mousedown-regression-target")
    target.click()
    page.wait_for_timeout(2800)
    after = page.locator("#mixed .cm-scroller").evaluate(
        "scroller => scroller.scrollTop"
    )
    assert abs(after - positions["before"]) <= 1, {
        **positions,
        "after": after,
    }
    frames = page.evaluate("window.__mixedClickScrollFrames")
    assert frames
    assert all(abs(value - positions["before"]) <= 1 for value in frames), {
        **positions,
        "frames": frames,
    }

    # The longer guard must yield immediately to a fresh user gesture.
    released = page.locator("#mixed .cm-scroller").evaluate(
        """(scroller, shifted) => {
          scroller.dispatchEvent(new WheelEvent("wheel", {bubbles: true, deltaY: 20}));
          scroller.scrollTop = shifted;
          return scroller.scrollTop;
        }""",
        positions["shifted"],
    )
    page.wait_for_timeout(100)
    assert page.locator("#mixed .cm-scroller").evaluate(
        "scroller => scroller.scrollTop"
    ) == released


def test_mixed_click_survives_codemirror_replacing_the_mark(
    page: Page, exact_scroll_url: str
):
    # After scrolling, CodeMirror can redraw a decoration during mousedown. The
    # following click then targets the editor rather than the now-detached mark.
    # One gesture must still focus the corresponding Exact and Updates entries,
    # while leaving the source editor at the user's scroll position.
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:2"]').first.click()
    page.wait_for_selector("#mixed .cm-scroller")
    entry_key = page.evaluate(
        """() => mixedCodeMirrorModel.marks.find(
          candidate => candidate.attributes["data-update-entry"]
        ).attributes["data-update-entry"]"""
    )
    page.wait_for_timeout(400)

    before = page.evaluate(
        """entryKey => {
          const pane = document.querySelector("#mixed");
          const scroller = pane.querySelector(".cm-scroller");
          scroller.scrollTop = (scroller.scrollHeight - scroller.clientHeight) / 2;
          const target = document.createElement("button");
          target.dataset.updateEntry = entryKey;
          target.className = "input-update";
          pane.appendChild(target);

          target.dispatchEvent(new MouseEvent("mousedown", {
            bubbles: true,
            button: 0,
            clientX: 20,
            clientY: 50,
          }));
          // Reproduce the decoration replacement: click reaches the pane after
          // the element carrying data-update-entry has left the document.
          target.remove();
          pane.dispatchEvent(new MouseEvent("click", {
            bubbles: true,
            button: 0,
            clientX: 20,
            clientY: 50,
          }));
          return scroller.scrollTop;
        }""",
        entry_key,
    )

    expect(page.locator(f'#exact [data-update-entry="{entry_key}"].exact-focus')).to_have_count(1)
    expect(page.locator("#updates .timeline-update-focus, #updates .update-jump.active")).to_have_count(1)
    page.wait_for_timeout(400)
    after = page.locator("#mixed .cm-scroller").evaluate("node => node.scrollTop")
    assert abs(after - before) <= 1, {"before": before, "after": after}


@pytest.fixture
def input_change_scroll_url(tmp_path):
    """Three chained calls whose only difference is the last user message, sitting
    deep inside a tall shared body. Selecting the newest call reconstructs the
    whole segment, so an earlier call's removed (historical) input part appears as
    a ``<del>`` far down the Mixed pane — the setup that exposes the focus-driven
    scroll jump."""
    store = TraceStore(tmp_path / "input-change.llmtrace")
    body = "\n".join(
        f"system line {index:03d}: long shared context about buildings and tools"
        for index in range(120)
    )

    def make_call(parent_state, user_message):
        kwargs = {} if parent_state is None else {"explicit_parent_state": parent_state}
        call_id = store.start_call(
            {
                "model": "local",
                "messages": [
                    {"role": "system", "content": body},
                    {"role": "user", "content": user_message},
                ],
            },
            session_id="chain",
            **kwargs,
        )
        store.finish_call(call_id, "response " + user_message[:6])
        return store.get_call(call_id)["request_state_id"]

    state = make_call(None, 'complex_text_search(regex="Q", limit=100) -> 64.3k chars AAA')
    state = make_call(state, 'complex_text_search(regex="Q", limit=200) -> 12.1k chars BBB')
    make_call(state, 'complex_text_search(regex="Q", limit=300) -> 5.5k chars CCC')

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_clicking_a_historical_removed_input_part_does_not_scroll_mixed(
    page: Page, input_change_scroll_url: str
):
    # Clicking a removed (historical) input change deep in the Mixed pane must
    # focus it without moving the pane the user is already looking at. Regression:
    # applying the focus decoration rebuilt the whole EditorState with setState,
    # which discarded CodeMirror's measured line heights; its next measure pass
    # then re-anchored the huge virtual document and violently scrolled the pane
    # (up by ~1000px and back) before the scroll pin could correct it.
    page.goto(f"{input_change_scroll_url}/")
    page.locator('.timeline-input[data-key="call:3"]').first.click()
    page.wait_for_selector("#mixed .cm-scroller")
    page.wait_for_timeout(600)

    # Target an earlier call's removed input mark (a <del>), not call 3's own, so
    # the reconstructed segment is deep and the reflow has room to jump.
    target_entry = page.evaluate(
        """() => {
          const marks = mixedCodeMirrorModel.marks.filter(
            m => m.classes.includes("cm-mixed-removed")
              && m.classes.includes("input-update")
          );
          return marks.length ? marks[marks.length - 1].attributes["data-update-entry"] : null;
        }"""
    )
    assert target_entry, "fixture did not produce a removed input part"

    # Reveal the del, keeping it comfortably inside the viewport so the click
    # itself never needs a legitimate scroll-into-view.
    for _ in range(60):
        rect = page.evaluate(
            """entry => {
              const el = [...document.querySelectorAll('#mixed .cm-mixed-removed.input-update')]
                .find(node => node.dataset.updateEntry === entry);
              if (!el) return null;
              const r = el.getBoundingClientRect();
              const s = document.querySelector('#mixed .cm-scroller').getBoundingClientRect();
              return {
                top: r.top, bottom: r.bottom, left: r.left,
                visible: r.top > s.top + 40 && r.bottom < s.bottom - 40,
              };
            }""",
            target_entry,
        )
        if rect and rect["visible"]:
            break
        page.evaluate(
            """entry => {
              const el = [...document.querySelectorAll('#mixed .cm-mixed-removed.input-update')]
                .find(node => node.dataset.updateEntry === entry);
              const s = document.querySelector('#mixed .cm-scroller');
              const sr = s.getBoundingClientRect();
              if (el) {
                const r = el.getBoundingClientRect();
                s.scrollTop += r.top - (sr.top + sr.height * 0.5);
              } else {
                s.scrollTop += 300;
              }
            }""",
            target_entry,
        )
        page.wait_for_timeout(100)
    assert rect and rect["visible"], rect

    # Sample the scroll every animation frame across the click so a transient
    # jump that restores itself still fails the test.
    page.evaluate(
        """() => {
          window.__mixedFrames = [];
          const s = document.querySelector('#mixed .cm-scroller');
          let n = 0;
          const sample = () => {
            window.__mixedFrames.push(s.scrollTop);
            if (++n < 200) requestAnimationFrame(sample);
          };
          requestAnimationFrame(sample);
        }"""
    )
    before = page.evaluate("() => document.querySelector('#mixed .cm-scroller').scrollTop")
    page.mouse.click(rect["left"] + 5, (rect["top"] + rect["bottom"]) / 2)
    page.wait_for_timeout(2000)

    frames = page.evaluate("() => window.__mixedFrames")
    max_delta = max(abs(value - before) for value in frames)
    assert max_delta <= 3, {"before": before, "max_delta": max_delta, "frames": frames[:40]}

    # The click must still focus the removed part across the panes.
    assert page.evaluate(
        """entry => mixedCodeMirrorModel.marks.some(
          m => m.focused && m.attributes["data-update-entry"] === entry
        )""",
        target_entry,
    )


def test_updates_checkpoint_preserves_nested_message_indentation(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:1"]').click()
    messages = page.locator(
        '.update-card[data-key="call:1"][data-phase="input"] '
        ".checkpoint-message-list"
    )
    expect(messages).to_be_visible()

    layout = messages.evaluate(
        """element => {
          const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
          let node = null;
          while (walker.nextNode()) {
            if (walker.currentNode.nodeValue.includes("line 001")) {
              node = walker.currentNode;
              break;
            }
          }
          if (!node) return null;
          const index = node.nodeValue.indexOf("line 001");
          const range = document.createRange();
          range.setStart(node, index);
          range.setEnd(node, index + 1);
          return {
            whiteSpace: getComputedStyle(element).whiteSpace,
            indent: range.getBoundingClientRect().left
              - element.getBoundingClientRect().left,
            text: node.nodeValue,
          };
        }"""
    )
    assert layout is not None
    assert layout["whiteSpace"] == "pre-wrap", layout
    assert "\n      line 001" in layout["text"], layout
    assert layout["indent"] >= 30, layout


def test_tool_calls_and_json_tool_results_are_formatted_structurally(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    rendered = page.evaluate(
        """() => {
          const request = {
            messages: [
              {
                role: "assistant",
                content: "",
                tool_calls: [{
                  function: {
                    name: "get_doc_names",
                    arguments: "{\\\"limit\\\":57}",
                  },
                  id: "FCla3m9v0B0mmOjkoMBF7Y9u21aDM2er",
                  type: "function",
                }],
              },
              {
                role: "tool",
                content: JSON.stringify({
                  count: 57,
                  documents: [{doc_uid: "1033-45-23-ИГДИ", doc_name: "Технический отчет"}],
                }),
              },
            ],
          };
          const source = yaml(requestContent(request));
          const template = document.createElement("template");
          template.innerHTML = messageStructureHtml(escapeHtml(source));
          return {
            source,
            text: template.content.textContent,
            separators: template.content.querySelectorAll(".message-separator").length,
            labels: [...template.content.querySelectorAll(".message-field-label")]
              .map(label => label.textContent),
          };
        }"""
    )
    assert rendered["source"].index("role:") < rendered["source"].index("content:")
    assert rendered["labels"][:4] == ["Role", "Content", "Role", "Content"]
    assert rendered["separators"] == 1
    assert 'name: "get_doc_names"' in rendered["text"]
    assert 'id: "FCla3m9v0B0mmOjkoMBF7Y9u21aDM2er"' in rendered["text"]
    assert 'type: "function"' in rendered["text"]
    assert "arguments:\n" in rendered["text"]
    assert "limit: 57" in rendered["text"]
    assert "count: 57" in rendered["text"]
    assert 'doc_uid: "1033-45-23-ИГДИ"' in rendered["text"]
    assert '\\"count\\"' not in rendered["text"]


@pytest.fixture
def empty_output_url(tmp_path):
    """Two calls: one that produced normal content, and one that failed the way a
    context-exceeded streaming call does — the upstream opened the SSE stream and
    sent only role/finish_reason frames plus an error envelope, so no content
    delta parses out. The raw body is captured but the parsed output is blank."""
    store = TraceStore(tmp_path / "empty-output.llmtrace")
    request = {
        "model": "local",
        "stream": True,
        "messages": [{"role": "user", "content": "a prompt that overflows context"}],
    }
    normal = store.start_call(request, session_id="ctx")
    store.finish_call(
        normal,
        "Here is a real answer.",
        raw_response=(
            'data: {"choices":[{"delta":{"content":"Here is a real answer."}}]}\n\n'
            "data: [DONE]\n\n"
        ),
    )
    state = store.get_call(normal)["request_state_id"]
    raw_sse = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"},'
        '"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
        'data: {"error":{"message":"the request exceeds the available context '
        'size","code":500}}\n\n'
        "data: [DONE]\n\n"
    )
    failed = store.start_call(request, session_id="ctx", explicit_parent_state=state)
    store.finish_call(failed, "", raw_response=raw_sse, status="error")

    reasoning_raw_sse = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
        '"content":null},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"We '
        'parsed this reasoning."},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    reasoning_only = store.start_call(
        request, session_id="ctx", explicit_parent_state=state
    )
    parsed = extract_model_response(reasoning_raw_sse, streaming=True)
    store.finish_call(
        reasoning_only,
        parsed.content,
        thoughts=parsed.thoughts,
        raw_response=reasoning_raw_sse,
        status="cancelled",
    )

    # Guard the premise: the parsed output really is empty while the raw body is
    # not — otherwise this test could pass without exercising the fallback.
    detail = store.get_call(failed)
    assert detail["response"] == ""
    assert "the request exceeds" in detail["raw_response"]

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_empty_parse_output_falls_back_to_raw_response_in_exact(
    page: Page, empty_output_url: str
):
    # A context-exceeded/cancelled call parses to empty content, but the raw body
    # was captured. The Exact pane's Output scope must show that raw body (so the
    # user sees the error / finish_reason instead of a blank pane) and flag it as
    # unparsed. Regression: the Output scope rendered nothing at all.
    page.goto(f"{empty_output_url}/")
    page.locator('.timeline-input[data-key="call:2"]').first.click()

    output = page.locator('#exact [data-state-scope="output"]')
    expect(output).to_be_visible()
    # The captured raw frames — including the context-size error — are shown.
    expect(output.locator(".state-scope-content")).to_contain_text(
        "the request exceeds the available context size"
    )
    expect(output.locator(".state-scope-content")).to_contain_text('"finish_reason":"length"')
    # And it is clearly marked as raw rather than mistaken for real model output.
    expect(output.locator(".scope-badge")).to_have_count(1)

    # A normal call must be untouched: parsed content shows, no raw badge.
    page.locator('.timeline-input[data-key="call:1"]').first.click()
    normal_output = page.locator('#exact [data-state-scope="output"]')
    expect(normal_output.locator(".state-scope-content")).to_contain_text(
        "Here is a real answer."
    )
    expect(normal_output.locator(".scope-badge")).to_have_count(0)

    # A reasoning-only stream was parsed, even though it was cancelled before
    # public content arrived. Thoughts show the reasoning; Output must not dump
    # raw SSE.
    page.locator('.timeline-input[data-key="call:3"]').first.click()
    expect(page.locator('#exact [data-state-scope="thoughts"]')).to_contain_text(
        "We parsed this reasoning."
    )
    reasoning_output = page.locator('#exact [data-state-scope="output"]')
    expect(reasoning_output.locator(".state-scope-content")).not_to_contain_text(
        "data:"
    )
    expect(reasoning_output.locator(".state-scope-content")).to_contain_text(
        "No output content before cancellation."
    )
    expect(reasoning_output.locator(".scope-badge")).to_have_count(0)
    reasoning_output.locator(".state-scope-content").click()
    page.wait_for_function(
        "state.timelineFocus?.key === 'call:3' && state.timelineFocus?.phase === 'output'"
    )
    expect(page.locator('.timeline-output[data-call-key="call:3"]')).to_have_class(
        re.compile(r"\bactive\b")
    )
    assert page.evaluate("state.timelineFocus") == {
        "key": "call:3",
        "phase": "output",
    }


class _StreamingContextExceededUpstream(BaseHTTPRequestHandler):
    """A mock model server that streams a couple of content deltas and then fails
    the way a context-exceeded call does: a finish_reason=length frame followed by
    an error envelope. Frames are flushed with a delay so the relay forwards them
    incrementally, exercising the live side-channel."""

    FRAMES = [
        'data: {"choices":[{"delta":{"content":"Analyzing"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" buildings"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: {"error":{"message":"the request exceeds the available '
        'context size"}}\n\n',
        "data: [DONE]\n\n",
    ]

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        # The proxy must publish the request before upstream response headers
        # arrive; the caller-provided request id is already available then.
        time.sleep(0.8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Request-ID", "upstream-live-request-42")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in self.FRAMES:
            self.wfile.write(frame.encode())
            # Exceed the relay's read chunk so each small SSE frame is delivered
            # immediately instead of being buffered until connection close. SSE
            # comments are ignored by the parser and do not alter model output.
            self.wfile.write((f": {'x' * 4096}\n\n").encode())
            self.wfile.flush()
            time.sleep(0.3)


def test_live_side_channel_shows_streaming_output_before_it_is_stored(
    page: Page, tmp_path
):
    # The viewer must show a call's output while it is still streaming — before any
    # durable record exists — via the SSE /api/live side-channel, rendered in the
    # Exact pane (03) as the current call's output being reconstructed live. This
    # is the only way to see what a call produced before a mid-stream failure
    # (e.g. a context-exceeded error), since storage happens after the stream ends.
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", 0), _StreamingContextExceededUpstream
    )
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = TraceStore(tmp_path / "live.llmtrace")
    proxy = TraceServer(
        ("127.0.0.1", 0),
        store,
        f"http://127.0.0.1:{upstream.server_port}",
        default_session="live",
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{proxy.server_port}"

    def drive():
        # Let the browser's EventSource subscribe before the stream starts.
        time.sleep(1.0)
        requests.post(
            f"{base}/v1/chat/completions",
            headers={
                "X-LLMTrace-Session": "live",
            },
            json={
                "model": "local",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "MARKER-INPUT extract entities"}
                ],
            },
            stream=True,
            timeout=15,
        ).content

    try:
        page.goto(f"{base}/")
        page.wait_for_timeout(500)  # startLiveStream() opens the EventSource
        threading.Thread(target=drive, daemon=True).start()

        # The streaming call appears in the Timeline as an ordinary call: a → input
        # item and a streaming ← output item (marked not-finished).
        input_item = page.locator("#timeline .timeline-input")
        expect(input_item).to_have_count(1, timeout=8000)
        expect(input_item).to_contain_text("#1")
        # The first live input replaces the empty-session placeholder in Updates;
        # it must never claim there are no updates while the input card exists.
        expect(page.locator("#updates .update-card")).to_have_count(1)
        expect(page.locator("#updates .empty-session")).to_have_count(0)
        expect(page.locator("#updates")).to_contain_text(
            "MARKER-INPUT extract entities"
        )
        input_item.evaluate("node => { node.dataset.liveIdentitySentinel = 'same'; }")
        # Only the input phase exists while the proxy is waiting for response
        # headers. Response start updates this call rather than replacing it.
        expect(page.locator("#timeline .timeline-output")).to_have_count(0)
        streaming_output = page.locator("#timeline .timeline-output.timeline-streaming")
        expect(streaming_output).to_have_count(1, timeout=8000)
        expect(input_item).to_have_attribute("data-live-identity-sentinel", "same")

        # The compact proxy request number labels both phases; the synthetic
        # ``live-N`` key and upstream's long id must never replace it.
        expect(input_item).to_contain_text("#1")
        expect(streaming_output).to_contain_text("#1")
        expect(page.locator("#timeline")).not_to_contain_text("#live-")

        # Updates already knows this live input is a complete new state. Timeline
        # must reflect that immediately, while output is still streaming.
        expect(page.locator("#timeline .timeline-streaming")).to_have_count(1)
        expect(input_item.locator(".item-label")).to_have_text("→ new state input")
        expect(input_item).to_have_class(re.compile("checkpoint-call"))

        # Input appears in all panes as soon as it comes: on the auto-selected input
        # item, the Exact pane shows the forwarded request.
        input_scope = page.locator('#exact [data-state-scope="input"]')
        expect(input_scope).to_contain_text("MARKER-INPUT extract entities", timeout=8000)

        # Clicking the streaming output item shows its live output in the Exact
        # pane's output scope: parsed generated text (not raw SSE) plus the error.
        streaming_output.click()
        out = page.locator('#exact .live-output-scope[data-state-scope="output"] [data-live-body="output"]')
        expect(out).to_contain_text("Analyzing buildings", timeout=8000)
        expect(out).to_contain_text("the request exceeds the available context size")
        expect(out).not_to_contain_text('"choices"')

        # Once the output finishes (and the call is stored/reconciled), the
        # streaming marker clears — no in-flight output item remains.
        expect(page.locator("#timeline .timeline-streaming")).to_have_count(0, timeout=10000)
        expect(page.locator("#timeline")).to_contain_text(
            "#1"
        )
        expect(input_item).to_have_attribute("data-live-identity-sentinel", "same")
    finally:
        proxy.shutdown()
        proxy.server_close()
        store.close()
        upstream.shutdown()
        upstream.server_close()


def test_live_tool_call_deltas_are_assembled_into_readable_output(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    output = page.evaluate(
        """() => {
          const record = {
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {},
          };
          const frame = toolCalls => `data: ${JSON.stringify({
            choices: [{delta: {tool_calls: toolCalls}}],
          })}\n\n`;
          feedLive(record, frame([{
            index: 0,
            id: "tool-1",
            type: "function",
            function: {name: "get_toc_headings", arguments: "{"},
          }]));
          feedLive(record, frame([{
            index: 0,
            function: {arguments: "\\\"doc_id\\\":\\\"СМЛ*Раздел ПД №3*V0\\\"}"},
          }]));
          return record.output;
        }"""
    )
    assert json.loads(output) == [
        {
            "index": 0,
            "id": "tool-1",
            "type": "function",
            "function": {
                "name": "get_toc_headings",
                "arguments": {"doc_id": "СМЛ*Раздел ПД №3*V0"},
            },
        }
    ]
    assert "\n  " in output


def test_streaming_timeline_row_shows_running_token_count(page: Page, viewer_url: str):
    # The token counts render from the timeline item's usage, but a delta only
    # updates the live record. The item must be refreshed on a usage-bearing delta
    # so the streaming output row shows the running count, not an empty/stale one.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof upsertLiveTimelineItem === 'function'")
    text = page.evaluate(
        """() => {
          const live_id = 555777;
          const rec = {
            live_id, call_id: null, req_id: "r", request_id: "r", status: "streaming",
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {}, usage: {},
            request: {model: "local", messages: [{role: "user", content: "go"}]},
            started_ms: Date.now(),
          };
          state.live.set(live_id, rec);
          upsertLiveTimelineItem(rec); liveDetailFor(rec); renderLiveTimelinePanes();
          // A usage-bearing delta, exactly as the SSE handler processes it.
          const prev = JSON.stringify(rec.usage || {});
          feedLive(rec, 'data: {"choices":[{"delta":{"content":"hi"}}],'
            + '"usage":{"completion_tokens":1234,"prompt_tokens":444,"total_tokens":5678}}\\n\\n');
          if (JSON.stringify(rec.usage || {}) !== prev) upsertLiveTimelineItem(rec);
          renderLiveTimelinePanes();
          const row = document.querySelector(
            "#timeline .timeline-output.timeline-streaming"
          );
          return row ? row.textContent : null;
        }"""
    )
    assert text is not None, "streaming output row should exist"
    assert "1,234 out" in text, text
    assert "5,678 total" in text, text
    assert "streaming" in text, text


def test_timeline_shows_generation_speed(page: Page, viewer_url: str):
    # The output row shows the generation speed: the model's real decode rate for
    # a stored call, and a live estimate (streamed tokens / elapsed) while running.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof timelineUsageText === 'function'")
    result = page.evaluate(
        """async () => {
          const stored = timelineUsageText(
            {usage: {output_tokens: 310, total_tokens: 21574, output_per_second: 31.66}},
            "output"
          );
          // Live: stream some pieces across a measurable interval.
          const rec = {
            live_id: 888444, call_id: null, req_id: "r", request_id: "r", status: "streaming",
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {}, usage: {},
            request: {model: "local", messages: [{role: "user", content: "go"}]},
            started_ms: Date.now(),
          };
          state.live.set(888444, rec);
          upsertLiveTimelineItem(rec); liveDetailFor(rec);
          for (let i = 0; i < 6; i += 1) {
            feedLive(rec, 'data: {"choices":[{"delta":{"reasoning_content":"x "}}]}\\n\\n');
            await new Promise(r => setTimeout(r, 60));
          }
          feedLive(rec, 'data: {"choices":[{"delta":{"reasoning_content":"y "}}]}\\n\\n');
          return { stored, liveSpeed: rec.usage.output_per_second };
        }"""
    )
    assert "32 tok/s" in result["stored"], result           # 31.66 rounds to 32
    assert result["liveSpeed"] and result["liveSpeed"] > 0, result


def test_streaming_input_row_estimates_tokens_until_real_count_arrives(
    page: Page, viewer_url: str
):
    # The model reports the real prompt-token count only in its final frame, but
    # the input is known immediately, so the input row estimates from the request
    # text during the stream and switches to the exact count when it arrives.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof applyLiveInputEstimate === 'function'")
    result = page.evaluate(
        """() => {
          const live_id = 777222;
          const content = "слово ".repeat(540);  // ~3240 chars -> ~1200 tokens
          const rec = {
            live_id, call_id: null, req_id: "r", request_id: "r", status: "streaming",
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {}, usage: {},
            request: {model: "local", messages: [{role: "user", content}]},
            started_ms: Date.now(),
          };
          applyLiveInputEstimate(rec);
          state.live.set(live_id, rec);
          upsertLiveTimelineItem(rec); liveDetailFor(rec); renderLiveTimelinePanes();
          const inputRow = () => (document.querySelector(
            '#timeline .timeline-input[data-key="call:live-777222"]'
          ) || {}).textContent || "";
          const estimate = rec.usage.input_tokens;
          const during = inputRow();
          feedLive(rec, 'data: {"choices":[],"usage":'
            + '{"completion_tokens":10,"prompt_tokens":22461,"total_tokens":22471}}\\n\\n');
          upsertLiveTimelineItem(rec); renderLiveTimelinePanes();
          return { estimate, during, after: inputRow() };
        }"""
    )
    # Estimate ~ chars / 2.7, shown live as "N in".
    assert 900 < result["estimate"] < 1500, result
    assert f"{result['estimate']:,} in" in result["during"], result
    # Real prompt count takes over.
    assert "22,461 in" in result["after"], result


def test_streaming_row_estimates_output_tokens_until_real_count_arrives(
    page: Page, viewer_url: str
):
    # The server reports real token counts only in its final frame, so during the
    # stream the active row estimates output tokens by counting streamed pieces
    # (~1 token each). When the real count arrives it takes over and locks.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof feedLive === 'function'")
    result = page.evaluate(
        """() => {
          const live_id = 666333;
          const rec = {
            live_id, call_id: null, req_id: "r", request_id: "r", status: "streaming",
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {}, usage: {},
            request: {model: "local", messages: [{role: "user", content: "go"}]},
            started_ms: Date.now(),
          };
          state.live.set(live_id, rec);
          upsertLiveTimelineItem(rec); liveDetailFor(rec); renderLiveTimelinePanes();
          const row = () => (document.querySelector(
            "#timeline .timeline-output.timeline-streaming"
          ) || {}).textContent || "";
          // Seven reasoning deltas, no usage — the real Qwen3 streaming shape.
          for (let i = 0; i < 7; i += 1) {
            feedLive(rec, 'data: {"choices":[{"delta":{"reasoning_content":"x "}}]}\\n\\n');
            upsertLiveTimelineItem(rec);
          }
          renderLiveTimelinePanes();
          const during = row();
          // Final frame carries the model's real count.
          feedLive(rec, 'data: {"choices":[],"usage":'
            + '{"completion_tokens":310,"prompt_tokens":21264,"total_tokens":21574}}\\n\\n');
          upsertLiveTimelineItem(rec);
          renderLiveTimelinePanes();
          return { during, after: row(), usageIsReal: !!rec.usageIsReal };
        }"""
    )
    # Live estimate = number of streamed pieces.
    assert "7 out" in result["during"], result
    assert "streaming" in result["during"], result
    # Real count takes over and is marked authoritative.
    assert "310 out" in result["after"], result
    assert "21,574 total" in result["after"], result
    assert result["usageIsReal"] is True, result


def test_stored_live_call_reconciles_off_the_stale_render(page: Page, viewer_url: str):
    # A call selected while it streamed rendered against the synthetic live detail
    # (empty output during the stream). When it lands, the panes must re-render
    # from the real stored detail — not keep the stale empty-output DOM. The server
    # gives the exact live_id -> call_id mapping, so the swap must be definitive
    # even when loadTimeline's request-id heuristic misses (no request id sent).
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof reconcileStoredLive === 'function'")
    result = page.evaluate(
        """async () => {
          const stored = await detailFor("call", 1);   // any real stored call
          const live_id = 987654;
          const rec = {
            live_id, call_id: null, req_id: null, request_id: null, status: "streaming",
            output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {},
            request: stored.request, started_ms: Date.now(),
          };
          state.live.set(live_id, rec);
          upsertLiveTimelineItem(rec);
          liveDetailFor(rec);
          renderLiveTimelinePanes();
          await selectItem("call", liveId(live_id), null, false, false, "live");
          const streaming = state.detail && state.detail.live === true
            && (state.detail.response || "") === "";

          // Stream ends and the server commits the durable call #1.
          rec.status = "ok"; rec.endedAt = Date.now();
          await reconcileStoredLive(live_id, 1);

          return {
            streamingWasEmpty: streaming,
            selected: state.selected && state.selected.id,
            liveStillPresent: state.live.has(live_id),
            detailIsLive: !!(state.detail && state.detail.live),
            detailHasResponse: !!(state.detail && (state.detail.response || "").length),
          };
        }"""
    )
    assert result["streamingWasEmpty"] is True, result
    # Landed on the real stored call, synthetic gone, detail is the stored one.
    assert result["selected"] == 1, result
    assert result["liveStillPresent"] is False, result
    assert result["detailIsLive"] is False, result
    assert result["detailHasResponse"] is True, result


def test_stats_header_shows_db_size_and_flags_over_limit(page: Page, viewer_url: str):
    # The header must show the current database size (labelled), and flag it when
    # it exceeds the retention limit.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof loadStats === 'function'")
    result = page.evaluate(
        """async () => {
          const original = fetchJson;
          fetchJson = async (url) => (String(url).includes("/api/stats")
            ? {calls: 42, file_bytes: 20 * 1024 * 1024, max_file_bytes: 15 * 1024 * 1024,
               logical_bytes: 100, stored_bytes: 40}
            : original(url));
          try { await loadStats(); } finally { fetchJson = original; }
          const node = document.querySelector("#stats");
          return { text: node.textContent, overLimit: node.classList.contains("over-limit"),
                   title: node.title };
        }"""
    )
    assert "DB 20.00 MB / 15 MB" in result["text"], result
    assert "⚠" in result["text"], result
    assert result["overLimit"] is True, result
    assert "over the 15 MB retention limit" in result["title"], result


def test_live_deltas_are_coalesced_into_few_renders(page: Page, viewer_url: str):
    # A fast stream can deliver deltas far quicker than the screen refreshes.
    # Their DOM work must be coalesced into a per-frame flush; running a full
    # Timeline/Updates rebuild per delta is what froze the page. Here 500 delta
    # flushes must collapse to a couple of pane renders, not 500.
    page.goto(f"{viewer_url}/")
    page.wait_for_function("() => typeof scheduleLiveFlush === 'function'")
    result = page.evaluate(
        """async () => {
          let panesRenders = 0;
          const original = renderLiveTimelinePanes;
          renderLiveTimelinePanes = () => { panesRenders += 1; };
          try {
            for (let i = 0; i < 500; i += 1) scheduleLiveFlush(true);
            // Let a few animation frames pass so any scheduled flush runs.
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            await new Promise(r => setTimeout(r, 60));
            await new Promise(r => requestAnimationFrame(r));
            return { panesRenders };
          } finally {
            renderLiveTimelinePanes = original;
          }
        }"""
    )
    assert result["panesRenders"] <= 3, result


def test_new_live_item_preserves_selection_and_pane_scroll_when_follow_is_off(
    page: Page, tall_updates_url: str
):
    page.goto(f"{tall_updates_url}/")
    page.wait_for_selector("#timeline .timeline-item")
    result = page.evaluate(
        """async () => {
          const timeline = document.querySelector("#timeline");
          const maximum = timeline.scrollHeight - timeline.clientHeight;
          timeline.scrollTop = Math.max(1, Math.floor(maximum / 2));
          state.followNewItems = false;
          document.querySelector("#follow-new-items").checked = false;
          const selectedBefore = itemKey(state.selected);
          const before = {};
          for (const id of ["timeline", "mixed", "exact", "updates"]) {
            const pane = document.querySelector(`#${id}`);
            const scroller = pane.querySelector(".cm-scroller") || pane;
            const paneMaximum = scroller.scrollHeight - scroller.clientHeight;
            if (id !== "timeline" && paneMaximum > 4) {
              scroller.scrollTop = Math.round(paneMaximum * 0.37);
            }
            before[id] = scroller.scrollTop;
          }
          const record = {
            live_id: 9001,
            call_id: 9001,
            request_id: 9001,
            session: state.session,
            status: "running",
            started_ms: Date.now(),
            request: {model: "local", messages: [{role: "user", content: "new live call"}]},
            output: "",
            thoughts: "",
            buffer: "",
          };
          state.live.set(record.live_id, record);
          upsertLiveTimelineItem(record);
          liveDetailFor(record);
          renderLiveTimelinePanes();
          const selection = selectNewLiveItem(record);
          if (selection) await selection;
          const after = {};
          for (const id of Object.keys(before)) {
            const pane = document.querySelector(`#${id}`);
            const scroller = pane.querySelector(".cm-scroller") || pane;
            after[id] = scroller.scrollTop;
          }
          return {
            before,
            after,
            maximum,
            selectedBefore,
            selectedAfter: itemKey(state.selected),
            liveActive: document.querySelector(
              '.timeline-input[data-key="call:live-9001"]'
            )?.classList.contains("active"),
          };
        }"""
    )
    assert result["maximum"] > 0
    assert result["selectedAfter"] == result["selectedBefore"], result
    assert result["liveActive"] is False, result
    assert all(
        abs(result["after"][pane] - top) <= 1
        for pane, top in result["before"].items()
    ), result


def test_follow_selects_the_actual_last_timeline_event(
    page: Page, tall_updates_url: str
):
    page.goto(f"{tall_updates_url}/")
    items = page.locator("#timeline .timeline-item")
    expect(items).not_to_have_count(0)
    page.evaluate(
        """() => {
          state.followNewItems = false;
          document.querySelector("#follow-new-items").checked = false;
        }"""
    )
    page.locator("#timeline .timeline-input").first.click()

    last = items.last
    expected = last.evaluate(
        """node => ({
          key: node.dataset.key || node.dataset.callKey,
          phase: node.dataset.phase,
        })"""
    )
    assert expected["phase"] == "output"

    page.locator("#follow-new-items").check()
    expect(last).to_have_class(re.compile(r"\bactive\b"))
    page.wait_for_function(
        """expected => state.timelineFocus?.key === expected.key
          && state.timelineFocus?.phase === expected.phase""",
        arg=expected,
    )


def test_selecting_a_call_scrolls_updates_to_the_start_of_its_card(
    page: Page, tall_updates_url: str
):
    # Clicking a timeline item must bring the top of that call's update card
    # (its "New current state / call #N" header) to the top of the Updates pane,
    # not center a mark inside it and push the header off the top. Regression for
    # a tall card opening mid-content with no indication of which call it was.
    page.goto(f"{tall_updates_url}/")
    expect(page.locator(".timeline-item")).not_to_have_count(0)

    # An older call (call #1) with several cards below it, so its top has room to
    # reach the top of the pane and it is not the one auto-selected on load.
    older = page.locator('.timeline-input[data-key="call:1"]')
    expect(older).to_have_count(1)
    older.click()

    active = page.locator("#updates .update-card.active")
    expect(active).to_have_count(1)

    def offset_from_top():
        return page.evaluate(
            """() => {
              const pane = document.querySelector('#updates');
              const card = document.querySelector('#updates .update-card.active');
              return {
                top: card.getBoundingClientRect().top - pane.getBoundingClientRect().top,
                overflowing: pane.scrollHeight > pane.clientHeight,
              };
            }"""
        )

    # The fixture is built so the pane overflows; otherwise "scroll to start" is
    # vacuous.
    assert offset_from_top()["overflowing"] is True

    # Let the smooth-scroll animation settle, then the card's start sits at the
    # top of the pane (never scrolled past it, which was the bug: ~ -242px).
    for _ in range(40):
        offset = offset_from_top()["top"]
        if -3 <= offset <= 3:
            break
        page.wait_for_timeout(50)
    assert -3 <= offset <= 3, f"card start not anchored to pane top: {offset}px"


def test_clicking_updates_keeps_updates_fixed_and_starts_other_panes(
    page: Page, tall_updates_url: str
):
    page.goto(f"{tall_updates_url}/")
    card = page.locator(
        '#updates .update-card[data-key="call:3"][data-phase="output"]'
    )
    card.scroll_into_view_if_needed()
    expect(card).to_have_attribute("data-loaded", "true")
    # Initial lazy-card expansion and follow cleanup can legitimately adjust the
    # pane before the interaction under test. Let those finish first.
    page.wait_for_timeout(700)

    # Put the source card comfortably inside the viewport, rather than already
    # at an edge where preserving its scroll would be impossible to distinguish
    # from navigation to it.
    source_scroll = card.evaluate(
        """element => {
          const pane = document.querySelector('#updates');
          pane.scrollTop = Math.max(0, element.offsetTop - pane.clientHeight / 3);
          return pane.scrollTop;
        }"""
    )
    assert source_scroll > 0

    page.evaluate(
        """() => {
          window.__focusScrollCalls = [];
          window.__originalFocusScrollIntoView = focusScrollIntoView;
          focusScrollIntoView = (target, block) => {
            const pane = target?.closest?.('#timeline, #mixed, #exact, #updates');
            window.__focusScrollCalls.push({pane: pane?.id || null, block});
            return window.__originalFocusScrollIntoView(target, block);
          };
        }"""
    )

    # Use a DOM click so Playwright itself does not scroll the source to satisfy
    # actionability. This changed text has a precise corresponding item in the
    # Timeline, Mixed, and Exact panes.
    changed_text = card.locator(
        '.checkpoint-section[data-checkpoint-scope="output"]'
    )
    expect(changed_text).to_be_attached()
    changed_text.evaluate("element => element.click()")

    max_delta = 0
    for _ in range(14):
        page.wait_for_timeout(50)
        current_scroll = page.locator("#updates").evaluate(
            "element => element.scrollTop"
        )
        max_delta = max(max_delta, abs(current_scroll - source_scroll))

    calls = page.evaluate(
        """() => {
          focusScrollIntoView = window.__originalFocusScrollIntoView;
          return window.__focusScrollCalls;
        }"""
    )
    assert max_delta <= 3, {"maxDelta": max_delta, "calls": calls}
    assert not any(call["pane"] == "updates" for call in calls), calls
    for pane in ("timeline", "exact"):
        assert any(
            call["pane"] == pane and call["block"] == "start" for call in calls
        ), calls
    assert page.evaluate(
        """() => mixedCodeMirrorModel.marks.some(mark => (
          mark.focused && mark.attributes['data-state-scope'] === 'output'
        ))"""
    )


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
    # The waiting/running counter reflects in-flight live streams (storage is
    # deferred, so there is never a stored "running" call). Each live stream also
    # appears as its own synthetic Timeline item, so this is just the count.
    # startLiveStream() clears state.live once on load; wait for it (it sets
    # state.liveSource) before poking state.live so the reset does not race us.
    page.wait_for_function("() => state.liveSource")
    page.evaluate(
        """() => {
          state.live.set(3, {live_id: 3, status: "streaming"});
          renderWaitingCalls();
        }"""
    )
    expect(page.locator("#waiting-calls")).to_contain_text("1 waiting / running")
    expect(page.locator("#waiting-calls .waiting-calls-head")).to_have_count(1)
    page.evaluate(
        """() => {
          state.live.set(4, {live_id: 4, status: "streaming"});
          state.live.set(5, {live_id: 5, status: "streaming"});
          renderWaitingCalls();
        }"""
    )
    expect(page.locator("#waiting-calls")).to_have_text("3 waiting / running")
    page.evaluate("() => { state.live.clear(); renderWaitingCalls(); }")
    expect(page.locator("#waiting-calls")).to_have_text("0 waiting / running")
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
          const mixedText = mixedCodeMirrorModel.text;
          const markedText = mark => mixedText.slice(mark.start, mark.end);
          const retainedHistoricalRemoved = mixedCodeMirrorModel.marks.some(
            mark => mark.classes.includes("removed-part")
              && markedText(mark).includes("retained historical output")
          );
          const retainedHistoricalPresent = mixedCodeMirrorModel.marks.some(
            mark => mark.classes.includes("added-part")
              && markedText(mark).includes("retained historical output")
          );
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
    expect(
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-input").first
    ).to_be_visible()
    expect(page.locator("#exact .checkpoint-pane-focus.trace-kind-input")).to_be_visible()
    summary_checkpoint_output.locator(".checkpoint-output").click()
    expect(
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-output").first
    ).to_be_visible()
    expect(page.locator("#exact .checkpoint-pane-focus.trace-kind-output")).to_be_visible()
    summary_checkpoint.locator(".checkpoint-parameters").click()
    expect(page.get_by_role("button", name="Params")).to_have_class(re.compile("active"))
    expect(
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-input-params").first
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
        page.locator("#mixed .checkpoint-pane-focus.trace-kind-output").first
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
              const mixedOutputScope = mixedCodeMirrorModel.marks.some(
                mark => mark.focused
                  && mark.attributes["data-state-scope"] === "output"
              );
              const mixedFocusedUpdate = mixedCodeMirrorModel.marks.some(
                mark => mark.focused
                  && mark.attributes["data-update-entry"]
              );
              return entryKey === null
                && mixedOutputScope
            && document.querySelector(
              '#exact [data-state-scope="output"]'
            ).classList.contains("timeline-scope-focus")
            && document.querySelector(
              '#exact [data-state-scope="output"]'
            ).classList.contains("flash")
            && getComputedStyle(
              document.querySelector('#exact [data-state-scope="output"]')
            ).animationName === "timeline-scope-flash"
                && !mixedFocusedUpdate
                && !document.querySelector("#exact .exact-focus.input-update")
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
    assert page.evaluate(
        """() => mixedCodeMirrorModel.marks.some(mark =>
          mark.focused
          && mark.classes.includes("input-update")
          && mixedCodeMirrorModel.text
            .slice(mark.start, mark.end)
            .includes("Remember cobalt blue.")
        )"""
    )
    expect(
        page.locator(
            '.update-card[data-key="call:3"][data-phase="input"] '
            ".input-update-card.trace-op-changed"
        )
    ).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator('.timeline-input[data-key="call:3"]')).to_have_class(
        re.compile("active")
    )
    page.evaluate(
        """() => focusUpdateFromState(
          document.querySelector('#exact [data-state-scope="output"]')
        )"""
    )
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
    assert page.evaluate(
        """() => {
          const scope = mixedCodeMirrorModel.marks.find(
            mark => mark.attributes["data-state-scope"] === "output"
          );
          return scope && mixedCodeMirrorModel.text
            .slice(scope.start, scope.end)
            .includes("continuing");
        }"""
    )
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
    assert "prompt:" in page.evaluate("mixedCodeMirrorModel.text")
    expect(
        page.locator('#exact [data-input-field="prompt"] > .state-field-label')
    ).to_have_text("Prompt")
    expect(
        page.locator('#exact [data-input-field="prompt"] > .state-field-content')
    ).to_contain_text("retained prompt line one")
    assert not page.locator(
        '#exact [data-input-field="prompt"] > .state-field-content'
    ).text_content().lstrip().startswith("prompt:")
    assert page.evaluate(
        """() => {
          const text = mixedCodeMirrorModel.text;
          const scopes = mixedCodeMirrorModel.marks.filter(
            mark => mark.attributes["data-state-scope"]
          );
          const parameters = scopes.find(
            mark => mark.attributes["data-state-scope"] === "input-params"
          );
          return parameters
            && text.slice(parameters.start, parameters.end).includes('model: "local"')
            && scopes.some(mark => mark.attributes["data-state-scope"] === "input")
            && scopes.some(mark => mark.attributes["data-state-scope"] === "output");
        }"""
    )
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
    # 901 added this text and 902 removed it. The one struck span carries both
    # references: the strike-through focuses the removal (902), and the green
    # underline (its addition origin) focuses the addition (901).
    expect(
        page.locator('#mixed [data-update-entry="902:0"].removed-part')
    ).to_contain_text("first accumulated addition")
    origin = page.evaluate(
        """() => {
          const mark = mixedCodeMirrorModel.marks.find(candidate => (
            candidate.attributes["data-update-entry"] === "902:0"
            && candidate.classes.includes("removed-part")
            && mixedCodeMirrorModel.text.slice(candidate.start, candidate.end)
              .includes("first accumulated addition")
          ));
          return {
            added: mark?.attributes["data-added-entry"] || null,
            underline: !!mark?.classes.includes("cm-mixed-added-origin"),
          };
        }"""
    )
    assert origin == {"added": "901:0", "underline": True}
    # Navigation picks the reference by which decoration the click landed on:
    # the lower band (underline) → addition; anywhere else on the strike → removal.
    navigation = page.evaluate(
        """() => {
          const target = {
            dataset: {updateEntry: "902:0", addedEntry: "901:0"},
            closest() { return target; },
            getClientRects: () => [{left: 0, right: 100, top: 0, bottom: 20, height: 20}],
          };
          return {
            strike: mixedNavigationEntryKey(target, {clientX: 50, clientY: 6}),
            underline: mixedNavigationEntryKey(target, {clientX: 50, clientY: 18}),
          };
        }"""
    )
    assert navigation == {"strike": "902:0", "underline": "901:0"}
    expect(page.locator("#mixed")).to_contain_text("first accumulated addition")
    expect(
        page.locator(
            '#mixed [data-update-entry="902:0"].added-part',
            has_text="second accumulated addition",
        )
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


def test_search_result_highlights_and_reveals_match_in_state_panes(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    filters = page.locator('#search-form input[name="search-field"]')
    expect(filters).to_have_count(3)
    expect(page.locator('#search-form input[value="input"]')).to_be_checked()
    expect(page.locator('#search-form input[value="thoughts"]')).to_be_checked()
    expect(page.locator('#search-form input[value="output"]')).to_be_checked()
    page.get_by_placeholder("Search inputs, outputs, state…").fill("cobalt blue")
    page.get_by_role("button", name="Search").click()
    result = page.locator(".result", has_text="LLM call #2 · input").first
    expect(result).to_be_visible()
    result.click()

    page.wait_for_function("state.detail?.id === 2 && state.searchFocus?.owner_id === 2")
    expect(page.locator("#mixed .cm-search-match")).not_to_have_count(0)
    expect(page.locator("#exact .search-text-match")).not_to_have_count(0)
    expect(page.locator("#updates .search-text-match")).not_to_have_count(0)
    expect(page.locator("#mixed .cm-search-match-selected")).to_be_visible()
    expect(page.locator("#exact .search-text-match-selected")).to_be_visible()
    expect(page.locator("#updates .search-text-match-selected")).to_be_visible()
    expect(page.locator('.timeline-input[data-key="call:2"]')).to_have_class(
        re.compile(r"\bactive\b")
    )

    for pane, match in (
        ("mixed", ".cm-search-match-selected"),
        ("exact", ".search-text-match-selected"),
        ("updates", ".search-text-match-selected"),
    ):
        page.wait_for_function(
            """([paneId, selector]) => {
              const container = document.querySelector(`#${paneId}`);
              const target = container.querySelector(selector);
              if (!target) return false;
              const paneRect = container.getBoundingClientRect();
              const targetRect = target.getBoundingClientRect();
              return targetRect.bottom >= paneRect.top
                && targetRect.top <= paneRect.bottom;
            }""",
            arg=[pane, match],
        )


def test_first_search_result_click_scrolls_without_needing_a_second_click(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    search = page.get_by_placeholder("Search inputs, outputs, state…")

    # Call 2 is already selected when the viewer opens. Move all state panes away
    # from the match so this exercises repeat selection of the same call, which
    # is where the first search click intermittently used to lose its focus.
    page.evaluate(
        """() => {
          for (const id of ["mixed", "exact", "updates"]) {
            const pane = document.querySelector(`#${id}`);
            const scroller = pane.querySelector(".cm-scroller") || pane;
            scroller.scrollTop = 0;
          }
        }"""
    )
    search.fill("BBB changed bottom message different")
    page.get_by_role("button", name="Search", exact=True).click()
    result = page.locator(".result", has_text="LLM call #2 · input").first
    expect(result).to_be_visible()
    result.click()

    page.wait_for_function("state.searchFocus?.owner_id === 2")
    for pane, match in (
        ("mixed", ".cm-search-match-selected"),
        ("exact", ".search-text-match-selected"),
        ("updates", ".search-text-match-selected"),
    ):
        page.wait_for_function(
            """([paneId, selector]) => {
              const container = document.querySelector(`#${paneId}`);
              const target = container.querySelector(selector);
              if (!target) return false;
              const paneRect = container.getBoundingClientRect();
              const targetRect = target.getBoundingClientRect();
              return targetRect.bottom >= paneRect.top
                && targetRect.top <= paneRect.bottom;
            }""",
            arg=[pane, match],
        )


def test_focus_after_clearing_search_is_not_overwritten_by_scroll_restore(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    search = page.get_by_placeholder("Search inputs, outputs, state…")

    # Put Exact near the beginning through search focus. Clearing the query
    # schedules a few restoration passes to counter DOM scroll anchoring; the
    # immediately following call focus must supersede those stale restorations.
    search.fill("line 000: shared unchanged content")
    page.get_by_role("button", name="Search", exact=True).click()
    page.locator(".result", has_text="LLM call #2 · input").first.click()
    page.wait_for_function("state.searchFocus?.owner_id === 2")
    page.wait_for_timeout(300)

    page.evaluate(
        """() => {
          const search = document.querySelector("#search");
          search.value = "";
          search.dispatchEvent(new Event("input", {bubbles: true}));
          document.querySelector('.timeline-input[data-key="call:2"]').click();
        }"""
    )
    page.wait_for_timeout(350)
    focused = page.locator("#exact .exact-focus").first
    expect(focused).to_have_count(1)
    visibility = page.evaluate(
        """() => {
          const pane = document.querySelector("#exact");
          const target = pane.querySelector(".exact-focus");
          const paneRect = pane.getBoundingClientRect();
          const targetRect = target.getBoundingClientRect();
          const mixedFocus = mixedCodeMirrorModel.marks.find(mark => mark.focused);
          const viewport = mixedCodeMirrorView.viewport;
          return {
            exact: targetRect.bottom >= paneRect.top && targetRect.top <= paneRect.bottom,
            mixed: Boolean(
              mixedFocus
              && mixedFocus.start <= viewport.to
              && mixedFocus.end >= viewport.from
            ),
          };
        }"""
    )
    assert visibility == {"exact": True, "mixed": True}, (
        "the stale search-clear restore hid the first-click focus",
        visibility,
    )


def test_clearing_search_does_not_scroll_any_pane(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    search = page.get_by_placeholder("Search inputs, outputs, state…")
    search.fill("buildings")
    page.get_by_role("button", name="Search").click()
    page.locator(".result", has_text="LLM call #2 · input").first.click()
    page.wait_for_function("state.searchFocus?.owner_id === 2")
    page.wait_for_timeout(300)

    positions = page.evaluate(
        """() => {
          const positions = {};
          for (const id of ["timeline", "mixed", "exact", "updates"]) {
            const pane = document.querySelector(`#${id}`);
            const scroller = pane.querySelector(".cm-scroller") || pane;
            const maximum = scroller.scrollHeight - scroller.clientHeight;
            if (maximum <= 4) continue;
            scroller.scrollTop = Math.round(maximum * 0.37);
            positions[id] = scroller.scrollTop;
          }
          return positions;
        }"""
    )
    assert "mixed" in positions, positions

    search.fill("")
    expect(page.locator(".search-text-match, .cm-search-match")).to_have_count(0)
    page.wait_for_timeout(150)
    after = page.evaluate(
        """positions => Object.fromEntries(
          Object.keys(positions).map(id => {
            const pane = document.querySelector(`#${id}`);
            const scroller = pane.querySelector(".cm-scroller") || pane;
            return [id, scroller.scrollTop];
          })
        )""",
        positions,
    )
    assert all(abs(after[pane] - top) <= 1 for pane, top in positions.items()), {
        "before": positions,
        "after": after,
    }


def test_many_search_results_keep_visible_space_above_timeline(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector(".timeline-item")
    layout = page.evaluate(
        """() => {
          const timeline = document.querySelector("#timeline");
          const sourceItem = timeline.querySelector(".timeline-item");
          while (timeline.children.length < 200) {
            timeline.appendChild(sourceItem.cloneNode(true));
          }
          const results = document.querySelector("#search-results");
          results.innerHTML = `
            <div class="search-results-head">
              <strong>46 matches</strong><button>×</button>
            </div>
          ` + Array.from({length: 46}, (_, index) => `
            <div class="result">
              <small>LLM call #${index + 1} · output</small>
              visible result ${index + 1}
            </div>
          `).join("");
          results.classList.remove("hidden");
          const pane = document.querySelector(".timeline-pane");
          const head = results.querySelector(".search-results-head");
          const first = results.querySelector(".result");
          const bounds = node => node.getBoundingClientRect();
          return {
            paneHeight: bounds(pane).height,
            resultsHeight: bounds(results).height,
            visibleContentHeight: bounds(first).bottom - bounds(head).top,
            firstInsideResults: bounds(first).bottom <= bounds(results).bottom,
            timelineHeight: bounds(timeline).height,
            resultsScrollable: results.scrollHeight > results.clientHeight,
          };
        }"""
    )
    assert layout["firstInsideResults"] is True, layout
    assert layout["resultsHeight"] >= layout["visibleContentHeight"], layout
    assert layout["resultsHeight"] <= layout["paneHeight"] * 0.46, layout
    assert layout["timelineHeight"] > 100, layout
    assert layout["resultsScrollable"] is True, layout


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
            """() => mixedCodeMirrorModel.marks
                 .filter(mark => mark.focused)
                 .map(mark => mark.attributes["data-update-entry"])"""
        )
        # The clicked entry remains represented even while CodeMirror also
        # decorates the selected phase.
        keys = set(focused)
        key = f"{call_id}:{entries.nth(index).get_attribute('data-update-index')}"
        assert key in keys, focused
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
        """(id) => id === "mixed"
          ? mixedCodeMirrorModel.marks
              .filter(mark => mark.attributes["data-update-entry"])
              .map(mark => mark.attributes["data-update-entry"]
                + (mark.attributes["data-output-fragment"]
                  ? "/f" + mark.attributes["data-output-fragment"] : ""))
          : [...document.querySelectorAll('#' + id + ' [data-update-entry]')].map(
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
            .filter(index => !mixedCodeMirrorModel.marks.some(mark =>
              mark.attributes["data-update-entry"] === entry.entryKey
              && Number(mark.attributes["data-output-fragment"]) === index
            ));
        }}"""
    )
    assert missing == [], missing

    # And every one of them focuses when clicked, including a pure removal.
    for index in range(count):
        fragments.nth(index).click()
        page.wait_for_timeout(150)
        focused = page.evaluate(
            """() => mixedCodeMirrorModel.marks
                 .filter(mark => mark.focused)
                 .map(mark => mark.attributes["data-output-fragment"])"""
        )
        assert focused, f"fragment {index} focused nothing in Mixed"


def test_mixed_replacement_places_new_text_after_all_removed_text(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    result = page.evaluate(
        """() => {
          const common = [
            {role: "system", content: "stable system context"},
            {role: "user", content: "stable user context"},
          ];
          const oldMessages = [
            {role: "assistant", content: "OLD FIRST"},
            {role: "tool", content: "OLD SECOND"},
          ];
          const newMessages = [
            {role: "assistant", content: "NEW REPLACEMENT"},
          ];
          const detail = {
            ...state.detail,
            id: 99001,
            request: {model: "local", messages: [...common, ...newMessages]},
            response: "unchanged output",
            thoughts: "",
            diff: {
              mode: "diff",
              parameters: {},
              messages: [
                {op: "=", old: [0, 2], new: [0, 2]},
                {
                  op: "~", old: [2, 4], new: [2, 3],
                  old_messages: oldMessages,
                  new_messages: newMessages,
                  messages: newMessages,
                },
              ],
            },
            output_diff: {mode: "unchanged", changes: []},
            thoughts_diff: {mode: "unchanged", changes: []},
          };
          state.detail = detail;
          state.selected = {type: "call", id: detail.id};
          state.mixedSegmentDetails = [detail];
          renderMixed();
          renderExact();
          const marks = mixedCodeMirrorModel.marks
            .filter(mark => mark.classes.includes("input-update") && (
              mark.classes.includes("removed-part")
              || mark.classes.includes("added-part")
            ))
            .sort((left, right) => left.start - right.start || left.end - right.end)
            .map(mark => ({
              kind: mark.classes.includes("removed-part") ? "red" : "green",
              text: mixedCodeMirrorModel.text.slice(mark.start, mark.end),
            }));
          return {
            marks,
            exactRemoved: document.querySelectorAll("#exact .removed-part").length,
          };
        }"""
    )
    assert [mark["kind"] for mark in result["marks"]] == ["red", "red", "green"]
    assert "OLD FIRST" in result["marks"][0]["text"]
    assert "OLD SECOND" in result["marks"][1]["text"]
    assert "NEW REPLACEMENT" in result["marks"][2]["text"]
    assert result["exactRemoved"] == 0


def test_mixed_overlapping_history_is_removed_before_current_addition(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    parts = page.evaluate(
        """() => {
          const current = {
            entryKey: "current:0",
            operation: "+",
            newText: "NEW REPLACEMENT",
            needle: "NEW REPLACEMENT",
            needles: ["NEW REPLACEMENT"],
            scope: "output",
            category: "output",
          };
          const replacedHistory = {
            entryKey: "earlier:0",
            operation: "+",
            newText: "NEW REPLACEMENT",
            needle: "NEW REPLACEMENT",
            needles: ["NEW REPLACEMENT"],
            scope: "output",
            category: "output",
            fromEarlierCall: true,
          };
          const template = document.createElement("template");
          template.innerHTML = mixedStateHtml(
            "NEW REPLACEMENT",
            [current, replacedHistory],
          );
          return [...template.content.querySelectorAll("del, ins")].map(node => ({
            kind: node.tagName === "DEL" ? "red" : "green",
            text: node.textContent,
          }));
        }"""
    )
    assert [part["kind"] for part in parts] == ["red", "green"]
    assert [part["text"] for part in parts] == [
        "NEW REPLACEMENT",
        "NEW REPLACEMENT",
    ]


def test_first_mixed_click_works_with_an_existing_text_selection(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    page.locator('.timeline-input[data-key="call:3"]').click()
    mark = page.locator("#mixed .cm-mixed-added[data-update-entry]").first
    expect(mark).to_be_visible()
    clicked = mark.evaluate(
        """node => {
          const scroller = document.querySelector("#mixed .cm-scroller");
          scroller.scrollTop = Math.min(40, scroller.scrollHeight - scroller.clientHeight);
          const before = scroller.scrollTop;
          const text = node.firstChild;
          const range = document.createRange();
          range.setStart(text, 0);
          range.setEnd(text, Math.min(3, text.length));
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          const rect = node.getBoundingClientRect();
          const init = {
            bubbles: true,
            button: 0,
            clientX: rect.left + 2,
            clientY: rect.top + 2,
          };
          node.dispatchEvent(new MouseEvent("mousedown", init));
          node.dispatchEvent(new MouseEvent("mouseup", init));
          node.dispatchEvent(new MouseEvent("click", init));
          return {entryKey: node.dataset.updateEntry, before};
        }"""
    )
    expect(
        page.locator(
            f'#exact [data-update-entry="{clicked["entryKey"]}"].exact-focus'
        )
    ).not_to_have_count(0)
    expect(
        page.locator(
            f'#updates .update-jump.active[data-update-index="{clicked["entryKey"].split(":")[-1]}"]'
        )
    ).not_to_have_count(0)
    page.wait_for_timeout(350)
    assert page.locator("#mixed .cm-scroller").evaluate("node => node.scrollTop") == pytest.approx(
        clicked["before"], abs=1
    )


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
    page.wait_for_selector("#mixed .cm-editor")
    page.locator(f'.timeline-output[data-call-key="call:{call_id}"]').click()
    card = page.locator(f'.update-card[data-key="call:{call_id}"][data-phase="output"]')
    pair = card.locator(".fragment-change").filter(has=page.locator("del")).first
    expect(pair.locator("del")).not_to_have_count(0)
    expect(pair.locator("ins")).not_to_have_count(0)

    def focused_tags():
        return page.evaluate(
            """() => mixedCodeMirrorModel.marks
                 .filter(mark => mark.focused && (
                   mark.classes.includes("removed-part")
                   || mark.classes.includes("added-part")
                 ))
                 .map(mark => mark.classes.includes("removed-part") ? "DEL" : "INS")"""
        )

    # Clicking the removed half asks about removed text, and Exact State holds
    # no removed text, so nothing is flashed there either.
    pair.locator("del").first.click()
    page.wait_for_timeout(250)
    assert "DEL" in set(focused_tags()), focused_tags()
    assert page.locator(
        "#mixed .cm-mixed-removed.fragment-focus"
    ).first.evaluate("node => getComputedStyle(node).animationName") == (
        "mixed-removed-focus-pulse"
    )
    assert page.locator(
        "#mixed .cm-mixed-removed.fragment-focus"
    ).first.evaluate("node => getComputedStyle(node).outlineOffset") == "-1px"
    assert page.locator("#exact .exact-focus").count() == 0

    # Clicking a decoration which is already visible in Mixed must not navigate
    # CodeMirror to the beginning of a large, multiline mark. Other panes still
    # use this navigation path to reveal their corresponding Mixed fragment.
    navigation = page.evaluate(
        """async () => {
          const view = mixedCodeMirrorView;
          const originalDispatch = view.dispatch.bind(view);
          const originalFocusScrollIntoView = focusScrollIntoView;
          let codeMirrorScrolls = 0;
          let genericMixedScrolls = 0;
          view.dispatch = (...transactions) => {
            if (transactions.some(transaction => transaction?.scrollIntoView)) {
              codeMirrorScrolls += 1;
            }
            return originalDispatch(...transactions);
          };
          focusScrollIntoView = (target, ...args) => {
            if (target && document.querySelector("#mixed").contains(target)) {
              genericMixedScrolls += 1;
            }
            return originalFocusScrollIntoView(target, ...args);
          };
          const before = view.scrollDOM.scrollTop;
          document.querySelector(
            "#mixed .cm-mixed-removed[data-update-entry]"
          ).click();
          await new Promise(resolve => setTimeout(resolve, 120));
          view.dispatch = originalDispatch;
          focusScrollIntoView = originalFocusScrollIntoView;
          return {
            codeMirrorScrolls,
            genericMixedScrolls,
            before,
            after: view.scrollDOM.scrollTop,
          };
        }"""
    )
    assert navigation["codeMirrorScrolls"] == 0, navigation
    assert navigation["genericMixedScrolls"] == 0, navigation
    assert navigation["after"] == navigation["before"], navigation

    # Focus is the last style layer: it has to be visible on top of the kind and
    # operation decoration, which carry their own outline.
    page.wait_for_selector("#mixed .cm-editor")

    pair.locator("ins").first.click()
    page.wait_for_timeout(250)
    assert "INS" in set(focused_tags()), focused_tags()

    # Clicking the entry itself, away from either half, still focuses both.
    pair.locator(".fragment-location").click()
    page.wait_for_timeout(250)
    assert set(focused_tags()) == {"DEL", "INS"}, focused_tags()

    # Exact contains the current value only. Clicking its changed mark must
    # therefore navigate to Mixed's added/current half, not the removed half
    # which commonly appears first in document order.
    entry_index = pair.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
        "' update-jump ')]"
    ).get_attribute("data-update-index")
    exact_target = page.locator(
        f'#exact [data-update-entry="{call_id}:{entry_index}"]'
    ).first
    expect(exact_target).to_be_visible()
    exact_self_navigation = page.evaluate(
        """async (selector) => {
          const pane = document.querySelector("#exact");
          const target = document.querySelector(selector);
          const before = pane.scrollTop;
          const originalFocusScrollIntoView = focusScrollIntoView;
          let genericExactScrolls = 0;
          focusScrollIntoView = (element, ...args) => {
            if (element && pane.contains(element)) genericExactScrolls += 1;
            return originalFocusScrollIntoView(element, ...args);
          };
          target.click();
          await new Promise(resolve => setTimeout(resolve, 400));
          focusScrollIntoView = originalFocusScrollIntoView;
          return {
            before,
            after: pane.scrollTop,
            genericExactScrolls,
          };
        }""",
        arg=f'#exact [data-update-entry="{call_id}:{entry_index}"]',
    )
    assert exact_self_navigation["genericExactScrolls"] == 0, exact_self_navigation
    assert exact_self_navigation["after"] == exact_self_navigation["before"], (
        exact_self_navigation
    )
    assert set(focused_tags()) == {"INS"}, focused_tags()
    exact_navigation = page.evaluate(
        """() => {
          const focused = mixedCodeMirrorModel.marks.find(
            mark => mark.focused && mark.classes.includes("cm-mixed-added")
          );
          const viewport = mixedCodeMirrorView.viewport;
          const selection = mixedCodeMirrorView.state.selection.main;
          return {
            collapsedAtFocusedStart: Boolean(
              focused
              && selection.from === focused.start
              && selection.to === focused.start
            ),
            focusedInViewport: Boolean(
              focused
              && focused.start >= viewport.from
              && focused.start <= viewport.to
            ),
          };
        }"""
    )
    assert exact_navigation == {
        "collapsedAtFocusedStart": True,
        "focusedInViewport": True,
    }

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
        """(key) => mixedCodeMirrorModel.marks.some(
             mark => mark.focused && mark.attributes["data-update-entry"] === key
           )""",
        arg=f"{call_id}:{last_index}",
        timeout=5000,
    )


def test_switching_phase_on_one_call_clears_the_other_phase_focus(
    page: Page, transition_viewer_url
):
    url, call_id = transition_viewer_url
    page.goto(f"{url}/")
    page.wait_for_selector("#mixed .cm-editor")

    # This call changed both its input and its output.
    page.locator(f'.timeline-input[data-key="call:{call_id}"]').click()
    page.wait_for_timeout(300)
    assert page.evaluate(
        """() => mixedCodeMirrorModel.marks.filter(
             mark => mark.focused && mark.scope === "input"
           ).length"""
    ) > 0

    # Clicking the output event must move focus to output, not add to it: the
    # input marks left behind would otherwise be what the pane rests on, so the
    # output click reads as if nothing happened.
    page.locator(f'.timeline-output[data-call-key="call:{call_id}"]').click()
    page.wait_for_timeout(400)
    state = page.evaluate(
        """() => {
          const inputFocus = mixedCodeMirrorModel.marks.filter(
            mark => mark.focused && mark.scope === "input"
          ).length;
          const outputFocus = mixedCodeMirrorModel.marks.filter(
            mark => mark.focused && mark.scope === "output"
          );
          const outputEntries = updateEntries(state.detail).filter(
            entry => entry.scope === "output" || entry.scope === "thoughts"
          );
          const primaryEntry = outputEntries.find(
            entry => entry.scope === "output"
          ) || outputEntries[0];
          const primaryMark = mixedCodeMirrorModel.marks.find(
            mark => mark.attributes["data-update-entry"] === primaryEntry?.entryKey
          );
          const viewport = mixedCodeMirrorView.viewport;
          return {
            inputFocus,
            outputFocus: outputFocus.length,
            outputInView: outputFocus.some(
              mark => mark.start <= viewport.to && mark.end >= viewport.from
            ),
            primaryEntryKey: primaryEntry?.entryKey,
            primaryInView: Boolean(
              primaryMark
              && primaryMark.start <= viewport.to
              && primaryMark.end >= viewport.from
            ),
            exactStale: document.querySelectorAll(
              '#exact [data-state-scope="input"] .exact-focus'
            ).length,
          };
        }"""
    )
    assert state["inputFocus"] == 0, state
    assert state["outputFocus"] > 0, state
    assert state["outputInView"] is True, state
    assert state["primaryInView"] is True, state
    assert state["exactStale"] == 0, state

    # Re-selecting the same phase must navigate to the same primary entry. A
    # virtualized Mixed document may not have that DOM mark mounted before the
    # CodeMirror scroll completes, so no DOM fallback may override it.
    page.locator(f'.timeline-output[data-call-key="call:{call_id}"]').click()
    page.wait_for_timeout(400)
    repeated = page.evaluate(
        """(primaryEntryKey) => {
          const primaryMark = mixedCodeMirrorModel.marks.find(
            mark => mark.attributes["data-update-entry"] === primaryEntryKey
          );
          const viewport = mixedCodeMirrorView.viewport;
          return {
            primaryInView: Boolean(
              primaryMark
              && primaryMark.start <= viewport.to
              && primaryMark.end >= viewport.from
            ),
            focused: Boolean(primaryMark?.focused),
          };
        }""",
        arg=state["primaryEntryKey"],
    )
    assert repeated == {"primaryInView": True, "focused": True}


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


def test_removed_text_strike_links_removal_and_underline_links_addition(
    page: Page, add_then_remove_viewer_url
):
    # A removed part with a known origin carries both references on one struck
    # element: the red strike-through focuses where it was removed, and the green
    # underline at the baseline focuses where it was added. A click in the lower
    # band (on the underline) follows the addition; the rest focuses the removal.
    url, adder, remover, marker = add_then_remove_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{remover}"]').click()
    page.wait_for_timeout(500)

    # The removed marker is shown in full, owns its removal entry, and links back
    # to its addition origin (rendered as the green underline).
    refs = page.evaluate(
        """(marker) => mixedCodeMirrorModel.marks
             .filter(mark => mark.classes.includes("removed-part")
               && mixedCodeMirrorModel.text.slice(mark.start, mark.end).includes(marker))
             .map(mark => ({
               entry: mark.attributes["data-update-entry"],
               addedEntry: mark.attributes["data-added-entry"] || null,
               underline: mark.classes.includes("cm-mixed-added-origin"),
               len: mark.end - mark.start,
             }))""",
        marker,
    )
    assert refs, "removed marker should appear in Mixed"
    assert all(ref["entry"].startswith(f"{remover}:") for ref in refs), refs
    assert all(ref["addedEntry"].startswith(f"{adder}:") for ref in refs), refs
    assert all(ref["underline"] for ref in refs), refs
    assert all(ref["len"] >= len(marker) for ref in refs), refs  # full text, not truncated

    def del_box():
        return page.locator("#mixed del.removed-part", has_text=marker).first.evaluate(
            """node => {
              const r = [...node.getClientRects()].sort((a, b) => b.width - a.width)[0];
              return {x: r.left + r.width / 2, top: r.top, height: r.height};
            }"""
        )

    # Clicking the upper part of the struck text navigates to where it was removed.
    box = del_box()
    page.mouse.click(box["x"], box["top"] + box["height"] * 0.3)
    page.wait_for_timeout(500)
    assert page.evaluate("() => state.timelineFocus?.key") == f"call:{remover}"

    # Re-select the remover, then clicking the lower band (the underline)
    # navigates to where it was added.
    page.locator(f'.timeline-input[data-key="call:{remover}"]').click()
    page.wait_for_timeout(500)
    box = del_box()
    page.mouse.click(box["x"], box["top"] + box["height"] * 0.85)
    page.wait_for_timeout(500)
    assert page.evaluate("() => state.timelineFocus?.key") == f"call:{adder}"


@pytest.fixture
def present_then_removed_viewer_url(tmp_path):
    """A checkpoint call already contains a distinctive message; a later call
    removes it. The text has no explicit add event in the loaded history, so its
    origin is the earliest loaded call that still shows it."""
    store = TraceStore(tmp_path / "present-removed.llmtrace")
    marker = "DISTINCTIVE PRESENT SINCE START PARAGRAPH ABOUT BUILDINGS"
    body = "\n".join(f"ctx line {i:03d}" for i in range(8))
    origin = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": body},
                {"role": "user", "content": "keep this"},
                {"role": "assistant", "content": marker},
            ],
        },
        session_id="pr",
        branch_id="main",
    )
    store.finish_call(origin, "out one")
    state = store.get_call(origin)["request_state_id"]
    remover = store.start_call(
        {
            "model": "local",
            "messages": [
                {"role": "system", "content": body},
                {"role": "user", "content": "keep this"},
            ],
        },
        session_id="pr",
        branch_id="main",
        explicit_parent_state=state,
    )
    store.finish_call(remover, "out two")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", origin, remover, marker
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_present_then_removed_underline_links_to_earliest_call(
    page: Page, present_then_removed_viewer_url
):
    # Text that was already present when the loaded history begins has no explicit
    # "added" change, so its underline origin is the earliest loaded call that
    # still shows it; clicking that underline selects that call.
    url, origin, remover, marker = present_then_removed_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{remover}"]').click()
    page.wait_for_timeout(500)

    ref = page.evaluate(
        """(marker) => {
          const mark = mixedCodeMirrorModel.marks.find(candidate => (
            candidate.classes.includes("removed-part")
            && mixedCodeMirrorModel.text.slice(candidate.start, candidate.end).includes(marker)
          ));
          if (!mark) return null;
          return {
            entry: mark.attributes["data-update-entry"],
            addedEntry: mark.attributes["data-added-entry"] || null,
            addedCall: mark.attributes["data-added-call"] || null,
            underline: mark.classes.includes("cm-mixed-added-origin"),
          };
        }""",
        marker,
    )
    assert ref, "removed marker should appear in Mixed"
    assert ref["entry"].startswith(f"{remover}:"), ref
    assert ref["addedEntry"] is None, ref  # no explicit add event exists
    assert ref["addedCall"] == str(origin), ref
    assert ref["underline"], ref

    # Clicking the lower band (the underline) points the timeline to the earliest
    # call that shows it, while leaving the selected call (and the Mixed pane the
    # user clicked in) unchanged — only the other panes move to where it was added.
    box = page.locator("#mixed del.removed-part", has_text=marker).first.evaluate(
        """node => {
          const r = [...node.getClientRects()].sort((a, b) => b.width - a.width)[0];
          return {x: r.left + r.width / 2, y: r.top + r.height * 0.85};
        }"""
    )
    page.mouse.click(box["x"], box["y"])
    page.wait_for_timeout(500)
    assert page.evaluate("() => state.timelineFocus && state.timelineFocus.key") == f"call:{origin}"
    # Selection stays on the remover: the Mixed pane is not rebuilt/navigated away.
    assert page.evaluate("() => state.selected && state.selected.id") == remover


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


@pytest.fixture
def debug_label_viewer_url(tmp_path):
    """Calls carrying a debug_label, and one without."""
    store = TraceStore(tmp_path / "debuglabel.llmtrace")
    context = (
        "You are inspecting a technical project. Preserve exact facts and prior "
        "decisions. " * 6
    )
    messages = [
        {"role": "system", "content": context},
        {"role": "user", "content": "Begin the outline."},
    ]

    def call(label, extra_user):
        payload = list(messages)
        if extra_user:
            payload = payload + [{"role": "user", "content": extra_user}]
        made = store.start_call(
            {"model": "local", "messages": payload, "temperature": 0.2},
            session_id="debuglabel",
            branch_id="main",
            metadata={"debug_label": label} if label else None,
        )
        store.finish_call(made, "outline", metadata={"duration_ms": 5})
        return made

    labelled = call("07-rewrite-attempt-with-a-really-long-step-name", None)
    labelled_two = call("08-review", "Expand section two.")
    plain = call(None, "Expand section two with detail.")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", labelled_two, plain
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_debug_label_rides_existing_lines_in_timeline_and_updates(
    page: Page, debug_label_viewer_url
):
    url, labelled, plain = debug_label_viewer_url
    page.goto(f"{url}/")
    expect(page.locator(".timeline-item")).not_to_have_count(0)

    # Without an explicit title, the debug label is the primary title fallback;
    # a call without either falls back to its purpose.
    labelled_item = page.locator(f'.timeline-input[data-key="call:{labelled}"]')
    plain_item = page.locator(f'.timeline-input[data-key="call:{plain}"]')
    expect(labelled_item.locator(".item-title")).to_have_text("08-review")
    expect(labelled_item.locator(".item-debug")).to_have_count(0)
    expect(plain_item.locator(".item-title")).to_have_text("chat")
    expect(plain_item.locator(".item-debug")).to_have_count(0)

    # It rides the action line — the item is no taller than the one without a
    # label (same two-line layout).
    labelled_h = labelled_item.bounding_box()["height"]
    plain_h = plain_item.bounding_box()["height"]
    assert abs(labelled_h - plain_h) < 1, (labelled_h, plain_h)

    # It appears on the update card head, on that single head line — the head is
    # no taller than a head without a label.
    page.evaluate(
        """async () => {
          for (const c of document.querySelectorAll(
            '.update-card[data-phase=\"input\"]'
          )) { c.scrollIntoView(); await loadUpdateCard(c); }
        }"""
    )
    page.wait_for_timeout(400)
    labelled_head = page.locator(
        f'.update-card[data-key="call:{labelled}"][data-phase="input"] .update-card-head'
    )
    plain_head = page.locator(
        f'.update-card[data-key="call:{plain}"][data-phase="input"] .update-card-head'
    )
    expect(labelled_head.locator(".update-card-debug")).to_have_text("08-review")
    expect(plain_head.locator(".update-card-debug")).to_have_count(0)
    assert abs(
        labelled_head.bounding_box()["height"] - plain_head.bounding_box()["height"]
    ) < 1


@pytest.fixture
def titled_call_viewer_url(tmp_path):
    store = TraceStore(tmp_path / "title.llmtrace")
    call_id = store.start_call(
        {"model": "local", "prompt": "find documents"},
        session_id="titles",
        req_id="01JTITLE",
        metadata={
            "title": "EXECUTE/find_documents",
            "debug_label": "harness step 7",
        },
    )
    store.finish_call(
        call_id,
        "done",
        metadata={
            "duration_ms": 5,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 34,
                "total_tokens": 1234,
            },
        },
    )
    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", call_id
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_title_is_primary_timeline_label_and_updates_without_height_change(
    page: Page, titled_call_viewer_url
):
    url, call_id = titled_call_viewer_url
    page.goto(f"{url}/")
    item = page.locator(f'.timeline-input[data-key="call:{call_id}"]')
    expect(item.locator(".item-title")).to_have_text("EXECUTE/find_documents")
    expect(item.locator(".item-meta")).to_contain_text(f"#{call_id}")
    expect(item.locator(".item-meta")).not_to_contain_text("01JTITLE")
    expect(item.locator(".item-label")).to_have_text("→ new state input")
    expect(item.locator(".item-checkpoint-marker")).to_have_count(0)
    expect(item.locator(".item-debug")).to_have_count(0)
    expect(item.locator(".item-meta")).to_contain_text("1,200 in")
    output_item = page.locator(f'.timeline-output[data-call-key="call:{call_id}"]')
    expect(output_item.locator(".item-meta")).to_contain_text("34 out · 1,234 total")
    before = item.bounding_box()["height"]

    response = requests.post(
        f"{url}/_llmtrace/update-title",
        json={
            "req_id": "01JTITLE",
            "title": "EXECUTE/find_documents:TOOL_CALLS/get_toc_headings",
        },
        timeout=5,
    )
    assert response.status_code == 204
    expect(item.locator(".item-title")).to_have_text(
        "EXECUTE/find_documents:TOOL_CALLS/get_toc_headings",
        timeout=4000,
    )
    expect(item.locator(".item-label")).to_have_text("→ new state input")
    expect(item.locator(".item-debug")).to_have_count(0)
    after = item.bounding_box()["height"]
    assert abs(after - before) < 1, (before, after)


@pytest.fixture
def req_id_viewer_url(tmp_path):
    """A caller-declared branch tree: B and C both continue A."""
    store = TraceStore(tmp_path / "reqid.llmtrace")

    def call(req_id, prev_req_id, content):
        made = store.start_call(
            {"model": "local", "messages": [{"role": "user", "content": content}]},
            session_id="nb",
            branch_id="main",
            req_id=req_id,
            prev_req_id=prev_req_id,
            metadata={"debug_label": f"{req_id}-step"},
        )
        store.finish_call(made, "ok", metadata={"duration_ms": 5})
        return made

    a = call("A", None, "root")
    b = call("B", "A", "continue B")
    c = call("C", "A", "branch C from A")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", a, b, c
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_req_id_lineage_is_shown_in_header_and_metadata(page: Page, req_id_viewer_url):
    url, _a, b, _c = req_id_viewer_url
    page.goto(f"{url}/")
    page.locator(f'.timeline-input[data-key="call:{b}"]').click()
    page.wait_for_timeout(400)

    # Timeline uses the caller's public request identity, not the storage id.
    expect(page.locator(f'.timeline-input[data-key="call:{b}"]')).to_contain_text(
        "#B"
    )
    page.locator("#view-branches").click()
    expect(page.locator(f'#branch-graph .branch-node[data-key="call:{b}"]')).to_contain_text(
        "#B"
    )
    page.locator("#view-list").click()

    # The lineage header names the request and its declared predecessor rather
    # than an inferred parent source.
    expect(page.locator("#lineage")).to_contain_text("B ·")
    expect(page.locator("#lineage")).to_contain_text("← req A")

    page.get_by_role("button", name="Metadata").click()
    expect(page.locator("#exact")).to_contain_text('req_id: "B"')
    expect(page.locator("#exact")).to_contain_text('prev_req_id: "A"')


@pytest.fixture
def window_group_viewer_url(tmp_path):
    """Overview root, two windows (grouping nodes), lines under each, and votes."""
    store = TraceStore(tmp_path / "window.llmtrace")
    root = "overview"

    def call(step, req, prev, content, group=None):
        metadata = {"debug_label": step}
        if group:
            metadata["group"] = group
        made = store.start_call(
            {"model": "local", "messages": [{"role": "user", "content": content}]},
            session_id="nb",
            branch_id="main",
            req_id=req,
            prev_req_id=prev,
            metadata=metadata,
        )
        store.finish_call(made, "ok", metadata={"duration_ms": 5})
        return made

    overview = call("document overview", root, None, "overview")
    line = call("w1 · L0", "w1-0-1", root, "w1 l0", group="window 1")
    call("w1 · L5", "w1-5-1", root, "w1 l5", group="window 1")
    call("w2 · L0", "w2-0-1", root, "w2 l0", group="window 2")
    vote = call("table vote", "table-1-0", root, "tv")

    server = TraceServer(("127.0.0.1", 0), store, "http://127.0.0.1:1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", overview, line, vote
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    store.close()


def test_branch_view_groups_lines_under_a_synthetic_window_node(
    page: Page, window_group_viewer_url
):
    url, overview, line, vote = window_group_viewer_url
    page.goto(f"{url}/")
    page.wait_for_selector(".timeline-item", timeout=20000)
    page.locator("#view-branches").click()
    page.wait_for_timeout(400)

    tree = page.evaluate(
        """() => {
          const g = buildBranchGraph(state.timelineItems);
          const p = {};
          for (const n of g.nodes) p[String(n.id)] = n;
          return {
            synthetic: g.nodes.filter(n => n.synthetic).map(n => n.group),
            lineParent: p[%d] ? p[%d].parentId : null,
            voteParent: p[%d] ? p[%d].parentId : null,
            overviewIsRoot: p[%d] && p[%d].parentId == null,
          };
        }""" % (line, line, vote, vote, overview, overview)
    )
    # Each window is a synthetic grouping node.
    assert tree["synthetic"] == ["window 1", "window 2"], tree
    # A line hangs off its window node, not directly off the overview.
    assert tree["lineParent"] == "group:window 1", tree
    # A vote (no group) hangs off the overview root directly.
    assert tree["voteParent"] == overview, tree
    assert tree["overviewIsRoot"] is True, tree

    # The window node is rendered, but it is not a request: no call id, inert.
    windows = page.locator(".branch-node.synthetic")
    expect(windows).to_have_count(2)
    assert page.evaluate(
        "() => [...document.querySelectorAll('.branch-node.synthetic')]"
        ".every(n => n.dataset.callId == null)"
    )
    # Its lines and votes are real, selectable calls.
    expect(page.locator(f'.branch-node[data-call-id="{line}"]')).to_have_count(1)


def test_timeline_pane_is_resizable_and_persists_width(page: Page, req_id_viewer_url):
    url, _a, _b, _c = req_id_viewer_url
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{url}/")
    handle = page.get_by_role("separator", name="Resize Timeline")
    expect(handle).to_be_visible()

    before = page.locator(".timeline-pane").bounding_box()["width"]
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 100)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 90, box["y"] + 100)
    page.mouse.up()

    after = page.locator(".timeline-pane").bounding_box()["width"]
    assert after >= before + 70, (before, after)
    stored = page.evaluate("() => Number(localStorage.getItem('insequent.timelinePaneWidth'))")
    assert abs(stored - after) <= 2, (stored, after)

    page.reload()
    persisted = page.locator(".timeline-pane").bounding_box()["width"]
    assert abs(persisted - after) <= 2, (persisted, after)


def test_other_pane_focus_targets_owning_timeline_item(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.locator('.timeline-input[data-key="call:3"]').click()

    # Focusing a real Exact-state fragment highlights and focuses its list item.
    exact_fragment = page.locator('#exact [data-update-entry^="3:"]').first
    exact_fragment.click()
    expect(page.locator('.timeline-input[data-key="call:3"]')).to_have_class(
        re.compile(r"\bactive\b")
    )
    expect(page.locator('.timeline-input[data-key="call:3"]')).to_be_focused()

    # Historical parts may belong to a different call than the current state.
    # Their timeline focus changes independently and must not replace that state.
    focused = page.evaluate(
        """async () => {
          const historicalPart = document.createElement("span");
          historicalPart.dataset.updateEntry = "2:0";
          document.querySelector("#exact").appendChild(historicalPart);
          await focusUpdateFromState(historicalPart);
          historicalPart.remove();
          return {
            selected: state.selected.id,
            focus: state.timelineFocus,
          };
        }"""
    )
    assert focused == {
        "selected": 3,
        "focus": {"key": "call:2", "phase": "input"},
    }
    expect(page.locator('.timeline-input[data-key="call:2"]')).to_have_class(
        re.compile(r"\bactive\b")
    )
    expect(page.locator('.timeline-input[data-key="call:2"]')).to_be_focused()
    expect(page.locator('.timeline-input[data-key="call:3"]')).not_to_have_class(
        re.compile(r"\bactive\b")
    )


def test_unchanged_state_content_links_to_its_checkpoint_origin(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    page.locator('.timeline-input[data-key="call:3"]').click()
    origin = page.locator(
        '#exact [data-checkpoint-call="2"][data-checkpoint-scope="input-params"]'
    )
    expect(origin).to_have_attribute("title", "Inherited from checkpoint call #2")
    page.wait_for_timeout(300)

    result = page.evaluate(
        """async () => {
          const pane = document.querySelector("#exact");
          const origin = pane.querySelector(
            '[data-checkpoint-call="2"][data-checkpoint-scope="input-params"]'
          );
          const before = pane.scrollTop;
          origin.click();
          await new Promise(resolve => setTimeout(resolve, 400));
          const mixedOrigin = mixedCodeMirrorModel.marks.find(mark => (
            mark.focused
            && mark.attributes["data-checkpoint-call"] === "2"
            && mark.attributes["data-checkpoint-scope"] === "input-params"
          ));
          const viewport = mixedCodeMirrorView.viewport;
          const card = document.querySelector(
            '.update-card[data-key="call:2"][data-phase="input"]'
          );
          return {
            selected: state.selected.id,
            timelineFocus: state.timelineFocus,
            exactScrollPreserved: pane.scrollTop === before,
            mixedOriginFocused: Boolean(mixedOrigin),
            mixedOriginInView: Boolean(
              mixedOrigin
              && mixedOrigin.start <= viewport.to
              && mixedOrigin.end >= viewport.from
            ),
            checkpointCard: card?.classList.contains("checkpoint"),
            parametersActive: card?.querySelector(
              '[data-checkpoint-scope="input-params"]'
            )?.classList.contains("active"),
          };
        }"""
    )
    assert result == {
        "selected": 3,
        "timelineFocus": {"key": "call:2", "phase": "input"},
        "exactScrollPreserved": True,
        "mixedOriginFocused": True,
        "mixedOriginInView": True,
        "checkpointCard": True,
        "parametersActive": True,
    }


def test_reset_history_permanently_deletes_calls_older_than_selection(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_selector("#mixed .cm-editor")
    # The reconstructed current state can remain on a newer call while a
    # historical input/output is the visibly focused timeline item. Cleanup
    # must use that focus, not the stale state selection behind the panes.
    page.locator('.timeline-input[data-key="call:4"]').click()
    page.wait_for_function("state.detail?.id === 4")
    page.evaluate('setTimelineFocus("call:3", "output", false)')
    assert page.evaluate("state.selected.id") == 4
    expect(page.locator('.timeline-output[data-call-key="call:3"]')).to_have_class(
        re.compile(r"\bactive\b")
    )
    reset = page.get_by_role("button", name="Clean from here")
    expect(reset).to_be_enabled()
    expect(reset).to_have_attribute(
        "title",
        "Permanently delete focused call #3 and every older call",
    )

    confirmations = []

    def accept_reset(dialog):
        confirmations.append(dialog.message)
        dialog.accept()

    page.once("dialog", accept_reset)
    reset.click()
    # Cleaning is inclusive: the selected call #3 and every older call (#2) are
    # deleted, so #3 itself disappears and #4 becomes the new oldest.
    page.wait_for_function(
        """() => !document.querySelector('.timeline-input[data-key="call:3"]')
          && !document.querySelector('.timeline-input[data-key="call:2"]')
          && document.querySelector('.timeline-input[data-key="call:4"]')"""
    )
    assert len(confirmations) == 1
    assert "delete selected call #3 and the 1 call older than it" in confirmations[0]
    assert "2 total" in confirmations[0]
    assert "cannot be undone" in confirmations[0].casefold()

    expect(page.locator('.timeline-item[data-key="call:2"]')).to_have_count(0)
    expect(page.locator('.timeline-item[data-key="call:3"]')).to_have_count(0)
    expect(page.locator('.timeline-input[data-key="call:4"]')).not_to_have_count(0)
    expect(page.locator('.update-card[data-key="call:3"]')).to_have_count(0)
    expect(page.get_by_label("Session").locator("option:checked")).to_contain_text(
        "viewer · 1 call"
    )

    assert requests.get(f"{viewer_url}/api/calls/2", timeout=5).status_code == 404
    assert requests.get(f"{viewer_url}/api/calls/3", timeout=5).status_code == 404
    boundary = requests.get(f"{viewer_url}/api/calls/4", timeout=5).json()
    assert boundary["parent_state_id"] is None
    assert boundary["chronological_parent_id"] is None
    assert boundary["diff"]["mode"] == "snapshot"
    # The unrelated session is outside the selected boundary.
    assert requests.get(f"{viewer_url}/api/calls/1", timeout=5).status_code == 200


def test_cleaning_the_last_call_clears_every_state_pane(
    page: Page, exact_scroll_url: str
):
    page.goto(f"{exact_scroll_url}/")
    page.locator('.timeline-input[data-key="call:2"]').click()
    page.wait_for_function("state.detail?.id === 2")
    expect(page.locator("#exact")).to_contain_text("BBB changed bottom message")

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Clean from here").click()

    expect(page.locator(".timeline-item")).to_have_count(0)
    expect(page.locator("#mixed")).to_have_text("No LLM calls in this session.")
    expect(page.locator("#exact")).to_have_text("No current state.")
    expect(page.locator("#updates")).to_have_text("No updates in this session.")
    expect(page.locator("#mixed-status")).to_have_text("Select a call")
    expect(page.locator("#lineage")).to_have_text("Select an event")
    expect(page.locator("#mixed")).to_have_class(re.compile(r"\bempty\b"))
    expect(page.locator("#exact")).to_have_class(re.compile(r"\bempty\b"))
    expect(page.get_by_role("button", name="Clean from here")).to_be_disabled()


def test_three_thousand_calls_render_without_quadratic_ui_work(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    result = page.evaluate(
        """() => {
          const origin = Date.parse("2026-07-25T10:00:00Z");
          const items = Array.from({length: 3000}, (_, index) => ({
            type: "call",
            id: 10000 + index,
            sequence: index + 1,
            created_at: new Date(origin + index * 10).toISOString(),
            duration_ms: 1,
            status: "complete",
            branch_id: "main",
            label: "rewrite",
            request_state_id: 10000 + index,
            parent_state_id: index ? 9999 + index : null,
          }));
          const startedAt = performance.now();
          renderUpdateCards(items);
          renderTimelineEvents(items);
          const elapsed = performance.now() - startedAt;
          state.observer?.disconnect();
          return {
            elapsed,
            timelineEvents: document.querySelectorAll(".timeline-item").length,
            updateCards: document.querySelectorAll(".update-card").length,
            contentVisibility: getComputedStyle(
              document.querySelector(".timeline-item")
            ).contentVisibility,
          };
        }"""
    )
    assert result["timelineEvents"] == 6000
    assert result["updateCards"] == 6000
    assert result["contentVisibility"] == "auto"
    assert result["elapsed"] < 5000, result


def test_large_mixed_values_are_lazy_but_never_truncated(
    page: Page, viewer_url: str
):
    page.goto(f"{viewer_url}/")
    page.wait_for_function(
        "state.detail && state.mixedSegmentDetails.length > 0 && !state.pendingSelection"
    )
    expected = page.evaluate(
        """() => {
          const fullValue = "full-retained-value\\n".repeat(30000);
          state.liveBusy = true;
          loadSessions = async () => false;
          loadTimeline = async () => false;
          loadStats = async () => false;
          const detail = {
            ...state.detail,
            id: 900001,
            request: {model: "local", prompt: "large mixed value"},
            response: fullValue,
            thoughts: "",
            diff: {
              mode: "diff",
              prompt: {hunks: [{"=": "10 unchanged lines"}]},
              parameters: {},
            },
            output_diff: {mode: "snapshot"},
            thoughts_diff: {mode: "unchanged"},
          };
          state.detail = detail;
          state.selected = {type: "call", id: detail.id};
          state.mixedSegmentDetails = [detail];
          state.mixedHistoryComplete = true;
          renderMixed();
          return fullValue.length;
        }"""
    )
    expect(page.locator("#mixed-load-older")).to_have_count(0)
    expect(page.locator("#mixed-load-all")).to_have_count(0)
    page.wait_for_selector("#mixed .cm-editor")
    rendered = page.locator("#mixed").evaluate(
        """element => ({
          text: element._codeMirrorView.state.doc.toString(),
          documentLines: element._codeMirrorView.state.doc.lines,
          renderedLines: element.querySelectorAll(".cm-line").length,
          addedChars: mixedCodeMirrorModel.marks
            .filter(mark => mark.classes.includes("added-part"))
            .reduce((total, mark) => total + mark.end - mark.start, 0),
        })"""
    )
    assert len(rendered["text"]) >= expected
    assert rendered["text"].endswith("full-retained-value\n")
    assert "truncated" not in rendered["text"]
    assert rendered["documentLines"] >= 30000
    assert rendered["addedChars"] >= expected
    assert rendered["renderedLines"] < 200
