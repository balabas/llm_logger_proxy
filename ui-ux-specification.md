# Insequent Trace Viewer UI/UX Specification

## 1. Purpose

The trace viewer explains how an LLM call sequence evolves. It must let a user answer four
different questions without confusing them:

1. **When did data enter or leave the trace?**
2. **What is the exact input and output of the selected call?**
3. **What has accumulated since the last state checkpoint?**
4. **Which stored update produced a visible piece of state?**

The four panes deliberately provide different views of the same trace:

| Pane | Primary question | Time model | Mutation model |
| --- | --- | --- | --- |
| Timeline | What happened, and in what order? | Chronological input/output events | Live, preserves viewport |
| Mixed Trace | How did the current segment grow? | From checkpoint through selected call | Grow until checkpoint, then flush |
| Exact State | What did this one selected call contain? | Selected call only | Reconstructed exact value |
| Updates | What changed at each call? | Chronological input/output cards | Append-only per event |

The panes are linked, but they are not interchangeable. Exact State is a current-value
projection. Mixed Trace is a **mixed projection**: selected-call truth combined with retained
segment history and current-presence status. Updates is an append-only provenance journal.

“Mixed” does not mean concatenating requests or showing a conventional before/after diff. It
means preserving every meaningful change in the active checkpoint segment, projecting those
changes into the selected call’s semantic input/output structure, and visibly distinguishing
what is still present from what has been superseded.

## 2. UX approach

### 2.1 Coordinated views rather than one overloaded diff

The viewer uses coordinated panes because no single diff can simultaneously represent exact
state, accumulated history, chronology, and provenance without becoming ambiguous.

- Timeline owns event selection.
- Mixed owns retained segment history.
- Exact owns selected-call truth.
- Updates owns change provenance.

A selection is propagated across panes through stable call and update-entry identities. The
same data is allowed to look different when the pane answers a different question.

### 2.2 The mixing approach

Mixed Trace combines three sources without collapsing their meanings:

1. **The selected exact state** supplies the current input, parameters, and output structure.
2. **The active checkpoint segment** supplies every retained update through the selected call.
3. **Stable update provenance** links each marked fragment to its owning call and Updates
   entry.

The result is a state-shaped historical surface. It reads like the selected request and
response, but contains retained evidence of how that state developed.

The mixing procedure is:

1. Find the checkpoint that begins the selected call’s active segment.
2. Reconstruct the selected call’s exact input, parameters, and output.
3. Collect update entries from the segment start through the selected call.
4. Map surviving update text into its semantic location in the reconstructed state.
5. Keep superseded text near its former or replacement location instead of discarding it.
6. Classify every retained fragment by data kind, operation, and current presence.
7. Attach the original update-entry identity so marked text remains a back-reference.

This creates two simultaneous readings:

- read unmarked and green text to understand the selected current state;
- read red/green transitions to understand how the segment arrived there.

Mixing is deliberately not:

- a concatenation of complete requests;
- a replacement for Exact State;
- a diff only against the immediately preceding call;
- a chronological log pasted into the input;
- permission to alter copied payload text.

When mapping is uncertain, the renderer retains history visibly rather than presenting old
text as current. Exact State remains the authority for what the selected call actually
contains.

### 2.3 Progressive disclosure

The first visible level stays compact:

- timeline events show phase, call ID, branch, and status;
- update cards show concise change labels;
- pane headers show state/lineage summaries.

Full payloads remain available in the code surfaces and expanded update entries. Raw provider
data and metadata are secondary tabs rather than competing with the default I/O view.

### 2.4 Semantic decoration rather than text mutation

Status is expressed through CSS and surrounding labels. This keeps copied prompt/output text
faithful and allows the same fragment to combine several independent meanings:

- output data;
- originally added;
- now superseded;
- currently focused.

These meanings must compose as layers instead of being flattened into one color or prefixed
with operation characters.

### 2.5 Persistent state plus transient confirmation

Every navigation action has two feedback phases:

1. a short pulse confirms that the action happened;
2. a persistent outline or semantic surface shows where focus remains.

This is preferable to animation-only feedback, which disappears, or persistent-only
feedback, which does not visibly replay on a second click.

### 2.6 Local motion

Each pane scrolls independently to its own corresponding target. The interface does not move
the page as a whole on desktop, and one pane must not borrow another pane’s scroll state.
Motion is minimized to the shortest useful transition and cancelled when superseded.

## 3. Non-negotiable behavioral invariants

### 3.1 Mixed Trace grows or flushes

Within one checkpoint segment, Mixed Trace must never lose previously observed data.

- A later call adds information to the segment.
- Superseded text remains visible.
- Text still present in the selected exact value is rendered as present.
- Text no longer present is retained and rendered as removed.
- Only a checkpoint starts a new segment and flushes the prior mixed history.
- Selecting an earlier call reconstructs the segment only up to that call.

“Retained” and “present” are separate properties. Historical text can be retained in Mixed
Trace while being absent from Exact State. In that case it must be red and struck through,
not green.

Removed text is owned by the call that removed it. When a call removes text, that removal
carries the full removed text and links it back to the removing call, so clicking a removed
mark in Mixed lands on a removed side in Updates — removed points to removed, added to added.
An earlier call's addition that is now gone is retained as removed history, but only when no
real removal already covers that exact text; where a removal does cover it, that removal owns
it, and re-drawing the same text linked to the call that *added* it — which would point a
removed mark at an added side — is not done. Nothing is lost either way: the text stays
visible, once, attributed to the call that removed it.

A flat text can carry only one mark per character, so spans compete. Competition is resolved,
never silently dropped:

- the selected call's own change claims its span first — an earlier call's retained span,
  above all a whole-scope one, must never swallow the fragments of the call being looked at
  and leave them with no mark and no focus target;
- whatever loses is kept as retained history rather than discarded;
- a removal is an anchor, not a span: it collides with nothing, and if its position falls
  inside an already-marked span it moves to the end of that span. Removed text cannot vanish
  from Mixed Trace — being marked as removed is what it means to be there.

Mixed Trace must preserve semantic placement. Input history stays in Input, parameter history
stays in Input parameters, and output history stays in Output. The renderer must never move a
fragment into another scope merely because the same text occurs there.

### 3.2 Exact State is selected-call truth

Exact State shows the selected call’s reconstructed values without accumulated historical
content.

- I/O shows input parameters, input content, and output.
- Params shows request controls only.
- Raw shows the original provider response envelope when available.
- Metadata shows lineage and storage metadata.
- Exact State must not silently hide stored prompt protocol tokens.
- Temporary focus marks may decorate exact text, but must not alter copied text.

### 3.2.1 Changes are placed by recorded position

The store records where every change sits: prompt hunks carry their line, message hunks their
index, response fragments their character offsets. Those positions place the fragment in Mixed
and Exact. Searching the state for a change's text cannot tell two identical lines apart, and
cannot place removed text at all — the first match wins and every later twin silently loses
its focus target.

Two cases fall back to content matching, and both are visible in the code:

- **A payload the pane reformats for display.** Recorded offsets index the stored payload;
  when a structured value is re-serialised for reading, they no longer describe what is on
  screen. Placement verifies the payload is rendered verbatim before trusting an offset.
- **An entry replayed from an earlier call in the segment.** Its positions describe that
  call's text, and every later insertion has shifted them, so whether its text is still
  present is a content question. Such an entry must never be placed at its recorded position:
  that position now belongs to whatever was inserted since. Repeated text on this path is
  disambiguated by occurrence so twins do not collapse onto one mark.

### 3.3 Updates is append-only

Each call owns one card per phase — what it sent, and what came back — and the two are placed
independently in the same chronological event sequence the Timeline uses. Overlapping calls
therefore interleave: an earlier call's response lands after a later call's request when that
is the order in which they happened.

Existing cards are preserved and moved, never rebuilt, when live calls arrive. A completing
call inserts its output card into the sequence without disturbing the viewport or the loaded
content of any other card. Lazy loading may fill a placeholder, but must not reorder or
replace unrelated cards.

### 3.4 Visual marks are not payload

Operation and navigation indicators must be implemented with labels, borders, backgrounds,
and CSS. UI-generated `+` and `−` characters must not be inserted into prompt or output
payload text.

Protocol tokens already present in the stored request, such as `<|start|>`, `<|message|>`,
`<<<`, and `>>>`, are real trace data and remain visible.

## 4. Information architecture

### 4.1 Global header

The header contains:

- product identity;
- session selector;
- full-trace search;
- storage/call statistics.

Changing the session resets the selected call and rebuilds all four panes. Search results are
session-scoped and must not mutate the trace.

### 4.2 Timeline

Each completed call produces two timeline events:

- **input** — when the request was sent;
- **output** — when the response completed.

Input and output use different data-kind colors and labels. A click selects both a call and a
phase. Phase controls which corresponding data is focused in the other panes.

Parallel calls are grouped between “parallel start” and “parallel end” dividers. Branch lanes
are indented and receive stable colors within a parallel block. Indentation communicates
concurrency, not semantic parenthood.

Checkpoint inputs use a diamond marker and “new state input” label. Running calls use a
continuous pulse only while waiting.

The Follow toggle owns every movement caused by arrival:

- off: live arrivals neither replace the current selection nor move any pane. A viewport that
  happens to rest at the newest end stays there while new events accumulate below it;
- on: the latest appended call becomes selected and is scrolled into view, even when the
  viewport had drifted away from the newest end.

Opening a session is not an arrival: the first render selects the newest call and opens at
that end whatever Follow says.

### 4.3 Mixed Trace

Mixed Trace is the historical working surface for the active checkpoint segment.

It contains four semantic scopes, with Thoughts omitted when the provider returns none:

1. input parameters;
2. input content;
3. thoughts;
4. output.

All updates from the segment are retained. The renderer resolves their current status against
the selected exact value:

| Historical value | Exists in selected exact value? | Mixed rendering |
| --- | --- | --- |
| Added earlier | Yes | Green/present |
| Added earlier | No | Red/removed, still retained |
| Removed | No | Red/removed |
| Changed | New value survives | Old red, new green |
| Changed | New value later superseded | Old red, new red |

This rule applies equally to input and output. Output history does not disappear merely
because Exact State contains only the selected call’s short response.

#### Mixed Trace reading model

The pane should look like state first and history second:

- structural labels establish Input parameters, Input, Prompt when applicable, Thoughts when
  present, and Output;
- unchanged payload remains quiet monospaced text;
- present additions remain embedded at their current location;
- replacements show an old-to-new transition;
- removed history remains available without pretending to belong to Exact State;
- focus decoration is applied only after kind, operation, and presence are resolved.

Labels such as Input, Output, Input parameters, Prompt, Messages, Content, Role, and Thoughts
are interface structure. They use a separate UI font, surface, and border treatment and must
not resemble or become part of model input/output text. Provider response structure is
rendered semantically rather than exposed as YAML keys.

Thoughts and Output are separate response scopes. Thoughts contains provider reasoning fields
such as `reasoning_content` or `thinking`; Output contains only the assistant answer delivered
to the application. Each scope has independent history, update cards, highlighting, and
back-reference navigation. The Raw tab may still show the unmodified provider envelope.

#### Identity and provenance

Every marked mixed fragment carries the stable identity of the update that produced it.
Repeated text is not sufficient identity: two equal strings introduced by different calls
remain different updates. Grouped output fragments additionally retain their fragment index
so navigation can select one grouped occurrence rather than the entire output change.

The same update identity may have different visual forms across panes:

| Surface | Representation of one update |
| --- | --- |
| Mixed Trace | Retained old/new fragment in state context |
| Exact State | Current surviving fragment only |
| Updates | Complete change entry with provenance details |
| Timeline | Owning call and input/output phase |

Cross-pane focus follows this identity, not text search or visual proximity.

The Mixed status label communicates either:

- checkpoint/new current state;
- identical call;
- accumulated update count.

### 4.4 Exact State

Exact State is the comparison anchor. Its default I/O order is:

1. `input_params`;
2. `input`;
3. `output`.

The output timeline event always focuses output:

- if output changed, focus the mapped changed fragments;
- if output is unchanged, focus and animate the complete output scope;
- if the call is a checkpoint, focus the checkpoint output scope.

This prevents “unchanged relative to a parent” from being misread as “no output exists.”

Exact State participates in mixing only as the current-value anchor. It must not absorb
retained removed text from Mixed Trace. A marked Exact fragment may link to an update, but
plain exact content is still payload, not a navigation control.

### 4.5 Updates

A card is scoped to one phase of one call and is headed by that phase (`→ input`,
`← output`) so a card answers "input or output?" before "which call?". An input card carries
parameter and input entries; an output card carries thoughts and output entries. Neither
phase ever shows the other's updates.

An Updates card can be one of four forms:

- checkpoint snapshot — input and parameters on the input card, thoughts and output on the
  output card;
- identical call — one compact card at the input moment, since the identity statement covers
  the whole call;
- a list of change entries for its phase;
- load error with retry.

Change entries are grouped by data kind and operation. Output token changes may be grouped
when identical fragments repeat; location and repeat-count labels are metadata outside the
payload.

Every prompt entry carries the line the differ recorded for it (`old 117`, `new 174`,
`line 12`). Identical text inserted at two places is two entries, and the label is what tells
them apart. The same holds for the other payloads: a message hunk records the message indexes
it touches, and a response fragment records its character offsets. Entries are never merged on proximity: a removal followed by a nearby insertion
stays two changes, because pairing them would invent a transition between unrelated lines and
give both one identity, so focusing the addition would light up the removal. Only a hunk the
differ itself recorded as a replacement renders as an old-to-new transition.

When output is identical to its comparison call but input changed, the output card includes a
dedicated “Unchanged output” row. This row is the focus target for the output timeline event.

## 5. Style layers

Styles must be composed in the following order. A later layer may emphasize an earlier one,
but must not change its meaning.

### 5.1 Layer 1: structural shell

The shell establishes hierarchy without encoding trace semantics.

- Warm paper background: `--paper`.
- Light pane surfaces: `--panel`.
- Neutral borders: `--line`.
- Dark monospaced data surfaces for Mixed, Exact, and Updates.
- Serif product title and sans-serif controls.
- Monospace for trace values, metadata, identifiers, and status labels.

The desktop layout is a four-column grid. Below 1050 px it becomes one column; every pane
receives a usable minimum height.

### 5.2 Layer 2: data kind

Data-kind color answers **what type of data is this?**

| Kind | Class | Accent role |
| --- | --- | --- |
| Input content | `.trace-kind-input` | Cyan/blue |
| Input parameters | `.trace-kind-input-params` | Violet |
| Thoughts | `.trace-kind-thoughts` | Purple |
| Output | `.trace-kind-output` | Gold/olive |

Each kind defines:

- `--trace-kind-accent`;
- `--trace-kind-surface`;
- `--trace-kind-hover`;
- `--trace-kind-text`.

These variables drive scope borders, card surfaces, focus treatment, and timeline markers.
New components should consume these variables rather than introduce another unrelated color.

### 5.3 Layer 3: operation

Operation styling answers **what happened to the data?**

| Operation | Class | Meaning | Rendering |
| --- | --- | --- | --- |
| Added/present | `.trace-op-added`, `.added-part` | Exists in selected state | Green surface and left inset |
| Removed/superseded | `.trace-op-removed`, `.removed-part` | Retained history, absent now | Red surface, red inset, strike-through |
| Changed | `.trace-op-changed` | Transition with old/new sides | Amber card edge; old red and new green |

Operation color must not be replaced by focus yellow. Green never means “clicked”; it means
present. Red never means “error”; it means removed or superseded trace content.

### 5.4 Layer 4: temporal truth

Temporal styling answers **is retained history still current?**

- Current text uses its operation’s present style.
- Earlier-call text that still maps into Exact State remains present.
- Earlier-call text that no longer maps into Exact State changes to removed styling.
- The DOM/content remains available in Mixed Trace until checkpoint flush.

This layer is computed, not merely inherited from the operation recorded at the original
call. An addition can later become a retained removal.

This computation is the defining visual operation of mixing. Operation describes what
happened when an update was recorded; temporal truth describes whether its value survives in
the selected state. Both must remain available at once.

### 5.5 Layer 5: interaction and navigation

Interaction styling answers **what is the user currently following?**

- Yellow is reserved for navigation focus and active cross-pane linkage.
- Interaction is the last layer, so its decoration must win over kind and operation
  decoration rather than lose to it. Kind styling carries its own outline; a focus outline
  stated earlier or at lower weight silently disappears, and the pane then scrolls to a
  fragment that is not visibly marked.
- Focus decoration applies to any focused fragment, whatever its kind or operation. Retained
  removed text and history parked out of line need it most: they are what the reader has to
  be led to.
- `.timeline-update-focus` is a persistent focused Updates target.
- `.fragment-focus` identifies linked Mixed fragments.
- `.exact-focus` identifies linked Exact fragments.
- `.timeline-scope-focus` focuses a whole semantic scope when no smaller changed fragment
  exists.
- `.checkpoint-pane-focus` focuses a checkpoint scope.
- `.update-back-focus` briefly confirms navigation from Mixed/Exact back to Updates.

Interaction styling must be additive. It may add an outline or pulse, but it must preserve
the underlying kind and operation colors.

Mixed and Exact stack many marked fragments on adjacent wrapped lines. An outline drawn
outside the box, pushed further out by a positive offset, bleeds into the lines above and
below; a dense stack then reads as one overlapping web instead of separate marks. Kind and
operation edges therefore sit inside their own box (non-positive offset), the change panes
carry enough leading to separate stacked marks, and the focus outline stays only slightly
proud — visible over the inset kind edge without merging into its neighbours.

## 6. Selection and cross-pane focus rules

### 6.1 Timeline input click

1. Mark that input event active.
2. Reconstruct the selected call.
3. Build Mixed Trace through the selected call.
4. Render Exact State I/O.
5. Focus all mapped input updates belonging to the selected call.
6. Focus every Updates entry of the input phase, not only the first — a call that changed both
   its parameters and its prompt lights both. The entry's data kind decides its phase, so all
   input and parameter entries are considered, not just the primary one.
7. If no input or parameter update changed, focus and pulse the “Unchanged input” row in
   Updates, never an output update and never the card as a whole.
8. Replay the Updates pulse even if the same event was clicked twice.

Parameter changes may focus the parameter scope when no exact fragment mapping exists.

### 6.2 Timeline output click

1. Mark that output event active.
2. Focus only selected-call output changes, never input changes.
3. Preserve all earlier output history in Mixed Trace.
4. If no output fragment changed, focus the complete output scope in Mixed and Exact.
5. Focus and pulse the output entry or “Unchanged output” row in Updates.

### 6.3 Update-entry click

1. Select the owning call without losing the chosen entry.
2. Activate the timeline event of the entry's own phase — an output entry never activates its
   call's input event.
3. Activate I/O unless the target is a parameter checkpoint.
4. Focus the matching Mixed and Exact fragments.
5. Mark the clicked entry active.

Clicking one half of a transition asks about that half: the removed text focuses only removed
text, the present text only present text, and clicking the entry outside either half focuses
the change as a whole. A click on removed text flashes nothing in Exact State, which by
definition does not contain it.

Selecting the same call twice is one selection. A second click must join the load the first
started rather than cancel it, and must not rebuild panes that already show that call —
otherwise the focus the first click was about to apply is silently lost, which is most likely
exactly when the segment is long enough to have made the user click again.

Reusing already-rendered panes must not leave the previous focus behind. Applying focus clears
the prior focus decoration from Mixed and Exact first, so switching phase on one call — input
then output — moves the highlight rather than adding to it. Stale marks left in the other scope
would be what the pane rests on, and the click the user just made would read as doing nothing.

Text selection takes precedence. Dragging to select text must not trigger navigation.

### 6.3.1 Update-card click

Entries, fragments, and checkpoint sections own their more specific navigation. Everywhere
else on a card — its head, its scope notice, its padding — the card itself is the control and
selects its own phase, exactly as clicking that phase’s timeline event would. A card is never
inert: one whose body holds only an “Unchanged input”, “Parallel lane input”, or “Unchanged
output” row still has a call and a moment to select.

### 6.4 Mixed/Exact back-reference click

Clicking marked state text navigates to the owning Updates entry. Fragment-level output
selection must select the matching grouped fragment rather than the whole output card.

The hit target determines the semantic action:

| Click target | Result |
| --- | --- |
| Marked fragment with an update identity | Focus only that update in Mixed, Exact, Updates, and its Timeline phase |
| Input, Thoughts, Output, or Input parameters label | Focus the corresponding whole scope and its mapped updates |
| Plain `.state-scope-content` payload | No navigation; leave it available for caret placement and text selection |
| Checkpoint snapshot content | Focus the owning checkpoint scope because the full snapshot is one update section |

A click on plain reconstructed content must never guess provenance, choose the first nearby
update, or focus every update in the scope. If provenance is not represented by a marked
fragment, the content is not a back-reference.

Prompt is a structural sub-label inside Input, not an independent trace phase. Clicking a
marked prompt fragment follows that fragment’s update identity. Clicking unmarked prompt text
does nothing.

## 7. Animation rules

Animation communicates a new action, not a permanent state.

| Animation | Duration | Trigger | Persistent state after animation |
| --- | ---: | --- | --- |
| Timeline running pulse | 1.4 s, repeating | Call is running | Stops when call completes |
| Updates focus pulse | 1.15 s | Timeline input/output click | Yellow focus outline remains |
| Whole-scope pulse | 1.15 s | Unchanged output or unmapped scope | Kind-colored scope focus remains |
| Exact fragment flash | 1.5 s | Linked fragment focused | Exact focus treatment remains |
| Added fragment flash | 1.5 s | Present fragment focused | Green present style remains |
| Removed fragment flash | 1.5 s | Removed fragment focused | Red removed style remains |
| Parameter flash | 1.5 s | Parameter fragment focused | Violet parameter style remains |
| Back-reference confirmation | 1.5 s | State-to-Updates navigation | Active entry remains |
| Focus scrolling | 200 ms | Selected target is out of view | Target rests at requested alignment |

### 7.1 Replay

A second click on the same timeline event is still an action. Its animation must restart.
Implementations may remove the animation class, force layout, and re-add it.

A pulse replays only on a user action, never on a live refresh. A refresh reconciles Updates
card order in place: a card that has not moved keeps its DOM node. Re-inserting a node — even
into the same position — restarts every CSS animation on it and its descendants, so a blanket
rebuild of the card list would replay every focused entry's pulse on every poll, and the
outline would appear to flash without end. Only genuinely new or reordered cards are moved.

### 7.2 No competing motion

One user action may issue at most one scroll command per pane.

For Updates, scroll directly to the final focused entry. Do not first scroll the entire card
and then center a child; tall cards would visibly jump and return.

Starting a new focus scroll in a pane cancels the previous animation for that pane.

### 7.3 Motion semantics

- Pulses must emphasize, not obscure, text.
- A flash must return to the correct persistent semantic color.
- Animation must never change content or selection.
- Continuous animation is allowed only for truly running calls.
- A future reduced-motion rule should replace smooth motion with immediate placement and
  suppress decorative pulses while retaining outlines.

## 8. Scrolling and live-update rules

Each pane owns its scroll position.

- Timeline refresh preserves the same visible event and pixel offset.
- An output completion inserted above the viewport must not move the user’s anchor.
- Updates preserves its viewport while lazy cards above expand.
- User wheel, touch, pointer, or keyboard scrolling cancels automatic following.
- With Follow off, appended calls do not change selection and do not scroll any pane —
  sticking to the newest end is following, and belongs to the toggle.
- With Follow on, only the latest appended call becomes selected, and the timeline brings it
  into view.
- Repeat clicks must not cause a jump-and-return cycle.
- Mixed may preserve scroll between structurally similar requests; a checkpoint starts at the
  beginning of the new state.

## 9. Checkpoints, identical calls, and unchanged output

### 9.1 Checkpoint

A checkpoint is a semantic flush, not merely a strongly different call.

- Timeline input uses a diamond.
- Mixed history before the checkpoint is removed from the new segment.
- Updates shows a full input/parameters/output snapshot.
- Timeline phase focuses the corresponding snapshot section.

### 9.2 Identical call

An identical call uses a compact dashed card and states that no input, parameter, or output
changed. It remains a selectable timeline event.

### 9.3 Unchanged output

Output can be unchanged relative to its comparison call even when input changed.

- Do not invent an output diff.
- Do not leave the output event without a focus target.
- Focus the exact output scope.
- Show and pulse the “Unchanged output” row in Updates.
- Retain earlier output history in Mixed Trace according to grow-or-flush rules.

### 9.4 Unchanged input

Input can be unchanged relative to its comparison call even when output changed, so a call
can carry no input update at all.

- Do not invent an input diff.
- Do not leave the input event without a focus target of its own.
- Focus the exact input scope.
- Show and pulse the “Unchanged input” row in Updates, naming the compared call.
- An input event never focuses an output update, and never falls back to the whole card,
  because a card whose only entry is an output change would read as an output focus.

### 9.5 Parallel lanes

A parallel lane is a concurrent fork, not a continuation. Its state records no parent state,
so the lane has no baseline it could have changed from; the input diff falls back to whichever
same-purpose call arrived before it, which is a sibling lane.

- Never present a sibling lane as an ancestor, and never call a lane input “unchanged”.
- The lane’s input row states that there is no previous state, and names no call at all: a
  reference would contradict the absence it reports. The sibling is an implementation detail
  of the diff, not a relationship the trace records.
- Because nothing the lane sent is a change, its input card shows the request itself —
  input and parameters — as a snapshot beneath that row. A lane moment is never represented
  by an empty card, and lanes read the same whether or not a sibling happened to precede them.
- The snapshot does not make the lane a checkpoint: Mixed Trace still grows rather than
  flushing, and the timeline keeps the plain input marker.
- Lane identity stays visible in the timeline (`main · p3`) and in the lineage line.

## 10. Accessibility and input behavior

- Timeline events and update entries are keyboard-operable buttons or button-like elements.
- Enter and Space activate focused update entries.
- `:focus-visible` must receive a visible outline.
- Color is reinforced by text labels, strike-through, borders, arrows, and structural
  placement.
- Trace text remains selectable in every pane.
- Navigation handlers must ignore clicks made while a non-collapsed text selection exists.
- Focus styling must maintain readable foreground/background contrast.
- Search and session controls require accessible labels.

## 11. Copy fidelity

Copied trace text must represent stored data.

- Do not insert operation symbols into payload spans.
- Do not remove real prompt delimiters or model-template tokens.
- CSS pseudo-elements, borders, and labels may communicate status without contaminating
  copied content.
- Metadata such as fragment locations and repeat counts must remain outside payload elements.
- Structural Input, Output, Thoughts, Input parameters, Prompt, Messages, Content, and Role
  labels must remain outside payload elements and must not appear in copied model text.

## 12. Loading, error, and empty states

- A loading Updates card reserves height to reduce layout shift.
- A pruned call reports “Call unavailable” and does not offer a retry that cannot succeed.
- A transient load error offers Retry.
- An empty session clears selection and gives explicit empty messages in all affected panes.
- Loading failure in one card must not block unrelated cards.

## 13. Validation checklist

Every UI change should verify:

### Mixed Trace

- Does history grow until checkpoint?
- Is superseded text retained but red?
- Does removed text reference the call that removed it, so a click lands on a removed side in
  Updates rather than the added side of the call that first introduced it?
- Is current text green?
- Does a checkpoint flush prior history?
- Does each retained fragment remain in its correct semantic scope?
- Does update identity, rather than equal text, drive cross-pane linking?
- Does every change of the selected call have a mark in Mixed, including when an earlier call
  in the segment covers the same text?
- Does every update entry, including a removal, focus something in Mixed when clicked?
- Does a rapid second click on the same call still end with its focus applied?
- Does switching phase on one call move the highlight to the clicked phase and clear the
  other, with the new focus scrolled into view?
- Is a fragment placed by its recorded position — prompt line, message index, response
  offsets — so repeated identical payload keeps a focus target per occurrence and removed
  text stays where it was?
- Do earlier-call entries fall back to content matching rather than trusting positions that
  later insertions have shifted?

### Exact State

- Does it contain only selected-call values?
- Does output focus work for both changed and unchanged output?
- Are copied values free of UI-injected operation characters?
- Does clicking plain payload leave focus unchanged?
- Does clicking a marked fragment focus only its owning update?

### Updates

- Does the correct entry pulse on every click, including a second click?
- Does a phase click focus every entry of that phase, not only the first?
- Does the focus pulse stay put across live refreshes instead of replaying every poll?
- Does clicking a card with no entries — head, notice, or padding — still select its call and
  activate the matching timeline event?
- Does every click inside an output card leave the output event active, never the input one?
- Does a parallel lane show its request rather than an empty card?
- Does the persistent focus remain after the pulse?
- Is unchanged output represented by a real focus target?
- Is unchanged input represented by a real focus target, so an input click never pulses an
  output update?
- Does a parallel lane say it has no previous state instead of claiming an unchanged input?
- Are cards append-only and stable during live refresh?
- Do input and output cards follow the same event order as the Timeline, including when
  overlapping calls interleave?

### Motion and scrolling

- Is there at most one automatic scroll per pane per action?
- Does a new action cancel the old scroll animation?
- Is viewport position stable during live insertion and lazy loading?
- With Follow off, does a timeline resting at the newest end stay put as events arrive?

### Semantics

- Does data-kind color still mean input, parameters, or output?
- Does green/red still mean present/removed?
- Is yellow used only for interaction focus?
- Does focus decoration stay visible on top of kind and operation decoration, including on
  retained removed text and on fragments parked out of line?
- Do stacked inline marks keep their outlines inside their own line, so a dense stack does not
  overlap into an unreadable web?
- Are real stored protocol symbols preserved and UI-generated `+`/`−` symbols absent?
- Are structural labels visually and textually separate from model payload?
