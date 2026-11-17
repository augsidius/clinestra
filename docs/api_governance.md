# Consent and governance API

Resources governing consent, clinical authority and configuration.

| Resource | Path | Use it for |
|---|---|---|
| Consent | `/v1/consents` | A patient's agreement for one scope |
| Proposal | `/v1/proposals` | A change waiting for a person to decide |
| Approval | `/v1/approvals` | A request to enter a gated state |
| Definition | `/v1/definitions` | A template for any configurable resource |

## Record consent

Scopes are `treatment`, `data_sharing`, `ai`, `procedure`, `communication` and `research`.

```bash
curl -s $API/consents -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"patient_id": "pat_123", "consent_scope": "ai"}'

curl -s $API/consents/cons_123/transition -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to": "granted", "reason": "Signed at registration"}'
```

## Decide a proposal

`edits` is optional and amends the proposed data before it is applied.

```bash
curl -s -X POST $API/proposals/prop_123/accept -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"edits": {"verification": "provisional"}}'

curl -s -X POST $API/proposals/prop_123/reject -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"reason": "Not supported by the results"}'
```

## Decide an approval

A transition into a gated state returns `202` and an approval. Someone other than the requester decides it.

```bash
curl -s -X POST $API/approvals/appr_123/decide -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"approve": true, "note": "Reviewed"}'
```

## Add a site template

Definitions start as `draft`. Publishing freezes that version. A site definition with the same key replaces the library one for that site.

```bash
curl -s $API/definitions -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"def_kind": "task", "key": "welcome_call", "name": "Welcome call", "config": {"defaults": {"title": "Welcome call"}}}'
```

| Route | Does |
|---|---|
| `POST /v1/definitions/{id}/review` | Records a review with `{"decision": "approve"}` or `"reject"` |
| `GET /v1/definition-kinds` | Lists definition kinds and their config schemas |
| `GET /v1/packs` | Lists loaded packs |
| `GET /v1/concepts` | Lists every resource with its states and moves |

## Consent forms

Send a form for signature, then read the signed result.

| Route | Does |
|---|---|
| `GET /v1/consent-forms/library` | Lists the included templates |
| `POST /v1/consent-forms/library/{lib_id}/install` | Copies a template to your site |
| `POST /v1/consent-forms/sessions` | Sends a form to a patient |
| `GET /v1/consent-forms/sessions/{sid}` | Reads a session |
| `GET /v1/consent-forms/sessions/{sid}/fhir` | Gets the signed form as a FHIR Consent |
