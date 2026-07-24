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
  liveBusy: false,
  followedUpdateKey: null,
  followedUpdateTimer: null,
  followNewItems: false,
  updatesUserScrollVersion: 0,
  selectionVersion: 0,
  selectedPhase: "input",
  checkpointKeys: new Set(),
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

function responseValue(detail) {
  try {
    return JSON.parse(detail.response);
  } catch {
    return detail.response;
  }
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

function stateDisplayParts(detail) {
  const parameters = requestParameters(detail.request);
  const topParameterText = Object.keys(parameters).length
    ? yaml({ input_params: parameters })
    : "input_params: {}";
  const parameterText = Object.keys(parameters).length
    ? yaml({ input_params: parameters }, 2)
    : "  input_params: {}";
  return {
    parameters,
    topParameterText,
    parameterText,
    contentText: yaml(requestContent(detail.request), 2),
    outputText: yaml({ output: responseValue(detail) }),
  };
}

function stateScopeHtml(kind, html, nested = false) {
  const labels = {
    input: "Input",
    "input-params": "Input parameters",
    output: "Output",
  };
  const sourceLabels = {
    input: "input",
    "input-params": "input_params",
    output: "output",
  };
  const sourceLabel = sourceLabels[kind];
  const labelPattern = new RegExp(`^\\s*${sourceLabel}:(?: |\\n)?`);
  const content = String(html).replace(labelPattern, "");
  return `<span class="state-scope ${nested ? "state-subscope " : ""}trace-kind-${kind}" data-state-scope="${kind}" role="button" tabindex="0" aria-label="${labels[kind]} scope"><span class="state-scope-label" aria-hidden="true">${labels[kind]}</span><span class="state-scope-content">${content}</span></span>`;
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
  return category === "parameter" ? "input-params" : category || "content";
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

function updateEntries(detail) {
  const diff = detail.diff || {};
  // Parameters are the request controls, so they lead the update block.
  const entries = collectParameterUpdates(diff.parameters);
  for (const change of diff.messages || []) {
    if (change.op === "=") continue;
    const oldMessages = change.old_messages || [];
    const newMessages = change.new_messages || change.messages || [];
    if (change.op === "-") {
      for (const message of oldMessages) {
        entries.push({
          label: `Removed input · ${message.role || "message"}`,
          text: markedValue("−", message),
          oldText: yaml(message),
          newText: "",
          mixedOldText: yaml(message),
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
    for (const message of newMessages) {
      entries.push({
        label: `Added input · ${message.role || "message"}`,
        text: markedValue("+", message),
        oldText: "",
        newText: yaml(message),
        needle: firstSearchableValue(message.content),
        needles: searchableLines(message.content),
        scope: "input",
        category: "input",
        operation: "+",
      });
    }
  }
  const promptHunks = diff.prompt?.hunks || [];
  for (let hunkIndex = 0; hunkIndex < promptHunks.length; hunkIndex += 1) {
    const hunk = promptHunks[hunkIndex];
    const hasAdded = Object.hasOwn(hunk, "+");
    const hasRemoved = Object.hasOwn(hunk, "-");
    if (!hasAdded && !hasRemoved) continue;
    const separator = promptHunks[hunkIndex + 1];
    const following = promptHunks[hunkIndex + 2];
    const separatorLines = Number.parseInt(separator?.["="], 10);
    const nearbyReplacement = hasRemoved
      && !hasAdded
      && separatorLines <= 1
      && Object.hasOwn(following || {}, "+")
      && !Object.hasOwn(following || {}, "-");
    if (nearbyReplacement) {
      const removed = hunk["-"]?.preview ?? "";
      const added = following["+"] ?? "";
      entries.push({
        label: "Changed prompt",
        text: transitionText({ op: "~", old: removed, new: added }),
        oldText: removed,
        newText: added,
        mixedOldText: removed,
        needle: firstUsefulLine(added),
        needles: searchableLines(added),
        scope: "input",
        category: "input",
        operation: "~",
      });
      hunkIndex += 2;
      continue;
    }
    const operation = hasAdded && hasRemoved ? "~" : hasAdded ? "+" : "-";
    const removed = hunk["-"]?.preview ?? "";
    const added = hunk["+"] ?? "";
    entries.push({
      label: `${operationName(operation)} prompt`,
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
  const outputDiff = detail.output_diff || { mode: "snapshot" };
  if (detail.response && outputDiff.mode === "diff" && outputDiff.changes?.length) {
    const fragments = outputDiff.changes.filter(
      fragment => fragment.old || fragment.new,
    );
    entries.push({
      label: `Changed output · from call #${outputDiff.base_call_id}`,
      text: "",
      oldText: "",
      newText: "",
      fragments,
      needles: fragments.flatMap(fragment => (
        fragment.new
          .split("\n")
          .map(line => line.trim())
          .filter(Boolean)
      )),
      needle: "",
      scope: "output",
      category: "output",
      operation: "~",
    });
  } else if (detail.response && outputDiff.mode !== "unchanged") {
    const output = responseValue(detail);
    entries.push({
      label: "Added output",
      text: markedValue("+", output),
      oldText: "",
      newText: yaml(output),
      needle: firstSearchableValue(output),
      wholeOutput: true,
      scope: "output",
      category: "output",
      operation: "+",
    });
  }
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
  if (entry.wholeOutput) {
    const boundary = text.startsWith("output:") ? 0 : text.indexOf("\noutput:");
    if (boundary < 0) return null;
    const labelStart = boundary + (boundary ? 1 : 0);
    const valueStart = labelStart + "output:".length
      + (text[labelStart + "output:".length] === " " ? 1 : 0);
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

function entryRanges(text, entry) {
  if (!entry.needles?.length) {
    const range = entryRange(text, entry);
    return range ? [range] : [];
  }
  const boundary = text.indexOf("\noutput:");
  const scopeStart = entry.scope === "output" && boundary >= 0 ? boundary : 0;
  const scopeEnd = entry.scope === "input" && boundary >= 0 ? boundary : text.length;
  const ranges = [];
  let cursor = scopeStart;
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

function updateEntryHtml(entry) {
  const category = entry.category || "content";
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
    return `<div class="change-pair">${
      changePartHtml("removed", entry.oldText || "", category)
    }<span class="change-arrow"> → </span>${
      changePartHtml("added", entry.newText || "", category)
    }</div>`;
  }
  const kind = entry.operation === "-" ? "removed" : "added";
  const text = entry.operation === "-"
    ? entry.oldText || entry.text
    : entry.newText || entry.text;
  return `<div class="single-change">${changePartHtml(kind, text, category)}</div>`;
}

function unchangedOutputNoticeHtml(detail) {
  if (detail.output_diff?.mode !== "unchanged") return "";
  const baseCallId = detail.output_diff.base_call_id;
  return `
    <div class="update-unchanged-output trace-kind-output" data-update-scope="output" role="status">
      <strong>Unchanged output</strong>
      <small>${baseCallId == null
        ? "Same output as its comparison call"
        : `Same output as call #${escapeHtml(baseCallId)}`}</small>
    </div>`;
}

function mixedStateHtml(text, entries) {
  const boundary = text.indexOf("\noutput:");
  const changes = [];
  for (const entry of entries) {
    if (entry.operation === "-") {
      let position = -1;
      if (entry.anchorNeedle) position = text.indexOf(entry.anchorNeedle);
      if (position < 0) {
        position = entry.scope === "input" && boundary >= 0 ? boundary : text.length;
      }
      changes.push({ start: position, end: position, entry });
      continue;
    }
    const ranges = entryRanges(text, entry);
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
  changes.sort((left, right) => left.start - right.start || left.end - right.end);
  let html = "";
  let cursor = 0;
  for (const change of changes) {
    if (change.start < cursor) continue;
    const { entry } = change;
    const category = entry.category || "content";
    html += escapeHtml(text.slice(cursor, change.start));
    const fragmentAttribute = entry.fragmentIndex == null
      ? ""
      : ` data-output-fragment="${entry.fragmentIndex}"`;
    const entryAttribute =
      `data-update-entry="${entry.entryKey}"${fragmentAttribute} role="button" tabindex="0"`;
    if (change.historical) {
      const oldText = entry.oldText || entry.mixedOldText || "";
      const newText = entry.newText || entry.text || "";
      html += "\n";
      if (entry.operation === "~" && oldText) {
        html += changePartHtml("removed", oldText, category, false, entryAttribute);
        html += '<span class="change-arrow inline-arrow"> → </span>';
      }
      if (entry.operation !== "-" && newText) {
        html += changePartHtml(
          entry.fromEarlierCall ? "removed" : "added",
          newText,
          category,
          false,
          entryAttribute,
        );
      }
      html += "\n";
    } else if (entry.operation === "-") {
      const removed = entry.mixedOldText || entry.oldText || entry.text;
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
          entry.mixedOldText,
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

function appendUpdateCard(item) {
  ensureObserver();
  const key = itemKey(item);
  const card = document.createElement("article");
  card.className = "update-card loading";
  card.dataset.key = key;
  card.dataset.type = item.type;
  card.dataset.id = item.id;
  card.innerHTML = `<div class="update-card-head">LLM call #${item.id}</div>`;
  $("updates").appendChild(card);
  state.observer.observe(card);
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

function renderTimelineEvents(items) {
  const events = [];
  for (const item of items) {
    const startedAt = Date.parse(item.created_at);
    events.push({ item, phase: "input", at: startedAt, sortOrder: 3 });
    if (item.status !== "running") {
      const duration = Number(item.duration_ms);
      const completedAt = Number.isFinite(startedAt) && Number.isFinite(duration)
        ? startedAt + Math.max(duration, 0)
        : startedAt + 0.001;
      events.push({ item, phase: "output", at: completedAt, sortOrder: 0 });
    }
  }
  for (const [index, block] of timelineCallBlocks(items).entries()) {
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
  events.sort((left, right) => (
    left.at - right.at
    || left.sortOrder - right.sortOrder
    || (left.item?.sequence || 0) - (right.item?.sequence || 0)
  ));

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
      <span class="item-label">${
        phase === "input"
          ? checkpoint ? "→ new state input" : "→ input"
          : "← output"
      }</span>
      <span class="item-meta">
        <span>#${item.id} · <b class="branch">${escapeHtml(item.branch_id || "main")}</b></span>
        <span>${escapeHtml(phase === "input" ? "sent" : item.status)}</span>
      </span>`;
    button.classList.toggle(
      "active",
      state.selected
        && itemKey(state.selected) === key
        && state.selectedPhase === phase,
    );
    button.onclick = () => selectItem(item.type, item.id, button);
    $("timeline").appendChild(button);
  }
  applyBranchIndentation();
}

function captureTimelineViewport() {
  const container = $("timeline");
  const containerRect = container.getBoundingClientRect();
  const visibleItem = [...container.querySelectorAll(".timeline-item")].find(node => {
    const rect = node.getBoundingClientRect();
    return rect.bottom > containerRect.top && rect.top < containerRect.bottom;
  });
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

function restoreTimelineViewport(anchor) {
  const container = $("timeline");
  if (anchor.nearBottom) {
    container.scrollTop = container.scrollHeight;
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

function renderWaitingCalls(items) {
  const box = $("waiting-calls");
  const waiting = items.filter(item => item.type === "call" && item.status === "running");
  box.innerHTML = `
    <div class="waiting-calls-head">
      <span>${waiting.length} waiting / running</span>
    </div>`;
}

function applyBranchIndentation() {
  for (const currentBlock of timelineCallBlocks(state.timelineItems)) {
    const branchLanes = new Map();
    const parallel = currentBlock.calls.length > 1;
    for (const { item } of currentBlock.calls) {
      const branch = item.branch_id || "main";
      if (!branchLanes.has(branch)) {
        const usedLanes = new Set(branchLanes.values());
        let availableLane = 0;
        while (usedLanes.has(availableLane)) availableLane += 1;
        branchLanes.set(branch, availableLane);
      }
      const lane = branchLanes.get(branch);
      const buttons = [
        document.querySelector(`.timeline-input[data-key="${itemKey(item)}"]`),
        document.querySelector(`.timeline-output[data-call-key="${itemKey(item)}"]`),
      ].filter(Boolean);
      if (!buttons.length) continue;
      const branchRoot = item.branch_root_id
        || branch.split("~parallel-", 1)[0]
        || "main";
      const visibleBranch = branch === branchRoot || lane === 0
        ? branchRoot
        : `${branchRoot} · p${lane + 1}`;
      buttons.forEach(button => {
        const branchColor = `hsl(${145 + lane * 31}, 36%, 42%)`;
        button.style.setProperty("--branch-lane", lane);
        button.style.setProperty("--branch-depth", parallel ? lane + 1 : 0);
        button.style.setProperty("--branch-color", branchColor);
        button.dataset.branchLane = String(lane);
        button.dataset.branchDepth = String(parallel ? lane + 1 : 0);
        button.classList.toggle("parallel-block", parallel);
        button.classList.toggle("parallel-branch", lane > 0);
        const branchLabel = button.querySelector(".branch");
        if (branchLabel) branchLabel.textContent = visibleBranch;
        button.title = parallel
          ? `Stored branch ${branch} · parallel lane ${lane + 1}`
          : `Stored branch ${branch}`;
      });
    }
  }
}

function keepFollowedUpdateVisible() {
  if (!state.followedUpdateKey) return;
  const card = document.querySelector(
    `.update-card[data-key="${state.followedUpdateKey}"]`,
  );
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

function focusMixedFragments(indices, entry) {
  $("mixed").querySelectorAll(".fragment-focus").forEach(node => {
    node.classList.remove("fragment-focus", "flash");
  });
  const targets = indices.flatMap(index => (
    [...$("mixed").querySelectorAll(
      `[data-update-entry="${entry.entryKey}"][data-output-fragment="${index}"]`,
    )]
  ));
  for (const target of targets) {
    target.classList.add("fragment-focus", "flash");
  }
  focusScrollIntoView(targets[0]);
}

function focusMixedEntry(entry) {
  if (entry.fragments) {
    focusMixedFragments(entry.fragments.map((_, index) => index), entry);
    return;
  }
  $("mixed").querySelectorAll(".fragment-focus").forEach(node => {
    node.classList.remove("fragment-focus", "flash");
  });
  const targets = [
    ...$("mixed").querySelectorAll(`[data-update-entry="${entry.entryKey}"]`),
  ];
  for (const target of targets) {
    target.classList.add("fragment-focus", "flash");
  }
  focusScrollIntoView(targets[0]);
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
      card.innerHTML = `<div class="update-card-head">LLM call #${escapeHtml(card.dataset.id)}</div>`;
      loadUpdateCard(card);
    });
    return;
  }
  delete card.dataset.loading;
  card.dataset.loaded = "true";
  card.classList.remove("load-error");
  const checkpoint = isCheckpoint(detail);
  const identicalTo = identicalBaseCall(detail);
  const entries = updateEntries(detail);
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
    card.innerHTML = `
      <button class="checkpoint-jump">
        <strong>◆ New current state</strong>
        <span>LLM call #${detail.id}</span>
      </button>
      <div class="checkpoint-state">
        <section class="checkpoint-section checkpoint-input trace-kind-input" role="button" tabindex="0" data-checkpoint-scope="input">
          <strong>Input</strong>
          <pre>${escapeHtml(yaml(input))}</pre>
        </section>
        ${Object.keys(parameters).length ? `
          <section class="checkpoint-section checkpoint-parameters trace-kind-input-params" role="button" tabindex="0" data-checkpoint-scope="input-params">
            <strong>Parameters</strong>
            <pre>${escapeHtml(yaml(parameters))}</pre>
          </section>` : ""}
        <section class="checkpoint-section checkpoint-output trace-kind-output" role="button" tabindex="0" data-checkpoint-scope="output">
          <strong>Output</strong>
          <pre>${escapeHtml(yaml(output))}</pre>
        </section>
      </div>`;
    const openScope = async scope => {
      if (hasTextSelectionWithin(card)) return;
      await selectItem("call", detail.id, null, true);
      focusCheckpointScope(scope);
      card.querySelectorAll(".checkpoint-section.active").forEach(node => {
        node.classList.remove("active");
      });
      card.querySelector(`[data-checkpoint-scope="${scope}"]`)?.classList.add("active");
    };
    card.querySelector("button").onclick = () => openScope("input");
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
  if (identicalTo) {
    card.classList.add("identical");
    card.innerHTML = `
      <button class="identical-jump" data-update-scope="output">
        <strong>↻ Identical call</strong>
        <span>LLM call #${detail.id} = call #${identicalTo}</span>
        <small>No input, parameter, or output changes</small>
      </button>`;
    card.querySelector("button").onclick = () => selectItem("call", detail.id, null, true);
    keepFollowedUpdateVisible();
    return;
  }
  card.innerHTML = `
    <div class="update-card-head">Δ LLM call #${detail.id}</div>
    <div class="update-card-body">
      ${entries.map((entry, index) => `
        <div class="update-jump ${escapeHtml(entry.category || "content")}-update-card trace-kind-${traceKind(entry.category)} trace-op-${traceOperation(entry.operation)} op-${escapeHtml(entry.operation || "change")}" data-update-index="${index}" role="button" tabindex="0">
          <strong>${escapeHtml(entry.label)}</strong>
          ${updateEntryHtml(entry)}
        </div>`).join("") || '<div class="no-update">No textual update</div>'}
      ${unchangedOutputNoticeHtml(detail)}
    </div>`;
  card.querySelectorAll(".update-jump").forEach(button => {
    const openUpdate = async () => {
      if (hasTextSelectionWithin(button)) return;
      const entry = entries[Number(button.dataset.updateIndex)];
      await selectItem("call", detail.id, null, true);
      activateTab("state");
      renderExact(entry);
      focusMixedEntry(entry);
    };
    button.onclick = openUpdate;
    button.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openUpdate();
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
      await selectItem("call", detail.id, null, true);
      activateTab("state");
      renderExact(focusedEntry);
      focusMixedFragments(indices, entry);
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
    }
    return selectionIsCurrent();
  }
  const selectedIndex = state.timelineItems.findIndex(
    item => item.type === type && Number(item.id) === Number(id),
  );
  if (selectedIndex < 0) {
    if (selectionIsCurrent()) {
      state.mixedSegmentDetails = selectedDetail ? [selectedDetail] : [];
    }
    return selectionIsCurrent();
  }
  const details = [];
  for (let index = selectedIndex; index >= 0; index -= 1) {
    if (!selectionIsCurrent()) return false;
    const item = state.timelineItems[index];
    if (item.type !== "call") continue;
    const detail = Number(item.id) === Number(id)
      ? selectedDetail
      : await detailFor(item.type, item.id);
    if (!selectionIsCurrent()) return false;
    details.unshift(detail);
    if (isCheckpoint(detail)) break;
  }
  if (!selectionIsCurrent()) return false;
  state.mixedSegmentDetails = details;
  return true;
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
  const entries = checkpoint
    ? []
    : [
        ...selectedEntries,
        ...segment.slice(0, -1).flatMap(
          detail => isCheckpoint(detail)
            ? []
            : updateEntries(detail).map(entry => ({
                ...entry,
                fromEarlierCall: true,
              })),
        ),
      ];
  const parts = stateDisplayParts(state.detail);
  const parameterEntries = entries.filter(entry => entry.category === "parameter");
  const inputEntries = entries.filter(
    entry => entry.scope === "input" && entry.category !== "parameter",
  );
  const outputEntries = mixedOutputEntries(
    entries.filter(entry => entry.scope === "output"),
  );
  const parameterHtml = mixedStateHtml(parts.parameterText, parameterEntries);
  const contentHtml = mixedStateHtml(parts.contentText, inputEntries);
  const inputHtml = stateScopeHtml(
    "input",
    `input:\n${stateScopeHtml("input-params", parameterHtml, true)}\n${contentHtml}`,
  );
  const outputHtml = stateScopeHtml(
    "output",
    checkpoint
      ? escapeHtml(parts.outputText)
      : mixedStateHtml(parts.outputText, outputEntries),
  );
  $("mixed-status").textContent = checkpoint
    ? "◆ new current state"
    : identicalTo
      ? `↻ identical to call #${identicalTo}`
      : `Δ ${entries.length} accumulated update${entries.length === 1 ? "" : "s"}`;
  $("mixed-status").className = checkpoint ? "mixed-legend checkpoint" : "mixed-legend delta";
  $("mixed").classList.remove("empty");
  $("mixed").innerHTML = `${inputHtml}\n${outputHtml}`;
  $("mixed").scrollTop = preserveScroll ? previousScroll : 0;
}

function activateTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs button").forEach(button => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
}

function exactUpdateRanges(text, entries) {
  const updates = [];
  for (const entry of entries) {
    if (entry.operation === "-") continue;
    if (entry.fragments) {
      let searchFrom = 0;
      entry.fragments.forEach((fragment, fragmentIndex) => {
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
    for (const [start, end] of entryRanges(text, entry)) {
      updates.push({ start, end, entry, fragmentIndex: null });
    }
  }
  return updates.sort((left, right) => left.start - right.start || left.end - right.end);
}

function exactStateHtml(text, entries, focusEntry = null) {
  const ranges = exactUpdateRanges(text, entries);
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
    const contentHtml = exactStateHtml(
      parts.contentText,
      entries.filter(entry => entry.scope === "input" && entry.category !== "parameter"),
      focusEntry,
    );
    const outputHtml = exactStateHtml(
      parts.outputText,
      entries.filter(entry => entry.scope === "output"),
      focusEntry,
    );
    $("exact").innerHTML = `${
      stateScopeHtml("input-params", parameterHtml)
    }\n${
      stateScopeHtml("input", `input:\n${contentHtml}`)
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

function focusTimelineSelection(detail, preferredScope = "input") {
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
  const scopedEntries = entries.filter(entry => entry.scope === preferredScope);
  const scopedEntryKeys = new Set(scopedEntries.map(entry => entry.entryKey));
  const primaryEntry = scopedEntries[0] || null;
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
  const entryIndex = entryKey?.split(":").at(-1);
  const update = entryIndex == null
    ? null
    : card.querySelector(`.update-jump[data-update-index="${entryIndex}"]`);
  const checkpointTarget = card.classList.contains("checkpoint")
    ? card.querySelector(`[data-checkpoint-scope="${preferredScope}"]`)
    : null;
  const unchangedOutputTarget = preferredScope === "output"
    ? card.querySelector('[data-update-scope="output"]')
    : null;
  const target = update || checkpointTarget || unchangedOutputTarget || card;
  // Force a fresh animation frame so a second click on the same event pulses
  // the corresponding update again.
  void target.offsetWidth;
  target.classList.add("timeline-update-focus", "timeline-update-flash");
  focusScrollIntoView(
    target,
    update || checkpointTarget || unchangedOutputTarget ? "center" : "start",
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

async function focusUpdateFromState(element) {
  const updateElement = element.closest("[data-update-entry]");
  if (updateElement) {
    const [rawCallId, rawEntryIndex] = updateElement.dataset.updateEntry.split(":");
    const callId = Number(rawCallId);
    const entryIndex = Number(rawEntryIndex);
    const key = `call:${callId}`;
    const card = document.querySelector(`.update-card[data-key="${key}"]`);
    if (!card) return;
    await loadUpdateCard(card);
    document.querySelectorAll(".timeline-item.active").forEach(node => {
      node.classList.remove("active");
    });
    document.querySelector(`.timeline-item[data-key="${key}"]`)?.classList.add("active");
    document.querySelectorAll(".update-card.active").forEach(node => {
      node.classList.remove("active");
    });
    const button = card.querySelector(`.update-jump[data-update-index="${entryIndex}"]`);
    if (!button) return;
    const fragmentIndex = updateElement.dataset.outputFragment == null
      ? null
      : Number(updateElement.dataset.outputFragment);
    const fragment = updateFragmentForIndex(button, fragmentIndex);
    markUpdateTarget(card, fragment || button, button);
    return;
  }

  if (!state.selected) return;
  const key = itemKey(state.selected);
  const card = document.querySelector(`.update-card[data-key="${key}"]`);
  if (!card) return;
  await loadUpdateCard(card);
  const scope = element.closest("[data-state-scope]")?.dataset.stateScope;
  if (!scope) return;
  const phase = scope === "output" ? "output" : "input";
  state.selectedPhase = phase;
  document.querySelectorAll(".timeline-item.active").forEach(node => {
    node.classList.remove("active");
  });
  const timelineTarget = phase === "output"
    ? document.querySelector(`.timeline-output[data-call-key="${key}"]`)
    : document.querySelector(`.timeline-input[data-key="${key}"]`);
  timelineTarget?.classList.add("active");

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
    const target = event.target.closest("[data-update-entry], [data-state-scope]");
    if (target) focusUpdateFromState(target);
  });
  pane.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest("[data-update-entry], [data-state-scope]");
    if (!target) return;
    event.preventDefault();
    focusUpdateFromState(target);
  });
}

async function selectItem(
  type,
  id,
  element = null,
  scrollTimeline = true,
  focusSelection = element !== null,
) {
  const button = element || document.querySelector(`.timeline-item[data-key="${type}:${id}"]`);
  const phase = button?.dataset.phase || "input";
  const selectionVersion = ++state.selectionVersion;
  document.querySelectorAll(".timeline-item.active").forEach(node => node.classList.remove("active"));
  button?.classList.add("active");
  if (scrollTimeline && button) focusScrollIntoView(button, "nearest");
  document.querySelectorAll(".update-card.active").forEach(node => node.classList.remove("active"));
  const updateCard = document.querySelector(`.update-card[data-key="${type}:${id}"]`);
  updateCard?.classList.add("active");
  const previousDetail = state.detail;
  state.selected = { type, id };
  state.selectedPhase = phase;
  const detail = await detailFor(type, id);
  if (selectionVersion !== state.selectionVersion) return;
  state.detail = detail;
  if (!await loadMixedSegment(type, id, selectionVersion)) return;
  const score = state.detail.similarity == null
    ? ""
    : ` · ${(state.detail.similarity * 100).toFixed(0)}%`;
  $("lineage").textContent =
    `state S${state.detail.request_state_id} ← ${state.detail.parent_source || "root"}${score}`;
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
  for (const item of items) appendUpdateCard(item);
  renderTimelineEvents(items);
  restoreTimelineViewport(timelineViewport);
  $("updates").scrollTop = updatesScroll;
  if (!items.length) {
    state.selected = null;
    state.detail = null;
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
  const card = document.querySelector(`.update-card[data-key="${key}"]`);
  if (card) {
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
    focusTimelineUpdateCard(card, focusedEntryKey, state.selectedPhase);
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
  for (const item of items) {
    const previous = previousByKey.get(itemKey(item));
    if (previous && previous.status !== item.status) {
      await refreshChangedItem(item);
      Object.assign(previous, item);
    }
  }
  const appended = items.filter(item => !previousByKey.has(itemKey(item)));
  for (const item of appended) appendUpdateCard(item);
  // Preserve the viewport immediately after the synchronous append. Do not
  // restore it after awaited detail loading: the user may scroll meanwhile.
  if (state.updatesUserScrollVersion === updatesUserScrollVersion) {
    updates.scrollTop = updatesScroll;
  }
  state.timelineItems = [...previousItems, ...appended];
  const timelineViewport = captureTimelineViewport();
  renderTimelineEvents(state.timelineItems);
  // Output events can be inserted above the viewport when an earlier running
  // call completes. Keep the same visible event at the same screen position.
  restoreTimelineViewport(timelineViewport);
  state.lastTimelineKey = state.timelineItems.length
    ? itemKey(state.timelineItems[state.timelineItems.length - 1])
    : null;
  if (appended.length && followNewItems) {
    const last = appended[appended.length - 1];
    await selectItem(last.type, last.id, null, false, true);
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
    state.selected = null;
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

async function start() {
  await loadSessions();
  await Promise.all([loadTimeline(), loadStats()]);
  setInterval(liveTick, 1000);
}

start().catch(error => {
  $("mixed").textContent = `Failed to load trace: ${error.message}`;
});
