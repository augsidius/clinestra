# People API

Resources describing patients, care providers and care settings. These resources follow the [standard operations](api_basics.md#the-resource-pattern).

| Resource | Path | Use it for |
|---|---|---|
| Patient | `/v1/patients` | Anyone receiving care, including family members |
| Relationship | `/v1/relationships` | Links a patient to a guardian or care taker |
| Provider | `/v1/providers` | Clinicians who make clinical decisions |
| Care team member | `/v1/care-team-members` | Nurses, coordinators, dietitians and others |
| Care team | `/v1/care-teams` | The roster around one patient |
| Clinical setting | `/v1/clinical-settings` | A place or service that fulfils orders |

## Create a patient

`name`, `country`, `gender` and either `date_of_birth` or `age_years` are required.

```bash
curl -s $API/patients -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Asha K", "country": "IN", "gender": "female", "age_years": 54}'
```

## See a patient at a glance

| Route | Returns |
|---|---|
| `GET /v1/patients/{patient_id}/summary` | Profiles, with what is present and absent, and pending actions |
| `GET /v1/patients/{patient_id}/pending-actions` | What is waiting for this patient |
| `GET /v1/patients/{patient_id}/facts` | The facts that cohort rules see |
| `GET /v1/patients/{patient_id}/timeline` | The audit trail |
| `GET /v1/patients/{patient_id}/timeline/verify` | Whether the trail is intact |
