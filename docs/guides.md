# How-to guides

Each guide describes one operation. Set `$API` and `$AUTH` as shown in [API conventions](api_basics.md). Replace ids such as `pat_123` with your own.

## Register a patient and family member

Create both as patients, then link them.

```bash
curl -s $API/relationships -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "related_patient_id": "pat_456", "relation": "daughter", "is_care_taker": true}'
```

## Open a visit

```bash
curl -s $API/encounters -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "definition_key": "opd_visit"}'
```

The case sheet is ready. Find it with:

```bash
curl -s "$API/note-pages?patient_id=pat_123&encounter_id=enc_123" -H "$AUTH"
```

## Correct a signed note

Finalized pages cannot be edited. Add an addendum.

```bash
curl -s $API/note-pages/page_123/addenda -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text": "Patient called, headache resolved."}'
```

## Get a second person to approve a note

Request the `approved` state. You get `202` and an approval, and a different person decides it with `POST /v1/approvals/{id}/decide`.

```bash
curl -s $API/note-pages/page_123/transition -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to": "approved", "reason": "Please review"}'
```

## Give the team a checklist

```bash
curl -s $API/tasks -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "title": "Call patient", "assignee_role": "nurse", "priority": "high", "checklist": [{"item": "Confirm medicines"}]}'
```

## Show a patient queue

Use a journey with an encounter workflow, then read the board.

```bash
curl -s $API/workflows/opd_flow/board -H "$AUTH"
```

## Add your own template

Send a [draft definition](api_governance.md#add-a-site-template), publish it, then create records with its key. To see the fields a kind accepts:

```bash
clinestra schema task
```

## Share a report with a patient

The document must be `ready` and marked shareable, and the patient needs `data_sharing` consent.

```bash
curl -s -X POST $API/clinical-documents/doc_123/share -H "$AUTH" -H 'Content-Type: application/json' -d '{}'
```

## Connect your own model

Fetch [tools for one patient](api_ai.md#tools-for-your-own-model) and pass them to your model. In Python:

```python
from clinestra.server.agentic.tools import registry

ctx = {"scope": "patient", "patient_id": patient.id}
tools = registry(h)
definitions = tools.definitions(ctx, actor=agent)   # give these to your model
result = tools.call(agent, "summarize_patient", {}, ctx)
```

## Draw agent output in React

See [UI components](ui_components.md).

## Check nothing was altered

```bash
clinestra audit verify --site clinic_1
```

## Move from SQLite to Postgres

```bash
clinestra db copy --from sqlite:///clinestra.db --to postgresql://user:password@host:5432/clinestra
```

The target must be empty. Stop the server first, then point `CLINESTRA_DATABASE_URL` at the new database.

## Use the library without a server

```python
from clinestra import Actor, Harness

h = Harness()
dr = Actor(id="dr_rao", site="clinic_1", role="clinician")
patient = h.create(dr, "patient", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 54})
```

`h.create`, `h.update`, `h.transition`, `h.list` and `h.get` work the same way as the routes, and apply the same rules.
