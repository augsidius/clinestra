# Clinical data API

Resources describing the patient's clinical status. These resources follow the [standard operations](api_basics.md#the-resource-pattern).

| Resource | Path | Use it for |
|---|---|---|
| Observation | `/v1/observations` | One clinical fact: a vital, lab, score or symptom |
| Condition | `/v1/conditions` | A diagnosis or problem |
| Medication | `/v1/medications` | A medicine being taken |
| Clinical state | `/v1/clinical-states` | Current grade of a condition, or a general state |
| Outcome | `/v1/outcomes` | A target to reach, tied to a measure |
| Health profile | `/v1/health-profiles` | A view of what is present and missing |

## Record an observation

```bash
curl -s $API/observations -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "code": "bp_systolic", "category": "vital", "value": 138, "unit": "mmHg"}'
```

Most observations are written for you when a note page is finalized, or when a journal entry or assessment is saved.

## Record a condition

Recording a condition is a clinical decision, so it needs a clinician.

```bash
curl -s $API/conditions -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "code": "htn", "verification": "confirmed"}'
```

## Clinical states

A patient has at most one current state in general and one per condition. A new state replaces the old one in the record, and the old one is kept.

## Other routes

| Route | Returns |
|---|---|
| `GET /v1/health-profiles/{id}/view` | What is present and absent in a profile |
| `GET /v1/outcomes/{id}/evaluate` | The trend of the outcome's measure |
| `POST /v1/calculators/{kernel}` | Runs a calculator directly |
