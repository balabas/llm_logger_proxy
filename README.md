# proxy llm logger

This is a compact tracing proxy and browser viewer for sequential LLM calls. It records
how prompts, parameters, outputs, branches, and application state evolve without storing every
request as an unrelated full snapshot.

It sits between an OpenAI-compatible client and an upstream model server:

```text
Application → proxy → OpenAI-compatible model server
                    ↓
              .llmtrace database
                    ↓
               Browser viewer
```

## Features

- Proxies OpenAI-compatible chat and text completion requests.
- Supports streaming responses while preserving the original provider envelope.
- Reconstructs exact request and response state from compact snapshots and deltas.
- Tracks chronological order separately from request-state lineage.
- Detects divergent inputs and creates visible state checkpoints.
- Identifies overlapping calls and assigns stable parallel branches.
- Records optional non-LLM application events.
- Provides full-text search across traced state.
- Limits disk usage by removing complete old sessions.
- Updates the browser viewer live without disturbing the current selection or scroll position.

## Requirements

- Python 3.11 or newer
- An OpenAI-compatible upstream server, such as a local llama.cpp server
- A Chromium-compatible browser for the viewer

## Installation

Clone the repository, create a virtual environment, install the dependencies, and install the
package in editable mode:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

Install the OpenAI Python client if you want to run the client example below:

```bash
.venv/bin/python -m pip install openai
```

## Quick start

Logger loads [`config.toml`](config.toml) by default. The included configuration expects the
upstream model server at `http://127.0.0.1:8080` and starts Logger at
`http://127.0.0.1:8081`.

```bash
.venv/bin/python -m insequent_logger
```

Then:

- Open the viewer at <http://127.0.0.1:8081/>.
- Point OpenAI-compatible clients to `http://127.0.0.1:8081/v1`.

For example:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8081/v1",
    api_key="local-no-key-required",
    default_headers={
        "X-LLMTrace-Session": "my-run",
        "X-LLMTrace-Branch": "main",
    },
)

response = client.chat.completions.create(
    model="local-model",
    messages=[{"role": "user", "content": "Explain the current project state."}],
)
print(response.choices[0].message.content)
```

Logger also forwards llama.cpp-style utility routes such as `/apply-template`, `/tokenize`,
`/detokenize`, and `/health`.

## Configuration

```toml
[server]
host = "127.0.0.1"
port = 8081

[upstream]
url = "http://127.0.0.1:8080"

[reranker]
enabled = false

[reranker.llama_cpp]
host = "127.0.0.1"
port = 8082

[reranker.listen]
host = "127.0.0.1"
port = 8083

[storage]
path = "trace.llmtrace"
max_mb = 3

[defaults]
session_id = "unassigned"
branch_id = "main"
```

Enable `[reranker]` to expose a second listener dedicated to `/rerank` and
`/v1/rerank`. `[reranker.listen]` configures the logging listener and
`[reranker.llama_cpp]` configures the actual llama.cpp reranker. It forwards while recording
the request and response in the same trace file, session, and chronological
Timeline as calls received by the main LLM proxy. Reranker calls receive the
default `rerank` debug label so they remain distinguishable in the merged view.

Command-line options can override the main settings. Use `--config PATH` only to load a file
other than the default `config.toml`:

```text
--config PATH
--db PATH
--host HOST
--port PORT
--upstream URL
```

Example:

```bash
.venv/bin/python -m insequent_logger \
  --db experiment.llmtrace \
  --port 8090
```

## What is traced

Every completion or enabled reranker request routed through the proxy is recorded,
**with or without any headers**.
Both the OpenAI-compatible routes (`/v1/chat/completions`, `/v1/completions`) and llama.cpp's
native `/completion` are captured, with or without a `/v1` prefix. A request that sets no
`X-LLMTrace-*` headers is still traced — it lands in the default session (`[defaults].session_id`,
`unassigned` unless configured). Non-completion routes (`/apply-template`, `/tokenize`,
`/embeddings`, `/health`, …) pass through untraced. Only requests that bypass the proxy and talk
to the upstream server directly are invisible.

## Trace headers

Clients can describe exact lineage using optional request headers:

| Header | Purpose |
| --- | --- |
| `X-LLMTrace-Session` | Groups calls into a selectable viewer session. |
| `X-LLMTrace-Branch` | Names the logical branch; defaults to `main`. |
| `X-LLMTrace-Purpose` | Adds a short call purpose such as `chat` or `summarize`. |
| `X-LLMTrace-Base-State` | Identifies the exact request-state parent. |
| `X-LLMTrace-Run` | Associates calls with a larger application run. |
| `X-LLMTrace-Debug-Label` | A per-request step label (e.g. `07-rewrite`) shown on the call's timeline and update rows. Set it per request. |
| `X-LLMTrace-Debug-Label-Encoding` | Set to `percent` when the debug label is percent-encoded so non-ASCII step names can be transported safely in an HTTP header. |
| `X-LLMTrace-Title` | Primary human-readable Timeline title supplied by the harness. The input/output phase remains visible as smaller secondary text. |
| `X-LLMTrace-Req-Id` | A caller-assigned identity for this request. |
| `X-LLMTrace-Prev-Req-Id` | The `req_id` of the request this one continues. Declares the branch tree explicitly: two requests naming the same predecessor become sibling branches. Takes the place of similarity inference; an unknown value is rejected with `400`. |
| `X-LLMTrace-Group` | Names a structural unit the call belongs to (e.g. `window 3`). The branch view groups calls sharing a value under one synthetic grouping node — not a request — between them and their shared parent. |

Dynamic callers may supply an initial `X-LLMTrace-Title`, then finalize that
exact request during or after streaming:

```http
POST /_llmtrace/update-title
Content-Type: application/json

{"req_id":"caller-generated-id","title":"EXECUTE/find_documents:TOOL_CALLS/get_toc_headings"}
```

The command replaces only the displayed title of the call identified by
`X-LLMTrace-Req-Id`; it does not change `X-LLMTrace-Debug-Label` or inspect the
model response. An open viewer updates the existing Timeline item in place.

When the provider reports token usage, Timeline shows input tokens on the input
event and output plus total tokens on the output event. OpenAI-compatible
`usage` fields and llama.cpp native `timings.prompt_n` / `timings.predicted_n`
are supported; unavailable counts are not estimated.

If no base state is supplied, Logger chooses the best recent parent and labels the
relationship as inferred.

### Parallel calls

If a request starts before the best-matching call on its branch finishes, Logger
automatically forks it into a stable branch such as `main~parallel-2`. Later calls are routed
back to matching branches using request-state similarity and completed assistant-response
identity.

Explicit `X-LLMTrace-Branch` values remain branch roots. Supplying
`X-LLMTrace-Base-State` is recommended when the application already knows the precise parent.

### Branch labels in the timeline

Each timeline item names its branch. `main` is the branch root — the header
`X-LLMTrace-Branch` value, defaulting to `main`. `p2`, `p3`, … mark **parallel lanes**:
concurrent calls that forked from the same parent state and ran at the same time. Lane 0 keeps
the root name (`main`); lane 1 shows as `main · p2`, lane 2 as `main · p3`, and so on. These
lanes correspond to stored branches `main~parallel-2`, `main~parallel-3`, and are created
automatically when calls overlap — they are concurrency, not authored branches.

### Declaring branches explicitly (`req_id` / `prev_req_id`)

When the caller already knows its own branch tree — as a notebook driving multiple steps and
retries does — it can declare lineage directly instead of relying on similarity inference.
Give each request a `X-LLMTrace-Req-Id`, and point it at its predecessor with
`X-LLMTrace-Prev-Req-Id`. The predecessor's resulting state becomes this request's parent, so
two requests naming the same predecessor become sibling branches. The pair is stored on the
call, surfaced on the timeline row and in the Metadata tab, and shown in the pane header as
`<req_id> · state S… ← req <prev_req_id>`. Pair this with a meaningful
`X-LLMTrace-Debug-Label` per step so each request reads as an explainable step name.

### Request identity (`X-Request-ID`)

Besides the `X-LLMTrace-*` family, Logger reads the conventional request-id headers.
`X-Request-ID` (or `Request-ID`) on the incoming request is recorded as the call's provider
request id; if the client sends neither, the same headers are read off the *upstream*
response instead. A client-supplied value always wins over the upstream one. This is
independent of `X-LLMTrace-Req-Id`, which declares branch lineage rather than provider identity.

### Header forwarding

What reaches the upstream server depends on which path a request takes.

**Traced completion and reranker requests** are rebuilt rather than relayed. Only three
headers are forwarded upstream:

| Forwarded | Value |
| --- | --- |
| `Content-Type` | Always `application/json`. |
| `Accept` | Copied from the client, `*/*` if absent. |
| `Authorization` | Copied only when the client sent one. |

Every other request header — including `X-LLMTrace-*`, `Accept-Encoding` and any custom
header — is dropped and never reaches the upstream server. Callers that must pass extra
headers to the provider cannot do so through a traced route.

**Untraced pass-through routes** (`/apply-template`, `/tokenize`, `/embeddings`, …) relay the
client's headers as-is, minus hop-by-hop headers (`Host`, `Content-Length`, `Connection`) and
the lineage headers `X-LLMTrace-Session`, `-Branch`, `-Purpose`, `-Base-State`, `-Run`.

On the way back, Logger adds `X-LLMTrace-Call` to every traced response, carrying the integer
call id that the request was recorded under; use it to correlate a client-side result with a
row in the viewer. Upstream `Content-Type` is preserved. Proxied responses are returned with
`Cache-Control: no-cache` unless the upstream sets its own.

### A recorded sequence

The headers below reconstruct five consecutive calls from a real trace
(`session c90bcfe569dd44648a20f117d897b342`, run `6e4077f44c06`), showing how a
harness drives a multi-step task through the proxy.

The run opens each step with a stable session and run id, a per-step
`Debug-Label`, and a caller-generated `Req-Id`:

```http
POST /v1/chat/completions
X-LLMTrace-Session: c90bcfe569dd44648a20f117d897b342
X-LLMTrace-Run: 6e4077f44c06
X-LLMTrace-Purpose: task
X-LLMTrace-Req-Id: eb32e4a99db948f2a772f951abec200f
X-LLMTrace-Debug-Label: CLARIFY:ANSWER_GEN:0******
```

The step's outcome is not known when the request is sent, so the harness
finalizes the displayed title once the response has been read:

```http
POST /_llmtrace/update-title
Content-Type: application/json

{"req_id":"eb32e4a99db948f2a772f951abec200f",
 "title":"CLARIFY:ANSWER_GEN:0:TOOL_CALLS:GET_TOC_HEADINGS+GET_TOC_HEADINGS+GET_TOC_HEADINGS"}
```

Step names in this run were written in Russian; they are shown translated
throughout this section. That is exactly the case percent-encoding exists for —
an HTTP header cannot carry non-ASCII text raw, so such a label must be encoded
and declared as encoded. The value below is the run's original label:

```http
POST /v1/chat/completions
X-LLMTrace-Session: c90bcfe569dd44648a20f117d897b342
X-LLMTrace-Run: 6e4077f44c06
X-LLMTrace-Req-Id: d858739d95c5452aa590c021faaf5b48
X-LLMTrace-Debug-Label: EXECUTE:%D0%A1%D0%BE%D0%B1%D1%80%D0%B0%D1%82%D1%8C%20%D0%BF%D0%B5%D1%80%D0%B5%D1%87%D0%B5%D0%BD%D1%8C:ANSWER_GEN:0******
X-LLMTrace-Debug-Label-Encoding: percent
```

Every response carries the id the call was stored under:

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-LLMTrace-Call: 10943
```

Note what this run does *not* send. No request sets `X-LLMTrace-Branch`,
`X-LLMTrace-Base-State`, `X-LLMTrace-Prev-Req-Id` or `X-LLMTrace-Group` — the
harness supplies identity and lets Logger infer structure. Call `10943` still
landed on its own branch, `run-6e4077f44c06~parallel-2`, which no header asked
for: a lane forks whenever another call on the same branch root is still
recorded as `running`. Here `10943` arrived 0.57s after `10942` finished
streaming — close enough that the earlier call had not yet been marked
complete. Inference like this is timing-sensitive by nature; supplying
`X-LLMTrace-Prev-Req-Id` on each request replaces it with a tree the caller
states outright.

#### How those headers surface in the viewer

The same two calls, as the interface renders them. Header values do not appear
verbatim — each one is placed where it answers a different question.

`X-LLMTrace-Title` (or, absent one, `X-LLMTrace-Debug-Label`) leads the Timeline
row; the call id and branch sit beneath it as secondary text, and each call
contributes an input row and an output row:

```
EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0:TOOL_CALLS:GET_TEXT_SUMMARY+GET_TEXT_SUMMARY
#10944 · run-6e4077f44c06          → input    17,924 in · sent
EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0:TOOL_CALLS:GET_TEXT_SUMMARY+GET_TEXT_SUMMARY
#10944 · run-6e4077f44c06          ← output   355 out · 18,279 total · 26 tok/s · ok
```

The Branch view answers "which step is this", so it leads with the raw
`Debug-Label` instead of the finalized title — the label names the step, while
the title reports the outcome:

```
● EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0******
  #10943 · run-6e4077f44c06~parallel-2
● EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0******
  #10944 · run-6e4077f44c06
```

`X-LLMTrace-Req-Id` is never used as the visible call number — the `#10944` is
the proxy's own durable id, since caller ids are usually opaque hashes. The
req id instead opens the pane header, followed by the state this request
produced and where its parent came from:

```
31ca751774f7454abebb5054a46e22b0 · state S10189 ← inferred · 50%
```

Compare call `10943`, which opened the parallel lane and had no parent to match
against:

```
d858739d95c5452aa590c021faaf5b48 · state S10188 ← root
```

That `← inferred · 50%` is the honest reading of the run: nothing declared the
lineage, so Logger matched on request similarity and says so. Had the caller
sent `X-LLMTrace-Prev-Req-Id`, the same slot would read `← req <that id>` with
no percentage, because a declared predecessor is not a guess.

The Metadata tab shows the stored record as YAML, where the headers appear
under their stored names:

```yaml
id: 10944
created_at: 2026-08-29T01:41:57.895683+00:00
session_id: c90bcfe569dd44648a20f117d897b342
status: ok
chronological_parent_id: 10943
request_state_id: 10189
parent_state_id: 10188
parent_source: inferred
similarity: 0.5
req_id: 31ca751774f7454abebb5054a46e22b0
debug_label: EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0******
duration_ms: 21645.344
endpoint: /v1/chat/completions
http_status: 200
stream_chunks: 249
title: EXECUTE:Collect list of buildings and structures:ANSWER_GEN:0:TOOL_CALLS:GET_TEXT_SUMMARY+GET_TEXT_SUMMARY
usage:
  input_per_second: 746.7472859442461
  input_tokens: 17924
  output_per_second: 25.777457962536612
  output_tokens: 355
  total_tokens: 18279
```

Two things are worth noticing here. The percent-encoding is gone — a label sent
with `X-LLMTrace-Debug-Label-Encoding: percent` is decoded on the way in and
stored and displayed as text. And `X-LLMTrace-Run` is deliberately omitted from
this tab: the run id is already carried by the branch name, so repeating it adds
nothing.

## Viewer

The browser interface has four coordinated panes:

1. **Timeline** shows input and output phases, branches, checkpoints, status, and duration.
   A **List / Branches** switch toggles between the linear event list and a branching graph of
   the call tree. In the graph each call is a node led by its step name (`X-LLMTrace-Debug-Label`),
   parents connect to children, and a parent with several children is a branch. The graph
   orients **vertically** (grows downward) or **horizontally** (grows rightward). Nodes lead
   with the step name (`X-LLMTrace-Debug-Label`), falling back to the call purpose when none is
   set. Clicking a node selects that call, exactly like a list item.
2. **Mixed trace** reconstructs the current state and marks accumulated changes in place.
3. **Exact state** shows the reconstructed state without historical replacement text.
4. **Updates** lists changes chronologically and never rewrites earlier update cards.

Input, input-parameter, thoughts, and output labels are visually separate from model content.
Message fields are rendered as semantic UI blocks instead of exposed YAML keys. Provider
reasoning is shown in its own **Thoughts** scope; **Output** contains only the final assistant
answer. Clicking a scope or update focuses and highlights the corresponding items in the other
panes. The exact pane also exposes parameter, raw-response, and metadata tabs.

The complete interaction and visual contract is documented in the
[`UI/UX specification`](ui-ux-specification.md).

## Example scenarios

Start the proxy, then run:

```bash
.venv/bin/python examples/sequential_live_history.py
.venv/bin/python examples/summarize_scenario.py
.venv/bin/python examples/notebook_v18_scenario.py --windows 2
```

The sequential-history example makes three real model calls and appends each returned
assistant message to the next request. Its final trace contains the sequence system, user,
assistant, user, assistant, user, assistant. It prints the generated session ID and every
exact live response.

The summarization example traces a main conversation, a side summarization call, compression,
and a return to the main branch. The notebook example records a small mixture of LLM and
application-state events based on the notebook v18 workflow.

## Notebook integration

Install this repository into the notebook kernel:

```python
%pip install -e /path/to/insequent_logger
```

Use a unique session ID for each notebook execution:

```python
from datetime import datetime, timezone
from openai import OpenAI

notebook_id = "guided-doc-indexing-v18"
run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
session_id = f"{notebook_id}:{run_id}"

client = OpenAI(
    base_url="http://127.0.0.1:8081/v1",
    api_key="local-no-key-required",
    default_headers={"X-LLMTrace-Session": session_id},
)
```

Send llama.cpp utility requests through the same proxy and attach the same session header so
all related activity remains grouped.

## Application events

Non-LLM pipeline state can be added to the timeline:

```http
POST /api/events
Content-Type: application/json
X-LLMTrace-Session: my-run
X-LLMTrace-Branch: main

{
  "event": "resolved_snapshot",
  "payload": {
    "window": 4,
    "decisions": {
      "288": {"kind": "H"}
    }
  }
}
```

Large strings, lists, and mappings are stored as content-addressed blobs. Similar versions may
be represented as validated deltas against a prior field of the same event kind.

## Storage retention

After a call or application event finishes, Logger checkpoints its SQLite database. When
the file exceeds `storage.max_mb`, it deletes complete oldest sessions and compacts the file.
A currently streaming session is never removed mid-call, so the file can temporarily exceed
the configured limit.

The Timeline **Clean from here** control permanently deletes the focused call and every older
call in that session. It previews the exact deletion count and requires confirmation. The next
surviving call becomes a new snapshot boundary; newer calls and other sessions remain intact. Cleaning is
blocked if any call that would be deleted is still running.

## Development and tests

The dependencies in `requirements.txt` include the packages needed by the test suite. Install
Chromium for the Playwright browser tests:

```bash
.venv/bin/python -m playwright install chromium
```

Run the full suite:

```bash
.venv/bin/python -m pytest -q
```

The browser tests cover timeline ordering, state reconstruction, cross-pane focus, live
updates, scrolling behavior, and retention of append-only update cards.

## Project structure

```text
insequent_logger/
├── insequent_logger/
│   ├── server.py       # Proxy, viewer, and JSON API
│   ├── store.py        # SQLite trace storage and reconstruction
│   ├── diffing.py      # Compact request/output differences
│   ├── protocol.py     # Streaming and provider-response handling
│   ├── notebook.py     # Notebook recording helper
│   └── static/         # Four-pane browser viewer
├── examples/
├── tests/
├── docs/
└── config.toml
```
