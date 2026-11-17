# AI runtime, admin and patient portal (`clinestra[full]`)

One install gives a tenant working AI with **their own model keys**. Install the extra and the API server mounts
three groups of routes next to the Clinical API:

```
pip install 'clinestra[full]'
clinestra serve --keys keys.json --db clinestra.db
```

| Prefix | What |
|---|---|
| `/v1/ai` | AI features for client applications: `POST /v1/ai` (streamed feature text, or one console-driven tool step), `GET /v1/ai/status`, `GET /v1/ai/config` (navigation, features, agents for this role), `POST /v1/ai/agents/{id}/chat` |
| `/v1/admin` | Model providers (BYOK), models & routing, policy, prompts, usage & audit, staff keys, admins, agents and their tool catalog, navigation & features, patient portal, realtime voice |
| `/v1/portal` | Patient app: `otp/request`, `otp/verify`, `session`, `logout`, `config`, `site`, `site/{key}/config`, `interview/session`, `interview/finish` |

Set `CLINESTRA_AI=0` to leave them off. Without the extra the server is the plain Clinical API.

## Keys and secrets

- Provider keys (Anthropic API, Amazon Bedrock, Google Vertex AI, Microsoft Foundry) and the OpenAI realtime key are
  entered by an administrator, sent once, and stored **Fernet-encrypted** in the harness store. Responses only ever
  carry masked hints; secrets are write-only (rotate by sending a new value).
- Master key: `CLINESTRA_MASTER_KEY` (a Fernet key from your secrets manager) or, failing that, a key file
  (`CLINESTRA_MASTER_KEY_FILE`, default `clinestra_master.key` next to `CLINESTRA_DB`) created on first use. Back it
  up: losing it means re-entering keys. Admin → Overview warns while a key file is in use.

## Per site

All AI state lives in the harness store as private document kinds (`_ai.*`, `_portal.*`), scoped to the caller's site:
settings, providers, models, routes, policy, prompts, agents, model threads, the AI audit log, the admin log and
portal sign-in state. None of it is a clinical concept or reaches the patient timeline.

Admin access: role `admin`, or an actor listed in the site's admins (seeded from `CLINESTRA_ADMINS` on first use,
then edited in Admin → Staff & access).

## Models in the harness

When a site routes a model to `agent`, `h.llm` is a `TenantLLM` (an `LLMProvider`) on that model, so the harness
tools (`summarize_patient(prose=True)`, `extract_observations`, `clinical_reasoning`, `generate_definition`) use it and
`/v1/health` reports `llm: true`. Outside a request it uses the install's main site (`CLINESTRA_SITE`, else the
`portal_service` key's site, else the first staff key's site).

## Agents

Agents are per-site configuration (name, audience, scope, model, prompt, agentic and clinical tool allow-lists). They
run as the `clinestra_agent` identity (`actor_type=agent`), through the same harness calls as the HTTP API, so the harness
enforces AI consent and turns clinical changes into proposals. Without a model they answer from the deterministic
harness tools. Service identities (`clinestra_agent`, `portal_service`) are taken from the keys file when present there,
otherwise built in-process with fixed roles; they cannot be used over HTTP unless an administrator issues a key.

## Patient portal

Phone → OTP → profile → session. A session is a bearer token `pt-<random>.<site>`; the server's authenticator accepts
it as a `patient` actor bound to that patient (and anyone they are guardian for). Only a hash of the token is stored.
`otp_provider: dev_screen` returns the code in the response (development); `webhook` posts it to your SMS service.

## Hosted provider API (future)

`clinestra.providers.remote` documents the seam for a hosted, credit-metered service (evidence search,
managed models, managed voice) configured by a single credit key (`CLINESTRA_REMOTE_URL`, `CLINESTRA_REMOTE_KEY`).
It plugs into the existing `EvidenceProvider` and `LLMProvider` interfaces. It is an interface
stub only: no service exists yet and the harness contains no billing logic.
