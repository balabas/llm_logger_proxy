const FRONTEND_CONFIG = Object.freeze({
  branchGraph: Object.freeze({
    enabled: true,
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
  timelineEpoch: 0,
  searchFocus: null,
  lastTimelineKey: null,
  timelineItems: [],
  mixedSegmentDetails: [],
  mixedHistoryComplete: true,
  mixedHistoryLoading: false,
  liveBusy: false,
  live: new Map(),
  liveSource: null,
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

// A stored call id is numeric; an in-flight (synthetic) streaming call uses a
// string id "live-<n>". Keep live ids as strings so they survive Number()-based
// dataset round-trips.
function parseItemId(value) {
  const text = String(value);
  return text.startsWith("live-") ? text : Number(text);
}

function isLiveId(id) {
  return String(id).startsWith("live-");
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

function parsedStructuredJson(value) {
  if (typeof value !== "string") return value;
  let parsed = value;
  for (let depth = 0; depth < 2 && typeof parsed === "string"; depth += 1) {
    const candidate = parsed.trim();
    if (!candidate || !"[{\"".includes(candidate[0])) break;
    try {
      parsed = JSON.parse(candidate);
    } catch {
      return value;
    }
  }
  return parsed && typeof parsed === "object" ? parsed : value;
}

function messageForDisplay(message) {
  if (!message || typeof message !== "object") return message;
  // Message objects may arrive content-first from reconstructed storage or
  // role-first from a live request. Normalize the visual field order while
  // retaining every provider-specific field after the two primary fields.
  const displayed = {};
  if (Object.hasOwn(message, "role")) displayed.role = message.role;
  if (Object.hasOwn(message, "content")) displayed.content = message.content;
  for (const [key, value] of Object.entries(message)) {
    if (key !== "role" && key !== "content") displayed[key] = value;
  }
  if (displayed.role === "tool" && typeof displayed.content === "string") {
    displayed.content = parsedStructuredJson(displayed.content);
  }
  if (Array.isArray(displayed.tool_calls)) {
    displayed.tool_calls = displayed.tool_calls.map(call => {
      if (!call || typeof call !== "object") return call;
      const shownCall = {...call};
      if (call.function && typeof call.function === "object") {
        shownCall.function = {
          ...call.function,
          arguments: parsedStructuredJson(call.function.arguments),
        };
      }
      return shownCall;
    });
  }
  if (displayed.function_call && typeof displayed.function_call === "object") {
    displayed.function_call = {
      ...displayed.function_call,
      arguments: parsedStructuredJson(displayed.function_call.arguments),
    };
  }
  return displayed;
}

function requestContent(request) {
  if (Array.isArray(request?.messages)) {
    return {messages: request.messages.map(messageForDisplay)};
  }
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
  // A context-exceeded or cancelled call may stream no usable parsed data, so
  // carry the raw body into Output as a diagnostic fallback. A reasoning-only
  // response is different even when cancelled: its SSE was parsed into Thoughts
  // and an empty public Output is legitimate; showing the raw transport there
  // would duplicate Thoughts and falsely label parsed data as unparsed.
  const rawText = typeof detail.raw_response === "string" ? detail.raw_response : "";
  const outputEmpty = output == null
    || (typeof output === "string" && output.trim() === "");
  const hasParsedThoughts = typeof detail.thoughts === "string"
    && detail.thoughts.trim() !== "";
  const outputIsRaw = outputEmpty
    && rawText.trim() !== ""
    && !hasParsedThoughts;
  return {
    outputIsRaw,
    rawOutputText: `${outputLabel}${rawText}`,
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

function stateScopeHtml(kind, html, nested = false, originAttributes = "", badge = "") {
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
  const badgeHtml = badge
    ? `<span class="scope-badge" title="Parsed output was empty — showing the raw captured response.">${escapeHtml(badge)}</span>`
    : "";
  return `<span class="state-scope ${nested ? "state-subscope " : ""}trace-kind-${kind}${originAttributes ? " checkpoint-origin" : ""}" data-state-scope="${kind}"><span class="state-scope-label" role="button" tabindex="0" aria-label="${labels[kind]} scope">${labels[kind]}${badgeHtml}</span><span class="state-scope-content"${originAttributes}>${content}</span></span>`;
}

function inheritedCheckpointDetail() {
  if (!state.detail || isCheckpoint(state.detail)) return null;
  return state.mixedSegmentDetails.find(detail => isCheckpoint(detail)) || null;
}

function scopeIsInherited(detail, scope, currentEntries) {
  if (!detail || isCheckpoint(detail)) return false;
  if (scope === "output") {
    // An empty value has no text provenance to navigate to. Its scope represents
    // the selected call's lack of output, even if another call was also empty.
    return String(detail.response || "").trim() !== ""
      && detail.output_diff?.mode === "unchanged";
  }
  if (scope === "thoughts") {
    return String(detail.thoughts || "").trim() !== ""
      && detail.thoughts_diff?.mode === "unchanged";
  }
  // Input scopes contain a mixture of inherited and current ranges. Their
  // individual update marks intercept current text; plain text belongs to the
  // checkpoint and retains the scope-level fallback origin.
  return true;
}

function checkpointOriginAttributes(detail, scope) {
  if (!detail) return "";
  return ` data-checkpoint-call="${detail.id}" data-checkpoint-scope="${scope}" title="Inherited from checkpoint call #${detail.id}"`;
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
  // line, so keep any leading tags and relabel what follows them. Either field
  // may lead the list item (the store serializes content-first; a live call
  // carries the request as sent — usually role-first), so the leading "- " dash
  // is optional on both.
  let roleIndex = 0;
  content = content
    .replace(
      /^((?:<[^>]+>)*)[ \t]*(?:-[ \t]+)?content:(?: )?/gm,
      '$1<span class="message-field-label">Content</span> ',
    )
    .replace(
      /^((?:<[^>]+>)*)[ \t]*(?:-[ \t]+)?role:(?: )?/gm,
      (_match, leadingTags) => {
        const separator = roleIndex > 0
          ? '<span class="message-separator" aria-hidden="true"></span>'
          : "";
        roleIndex += 1;
        return `${separator}${leadingTags}<span class="message-field-label message-role-label">Role</span> `;
      },
    )
    .replace(
      /(<span class="message-field-label[^"]*">[^<]+<\/span>) &quot;/g,
      "$1 ",
    )
    // A closing update-mark tag can now sit between the trailing quote and the
    // line end. Remove that serializer quote only on the Content/Role lines we
    // relabelled above; fields such as tool-call name/id/type retain both quotes.
    .replace(
      /^(.*message-field-label.*)&quot;(?=(?:<\/[^>]+>)*(?:\n|$))/gm,
      "$1",
    );
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
          oldText: yaml(messageForDisplay(message)),
          newText: "",
          mixedOldText: yaml(messageForDisplay(message)),
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
          // When a replacement removes more messages than it adds, anchor the
          // unpaired removals at the last replacement message. Mixed then sorts
          // the zero-width red removals before that message's green span instead
          // of parking them at the end of the scope after the new text.
          const replacementAnchorIndex = newStartIndex == null
            ? null
            : newStartIndex + Math.max(newMessages.length - 1, 0);
          entries.push({
            label: `${operationName(operation)} input · ${message.role || "message"}`,
            text: markedValue(operation === "+" ? "+" : "−", message),
            oldText: oldMessage ? yaml(messageForDisplay(oldMessage)) : "",
            newText: newMessage ? yaml(messageForDisplay(newMessage)) : "",
            mixedOldText: oldMessage ? yaml(messageForDisplay(oldMessage)) : "",
            messageIndex: newMessage
              ? (newStartIndex == null ? null : newStartIndex + index)
              : replacementAnchorIndex,
            needle: newMessage ? firstSearchableValue(newMessage.content) : "",
            needles: newMessage ? searchableLines(newMessage.content) : [],
            scope: "input",
            category: "input",
            operation,
            replacementOrder: index,
          });
        } else {
          entries.push({
            label: `Changed input · ${newMessage.role || oldMessage.role || "message"}`,
            text: transitionText({ op: "~", old: oldMessage, new: newMessage }),
            oldText: yaml(messageForDisplay(oldMessage)),
            newText: yaml(messageForDisplay(newMessage)),
            mixedOldText: firstSearchableValue(oldMessage.content),
            messageIndex: newStartIndex == null ? null : newStartIndex + index,
            needle: firstSearchableValue(newMessage.content),
            needles: searchableLines(newMessage.content),
            scope: "input",
            category: "input",
            operation: "~",
            replacementOrder: index,
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
        newText: yaml(messageForDisplay(message)),
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

function removedTextAdditionOrigins(entries) {
  const originsByRemoval = new Map();
  const activeOrigins = new Map();
  const chronological = [...entries].sort((left, right) => (
    Number(left.callId) - Number(right.callId)
    || Number(left.entryIndex) - Number(right.entryIndex)
    || Number(left.fragmentIndex ?? -1) - Number(right.fragmentIndex ?? -1)
  ));
  for (const entry of chronological) {
    const oldText = entry.oldText || entry.mixedOldText || "";
    const newText = entry.newText || "";
    if ((entry.operation === "-" || entry.operation === "~") && oldText) {
      const origins = activeOrigins.get(oldText) || [];
      const origin = origins.pop();
      if (origin) originsByRemoval.set(entry, origin);
      if (origins.length) activeOrigins.set(oldText, origins);
      else activeOrigins.delete(oldText);
    }
    if ((entry.operation === "+" || entry.operation === "~") && newText) {
      const origins = activeOrigins.get(newText) || [];
      origins.push(entry);
      activeOrigins.set(newText, origins);
    }
  }
  return originsByRemoval;
}

// A removed part whose text was already present before the loaded history begins
// has no explicit "+" that introduced it. Attribute it to the earliest loaded
// call whose reconstructed state still contains it, so the "where added"
// reference can point somewhere rather than being absent. A distinctive line
// survives reformatting between the diff text and a call's rendered state better
// than the whole block, so match on the removed part's longest line.
// Search every scope of a call, not just the removal's own scope: in agentic
// loops a call's output (a step_result) becomes a later call's input, so removed
// input text often originated as an earlier call's output.
// Per-render budget: the maximum number of (removed-text, detail) substring scans
// the origin search may run. Sized to cover ordinary segments fully (early hits
// return before spending much) while capping pathological deep/large ones so the
// page cannot freeze. MIXED_ORIGIN_HAYSTACK_CAP bounds each scan's cost so the
// budget maps to bounded time even when a call's state is multiple megabytes.
const MIXED_ORIGIN_SCAN_BUDGET = 6000;
const MIXED_ORIGIN_HAYSTACK_CAP = 400000;
let mixedOriginScanBudget = 0;

const mixedOriginPartsCache = new WeakMap();
function mixedOriginSearchText(detail) {
  let combined = mixedOriginPartsCache.get(detail);
  if (combined == null) {
    const parts = stateDisplayParts(detail);
    combined = [
      parts.parameterText,
      parts.contentText,
      parts.outputText,
      parts.thoughtsText,
    ].filter(Boolean).join("\n").slice(0, MIXED_ORIGIN_HAYSTACK_CAP);
    mixedOriginPartsCache.set(detail, combined);
  }
  return combined;
}

function removedTextOriginCall(removedText) {
  // JSON string values keep their newlines escaped ("\\n"), so a whole block can
  // arrive as one giant escaped line. Split on both real and escaped newlines and
  // common structural punctuation so the needle is a short, distinctive segment
  // that survives reformatting, rather than a huge exact-match-only string.
  const segments = (removedText || "")
    .split(/\\+n|\r?\n|","|":\s*"|[[\]{}]/)
    .map(part => part.replace(/^[\s"',:*]+|[\s"',:*]+$/g, ""))
    .filter(part => part.length >= 6);
  if (!segments.length) return null;
  // Prefer the longest distinctive segment, but keep a few candidates: the single
  // longest can still be a coincidence, so a call must merely contain one of them.
  const needles = [...new Set(segments)]
    .sort((left, right) => right.length - left.length)
    .slice(0, 6);
  // mixedSegmentDetails runs earliest → selected, so the first match is the
  // earliest loaded call that still shows this text.
  for (const detail of state.mixedSegmentDetails) {
    if (mixedOriginScanBudget <= 0) return null;  // spent: leave this part un-underlined
    mixedOriginScanBudget -= 1;
    const haystack = mixedOriginSearchText(detail);
    if (needles.some(needle => haystack.includes(needle))) return detail.id;
  }
  return null;
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
  const additionOrigins = removedTextAdditionOrigins(entries);
  // Resolve the "where added" call once per removal (grouped by its owning
  // change), using the whole removed text, then share it across every fragment
  // of that removal. Resolving per token-fragment would leave short fragments
  // (keys, punctuation, a single word) without a distinctive line to match, so
  // only part of one removed block would carry the reference.
  // Search on all available removed text for a change, not just mixedOldText —
  // for a message replacement that field is only the first value, while the full
  // block (oldText) is what is rendered and what an earlier call still contains.
  // A distinctive needle needs only a short prefix, so cap the text kept per
  // change. Without this, a large block (e.g. a task prompt) repeated across
  // hundreds of removed marks would grow one string by O(n^2) concatenation and
  // then be hashed whole as a cache key — enough to freeze the page.
  const ORIGIN_TEXT_CAP = 2000;
  const removedTextOf = entry => [entry.oldText, entry.mixedOldText, entry.text]
    .filter(Boolean).join("\n").slice(0, ORIGIN_TEXT_CAP);
  const removedTextByEntryKey = new Map();
  for (const entry of entries) {
    if (entry.operation !== "-" && entry.operation !== "~") continue;
    const previous = removedTextByEntryKey.get(entry.entryKey) || "";
    if (previous.length >= ORIGIN_TEXT_CAP) continue;  // already enough for a needle
    const removed = removedTextOf(entry);
    if (!removed) continue;
    const combined = previous ? `${previous}\n${removed}` : removed;
    removedTextByEntryKey.set(entry.entryKey, combined.slice(0, ORIGIN_TEXT_CAP));
  }
  const textOriginCache = new Map();
  const originCallForText = removed => {
    if (!removed) return null;
    if (textOriginCache.has(removed)) return textOriginCache.get(removed);
    const call = removedTextOriginCall(removed);
    textOriginCache.set(removed, call);
    return call;
  };
  const entryOriginCall = entry => originCallForText(
    removedTextByEntryKey.get(entry.entryKey) || removedTextOf(entry),
  );
  const addedCallAttribute = removed => {
    const originCall = originCallForText(removed);
    return originCall != null ? ` data-added-call="${originCall}"` : "";
  };
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
      changes.push({
        start: position,
        end: position,
        entry,
        replacementOrder: entry.replacementOrder,
      });
      continue;
    }
    const ranges = entryRanges(text, entry, anchors);
    ranges.forEach((range, rangeIndex) => {
      if (
        entry.operation === "~"
        && rangeIndex === 0
        && entry.mixedOldText
      ) {
        changes.push({
          start: range[0],
          end: range[0],
          entry,
          removedOnly: true,
          replacementOrder: entry.replacementOrder,
        });
      }
      changes.push({
        start: range[0],
        end: range[1],
        entry,
        firstRange: rangeIndex === 0,
        skipInlineRemoval: entry.operation === "~" && rangeIndex === 0,
        replacementOrder: entry.replacementOrder,
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
    const collision = claimed.find(
      span => change.start < span[1] && span[0] < change.end,
    );
    if (collision) {
      // Retained history that was replaced by the selected call belongs before
      // the selected call's present (green) span. Anchoring it at the end of the
      // document reverses the transition visually: green first, then red.
      // Make it a zero-width insertion at the replacing span's start so the
      // renderer emits the removed history first without competing for text.
      placed.push({
        ...change,
        historical: true,
        start: collision[0],
        end: collision[0],
      });
      continue;
    }
    claimed.push([change.start, change.end]);
    placed.push(change);
  }
  placed.sort((left, right) => (
    left.start - right.start
    || left.end - right.end
    || (left.replacementOrder ?? Number.MAX_SAFE_INTEGER)
      - (right.replacementOrder ?? Number.MAX_SAFE_INTEGER)
  ));
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
    // A removed part carries both references on the one struck element: the
    // strike-through focuses its removal, and a green underline (the addition
    // origin) focuses where it was added. The origin is either a specific earlier
    // change (data-added-entry) or, for text already present before the loaded
    // history, the earliest call that still shows it (data-added-call).
    const additionOrigin = additionOrigins.get(entry);
    let addedAttribute = "";
    if (additionOrigin) {
      addedAttribute = ` data-added-entry="${additionOrigin.entryKey}"`;
    } else {
      const originCall = entryOriginCall(entry);
      if (originCall != null) addedAttribute = ` data-added-call="${originCall}"`;
    }
    const removedEntryAttribute = `${entryAttribute}${addedAttribute}`;
    if (change.historical) {
      const oldText = entry.oldText || entry.mixedOldText || "";
      const newText = entry.newText || entry.text || "";
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
        html += changePartHtml("removed", oldText, category, false, removedEntryAttribute);
        if (showNew) html += '<span class="change-arrow inline-arrow"> → </span>';
      }
      if (showNew) {
        // An earlier call's addition that is now gone is shown as removed
        // history; it still links back to the call that first added it.
        const newAttribute = newKind === "removed"
          ? `${entryAttribute}${addedCallAttribute(newText)}`
          : entryAttribute;
        html += changePartHtml(newKind, newText, category, false, newAttribute);
      }
      html += "\n";
    } else if (change.removedOnly) {
      html += changePartHtml(
        "removed",
        entry.mixedOldText || entry.oldText || entry.text,
        category,
        true,
        removedEntryAttribute,
      );
    } else if (entry.operation === "-") {
      const removed = entry.mixedOldText || entry.oldText || entry.text;
      html += `\n${changePartHtml(
        "removed",
        removed,
        category,
        false,
        removedEntryAttribute,
      )}\n`;
    } else {
      const current = text.slice(change.start, change.end);
      if (
        entry.operation === "~"
        && entry.mixedOldText
        && change.firstRange
        && !change.skipInlineRemoval
      ) {
        html += changePartHtml(
          "removed",
          entry.mixedOldText,
          category,
          true,
          removedEntryAttribute,
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
  // In-flight streaming calls are not stored yet; serve a synthetic detail built
  // from the live record (a stable, mutated-in-place object) instead of fetching.
  if (isLiveId(id)) {
    const record = state.live.get(Number(String(id).slice(5)));
    if (record) return liveDetailFor(record);
    throw Object.assign(new Error("live call ended"), { status: 404 });
  }
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
  // The empty-session message is real pane state, not decoration. A live call
  // can arrive before the first durable timeline poll, so remove the placeholder
  // as part of the same reconciliation that creates its input card.
  updates.querySelectorAll(".empty-session").forEach(node => node.remove());
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
  if (!desired.length) {
    const empty = document.createElement("div");
    empty.className = "empty-session";
    empty.textContent = "No updates in this session.";
    updates.replaceChildren(empty);
    return;
  }
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
    // A stored call gets an output event once finished; a still-running call has
    // none. A streaming (synthetic, in-flight) call gets one immediately, flagged
    // not-finished, so its live output is selectable while it streams.
    const streaming = item.status === "streaming";
    if (item.status !== "running") {
      const duration = Number(item.duration_ms);
      const completedAt = dated && Number.isFinite(duration)
        ? startedAt + Math.max(duration, 0)
        : startedAt + 1;
      events.push({
        item, phase: "output", at: completedAt, sortOrder: 0, streaming,
      });
    }
  }
  return events;
}

function timelineDisplayId(item) {
  // The visible #number is the proxy's durable call id. req_id/request_id may be
  // caller-owned opaque strings (often long hashes); keep those for lineage and
  // live→stored reconciliation, never as the human-facing timeline number.
  return item.live ? item.call_id : item.id;
}

function formatSpeed(value) {
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} tok/s`;
}

function timelineUsageText(item, phase) {
  const usage = item.usage;
  if (!usage || typeof usage !== "object") return "";
  const input = Number.isInteger(usage.input_tokens) ? usage.input_tokens : null;
  const output = Number.isInteger(usage.output_tokens) ? usage.output_tokens : null;
  const total = Number.isInteger(usage.total_tokens) ? usage.total_tokens : null;
  const speed = typeof usage.output_per_second === "number" && usage.output_per_second > 0
    ? usage.output_per_second
    : null;
  if (phase === "input") {
    return input == null ? "" : `${input.toLocaleString()} in`;
  }
  const parts = [];
  if (output != null) parts.push(`${output.toLocaleString()} out`);
  if (total != null) parts.push(`${total.toLocaleString()} total`);
  if (speed != null) parts.push(formatSpeed(speed));
  return parts.join(" · ");
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

  const timeline = $("timeline");
  const existingByKey = new Map();
  const existingByIdentity = new Map();
  for (const node of timeline.querySelectorAll(".timeline-item")) {
    const phase = node.dataset.phase;
    const key = node.dataset.key || node.dataset.callKey;
    if (phase && key) existingByKey.set(`${phase}:${key}`, node);
    if (node.dataset.timelineIdentity) {
      const matches = existingByIdentity.get(node.dataset.timelineIdentity) || [];
      matches.push(node);
      existingByIdentity.set(node.dataset.timelineIdentity, matches);
    }
  }
  const reusedNodes = new Set();
  const reusableNode = (phase, key, identity) => {
    const exact = existingByKey.get(`${phase}:${key}`);
    if (exact && !reusedNodes.has(exact)) {
      reusedNodes.add(exact);
      return exact;
    }
    const identityMatch = (existingByIdentity.get(identity) || [])
      .find(node => !reusedNodes.has(node));
    if (identityMatch) reusedNodes.add(identityMatch);
    return identityMatch || document.createElement("button");
  };
  const nextChildren = document.createDocumentFragment();
  for (const event of events) {
    if (event.phase === "parallel-start" || event.phase === "parallel-end") {
      const divider = document.createElement("div");
      const edge = event.phase === "parallel-start" ? "start" : "end";
      divider.className = `timeline-parallel-divider parallel-${edge}`;
      divider.dataset.parallelBlock = String(event.blockIndex);
      divider.innerHTML = `
        <span>parallel ${edge}</span>
        <small>${event.branchCount} branches</small>`;
      nextChildren.appendChild(divider);
      continue;
    }
    const { item, phase } = event;
    const key = itemKey(item);
    const stableId = item.req_id || item.request_id || item.call_id || item.id;
    const identity = `${phase}:${item.session_id || ""}:${stableId}`;
    // Reuse the phase node across live updates and the live→stored handoff. The
    // public request id is stable even when the internal id changes from live-N
    // to the durable numeric call id.
    const button = reusableNode(phase, key, identity);
    button.className = `timeline-item timeline-${phase} trace-kind-${phase} call${
      event.streaming ? " timeline-streaming" : ""
    }`;
    button.dataset.phase = phase;
    button.dataset.timelineIdentity = identity;
    if (phase === "input") {
      button.dataset.key = key;
      delete button.dataset.callKey;
      button.classList.toggle("checkpoint-call", state.checkpointKeys.has(key));
    } else {
      button.dataset.callKey = key;
      delete button.dataset.key;
    }
    const checkpoint = phase === "input" && state.checkpointKeys.has(key);
    const phaseHtml = phase === "input"
      ? checkpoint ? "→ new state input" : "→ input"
      : event.streaming ? '<span class="live-dot"></span>← output' : "← output";
    const explicitTitle = item.title || null;
    const displayedTitle = explicitTitle || item.debug_label || item.label || "LLM call";
    const usageText = timelineUsageText(item, phase);
    const statusText = phase === "input" ? "sent" : item.status;
    button.innerHTML = `
      <span class="item-head">
        <span class="item-title" title="${escapeHtml(displayedTitle)}">${escapeHtml(displayedTitle)}</span>
      </span>
      <span class="item-meta">
        <span>#${escapeHtml(String(timelineDisplayId(item)))} · <b class="branch">${escapeHtml(item.branch_id || "main")}</b></span>
        <span class="item-tail" title="${escapeHtml(usageText || statusText)}">
          <span class="item-label">${phaseHtml}</span>${
            escapeHtml([usageText, statusText].filter(Boolean).join(" · "))
        }</span>
      </span>`;
    button.classList.toggle(
      "active",
      focusedTimelineKey() === key
        && (state.timelineFocus?.phase || state.selectedPhase) === phase,
    );
    button.onclick = () => selectItem(
      item.type, item.id, button, false, true, "timeline",
    );
    nextChildren.appendChild(button);
  }
  timeline.replaceChildren(nextChildren);
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

  // A window (or any declared group) is a grouping node, not a request. Insert
  // a synthetic node per group between its members and their shared parent, so
  // the lines hang off the window and the window hangs off the root. The
  // synthetic node's id is a string, distinct from the numeric call ids.
  const groupNodeId = new Map();
  const groupParent = new Map();
  const orderedEntries = [];
  for (const item of calls) {
    const group = item.group;
    if (group && !groupNodeId.has(group)) {
      const syntheticId = `group:${group}`;
      groupNodeId.set(group, syntheticId);
      groupParent.set(group, parentOf.get(item.id) ?? null);
      orderedEntries.push({ synthetic: true, id: syntheticId, group });
    }
    orderedEntries.push({ synthetic: false, item, id: item.id });
  }
  const resolvedParent = entry => {
    if (entry.synthetic) return groupParent.get(entry.group);
    if (entry.item.group) return groupNodeId.get(entry.item.group);
    return parentOf.get(entry.id) ?? null;
  };
  // Child counts over the reparented tree drive lane reuse.
  const laneChildCount = new Map();
  for (const entry of orderedEntries) {
    const parent = resolvedParent(entry);
    if (parent != null) laneChildCount.set(parent, (laneChildCount.get(parent) || 0) + 1);
  }

  // Lane assignment with reuse: a continuation keeps its parent's lane, a branch
  // takes the lowest free lane, and a lane frees when its tip has no children
  // left to place. This keeps a mostly-linear trace narrow.
  const laneTip = [];
  const laneOf = new Map();
  const extended = new Set();
  const remaining = new Map(laneChildCount);
  const nodes = [];
  orderedEntries.forEach((entry, depth) => {
    const parent = resolvedParent(entry);
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
    laneOf.set(entry.id, lane);
    laneTip[lane] = entry.id;
    nodes.push({
      item: entry.item ?? null,
      id: entry.id,
      synthetic: !!entry.synthetic,
      group: entry.group ?? null,
      depth,
      lane,
      parentId: parent,
    });
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
  const displayId = escapeHtml(String(timelineDisplayId(item)));
  return `<span class="branch-node-step">${step || `#${displayId}`}</span>`
    + `<span class="branch-node-meta">${step ? `#${displayId} · ` : ""}${branch}</span>`;
}

// A node is either a real call or a synthetic grouping node (a window). The
// grouping node is not a request: it has no call id, is not selectable, and
// reads as its group name.
function branchNodeInfo(node) {
  if (node.synthetic) {
    const label = escapeHtml(node.group || "group");
    return {
      selectable: false,
      dotClass: "synthetic",
      title: `window ${node.group || ""}`.trim(),
      labelClass: "synthetic",
      labelHtml: `<span class="branch-node-step">${label}</span>`
        + `<span class="branch-node-meta">window</span>`,
    };
  }
  const item = node.item;
  const running = item.status === "running";
  const checkpoint = state.checkpointKeys.has(`call:${node.id}`);
  const displayId = escapeHtml(String(timelineDisplayId(item)));
  return {
    selectable: true,
    dotClass: `${running ? " running" : ""}${checkpoint ? " checkpoint" : ""}`,
    title: `LLM call #${displayId}`
      + `${item.debug_label ? ` · ${escapeHtml(item.debug_label)}` : ""}`
      + ` · ${escapeHtml(item.branch_id || "main")}`,
    labelClass: "",
    labelHtml: branchNodeLabel(item),
  };
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
      const info = branchNodeInfo(node);
      const active = info.selectable && focusedTimelineKey() === `call:${node.id}`;
      const callAttr = info.selectable ? ` data-call-id="${node.id}"` : "";
      return `<circle class="branch-dot${info.dotClass}${active ? " active" : ""}"
        cx="${cx}" cy="${cy}" r="${BRANCH_NODE_R}" fill="${branchLaneColor(node.lane)}"${callAttr}/>`;
    }).join("")}
  </svg>`;
  const labels = nodes.map(node => {
    const cx = xOf(node);
    const cy = yOf(node);
    const info = branchNodeInfo(node);
    const key = `call:${node.id}`;
    const active = info.selectable && focusedTimelineKey() === key;
    const attrs = info.selectable
      ? ` data-key="${key}" data-call-id="${node.id}"`
      : "";
    return `<button class="branch-node horizontal${active ? " active" : ""} ${info.labelClass}"
     ${attrs} style="left:${cx}px;top:${cy + BRANCH_NODE_R + 3}px;"
      title="${info.title}">
      ${info.labelHtml}
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
    const info = branchNodeInfo(node);
    const active = info.selectable && focusedTimelineKey() === `call:${node.id}`;
    const callAttr = info.selectable ? ` data-call-id="${node.id}"` : "";
    return `<circle class="branch-dot${info.dotClass}${active ? " active" : ""}"
      cx="${point.x}" cy="${point.y}" r="${BRANCH_NODE_R}" fill="${branchLaneColor(node.lane)}"${callAttr}/>`;
  }).join("");
  const labels = nodes.map(node => {
    const point = positions.get(node.id);
    const roomRight = width - point.x;
    const placeLeft = roomRight < 150;
    const available = Math.max(76, Math.min(190, placeLeft ? point.x - 18 : roomRight - 18));
    const info = branchNodeInfo(node);
    const active = info.selectable && focusedTimelineKey() === `call:${node.id}`;
    const attrs = info.selectable
      ? ` data-key="call:${node.id}" data-call-id="${node.id}"`
      : "";
    return `<button class="branch-node${active ? " active" : ""}${placeLeft ? " place-left" : ""} ${info.labelClass}"
     ${attrs}
      style="left:${point.x + (placeLeft ? -8 : 8)}px;top:${point.y}px;width:${available}px;"
      title="${info.title}">
      ${info.labelHtml}
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
    // A synthetic grouping node (a window) is not a call; it has no id, so it is
    // inert rather than selecting an undefined call.
    if (button.dataset.callId == null) return;
    button.onclick = () => selectItem(
      "call", Number(button.dataset.callId), button, false, true, "timeline",
    );
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
function setTimelineFocus(
  key,
  phase = "input",
  scroll = false,
  element = null,
  block = "start",
  flash = scroll,
) {
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
  const graphTarget = state.timelineView === "branches"
    ? $("branch-graph")?.querySelector(`.branch-node[data-key="${key}"]`)
    : null;
  const listTarget = phase === "output"
    ? document.querySelector(`.timeline-output[data-call-key="${key}"]`)
    : document.querySelector(`.timeline-input[data-key="${key}"]`);
  const target = graphTarget || element || listTarget;
  if (flash && target) {
    // Active is a persistent state and is easy to mistake for a failed repeat
    // focus. Restart a short pulse even when this same item was already active.
    target.classList.remove("timeline-focus-flash");
    void target.offsetWidth;
    target.classList.add("timeline-focus-flash");
  }
  updateCleanHistoryControl();
  if (!scroll) return;
  target?.focus({ preventScroll: true });
  focusScrollIntoView(target, block);
}

function cleanHistoryTarget() {
  const focusedKey = state.timelineFocus?.key;
  const focused = focusedKey?.startsWith("call:")
    ? state.timelineItems.find(item => itemKey(item) === focusedKey)
    : null;
  const item = focused || (
    state.selected?.type === "call"
      ? state.timelineItems.find(item => itemKey(item) === itemKey(state.selected))
        || state.selected
      : null
  );
  if (!item || item.type !== "call") return null;
  const durableId = item.live ? item.call_id : item.id;
  if (durableId == null || !Number.isFinite(Number(durableId))) return null;
  return {
    id: Number(durableId),
    key: itemKey(item),
    displayId: timelineDisplayId(item),
  };
}

function updateCleanHistoryControl() {
  const button = $("reset-history");
  if (!button) return;
  const target = cleanHistoryTarget();
  button.disabled = !target || button.dataset.busy === "true";
  button.title = target
    ? `Permanently delete focused call #${target.displayId} and every older call`
    : "Select a call before cleaning history";
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

function renderWaitingCalls() {
  const box = $("waiting-calls");
  // Storage is deferred, so there is never a stored "running" call. The live
  // streams ARE the running calls, and each already appears as its own synthetic
  // Timeline item (input + streaming output); this is just the running count.
  const running = [...state.live.values()].filter(
    record => (record.status || "streaming") === "streaming",
  ).length;
  box.innerHTML = `
    <div class="waiting-calls-head">
      <span>${running} waiting / running</span>
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
const SOURCE_PANE_SCROLL_GUARD_MS = 5000;
const focusScrollAnimations = new WeakMap();
const panePointerScrolls = new WeakMap();
const paneScrollPins = new WeakMap();
let paneScrollPreservationGeneration = 0;

// Removing search marks briefly preserves every pane's viewport to defeat DOM
// scroll anchoring. Any later explicit focus owns the viewport, so invalidate
// those queued restores before starting its scroll. Otherwise a fast click can
// be reset on the next frame and only the second click appears to work.
function supersedePaneScrollPreservation() {
  paneScrollPreservationGeneration += 1;
}

// Clicking a mark inside a pane focuses it across every pane, but the pane the
// user clicked in must not move — they are already looking at it. Focus and
// CodeMirror's async re-measure both try to reveal the target, so hold the
// clicked pane's scroll for a short window rather than trusting a single
// restore. The other panes still scroll to their corresponding item.
function paneScrollPosition(pane) {
  const scroller = pane.querySelector(".cm-scroller") || pane;
  return { scroller, top: scroller.scrollTop, left: scroller.scrollLeft };
}

function pinPaneScroll(pane, position = null) {
  // Replace an older guard instead of stacking scroll listeners and timers when
  // several marks are clicked quickly in the same large virtual document.
  paneScrollPins.get(pane)?.();
  const current = paneScrollPosition(pane);
  const { scroller } = current;
  // A CodeMirror mousedown may already have scrolled by the time the click
  // handler runs. Prefer the capture-phase position recorded before CodeMirror
  // saw that mousedown; keyboard and synthetic clicks use the current position.
  const top = position?.scroller === scroller ? position.top : current.top;
  const left = position?.scroller === scroller ? position.left : current.left;
  let restoring = false;
  let pinned = true;
  const directScrollEvents = ["wheel", "touchstart", "pointerdown", "keydown"];
  const release = () => {
    if (!pinned) return;
    pinned = false;
    scroller.removeEventListener("scroll", restore);
    for (const eventName of directScrollEvents) {
      scroller.removeEventListener(eventName, release);
    }
    for (const eventName of ["pointerdown", "click", "keydown"]) {
      document.removeEventListener(eventName, release, true);
    }
    if (paneScrollPins.get(pane) === release) paneScrollPins.delete(pane);
  };
  paneScrollPins.set(pane, release);
  const restore = () => {
    if (!pinned || restoring) return;
    restoring = true;
    if (scroller.scrollTop !== top) scroller.scrollTop = top;
    if (scroller.scrollLeft !== left) scroller.scrollLeft = left;
    restoring = false;
  };
  // CodeMirror may defer revealing its new selection until a later animation
  // frame. Timed restores eventually put the pane back, but the intermediate
  // position can still be painted as a visible jump. A scroll listener reverses
  // that deferred movement in the same scroll-delivery cycle, before paint.
  scroller.addEventListener("scroll", restore);
  // Large virtual documents can finish measuring well after the click. Keep
  // guarding against those delayed programmatic reveals, but release before a
  // new intentional user gesture so this never makes Mixed feel scroll-locked.
  for (const eventName of directScrollEvents) {
    scroller.addEventListener(eventName, release, { once: true, passive: true });
  }
  // The current gesture reached this function from the pane's bubbling click,
  // after document capture has already run. These capture listeners therefore
  // release only for the *next* action, including a click in another pane.
  for (const eventName of ["pointerdown", "click", "keydown"]) {
    document.addEventListener(eventName, release, { capture: true, once: true });
  }
  restore();
  requestAnimationFrame(() => {
    restore();
    requestAnimationFrame(restore);
  });
  for (const delay of [0, 30, 80, 160, 260, 500, 900, 1400, 2200, 3200, 4400]) {
    setTimeout(restore, delay);
  }
  setTimeout(release, SOURCE_PANE_SCROLL_GUARD_MS);
}

function focusScrollIntoView(element, block = "center") {
  if (!element) return;
  supersedePaneScrollPreservation();
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
  } else if (block === "end") {
    target = elementBottom - container.clientHeight;
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
  // A long animated jump can briefly park a section header at the bottom edge
  // while all of its content remains below the viewport. That looks exactly
  // like an empty Output pane. Land long cross-document jumps immediately;
  // animation is useful only when the destination is already nearby.
  if (
    Math.abs(target - start) > container.clientHeight
    || window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    container.scrollTop = target;
    focusScrollAnimations.delete(container);
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
  const storedPart = event?.currentTarget?.dataset?.mixedPartKind;
  if (storedPart) {
    delete event.currentTarget.dataset.mixedPartKind;
    return storedPart;
  }
  if (event?.mixedPartKind) return event.mixedPartKind;
  const part = event?.composedPath?.().find(node => (
    node?.tagName === "DEL" || node?.tagName === "INS"
  )) || event?.target?.closest?.("del, ins");
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
  focusScrollIntoView(targets[0], "start");
}

function applyMixedCodeMirrorRanges(
  ranges,
  preferredRange = null,
  scrollIntoRange = true,
) {
  if (!mixedCodeMirrorModel || !ranges.length) return false;
  const range = preferredRange || ranges[0];
  const focusedRanges = new Set(ranges);
  const focusChanged = mixedCodeMirrorModel.marks.some(
    mark => Boolean(mark.focused) !== focusedRanges.has(mark),
  );
  mixedCodeMirrorModel.marks.forEach(mark => {
    mark.focused = focusedRanges.has(mark);
  });
  if (!mixedCodeMirrorView) return true;
  const scrollTop = mixedCodeMirrorView.scrollDOM.scrollTop;
  if (focusChanged) {
    // Swap decorations in place (see reconfigureMixedDecorations); a full
    // setState here would reset height measurements and jerk the scroll.
    if (!reconfigureMixedDecorations()) {
      const selection = mixedCodeMirrorView.state.selection;
      mixedCodeMirrorView.setState(
        mixedCodeMirrorState(mixedCodeMirrorApi, mixedCodeMirrorModel, selection),
      );
    }
    mixedCodeMirrorView.scrollDOM.scrollTop = scrollTop;
  } else {
    // Restart the visible pulse without rebuilding the enormous virtual
    // document when the user clicks the same timeline phase again.
    mixedCodeMirrorView.dom.querySelectorAll(".fragment-focus.flash").forEach(node => {
      node.classList.remove("flash");
      void node.offsetWidth;
      node.classList.add("flash");
    });
  }
  if (scrollIntoRange) {
    supersedePaneScrollPreservation();
    mixedCodeMirrorView.dispatch({
      // The decoration already highlights the complete change. Selecting a
      // potentially huge range makes CodeMirror reveal its far end, which may
      // be nowhere near the mark the timeline item is meant to lead to.
      selection: { anchor: range.start },
      effects: mixedCodeMirrorApi.EditorView.scrollIntoView(
        range.start,
        { y: "start" },
      ),
    });
    mixedCodeMirrorView.focus();
  } else {
    // CodeMirror measures its virtual viewport after setState. Reapply the
    // captured position after that measurement so a click on a visible mark
    // cannot be nudged by the refreshed decorations.
    const focusedView = mixedCodeMirrorView;
    const restoreScroll = () => {
      if (mixedCodeMirrorView === focusedView) {
        focusedView.scrollDOM.scrollTop = scrollTop;
      }
    };
    window.requestAnimationFrame(() => {
      restoreScroll();
      // A click inside CodeMirror may queue its own selection measurement
      // during the same frame. Restore once more after that measurement.
      window.requestAnimationFrame(restoreScroll);
    });
    window.setTimeout(restoreScroll, 50);
  }
  return true;
}

function focusMixedCodeMirror(
  entryKey,
  fragmentIndices = null,
  part = null,
  scrollIntoRange = true,
) {
  if (!mixedCodeMirrorView || !mixedCodeMirrorModel) return false;
  const ranges = mixedCodeMirrorModel.marks.filter(mark => (
    (
      mark.attributes["data-update-entry"] === entryKey
      || mark.attributes["data-added-entry"] === entryKey
    )
    && (
      !fragmentIndices
      || fragmentIndices.includes(Number(mark.attributes["data-output-fragment"]))
    )
    && (
      !part
      || mark.classes.includes(
        part === "removed" ? "cm-mixed-removed" : "cm-mixed-added",
      )
    )
  ));
  return applyMixedCodeMirrorRanges(ranges, null, scrollIntoRange);
}

function focusMixedCodeMirrorEntries(entryKeys, preferredEntryKey = null) {
  if (!mixedCodeMirrorModel) return false;
  const keys = new Set(entryKeys);
  const ranges = mixedCodeMirrorModel.marks.filter(
    mark => keys.has(mark.attributes["data-update-entry"]),
  );
  const preferred = preferredEntryKey
    ? ranges.find(mark => mark.attributes["data-update-entry"] === preferredEntryKey)
    : null;
  return applyMixedCodeMirrorRanges(ranges, preferred);
}

function focusMixedCodeMirrorScope(scope) {
  if (!mixedCodeMirrorModel) return false;
  return applyMixedCodeMirrorRanges(mixedCodeMirrorModel.marks.filter(
    mark => mark.attributes["data-state-scope"] === scope,
  ));
}

function focusMixedCodeMirrorCheckpointOrigin(
  callId,
  scope,
  scrollIntoRange = true,
) {
  if (!mixedCodeMirrorModel) return false;
  return applyMixedCodeMirrorRanges(mixedCodeMirrorModel.marks.filter(mark => (
    mark.attributes["data-checkpoint-call"] === String(callId)
    && mark.attributes["data-checkpoint-scope"] === scope
  )), null, scrollIntoRange);
}

function clearMixedCodeMirrorFocus(render = true) {
  if (!mixedCodeMirrorModel) return;
  if (!mixedCodeMirrorModel.marks.some(mark => mark.focused)) return;
  mixedCodeMirrorModel.marks.forEach(mark => {
    mark.focused = false;
  });
  if (!render || !mixedCodeMirrorView) return;
  const scrollTop = mixedCodeMirrorView.scrollDOM.scrollTop;
  // Clear the focus decorations in place; a full setState would reset height
  // measurements and jerk the scroll (see reconfigureMixedDecorations).
  if (!reconfigureMixedDecorations()) {
    const selection = mixedCodeMirrorView.state.selection;
    mixedCodeMirrorView.setState(
      mixedCodeMirrorState(mixedCodeMirrorApi, mixedCodeMirrorModel, selection),
    );
  }
  mixedCodeMirrorView.scrollDOM.scrollTop = scrollTop;
}

function focusMixedFragments(indices, entry, part = null) {
  focusMixedCodeMirror(entry.entryKey, indices, part);
  const targets = indices.flatMap(index => (
    [...$("mixed").querySelectorAll(
      `[data-update-entry="${entry.entryKey}"][data-output-fragment="${index}"]`,
    )]
  )).filter(node => (
    node.classList.contains("mixed-history-value") || partMatches(node, part)
  ));
  applyMixedFocus(targets);
}

function focusMixedEntry(entry, part = null) {
  if (entry.fragments) {
    focusMixedFragments(entry.fragments.map((_, index) => index), entry, part);
    return;
  }
  focusMixedCodeMirror(entry.entryKey, null, part);
  const targets = [
    ...$("mixed").querySelectorAll(`[data-update-entry="${entry.entryKey}"]`),
  ].filter(node => (
    node.classList.contains("mixed-history-value") || partMatches(node, part)
  ));
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
  focusMixedCodeMirrorScope(scope);
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

// Follow tracks the final *visible timeline event*, not merely the final call.
// A call can contribute both an input and a later output, and overlapping calls
// can make an older call's output the newest event. Passing the actual event
// element also preserves its phase; without it selectItem defaults to input.
function latestTimelineEvent(items = state.timelineItems) {
  const events = timelinePhaseEvents(items).sort(compareTimelineEvents);
  return events.at(-1) || null;
}

function followLatestTimelineEvent(focusSelection = true) {
  const latest = latestTimelineEvent();
  if (!latest) return null;
  const element = timelineEventFor(latest.item.id, latest.phase);
  return selectItem(
    latest.item.type,
    latest.item.id,
    element,
    true,
    focusSelection,
    "follow",
  );
}

async function selectPhaseFromCard(card) {
  const id = parseItemId(card.dataset.id);
  await selectItem(
    "call", id, timelineEventFor(id, card.dataset.phase), true, true, "updates",
  );
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
    detail = await detailFor(card.dataset.type, parseItemId(card.dataset.id));
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
  // Timeline and Updates must agree immediately. A live call has no parent diff
  // yet and Updates renders it as a new current state, so its input phase carries
  // the same checkpoint label before output/storage finishes.
  const timelineCheckpoint = checkpoint;
  const timelineItem = document.querySelector(
    `.timeline-item[data-key="call:${detail.id}"]`,
  );
  const timelineKey = `call:${detail.id}`;
  if (timelineCheckpoint) {
    state.checkpointKeys.add(timelineKey);
  } else {
    state.checkpointKeys.delete(timelineKey);
  }
  timelineItem?.classList.toggle("checkpoint-call", timelineCheckpoint);
  const timelinePhase = timelineItem?.querySelector(".item-phase")
    || timelineItem?.querySelector(".item-label");
  if (timelinePhase) {
    timelinePhase.textContent = timelineCheckpoint ? "→ new state input" : "→ input";
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
  card.querySelectorAll("del, ins").forEach(part => {
    part.addEventListener("click", event => {
      const kind = part.tagName === "DEL" ? "removed" : "added";
      event.mixedPartKind = kind;
      const owner = part.closest(".fragment-change, .update-jump");
      if (owner) owner.dataset.mixedPartKind = kind;
    }, { capture: true });
  });
  card.querySelectorAll(".update-jump").forEach(button => {
    const openUpdate = async event => {
      if (hasTextSelectionWithin(button)) return;
      const entry = entries[Number(button.dataset.updateIndex)];
      const part = clickedPartKind(event);
      await selectItem(
        "call", detail.id, timelineEventFor(detail.id, phase), true, false,
      );
      activateTab("state");
      // The clicked jump is the focused entry now: light it (and only it) in the
      // Updates pane. No scroll — this pane is the one the user clicked in.
      clearUpdateEntryFocus();
      card.classList.add("active");
      button.classList.add("timeline-update-focus", "timeline-update-flash");
      // Removed text is absent from Exact State by definition, so a click on
      // the removed half must not flash the present half there instead.
      renderExact(part === "removed" ? null : entry);
      focusMixedEntry(entry, part);
      window.setTimeout(() => {
        if (Number(state.detail?.id) === Number(detail.id)) {
          focusMixedEntry(entry, part);
        }
      }, 0);
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
      window.setTimeout(() => {
        if (Number(state.detail?.id) === Number(detail.id)) {
          focusMixedFragments(indices, entry, part);
        }
      }, 0);
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

// Mixed is one complete reconstructed document. Load every call back to the
// checkpoint automatically; batches cap request concurrency without exposing a
// pagination control or omitting any history.
async function mixedDetailsThroughCheckpoint(startIndex, selectedDetail) {
  const candidates = [];
  let cursor = startIndex;
  while (cursor >= 0) {
    const item = state.timelineItems[cursor];
    if (item.type === "call") {
      candidates.push(item);
      if (state.checkpointKeys.has(itemKey(item))) break;
    }
    cursor -= 1;
  }
  const details = [];
  const batchSize = 48;
  for (let offset = 0; offset < candidates.length; offset += batchSize) {
    const batch = candidates.slice(offset, offset + batchSize);
    const loaded = await Promise.all(batch.map(item => (
      Number(item.id) === Number(selectedDetail?.id)
        ? selectedDetail
        : detailFor(item.type, item.id)
    )));
    for (const detail of loaded) {
      details.unshift(detail);
      if (isCheckpoint(detail)) return details;
    }
  }
  return details;
}

async function loadMixedSegment(type, id, selectionVersion = null) {
  const selectionIsCurrent = () => (
    selectionVersion == null || selectionVersion === state.selectionVersion
  );
  const selectedDetail = state.detail;
  state.mixedHistoryLoading = false;
  // A live (in-flight) call has no stored history to diff against; render it as a
  // single-item segment so Mixed shows its input as the current state.
  if (type !== "call" || isLiveId(id)) {
    if (selectionIsCurrent()) {
      state.mixedSegmentDetails = selectedDetail ? [selectedDetail] : [];
      state.mixedHistoryComplete = true;
    }
    return selectionIsCurrent();
  }
  const selectedIndex = state.timelineItems.findIndex(
    item => item.type === type && Number(item.id) === Number(id),
  );
  if (selectedIndex < 0) {
    if (selectionIsCurrent()) {
      state.mixedSegmentDetails = selectedDetail ? [selectedDetail] : [];
      state.mixedHistoryComplete = true;
    }
    return selectionIsCurrent();
  }
  state.mixedHistoryLoading = true;
  try {
    const details = await mixedDetailsThroughCheckpoint(selectedIndex, selectedDetail);
    if (!selectionIsCurrent()) return false;
    state.mixedSegmentDetails = details;
    state.mixedHistoryComplete = true;
    return true;
  } finally {
    if (selectionIsCurrent()) state.mixedHistoryLoading = false;
  }
}

let mixedCodeMirrorRuntime = null;
let mixedCodeMirrorApi = null;
const mixedCodeMirrorViews = new Set();
let mixedCodeMirrorView = null;
let mixedCodeMirrorGeneration = 0;
let mixedCodeMirrorModel = null;

function loadMixedCodeMirror() {
  if (!mixedCodeMirrorRuntime) {
    mixedCodeMirrorRuntime = Promise.all([
      import("https://esm.sh/@codemirror/state@6"),
      import("https://esm.sh/@codemirror/view@6"),
    ]).then(([stateModule, viewModule]) => {
      mixedCodeMirrorApi = { ...stateModule, ...viewModule };
      return mixedCodeMirrorApi;
    });
  }
  return mixedCodeMirrorRuntime;
}

function mixedCodeMirrorDocument(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  let text = "";
  const marks = [];
  const visit = (node, inheritedScope = null) => {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.nodeValue;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      node.childNodes.forEach(child => visit(child, inheritedScope));
      return;
    }
    const scope = node.dataset.stateScope || inheritedScope;
    const start = text.length;
    node.childNodes.forEach(child => visit(child, scope));
    const end = text.length;
    if (start === end) return;
    const classes = [];
    if (node.tagName === "INS") {
      classes.push("cm-mixed-added", ...node.classList);
    }
    if (node.tagName === "DEL") {
      classes.push("cm-mixed-removed", ...node.classList);
    }
    if (node.classList.contains("change-arrow")) classes.push("cm-mixed-arrow");
    if (node.dataset.stateScope) {
      classes.push("cm-mixed-scope", `cm-mixed-scope-${node.dataset.stateScope}`);
    }
    if (node.dataset.checkpointCall) {
      classes.push("cm-mixed-checkpoint-origin");
    }
    const attributes = {};
    if (node.dataset.updateEntry) {
      attributes["data-update-entry"] = node.dataset.updateEntry;
      classes.push("cm-mixed-update");
    }
    if (node.dataset.addedEntry) {
      attributes["data-added-entry"] = node.dataset.addedEntry;
      classes.push("cm-mixed-added-origin");
    }
    if (node.dataset.addedCall) {
      attributes["data-added-call"] = node.dataset.addedCall;
      classes.push("cm-mixed-added-origin");
    }
    if (node.dataset.outputFragment != null) {
      attributes["data-output-fragment"] = node.dataset.outputFragment;
    }
    if (node.dataset.stateScope) {
      attributes["data-state-scope"] = node.dataset.stateScope;
    }
    if (node.dataset.checkpointCall) {
      attributes["data-checkpoint-call"] = node.dataset.checkpointCall;
    }
    if (node.dataset.checkpointScope) {
      attributes["data-checkpoint-scope"] = node.dataset.checkpointScope;
    }
    if (classes.length) {
      marks.push({ start, end, classes, attributes, scope });
    }
  };
  template.content.childNodes.forEach(visit);
  return { text, marks };
}

async function mountMixedCodeMirror(
  parent,
  model,
  generation,
  initialScroll = 0,
) {
  const runtime = await loadMixedCodeMirror();
  if (!parent.isConnected || generation !== mixedCodeMirrorGeneration) return null;
  const editorState = mixedCodeMirrorState(runtime, model);
  parent.textContent = "";
  const view = new runtime.EditorView({ state: editorState, parent });
  parent._codeMirrorView = view;
  mixedCodeMirrorViews.add(view);
  mixedCodeMirrorView = view;
  view.scrollDOM.scrollTop = initialScroll;
  return view;
}

// A single stable compartment lets a focus change swap only the decoration
// facet through `dispatch`, instead of recreating the whole EditorState with
// `setState`. Recreating the state discards CodeMirror's measured line heights
// for this (often enormous, virtualized) document, so its next measure pass
// re-anchors the viewport and visibly jerks the scroll before any restore can
// catch it. A compartment reconfigure keeps the measurements and the scroll.
let mixedDecorationsCompartment = null;
function mixedGetDecorationsCompartment(runtime) {
  if (!mixedDecorationsCompartment) {
    mixedDecorationsCompartment = new runtime.Compartment();
  }
  return mixedDecorationsCompartment;
}

function buildMixedDecorations(runtime, model) {
  const { Decoration } = runtime;
  return Decoration.set(model.marks.map(mark => (
    Decoration.mark({
      class: `${mark.classes.join(" ")}${
        mark.focused
          ? mark.attributes["data-state-scope"]
            ? ` checkpoint-pane-focus flash trace-kind-${mark.attributes["data-state-scope"]}`
            : " fragment-focus flash"
          : ""
      }`,
      attributes: mark.attributes,
      tagName: mark.classes.includes("cm-mixed-removed")
        ? "del"
        : mark.classes.includes("cm-mixed-added") ? "ins" : "span",
    }).range(mark.start, mark.end)
  )), true);
}

// Swap only the focus decorations on the live view without recreating its
// state, preserving measured heights and scroll. Returns false if there is no
// view to update (callers then fall back to a full state build).
function reconfigureMixedDecorations() {
  if (!mixedCodeMirrorView || !mixedCodeMirrorApi || !mixedCodeMirrorModel) {
    return false;
  }
  const compartment = mixedGetDecorationsCompartment(mixedCodeMirrorApi);
  mixedCodeMirrorView.dispatch({
    effects: compartment.reconfigure(
      mixedCodeMirrorApi.EditorView.decorations.of(
        buildMixedDecorations(mixedCodeMirrorApi, mixedCodeMirrorModel),
      ),
    ),
  });
  return true;
}

function mixedCodeMirrorState(runtime, model, selection = undefined) {
  const {
    EditorState,
    EditorView,
  } = runtime;
  const compartment = mixedGetDecorationsCompartment(runtime);
  return EditorState.create({
    doc: model.text,
    selection,
    extensions: [
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
      EditorView.lineWrapping,
      compartment.of(EditorView.decorations.of(buildMixedDecorations(runtime, model))),
      EditorView.theme({
        "&": {
          height: "100%",
          color: "#dbe5df",
          backgroundColor: "#17201d",
          fontSize: "10px",
        },
        ".cm-scroller": {
          overflow: "auto",
          fontFamily: 'ui-monospace, "SFMono-Regular", "DejaVu Sans Mono", "Liberation Mono", Consolas, monospace',
          lineHeight: "1.5",
        },
        ".cm-content": { padding: "10px" },
        ".cm-gutters": { display: "none" },
        ".cm-mixed-added": {
          color: "#bff3d3",
          backgroundColor: "rgba(38, 119, 76, .48)",
          textDecoration: "none",
          borderBottom: "1px solid #59c98a",
        },
        ".cm-mixed-removed": {
          color: "#ffd0cc",
          backgroundColor: "rgba(139, 55, 51, .5)",
          textDecoration: "line-through",
          borderBottom: "1px solid #e66f6a",
        },
        ".cm-mixed-arrow": {
          color: "#d0a550",
          fontWeight: "700",
          textDecoration: "none",
        },
        // A removed part that is also linked to where it was added: keep the red
        // strike-through (removal) and add a green underline (addition origin), so
        // the two references are visually distinct on the one struck element.
        ".cm-mixed-added-origin": {
          borderBottom: "2px solid #59c98a",
          cursor: "pointer",
        },
        ".cm-mixed-scope": {
          borderLeft: "2px solid rgba(137, 183, 166, .42)",
        },
        ".cm-mixed-update": {
          cursor: "pointer",
        },
        ".cm-mixed-checkpoint-origin": {
          cursor: "pointer",
        },
        ".cm-mixed-update:hover": {
          outline: "1px solid #fff0a6",
          outlineOffset: "-1px",
        },
        ".cm-search-match": {
          color: "#18201d",
          backgroundColor: "#f4d96b",
          borderRadius: "2px",
          boxShadow: "inset 0 -1px #b98d18",
        },
        ".cm-search-match-selected": {
          backgroundColor: "#ffb84d",
          outline: "2px solid #fff3ba",
          outlineOffset: "1px",
        },
        "&.cm-focused": { outline: "2px solid #5d9c86" },
      }),
    ],
  });
}

function destroyMixedCodeMirrorViews() {
  mixedCodeMirrorGeneration += 1;
  mixedCodeMirrorViews.forEach(view => view.destroy());
  mixedCodeMirrorViews.clear();
  mixedCodeMirrorView = null;
  mixedCodeMirrorModel = null;
}

function renderMixed(previousDetail = null) {
  if (!state.detail) return;
  // Bound "where added" origin resolution for this render. Its cost is
  // distinct-removed-texts x segment-details x substring-scans, which on a deep
  // segment of large states would otherwise run for many seconds and freeze the
  // page. When the budget is spent, remaining removed parts simply get no
  // underline — the trace still renders.
  mixedOriginScanBudget = MIXED_ORIGIN_SCAN_BUDGET;
  const previousScroll = mixedCodeMirrorView?.scrollDOM.scrollTop || 0;
  const reusableView = mixedCodeMirrorView && mixedCodeMirrorApi
    ? mixedCodeMirrorView
    : null;
  if (!reusableView) destroyMixedCodeMirrorViews();
  const generation = mixedCodeMirrorGeneration;
  $("mixed").classList.remove("codemirror-fallback");
  const preserveScroll = requestsAreSimilar(previousDetail, state.detail);
  const checkpoint = isCheckpoint(state.detail);
  const identicalTo = identicalBaseCall(state.detail);
  const segment = state.mixedSegmentDetails.length
    && state.mixedSegmentDetails[state.mixedSegmentDetails.length - 1]?.id === state.detail.id
    ? state.mixedSegmentDetails
    : [state.detail];
  const selectedEntries = checkpoint ? [] : updateEntries(state.detail);
  const historicalEntries = checkpoint
    ? []
    : segment.slice(0, -1).flatMap(
        detail => isCheckpoint(detail)
          ? []
          : updateEntries(detail).map(entry => ({
              ...entry,
              fromEarlierCall: true,
            })),
      );
  const entries = checkpoint
    ? []
    : [...selectedEntries, ...historicalEntries];
  const inheritedCheckpoint = checkpoint
    ? null
    : segment.find(detail => isCheckpoint(detail)) || null;
  const checkpointAttributes = scope => scopeIsInherited(
    state.detail, scope, selectedEntries,
  ) ? checkpointOriginAttributes(inheritedCheckpoint, scope) : "";
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
  const contentHtml = mixedStateHtml(
    parts.contentText,
    inputEntries,
    parts.contentAnchors,
  );
  const inputHtml =
    `<span class="checkpoint-origin" data-state-scope="input"${
      checkpointAttributes("input")
    }>input:\n` +
    `<span class="checkpoint-origin" data-state-scope="input-params"${
      checkpointAttributes("input-params")
    }>${parameterHtml}</span>\n` +
    `${contentHtml}</span>`;
  const outputHtml =
    `<span class="checkpoint-origin" data-state-scope="output"${
      checkpointAttributes("output")
    }>${
      checkpoint
        ? escapeHtml(parts.outputText)
        : mixedStateHtml(parts.outputText, outputEntries, parts.outputAnchors)
    }</span>`;
  const thoughtsHtml = state.detail.thoughts
    ? `<span class="checkpoint-origin" data-state-scope="thoughts"${
        checkpointAttributes("thoughts")
      }>${
        checkpoint
          ? escapeHtml(parts.thoughtsText)
          : mixedStateHtml(parts.thoughtsText, thoughtsEntries, parts.thoughtsAnchors)
      }</span>`
    : "";
  $("mixed-status").textContent = checkpoint
    ? "◆ new current state"
    : identicalTo
      ? `↻ identical to call #${identicalTo}`
      : `Δ ${entries.length} accumulated update${entries.length === 1 ? "" : "s"} · ${
          segment.length
        } calls`;
  $("mixed-status").className = checkpoint ? "mixed-legend checkpoint" : "mixed-legend delta";
  $("mixed").classList.remove("empty");
  if (!reusableView) $("mixed").textContent = "Rendering complete Mixed trace…";
  const model = mixedCodeMirrorDocument(`${inputHtml}\n${thoughtsHtml}\n${outputHtml}`);
  mixedCodeMirrorModel = model;
  if (reusableView) {
    reusableView.setState(mixedCodeMirrorState(mixedCodeMirrorApi, model));
    reusableView.scrollDOM.scrollTop = preserveScroll ? previousScroll : 0;
    return;
  }
  mountMixedCodeMirror(
    $("mixed"),
    model,
    generation,
    preserveScroll ? previousScroll : 0,
  ).catch(() => {
    if (generation !== mixedCodeMirrorGeneration) return;
    // Full data remains visible if CodeMirror cannot be fetched.
    $("mixed").textContent = model.text;
    $("mixed").classList.add("codemirror-fallback");
  });
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

// The output/thoughts scope of an in-flight call, styled live (pulsing) while
// streaming. `data-live-body` lets a delta patch the text without a full rebuild.
function liveOutputScope(kind, bodyText, streaming) {
  const label = kind === "thoughts" ? "Thoughts" : "Output";
  return (
    `<span class="state-scope trace-kind-${kind} live-output-scope${streaming ? "" : " ended"}" data-state-scope="${kind}">` +
    `<span class="state-scope-label live-output-label">` +
    `<span class="live-dot${streaming ? "" : " off"}"></span>${label}` +
    (streaming ? ' <span class="live-status">streaming</span>' : "") +
    `</span>` +
    `<span class="state-scope-content live-output-body" data-live-body="${kind}">${escapeHtml(bodyText || "…")}</span>` +
    `</span>`
  );
}

function renderExact(focusEntry = null) {
  if (!state.detail) return;
  $("exact").classList.remove("empty");
  if (state.tab === "state") {
    const parts = stateDisplayParts(state.detail);
    const entries = isCheckpoint(state.detail) ? [] : updateEntries(state.detail);
    const inheritedCheckpoint = inheritedCheckpointDetail();
    const checkpointAttributes = scope => scopeIsInherited(
      state.detail, scope, entries,
    ) ? checkpointOriginAttributes(inheritedCheckpoint, scope) : "";
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
    // On an empty-parse call (context-exceeded / cancelled) show the raw captured
    // response verbatim with no diff marks, so the Output scope is never blank.
    let outputHtml = parts.outputIsRaw
      ? exactStateHtml(parts.rawOutputText, [])
      : exactStateHtml(
          parts.outputText,
          entries.filter(entry => entry.scope === "output"),
          focusEntry,
          parts.outputAnchors,
        );
    if (
      !parts.outputIsRaw
      && !String(state.detail.response || "").trim()
      && String(state.detail.thoughts || "").trim()
    ) {
      const reason = state.detail.status === "cancelled"
        ? "No output content before cancellation."
        : "No public output content; reasoning was captured in Thoughts.";
      outputHtml = `<span class="empty-output-note">${escapeHtml(reason)}</span>`;
    }
    const thoughtsHtml = state.detail.thoughts
      ? exactStateHtml(
          parts.thoughtsText,
          entries.filter(entry => entry.scope === "thoughts"),
          focusEntry,
          parts.thoughtsAnchors,
        )
      : "";
    // An in-flight call shows its output/thoughts live-styled (and streaming from
    // the synthetic detail); a stored call uses the normal diff-marked scopes.
    const live = state.detail.live;
    const streaming = state.detail.status === "streaming";
    const thoughtsScopeHtml = live
      ? (state.detail.thoughts ? liveOutputScope("thoughts", state.detail.thoughts, streaming) : "")
      : (thoughtsHtml ? stateScopeHtml("thoughts", thoughtsHtml, false, checkpointAttributes("thoughts")) : "");
    const outputScopeHtml = live
      ? liveOutputScope("output", state.detail.response, streaming)
      : stateScopeHtml(
          "output",
          outputHtml,
          false,
          checkpointAttributes("output"),
          parts.outputIsRaw ? "raw · unparsed" : "",
        );
    $("exact").innerHTML = `${
      stateScopeHtml(
        "input-params",
        parameterHtml,
        false,
        checkpointAttributes("input-params"),
      )
    }\n${
      stateScopeHtml("input", `input:\n${contentHtml}`, false, checkpointAttributes("input"))
    }\n${thoughtsScopeHtml}\n${outputScopeHtml}`;
    if (focusEntry) {
      requestAnimationFrame(() => {
        const target = $("exact").querySelector(".exact-focus");
        focusScrollIntoView(target, "start");
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
  // The next CodeMirror focus replaces the complete focused-range set, so it
  // clears stale model marks itself. Clearing here would force an unnecessary
  // intermediate rebuild and make repeated navigation race virtual layout.
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

function focusTimelineSelection(detail, preferredScope = "input", sourcePane = null) {
  clearPaneFocus();
  const panes = [$("mixed"), $("exact")];
  if (isCheckpoint(detail)) {
    const mixedCodeMirrorFocused = focusMixedCodeMirrorScope(preferredScope);
    for (const pane of panes) {
      const targets = [...pane.querySelectorAll("[data-state-scope]")].filter(node => (
        preferredScope === "output"
          ? node.dataset.stateScope === "output"
          : node.dataset.stateScope === "input"
      ));
      targets.forEach(node => {
        node.classList.add("checkpoint-pane-focus");
      });
      if (pane.id !== sourcePane && (pane.id !== "mixed" || !mixedCodeMirrorFocused)) {
        focusScrollIntoView(targets[0], "start");
      }
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
  const mixedEntryFocused = focusMixedCodeMirrorEntries(
    scopedEntries.map(entry => entry.entryKey),
    primaryEntry?.entryKey || null,
  );
  const mixedCodeMirrorFocused = mixedEntryFocused
    || focusMixedCodeMirrorScope(preferredScope);
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
  if (sourcePane !== "mixed" && !mixedCodeMirrorFocused) {
    if (primaryMixedTarget || mixedTargets[0]) {
      focusScrollIntoView(primaryMixedTarget || mixedTargets[0], "start");
    } else {
      const scope = $("mixed").querySelector(
        `[data-state-scope="${preferredScope}"]`,
      );
      scope?.classList.add("timeline-scope-focus", "flash");
      focusScrollIntoView(scope, "start");
    }
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
  if (sourcePane === "exact") {
    // A click inside Exact may update focus decoration, but it must never move
    // the pane the user is currently reading.
  } else if (primaryExactTarget) {
    focusScrollIntoView(primaryExactTarget, "start");
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

function focusTimelineUpdateCard(
  card,
  entryKey = null,
  preferredScope = "input",
  scrollIntoView = true,
) {
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
  for (const mark of marks) {
    // Force a fresh animation frame so a second click on the same event pulses
    // the corresponding updates again.
    void mark.offsetWidth;
    mark.classList.add("timeline-update-focus", "timeline-update-flash");
  }
  // Selecting a call/phase reveals the whole update from its start: anchor the
  // card's top (its "New current state / call #N" header) to the top of the
  // pane. Centering a mark inside instead pushed that header off the top, so a
  // tall card opened mid-content with no indication of which call it was.
  if (scrollIntoView) focusScrollIntoView(card, "start");
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

// Clear any prior update-entry focus across ALL cards, in BOTH styles — the
// mark-click style (.active/.update-back-focus) and the timeline/scope-click
// style (.timeline-update-focus/.flash). Clearing only within one card, or only
// one style, leaves the previous entry (often a different scope) still lit — the
// "second click focuses the wrong item" in the Updates pane.
function clearUpdateEntryFocus() {
  document.querySelectorAll(
    ".update-jump.active, .fragment-change.active, .checkpoint-section.active, "
    + ".update-back-focus, .timeline-update-focus, .timeline-update-flash",
  ).forEach(node => {
    node.classList.remove(
      "active", "update-back-focus", "timeline-update-focus", "timeline-update-flash",
    );
  });
}

function markUpdateTarget(card, target, button = null) {
  clearUpdateEntryFocus();
  card.classList.add("active");
  button?.classList.add("active");
  target.classList.add("active", "update-back-focus");
  focusScrollIntoView(target, "start");
  window.setTimeout(() => target.classList.remove("update-back-focus"), 1600);
}

// A removed part with a known origin shows two decorations: a strike-through
// (its removal) and a green underline at the baseline (where it was added). A
// click in the lower band — where the underline sits, clear of the strike line —
// follows the addition; anywhere else on the struck text focuses the removal.
function pointsAtAddedUnderline(event, element) {
  if (!element || !Number.isFinite(event?.clientY)) return false;
  const rect = [...element.getClientRects()].find(candidate => (
    event.clientX >= candidate.left && event.clientX <= candidate.right
    && event.clientY >= candidate.top && event.clientY <= candidate.bottom
  ));
  if (!rect) return false;
  return event.clientY >= rect.top + rect.height * 0.6;
}

function mixedNavigationEntryKey(element, event) {
  const owner = element.closest("[data-update-entry]");
  const removalKey = owner ? owner.dataset.updateEntry : element.dataset.updateEntry || null;
  const addedKey = owner?.dataset.addedEntry;
  if (addedKey && pointsAtAddedUnderline(event, owner)) return addedKey;
  return removalKey;
}

function focusStateUpdateEntry(
  entryKey,
  fragmentIndex = null,
  scrollMixedIntoView = true,
  preferredPart = null,
  scrollExactIntoView = true,
) {
  const mixedCodeMirrorFocused = focusMixedCodeMirror(
    entryKey,
    fragmentIndex == null ? null : [fragmentIndex],
    preferredPart,
    scrollMixedIntoView,
  );
  for (const pane of [$("mixed"), $("exact")]) {
    // CodeMirror owns both decoration and navigation for Mixed. Its virtual DOM
    // may still contain an old or different half of this update while the
    // target scroll is pending, so a DOM fallback here would override it.
    if (pane.id === "mixed" && mixedCodeMirrorFocused) continue;
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
    let targets = [...pane.querySelectorAll(
      `[data-update-entry="${entryKey}"], [data-added-entry="${entryKey}"]`,
    )];
    if (fragmentIndex != null) {
      const fragmentTargets = targets.filter(
        node => Number(node.dataset.outputFragment) === fragmentIndex,
      );
      if (fragmentTargets.length) targets = fragmentTargets;
    }
    if (pane.id === "mixed" && preferredPart) {
      const partTargets = targets.filter(node => partMatches(node, preferredPart));
      if (partTargets.length) targets = partTargets;
    }
    targets.forEach(node => {
      // Restart the pulse when the same update is clicked repeatedly.
      void node.offsetWidth;
      node.classList.add(
        pane.id === "mixed" ? "fragment-focus" : "exact-focus",
        "flash",
      );
    });
    const scrollPaneIntoView = pane.id === "mixed"
      ? scrollMixedIntoView
      : scrollExactIntoView;
    if (scrollPaneIntoView) {
      focusScrollIntoView(targets[0], "start");
    }
  }
}

async function focusStateCheckpointOrigin(element, origin, sourcePane = null) {
  const callId = Number(origin.dataset.checkpointCall);
  const scope = origin.dataset.checkpointScope;
  if (!Number.isFinite(callId) || !scope) return;
  const clickedInMixed = sourcePane === "mixed" || $("mixed").contains(element);
  const clickedInExact = sourcePane === "exact" || $("exact").contains(element);
  const mixedCodeMirrorFocused = focusMixedCodeMirrorCheckpointOrigin(
    callId,
    scope,
    !clickedInMixed,
  );
  for (const pane of [$("mixed"), $("exact")]) {
    if (pane.id === "mixed" && mixedCodeMirrorFocused) continue;
    pane.querySelectorAll(
      ".fragment-focus, .exact-focus, .checkpoint-pane-focus, .timeline-scope-focus, .flash",
    ).forEach(node => {
      node.classList.remove(
        "fragment-focus",
        "exact-focus",
        "checkpoint-pane-focus",
        "timeline-scope-focus",
        "flash",
      );
    });
    const target = pane.querySelector(
      `[data-checkpoint-call="${callId}"][data-checkpoint-scope="${scope}"]`,
    );
    target?.classList.add("checkpoint-pane-focus", "flash");
    const preserveSourcePane = pane.id === "mixed" ? clickedInMixed : clickedInExact;
    if (!preserveSourcePane) focusScrollIntoView(target, "start");
  }

  const phase = scope === "output" || scope === "thoughts" ? "output" : "input";
  const key = `call:${callId}`;
  setTimelineFocus(key, phase, true);
  const card = updateCardFor(key, phase);
  if (!card) return;
  await loadUpdateCard(card);
  document.querySelectorAll(".update-card.active").forEach(node => {
    node.classList.remove("active");
  });
  const section = card.querySelector(`[data-checkpoint-scope="${scope}"]`);
  if (section) {
    markUpdateTarget(card, section);
  } else {
    card.classList.add("active");
    focusScrollIntoView(card, "start");
  }
}

async function focusUpdateFromState(
  element,
  sourcePane = null,
  navigationEntryKey = null,
) {
  const updateElement = element.closest("[data-update-entry]");
  if (updateElement) {
    const clickedInMixed = sourcePane === "mixed" || $("mixed").contains(updateElement);
    const clickedInExact = sourcePane === "exact" || $("exact").contains(updateElement);
    const preferredPart = clickedInMixed
      ? updateElement.tagName === "DEL"
        ? "removed"
        : updateElement.tagName === "INS" ? "added" : null
      // Exact reconstructs current state, so a changed range corresponds to
      // Mixed's present/added half rather than its removed historical half.
      : $("exact").contains(updateElement) ? "added" : null;
    const selectedEntryKey = navigationEntryKey || updateElement.dataset.updateEntry;
    const navigatesToAddition = selectedEntryKey !== updateElement.dataset.updateEntry;
    const [rawCallId, rawEntryIndex] = selectedEntryKey.split(":");
    const callId = Number(rawCallId);
    const entryIndex = Number(rawEntryIndex);
    const key = `call:${callId}`;
    const fragmentIndex = navigatesToAddition || updateElement.dataset.outputFragment == null
      ? null
      : Number(updateElement.dataset.outputFragment);
    focusStateUpdateEntry(
      selectedEntryKey,
      fragmentIndex,
      !clickedInMixed,
      preferredPart,
      !clickedInExact,
    );
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

  const checkpointOrigin = element.closest(".state-scope-label")
    ? null
    : element.closest("[data-checkpoint-call][data-checkpoint-scope]");
  if (checkpointOrigin) {
    await focusStateCheckpointOrigin(element, checkpointOrigin, sourcePane);
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
  // The pane the click came from must not move — the user is already looking at
  // it. Only the *other* pane scrolls to the corresponding item. This runs after
  // an awaited card load, past pinPaneScroll's short restore window, so the skip
  // has to be explicit here rather than relying on the pin to undo it.
  const clickedInMixed = sourcePane === "mixed" || $("mixed").contains(element);
  const clickedInExact = sourcePane === "exact" || $("exact").contains(element);
  const entryKeys = new Set(entries.map(entry => entry.entryKey));
  let foundEntry = false;
  for (const pane of [$("mixed"), $("exact")]) {
    const isSourcePane = pane.id === "mixed" ? clickedInMixed : clickedInExact;
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
      if (!isSourcePane) focusScrollIntoView(targets[0], "start");
    } else {
      const targetScope = pane.querySelector(`[data-state-scope="${scope}"]`);
      targetScope?.classList.add(
        isCheckpoint(state.detail) ? "checkpoint-pane-focus" : "timeline-scope-focus",
        "flash",
      );
      if (!isSourcePane) focusScrollIntoView(targetScope, "start");
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
  pane.addEventListener("mousedown", event => {
    if (event.button !== 0) return;
    panePointerScrolls.set(pane, {
      ...paneScrollPosition(pane),
      pointerX: event.clientX,
      pointerY: event.clientY,
      // CodeMirror may replace a decoration while processing this mousedown.
      // Retain the original semantic element so the subsequent pane-level click
      // still knows which update/checkpoint/scope the user chose.
      semanticTarget: event.target.closest(
        "[data-update-entry], [data-checkpoint-call][data-checkpoint-scope], [data-state-scope]",
      ),
      navigationEntryKey: pane.id === "mixed"
        ? mixedNavigationEntryKey(
            event.target.closest("[data-update-entry]") || event.target,
            event,
          )
        : null,
    });
  }, { capture: true });
  pane.addEventListener("click", event => {
    const pointerScroll = panePointerScrolls.get(pane) || null;
    panePointerScrolls.delete(pane);
    const pointerDistance = pointerScroll
      ? Math.hypot(
          event.clientX - pointerScroll.pointerX,
          event.clientY - pointerScroll.pointerY,
        )
      : 0;
    // Preserve text selection only when this gesture actually dragged. A stale
    // selection from an earlier gesture must not consume the first normal click
    // and force the user to click a mark twice.
    if (pointerDistance > 4 && hasTextSelectionWithin(pane)) return;
    const capturedTarget = pointerScroll?.semanticTarget || null;
    const updateTarget = event.target.closest("[data-update-entry]")
      || capturedTarget?.closest("[data-update-entry]");
    if (updateTarget) {
      // A removed part whose "where added" origin is a whole earlier call (rather
      // than a specific change): clicking its green underline points to that call
      // in the other panes — the timeline scrolls to and flashes it — while the
      // Mixed pane the user clicked in stays put. The rest of the struck text
      // focuses the removal.
      const addedCall = updateTarget.dataset.addedCall;
      if (addedCall && pointsAtAddedUnderline(event, updateTarget)) {
        pinPaneScroll(pane, pointerScroll);
        setTimelineFocus(`call:${addedCall}`, "input", true);
        return;
      }
      pinPaneScroll(pane, pointerScroll);
      focusUpdateFromState(
        updateTarget,
        pane.id,
        pointerScroll?.navigationEntryKey || mixedNavigationEntryKey(updateTarget, event),
      );
      return;
    }
    const checkpointOrigin = event.target.closest(
      "[data-checkpoint-call][data-checkpoint-scope]",
    ) || capturedTarget?.closest("[data-checkpoint-call][data-checkpoint-scope]");
    if (checkpointOrigin && !event.target.closest(".state-scope-label")) {
      pinPaneScroll(pane, pointerScroll);
      focusUpdateFromState(checkpointOrigin, pane.id);
      return;
    }
    // A drag remains plain text selection (handled above), while a normal click
    // anywhere in reconstructed scope content focuses that scope's owning
    // Timeline/Updates item. Limiting this to the label made clicks on "input:"
    // and its plain inherited text appear to do nothing.
    const scopeLabel = event.target.closest(".state-scope-label")
      || capturedTarget?.closest(".state-scope-label");
    const target = scopeLabel
      || event.target.closest("[data-state-scope]")
      || capturedTarget?.closest("[data-state-scope]");
    if (target) {
      pinPaneScroll(pane, pointerScroll);
      focusUpdateFromState(target, pane.id);
    }
  });
  pane.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest(
      "[data-update-entry], [data-checkpoint-call][data-checkpoint-scope], .state-scope-label",
    );
    if (!target) return;
    event.preventDefault();
    pinPaneScroll(pane);
    focusUpdateFromState(
      target.closest("[data-update-entry], [data-state-scope]"),
      pane.id,
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
  sourcePane = null,
) {
  const key = `${type}:${id}`;
  if (state.pendingSelection?.key === key) {
    await state.pendingSelection.promise.catch(() => {});
    return applySelection(
      type, id, element, scrollTimeline, focusSelection, sourcePane,
    );
  }
  const promise = applySelection(
    type, id, element, scrollTimeline, focusSelection, sourcePane,
  );
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
  sourcePane = null,
) {
  const button = element || document.querySelector(`.timeline-item[data-key="${type}:${id}"]`);
  const phase = button?.dataset.phase || "input";
  const key = `${type}:${id}`;
  const selectionVersion = ++state.selectionVersion;
  setTimelineFocus(
    key,
    phase,
    scrollTimeline && sourcePane !== "timeline",
    button,
    sourcePane === "follow" ? "end" : "start",
  );
  document.querySelectorAll(".update-card.active").forEach(node => node.classList.remove("active"));
  const updateCard = updateCardFor(key, phase);
  updateCard?.classList.add("active");
  const previousDetail = state.detail;
  // Re-selecting the call already on screen must not rebuild Mixed: for a long
  // segment that costs a visible pause, and every entry click inside one card
  // would restart it — long enough for the next click to abort the previous
  // selection, so the focus it was about to apply never arrived.
  // Compare by string so live (synthetic) string ids like "live-1" match — a
  // Number() comparison yields NaN !== NaN and forces a full re-render on every
  // repeat click of a streaming call, making its focus behave unlike a stored one.
  const alreadyRendered = state.selected
    && state.selected.type === type
    && String(state.selected.id) === String(id)
    && String(state.detail?.id) === String(id)
    && state.mixedSegmentDetails.at(-1)?.id === state.detail?.id;
  state.selected = { type, id };
  updateCleanHistoryControl();
  state.selectedPhase = phase;
  syncBranchGraphSelection();
  if (alreadyRendered) {
    activateTab("state");
    if (focusSelection) {
      if (updateCard) await loadUpdateCard(updateCard);
      if (selectionVersion !== state.selectionVersion) return;
      const renderedEntryKey = focusTimelineSelection(state.detail, phase, sourcePane);
      focusTimelineUpdateCard(
        updateCard, renderedEntryKey, phase, sourcePane !== "updates",
      );
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
  $("lineage").textContent = state.detail.live
    ? (state.detail.status === "streaming" ? "live · streaming" : "live")
    : `${reqLabel}state S${state.detail.request_state_id} ← ${parentLabel}${score}`;
  activateTab("state");
  renderMixed(previousDetail);
  renderExact();
  if (focusSelection) {
    if (updateCard) await loadUpdateCard(updateCard);
    if (selectionVersion !== state.selectionVersion) return;
    const focusedEntryKey = focusTimelineSelection(state.detail, phase, sourcePane);
    focusTimelineUpdateCard(
      updateCard, focusedEntryKey, phase, sourcePane !== "updates",
    );
  }
}

function renderEmptyCurrentState() {
  state.selected = null;
  state.timelineFocus = null;
  updateCleanHistoryControl();
  state.detail = null;
  state.mixedHistoryComplete = true;
  state.searchFocus = null;
  destroyMixedCodeMirrorViews();
  $("mixed-status").textContent = "Select a call";
  $("mixed-status").className = "mixed-legend";
  $("lineage").textContent = "Select an event";
  $("mixed").classList.add("empty");
  $("mixed").textContent = "No LLM calls in this session.";
  $("exact").classList.add("empty");
  $("exact").textContent = "No current state.";
  $("updates").innerHTML = '<div class="empty-session">No updates in this session.</div>';
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
    renderEmptyCurrentState();
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
  const epoch = state.timelineEpoch;
  const sessionQuery = state.session ? `&session=${encodeURIComponent(state.session)}` : "";
  const records = await fetchJson(`/api/timeline?limit=1000${sessionQuery}`);
  // Do not allow a poll started before a destructive clean to restore rows that
  // were deleted while its request was in flight.
  if (epoch !== state.timelineEpoch) return false;
  // A durable running row and its live side-channel record represent the same
  // call. Keep rendering the richer live version until response persistence is
  // confirmed, then reconcile it directly to the durable row.
  const liveCallIds = new Set(
    [...state.live.values()]
      .filter(record => !record.persisted && record.call_id != null)
      .map(record => Number(record.call_id)),
  );
  const items = records.filter(item => (
    item.type === "call" && !liveCallIds.has(Number(item.id))
  ));
  renderWaitingCalls();
  const signature = items.map(item => (
    `${itemKey(item)}:${item.status}:${item.branch_id}:${item.duration_ms ?? ""}:`
    + `${item.title ?? ""}:${item.debug_label ?? ""}:${JSON.stringify(item.usage || {})}`
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
    if (previous && (
      previous.status !== item.status
      || previous.title !== item.title
      || previous.debug_label !== item.debug_label
      || previous.duration_ms !== item.duration_ms
      || JSON.stringify(previous.usage || {}) !== JSON.stringify(item.usage || {})
    )) {
      // Apply the new status first: a completed call earns an output card, and
      // its arrival time decides where that card belongs in the sequence.
      Object.assign(previous, item);
      changed.push(item);
    }
  }
  const appended = items.filter(item => !previousByKey.has(itemKey(item)));
  state.timelineItems = [...previousItems, ...appended];
  // Reconcile: each newly stored call supersedes an ended synthetic live call
  // (streams are near-sequential; match oldest-ended → each new stored call).
  const endedLive = [...state.live.values()]
    .filter(record => record.endedAt)
    .sort((left, right) => left.endedAt - right.endedAt);
  let reselectStored = null;
  const droppedLiveKeys = new Set();
  const unmatchedEnded = [...endedLive];
  for (const storedItem of appended) {
    const storedIdentity = storedItem.req_id || storedItem.request_id || null;
    let recordIndex = storedIdentity
      ? unmatchedEnded.findIndex(record => (
          (record.req_id || record.request_id || null) === storedIdentity
        ))
      : -1;
    // Older clients may not send a public request id. Retain the chronological
    // fallback for those calls, while matching identified parallel calls exactly.
    if (recordIndex < 0 && !storedIdentity) recordIndex = 0;
    if (recordIndex < 0 || recordIndex >= unmatchedEnded.length) continue;
    const [record] = unmatchedEnded.splice(recordIndex, 1);
    if (isLiveSelected(record.live_id)) reselectStored = storedItem;
    state.live.delete(record.live_id);
    const liveKey = `call:${liveId(record.live_id)}`;
    state.details.delete(liveKey);
    droppedLiveKeys.add(liveKey);
  }
  if (droppedLiveKeys.size) {
    state.timelineItems = state.timelineItems.filter(
      item => !droppedLiveKeys.has(itemKey(item)),
    );
  }
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
  if (followNewItems && (appended.length || changed.length)) {
    // Status changes can add an output event without appending a call. Follow
    // whichever event is now last in the same ordering rendered by Timeline.
    await followLatestTimelineEvent();
  } else if (reselectStored) {
    // The live call the user was watching just became a stored call; move the
    // selection onto it so Mixed/Updates show the real diffs.
    await selectItem(reselectStored.type, reselectStored.id, null, false, false);
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
    state.mixedHistoryComplete = true;
    destroyMixedCodeMirrorViews();
    state.selected = null;
    state.timelineFocus = null;
    updateCleanHistoryControl();
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
  const overLimit = data.max_file_bytes
    && data.file_bytes > data.max_file_bytes;
  const node = $("stats");
  node.textContent =
    `${data.calls} calls · DB ${size}${limit}${overLimit ? " ⚠" : ""} · ${saved.toFixed(0)}% blob reduction`;
  node.classList.toggle("over-limit", Boolean(overLimit));
  node.title = overLimit
    ? `Database is ${size}, over the ${(data.max_file_bytes / 1024 / 1024).toFixed(0)} MB retention limit — `
      + "oldest sessions are pruned, but the current session is protected and cannot shrink."
    : `Database size: ${size}${limit ? `, retention limit ${limit.slice(3)}` : " (no retention limit set)"}`;
}

function clearDomSearchHighlights(root = document) {
  root.querySelectorAll("mark.search-text-match").forEach(mark => {
    mark.replaceWith(document.createTextNode(mark.textContent || ""));
  });
  root.normalize();
}

function highlightDomSearch(root, query, preferredSelector = null) {
  clearDomSearchHighlights(root);
  const needle = query.toLocaleLowerCase();
  if (!needle) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue?.toLocaleLowerCase().includes(needle)) {
        return NodeFilter.FILTER_REJECT;
      }
      if (node.parentElement?.closest("script, style, mark.search-text-match")) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  const matches = [];
  for (const textNode of textNodes) {
    const value = textNode.nodeValue || "";
    const folded = value.toLocaleLowerCase();
    const preferred = Boolean(
      preferredSelector && textNode.parentElement?.closest(preferredSelector),
    );
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    while (cursor < value.length) {
      const start = folded.indexOf(needle, cursor);
      if (start < 0) break;
      if (start > cursor) fragment.append(value.slice(cursor, start));
      const mark = document.createElement("mark");
      mark.className = "search-text-match";
      mark.textContent = value.slice(start, start + query.length);
      mark.dataset.searchPreferred = preferred ? "true" : "false";
      fragment.append(mark);
      matches.push(mark);
      cursor = start + query.length;
    }
    if (cursor < value.length) fragment.append(value.slice(cursor));
    textNode.replaceWith(fragment);
  }
  const selected = matches.find(mark => mark.dataset.searchPreferred === "true")
    || matches[0]
    || null;
  selected?.classList.add("search-text-match-selected");
  return selected;
}

function mixedSearchScope(field) {
  return field === "thoughts" ? "thoughts" : field === "output" ? "output" : "input";
}

function highlightMixedSearch(query, field) {
  if (!mixedCodeMirrorModel || !mixedCodeMirrorApi || !mixedCodeMirrorView) return null;
  mixedCodeMirrorModel.marks = mixedCodeMirrorModel.marks.filter(
    mark => !mark.classes.includes("cm-search-match"),
  );
  const folded = mixedCodeMirrorModel.text.toLocaleLowerCase();
  const needle = query.toLocaleLowerCase();
  const scope = mixedSearchScope(field);
  const scopeRanges = mixedCodeMirrorModel.marks.filter(mark => (
    mark.attributes["data-state-scope"] === scope
  ));
  const matches = [];
  let cursor = 0;
  while (needle && cursor < folded.length) {
    const start = folded.indexOf(needle, cursor);
    if (start < 0) break;
    const end = start + query.length;
    matches.push({
      start,
      end,
      preferred: scopeRanges.some(range => start >= range.start && end <= range.end),
    });
    cursor = end;
  }
  const selected = matches.find(match => match.preferred) || matches[0] || null;
  for (const match of matches) {
    mixedCodeMirrorModel.marks.push({
      start: match.start,
      end: match.end,
      classes: [
        "cm-search-match",
        match === selected ? "cm-search-match-selected" : "",
      ].filter(Boolean),
      attributes: {},
      scope,
    });
  }
  mixedCodeMirrorModel.marks.sort(
    (left, right) => left.start - right.start || left.end - right.end,
  );
  mixedCodeMirrorView.setState(mixedCodeMirrorState(
    mixedCodeMirrorApi,
    mixedCodeMirrorModel,
    mixedCodeMirrorView.state.selection,
  ));
  if (selected) {
    supersedePaneScrollPreservation();
    mixedCodeMirrorView.dispatch({
      selection: { anchor: selected.start },
      effects: mixedCodeMirrorApi.EditorView.scrollIntoView(
        selected.start,
        { y: "center" },
      ),
    });
  }
  return selected;
}

function mutatePreservingPaneScroll(action) {
  const generation = ++paneScrollPreservationGeneration;
  const snapshots = [$("timeline"), $("mixed"), $("exact"), $("updates")]
    .filter(Boolean)
    .map(paneScrollPosition);
  for (const { scroller } of snapshots) {
    const animation = focusScrollAnimations.get(scroller);
    if (animation) cancelAnimationFrame(animation);
    focusScrollAnimations.delete(scroller);
  }

  action();

  let userMoved = false;
  const stopRestoring = () => { userMoved = true; };
  const directScrollEvents = ["wheel", "touchstart", "pointerdown", "keydown"];
  for (const { scroller } of snapshots) {
    for (const eventName of directScrollEvents) {
      scroller.addEventListener(eventName, stopRestoring, { once: true, passive: true });
    }
  }
  const restore = () => {
    if (userMoved || generation !== paneScrollPreservationGeneration) return;
    for (const { scroller, top, left } of snapshots) {
      if (scroller.scrollTop !== top) scroller.scrollTop = top;
      if (scroller.scrollLeft !== left) scroller.scrollLeft = left;
    }
  };
  const cleanup = () => {
    for (const { scroller } of snapshots) {
      for (const eventName of directScrollEvents) {
        scroller.removeEventListener(eventName, stopRestoring);
      }
    }
  };
  // Removing DOM marks can trigger scroll anchoring, and CodeMirror measures its
  // replacement decorations on the following frame. Preserve the viewport
  // through both without preventing a new, intentional user scroll.
  restore();
  requestAnimationFrame(() => {
    restore();
    requestAnimationFrame(restore);
  });
  setTimeout(restore, 50);
  setTimeout(cleanup, 80);
}

function clearSearchHighlights() {
  state.searchFocus = null;
  mutatePreservingPaneScroll(() => {
    clearDomSearchHighlights();
    highlightMixedSearch("", "input");
  });
}

async function focusSearchResult(result, query) {
  state.searchFocus = { ...result, query };
  await selectItem("call", result.owner_id, null, true, false, "search");
  const key = `call:${result.owner_id}`;
  const phase = result.field === "input" ? "input" : "output";
  for (const card of document.querySelectorAll(`.update-card[data-key="${key}"]`)) {
    await loadUpdateCard(card);
  }
  highlightMixedSearch(query, result.field);
  const exactScope = mixedSearchScope(result.field);
  const exactMatch = highlightDomSearch(
    $("exact"), query, `[data-state-scope="${exactScope}"]`,
  );
  const updatesMatch = highlightDomSearch(
    $("updates"), query, `.update-card[data-key="${key}"][data-phase="${phase}"]`,
  );
  const timelineMatch = highlightDomSearch(
    $("timeline"), query, `[data-key="${key}"], [data-call-key="${key}"]`,
  );
  for (const match of [exactMatch, updatesMatch, timelineMatch]) {
    if (match) focusScrollIntoView(match, "center");
  }
}

async function runSearch(event) {
  event.preventDefault();
  const query = $("search").value.trim();
  const box = $("search-results");
  if (!query) {
    box.classList.add("hidden");
    clearSearchHighlights();
    return;
  }
  const fields = [...document.querySelectorAll(
    'input[name="search-field"]:checked',
  )].map(input => input.value);
  if (!fields.length) {
    box.innerHTML = `
      <div class="search-results-head">
        <strong>Select at least one field</strong>
        <button type="button" aria-label="Hide search results">×</button>
      </div>`;
    box.querySelector("button").onclick = () => box.classList.add("hidden");
    box.classList.remove("hidden");
    return;
  }
  const sessionPart = state.session ? `&session=${encodeURIComponent(state.session)}` : "";
  const fieldPart = `&fields=${encodeURIComponent(fields.join(","))}`;
  const allResults = await fetchJson(
    `/api/search?q=${encodeURIComponent(query)}${sessionPart}${fieldPart}`,
  );
  if ($("search").value.trim() !== query) return;
  // Filter locally as well so the UI remains correct while an older running
  // proxy process is still serving the newly loaded static frontend. The
  // backend filter remains necessary to apply the limit after field selection.
  const selectedFields = new Set(fields);
  const results = allResults.filter(result => (
    result.owner_type === "call" && selectedFields.has(result.field)
  ));
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
    node.onclick = async () => {
      box.classList.add("hidden");
      await focusSearchResult(result, query);
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

async function cleanHistoryFromHere() {
  const button = $("reset-history");
  const selected = cleanHistoryTarget();
  if (!button || !selected) return;
  button.dataset.busy = "true";
  button.disabled = true;
  const previousLabel = button.textContent;
  button.textContent = "…";
  try {
    const preview = await fetchJson(
      `/api/history/reset?call_id=${encodeURIComponent(selected.id)}`,
    );
    if (!preview.delete_calls) {
      window.alert(`Call #${selected.id} has no history to remove in this session.`);
      return;
    }
    if (!preview.can_reset) {
      window.alert(
        `Cannot clean history while older calls are running: ${
          preview.running_call_ids.map(id => `#${id}`).join(", ")
        }`,
      );
      return;
    }
    const confirmed = window.confirm(
      `Clean from here: permanently delete selected call #${selected.id} and the ${
        preview.delete_calls - 1
      } call${preview.delete_calls - 1 === 1 ? "" : "s"} older than it (${
        preview.delete_calls
      } total) from session “${
        preview.session_id
      }”?\n\nNewer calls will remain. This cannot be undone.`,
    );
    if (!confirmed) return;
    await fetchJson("/api/history/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: selected.id }),
    });

    // Invalidate any poll that began before the deletion, then remove the
    // deleted prefix synchronously. The reconciliation fetch below should never
    // be required just to make deleted items disappear from the screen.
    state.timelineEpoch += 1;
    const selectedIndex = state.timelineItems.findIndex(
      item => itemKey(item) === selected.key,
    );
    const removedItems = selectedIndex >= 0
      ? state.timelineItems.slice(0, selectedIndex + 1)
      : state.timelineItems.filter(item => itemKey(item) === selected.key);
    const removedKeys = new Set(removedItems.map(itemKey));
    state.timelineItems = selectedIndex >= 0
      ? state.timelineItems.slice(selectedIndex + 1)
      : state.timelineItems.filter(item => itemKey(item) !== selected.key);
    for (const key of removedKeys) state.details.delete(key);
    renderTimelineEvents(state.timelineItems);
    renderUpdateCards(state.timelineItems);

    state.selectionVersion += 1;
    state.pendingSelection = null;
    state.details.clear();
    state.detail = null;
    state.mixedSegmentDetails = [];
    state.mixedHistoryComplete = true;
    state.timelineSignature = "";
    state.lastTimelineKey = null;
    // The selected call was deleted too; let the rebuild choose the new oldest.
    state.timelineFocus = null;
    state.selected = null;
    // Clear the deleted selection before any reconciliation request. In
    // particular, an empty server result has the same empty signature assigned
    // above and may legitimately short-circuit loadTimeline().
    renderEmptyCurrentState();
    $("search-results").classList.add("hidden");
    state.sessionsSignature = "";
    await loadSessions();
    await loadTimeline();
    if (!state.selected && state.timelineItems.length) {
      const oldestRemaining = state.timelineItems[0];
      await selectItem(
        oldestRemaining.type, oldestRemaining.id, null, false, true,
      );
    }
    await loadStats();
  } catch (error) {
    window.alert(`Could not clean history: ${error.message}`);
  } finally {
    delete button.dataset.busy;
    button.textContent = previousLabel;
    updateCleanHistoryControl();
  }
}

$("reset-history").onclick = cleanHistoryFromHere;
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
  await followLatestTimelineEvent();
};
$("search-form").onsubmit = runSearch;
$("search").addEventListener("input", () => {
  if (!$("search").value.trim()) {
    $("search-results").classList.add("hidden");
    clearSearchHighlights();
  }
});
$("search").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    $("search-results").classList.add("hidden");
    clearSearchHighlights();
  }
});
$("session").onchange = async () => {
  state.session = $("session").value;
  state.selected = null;
  state.timelineFocus = null;
  updateCleanHistoryControl();
  state.detail = null;
  state.details.clear();
  state.mixedSegmentDetails = [];
  state.mixedHistoryComplete = true;
  destroyMixedCodeMirrorViews();
  state.timelineSignature = "";
  state.timelineItems = [];
  $("search-results").classList.add("hidden");
  startLiveStream();
  await loadTimeline();
};

// The live side-channel (SSE /api/live) shows a call's raw output while it is
// still streaming — before the durable record exists. It is independent of the
// timeline poll: watch-only, and cleared once the stream ends (the completed
// call then arrives through the normal timeline/update path).
// The server relays the upstream bytes verbatim (SSE frames), which are not
// readable. Parse them here into the generated text — the model's content and
// reasoning deltas — so the live card shows words, not JSON envelopes. Chunks do
// not align to frame boundaries, so an incomplete trailing frame is held back in
// `buffer` and completed by the next chunk.
function mergeLiveToolCallDeltas(record, deltas) {
  if (!Array.isArray(deltas)) return;
  record.toolCalls ||= {};
  deltas.forEach((delta, position) => {
    if (!delta || typeof delta !== "object") return;
    const index = Number.isInteger(delta.index) ? delta.index : position;
    const call = record.toolCalls[index] ||= {
      index,
      id: "",
      type: "",
      function: {name: "", arguments: ""},
    };
    if (!call.id && typeof delta.id === "string") call.id = delta.id;
    if (!call.type && typeof delta.type === "string") call.type = delta.type;
    const fn = delta.function;
    if (!fn || typeof fn !== "object") return;
    if (typeof fn.name === "string") call.function.name += fn.name;
    if (typeof fn.arguments === "string") {
      call.function.arguments += fn.arguments;
    }
  });
}

function readableLiveToolCalls(record) {
  const calls = Object.values(record.toolCalls || {})
    .sort((left, right) => left.index - right.index)
    .map(call => {
      let args = call.function.arguments;
      try {
        args = JSON.parse(args);
      } catch {
        // An in-flight call may still have incomplete JSON. Keep the joined
        // argument text readable until later deltas complete it.
      }
      return {
        index: call.index,
        ...(call.id ? {id: call.id} : {}),
        type: call.type || "function",
        function: {name: call.function.name, arguments: args},
      };
    });
  return calls.length ? JSON.stringify(calls, null, 2) : "";
}

function refreshLiveOutput(record) {
  const text = record.outputText || "";
  const calls = readableLiveToolCalls(record);
  // Common case (no tool calls) reuses the accumulated string instead of
  // concatenating a fresh full copy every chunk (O(n^2) over a long stream).
  record.output = calls
    ? [text, calls].filter(Boolean).join(text.endsWith("\n") ? "" : "\n")
    : text;
}

function liveUsage(payload) {
  const usage = payload?.usage && typeof payload.usage === "object"
    ? payload.usage
    : {};
  const timings = payload?.timings && typeof payload.timings === "object"
    ? payload.timings
    : {};
  const input = Number.isInteger(usage.prompt_tokens)
    ? usage.prompt_tokens
    : Number.isInteger(usage.input_tokens)
      ? usage.input_tokens
      : Number.isInteger(timings.prompt_n) ? timings.prompt_n : null;
  const output = Number.isInteger(usage.completion_tokens)
    ? usage.completion_tokens
    : Number.isInteger(usage.output_tokens)
      ? usage.output_tokens
      : Number.isInteger(timings.predicted_n) ? timings.predicted_n : null;
  const total = Number.isInteger(usage.total_tokens)
    ? usage.total_tokens
    : input != null && output != null ? input + output : null;
  return Object.fromEntries([
    ["input_tokens", input],
    ["output_tokens", output],
    ["total_tokens", total],
  ].filter(([, value]) => value != null));
}

function feedLive(record, chunk) {
  record.buffer = (record.buffer || "") + (chunk || "");
  const frames = record.buffer.split("\n\n");
  record.buffer = frames.pop() ?? "";
  let changed = false;
  for (const frame of frames) {
    for (const line of frame.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      let parsed;
      try {
        parsed = JSON.parse(payload);
      } catch {
        continue;
      }
      const choice = (parsed.choices && parsed.choices[0]) || {};
      const usage = liveUsage(parsed);
      if (Object.keys(usage).length) {
        record.usage = {...record.usage, ...usage};
        // The server reports real token counts only in its final frame. Once it
        // does, stop overriding them with the streamed estimate.
        if (usage.output_tokens != null) record.usageIsReal = true;
      }
      const delta = choice.delta || choice.message || {};
      let produced = false;
      // Reasoning streams into the thoughts scope, content into the output scope
      // — mirroring how a stored call splits them in the Exact pane.
      if (typeof delta.reasoning_content === "string") {
        record.thoughts = (record.thoughts || "") + delta.reasoning_content;
        produced = produced || delta.reasoning_content !== "";
      }
      if (typeof delta.content === "string") {
        record.outputText = (record.outputText || "") + delta.content;
        produced = produced || delta.content !== "";
      }
      if (typeof choice.text === "string") {
        record.outputText = (record.outputText || "") + choice.text;
        produced = produced || choice.text !== "";
      }
      if (delta.tool_calls) {
        mergeLiveToolCallDeltas(record, delta.tool_calls);
        produced = true;
      }
      if (parsed.error) {
        const message = parsed.error.message || JSON.stringify(parsed.error);
        record.outputText = `${record.outputText || ""}\n\n⚠ ${message}`;
      }
      // Each streamed piece is ~one token; count them so the active row shows a
      // live output-token estimate until the server sends its real count. Mark
      // when generation actually began (first produced token) so the live speed
      // measures decode rate, not the prompt-eval wait before it.
      if (produced) {
        record.streamTokens = (record.streamTokens || 0) + 1;
        if (!record.genStartMs) record.genStartMs = Date.now();
      }
      changed = true;
    }
  }
  // Rebuild the assembled output once per chunk, not per line: rebuilding the
  // whole growing string on every frame is O(n^2) over a long stream.
  if (changed) {
    refreshLiveOutput(record);
    if (!record.usageIsReal && record.streamTokens) {
      const updates = { output_tokens: record.streamTokens };
      const elapsed = record.genStartMs ? (Date.now() - record.genStartMs) / 1000 : 0;
      if (elapsed > 0.25) updates.output_per_second = record.streamTokens / elapsed;
      record.usage = {...record.usage, ...updates};
    }
  }
}

// The model reports the real prompt-token count only in its final frame, but the
// input is fully known the moment the request is forwarded. Estimate it from the
// request text (~2.7 chars/token for this workload) so the input row shows a
// count during the stream; the real count replaces it when the stream ends.
function estimateInputTokens(request) {
  if (!request || typeof request !== "object") return null;
  let chars = 0;
  if (typeof request.prompt === "string") {
    chars = request.prompt.length;
  } else if (Array.isArray(request.messages)) {
    for (const message of request.messages) {
      const content = message?.content;
      chars += typeof content === "string"
        ? content.length
        : content ? JSON.stringify(content).length : 0;
    }
  }
  return chars > 0 ? Math.max(1, Math.round(chars / 2.7)) : null;
}

function applyLiveInputEstimate(record) {
  if (record.usageIsReal) return;
  const estimate = estimateInputTokens(record.request);
  if (estimate != null) record.usage = {...record.usage, input_tokens: estimate};
}

function liveId(liveNum) {
  return `live-${liveNum}`;
}

function isLiveSelected(liveNum) {
  return state.selected?.type === "call"
    && String(state.selected.id) === liveId(liveNum);
}

// A stored call's request produces exactly one timeline call. An in-flight stream
// is the same shape — its input is fully known (the forwarded request), its
// output arrives live — so it becomes a synthetic timeline item that flows
// through the normal Timeline/selection/pane renderers.
function liveTimelineItem(record) {
  return {
    type: "call",
    id: liveId(record.live_id),
    live: true,
    live_id: record.live_id,
    call_id: record.call_id,
    status: record.status || "streaming",
    created_at: new Date(record.started_ms || Date.now()).toISOString(),
    session_id: record.session || state.session,
    branch_id: "main",
    debug_label: record.label || null,
    title: record.title || null,
    usage: record.usage || null,
    req_id: record.req_id || null,
    request_id: record.request_id || record.req_id || null,
  };
}

// A stable, mutated-in-place synthetic detail (no `diff` → isCheckpoint()==true →
// the normal renderers show it as a plain current state). Shared by reference
// with state.detail, so delta/end just update its fields.
function liveDetailFor(record) {
  if (!record.detail) {
    record.detail = {
      id: liveId(record.live_id),
      live: true,
      live_id: record.live_id,
      created_at: new Date(record.started_ms || Date.now()).toISOString(),
      session_id: record.session || state.session,
      parent_source: "live",
      request_state_id: null,
      parent_state_id: null,
      similarity: null,
      metadata: {
        ...(record.request_id ? {request_id: record.request_id} : {}),
        ...(record.title ? {title: record.title} : {}),
        ...(record.label ? {debug_label: record.label} : {}),
        ...(record.usage ? {usage: record.usage} : {}),
      },
    };
  }
  record.detail.request = record.request || {};
  record.detail.response = record.output || "";
  record.detail.thoughts = record.thoughts || "";
  record.detail.status = record.status || "streaming";
  if (record.title) record.detail.metadata.title = record.title;
  if (record.usage) record.detail.metadata.usage = record.usage;
  return record.detail;
}

function upsertLiveTimelineItem(record) {
  const item = liveTimelineItem(record);
  const key = itemKey(item);
  if (record.call_id != null) {
    // A poll may have seen start_call's durable running row just before the SSE
    // start event. It is this same request, so replace it with the richer live
    // representation rather than showing two timeline calls.
    state.timelineItems = state.timelineItems.filter(existing => (
      existing.live || Number(existing.id) !== Number(record.call_id)
    ));
  }
  const index = state.timelineItems.findIndex(existing => itemKey(existing) === key);
  if (index >= 0) state.timelineItems[index] = item;
  else state.timelineItems.push(item);
}

// Re-render the Timeline (01) and Updates (04) panes from the current items so a
// live call shows across both, not only in Exact.
function renderLiveTimelinePanes() {
  const timelineViewport = captureTimelineViewport();
  renderTimelineEvents(state.timelineItems);
  restoreTimelineViewport(timelineViewport);
  renderUpdateCards(state.timelineItems);
}

function selectNewLiveItem(record) {
  // Follow off means the current inspection is pinned. The live item should be
  // visible in Timeline and Updates, but it must not replace the selected call
  // (which would rebuild and scroll Mixed/Exact). An empty viewer still opens
  // its first request so it does not remain on the empty-state placeholders.
  if (!state.followNewItems && state.selected) return null;
  if (state.followNewItems) return followLatestTimelineEvent(false);
  return selectItem(
    "call",
    liveId(record.live_id),
    null,
    false,
    false,
    "live",
  );
}

// A delta only touches the DOM when its own live call is the current selection;
// otherwise it just accumulates so the output is ready when you click its item.
// Deltas can arrive far faster than the screen refreshes. Coalesce their DOM
// work into one animation-frame flush: patch the selected call's live output at
// most once per frame, and rebuild the Timeline/Updates panes at most a few times
// a second (they only show usage counts, which need not track every token).
// Without this, a fast stream runs a full pane rebuild per delta and freezes.
let liveFlushScheduled = false;
let liveFlushPanesDirty = false;
let liveLastPanesRenderMs = 0;
function flushLiveUpdates() {
  liveFlushScheduled = false;
  // Patch whichever live call is currently selected — its output was just fed.
  const id = state.selected?.type === "call" ? String(state.selected.id) : "";
  if (id.startsWith("live-")) {
    const record = state.live.get(Number(id.slice(5)));
    if (record) patchLiveOutput(record);
  }
  if (liveFlushPanesDirty && performance.now() - liveLastPanesRenderMs > 250) {
    liveFlushPanesDirty = false;
    liveLastPanesRenderMs = performance.now();
    renderLiveTimelinePanes();
  }
}
function scheduleLiveFlush(panesDirty) {
  if (panesDirty) liveFlushPanesDirty = true;
  if (liveFlushScheduled) return;
  liveFlushScheduled = true;
  requestAnimationFrame(flushLiveUpdates);
}

function patchLiveOutput(record) {
  if (!isLiveSelected(record.live_id)) return;
  const outBody = $("exact").querySelector('[data-live-body="output"]');
  const thoughtBody = $("exact").querySelector('[data-live-body="thoughts"]');
  // A thoughts scope that only appeared after the first render needs a full
  // rebuild; otherwise patch the text nodes in place to preserve input scroll.
  if (!outBody || (record.thoughts && !thoughtBody)) {
    renderExact();
    return;
  }
  outBody.textContent = record.output || "…";
  outBody.scrollTop = outBody.scrollHeight;
  if (thoughtBody) thoughtBody.textContent = record.thoughts || "";
}

function startLiveStream() {
  if (state.liveSource) state.liveSource.close();
  state.live.clear();
  const sessionQuery = state.session
    ? `?session=${encodeURIComponent(state.session)}`
    : "";
  const source = new EventSource(`/api/live${sessionQuery}`);
  state.liveSource = source;
  const begin = event => {
    const incoming = JSON.parse(event.data);
    // A brand-new trace has no stored session for /api/sessions to return yet.
    // Adopt the live request's session immediately so the first durable-session
    // refresh does not treat it as a session switch and clear the live timeline.
    if (!state.session && incoming.session) state.session = incoming.session;
    const isNew = !state.live.has(incoming.live_id);
    const existing = state.live.get(incoming.live_id)
      || { output: "", outputText: "", thoughts: "", buffer: "", toolCalls: {} };
    const hadTimelineOutput = existing.status && existing.status !== "running";
    const record = { ...existing, ...incoming };
    // A catch-up snapshot carries the raw bytes so far; parse them once so a
    // viewer that joined mid-stream still sees the readable text.
    if (incoming.text && !existing.output && !existing.thoughts) {
      record.output = "";
      record.outputText = "";
      record.thoughts = "";
      record.buffer = "";
      record.toolCalls = {};
      feedLive(record, incoming.text);
    }
    if (incoming.ended_at_ms) record.endedAt = incoming.ended_at_ms;
    if (existing.detail) record.detail = existing.detail;  // keep the shared ref
    applyLiveInputEstimate(record);
    state.live.set(incoming.live_id, record);
    upsertLiveTimelineItem(record);
    liveDetailFor(record);
    renderLiveTimelinePanes();
    renderWaitingCalls();
    if (isNew) {
      // Follow may open the forwarded input; otherwise preserve the call the
      // user is already inspecting.
      selectNewLiveItem(record);
    } else if (
      state.followNewItems
      && !hadTimelineOutput
      && record.status !== "running"
    ) {
      // Response start adds a new output item to an existing live call.
      followLatestTimelineEvent(false);
    }
  };
  source.addEventListener("snapshot", begin);
  source.addEventListener("start", begin);
  source.addEventListener("update", begin);
  source.addEventListener("title", event => {
    const update = JSON.parse(event.data);
    let changed = false;
    for (const item of state.timelineItems) {
      if (item.req_id !== update.req_id) continue;
      item.title = update.title;
      changed = true;
    }
    if (state.detail?.req_id === update.req_id) {
      state.detail.metadata ||= {};
      state.detail.metadata.title = update.title;
    }
    if (!changed) return;
    const viewport = captureTimelineViewport();
    renderTimelineEvents(state.timelineItems);
    restoreTimelineViewport(viewport);
  });
  source.addEventListener("delta", event => {
    const { live_id, text } = JSON.parse(event.data);
    const record = state.live.get(live_id);
    if (!record) return;
    const previousUsage = JSON.stringify(record.usage || {});
    feedLive(record, text);
    liveDetailFor(record);
    const usageChanged = JSON.stringify(record.usage || {}) !== previousUsage;
    // The timeline item carries the usage the token counts are rendered from, but
    // a delta only updates the live record. Refresh the item so the streaming
    // output row shows the running token count, not a stale/empty one.
    if (usageChanged) upsertLiveTimelineItem(record);
    scheduleLiveFlush(usageChanged);
  });
  source.addEventListener("end", event => {
    const { live_id, status } = JSON.parse(event.data);
    const record = state.live.get(live_id);
    if (!record) return;
    record.status = status;
    record.endedAt = Date.now();
    liveDetailFor(record);
    upsertLiveTimelineItem(record);  // streaming item becomes finished
    renderLiveTimelinePanes();
    if (isLiveSelected(live_id)) renderExact();
    renderWaitingCalls();
  });
  source.addEventListener("stored", event => {
    const { live_id, call_id } = JSON.parse(event.data);
    reconcileStoredLive(live_id, call_id);
  });
  // EventSource reconnects on its own after a drop; nothing to do on error.
}

// The server commits the durable call and tells us its exact id. Reconcile the
// synthetic live call to it. loadTimeline() swaps it via a request-id heuristic,
// but that misses when the call sent no request id (e.g. FIT_TO_SCHEMA) — the
// synthetic item then lingers and its stale, often empty-output DOM stays on
// screen even though the real call is now fetchable. So finish the swap here
// using the definitive id: drop the synthetic, drop any stale cached detail for
// the durable id, and move a selection that was watching it onto the real call.
async function reconcileStoredLive(live_id, call_id) {
  const record = state.live.get(live_id);
  if (!record) return;
  record.persisted = true;
  if (call_id != null) record.call_id = call_id;
  const wasSelected = isLiveSelected(live_id);
  // A poll may have cached a placeholder for the durable id before its output
  // committed; drop it so the reconcile re-fetches the real, complete detail.
  if (call_id != null) state.details.delete(`call:${call_id}`);
  await loadTimeline();
  if (call_id == null) return;
  // loadTimeline's request-id heuristic may drop the synthetic without landing
  // selection on the right stored id (or leave it lingering). Finish the swap
  // authoritatively with the server-provided id: land a watcher on the real call
  // and clear any synthetic that its heuristic left behind.
  if (wasSelected && String(state.selected?.id) !== String(call_id)) {
    await selectItem("call", call_id, null, false, false);
  }
  if (state.live.has(live_id)) dropLiveStream(live_id);
}

// Remove a synthetic live call (and its timeline item), moving any selection off
// it first. With staleOnly, only drop it if it already ended.
function dropLiveStream(liveNum, { staleOnly = false } = {}) {
  const record = state.live.get(liveNum);
  if (!record) return;
  if (staleOnly && !record.endedAt) return;
  state.live.delete(liveNum);
  const key = `call:${liveId(liveNum)}`;
  state.timelineItems = state.timelineItems.filter(item => itemKey(item) !== key);
  state.details.delete(key);
  if (isLiveSelected(liveNum)) {
    state.selected = null;
    state.detail = null;
  }
  renderLiveTimelinePanes();
  renderWaitingCalls();
}

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
  startLiveStream();
  setInterval(liveTick, 1000);
}

start().catch(error => {
  $("mixed").textContent = `Failed to load trace: ${error.message}`;
});
