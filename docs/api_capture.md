# Visits and capture API

Resources describing clinical interactions and the data captured during them. These resources follow the [standard operations](api_basics.md#the-resource-pattern).

| Resource | Path | Use it for |
|---|---|---|
| Encounter | `/v1/encounters` | One visit or interaction |
| Clinical record | `/v1/clinical-records` | The note pages of one encounter |
| Note page | `/v1/note-pages` | A case sheet or chart |
| Assessment | `/v1/assessments` | A questionnaire or calculator |
| Clinical document | `/v1/clinical-documents` | A prescription, report or consent form |
| Journal entry | `/v1/journal-entries` | A log from the patient |
| Conversation | `/v1/conversations` | An AI conversation |

## Fill and sign a note page

Creating an encounter from a template opens its note pages. Finalizing a page writes its mapped fields as observations and locks it.

```bash
curl -s -X PATCH $API/note-pages/page_123 -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"blocks": {"chief_complaint": "Headache", "bp_systolic": 150, "bp_diastolic": 96, "assessment_plan": "Hypertension"}}'

curl -s $API/note-pages/page_123/transition -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to": "finalized", "reason": "Signed"}'
```

To change a finalized page, add an addendum with `POST /v1/note-pages/{id}/addenda`.

## Score an assessment

```bash
curl -s $API/assessments -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "definition_key": "bmi", "answers": {"weight": 58, "height": 160}}'
```

The response holds `score` and, when the definition has them, `band` and `safety_flag`.

## Log from the patient

```bash
curl -s $API/journal-entries -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "plugin_key": "vitals_log", "definition_key": "vitals_log", "data": {"bp_systolic": 132, "weight": 58}}'
```

## Other routes

| Route | Does |
|---|---|
| `POST /v1/clinical-documents/{id}/share` | Releases a ready document to the patient. Needs `data_sharing` consent. |
| `POST /v1/conversations/{id}/turns` | Adds a turn to a conversation |
