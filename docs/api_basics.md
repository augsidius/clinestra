# API conventions

All resources expose the same operations and follow the conventions described on this page.

The base URL is `http://127.0.0.1:8000/v1` by default. Bodies are JSON. The examples use these two variables:

```bash
export API=http://127.0.0.1:8000/v1
export AUTH="Authorization: Bearer <your key>"
```

## Authentication

Send your key on every request, except `GET /v1/health`. Create keys with [`clinestra keys create`](cli.md#keys). The key sets your identity, role and site, so these never go in a body.

Roles are `clinician`, `nurse`, `chw`, `coordinator`, `admin`, `clinical_lead`, `patient` and `system`.

## The resource pattern

| Call | Route |
|---|---|
| List | `GET /v1/{resource}` |
| Create | `POST /v1/{resource}` |
| Read | `GET /v1/{resource}/{id}` |
| Update | `PATCH /v1/{resource}/{id}` |
| Change state | `POST /v1/{resource}/{id}/transition` with `{"to": "...", "reason": "..."}` |
| Soft-delete | `DELETE /v1/{resource}/{id}?reason=...` |

Not every resource has every call. Clinical records such as observations cannot be deleted.

To see a resource's fields and states, run `clinestra concepts patient`, or open `/docs` on your server.

## Create from a template

Pass `definition_key` to start from a definition. Anything else you send overrides its defaults.

```bash
curl -s $API/encounters -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "definition_key": "opd_visit"}'
```

## Lists

| Parameter | Effect |
|---|---|
| `patient_id` | One patient's records |
| `status` | One state |
| `encounter_id` | One encounter's records |
| `definition_key` | Made from one template |
| `include_deleted` | Include soft-deleted records |
| `limit`, `cursor` | Paging, up to 1000 per page |

Without `limit` you get an array. With it you get `{"items": [...], "next_cursor": "..."}`.

## Status codes

| Code | Meaning |
|---|---|
| 200, 201 | Done, created |
| 202 | The state needs approval. The response is the approval. |
| 401 | Key missing or unknown |
| 403 | Your role may not do this, or a required consent is missing |
| 404 | Not found, or in another site |
| 409 | A lifecycle rule refused it, such as editing a finalized page |
| 422 | Invalid input, such as a missing required field |

Rule errors include a request id for support:

```json
{"error": "PermissionDenied", "detail": "No active 'data_sharing' consent for patient pat_123", "request_id": "62eabf550546"}
```

## API by area

- [People](api_people.md)
- [Clinical data](api_clinical.md)
- [Care](api_care.md)
- [Visits and capture](api_capture.md)
- [Consent and governance](api_governance.md)
- [AI](api_ai.md)
- [All routes](api.md)
