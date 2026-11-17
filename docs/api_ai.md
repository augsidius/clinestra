# AI API

Routes for connecting an external model to scoped clinical tools and for operating the built-in agents. They require `clinestra[full]`.

## Tools for your own model

Fetch tool definitions scoped to one patient, give them to your model, and route each tool call back.

| Route | Does |
|---|---|
| `GET /v1/tools` | Lists tools for a context |
| `POST /v1/tools/call` | Calls a tool as the authenticated actor |

```bash
curl -s "$API/tools?scope=patient&patient_id=pat_123" -H "$AUTH"

curl -s $API/tools/call -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "summarize_patient", "args": {}, "scope": "patient", "patient_id": "pat_123"}'
```

Use a key whose actor type is agent for your model. The patient needs an active `ai` consent, or the call returns `403`.

## Built-in agents

| Route | Does |
|---|---|
| `GET /v1/ai/status` | Shows whether AI is configured |
| `GET /v1/ai/agents/personal` | Gets your personal agent |
| `GET /v1/ai/agents/teams` | Lists care team agents |
| `POST /v1/ai/agents/{agent_id}/chat` | Sends one message to an agent |
| `POST /v1/ai/agents/{agent_id}/runs` | Runs a care team skill |
| `GET /v1/ai/agents/{agent_id}/runs` | Reads reports from those runs |

## Generative UI

Agents answer with typed blocks instead of markup. `GET /v1/ai/content-blocks` lists them, and [UI components](ui_components.md) draws them.

## Administration

An admin connects model providers and sets policy. Provider keys are stored encrypted and are never returned.

| Route | Does |
|---|---|
| `POST /v1/admin/providers` | Adds a provider |
| `POST /v1/admin/providers/{pid}/test` | Tests a provider |
| `GET /v1/admin/models` | Lists models |
| `PUT /v1/admin/routes` | Sets which model serves which feature |
| `PUT /v1/admin/policy` | Sets AI policy |
| `GET /v1/admin/usage` | Shows usage |
| `GET /v1/admin/audit` | Shows the admin audit log |

More detail is in [AI harness](ai_harness.md) and [AI runtime](ai_runtime.md).
