# Insequent

Insequent is a compact tracing proxy and browser viewer for sequential LLM calls. It records
how prompts, parameters, outputs, branches, and application state evolve without storing every
request as an unrelated full snapshot.

It sits between an OpenAI-compatible client and an upstream model server:

```text
Application → Insequent proxy → OpenAI-compatible model server
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

Clone the repository, create a virtual environment, and install the package:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Install the OpenAI Python client if you want to run the client example below:

```bash
.venv/bin/python -m pip install openai
```

## Quick start

The included [`insequent.toml`](insequent.toml) expects the upstream model server at
`http://127.0.0.1:8080` and starts Insequent at `http://127.0.0.1:8081`.

```bash
.venv/bin/python -m insequent_logger --config insequent.toml
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

Insequent also forwards llama.cpp-style utility routes such as `/apply-template`, `/tokenize`,
`/detokenize`, and `/health`.

## Configuration

```toml
[server]
host = "127.0.0.1"
port = 8081

[upstream]
url = "http://127.0.0.1:8080"

[storage]
path = "trace.llmtrace"
max_mb = 3

[defaults]
session_id = "unassigned"
branch_id = "main"
```

Command-line options can override the main settings:

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
  --config insequent.toml \
  --db experiment.llmtrace \
  --port 8090
```

## Trace headers

Clients can describe exact lineage using optional request headers:

| Header | Purpose |
| --- | --- |
| `X-LLMTrace-Session` | Groups calls into a selectable viewer session. |
| `X-LLMTrace-Branch` | Names the logical branch; defaults to `main`. |
| `X-LLMTrace-Purpose` | Adds a short call purpose such as `chat` or `summarize`. |
| `X-LLMTrace-Base-State` | Identifies the exact request-state parent. |
| `X-LLMTrace-Run` | Associates calls with a larger application run. |

If no base state is supplied, Insequent chooses the best recent parent and labels the
relationship as inferred.

### Parallel calls

If a request starts before the best-matching call on its branch finishes, Insequent
automatically forks it into a stable branch such as `main~parallel-2`. Later calls are routed
back to matching branches using request-state similarity and completed assistant-response
identity.

Explicit `X-LLMTrace-Branch` values remain branch roots. Supplying
`X-LLMTrace-Base-State` is recommended when the application already knows the precise parent.

## Viewer

The browser interface has four coordinated panes:

1. **Timeline** shows input and output phases, branches, checkpoints, status, and duration.
2. **Mixed trace** reconstructs the current state and marks accumulated changes in place.
3. **Exact state** shows the reconstructed state without historical replacement text.
4. **Updates** lists changes chronologically and never rewrites earlier update cards.

Input, input-parameter, and output labels are visually separate from model content. Clicking a
scope or update focuses and highlights the corresponding items in the other panes. The exact
pane also exposes parameter, raw-response, and metadata tabs.

The complete interaction and visual contract is documented in the
[`UI/UX specification`](ui-ux-specification.md).

## Example scenarios

Start the proxy, then run:

```bash
.venv/bin/python examples/summarize_scenario.py
.venv/bin/python examples/notebook_v18_scenario.py --windows 2
```

The first example traces a main conversation, a side summarization call, compression, and a
return to the main branch. The second records a small mixture of LLM and application-state
events based on the notebook v18 workflow.

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

After a call or application event finishes, Insequent checkpoints its SQLite database. When
the file exceeds `storage.max_mb`, it deletes complete oldest sessions and compacts the file.
A currently streaming session is never removed mid-call, so the file can temporarily exceed
the configured limit.

## Development and tests

Install the test dependencies and Chromium:

```bash
.venv/bin/python -m pip install pytest pytest-playwright
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
└── insequent.toml
```
