const FRONTEND_CONFIG = Object.freeze({
  branchGraph: Object.freeze({
    enabled: false,
    lanePitch: 31,
    overlapShift: 8,
    bandPitch: 32,
    rowGap: 14,
    edgeHoverWidth: 16,
  }),
  timeline: Object.freeze({
    minWidth: 205,
    otherPanesMinWidth: 780,
    storageKey: "insequent.timelinePaneWidth",
  }),
  mixedTrace: Object.freeze({
    maxCalls: 12,
    maxHistoricalChars: 250000,
    maxEntryChars: 1000,
  }),
});

const state = {
  selected: null,
  detail: null,
  tab: "state",
  details: new Map(),
  observer: null,
  session: null,
  latestSession: null,
  sessionsSignature: "",
  timelineSignature: "",
  lastTimelineKey: null,
  timelineItems: [],
  mixedSegmentDetails: [],
  mixedHistoryTruncated: false,
  liveBusy: false,
  followedUpdateKey: null,
  pendingSelection: null,
  followedUpdateTimer: null,
  followNewItems: false,
  updatesUserScrollVersion: 0,
  selectionVersion: 0,
  selectedPhase: "input",
  timelineFocus: null,
  checkpointKeys: new Set(),
  timelineView: "list",
  branchOrientation:
    localStorage.getItem("insequent.branchOrientation") === "horizontal"
      ? "horizontal"
      : "vertical",
};

const $ = id => document.getElementById(id);

function scalar(value) {
  if (value === null) return "null";
  if (typeof value === "string") {
    if (value.includes("\n")) {
      return `\n${value.split("\n").map(line => `  ${line}`).join("\n")}`;
    }
    return JSON.stringify(value);
  }
  return JSON.stringify(value);
}

function yaml(value, indent = 0) {
  const pad = " ".repeat(indent);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    return value.map(item => {
      if (item && typeof item === "object") {
        return `${pad}- ${yaml(item, indent + 2).trimStart()}`;
      }
      return `${pad}- ${scalar(item)}`;
    }).join("\n");
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return "{}";
    return entries.map(([key, item]) => {
      if (item && typeof item === "object") {
        return `${pad}${key}:\n${yaml(item, indent + 2)}`;
      }
      if (typeof item === "string" && item.includes("\n")) {
        return `${pad}${key}:\n${item.split("\n").map(line => `${pad}  ${line}`).join("\n")}`;
      }
      return `${pad}${key}: ${scalar(item)}`;
    }).join("\n");
  }
  return `${pad}${scalar(value)}`;
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    character => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character],
  );
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = new Error((await response.json()).error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function itemKey(item) {
  return `${item.type}:${item.id}`;
}

function focusedTimelineKey() {
  return state.timelineFocus?.key
    || (state.selected ? itemKey(state.selected) : null);
}

function responseValue(detail) {
  try {
    return JSON.parse(detail.response);
  } catch {
    return detail.response;
  }
}

function displayValue(value) {
  return typeof value === "string"
    ? value
    : JSON.stringify(value, null, 2);
}

function requestParameters(request) {
  return Object.fromEntries(
    Object.entries(request || {}).filter(([key]) => key !== "messages" && key !== "prompt"),
  );
}

function requestContent(request) {
  if (Array.isArray(request?.messages)) return { messages: request.messages };
  if (Object.hasOwn(request || {}, "prompt")) return { prompt: request.prompt };
  return request || {};
}

// Where each stored message begins inside the rendered content, derived from
// the serializer's own list structure: a message item is a line at the item
// indent starting with "- ", and deeper lines belong to it. Nothing here looks
// at message text, so two identical messages still get distinct spans.
function messageItemSpans(text, itemIndent) {
  const marker = `${" ".repeat(itemIndent)}- `;
  const spans = [];
  let cursor = 0;
  while (cursor <= text.length) {
    const lineEnd = text.indexOf("\n", cursor);
    if (text.startsWith(marker, cursor)) {
      // End the previous span before the line break, so a mark never spills
      // onto the next message's line.
      if (spans.length) spans[spans.length - 1][1] = Math.max(cursor - 1, 0);
      spans.push([cursor, text.length]);
    }
    if (lineEnd < 0) break;
    cursor = lineEnd + 1;
  }
  return spans;
}

// A payload is placeable by recorded offset only while the pane renders it
// verbatim. Structured values are reformatted for display, which invalidates
// every stored offset, so those fall back to content matching.
function payloadAnchors(label, value, rendered) {
  const verbatim = typeof value === "string" && rendered === value;
  return { payloadStart: label.length, verbatim, messageSpans: null };
}

function stateDisplayParts(detail) {
  const parameters = requestParameters(detail.request);
  const topParameterText = Object.keys(parameters).length
    ? yaml({ input_params: parameters })
    : "input_params: {}";
  const parameterText = Object.keys(parameters).length
    ? yaml({ input_params: parameters }, 2)
    : "  input_params: {}";
  const hasPrompt = Object.hasOwn(detail.request || {}, "prompt");
  const promptLabel = "  prompt:\n";
  const promptRendered = hasPrompt ? displayValue(detail.request.prompt) : "";
  const contentText = hasPrompt
    ? `${promptLabel}${promptRendered}`
    : yaml(requestContent(detail.request), 2);
  const thoughtsLabel = "thoughts:\n";
  const outputLabel = "output:\n";
  const output = responseValue(detail);
  const outputRendered = displayValue(output);
  return {
    parameters,
    topParameterText,
    parameterText,
    contentText,
    contentAnchors: hasPrompt
      ? payloadAnchors(promptLabel, detail.request.prompt, promptRendered)
      : {
          payloadStart: null,
          verbatim: false,
          messageSpans: messageItemSpans(contentText, 4),
        },
    thoughtsText: `${thoughtsLabel}${detail.thoughts || ""}`,
    thoughtsAnchors: payloadAnchors(
      thoughtsLabel,
      detail.thoughts || "",
      detail.thoughts || "",
    ),
    outputText: `${outputLabel}${outputRendered}`,
    outputAnchors: payloadAnchors(outputLabel, output, outputRendered),
  };
}

function stateScopeHtml(kind, html, nested = false) {
  const labels = {
    input: "Input",
    "input-params": "Input parameters",
    thoughts: "Thoughts",
    output: "Output",
  };
  const sourceLabels = {
    input: "input",
    "input-params": "input_params",
    thoughts: "thoughts",
    output: "output",
  };
  const sourceLabel = sourceLabels[kind];
  const labelPattern = new RegExp(`^\\s*${sourceLabel}:(?: |\\n)?`);
  const content = String(html).replace(labelPattern, "");
  return `<span class="state-scope ${nested ? "state-subscope " : ""}trace-kind-${kind}" data-state-scope="${kind}"><span class="state-scope-label" role="button" tabindex="0" aria-label="${labels[kind]} scope">${labels[kind]}</span><span class="state-scope-content">${content}</span></span>`;
}

function messageStructureHtml(html, includeListLabel = true) {
  let content = String(html);
  if (includeListLabel) {
    content = content.replace(
      /^[ \t]*messages:\n?/,
      '<span class="message-list-label">Messages</span>\n',
    );
  }
  // A structural key can now be preceded by an update mark that opens on the
  // line, so keep any leading tags and relabel what follows them.
  content = content
    .replace(
      /^((?:<[^>]+>)*)[ \t]*-[ \t]+content:(?: )?/gm,
      '$1<span class="message-field-label">Content</span> ',
    )
    .replace(
      /^((?:<[^>]+>)*)[ \t]*role:(?: )?/gm,
      '$1<span class="message-field-label message-role-label">Role</span> ',
    )
    .replace(
      /(<span class="message-field-label[^"]*">[^<]+<\/span>) &quot;/g,
      "$1 ",
    )
    // A closing update-mark tag can now sit between the trailing quote and the
    // line end; the quote is still serializer syntax, not payload.
    .replace(/&quot;(?=(?:<\/[^>]+>)*(?:\n|$))/gm, "");
  return content;
}

function inputContentHtml(detail, html) {
  if (Object.hasOwn(detail?.request || {}, "prompt")) {
    const content = String(html).replace(/^\s*prompt:(?: |\n)?/, "");
    return `<span class="state-field state-prompt" data-input-field="prompt"><span class="state-field-label">Prompt</span><span class="state-field-content">${content}</span></span>`;
  }
  if (Array.isArray(detail?.request?.messages)) {
    return `<span class="message-list">${messageStructureHtml(html)}</span>`;
  }
  return html;
}

function checkpointInputHtml(detail, input) {
  if (Array.isArray(detail?.request?.messages)) {
    return `<span class="message-list checkpoint-message-list">${messageStructureHtml(escapeHtml(yaml(input)))}</span>`;
  }
  if (!Object.hasOwn(detail?.request || {}, "prompt")) {
    return `<pre>${escapeHtml(displayValue(input))}</pre>`;
  }
  return `<span class="state-field checkpoint-field state-prompt" data-input-field="prompt"><span class="state-field-label">Prompt</span><pre class="state-field-content">${escapeHtml(displayValue(detail.request.prompt))}</pre></span>`;
}

function firstUsefulLine(value) {
  return String(value ?? "").split("\n").find(line => line.trim())?.trim() || "";
}

function firstSearchableValue(value) {
  if (typeof value === "string") return firstUsefulLine(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstSearchableValue(item);
      if (found) return found;
    }
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) {
      const found = firstSearchableValue(item);
      if (found) return found;
    }
  } else if (value != null) {
    return String(value);
  }
  return "";
}

function searchableLines(value) {
  if (typeof value !== "string") {
    const first = firstSearchableValue(value);
    return first ? [first] : [];
  }
  return value.split("\n").map(line => line.trim()).filter(Boolean);
}

function markedValue(marker, value, indent = 0) {
  const pad = " ".repeat(indent);
  if (typeof value === "string" && value.includes("\n")) {
    return `${pad}${marker}\n${value.split("\n").map(line => `${pad}  ${line}`).join("\n")}`;
  }
  if (value && typeof value === "object") {
    return `${pad}${marker}\n${yaml(value, indent + 2)}`;
  }
  return `${pad}${marker} ${scalar(value)}`;
}

function operationName(operation) {
  return ({ "+": "Added", "-": "Removed", "~": "Changed" })[operation] || "Changed";
}

function traceKind(category) {
  if (category === "parameter") return "input-params";
  return category || "content";
}

function traceOperation(operation) {
  return ({ "+": "added", "-": "removed", "~": "changed" })[operation]
    || operation
    || "changed";
}

function transitionText(change) {
  if (change.op === "+") return markedValue("+", change.value);
  if (change.op === "-") return markedValue("−", change.value);
  const oldText = yaml(change.old);
  const newText = yaml(change.new);
  if (!oldText.includes("\n") && !newText.includes("\n")) {
    return `${oldText} → ${newText}`;
  }
  const from = oldText.split("\n").map(line => `  ${line}`).join("\n");
  const to = newText.split("\n").map(line => `  ${line}`).join("\n");
  return `from:\n${from}\n→ to:\n${to}`;
}

function collectParameterUpdates(changes, path = []) {
  const entries = [];
  for (const [field, change] of Object.entries(changes || {})) {
    const fieldPath = [...path, field];
    if (change.fields) {
      entries.push(...collectParameterUpdates(change.fields, fieldPath));
      continue;
    }
    const current = change.op === "-" ? null : change.value ?? change.new;
    entries.push({
      label: `${operationName(change.op)} parameter · ${fieldPath.join(".")}`,
      text: transitionText(change),
      oldText: change.op === "+" ? "" : yaml(change.old ?? change.value),
      newText: change.op === "-" ? "" : yaml(change.new ?? change.value),
      mixedOldText: change.op === "-"
        ? `${fieldPath.join(".")}: ${yaml(change.value)}`
        : yaml(change.old),
      needle: firstSearchableValue(current ?? field),
      scope: "input",
      category: "parameter",
      operation: change.op,
    });
  }
  return entries;
}

function appendResponseUpdate(entries, detail, {
  value,
  diff,
  scope,
  category,
  noun,
}) {
  if (value && diff.mode === "diff" && diff.changes?.length) {
    const fragments = diff.changes.filter(fragment => fragment.old || fragment.new);
    entries.push({
      label: `Changed ${noun} · from call #${diff.base_call_id}`,
      text: "",
      oldText: "",
      newText: "",
      fragments,
      needles: fragments.flatMap(fragment => searchableLines(fragment.new)),
      needle: "",
      scope,
      category,
      operation: "~",
    });
  } else if (value && diff.mode !== "unchanged") {
    const renderedValue = displayValue(value);
    entries.push({
      label: `Added ${noun}`,
      text: `+ ${renderedValue}`,
      oldText: "",
      newText: renderedValue,
      needle: firstSearchableValue(value),
      wholeScope: true,
      scope,
      category,
      operation: "+",
    });
  }
}

function updateEntries(detail) {
  const diff = detail.diff || {};
  // Parameters are the request controls, so they lead the update block.
  const entries = collectParameterUpdates(diff.parameters);
  for (const change of diff.messages || []) {
    if (change.op === "=") continue;
    const oldMessages = change.old_messages || [];
    const newMessages = change.new_messages || change.messages || [];
    // The hunk records which message indexes it touches; that index is the
    // position, so identical messages never collapse onto one another.
    const newStartIndex = Number.isFinite(change.new?.[0]) ? change.new[0] : null;
    if (change.op === "-") {
      for (const message of oldMessages) {
        entries.push({
          label: `Removed input · ${message.role || "message"}`,
          text: markedValue("−", message),
          oldText: yaml(message),
          newText: "",
          mixedOldText: yaml(message),
          messageIndex: newStartIndex,
          anchorNeedle: firstSearchableValue(
            detail.request?.messages?.[change.new?.[0]]?.content,
          ),
          needle: "",
          scope: "input",
          category: "input",
          operation: "-",
        });
      }
      continue;
    }
    if (change.op === "~") {
      const count = Math.max(oldMessages.length, newMessages.length);
      for (let index = 0; index < count; index += 1) {
        const oldMessage = oldMessages[index];
        const newMessage = newMessages[index];
        if (!oldMessage || !newMessage) {
          const message = newMessage || oldMessage;
          const operation = newMessage ? "+" : "-";
          entries.push({
            label: `${operationName(operation)} input · ${message.role || "message"}`,
            text: markedValue(operation === "+" ? "+" : "−", message),
            oldText: oldMessage ? yaml(oldMessage) : "",
            newText: newMessage ? yaml(newMessage) : "",
            mixedOldText: oldMessage ? yaml(oldMessage) : "",
            messageIndex: newStartIndex == null ? null : newStartIndex + index,
            needle: newMessage ? firstSearchableValue(newMessage.content) : "",
            needles: newMessage ? searchableLines(newMessage.content) : [],
            scope: "input",
            category: "input",
            operation,
          });
        } else {
          entries.push({
            label: `Changed input · ${newMessage.role || oldMessage.role || "message"}`,
            text: transitionText({ op: "~", old: oldMessage, new: newMessage }),
            oldText: yaml(oldMessage),
            newText: yaml(newMessage),
            mixedOldText: firstSearchableValue(oldMessage.content),
            messageIndex: newStartIndex == null ? null : newStartIndex + index,
            needle: firstSearchableValue(newMessage.content),
            needles: searchableLines(newMessage.content),
            scope: "input",
            category: "input",
            operation: "~",
          });
        }
      }
      continue;
    }
    newMessages.forEach((message, index) => {
      entries.push({
        label: `Added input · ${message.role || "message"}`,
        text: markedValue("+", message),
        oldText: "",
        newText: yaml(message),
        messageIndex: newStartIndex == null ? null : newStartIndex + index,
        needle: firstSearchableValue(message.content),
        needles: searchableLines(message.content),
        scope: "input",
        category: "input",
        operation: "+",
      });
    });
  }
  const promptHunks = diff.prompt?.hunks || [];
  for (const hunk of promptHunks) {
    const hasAdded = Object.hasOwn(hunk, "+");
    const hasRemoved = Object.hasOwn(hunk, "-");
    if (!hasAdded && !hasRemoved) continue;
    // Only a hunk the differ itself recorded as a replacement is one. Pairing a
    // removal with a later insertion because they sit near each other invents a
    // transition between unrelated lines, and binds both to one identity so
    // focusing the addition also lights up the removal.
    const operation = hasAdded && hasRemoved ? "~" : hasAdded ? "+" : "-";
    // The full removed text belongs to the call that removed it, so Mixed can
    // show what was removed in full rather than truncated to a one-line preview.
    const removed = hunk["-"]?.text ?? hunk["-"]?.preview ?? "";
    const added = hunk["+"] ?? "";
    entries.push({
      label: `${operationName(operation)} prompt`,
      location: promptLocationLabel(hunk, operation),
      promptLine: Number.isFinite(hunk.at_new) ? hunk.at_new : null,
      text: operation === "~"
        ? transitionText({ op: "~", old: removed, new: added })
        : markedValue(operation === "+" ? "+" : "−", hasAdded ? added : removed),
      oldText: hasRemoved ? removed : "",
      newText: hasAdded ? added : "",
      mixedOldText: removed,
      needle: hasAdded ? firstUsefulLine(added) : "",
      needles: hasAdded ? searchableLines(added) : [],
      scope: "input",
      category: "input",
      operation,
    });
  }
  appendResponseUpdate(entries, detail, {
    value: detail.thoughts || "",
    diff: detail.thoughts_diff || { mode: "snapshot" },
    scope: "thoughts",
    category: "thoughts",
    noun: "thoughts",
  });
  appendResponseUpdate(entries, detail, {
    value: responseValue(detail),
    diff: detail.output_diff || { mode: "snapshot" },
    scope: "output",
    category: "output",
    noun: "output",
  });
  const occurrences = new Map();
  return entries.map((entry, entryIndex) => {
    const identity = {
      entryIndex,
      entryKey: `${detail.id}:${entryIndex}`,
      callId: detail.id,
    };
    if (!entry.needle) return { ...entry, ...identity, occurrence: 0 };
    const key = `${entry.scope}:${entry.needle}`;
    const occurrence = occurrences.get(key) || 0;
    occurrences.set(key, occurrence + 1);
    return { ...entry, ...identity, occurrence };
  });
}

function retainedContentRatio(detail) {
  const diff = detail?.diff || {};
  if (diff.mode !== "diff") return 0;
  if (Array.isArray(diff.messages)) {
    let retained = 0;
    let oldTotal = 0;
    let newTotal = 0;
    for (const change of diff.messages) {
      const oldRange = change.old || [0, 0];
      const newRange = change.new || [0, 0];
      oldTotal = Math.max(oldTotal, oldRange[1] || 0);
      newTotal = Math.max(newTotal, newRange[1] || 0);
      if (change.op === "=") retained += Math.max(0, oldRange[1] - oldRange[0]);
    }
    const total = Math.max(oldTotal, newTotal);
    return total ? retained / total : 1;
  }
  if (Array.isArray(diff.prompt?.hunks)) {
    let retained = 0;
    let oldTotal = 0;
    let newTotal = 0;
    for (const hunk of diff.prompt.hunks) {
      if (hunk["="]) {
        const count = Number.parseInt(hunk["="], 10) || 0;
        retained += count;
        oldTotal += count;
        newTotal += count;
      } else {
        oldTotal += hunk["-"]?.lines || 0;
        if (Object.hasOwn(hunk, "+")) newTotal += String(hunk["+"]).split("\n").length;
      }
    }
    const total = Math.max(oldTotal, newTotal);
    return total ? retained / total : 1;
  }
  return 1;
}

function isCheckpoint(detail) {
  if (!detail || detail.diff?.mode !== "diff") return true;
  const jumpedFromChronologicalState = detail.request_state_id != null
    && detail.parent_state_id != null
    && detail.chronological_parent_state_id != null
    && Number(detail.request_state_id) !== Number(detail.chronological_parent_state_id)
    && Number(detail.parent_state_id) !== Number(detail.chronological_parent_state_id);
  if (
    jumpedFromChronologicalState
    && detail.chronological_similarity != null
  ) {
    return Number(detail.chronological_similarity) < 0.20;
  }
  return retainedContentRatio(detail) < 0.20;
}

function requestHasUpdates(diff = {}) {
  if (Object.keys(diff.parameters || {}).length) return true;
  if (diff.prompt && diff.prompt.op !== "=") return true;
  return (diff.messages || []).some(change => change.op !== "=");
}

function identicalBaseCall(detail) {
  if (detail.output_diff?.mode !== "unchanged") return null;
  if (detail.output_parent_same_request) return detail.output_parent_call_id || null;
  if (detail?.diff?.mode !== "diff" || requestHasUpdates(detail.diff)) return null;
  return detail.output_parent_call_id || detail.chronological_parent_id || null;
}

async function resolveOutputParentRequestIdentity(detail) {
  if (
    typeof detail.output_parent_same_request === "boolean"
    || detail.output_diff?.mode !== "unchanged"
    || !detail.output_parent_call_id
  ) {
    return;
  }
  try {
    const parent = await detailFor("call", detail.output_parent_call_id);
    detail.output_parent_same_request =
      JSON.stringify(parent.request) === JSON.stringify(detail.request);
  } catch {
    // A pruned comparison call must not prevent this call from rendering.
    detail.output_parent_same_request = false;
  }
}

function requestsAreSimilar(previous, current) {
  if (!previous || !current) return false;
  const left = JSON.stringify(previous.request || {});
  const right = JSON.stringify(current.request || {});
  const longest = Math.max(left.length, right.length);
  if (!longest) return true;
  let prefix = 0;
  while (prefix < left.length && prefix < right.length && left[prefix] === right[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < left.length - prefix
    && suffix < right.length - prefix
    && left[left.length - suffix - 1] === right[right.length - suffix - 1]
  ) {
    suffix += 1;
  }
  return (prefix + suffix) / longest >= 0.55;
}

function entryRange(text, entry) {
  if (entry.wholeScope) {
    const label = `${entry.scope}:`;
    const boundary = text.startsWith(label) ? 0 : text.indexOf(`\n${label}`);
    if (boundary < 0) return null;
    const labelStart = boundary + (boundary ? 1 : 0);
    const valueStart = labelStart + label.length
      + (text[labelStart + label.length] === " " ? 1 : 0);
    return [valueStart, text.length];
  }
  if (!entry.needle) return null;
  const boundary = text.indexOf("\noutput:");
  const scopeStart = entry.scope === "output" && boundary >= 0 ? boundary : 0;
  const scopeEnd = entry.scope === "input" && boundary >= 0 ? boundary : text.length;
  const matches = [];
  let cursor = scopeStart;
  while (cursor < scopeEnd) {
    const index = text.indexOf(entry.needle, cursor);
    if (index < 0 || index >= scopeEnd) break;
    matches.push(index);
    cursor = index + Math.max(entry.needle.length, 1);
  }
  const start = matches[Math.min(entry.occurrence || 0, Math.max(matches.length - 1, 0))];
  return start == null ? null : [start, start + entry.needle.length];
}

// The store records where every change sits: prompt hunks carry their line,
// message hunks their index, response fragments their character offsets. Those
// positions are what place a fragment. Text matching cannot tell two identical
// lines apart, and cannot place a removed line at all.
function payloadLineOffset(text, anchors, line) {
  if (!Number.isFinite(line) || line < 1) return null;
  let cursor = anchors.payloadStart;
  for (let remaining = line - 1; remaining > 0; remaining -= 1) {
    const next = text.indexOf("\n", cursor);
    if (next < 0) return null;
    cursor = next + 1;
  }
  return cursor <= text.length ? cursor : null;
}

// Recorded positions describe the call that produced them. An entry replayed
// from an earlier call in the segment describes that call's text, so its
// presence in the current state stays a content question.
function recordedRange(text, entry, anchors) {
  if (!anchors || entry.fromEarlierCall) return null;
  if (entry.messageIndex != null && anchors.messageSpans) {
    const span = anchors.messageSpans[entry.messageIndex];
    if (!span) return null;
    return entry.operation === "-" ? [span[0], span[0]] : [span[0], span[1]];
  }
  if (!anchors.verbatim || anchors.payloadStart == null) return null;
  if (entry.promptLine != null) {
    const start = payloadLineOffset(text, anchors, entry.promptLine);
    if (start == null) return null;
    if (entry.operation === "-") return [start, start];
    const added = entry.newText || "";
    if (!added || !text.startsWith(added, start)) return null;
    return [start, start + added.length];
  }
  if (entry.newStart != null && entry.newEnd != null) {
    const start = anchors.payloadStart + entry.newStart;
    const end = anchors.payloadStart + entry.newEnd;
    if (end > text.length) return null;
    return [start, end];
  }
  return null;
}

function entryRanges(text, entry, anchors = null) {
  const recorded = recordedRange(text, entry, anchors);
  if (recorded) return [recorded];
  if (!entry.needles?.length) {
    const range = entryRange(text, entry);
    return range ? [range] : [];
  }
  const boundary = text.indexOf("\noutput:");
  const scopeStart = entry.scope === "output" && boundary >= 0 ? boundary : 0;
  const scopeEnd = entry.scope === "input" && boundary >= 0 ? boundary : text.length;
  const ranges = [];
  let cursor = scopeStart;
  // Two entries of one call can carry the same text. On the content-matching
  // path they must still land on different occurrences of it.
  for (let skipped = entry.occurrence || 0; skipped > 0; skipped -= 1) {
    const at = text.indexOf(entry.needles[0], cursor);
    if (at < 0 || at >= scopeEnd) break;
    cursor = at + Math.max(entry.needles[0].length, 1);
  }
  for (const needle of entry.needles) {
    const start = text.indexOf(needle, cursor);
    if (start < 0 || start >= scopeEnd) continue;
    ranges.push([start, start + needle.length]);
    cursor = start + needle.length;
  }
  return ranges;
}

function changePartHtml(kind, text, category, inline = false, attributes = "") {
  const tag = kind === "removed" ? "del" : "ins";
  const classes = [
    `${kind}-part`,
    `${category || "content"}-update`,
    "trace-part",
    `trace-kind-${traceKind(category)}`,
    `trace-op-${kind}`,
    inline ? "inline-part inline-update" : "",
  ].filter(Boolean).join(" ");
  return `<${tag} class="${classes}"${attributes ? ` ${attributes}` : ""}>${escapeHtml(text)}</${tag}>`;
}

// Same convention as an output fragment's location, so identical prompt text at
// two different places is distinguishable at a glance.
function promptLocationLabel(hunk, operation) {
  const oldLine = hunk.at_old;
  const newLine = hunk.at_new;
  if (operation === "-") return Number.isFinite(oldLine) ? `old ${oldLine}` : "";
  if (operation === "+") return Number.isFinite(newLine) ? `new ${newLine}` : "";
  if (!Number.isFinite(oldLine) || !Number.isFinite(newLine)) return "";
  return oldLine === newLine ? `line ${newLine}` : `old ${oldLine} → new ${newLine}`;
}

function updateEntryHtml(entry) {
  const category = entry.category || "content";
  const location = entry.location
    ? `<span class="fragment-location">${escapeHtml(entry.location)}</span>`
    : "";
  if (entry.fragments) {
    const grouped = new Map();
    entry.fragments.forEach((fragment, index) => {
      const key = `${fragment.op}\u0000${fragment.old}\u0000${fragment.new}`;
      const existing = grouped.get(key);
      const location = {
        oldLine: fragment.old_line,
        newLine: fragment.new_line,
      };
      if (existing) {
        existing.count += 1;
        existing.locations.push(location);
        existing.indices.push(index);
      } else {
        grouped.set(key, {
          ...fragment,
          count: 1,
          locations: [location],
          indices: [index],
        });
      }
    });
    return `<div class="output-fragments">${[...grouped.values()].map(fragment => {
      const locations = [...new Set(fragment.locations.map(location => {
        if (fragment.op === "-") return `old ${location.oldLine}`;
        if (fragment.op === "+") return `new ${location.newLine}`;
        return location.oldLine === location.newLine
          ? `line ${location.newLine}`
          : `old ${location.oldLine} → new ${location.newLine}`;
      }))];
      const shownLocations = locations.slice(0, 8).join(", ");
      const remaining = locations.length - 8;
      const lineLabel = `${shownLocations}${remaining > 0 ? `, … ${remaining} more` : ""}`;
      const removed = fragment.old
        ? changePartHtml("removed", fragment.old, category)
        : "";
      const added = fragment.new
        ? changePartHtml("added", fragment.new, category)
        : "";
      const arrow = removed && added ? '<span class="change-arrow"> → </span>' : "";
      const count = fragment.count > 1
        ? `<span class="fragment-count">× ${fragment.count}</span>`
        : "";
      return `<div class="fragment-change" role="button" tabindex="0" data-fragment-indices="${fragment.indices.join(",")}">
        <span class="fragment-location">${escapeHtml(lineLabel)}</span>
        ${removed}${arrow}${added}${count}
      </div>`;
    }).join("")}</div>`;
  }
  if (entry.operation === "~") {
    return `<div class="change-pair">${location}${
      changePartHtml("removed", entry.oldText || "", category)
    }<span class="change-arrow"> → </span>${
      changePartHtml("added", entry.newText || "", category)
    }</div>`;
  }
  const kind = entry.operation === "-" ? "removed" : "added";
  const text = entry.operation === "-"
    ? entry.oldText || entry.text
    : entry.newText || entry.text;
  return `<div class="single-change">${location}${changePartHtml(kind, text, category)}</div>`;
}

function updateEntryBodyHtml(entry) {
  const html = updateEntryHtml(entry);
  return entry.scope === "input" ? messageStructureHtml(html, false) : html;
}

function unchangedOutputNoticeHtml(detail) {
  if (detail.output_diff?.mode !== "unchanged") return "";
  const baseCallId = detail.output_diff.base_call_id;
  return `
    <div class="update-unchanged-output trace-kind-output" data-update-scope="output" role="button" tabindex="0">
      <strong>Unchanged output</strong>
      <small>${baseCallId == null
        ? "Same output as its comparison call"
        : `Same output as call #${escapeHtml(baseCallId)}`}</small>
    </div>`;
}

// A call can carry no input update while its output changed. Without a row of
// its own the input event would fall back to the whole card and pulse the
// output update, which reads as "the input click focused an output change".
//
// A parallel lane forks with no parent state, so nothing it could have changed
// from exists: the diff baseline is a concurrent sibling picked by arrival
// order, not an ancestor. Such a lane names no call at all — a reference would
// contradict the very absence the row reports — and never says "unchanged".
function parallelLaneInput(detail) {
  return detail.parent_source === "parallel"
    || detail.input_parent_source === "sibling";
}

// With no previous state, nothing in the lane's request is a change — so the
// card shows the request itself, the way a snapshot card shows one, instead of
// leaving the moment unrepresented.
function laneInputSnapshotHtml(detail) {
  const parameters = requestParameters(detail.request);
  return `
    <div class="lane-snapshot">
      <section class="lane-snapshot-section trace-kind-input" data-lane-scope="input">
        <strong>Input</strong>
        ${checkpointInputHtml(detail, requestContent(detail.request))}
      </section>
      ${Object.keys(parameters).length ? `
        <section class="lane-snapshot-section trace-kind-input-params" data-lane-scope="input-params">
          <strong>Parameters</strong>
          <pre>${escapeHtml(yaml(parameters))}</pre>
        </section>` : ""}
    </div>`;
}

function unchangedInputNoticeHtml(detail, entries) {
  if (entries.some(entry => entry.scope === "input")) return "";
  const baseCallId = detail.input_parent_call_id;
  if (parallelLaneInput(detail)) {
    return `
      <div class="update-unchanged-input trace-kind-input" data-update-scope="input" role="button" tabindex="0">
        <strong>Parallel lane input</strong>
        <small>No previous state to compare</small>
      </div>`;
  }
  return `
    <div class="update-unchanged-input trace-kind-input" data-update-scope="input" role="button" tabindex="0">
      <strong>Unchanged input</strong>
      <small>${baseCallId == null
        ? "Same input as its comparison call"
        : `Same input as call #${escapeHtml(baseCallId)}`}</small>
    </div>`;
}

function limitedMixedTraceText(value) {
  const text = String(value || "");
  const limit = FRONTEND_CONFIG.mixedTrace.maxEntryChars;
  return text.length <= limit
    ? text
    : `${text.slice(0, limit)}\n… retained trace text truncated …`;
}

function mixedStateHtml(text, entries, anchors = null) {
  const boundary = text.indexOf("\noutput:");
  // Text that some entry explicitly removed. A removal owns its removed text and
  // links it back to the call that did the removing, so a later re-drawing of
  // that same text (an earlier addition now gone) would only duplicate it and,
  // worse, point removed text at the call that ADDED it. So an addition-now-gone
  // is shown as removed history only when no real removal already covers it —
  // which keeps grow-never-lose while fixing the del→added-side mismatch.
  const removedByEntry = new Set();
  for (const entry of entries) {
    if (entry.operation === "-" || entry.operation === "~") {
      const removed = entry.oldText || entry.mixedOldText || entry.text || "";
      if (removed) removedByEntry.add(removed);
    }
  }
  const changes = [];
  for (const entry of entries) {
    if (entry.operation === "-") {
      // Removed text is placed where the store says it used to be, so it stays
      // in its own neighbourhood instead of being parked at the scope end.
      const recorded = recordedRange(text, entry, anchors);
      let position = recorded ? recorded[0] : -1;
      if (position < 0 && entry.anchorNeedle) position = text.indexOf(entry.anchorNeedle);
      if (position < 0) {
        position = entry.scope === "input" && boundary >= 0 ? boundary : text.length;
      }
      changes.push({ start: position, end: position, entry });
      continue;
    }
    const ranges = entryRanges(text, entry, anchors);
    ranges.forEach((range, rangeIndex) => {
      changes.push({
        start: range[0],
        end: range[1],
        entry,
        firstRange: rangeIndex === 0,
      });
    });
    if (!ranges.length) {
      changes.push({
        start: text.length,
        end: text.length,
        entry,
        historical: true,
      });
    }
  }
  if (!changes.length) return escapeHtml(text);
  // A flat text can carry only one mark per character, so overlapping spans
  // compete — and the selected call's own change must win. An earlier call's
  // retained span, especially a whole-scope one, would otherwise swallow every
  // fragment of the call being looked at and leave it with no focus target.
  // Nothing is dropped: what loses is kept as retained history instead.
  const placed = [];
  const claimed = [];
  const ordered = [...changes].sort((left, right) => (
    (left.entry.fromEarlierCall ? 1 : 0) - (right.entry.fromEarlierCall ? 1 : 0)
    || left.start - right.start
    || left.end - right.end
  ));
  for (const change of ordered) {
    // A zero-width anchor claims nothing, so it can never collide.
    if (change.historical || change.start === change.end) {
      placed.push(change);
      continue;
    }
    const collides = claimed.some(
      span => change.start < span[1] && span[0] < change.end,
    );
    if (collides) {
      placed.push({ ...change, historical: true, start: text.length, end: text.length });
      continue;
    }
    claimed.push([change.start, change.end]);
    placed.push(change);
  }
  placed.sort((left, right) => left.start - right.start || left.end - right.end);
  let html = "";
  let cursor = 0;
  for (const change of placed) {
    if (change.start < cursor) {
      // An anchor inside an already-rendered span still deserves its place:
      // move it to the cursor rather than losing the fragment.
      if (change.start !== change.end) continue;
      change.start = cursor;
      change.end = cursor;
    }
    const { entry } = change;
    const category = entry.category || "content";
    html += escapeHtml(text.slice(cursor, change.start));
    const fragmentAttribute = entry.fragmentIndex == null
      ? ""
      : ` data-output-fragment="${entry.fragmentIndex}"`;
    const entryAttribute =
      `data-update-entry="${entry.entryKey}"${fragmentAttribute} role="button" tabindex="0"`;
    if (change.historical) {
      const oldText = limitedMixedTraceText(
        entry.oldText || entry.mixedOldText || "",
      );
      const newText = limitedMixedTraceText(
        entry.newText || entry.text || "",
      );
      // The selected call's own absent addition is still "added" (it is what the
      // call produced). An earlier call's absent addition is retained as removed
      // history — unless a real removal entry already shows that exact text, in
      // which case that removal owns it with the correct del↔del reference.
      const ownedByRemoval = entry.fromEarlierCall
        && newText
        && removedByEntry.has(newText);
      const showNew = newText && entry.operation !== "-" && !ownedByRemoval;
      const newKind = entry.fromEarlierCall ? "removed" : "added";
      html += "\n";
      if (entry.operation === "~" && oldText) {
        html += changePartHtml("removed", oldText, category, false, entryAttribute);
        if (showNew) html += '<span class="change-arrow inline-arrow"> → </span>';
      }
      if (showNew) {
        html += changePartHtml(newKind, newText, category, false, entryAttribute);
      }
      html += "\n";
    } else if (entry.operation === "-") {
      const removed = limitedMixedTraceText(
        entry.mixedOldText || entry.oldText || entry.text,
      );
      html += `\n${changePartHtml(
        "removed",
        removed,
        category,
        false,
        entryAttribute,
      )}\n`;
    } else {
      const current = text.slice(change.start, change.end);
      if (entry.operation === "~" && entry.mixedOldText && change.firstRange) {
        html += changePartHtml(
          "removed",
          limitedMixedTraceText(entry.mixedOldText),
          category,
          true,
          entryAttribute,
        );
        html += '<span class="change-arrow inline-arrow"> → </span>';
      }
      html += changePartHtml("added", current, category, true, entryAttribute);
    }
    cursor = change.end;
  }
  return html + escapeHtml(text.slice(cursor));
}

function mixedOutputEntries(entries) {
  return entries.flatMap(entry => {
    if (!entry.fragments) return [entry];
    return entry.fragments.map((fragment, fragmentIndex) => ({
      ...entry,
      fragments: null,
      fragmentIndex,
      operation: fragment.op,
      oldText: fragment.old || "",
      newText: fragment.new || "",
      mixedOldText: fragment.old || "",
      // The offsets the store recorded for this fragment, so two identical
      // fragments stay two fragments in Mixed as well as in Exact.
      newStart: fragment.new_start,
      newEnd: fragment.new_end,
      needle: firstUsefulLine(fragment.new),
      needles: searchableLines(fragment.new),
    }));
  });
}

function indentedOutputHtml(text) {
  return escapeHtml(text).replaceAll("\n", "\n  ");
}

function mixedOutputHtml(detail, outputEntry = null) {
  const output = String(detail.response ?? "");
  const diff = detail.output_diff || { mode: "snapshot" };
  if (!output) return 'output: ""';
  if (diff.mode === "unchanged") return `output:\n  ${indentedOutputHtml(output)}`;
  if (diff.mode !== "diff" || !diff.changes?.length) {
    const attribute = outputEntry
      ? `data-update-entry="${outputEntry.entryKey}" role="button" tabindex="0"`
      : "";
    return `output:\n  ${changePartHtml("added", output, "output", true, attribute)}`;
  }
  let html = "output:\n  ";
  let cursor = 0;
  diff.changes.forEach((change, index) => {
    html += indentedOutputHtml(output.slice(cursor, change.new_start));
    const entryAttribute = outputEntry == null
      ? ""
      : ` data-update-entry="${outputEntry.entryKey}"`;
    const fragmentAttribute =
      `data-output-fragment="${index}"${entryAttribute} role="button" tabindex="0"`;
    if (change.old) {
      html += changePartHtml("removed", change.old, "output", true, fragmentAttribute);
    }
    if (change.old && change.new) {
      html += '<span class="change-arrow inline-arrow"> → </span>';
    }
    if (change.new) {
      html += changePartHtml("added", change.new, "output", true, fragmentAttribute);
    }
    cursor = change.new_end;
  });
  return html + indentedOutputHtml(output.slice(cursor));
}

async function detailFor(type, id) {
  const key = `${type}:${id}`;
  if (!state.details.has(key)) {
    const request = fetchJson(`/api/calls/${id}`).catch(error => {
      if (state.details.get(key) === request) state.details.delete(key);
      throw error;
    });
    state.details.set(key, request);
  }
  return state.details.get(key);
}

function ensureObserver() {
  if (state.observer) return;
  state.observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) loadUpdateCard(entry.target);
    }
  }, { root: $("updates"), rootMargin: "500px" });
}

function phaseLabel(phase) {
  return phase === "output" ? "← output" : "→ input";
}

// The step's debug label rides on an existing line — the timeline item's action
// row and the update card's head — never a new one. It truncates rather than
// wraps, and is omitted entirely when absent.
function debugLabelHtml(label, className) {
  if (!label) return "";
  const text = escapeHtml(label);
  return `<span class="${className}" title="${text}">${text}</span>`;
}

function updateCardHeadHtml(id, phase, debugLabel = null) {
  return `<div class="update-card-head trace-kind-${phase}">
      <span class="update-card-phase">${phaseLabel(phase)}</span>
      <span class="update-card-id">LLM call #${escapeHtml(id)}</span>
      ${debugLabelHtml(debugLabel, "update-card-debug")}
    </div>`;
}

function createUpdateCard(item, phase) {
  ensureObserver();
  const card = document.createElement("article");
  card.className = `update-card update-card-${phase} trace-kind-${phase} loading`;
  card.dataset.key = itemKey(item);
  card.dataset.phase = phase;
  card.dataset.type = item.type;
  card.dataset.id = item.id;
  if (item.debug_label) card.dataset.debugLabel = item.debug_label;
  card.innerHTML = updateCardHeadHtml(item.id, phase, item.debug_label);
  state.observer.observe(card);
  return card;
}

function updateCardId(card) {
  return `${card.dataset.key}:${card.dataset.phase}`;
}

// One card per phase, laid out in timeline event order. Existing cards are
// moved rather than rebuilt, so loaded content and pending detail requests
// survive a reorder — an earlier call completing inserts its output card
// between later inputs without disturbing the rest.
function renderUpdateCards(items) {
  const updates = $("updates");
  const existing = new Map();
  updates.querySelectorAll(".update-card").forEach(card => {
    existing.set(updateCardId(card), card);
  });
  const ordered = timelinePhaseEvents(items).sort(compareTimelineEvents);
  const desired = [];
  for (const { item, phase } of ordered) {
    const id = `${itemKey(item)}:${phase}`;
    const card = existing.get(id) || createUpdateCard(item, phase);
    existing.delete(id);
    desired.push(card);
  }
  existing.forEach(card => {
    state.observer?.unobserve(card);
    card.remove();
  });
  // Reconcile order in place: a card that is already where it belongs is left
  // untouched. Re-inserting a node — even to the same spot — restarts every CSS
  // animation on it and its descendants, so a blanket rebuild would replay the
  // focus flash on every live refresh. Only genuinely out-of-order or new cards
  // are moved.
  let ref = updates.firstChild;
  for (const card of desired) {
    if (card === ref) {
      ref = ref.nextSibling;
    } else {
      updates.insertBefore(card, ref);
    }
  }
}

function updateCardFor(key, phase = "input") {
  return document.querySelector(
    `.update-card[data-key="${key}"][data-phase="${phase}"]`,
  ) || document.querySelector(`.update-card[data-key="${key}"]`);
}

function timelineCallBlocks(items) {
  const calls = items.map(item => {
    const startedAt = Date.parse(item.created_at);
    const duration = Number(item.duration_ms);
    const finishedAt = item.status === "running"
      ? Number.POSITIVE_INFINITY
      : Number.isFinite(duration) && Number.isFinite(startedAt)
        ? startedAt + Math.max(duration, 0)
        : startedAt;
    return { item, startedAt, finishedAt };
  }).sort((left, right) => (
    left.startedAt - right.startedAt
    || left.item.sequence - right.item.sequence
  ));

  const blocks = [];
  let block = null;
  for (const call of calls) {
    if (!block || call.startedAt >= block.finishedAt) {
      block = {
        calls: [],
        startedAt: call.startedAt,
        finishedAt: call.finishedAt,
      };
      blocks.push(block);
    }
    block.calls.push(call);
    block.finishedAt = Math.max(block.finishedAt, call.finishedAt);
  }
  return blocks;
}

// A call occupies two moments: the request leaves at created_at, the response
// lands duration_ms later. Timeline and Updates order the same event list, so
// both panes tell the same history — interleaved when calls overlap.
// An item without a usable timestamp still needs a defined position: park it at
// the end of history rather than letting NaN make the comparison inconsistent.
const UNDATED_EVENT_AT = 8.64e15;

function timelinePhaseEvents(items) {
  const events = [];
  for (const item of items) {
    const parsedStart = Date.parse(item.created_at);
    const dated = Number.isFinite(parsedStart);
    const startedAt = dated ? parsedStart : UNDATED_EVENT_AT;
    events.push({ item, phase: "input", at: startedAt, sortOrder: 3 });
    if (item.status !== "running") {
      const duration = Number(item.duration_ms);
      const completedAt = dated && Number.isFinite(duration)
        ? startedAt + Math.max(duration, 0)
        : startedAt + 1;
      events.push({ item, phase: "output", at: completedAt, sortOrder: 0 });
    }
  }
  return events;
}

function compareTimelineEvents(left, right) {
  return left.at - right.at
    || left.sortOrder - right.sortOrder
    || (left.item?.sequence || 0) - (right.item?.sequence || 0);
}

function renderTimelineEvents(items) {
  const events = timelinePhaseEvents(items);
  const callBlocks = timelineCallBlocks(items);
  for (const [index, block] of callBlocks.entries()) {
    if (block.calls.length < 2) continue;
    const branchCount = new Set(
      block.calls.map(({ item }) => item.branch_id || "main"),
    ).size;
    events.push({
      phase: "parallel-start",
      at: block.startedAt,
      sortOrder: 2,
      blockIndex: index,
      branchCount,
    });
    if (Number.isFinite(block.finishedAt)) {
      events.push({
        phase: "parallel-end",
        at: block.finishedAt,
        sortOrder: 1,
        blockIndex: index,
        branchCount,
      });
    }
  }
  events.sort(compareTimelineEvents);

  $("timeline").innerHTML = "";
  for (const event of events) {
    if (event.phase === "parallel-start" || event.phase === "parallel-end") {
      const divider = document.createElement("div");
      const edge = event.phase === "parallel-start" ? "start" : "end";
      divider.className = `timeline-parallel-divider parallel-${edge}`;
      divider.dataset.parallelBlock = String(event.blockIndex);
      divider.innerHTML = `
        <span>parallel ${edge}</span>
        <small>${event.branchCount} branches</small>`;
      $("timeline").appendChild(divider);
      continue;
    }
    const { item, phase } = event;
    const key = itemKey(item);
    const button = document.createElement("button");
    button.className = `timeline-item timeline-${phase} trace-kind-${phase} call`;
    button.dataset.phase = phase;
    if (phase === "input") {
      button.dataset.key = key;
      button.classList.toggle("checkpoint-call", state.checkpointKeys.has(key));
    } else {
      button.dataset.callKey = key;
    }
    const checkpoint = phase === "input" && state.checkpointKeys.has(key);
    button.innerHTML = `
      <span class="item-head">
        <span class="item-label">${
          phase === "input"
            ? checkpoint ? "→ new state input" : "→ input"
            : "← output"
        }</span>
        ${debugLabelHtml(item.debug_label, "item-debug")}
      </span>
      <span class="item-meta">
        <span>#${item.id} · <b class="branch">${escapeHtml(item.branch_id || "main")}</b></span>
        <span>${escapeHtml(phase === "input" ? "sent" : item.status)}</span>
      </span>`;
    button.classList.toggle(
      "active",
      focusedTimelineKey() === key
        && (state.timelineFocus?.phase || state.selectedPhase) === phase,
    );
    button.onclick = () => selectItem(item.type, item.id, button);
    $("timeline").appendChild(button);
  }
  applyBranchIndentation(callBlocks);
  renderBranchGraph(items);
}

// The branch graph is the call tree. Each call's parent is the request it
// continued: its declared predecessor (prev_req_id) when the caller named one,
// otherwise the most recent earlier call that produced its parent state. A
// parent with several children is a branch; a lane with no more children ends.
function buildBranchGraph(items) {
  const calls = items
    .filter(item => item.type === "call")
    .slice()
    .sort((left, right) => left.id - right.id);
  const byReqId = new Map();
  for (const item of calls) {
    if (item.req_id) byReqId.set(item.req_id, item.id);
  }
  const stateHead = new Map();
  const parentOf = new Map();
  const childCount = new Map();
  for (const item of calls) {
    let parent = null;
    if (item.prev_req_id != null && byReqId.get(item.prev_req_id) !== item.id) {
      parent = byReqId.get(item.prev_req_id) ?? null;
    } else if (item.parent_state_id != null && stateHead.has(item.parent_state_id)) {
      parent = stateHead.get(item.parent_state_id);
    }
    parentOf.set(item.id, parent);
    if (parent != null) childCount.set(parent, (childCount.get(parent) || 0) + 1);
    stateHead.set(item.request_state_id, item.id);
  }

  // Lane assignment with reuse: a continuation keeps its parent's lane, a branch
  // takes the lowest free lane, and a lane frees when its tip has no children
  // left to place. This keeps a mostly-linear trace narrow.
  const laneTip = [];
  const laneOf = new Map();
  const extended = new Set();
  const remaining = new Map(childCount);
  const nodes = [];
  calls.forEach((item, depth) => {
    const parent = parentOf.get(item.id);
    let lane;
    if (parent != null && laneOf.has(parent) && !extended.has(parent)) {
      lane = laneOf.get(parent);
      extended.add(parent);
    } else {
      lane = laneTip.findIndex(tip => tip == null);
      if (lane < 0) {
        lane = laneTip.length;
        laneTip.push(null);
      }
    }
    laneOf.set(item.id, lane);
    laneTip[lane] = item.id;
    nodes.push({ item, id: item.id, depth, lane, parentId: parent });
    if (parent != null) {
      remaining.set(parent, (remaining.get(parent) || 0) - 1);
      const parentLane = laneOf.get(parent);
      if (remaining.get(parent) === 0 && laneTip[parentLane] === parent) {
        laneTip[parentLane] = null;
      }
    }
  });
  const nodeById = new Map(nodes.map(node => [node.id, node]));
  return { nodes, nodeById, laneCount: Math.max(laneTip.length, 1) };
}

const BRANCH_MARGIN = 18;
const BRANCH_NODE_R = 5;
const BRANCH_HORIZONTAL_STEP = 138;
const BRANCH_HORIZONTAL_GAP = 54;
let branchResizeObserver = null;
let branchResizeFrame = 0;
let branchRenderedWidth = 0;

function branchNodeLabel(item) {
  // The step name leads — it is what identifies a call to a reader — with the id
  // and branch as secondary. A caller-set debug label is best; otherwise the
  // call's purpose (overview, rewrite, …) still names the step; the id is the
  // last resort.
  const step = item.debug_label
    ? escapeHtml(item.debug_label)
    : (item.label ? escapeHtml(item.label) : "");
  const branch = escapeHtml(item.branch_id || "main");
  return `<span class="branch-node-step">${step || `#${item.id}`}</span>`
    + `<span class="branch-node-meta">${step ? `#${item.id} · ` : ""}${branch}</span>`;
}

function branchLaneColor(lane) {
  return `hsl(${(145 + lane * 37) % 360}, 42%, 46%)`;
}

function wrapBranchLane(slot, columns) {
  const band = Math.floor(slot / columns);
  const offset = slot % columns;
  return {
    band,
    column: band % 2 === 0 ? offset : columns - 1 - offset,
  };
}

function horizontalBranchMarkup(nodes, nodeById, laneCount) {
  const depthCount = nodes.length;
  const width = BRANCH_MARGIN * 2 + Math.max(depthCount - 1, 0) * BRANCH_HORIZONTAL_STEP;
  const height = BRANCH_MARGIN * 2 + Math.max(laneCount - 1, 0) * BRANCH_HORIZONTAL_GAP + 24;
  const xOf = node => BRANCH_MARGIN + node.depth * BRANCH_HORIZONTAL_STEP;
  const yOf = node => BRANCH_MARGIN + node.lane * BRANCH_HORIZONTAL_GAP;
  const edges = nodes
    .filter(node => node.parentId != null && nodeById.has(node.parentId))
    .map(node => {
      const parent = nodeById.get(node.parentId);
      const x1 = xOf(parent);
      const y1 = yOf(parent);
      const x2 = xOf(node);
      const y2 = yOf(node);
      const branched = parent.lane !== node.lane;
      const d = branched
        ? `M ${x1} ${y1} C ${(x1 + x2) / 2} ${y1}, ${(x1 + x2) / 2} ${y2}, ${x2} ${y2}`
        : `M ${x1} ${y1} L ${x2} ${y2}`;
      const attributes = `data-parent-id="${parent.id}" data-child-id="${node.id}"`;
      return `<path class="branch-edge${branched ? " branch-edge-split" : ""}" d="${d}" stroke="${branchLaneColor(node.lane)}" ${attributes}/>`
        + `<path class="branch-edge-hit" d="${d}" ${attributes}/>`;
    })
    .join("");
  const svg = `<svg class="branch-graph-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${edges}
    ${nodes.map(node => {
      const cx = xOf(node);
      const cy = yOf(node);
      const running = node.item.status === "running";
      const checkpoint = state.checkpointKeys.has(`call:${node.id}`);
      const active = focusedTimelineKey() === `call:${node.id}`;
      return `<circle class="branch-dot${running ? " running" : ""}${checkpoint ? " checkpoint" : ""}${active ? " active" : ""}"
        cx="${cx}" cy="${cy}" r="${BRANCH_NODE_R}" fill="${branchLaneColor(node.lane)}" data-call-id="${node.id}"/>`;
    }).join("")}
  </svg>`;
  const labels = nodes.map(node => {
    const cx = xOf(node);
    const cy = yOf(node);
    const key = `call:${node.id}`;
    const active = focusedTimelineKey() === key;
    return `<button class="branch-node${active ? " active" : ""} horizontal"
      data-key="${key}" data-call-id="${node.id}" style="left:${cx}px;top:${cy + BRANCH_NODE_R + 3}px;"
      title="LLM call #${node.id}${node.item.debug_label ? ` · ${escapeHtml(node.item.debug_label)}` : ""} · ${escapeHtml(node.item.branch_id || "main")}">
      ${branchNodeLabel(node.item)}
    </button>`;
  }).join("");
  return { width, height, html: `<div class="branch-canvas" style="width:${width + 220}px;height:${height}px;">${svg}${labels}</div>` };
}

function verticalBranchMarkup(nodes, nodeById, width) {
  const config = FRONTEND_CONFIG.branchGraph;
  const padding = 8;
  const usableWidth = Math.max(config.lanePitch, width - padding * 2);
  const columns = Math.max(1, Math.floor(usableWidth / config.lanePitch));
  const edges = nodes
    .filter(node => node.parentId != null && nodeById.has(node.parentId))
    .map(node => ({
      node,
      parent: nodeById.get(node.parentId),
      lane: node.lane,
    }));

  const rows = [];
  let rowTop = BRANCH_MARGIN;
  for (let depth = 0; depth < nodes.length; depth += 1) {
    const active = new Set([nodes[depth].lane]);
    for (const edge of edges) {
      if (edge.parent.depth <= depth && depth <= edge.node.depth) active.add(edge.lane);
    }
    const lanes = [...active].sort((left, right) => left - right);
    const slotByLane = new Map(lanes.map((lane, slot) => [lane, slot]));
    const bands = Math.max(1, Math.ceil(lanes.length / columns));
    rows.push({ top: rowTop, slotByLane });
    rowTop += bands * config.bandPitch + config.rowGap;
  }
  const height = rowTop + BRANCH_MARGIN;

  const position = (lane, depth) => {
    const wrapped = wrapBranchLane(rows[depth].slotByLane.get(lane), columns);
    return {
      x: padding + wrapped.column * config.lanePitch + config.lanePitch / 2,
      y: rows[depth].top + wrapped.band * config.bandPitch + config.bandPitch / 2,
    };
  };
  const positions = new Map(nodes.map(node => [node.id, position(node.lane, node.depth)]));
  const geometries = edges.map(edge => {
    const points = [{ ...positions.get(edge.parent.id) }];
    for (let depth = edge.parent.depth; depth <= edge.node.depth; depth += 1) {
      if (!rows[depth].slotByLane.has(edge.lane)) continue;
      const point = position(edge.lane, depth);
      const previous = points[points.length - 1];
      if (point.x !== previous.x || point.y !== previous.y) points.push(point);
    }
    const segments = [];
    for (let index = 1; index < points.length; index += 1) {
      const from = points[index - 1];
      const to = points[index];
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const length = Math.hypot(dx, dy) || 1;
      let normalX = -dy / length;
      let normalY = dx / length;
      if (normalX < 0 || (Math.abs(normalX) < .001 && normalY < 0)) {
        normalX *= -1;
        normalY *= -1;
      }
      segments.push({
        edge, index, from, to, normalX, normalY,
        conflicts: new Set(), offset: 0, routed: false,
      });
    }
    return { edge, points, segments };
  });

  const verticalCorridors = new Map();
  const horizontalCorridors = new Map();
  const identicalFragments = new Map();
  for (const geometry of geometries) {
    for (const segment of geometry.segments) {
      const { from, to } = segment;
      if (Math.abs(from.x - to.x) <= .25) {
        const key = Math.round(from.x * 2) / 2;
        if (!verticalCorridors.has(key)) verticalCorridors.set(key, []);
        verticalCorridors.get(key).push({
          segment,
          start: Math.min(from.y, to.y),
          end: Math.max(from.y, to.y),
        });
      } else if (Math.abs(from.y - to.y) <= .25) {
        const key = Math.round(from.y * 2) / 2;
        if (!horizontalCorridors.has(key)) horizontalCorridors.set(key, []);
        horizontalCorridors.get(key).push({
          segment,
          start: Math.min(from.x, to.x),
          end: Math.max(from.x, to.x),
        });
      } else {
        const key = [
          Math.round(from.x * 2), Math.round(from.y * 2),
          Math.round(to.x * 2), Math.round(to.y * 2),
        ].join(":");
        if (!identicalFragments.has(key)) identicalFragments.set(key, []);
        identicalFragments.get(key).push(segment);
      }
    }
  }
  const registerConflict = (left, right) => {
    if (left.edge === right.edge) return;
    left.conflicts.add(right);
    right.conflicts.add(left);
  };
  for (const corridors of [verticalCorridors, horizontalCorridors]) {
    for (const fragments of corridors.values()) {
      fragments.sort((left, right) => left.start - right.start || left.end - right.end);
      for (let left = 0; left < fragments.length; left += 1) {
        for (let right = left + 1; right < fragments.length; right += 1) {
          if (fragments[right].start >= fragments[left].end - 1) break;
          registerConflict(fragments[left].segment, fragments[right].segment);
        }
      }
    }
  }
  for (const fragments of identicalFragments.values()) {
    for (let left = 0; left < fragments.length; left += 1) {
      for (let right = left + 1; right < fragments.length; right += 1) {
        registerConflict(fragments[left], fragments[right]);
      }
    }
  }

  const candidates = [0];
  for (let distance = 1; distance <= 8; distance += 1) {
    candidates.push(-distance * config.overlapShift, distance * config.overlapShift);
  }
  const segments = geometries
    .flatMap(geometry => geometry.segments)
    .sort((left, right) => right.conflicts.size - left.conflicts.size);
  for (const segment of segments) {
    const used = new Set(
      [...segment.conflicts].filter(other => other.routed).map(other => other.offset),
    );
    segment.offset = candidates.find(candidate => {
      if (used.has(candidate)) return false;
      return [segment.from, segment.to].every(point => {
        const x = point.x + candidate * segment.normalX;
        const y = point.y + candidate * segment.normalY;
        return x >= 3 && x <= width - 3 && y >= 3 && y <= height - 3;
      });
    }) ?? 0;
    segment.routed = true;
  }

  const edgeMarkup = geometries.map(geometry => {
    const { edge, points, segments: routeSegments } = geometry;
    const pointOffsets = points.map((point, index) => {
      if (index === 0 || index === points.length - 1) return { x: 0, y: 0 };
      const before = routeSegments[index - 1];
      const after = routeSegments[index];
      return {
        x: (before.offset * before.normalX + after.offset * after.normalX) / 2,
        y: (before.offset * before.normalY + after.offset * after.normalY) / 2,
      };
    });
    let d = `M ${points[0].x} ${points[0].y}`;
    for (const segment of routeSegments) {
      const shiftX = segment.offset * segment.normalX;
      const shiftY = segment.offset * segment.normalY;
      const target = pointOffsets[segment.index];
      const midpoint = (segment.from.y + segment.to.y) / 2;
      d += ` C ${segment.from.x + shiftX} ${midpoint + shiftY},`
        + ` ${segment.to.x + shiftX} ${midpoint + shiftY},`
        + ` ${segment.to.x + target.x} ${segment.to.y + target.y}`;
    }
    const branched = edge.parent.lane !== edge.node.lane;
    const attributes = `data-parent-id="${edge.parent.id}" data-child-id="${edge.node.id}"`;
    return `<path class="branch-edge${branched ? " branch-edge-split" : ""}" d="${d}" stroke="${branchLaneColor(edge.lane)}" ${attributes}/>`
      + `<path class="branch-edge-hit" d="${d}" ${attributes}/>`;
  }).join("");

  const dots = nodes.map(node => {
    const point = positions.get(node.id);
    const running = node.item.status === "running";
    const checkpoint = state.checkpointKeys.has(`call:${node.id}`);
    const active = focusedTimelineKey() === `call:${node.id}`;
    return `<circle class="branch-dot${running ? " running" : ""}${checkpoint ? " checkpoint" : ""}${active ? " active" : ""}"
      cx="${point.x}" cy="${point.y}" r="${BRANCH_NODE_R}" fill="${branchLaneColor(node.lane)}" data-call-id="${node.id}"/>`;
  }).join("");
  const labels = nodes.map(node => {
    const point = positions.get(node.id);
    const roomRight = width - point.x;
    const placeLeft = roomRight < 150;
    const available = Math.max(76, Math.min(190, placeLeft ? point.x - 18 : roomRight - 18));
    const active = focusedTimelineKey() === `call:${node.id}`;
    return `<button class="branch-node${active ? " active" : ""}${placeLeft ? " place-left" : ""}"
      data-key="call:${node.id}" data-call-id="${node.id}"
      style="left:${point.x + (placeLeft ? -8 : 8)}px;top:${point.y}px;width:${available}px;"
      title="LLM call #${node.id}${node.item.debug_label ? ` · ${escapeHtml(node.item.debug_label)}` : ""} · ${escapeHtml(node.item.branch_id || "main")}">
      ${branchNodeLabel(node.item)}
    </button>`;
  }).join("");
  const svg = `<svg class="branch-graph-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${edgeMarkup}${dots}</svg>`;
  return { width, height, html: `<div class="branch-canvas" style="width:${width}px;height:${height}px;">${svg}${labels}</div>` };
}

function clearBranchHover(container) {
  container.classList.remove("has-hover", "lane-hover", "node-hover");
  container.querySelectorAll(".hover-related, .hover-focus").forEach(element => {
    element.classList.remove("hover-related", "hover-focus");
  });
}

function markBranchNode(container, id, focus = false) {
  container.querySelectorAll(`[data-call-id="${id}"]`).forEach(element => {
    element.classList.add("hover-related");
    if (focus) element.classList.add("hover-focus");
  });
}

function highlightBranchEdge(container, edge) {
  clearBranchHover(container);
  container.classList.add("has-hover", "lane-hover");
  edge.classList.add("hover-related");
  markBranchNode(container, edge.dataset.parentId);
  markBranchNode(container, edge.dataset.childId);
}

function highlightBranchNode(container, id) {
  clearBranchHover(container);
  container.classList.add("has-hover", "node-hover");
  markBranchNode(container, id, true);
  container.querySelectorAll(".branch-edge").forEach(edge => {
    if (edge.dataset.parentId === id || edge.dataset.childId === id) {
      edge.classList.add("hover-related");
      markBranchNode(container, edge.dataset.parentId);
      markBranchNode(container, edge.dataset.childId);
    }
  });
}

function bindBranchGraphInteractions(container) {
  container.querySelectorAll(".branch-node").forEach(button => {
    button.onclick = () => selectItem("call", Number(button.dataset.callId), button);
    button.onmouseenter = () => highlightBranchNode(container, button.dataset.callId);
    button.onmouseleave = () => clearBranchHover(container);
  });
  container.querySelectorAll(".branch-dot").forEach(dot => {
    dot.onmouseenter = () => highlightBranchNode(container, dot.dataset.callId);
    dot.onmouseleave = () => clearBranchHover(container);
  });
  container.querySelectorAll(".branch-edge-hit").forEach(hit => {
    const edge = hit.previousElementSibling;
    hit.onmouseenter = () => highlightBranchEdge(container, edge);
    hit.onmouseleave = () => clearBranchHover(container);
  });
}

function renderBranchGraph(items) {
  const container = $("branch-graph");
  if (
    !FRONTEND_CONFIG.branchGraph.enabled
    || !container
    || state.timelineView !== "branches"
  ) return;
  const oldScrollHeight = Math.max(container.scrollHeight, 1);
  const oldScrollRatio = container.scrollTop / oldScrollHeight;
  const { nodes, nodeById, laneCount } = buildBranchGraph(items);
  const horizontal = state.branchOrientation === "horizontal";
  container.classList.toggle("horizontal", horizontal);
  container.style.setProperty(
    "--branch-edge-hover-width",
    `${FRONTEND_CONFIG.branchGraph.edgeHoverWidth}px`,
  );
  const availableWidth = Math.max(120, container.clientWidth - 20);
  branchRenderedWidth = container.clientWidth;
  const markup = horizontal
    ? horizontalBranchMarkup(nodes, nodeById, laneCount)
    : verticalBranchMarkup(nodes, nodeById, availableWidth);
  container.style.setProperty("--branch-canvas-width", `${markup.width}px`);
  container.style.setProperty("--branch-canvas-height", `${markup.height}px`);
  container.innerHTML = markup.html;
  bindBranchGraphInteractions(container);
  container.scrollTop = oldScrollRatio * container.scrollHeight;
}

function initBranchGraphResize() {
  const container = $("branch-graph");
  if (!container || branchResizeObserver) return;
  branchResizeObserver = new ResizeObserver(() => {
    if (
      state.timelineView !== "branches" ||
      state.branchOrientation !== "vertical" ||
      container.clientWidth === branchRenderedWidth
    ) return;
    cancelAnimationFrame(branchResizeFrame);
    branchResizeFrame = requestAnimationFrame(() => renderBranchGraph(state.timelineItems));
  });
  branchResizeObserver.observe(container);
}

function syncBranchGraphSelection() {
  const container = $("branch-graph");
  if (!container || state.timelineView !== "branches") return;
  const key = focusedTimelineKey();
  container.querySelectorAll(".branch-node, .branch-dot").forEach(node => {
    const nodeKey = node.dataset.key || `call:${node.dataset.callId}`;
    node.classList.toggle("active", nodeKey === key);
  });
}

// Selection owns the state rendered in Mixed/Exact. Timeline focus is separate:
// a historical fragment in those panes can point back to the call that created
// it without replacing the currently reconstructed state.
function setTimelineFocus(key, phase = "input", scroll = false, element = null) {
  state.timelineFocus = { key, phase };
  state.selectedPhase = phase;
  document.querySelectorAll(".timeline-item").forEach(node => {
    const nodeKey = node.dataset.key || node.dataset.callKey;
    node.classList.toggle(
      "active",
      nodeKey === key && node.dataset.phase === phase,
    );
  });
  syncBranchGraphSelection();
  if (!scroll) return;
  const graphTarget = state.timelineView === "branches"
    ? $("branch-graph")?.querySelector(`.branch-node[data-key="${key}"]`)
    : null;
  const listTarget = phase === "output"
    ? document.querySelector(`.timeline-output[data-call-key="${key}"]`)
    : document.querySelector(`.timeline-input[data-key="${key}"]`);
  const target = graphTarget || element || listTarget;
  target?.focus({ preventScroll: true });
  focusScrollIntoView(target, "nearest");
}

function applyTimelineView() {
  const branches = FRONTEND_CONFIG.branchGraph.enabled
    && state.timelineView === "branches";
  if (!branches) state.timelineView = "list";
  $("timeline").classList.toggle("hidden", branches);
  $("branch-graph").classList.toggle("hidden", !branches);
  $("orientation-switch")?.classList.toggle("hidden", !branches);
  document.querySelectorAll(".view-switch-btn[data-view]").forEach(button => {
    button.classList.toggle("active", button.dataset.view === state.timelineView);
  });
  document.querySelectorAll(".view-switch-btn[data-orient]").forEach(button => {
    button.classList.toggle("active", button.dataset.orient === state.branchOrientation);
  });
  if (branches) {
    renderBranchGraph(state.timelineItems);
    syncBranchGraphSelection();
  }
}

function visibleScrollableChild(container, selector) {
  const rect = container.getBoundingClientRect();
  const x = Math.min(rect.right - 2, rect.left + Math.max(2, rect.width / 2));
  for (let y = rect.top + 2; y < rect.bottom; y += 16) {
    const child = document.elementFromPoint(x, y)?.closest(selector);
    if (child && container.contains(child)) return child;
  }
  return null;
}

function captureTimelineViewport() {
  const container = $("timeline");
  const containerRect = container.getBoundingClientRect();
  const visibleItem = visibleScrollableChild(container, ".timeline-item");
  return {
    scrollTop: container.scrollTop,
    nearBottom: container.scrollHeight - container.scrollTop - container.clientHeight < 80,
    key: visibleItem?.dataset.key || visibleItem?.dataset.callKey || null,
    phase: visibleItem?.dataset.phase || null,
    offset: visibleItem
      ? visibleItem.getBoundingClientRect().top - containerRect.top
      : null,
  };
}

// Sticking to the newest end is following, so it belongs to Follow. With the
// option off, a viewport that happens to sit at the bottom must stay where the
// user left it instead of being dragged along by every arriving event.
function restoreTimelineViewport(anchor, stickToNewest = state.followNewItems) {
  const container = $("timeline");
  if (anchor.nearBottom) {
    container.scrollTop = stickToNewest
      ? container.scrollHeight
      : anchor.scrollTop;
    return;
  }
  container.scrollTop = anchor.scrollTop;
  if (!anchor.key || !anchor.phase || anchor.offset == null) return;
  const keyAttribute = anchor.phase === "input" ? "data-key" : "data-call-key";
  const target = container.querySelector(
    `.timeline-${anchor.phase}[${keyAttribute}="${anchor.key}"]`,
  );
  if (!target) return;
  const currentOffset =
    target.getBoundingClientRect().top - container.getBoundingClientRect().top;
  container.scrollTop += currentOffset - anchor.offset;
}

// Update cards are moved, not rebuilt, so the anchor holds the element itself:
// an output card inserted above the viewport must not shift what is on screen.
function captureUpdatesViewport() {
  const container = $("updates");
  const containerRect = container.getBoundingClientRect();
  const visibleCard = visibleScrollableChild(container, ".update-card");
  return {
    card: visibleCard || null,
    offset: visibleCard
      ? visibleCard.getBoundingClientRect().top - containerRect.top
      : null,
  };
}

function restoreUpdatesViewport(anchor, fallbackScrollTop) {
  const container = $("updates");
  container.scrollTop = fallbackScrollTop;
  if (!anchor.card?.isConnected || anchor.offset == null) return;
  const currentOffset =
    anchor.card.getBoundingClientRect().top - container.getBoundingClientRect().top;
  container.scrollTop += currentOffset - anchor.offset;
}

function renderWaitingCalls(items) {
  const box = $("waiting-calls");
  const waiting = items.filter(item => item.type === "call" && item.status === "running");
  box.innerHTML = `
    <div class="waiting-calls-head">
      <span>${waiting.length} waiting / running</span>
    </div>`;
}

function applyBranchIndentation(
  callBlocks = timelineCallBlocks(state.timelineItems),
) {
  const layout = new Map();
  for (const currentBlock of callBlocks) {
    const branchLanes = new Map();
    const parallel = currentBlock.calls.length > 1;
    for (const { item } of currentBlock.calls) {
      const branch = item.branch_id || "main";
      if (!branchLanes.has(branch)) {
        branchLanes.set(branch, branchLanes.size);
      }
      const lane = branchLanes.get(branch);
      const branchRoot = item.branch_root_id
        || branch.split("~parallel-", 1)[0]
        || "main";
      const visibleBranch = branch === branchRoot || lane === 0
        ? branchRoot
        : `${branchRoot} · p${lane + 1}`;
      layout.set(itemKey(item), {
        branch,
        branchColor: `hsl(${145 + lane * 31}, 36%, 42%)`,
        depth: parallel ? lane + 1 : 0,
        lane,
        parallel,
        visibleBranch,
      });
    }
  }
  document.querySelectorAll(".timeline-item").forEach(button => {
    const key = button.dataset.key || button.dataset.callKey;
    const itemLayout = layout.get(key);
    if (!itemLayout) return;
    button.style.setProperty("--branch-lane", itemLayout.lane);
    button.style.setProperty("--branch-depth", itemLayout.depth);
    button.style.setProperty("--branch-color", itemLayout.branchColor);
    button.dataset.branchLane = String(itemLayout.lane);
    button.dataset.branchDepth = String(itemLayout.depth);
    button.classList.toggle("parallel-block", itemLayout.parallel);
    button.classList.toggle("parallel-branch", itemLayout.lane > 0);
    const branchLabel = button.querySelector(".branch");
    if (branchLabel) branchLabel.textContent = itemLayout.visibleBranch;
    button.title = itemLayout.parallel
      ? `Stored branch ${itemLayout.branch} · parallel lane ${itemLayout.lane + 1}`
      : `Stored branch ${itemLayout.branch}`;
  });
}

function keepFollowedUpdateVisible() {
  if (!state.followedUpdateKey) return;
  const card = updateCardFor(state.followedUpdateKey, state.selectedPhase);
  card?.scrollIntoView({ behavior: "auto", block: "nearest" });
  window.clearTimeout(state.followedUpdateTimer);
  state.followedUpdateTimer = window.setTimeout(() => {
    state.followedUpdateKey = null;
    state.followedUpdateTimer = null;
  }, 300);
}

const FOCUS_SCROLL_DURATION_MS = 200;
const focusScrollAnimations = new WeakMap();

function focusScrollIntoView(element, block = "center") {
  if (!element) return;
  let container = element.parentElement;
  while (container && container !== document.body) {
    const overflowY = getComputedStyle(container).overflowY;
    if (/(auto|scroll)/.test(overflowY) && container.scrollHeight > container.clientHeight) {
      break;
    }
    container = container.parentElement;
  }
  if (!container || container === document.body) {
    element.scrollIntoView({ behavior: "smooth", block });
    return;
  }

  const start = container.scrollTop;
  const containerRect = container.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const elementTop = elementRect.top - containerRect.top + start;
  const elementBottom = elementTop + elementRect.height;
  let target;
  if (block === "start") {
    target = elementTop;
  } else if (block === "nearest") {
    if (elementTop < start) {
      target = elementTop;
    } else if (elementBottom > start + container.clientHeight) {
      target = elementBottom - container.clientHeight;
    } else {
      target = start;
    }
  } else {
    target = elementTop - (container.clientHeight - elementRect.height) / 2;
  }
  target = Math.max(0, Math.min(target, container.scrollHeight - container.clientHeight));
  if (target === start) return;

  const previousAnimation = focusScrollAnimations.get(container);
  if (previousAnimation) cancelAnimationFrame(previousAnimation);
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    container.scrollTop = target;
    return;
  }

  const startedAt = performance.now();
  const distance = target - start;
  const step = now => {
    const progress = Math.min(1, (now - startedAt) / FOCUS_SCROLL_DURATION_MS);
    const eased = 1 - Math.pow(1 - progress, 3);
    container.scrollTop = start + distance * eased;
    if (progress < 1) {
      focusScrollAnimations.set(container, requestAnimationFrame(step));
    } else {
      focusScrollAnimations.delete(container);
    }
  };
  focusScrollAnimations.set(container, requestAnimationFrame(step));
}

function hasTextSelectionWithin(element) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.toString()) return false;
  return [selection.anchorNode, selection.focusNode].some(node => (
    node && element.contains(node.nodeType === Node.TEXT_NODE ? node.parentNode : node)
  ));
}

function cancelFollowedUpdate() {
  state.updatesUserScrollVersion += 1;
  state.followedUpdateKey = null;
  window.clearTimeout(state.followedUpdateTimer);
  state.followedUpdateTimer = null;
}

function focusedFragmentEntry(entry, indices) {
  const fragments = indices.map(index => entry.fragments[index]).filter(Boolean);
  return {
    ...entry,
    fragments,
    focusedFragmentIndices: indices,
    needles: fragments.flatMap(fragment => (
      fragment.new
        .split("\n")
        .map(line => line.trim())
        .filter(Boolean)
    )),
  };
}

// Clicking one side of a transition asks about that side. Mixed renders the
// removed half as <del> and the present half as <ins>, so the clicked part
// selects which of them is focused; clicking elsewhere in the entry focuses the
// change as a whole.
function clickedPartKind(event) {
  const part = event?.target?.closest?.("del, ins");
  if (!part) return null;
  return part.tagName === "DEL" ? "removed" : "added";
}

function partMatches(node, part) {
  if (!part) return true;
  return part === "removed" ? node.tagName === "DEL" : node.tagName === "INS";
}

function applyMixedFocus(targets) {
  $("mixed").querySelectorAll(".fragment-focus").forEach(node => {
    node.classList.remove("fragment-focus", "flash");
  });
  for (const target of targets) {
    // Restart the pulse when the same target is chosen again.
    void target.offsetWidth;
    target.classList.add("fragment-focus", "flash");
  }
  focusScrollIntoView(targets[0]);
}

function focusMixedFragments(indices, entry, part = null) {
  const targets = indices.flatMap(index => (
    [...$("mixed").querySelectorAll(
      `[data-update-entry="${entry.entryKey}"][data-output-fragment="${index}"]`,
    )]
  )).filter(node => partMatches(node, part));
  applyMixedFocus(targets);
}

function focusMixedEntry(entry, part = null) {
  if (entry.fragments) {
    focusMixedFragments(entry.fragments.map((_, index) => index), entry, part);
    return;
  }
  const targets = [
    ...$("mixed").querySelectorAll(`[data-update-entry="${entry.entryKey}"]`),
  ].filter(node => partMatches(node, part));
  applyMixedFocus(targets);
}

function focusCheckpointScope(scope) {
  if (!state.detail) return;
  if (scope === "input-params") {
    activateTab("parameters");
  } else {
    activateTab("state");
  }
  renderExact();
  for (const pane of [$("mixed"), $("exact")]) {
    pane.querySelectorAll(".checkpoint-pane-focus").forEach(node => {
      node.classList.remove("checkpoint-pane-focus");
    });
    const target = pane.querySelector(`[data-state-scope="${scope}"]`);
    target?.classList.add("checkpoint-pane-focus");
    focusScrollIntoView(target, "start");
  }
}

// A card is a navigation control for its own phase, not only a container for
// entries: a card whose body is a scope notice must still select its call and
// activate the matching timeline event.
// Only input events carry data-key, so a card must hand selectItem the event
// element for its own phase: left to its fallback lookup, every update in an
// output card would activate that call's input.
function timelineEventFor(callId, phase) {
  const key = `call:${callId}`;
  return phase === "output"
    ? document.querySelector(`.timeline-output[data-call-key="${key}"]`)
    : document.querySelector(`.timeline-input[data-key="${key}"]`);
}

async function selectPhaseFromCard(card) {
  const id = Number(card.dataset.id);
  await selectItem("call", id, timelineEventFor(id, card.dataset.phase), true);
}

// Entries, fragments, and checkpoint sections carry a more specific target, so
// they keep their own handlers; the card answers for everything else.
const CARD_OWN_CONTROLS =
  ".update-jump, .fragment-change, .checkpoint-section, .checkpoint-jump, .identical-jump";

function bindCardPhaseSelection(card) {
  if (card.dataset.phaseBound) return;
  card.dataset.phaseBound = "true";
  card.addEventListener("click", event => {
    if (event.target.closest(CARD_OWN_CONTROLS)) return;
    if (hasTextSelectionWithin(card)) return;
    selectPhaseFromCard(card);
  });
  card.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest("[data-update-scope]");
    if (!target || target.closest(CARD_OWN_CONTROLS)) return;
    event.preventDefault();
    selectPhaseFromCard(card);
  });
}

async function loadUpdateCard(card) {
  if (card.dataset.loaded || card.dataset.loading) return;
  card.dataset.loading = "true";
  let detail;
  try {
    detail = await detailFor(card.dataset.type, Number(card.dataset.id));
    await resolveOutputParentRequestIdentity(detail);
  } catch (error) {
    delete card.dataset.loading;
    delete card.dataset.loaded;
    card.classList.remove("loading");
    card.classList.add("load-error");
    const unavailable = error.status === 404;
    card.innerHTML = `
      <div class="update-load-error">
        <strong>${unavailable ? "Call unavailable" : "Could not load call"}</strong>
        <span>LLM call #${escapeHtml(card.dataset.id)}</span>
        <small>${
          unavailable
            ? "This call is no longer present in trace storage. Reload the timeline to reconcile the view."
            : escapeHtml(error.message || "Unknown detail-loading error.")
        }</small>
        ${unavailable ? "" : '<button type="button">Retry</button>'}
      </div>`;
    card.querySelector("button")?.addEventListener("click", () => {
      card.classList.remove("load-error");
      card.classList.add("loading");
      card.innerHTML = updateCardHeadHtml(card.dataset.id, card.dataset.phase || "input", card.dataset.debugLabel || null);
      loadUpdateCard(card);
    });
    return;
  }
  delete card.dataset.loading;
  card.dataset.loaded = "true";
  card.classList.remove("load-error");
  bindCardPhaseSelection(card);
  const phase = card.dataset.phase || "input";
  const debugLabel = detail.metadata?.debug_label || card.dataset.debugLabel || null;
  if (debugLabel) card.dataset.debugLabel = debugLabel;
  const checkpoint = isCheckpoint(detail);
  const identicalTo = identicalBaseCall(detail);
  const entries = updateEntries(detail);
  const phaseEntries = entries.filter(entry => (
    phase === "output"
      ? entry.scope === "output" || entry.scope === "thoughts"
      : entry.scope === "input"
  ));
  card.classList.remove("loading");
  card.classList.toggle("checkpoint", checkpoint);
  const timelineItem = document.querySelector(
    `.timeline-item[data-key="call:${detail.id}"]`,
  );
  const timelineKey = `call:${detail.id}`;
  if (checkpoint) {
    state.checkpointKeys.add(timelineKey);
  } else {
    state.checkpointKeys.delete(timelineKey);
  }
  timelineItem?.classList.toggle("checkpoint-call", checkpoint);
  const timelineLabel = timelineItem?.querySelector(".item-label");
  if (timelineLabel) {
    timelineLabel.textContent = checkpoint ? "→ new state input" : "→ input";
  }
  if (checkpoint) {
    const input = requestContent(detail.request);
    const parameters = requestParameters(detail.request);
    const output = responseValue(detail);
    // The snapshot splits along the same seam as the timeline: the request it
    // flushed to, then the response that arrived against it.
    card.innerHTML = phase === "output"
      ? `
      ${updateCardHeadHtml(detail.id, phase, debugLabel)}
      <div class="checkpoint-state">
        ${detail.thoughts ? `
          <section class="checkpoint-section checkpoint-thoughts trace-kind-thoughts" role="button" tabindex="0" data-checkpoint-scope="thoughts">
            <strong>Thoughts</strong>
            <pre>${escapeHtml(detail.thoughts)}</pre>
          </section>` : ""}
        <section class="checkpoint-section checkpoint-output trace-kind-output" role="button" tabindex="0" data-checkpoint-scope="output">
          <strong>Output</strong>
          <pre>${escapeHtml(displayValue(output))}</pre>
        </section>
      </div>`
      : `
      <button class="checkpoint-jump">
        <strong>◆ New current state</strong>
        <span class="checkpoint-jump-id">LLM call #${detail.id}${
          debugLabel ? ` <span class="update-card-debug">${escapeHtml(debugLabel)}</span>` : ""
        }</span>
      </button>
      <div class="checkpoint-state">
        <section class="checkpoint-section checkpoint-input trace-kind-input" role="button" tabindex="0" data-checkpoint-scope="input">
          <strong>Input</strong>
          ${checkpointInputHtml(detail, input)}
        </section>
        ${Object.keys(parameters).length ? `
          <section class="checkpoint-section checkpoint-parameters trace-kind-input-params" role="button" tabindex="0" data-checkpoint-scope="input-params">
            <strong>Parameters</strong>
            <pre>${escapeHtml(yaml(parameters))}</pre>
          </section>` : ""}
      </div>`;
    const openScope = async scope => {
      if (hasTextSelectionWithin(card)) return;
      await selectItem(
        "call", detail.id, timelineEventFor(detail.id, phase), true, false,
      );
      focusCheckpointScope(scope);
      card.querySelectorAll(".checkpoint-section.active").forEach(node => {
        node.classList.remove("active");
      });
      card.querySelector(`[data-checkpoint-scope="${scope}"]`)?.classList.add("active");
    };
    card.querySelector(".checkpoint-jump")?.addEventListener("click", () => {
      openScope("input");
    });
    card.querySelectorAll("[data-checkpoint-scope]").forEach(section => {
      section.onclick = () => openScope(section.dataset.checkpointScope);
      section.onkeydown = event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openScope(section.dataset.checkpointScope);
        }
      };
    });
    keepFollowedUpdateVisible();
    return;
  }
  // The identity statement covers the whole call, so it stays a single compact
  // card at the input moment; the output card still carries its unchanged row.
  if (identicalTo && phase === "input") {
    card.classList.add("identical");
    card.innerHTML = `
      <button class="identical-jump" data-update-scope="input">
        <strong>↻ Identical call</strong>
        <span>LLM call #${detail.id} = call #${identicalTo}${
          debugLabel ? ` <span class="update-card-debug">${escapeHtml(debugLabel)}</span>` : ""
        }</span>
        <small>No input, parameter, or output changes</small>
      </button>`;
    card.querySelector("button").onclick = () => selectItem(
      "call", detail.id, timelineEventFor(detail.id, phase), true, false,
    );
    keepFollowedUpdateVisible();
    return;
  }
  // Each phase carries its own scope notice, so a phase with nothing to show
  // still owns a focus target instead of borrowing the other phase's updates.
  const notice = phase === "output"
    ? unchangedOutputNoticeHtml(detail)
    : unchangedInputNoticeHtml(detail, entries);
  const laneSnapshot = phase !== "output" && notice && parallelLaneInput(detail)
    ? laneInputSnapshotHtml(detail)
    : "";
  const entriesHtml = phaseEntries.map(entry => `
    <div class="update-jump ${escapeHtml(entry.category || "content")}-update-card trace-kind-${traceKind(entry.category)} trace-op-${traceOperation(entry.operation)} op-${escapeHtml(entry.operation || "change")}" data-update-index="${entry.entryIndex}" role="button" tabindex="0">
      <strong>${escapeHtml(entry.label)}</strong>
      ${updateEntryBodyHtml(entry)}
    </div>`).join("");
  card.innerHTML = `
    ${updateCardHeadHtml(detail.id, phase, debugLabel)}
    <div class="update-card-body">
      ${phase === "output" ? entriesHtml : notice + laneSnapshot + entriesHtml}
      ${phase === "output" ? notice : ""}
      ${entriesHtml || notice ? "" : '<div class="no-update">No textual update</div>'}
    </div>`;
  card.querySelectorAll(".update-jump").forEach(button => {
    const openUpdate = async event => {
      if (hasTextSelectionWithin(button)) return;
      const entry = entries[Number(button.dataset.updateIndex)];
      const part = clickedPartKind(event);
      await selectItem(
        "call", detail.id, timelineEventFor(detail.id, phase), true, false,
      );
      activateTab("state");
      // Removed text is absent from Exact State by definition, so a click on
      // the removed half must not flash the present half there instead.
      renderExact(part === "removed" ? null : entry);
      focusMixedEntry(entry, part);
    };
    button.onclick = openUpdate;
    button.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openUpdate(event);
      }
    };
  });
  card.querySelectorAll(".fragment-change").forEach(fragmentElement => {
    const selectFragment = async event => {
      event.stopPropagation();
      if (hasTextSelectionWithin(fragmentElement)) return;
      const button = fragmentElement.closest(".update-jump");
      const entry = entries[Number(button.dataset.updateIndex)];
      const indices = fragmentElement.dataset.fragmentIndices
        .split(",")
        .map(Number);
      const focusedEntry = focusedFragmentEntry(entry, indices);
      const part = clickedPartKind(event);
      await selectItem(
        "call", detail.id, timelineEventFor(detail.id, phase), true, false,
      );
      activateTab("state");
      renderExact(part === "removed" ? null : focusedEntry);
      focusMixedFragments(indices, entry, part);
      card.querySelectorAll(".fragment-change.active").forEach(node => {
        node.classList.remove("active");
      });
      fragmentElement.classList.add("active");
    };
    fragmentElement.onclick = selectFragment;
    fragmentElement.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectFragment(event);
      }
    };
  });
  keepFollowedUpdateVisible();
}

async function loadMixedSegment(type, id, selectionVersion = null) {
  const selectionIsCurrent = () => (
    selectionVersion == null || selectionVersion === state.selectionVersion
  );
  const selectedDetail = state.detail;
  if (type !== "call") {
    if (selectionIsCurrent()) {
      state.mixedSegmentDetails = selectedDetail ? [selectedDetail] : [];
      state.mixedHistoryTruncated = false;
    }
    return selectionIsCurrent();
  }
  const selectedIndex = state.timelineItems.findIndex(
    item => item.type === type && Number(item.id) === Number(id),
  );
  if (selectedIndex < 0) {
    if (selectionIsCurrent()) {
      state.mixedSegmentDetails = selectedDetail ? [selectedDetail] : [];
      state.mixedHistoryTruncated = false;
    }
    return selectionIsCurrent();
  }
  const details = [];
  // The walk back to the checkpoint is sequential, but its fetches need not be:
  // one round trip per call made selecting a long segment take most of a second,
  // which is the window in which a second click cancels the first.
  const WINDOW = 12;
  let index = selectedIndex;
  let reachedCheckpoint = false;
  while (
    index >= 0
    && !reachedCheckpoint
    && details.length < FRONTEND_CONFIG.mixedTrace.maxCalls
  ) {
    const windowStart = Math.max(index - WINDOW + 1, 0);
    const warming = [];
    for (let at = index; at >= windowStart; at -= 1) {
      const item = state.timelineItems[at];
      if (item.type === "call" && Number(item.id) !== Number(id)) {
        warming.push(detailFor(item.type, item.id).catch(() => null));
      }
    }
    await Promise.all(warming);
    if (!selectionIsCurrent()) return false;
    for (
      ;
      index >= windowStart
        && details.length < FRONTEND_CONFIG.mixedTrace.maxCalls;
      index -= 1
    ) {
      const item = state.timelineItems[index];
      if (item.type !== "call") continue;
      const detail = Number(item.id) === Number(id)
        ? selectedDetail
        : await detailFor(item.type, item.id);
      if (!selectionIsCurrent()) return false;
      details.unshift(detail);
      if (isCheckpoint(detail)) {
        reachedCheckpoint = true;
        break;
      }
    }
  }
  if (!selectionIsCurrent()) return false;
  state.mixedSegmentDetails = details;
  state.mixedHistoryTruncated = !reachedCheckpoint && index >= 0;
  return true;
}

function boundedHistoricalEntries(segment) {
  const entries = [];
  let chars = 0;
  let omitted = false;
  // Keep the most recent history first. Older changes are useful only while
  // their retained text stays inside the render budget.
  for (let index = segment.length - 2; index >= 0; index -= 1) {
    const detail = segment[index];
    if (isCheckpoint(detail)) continue;
    for (const entry of updateEntries(detail)) {
      const cost = [
        entry.text,
        entry.oldText,
        entry.newText,
        entry.mixedOldText,
      ].reduce((total, value) => total + String(value || "").length, 0);
      if (
        chars + cost > FRONTEND_CONFIG.mixedTrace.maxHistoricalChars
      ) {
        omitted = true;
        continue;
      }
      chars += cost;
      entries.push({ ...entry, fromEarlierCall: true });
    }
  }
  return { entries, omitted };
}

function renderMixed(previousDetail = null) {
  if (!state.detail) return;
  const previousScroll = $("mixed").scrollTop;
  const preserveScroll = requestsAreSimilar(previousDetail, state.detail);
  const checkpoint = isCheckpoint(state.detail);
  const identicalTo = identicalBaseCall(state.detail);
  const segment = state.mixedSegmentDetails.length
    && state.mixedSegmentDetails[state.mixedSegmentDetails.length - 1]?.id === state.detail.id
    ? state.mixedSegmentDetails
    : [state.detail];
  const selectedEntries = checkpoint ? [] : updateEntries(state.detail);
  const historical = checkpoint
    ? { entries: [], omitted: false }
    : boundedHistoricalEntries(segment);
  const entries = checkpoint
    ? []
    : [...selectedEntries, ...historical.entries];
  const parts = stateDisplayParts(state.detail);
  const parameterEntries = entries.filter(entry => entry.category === "parameter");
  const inputEntries = entries.filter(
    entry => entry.scope === "input" && entry.category !== "parameter",
  );
  const outputEntries = mixedOutputEntries(
    entries.filter(entry => entry.scope === "output"),
  );
  const thoughtsEntries = mixedOutputEntries(
    entries.filter(entry => entry.scope === "thoughts"),
  );
  const parameterHtml = mixedStateHtml(parts.parameterText, parameterEntries);
  const contentHtml = inputContentHtml(
    state.detail,
    mixedStateHtml(parts.contentText, inputEntries, parts.contentAnchors),
  );
  const inputHtml = stateScopeHtml(
    "input",
    `input:\n${stateScopeHtml("input-params", parameterHtml, true)}\n${contentHtml}`,
  );
  const outputHtml = stateScopeHtml(
    "output",
    checkpoint
      ? escapeHtml(parts.outputText)
      : mixedStateHtml(parts.outputText, outputEntries, parts.outputAnchors),
  );
  const thoughtsHtml = state.detail.thoughts
    ? stateScopeHtml(
        "thoughts",
        checkpoint
          ? escapeHtml(parts.thoughtsText)
          : mixedStateHtml(parts.thoughtsText, thoughtsEntries, parts.thoughtsAnchors),
      )
    : "";
  $("mixed-status").textContent = checkpoint
    ? "◆ new current state"
    : identicalTo
      ? `↻ identical to call #${identicalTo}`
      : `Δ ${entries.length} accumulated update${entries.length === 1 ? "" : "s"}${
          state.mixedHistoryTruncated || historical.omitted
            ? " · recent history only"
            : ""
        }`;
  $("mixed-status").className = checkpoint ? "mixed-legend checkpoint" : "mixed-legend delta";
  $("mixed").classList.remove("empty");
  $("mixed").innerHTML = `${inputHtml}\n${thoughtsHtml}\n${outputHtml}`;
  $("mixed").scrollTop = preserveScroll ? previousScroll : 0;
}

function activateTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs button").forEach(button => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
}

function exactUpdateRanges(text, entries, anchors = null) {
  const updates = [];
  for (const entry of entries) {
    if (entry.operation === "-") continue;
    if (entry.fragments) {
      let searchFrom = 0;
      entry.fragments.forEach((fragment, fragmentIndex) => {
        // Each fragment carries its own offsets into the stored payload.
        const recorded = recordedRange(
          text,
          { ...entry, newStart: fragment.new_start, newEnd: fragment.new_end },
          anchors,
        );
        if (recorded && recorded[1] > recorded[0]) {
          updates.push({
            start: recorded[0],
            end: recorded[1],
            entry,
            fragmentIndex,
          });
          searchFrom = recorded[1];
          return;
        }
        for (const needle of searchableLines(fragment.new)) {
          const start = text.indexOf(needle, searchFrom);
          if (start < 0) continue;
          updates.push({
            start,
            end: start + needle.length,
            entry,
            fragmentIndex,
          });
          searchFrom = start + needle.length;
        }
      });
      continue;
    }
    for (const [start, end] of entryRanges(text, entry, anchors)) {
      updates.push({ start, end, entry, fragmentIndex: null });
    }
  }
  return updates.sort((left, right) => left.start - right.start || left.end - right.end);
}

function exactStateHtml(text, entries, focusEntry = null, anchors = null) {
  const ranges = exactUpdateRanges(text, entries, anchors);
  if (!ranges.length) return escapeHtml(text);
  let html = "";
  let cursor = 0;
  for (const range of ranges) {
    const { start, end, entry, fragmentIndex } = range;
    if (start < cursor) continue;
    const category = entry.category || "content";
    const focusedEntry = focusEntry?.entryKey === entry.entryKey;
    const focusedFragment = !focusEntry?.focusedFragmentIndices
      || focusEntry.focusedFragmentIndices.includes(fragmentIndex);
    const focused = focusedEntry && focusedFragment;
    const fragmentAttribute = fragmentIndex == null
      ? ""
      : ` data-output-fragment="${fragmentIndex}"`;
    html += escapeHtml(text.slice(cursor, start));
    html += `<mark class="exact-update ${focused ? "exact-focus flash " : ""}${category}-update trace-kind-${traceKind(category)} trace-op-${traceOperation(entry.operation)}" data-update-entry="${entry.entryKey}"${fragmentAttribute} role="button" tabindex="0">${escapeHtml(text.slice(start, end))}</mark>`;
    cursor = end;
  }
  return html + escapeHtml(text.slice(cursor));
}

function renderExact(focusEntry = null) {
  if (!state.detail) return;
  $("exact").classList.remove("empty");
  if (state.tab === "state") {
    const parts = stateDisplayParts(state.detail);
    const entries = isCheckpoint(state.detail) ? [] : updateEntries(state.detail);
    const parameterHtml = exactStateHtml(
      parts.topParameterText,
      entries.filter(entry => entry.category === "parameter"),
      focusEntry,
    );
    const contentHtml = inputContentHtml(
      state.detail,
      exactStateHtml(
        parts.contentText,
        entries.filter(entry => entry.scope === "input" && entry.category !== "parameter"),
        focusEntry,
        parts.contentAnchors,
      ),
    );
    const outputHtml = exactStateHtml(
      parts.outputText,
      entries.filter(entry => entry.scope === "output"),
      focusEntry,
      parts.outputAnchors,
    );
    const thoughtsHtml = state.detail.thoughts
      ? exactStateHtml(
          parts.thoughtsText,
          entries.filter(entry => entry.scope === "thoughts"),
          focusEntry,
          parts.thoughtsAnchors,
        )
      : "";
    $("exact").innerHTML = `${
      stateScopeHtml("input-params", parameterHtml)
    }\n${
      stateScopeHtml("input", `input:\n${contentHtml}`)
    }\n${
      thoughtsHtml ? stateScopeHtml("thoughts", thoughtsHtml) : ""
    }\n${stateScopeHtml("output", outputHtml)}`;
    if (focusEntry) {
      requestAnimationFrame(() => {
        const target = $("exact").querySelector(".exact-focus");
        focusScrollIntoView(target);
      });
    }
    return;
  }
  let value;
  if (state.tab === "parameters") {
    value = requestParameters(state.detail.request);
  } else if (state.tab === "raw_response") {
    value = state.detail.raw_response;
  } else {
    value = {
      id: state.detail.id,
      created_at: state.detail.created_at,
      session_id: state.detail.session_id,
      status: state.detail.status,
      chronological_parent_id: state.detail.chronological_parent_id,
      request_state_id: state.detail.request_state_id,
      parent_state_id: state.detail.parent_state_id,
      parent_source: state.detail.parent_source,
      similarity: state.detail.similarity,
      ...(state.detail.req_id ? { req_id: state.detail.req_id } : {}),
      ...(state.detail.prev_req_id ? { prev_req_id: state.detail.prev_req_id } : {}),
      ...state.detail.metadata,
    };
    delete value.run_id;
  }
  const text = yaml(value);
  if (state.tab === "parameters") {
    $("exact").innerHTML = stateScopeHtml("input-params", escapeHtml(text));
  } else {
    $("exact").textContent = text;
  }
}

// Focus decoration from a previous selection must be cleared, not added to. A
// fresh renderMixed/renderExact already wipes it, but a re-selection that reuses
// the rendered panes does not — so clearing here keeps this correct for every
// caller, and a click on one phase never leaves the other phase's marks behind.
function clearPaneFocus() {
  for (const pane of [$("mixed"), $("exact")]) {
    pane.querySelectorAll(
      ".fragment-focus, .exact-focus, .timeline-scope-focus, .checkpoint-pane-focus, .flash",
    ).forEach(node => {
      node.classList.remove(
        "fragment-focus",
        "exact-focus",
        "timeline-scope-focus",
        "checkpoint-pane-focus",
        "flash",
      );
    });
  }
}

function focusTimelineSelection(detail, preferredScope = "input") {
  clearPaneFocus();
  const panes = [$("mixed"), $("exact")];
  if (isCheckpoint(detail)) {
    for (const pane of panes) {
      const targets = [...pane.querySelectorAll("[data-state-scope]")].filter(node => (
        preferredScope === "output"
          ? node.dataset.stateScope === "output"
          : node.dataset.stateScope === "input"
      ));
      targets.forEach(node => {
        node.classList.add("checkpoint-pane-focus");
      });
      focusScrollIntoView(targets[0], "start");
    }
    return null;
  }

  const entries = updateEntries(detail);
  const scopedEntries = entries.filter(entry => (
    preferredScope === "output"
      ? entry.scope === "output" || entry.scope === "thoughts"
      : entry.scope === preferredScope
  ));
  const scopedEntryKeys = new Set(scopedEntries.map(entry => entry.entryKey));
  const primaryEntry = preferredScope === "output"
    ? scopedEntries.find(entry => entry.scope === "output") || scopedEntries[0] || null
    : scopedEntries[0] || null;
  const entryPrefix = `${detail.id}:`;
  const mixedTargets = [...$("mixed").querySelectorAll("[data-update-entry]")].filter(
    node => (
      node.dataset.updateEntry.startsWith(entryPrefix)
      && scopedEntryKeys.has(node.dataset.updateEntry)
    ),
  );
  mixedTargets.forEach(node => {
    node.classList.add("fragment-focus", "flash");
  });
  const primaryMixedTarget = primaryEntry
    ? $("mixed").querySelector(`[data-update-entry="${primaryEntry.entryKey}"]`)
    : null;
  if (primaryMixedTarget || mixedTargets[0]) {
    focusScrollIntoView(primaryMixedTarget || mixedTargets[0]);
  } else {
    const scope = $("mixed").querySelector(
      `[data-state-scope="${preferredScope}"]`,
    );
    scope?.classList.add("timeline-scope-focus", "flash");
    focusScrollIntoView(scope, "start");
  }

  const exactTargets = [...$("exact").querySelectorAll("[data-update-entry]")].filter(
    node => (
      node.dataset.updateEntry.startsWith(entryPrefix)
      && scopedEntryKeys.has(node.dataset.updateEntry)
    ),
  );
  exactTargets.forEach(node => {
    node.classList.add("exact-focus", "flash");
  });
  const primaryExactTarget = primaryEntry
    ? $("exact").querySelector(`[data-update-entry="${primaryEntry.entryKey}"]`)
    : null;
  if (primaryExactTarget) {
    focusScrollIntoView(primaryExactTarget);
  } else if (primaryEntry) {
    const stateScope = primaryEntry.category === "parameter"
      ? "input-params"
      : primaryEntry.scope;
    const scope = $("exact").querySelector(
      `[data-state-scope="${stateScope}"]`,
    );
    scope?.classList.add("timeline-scope-focus", "flash");
    focusScrollIntoView(scope, "start");
  } else {
    const scope = $("exact").querySelector(
      `[data-state-scope="${preferredScope}"]`,
    );
    scope?.classList.add("timeline-scope-focus", "flash");
    focusScrollIntoView(scope, "start");
  }
  return primaryEntry?.entryKey || null;
}

function focusTimelineUpdateCard(card, entryKey = null, preferredScope = "input") {
  document.querySelectorAll(".timeline-update-focus, .timeline-update-flash").forEach(node => {
    node.classList.remove("timeline-update-focus", "timeline-update-flash");
  });
  document.querySelectorAll(
    ".update-jump.active, .fragment-change.active, .checkpoint-section.active, .update-back-focus",
  ).forEach(node => {
    node.classList.remove("active", "update-back-focus");
  });
  if (!card) return;
  const isOutput = preferredScope === "output" || preferredScope === "thoughts";
  const entryIndex = entryKey?.split(":").at(-1);
  const primary = entryIndex == null
    ? null
    : card.querySelector(`.update-jump[data-update-index="${entryIndex}"]`);
  // A phase click focuses every change of that phase, not just the first: a call
  // that changed both its parameters and its prompt lights both. The entry's
  // data kind decides the phase it belongs to, so nested and sibling references
  // are all considered rather than only the primary one.
  const scopeSelector = isOutput
    ? ".update-jump.output-update-card, .update-jump.thoughts-update-card"
    : ".update-jump.input-update-card, .update-jump.parameter-update-card";
  const phaseUpdates = [...card.querySelectorAll(scopeSelector)];
  const checkpointTarget = card.classList.contains("checkpoint")
    ? card.querySelector(`[data-checkpoint-scope="${preferredScope}"]`)
    : null;
  const unchangedOutputTarget = isOutput
    ? card.querySelector('[data-update-scope="output"]')
    : null;
  // An input phase never falls through to the card as a whole, because the
  // card body may hold only output updates.
  const unchangedInputTarget = !isOutput
    ? card.querySelector('[data-update-scope="input"]')
    : null;
  const scopeTarget = unchangedOutputTarget || unchangedInputTarget;
  const marks = phaseUpdates.length
    ? phaseUpdates
    : [checkpointTarget || scopeTarget || card];
  const scrollTarget = primary && marks.includes(primary) ? primary : marks[0];
  for (const mark of marks) {
    // Force a fresh animation frame so a second click on the same event pulses
    // the corresponding updates again.
    void mark.offsetWidth;
    mark.classList.add("timeline-update-focus", "timeline-update-flash");
  }
  focusScrollIntoView(
    scrollTarget,
    phaseUpdates.length || checkpointTarget || scopeTarget ? "center" : "start",
  );
}

function updateFragmentForIndex(button, fragmentIndex) {
  if (fragmentIndex == null) return null;
  return [...button.querySelectorAll(".fragment-change")].find(fragment => (
    fragment.dataset.fragmentIndices
      ?.split(",")
      .map(Number)
      .includes(fragmentIndex)
  )) || null;
}

function markUpdateTarget(card, target, button = null) {
  card.querySelectorAll(
    ".update-jump.active, .fragment-change.active, .checkpoint-section.active, .update-back-focus",
  ).forEach(node => {
    node.classList.remove("active", "update-back-focus");
  });
  card.classList.add("active");
  button?.classList.add("active");
  target.classList.add("active", "update-back-focus");
  focusScrollIntoView(target);
  window.setTimeout(() => target.classList.remove("update-back-focus"), 1600);
}

function focusStateUpdateEntry(entryKey, fragmentIndex = null) {
  for (const pane of [$("mixed"), $("exact")]) {
    pane.querySelectorAll(
      ".fragment-focus, .exact-focus, .checkpoint-pane-focus, .timeline-scope-focus",
    ).forEach(node => {
      node.classList.remove(
        "fragment-focus",
        "exact-focus",
        "checkpoint-pane-focus",
        "timeline-scope-focus",
        "flash",
      );
    });
    let targets = [...pane.querySelectorAll(`[data-update-entry="${entryKey}"]`)];
    if (fragmentIndex != null) {
      const fragmentTargets = targets.filter(
        node => Number(node.dataset.outputFragment) === fragmentIndex,
      );
      if (fragmentTargets.length) targets = fragmentTargets;
    }
    targets.forEach(node => {
      // Restart the pulse when the same update is clicked repeatedly.
      void node.offsetWidth;
      node.classList.add(
        pane.id === "mixed" ? "fragment-focus" : "exact-focus",
        "flash",
      );
    });
    focusScrollIntoView(targets[0]);
  }
}

async function focusUpdateFromState(element) {
  const updateElement = element.closest("[data-update-entry]");
  if (updateElement) {
    const [rawCallId, rawEntryIndex] = updateElement.dataset.updateEntry.split(":");
    const callId = Number(rawCallId);
    const entryIndex = Number(rawEntryIndex);
    const key = `call:${callId}`;
    const fragmentIndex = updateElement.dataset.outputFragment == null
      ? null
      : Number(updateElement.dataset.outputFragment);
    focusStateUpdateEntry(updateElement.dataset.updateEntry, fragmentIndex);
    const scope = updateElement.closest("[data-state-scope]")?.dataset.stateScope;
    const phase = scope === "output" || scope === "thoughts"
      || updateElement.classList.contains("output-update")
      || updateElement.classList.contains("thoughts-update")
      ? "output"
      : "input";
    setTimelineFocus(key, phase, true);

    const card = updateCardFor(key, phase);
    if (!card) return;
    await loadUpdateCard(card);
    document.querySelectorAll(".update-card.active").forEach(node => {
      node.classList.remove("active");
    });
    const button = card.querySelector(`.update-jump[data-update-index="${entryIndex}"]`);
    if (!button) return;
    const fragment = updateFragmentForIndex(button, fragmentIndex);
    markUpdateTarget(card, fragment || button, button);
    return;
  }

  if (!state.selected) return;
  const key = itemKey(state.selected);
  const scope = element.closest("[data-state-scope]")?.dataset.stateScope;
  if (!scope) return;
  const phase = scope === "output" || scope === "thoughts" ? "output" : "input";
  const card = updateCardFor(key, phase);
  if (!card) return;
  await loadUpdateCard(card);
  setTimelineFocus(key, phase, true);

  for (const pane of [$("mixed"), $("exact")]) {
    pane.querySelectorAll(
      ".fragment-focus, .exact-focus, .checkpoint-pane-focus, .timeline-scope-focus",
    ).forEach(node => {
      node.classList.remove(
        "fragment-focus",
        "exact-focus",
        "checkpoint-pane-focus",
        "timeline-scope-focus",
        "flash",
      );
    });
  }

  const entries = isCheckpoint(state.detail)
    ? []
    : updateEntries(state.detail).filter(entry => (
        scope === "input-params"
          ? entry.category === "parameter"
          : entry.scope === scope && entry.category !== "parameter"
      ));
  const entryKeys = new Set(entries.map(entry => entry.entryKey));
  let foundEntry = false;
  for (const pane of [$("mixed"), $("exact")]) {
    const targets = [...pane.querySelectorAll("[data-update-entry]")].filter(
      node => entryKeys.has(node.dataset.updateEntry),
    );
    targets.forEach(node => {
      node.classList.add(
        pane.id === "mixed" ? "fragment-focus" : "exact-focus",
        "flash",
      );
    });
    if (targets.length) {
      foundEntry = true;
      focusScrollIntoView(targets[0]);
    } else {
      const targetScope = pane.querySelector(`[data-state-scope="${scope}"]`);
      targetScope?.classList.add(
        isCheckpoint(state.detail) ? "checkpoint-pane-focus" : "timeline-scope-focus",
        "flash",
      );
      focusScrollIntoView(targetScope, "start");
    }
  }

  document.querySelectorAll(".update-card.active").forEach(node => {
    node.classList.remove("active");
  });
  const primaryEntryKey = entries[0]?.entryKey || null;
  focusTimelineUpdateCard(card, primaryEntryKey, scope);
  if (card.classList.contains("checkpoint")) {
    const section = card.querySelector(`[data-checkpoint-scope="${scope}"]`);
    if (section) markUpdateTarget(card, section);
  } else if (entries.length) {
    entries.forEach(entry => {
      const entryIndex = entry.entryKey.split(":").at(-1);
      card.querySelector(`.update-jump[data-update-index="${entryIndex}"]`)
        ?.classList.add("timeline-update-focus", "timeline-update-flash");
    });
  } else if (!foundEntry) {
    card.classList.add("active");
  }
}

function bindStateBackReferences(pane) {
  pane.addEventListener("click", event => {
    if (hasTextSelectionWithin(pane)) return;
    const updateTarget = event.target.closest("[data-update-entry]");
    if (updateTarget) {
      focusUpdateFromState(updateTarget);
      return;
    }
    // Plain reconstructed content is selectable state, not an update reference.
    // Whole-scope navigation belongs to its visible label. A checkpoint is the
    // exception because its complete snapshot scope has one owning Updates section.
    const scopeTarget = event.target.closest(".state-scope-label")
      ?.closest("[data-state-scope]")
      || (isCheckpoint(state.detail)
        ? event.target.closest("[data-state-scope]")
        : null);
    const target = scopeTarget;
    if (target) focusUpdateFromState(target);
  });
  pane.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest(
      "[data-update-entry], .state-scope-label",
    );
    if (!target) return;
    event.preventDefault();
    focusUpdateFromState(
      target.closest("[data-update-entry], [data-state-scope]"),
    );
  });
}

// Two clicks on one call are one selection. Without this, the second click
// bumps the selection version and cancels the load the first click started, so
// the focus it was about to apply never lands — on a long segment that load
// takes long enough for an impatient second click to be the normal case.
async function selectItem(
  type,
  id,
  element = null,
  scrollTimeline = true,
  focusSelection = element !== null,
) {
  const key = `${type}:${id}`;
  if (state.pendingSelection?.key === key) {
    await state.pendingSelection.promise.catch(() => {});
    return applySelection(type, id, element, scrollTimeline, focusSelection);
  }
  const promise = applySelection(type, id, element, scrollTimeline, focusSelection);
  state.pendingSelection = { key, promise };
  try {
    return await promise;
  } finally {
    if (state.pendingSelection?.promise === promise) state.pendingSelection = null;
  }
}

async function applySelection(
  type,
  id,
  element = null,
  scrollTimeline = true,
  focusSelection = element !== null,
) {
  const button = element || document.querySelector(`.timeline-item[data-key="${type}:${id}"]`);
  const phase = button?.dataset.phase || "input";
  const key = `${type}:${id}`;
  const selectionVersion = ++state.selectionVersion;
  setTimelineFocus(key, phase, scrollTimeline, button);
  document.querySelectorAll(".update-card.active").forEach(node => node.classList.remove("active"));
  const updateCard = updateCardFor(key, phase);
  updateCard?.classList.add("active");
  const previousDetail = state.detail;
  // Re-selecting the call already on screen must not rebuild Mixed: for a long
  // segment that costs a visible pause, and every entry click inside one card
  // would restart it — long enough for the next click to abort the previous
  // selection, so the focus it was about to apply never arrived.
  const alreadyRendered = state.selected
    && state.selected.type === type
    && Number(state.selected.id) === Number(id)
    && Number(state.detail?.id) === Number(id)
    && state.mixedSegmentDetails.at(-1)?.id === state.detail?.id;
  state.selected = { type, id };
  state.selectedPhase = phase;
  syncBranchGraphSelection();
  if (alreadyRendered) {
    activateTab("state");
    if (focusSelection) {
      if (updateCard) await loadUpdateCard(updateCard);
      if (selectionVersion !== state.selectionVersion) return;
      const renderedEntryKey = focusTimelineSelection(state.detail, phase);
      focusTimelineUpdateCard(updateCard, renderedEntryKey, phase);
    }
    return;
  }
  const detail = await detailFor(type, id);
  if (selectionVersion !== state.selectionVersion) return;
  state.detail = detail;
  if (!await loadMixedSegment(type, id, selectionVersion)) return;
  const score = state.detail.similarity == null
    ? ""
    : ` · ${(state.detail.similarity * 100).toFixed(0)}%`;
  // A caller-declared predecessor is the trustworthy lineage; show it in place
  // of the inferred parent source when present.
  const parentLabel = state.detail.prev_req_id
    ? `req ${state.detail.prev_req_id}`
    : state.detail.parent_source || "root";
  const reqLabel = state.detail.req_id ? `${state.detail.req_id} · ` : "";
  $("lineage").textContent =
    `${reqLabel}state S${state.detail.request_state_id} ← ${parentLabel}${score}`;
  activateTab("state");
  renderMixed(previousDetail);
  renderExact();
  if (focusSelection) {
    if (updateCard) await loadUpdateCard(updateCard);
    if (selectionVersion !== state.selectionVersion) return;
    const focusedEntryKey = focusTimelineSelection(state.detail, phase);
    focusTimelineUpdateCard(updateCard, focusedEntryKey, phase);
  }
}

async function rebuildTimeline(items, previousSelected, followNewItems) {
  const timelineViewport = captureTimelineViewport();
  const updatesScroll = $("updates").scrollTop;
  state.details.clear();
  $("timeline").innerHTML = "";
  $("updates").innerHTML = "";
  state.observer?.disconnect();
  state.observer = null;
  state.checkpointKeys.clear();
  renderUpdateCards(items);
  renderTimelineEvents(items);
  // A first render of a session has no viewport to preserve, and it selects the
  // newest call, so it opens at that end whatever Follow says.
  restoreTimelineViewport(timelineViewport, true);
  $("updates").scrollTop = updatesScroll;
  if (!items.length) {
    state.selected = null;
    state.timelineFocus = null;
    state.detail = null;
    state.mixedHistoryTruncated = false;
    $("mixed").textContent = "No LLM calls in this session.";
    $("exact").textContent = "No current state.";
    $("updates").innerHTML = '<div class="empty-session">No updates in this session.</div>';
    return;
  }
  const chosen = followNewItems
    ? items[items.length - 1]
    : items.find(item => itemKey(item) === previousSelected) || items[items.length - 1];
  await selectItem(chosen.type, chosen.id, null, false, true);
}

async function refreshChangedItem(item) {
  const key = itemKey(item);
  state.details.delete(key);
  for (const card of document.querySelectorAll(`.update-card[data-key="${key}"]`)) {
    delete card.dataset.loaded;
    await loadUpdateCard(card);
  }
  const status = document.querySelector(
    `.timeline-output[data-call-key="${key}"] .item-meta span:last-child`,
  );
  if (status) status.textContent = item.status === "running" ? "waiting" : item.status;
  document.querySelector(`.timeline-output[data-call-key="${key}"]`)
    ?.classList.toggle("status-running", item.status === "running");
  if (state.selected && itemKey(state.selected) === key) {
    const previousDetail = state.detail;
    state.detail = await detailFor(item.type, item.id);
    await loadMixedSegment(item.type, item.id);
    renderMixed(previousDetail);
    renderExact();
    const focusedEntryKey = focusTimelineSelection(
      state.detail,
      state.selectedPhase,
    );
    focusTimelineUpdateCard(
      updateCardFor(key, state.selectedPhase),
      focusedEntryKey,
      state.selectedPhase,
    );
  }
}

async function loadTimeline() {
  const sessionQuery = state.session ? `&session=${encodeURIComponent(state.session)}` : "";
  const records = await fetchJson(`/api/timeline?limit=1000${sessionQuery}`);
  const items = records.filter(item => item.type === "call");
  renderWaitingCalls(items);
  const signature = items.map(item => (
    `${itemKey(item)}:${item.status}:${item.branch_id}:${item.duration_ms ?? ""}`
  )).join("|");
  if (signature === state.timelineSignature) return false;

  const previousItems = state.timelineItems;
  const previousSelected = state.selected ? itemKey(state.selected) : null;
  const followNewItems = state.followNewItems;
  const updates = $("updates");
  const updatesScroll = updates.scrollTop;
  const updatesUserScrollVersion = state.updatesUserScrollVersion;
  state.timelineSignature = signature;
  if (previousItems.length === 0) {
    state.lastTimelineKey = items.length ? itemKey(items[items.length - 1]) : null;
    state.timelineItems = items;
    await rebuildTimeline(items, previousSelected, followNewItems);
    return true;
  }

  const previousByKey = new Map(previousItems.map(item => [itemKey(item), item]));
  const changed = [];
  for (const item of items) {
    const previous = previousByKey.get(itemKey(item));
    if (previous && previous.status !== item.status) {
      // Apply the new status first: a completed call earns an output card, and
      // its arrival time decides where that card belongs in the sequence.
      Object.assign(previous, item);
      changed.push(item);
    }
  }
  const appended = items.filter(item => !previousByKey.has(itemKey(item)));
  state.timelineItems = [...previousItems, ...appended];
  const updatesViewport = captureUpdatesViewport();
  renderUpdateCards(state.timelineItems);
  // Preserve the viewport immediately after the synchronous reorder. Do not
  // restore it after awaited detail loading: the user may scroll meanwhile.
  if (state.updatesUserScrollVersion === updatesUserScrollVersion) {
    restoreUpdatesViewport(updatesViewport, updatesScroll);
  }
  for (const item of changed) await refreshChangedItem(item);
  const timelineViewport = captureTimelineViewport();
  renderTimelineEvents(state.timelineItems);
  // Output events can be inserted above the viewport when an earlier running
  // call completes. Keep the same visible event at the same screen position.
  restoreTimelineViewport(timelineViewport);
  state.lastTimelineKey = state.timelineItems.length
    ? itemKey(state.timelineItems[state.timelineItems.length - 1])
    : null;
  if (appended.length && followNewItems) {
    // Follow means the newest item is the one being watched, so bring it into
    // view even when the viewport had drifted away from the newest end.
    const last = appended[appended.length - 1];
    await selectItem(last.type, last.id, null, true, true);
  }
  return true;
}

async function loadSessions() {
  const sessions = await fetchJson("/api/sessions");
  const signature = JSON.stringify(sessions);
  if (signature === state.sessionsSignature) return false;
  state.sessionsSignature = signature;
  const select = $("session");
  const previous = state.session;
  const previousMissing = previous
    && !sessions.some(session => session.session_id === previous);
  const displayedSessions = previousMissing
    ? [{
        session_id: previous,
        calls: state.timelineItems.length,
        retained: true,
      }, ...sessions]
    : sessions;
  select.innerHTML = "";
  for (const session of displayedSessions) {
    const option = document.createElement("option");
    option.value = session.session_id;
    option.textContent = `${session.session_id} · ${session.calls} calls${session.retained ? " · retained in viewer" : ""}`;
    select.appendChild(option);
  }
  state.latestSession = sessions[0]?.session_id || null;
  state.session = previous || state.latestSession;
  if (state.session) select.value = state.session;
  if (state.session !== previous) {
    state.timelineSignature = "";
    state.timelineItems = [];
    state.mixedSegmentDetails = [];
    state.mixedHistoryTruncated = false;
    state.selected = null;
    state.timelineFocus = null;
  }
  return true;
}

async function loadStats() {
  const data = await fetchJson("/api/stats");
  const saved = data.logical_bytes
    ? Math.max(0, 100 - (data.stored_bytes / data.logical_bytes * 100))
    : 0;
  const size = `${(data.file_bytes / 1024 / 1024).toFixed(2)} MB`;
  const limit = data.max_file_bytes
    ? ` / ${(data.max_file_bytes / 1024 / 1024).toFixed(0)} MB`
    : "";
  $("stats").textContent =
    `${data.calls} calls · ${size}${limit} · ${saved.toFixed(0)}% blob reduction`;
}

async function runSearch(event) {
  event.preventDefault();
  const query = $("search").value.trim();
  const box = $("search-results");
  if (!query) {
    box.classList.add("hidden");
    return;
  }
  const sessionPart = state.session ? `&session=${encodeURIComponent(state.session)}` : "";
  const allResults = await fetchJson(`/api/search?q=${encodeURIComponent(query)}${sessionPart}`);
  if ($("search").value.trim() !== query) return;
  const results = allResults.filter(result => result.owner_type === "call");
  box.innerHTML = `
    <div class="search-results-head">
      <strong>${results.length} matches</strong>
      <button type="button" aria-label="Hide search results">×</button>
    </div>`;
  box.querySelector("button").onclick = () => box.classList.add("hidden");
  for (const result of results) {
    const node = document.createElement("div");
    node.className = "result";
    node.innerHTML = `<small>LLM call #${result.owner_id} · ${result.field}</small>${result.snippet}`;
    node.onclick = () => {
      box.classList.add("hidden");
      selectItem("call", result.owner_id);
    };
    box.appendChild(node);
  }
  box.classList.remove("hidden");
}

document.querySelectorAll(".tabs button").forEach(button => {
  button.onclick = () => {
    activateTab(button.dataset.tab);
    renderExact();
  };
});

bindStateBackReferences($("mixed"));
bindStateBackReferences($("exact"));
for (const eventName of ["wheel", "touchstart", "pointerdown"]) {
  $("updates").addEventListener(eventName, cancelFollowedUpdate, { passive: true });
}
$("updates").addEventListener("keydown", event => {
  if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
    cancelFollowedUpdate();
  }
});

$("refresh").onclick = async () => {
  await loadSessions();
  await loadTimeline();
  await loadStats();
};
function setTimelineView(view) {
  state.timelineView = FRONTEND_CONFIG.branchGraph.enabled && view === "branches"
    ? "branches"
    : "list";
  localStorage.setItem("insequent.timelineView", state.timelineView);
  applyTimelineView();
  // Bring the selected node into view when switching to the graph.
  if (state.timelineView === "branches" && focusedTimelineKey()) {
    const node = $("branch-graph").querySelector(
      `.branch-node[data-key="${focusedTimelineKey()}"]`,
    );
    node?.scrollIntoView({ behavior: "auto", block: "nearest", inline: "nearest" });
  }
}
$("view-list")?.addEventListener("click", () => setTimelineView("list"));
$("view-branches")?.addEventListener("click", () => setTimelineView("branches"));
$("orient-vertical")?.addEventListener("click", () => setBranchOrientation("vertical"));
$("orient-horizontal")?.addEventListener("click", () => setBranchOrientation("horizontal"));
function setBranchOrientation(orient) {
  state.branchOrientation = orient;
  localStorage.setItem("insequent.branchOrientation", orient);
  applyTimelineView();
}
$("follow-new-items").onchange = async () => {
  state.followNewItems = $("follow-new-items").checked;
  if (!state.followNewItems || !state.timelineItems.length) return;
  const latest = state.timelineItems[state.timelineItems.length - 1];
  await selectItem(latest.type, latest.id, null, true, true);
};
$("search-form").onsubmit = runSearch;
$("search").addEventListener("input", () => {
  if (!$("search").value.trim()) $("search-results").classList.add("hidden");
});
$("search").addEventListener("keydown", event => {
  if (event.key === "Escape") $("search-results").classList.add("hidden");
});
$("session").onchange = async () => {
  state.session = $("session").value;
  state.selected = null;
  state.detail = null;
  state.details.clear();
  state.mixedSegmentDetails = [];
  state.mixedHistoryTruncated = false;
  state.timelineSignature = "";
  state.timelineItems = [];
  $("search-results").classList.add("hidden");
  await loadTimeline();
};

async function liveTick() {
  if (state.liveBusy) return;
  state.liveBusy = true;
  $("live-indicator").classList.add("waiting");
  try {
    await loadSessions();
    await loadTimeline();
    await loadStats();
    $("live-indicator").textContent = "● LIVE";
  } catch {
    $("live-indicator").textContent = "● OFFLINE";
  } finally {
    $("live-indicator").classList.remove("waiting");
    state.liveBusy = false;
  }
}

function initTimelineResize() {
  const main = document.querySelector("main");
  const handle = $("timeline-resizer");
  if (!main || !handle) return;

  const stored = Number(localStorage.getItem(FRONTEND_CONFIG.timeline.storageKey));
  const clampWidth = width => {
    const maximum = Math.max(
      FRONTEND_CONFIG.timeline.minWidth,
      main.clientWidth - FRONTEND_CONFIG.timeline.otherPanesMinWidth,
    );
    return Math.round(Math.min(maximum, Math.max(FRONTEND_CONFIG.timeline.minWidth, width)));
  };

  const setWidth = (width, persist = true) => {
    const next = clampWidth(width);
    main.style.setProperty("--timeline-pane-width", `${next}px`);
    handle.setAttribute("aria-valuemin", String(FRONTEND_CONFIG.timeline.minWidth));
    handle.setAttribute("aria-valuemax", String(clampWidth(Number.MAX_SAFE_INTEGER)));
    handle.setAttribute("aria-valuenow", String(next));
    if (persist) {
      localStorage.setItem(FRONTEND_CONFIG.timeline.storageKey, String(next));
    }
  };
  if (Number.isFinite(stored) && stored >= FRONTEND_CONFIG.timeline.minWidth) {
    setWidth(stored, false);
  }

  let startX = 0;
  let startWidth = 0;
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    startX = event.clientX;
    startWidth = document.querySelector(".timeline-pane").getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    document.body.classList.add("resizing-pane");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", event => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    setWidth(startWidth + event.clientX - startX);
  });
  const stopResize = event => {
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    handle.classList.remove("dragging");
    document.body.classList.remove("resizing-pane");
  };
  handle.addEventListener("pointerup", stopResize);
  handle.addEventListener("pointercancel", stopResize);
  handle.addEventListener("dblclick", () => {
    main.style.removeProperty("--timeline-pane-width");
    localStorage.removeItem(FRONTEND_CONFIG.timeline.storageKey);
  });
  handle.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
    const current = document.querySelector(".timeline-pane").getBoundingClientRect().width;
    if (event.key === "Home") {
      setWidth(FRONTEND_CONFIG.timeline.minWidth);
    } else {
      setWidth(current + (event.key === "ArrowRight" ? 16 : -16));
    }
    event.preventDefault();
  });
  window.addEventListener("resize", () => {
    if (!main.style.getPropertyValue("--timeline-pane-width")) return;
    const current = document.querySelector(".timeline-pane").getBoundingClientRect().width;
    setWidth(current, false);
  });
}

async function start() {
  initTimelineResize();
  applyTimelineView();
  await loadSessions();
  await Promise.all([loadTimeline(), loadStats()]);
  applyTimelineView();
  setInterval(liveTick, 1000);
}

start().catch(error => {
  $("mixed").textContent = `Failed to load trace: ${error.message}`;
});
