# AI harness

`registry(h)` from `clinestra.server.agentic.tools` (needs `clinestra[full]`) returns a `ToolRegistry`. It has two layers of tools:

1. **Clinical API tools**, generated for every concept:
   - `<kind>_list`, `_get`, `_create`, `_update`, `_transition`, `_propose`.
2. **Harness tools**:
   - `fetch_clinical_data`, `summarize_patient`, `extract_observations`, `clinical_reasoning`, `pending_actions`
   - `fetch_evidence`, `definition_schema`, `generate_definition`

## Contexts

| Scope | Pins | Concepts exposed |
|---|---|---|
| `patient` | `patient_id` | Everything about one patient |
| `encounter` | `patient_id`, `encounter_id` | The encounter-scoped subset: records, pages, assessments, observations, encounters … |
| `setting` | — | Site-wide: providers, members, settings, definitions, routines, tasks … |
| `general` | — | Definitions, evidence, definition generation |

Pinned ids are removed from the tool schemas the model sees, and they are injected on every call. A tool cannot reach a record that belongs to another patient than the one in its context.

```python
ctx = {"scope": "patient", "patient_id": p.id}
tools = registry(h).definitions(ctx, actor=agent)        # [{name, description, input_schema}]
result = registry(h).call(agent, "summarize_patient", {}, ctx)
```

Over HTTP:
- `GET /v1/tools?scope=patient&patient_id=...`
- `POST /v1/tools/call` with `{name, args, scope, patient_id, encounter_id}`

## What an agent gets

For an `actor_type="agent"` actor, the registry lists only these tools:

- read tools (`_list`, `_get`);
- `_create` and `_update` for concepts that allow AI drafts (note pages, documents, conversations, proposals);
- `_propose` for everything else;
- all harness tools.

Every call still runs through the harness rules, so the same checks apply whether the agent uses a tool or the API. Those checks include the patient's `ai` consent.

## Plugging in a model

```python
class MyLLM:
    def complete(self, system, prompt, json_schema=None):
        ...   # call any model; return text, or a dict matching json_schema

h = Harness(llm=MyLLM())
```

What each harness tool does depends on whether an LLM is connected:

| Tool | Without an LLM | With an LLM |
|---|---|---|
| `extract_observations` | Regex over the site vocabulary (blocks, journal fields, assessment codes) and blood pressure | Model-coded observations |
| `summarize_patient` | Structured summary from health profiles (present/absent) | Optional prose (`prose: true`) |
| `clinical_reasoning` | Facts, matching cohorts, pending actions | Assessment plus suggestions |
| `generate_definition` | Returns the config schema | Validated config: a draft definition for a person, a proposal for an agent |
| `fetch_evidence` | Pack PICO/evidence search | Your `EvidenceProvider` |

In every row, extracted values and suggestions become proposals; they are never written directly.

The tool definitions follow the Anthropic/MCP `input_schema` shape. To run an agent loop, pass `registry(h).definitions(ctx, agent)` as the tools and route each tool-use block to `registry(h).call(agent, name, input, ctx)`.
