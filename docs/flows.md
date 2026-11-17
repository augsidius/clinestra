# Example flows

Each flow lists the API calls for a complete clinical scenario, with the actor for each call. These flows are executed in the test suite, in `tests/test_doc_flows.py`. Each step names the actor making the call.

## Outpatient visit

A nurse opens the visit, a doctor signs it, and the measurements become data.

| Step | Actor | Call |
|---|---|---|
| 1. Register | Clinician | `POST /v1/patients` |
| 2. Open the visit with `definition_key: opd_visit` | Nurse | `POST /v1/encounters` |
| 3. Find the case sheet | Clinician | `GET /v1/note-pages?patient_id=…&encounter_id=…` |
| 4. Fill it in | Clinician | `PATCH /v1/note-pages/{id}` |
| 5. Sign with `to: finalized` | Clinician | `POST /v1/note-pages/{id}/transition` |
| 6. Read the results | Clinician | `GET /v1/observations?patient_id=…` |

After step 5, `bp_systolic` and `bp_diastolic` are observations. Another `PATCH` returns `409`, so corrections go through an [addendum](guides.md#correct-a-signed-note).

## Start a care programme

| Step | Actor | Call |
|---|---|---|
| 1. Record the diagnosis | Clinician | `POST /v1/conditions` |
| 2. Create the plan with `definition_key: hypertension_basic` | Clinician | `POST /v1/careplans` |
| 3. Activate with `to: active` | Clinician | `POST /v1/careplans/{id}/transition` |
| 4. See what it created | Anyone on the team | `GET /v1/care-actions?patient_id=…` |

A nurse trying step 3 gets `403`, because activating a plan is a clinical decision.

## AI suggests a diagnosis

| Step | Actor | Call | Result |
|---|---|---|---|
| 1. Propose with no consent | Agent | `POST /v1/proposals` | `403` |
| 2. Record consent, scope `ai` | Clinician | `POST /v1/consents`, then `transition` to `granted` | Active |
| 3. Propose again | Agent | `POST /v1/proposals` | `pending` |
| 4. Check the chart | Clinician | `GET /v1/conditions?patient_id=…` | Still empty |
| 5. Accept, with optional edits | Clinician | `POST /v1/proposals/{id}/accept` | Condition created |

To decline, the clinician calls `POST /v1/proposals/{id}/reject` with a reason.

## Senior review of a note

| Step | Actor | Call | Result |
|---|---|---|---|
| 1. Complete the required fields | Doctor A | `PATCH /v1/note-pages/{id}` | Saved |
| 2. Request `to: approved` | Doctor A | `POST /v1/note-pages/{id}/transition` | `202`, an approval |
| 3. Try to approve your own | Doctor A | `POST /v1/approvals/{id}/decide` | `403` |
| 4. Approve | Doctor B | `POST /v1/approvals/{id}/decide` | Note is `approved` |

## Patient logs at home

| Step | Actor | Call | Result |
|---|---|---|---|
| 1. Log with `plugin_key: vitals_log` | Patient | `POST /v1/journal-entries` | `recorded` |
| 2. Read | Clinician | `GET /v1/observations?patient_id=…` | Observations with `perceived_by: patient` |

## Clinic queue

| Step | Actor | Call | Result |
|---|---|---|---|
| 1. Start with `definition_key: opd_flow` | Nurse | `POST /v1/journeys` | `registered` |
| 2. Move to `waiting` | Nurse | `POST /v1/journeys/{id}/move` | `waiting` |
| 3. Jump to `done` | Nurse | `POST /v1/journeys/{id}/move` | `409` |
| 4. Force with a reason | Clinician | Same call with `force: true` | `done`, marked forced |

## Share a report

| Step | Actor | Call | Result |
|---|---|---|---|
| 1. Create, shareable | Clinician | `POST /v1/clinical-documents` | `draft` |
| 2. Sign with `to: ready` | Clinician | `POST /v1/clinical-documents/{id}/transition` | `ready` |
| 3. Share with no consent | Clinician | `POST /v1/clinical-documents/{id}/share` | `403` |
| 4. Record `data_sharing` consent and share again | Clinician | Same call | Released |
