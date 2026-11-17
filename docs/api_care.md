# Care API

Resources describing care pathways, plans and work in progress. These resources follow the [standard operations](api_basics.md#the-resource-pattern).

| Resource | Path | Use it for |
|---|---|---|
| Cohort membership | `/v1/cohort-memberships` | Puts a patient in a group |
| Protocol assignment | `/v1/protocol-assignments` | Applies a protocol, which creates its templates |
| Careplan | `/v1/careplans` | Everything planned for a patient |
| Care action | `/v1/care-actions` | Something the patient does or receives |
| Task | `/v1/tasks` | Something the care team does |
| Routine | `/v1/routines` | A repeating task for a role |
| Regimen | `/v1/regimens` | Drugs and orders prescribed together |
| Journey | `/v1/journeys` | A patient's path through configured states |

## Start a careplan

Create it from a template, then activate it. Activating needs a clinician.

```bash
curl -s $API/careplans -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "definition_key": "hypertension_basic"}'

curl -s $API/careplans/plan_123/transition -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to": "active", "reason": "Agreed with patient"}'
```

## Move a journey

```bash
curl -s $API/journeys/jrn_123/move -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to": "waiting"}'
```

To skip the allowed moves or unmet requirements, send `"force": true` with a reason. The move is marked as forced in the history.

## Other routes

| Route | Does |
|---|---|
| `POST /v1/journeys/{id}/move` | Moves to another state |
| `GET /v1/workflows` | Lists encounter workflows |
| `GET /v1/workflows/{key}/board` | Every encounter on a workflow, by stage |
| `POST /v1/patients/{patient_id}/suggest-cohorts` | Finds cohorts whose rules match |
| `GET /v1/care-actions/{id}/schedule` | What is due and when |
| `POST /v1/routines/run` | Creates tasks for due routines |
| `POST /v1/tick` | Runs housekeeping: routines, expiries, overdue outcomes |

Call `POST /v1/tick` every few minutes from a scheduler, using a `system` key.
