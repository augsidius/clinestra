# UI components

`@clinestra/blocks` is a React library for Clinestra records and agent output. It displays data and collects input, while the server enforces consent, approvals, permissions and scoring.

```bash
npm install @clinestra/blocks
```

```tsx
import { Blocks, ConceptView, ProposalInbox } from "@clinestra/blocks";
import "@clinestra/blocks/styles.css";
```

## Draw agent output

`Blocks` validates its input, so raw model output is safe to pass in.

```tsx
<Blocks blocks={agentOutput} theme="clinical" onAction={(item) => openScreen(item.screen)} />
```

## Draw a record

`ConceptView` has a card for each resource. It offers only the moves that record's lifecycle allows.

```tsx
<div className="cb-root" data-cb-theme="care">
  <ConceptView kind="task" record={task} onTransition={(to) => move(task.id, to)} />
</div>
```

## Build a workflow

| Component | Use it for |
|---|---|
| `JourneyBoard` | A queue with stages, time targets and forced moves |
| `ProposalInbox` | Suggestions waiting for a decision |
| `ApprovalQueue` | Requests for a second person |
| `ConversationThread` | Chat that can carry proposals |
| `NotePageEditor` | Filling and finalizing a note page |
| `AssessmentRunner` | Asking questions one at a time |
| `JournalLogger` | A patient log form |
| `ConsentGate` | Hiding content until consent exists |
| `ReactionCascade` | Showing what a state change sets off |
| `ProtocolLauncher` | Showing what a protocol will create |
| `ProvenanceTrail` | Showing where a fact came from |
| `CohortBoard` | Managing cohort members |
| `TaskBoard` | The team's work by priority |
| `ActivityTrail` | Who did what |

## Themes

Use `clinical`, `night`, `care`, `warm`, `paper` or `violet`. To make your own, override the `--cb-*` variables on `.cb-root`.
