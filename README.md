# Clinestra

**An open specification and runtime for longitudinal clinical care.**

Clinestra defines how long-term care runs: how clinical workflows are defined, how they are managed over months and years, and how the elements of care interact. The primitives stay the same. The care model changes by installing templates and extensions on top, so a hypertension clinic, an antenatal programme and an occupational health service can run on one foundation.

It works in any care setting: hospitals and wards, clinics and OPDs, primary and community health, home care, telehealth, chronic care programmes, occupational health, and mental health services. Every element of care is a typed record with its own state machine and rules, enforced in code. Use Clinestra as a Python library, run it as a ready-made Clinical API, or give scoped tools to AI agents that can propose changes but never make a clinical decision.

Clinestra does not compete with FHIR. FHIR is built for exchanging health data between systems. Clinestra describes how care runs over time. The two can be used together.

```bash
pip install "clinestra[full]"
clinestra demo            # walk one patient through the whole care structure
clinestra serve --dev     # Clinical API on http://127.0.0.1:8000/docs
```

> **Status: alpha (0.3).** Clinestra is decision support: the clinician keeps the judgement. The bundled core pack is example content. Review it clinically before using them with patients.

---

## Contents

- [Why Clinestra](#why-clinestra)
- [Install](#install)
- [Get started in five minutes](#get-started-in-five-minutes)
- [The care structure](#the-care-structure)
- [Workflows and care over time](#workflows-and-care-over-time)
- [What is not built yet](#what-is-not-built-yet)
- [The concepts](#the-concepts)
- [Safety rules enforced in code](#safety-rules-enforced-in-code)
- [Definitions: templates for everything](#definitions-templates-for-everything)
- [Packs](#packs)
- [Adding your own content](#adding-your-own-content)
- [The clinical engine](#the-clinical-engine)
- [AI harness](#ai-harness)
- [AI runtime, admin and patient portal](#ai-runtime-admin-and-patient-portal)
- [The Clinical API](#the-clinical-api)
- [Standards and interoperability](#standards-and-interoperability)
- [Deploying](#deploying)
- [CLI reference](#cli-reference)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Licence](#licence)

---

## Why Clinestra

Long-term care is more than a series of visits. Someone has to know who belongs in a programme, what is owed to them, what was missed, whether it is working, and what should change. Most systems store each visit and leave the rest to memory, spreadsheets or custom code for each disease. Clinestra makes that structure explicit:

- **Care as a model, not a pile of notes.** Membership, programmes, commitments, paths and outcomes are records that interact over time.
- **One foundation, many care models.** A new programme is a pack of versioned definitions, not new tables or a new product.
- **Patients stay on the version they started.** Publishing a new protocol doesn't silently change the rules for people already in care.
- **Tiered automation.** Routine, non-clinical work happens automatically. Anything clinical becomes a **proposal** that a clinician accepts or rejects.
- **Human in the loop by design.** AI agents need the patient's `ai` consent. They can read, draft and propose, and they can never prescribe, order, diagnose or sign.
- **Provenance.** Every write is attributed to an authenticated actor and appended to a hash-chained timeline for that patient. If a record is altered later, the chain shows it.
- **Mostly deterministic.** About 80% of the system needs no AI at all. Language models, evidence search and blob storage are pluggable providers.
- **Content as data.** Case sheets, programmes, calculators and workflows are versioned definitions shipped in packs, not hard-coded screens.

## Install

Clinestra needs Python 3.9 or later.

| Command | What you get |
|---|---|
| `pip install clinestra` | The core library: concepts, rules, engine, harness, and in-memory, SQLite and Postgres stores, with no server or AI. Its only dependency is `pydantic`. |
| `pip install "clinestra[server]"` | Adds the FastAPI Clinical API (`clinestra serve`). |
| `pip install "clinestra[postgres]"` | Adds the Postgres driver. Needed only for Postgres. |
| `pip install "clinestra[ai]"` | Adds the AI runtime: Claude with your own keys, encrypted secrets, admin and the patient portal. Includes `server`. |
| `pip install "clinestra[full]"` | Everything above: core, Clinical API, Postgres and AI. |
| `pip install "clinestra[dev]"` | Full, plus test and build tools, for contributors. |

From a source checkout:

```bash
pip install -e ".[dev]"
pytest
```

## Get started in five minutes

### 1. Run the demo

```bash
clinestra demo
```

The demo takes one patient through registration, consent, an encounter, a signed note, a careplan, an AI proposal, a calculator and a journal, and prints what each step creates.

### 2. Use it from Python

```python
from clinestra import Harness, Actor

h = Harness()                                    # in memory, with the built-in "core" pack
dr = Actor(id="dr_rao", site="clinic_1", role="clinician")
agent = Actor(id="copilot", site="clinic_1", role="clinician", actor_type="agent")

# Register a patient, record a condition and start a careplan from a template
p = h.create(dr, "patient", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34})
h.create(dr, "condition", {"patient_id": p.id, "code": "htn"})
plan = h.create(dr, "careplan", {"patient_id": p.id, "definition_key": "hypertension_basic"})
print([a.title for a in h.list(dr, "care_action", p.id, careplan_id=plan.id)])

# Run a calculator from its definition
bmi = h.create(dr, "assessment", {"patient_id": p.id, "definition_key": "bmi",
                                  "answers": {"weight": 58, "height": 160}})
print(bmi.score)                                 # 22.6562

# An AI agent needs the patient's consent, and can only propose
consent = h.create(dr, "consent", {"patient_id": p.id, "consent_scope": "ai"})
h.transition(dr, "consent", consent.id, "granted", "Signed at registration")
proposal = h.propose(agent, "create", "condition", {"patient_id": p.id, "code": "ckd"}, rationale="Mentioned in notes")

# A clinician decides, and the change is made under their name
h.accept_proposal(dr, proposal.id)

# Check that the audit chain is intact
print(h.verify_timeline(dr, p.id))               # {'valid': True, 'checked': ..., 'broken_at': None}
```

To keep data between runs, use SQLite:

```python
from clinestra import Harness, SQLiteStore
h = Harness(store=SQLiteStore("care.db"), packs=["core"])
```

### 3. Use it over HTTP

```bash
clinestra serve --dev --db care.db
```

```bash
curl -s -X POST localhost:8000/v1/patients \
  -H 'X-Actor-Id: dr_rao' -H 'X-Actor-Role: clinician' -H 'X-Site: clinic_1' \
  -H 'Content-Type: application/json' \
  -d '{"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34}'
```

Open `http://127.0.0.1:8000/docs` for the interactive API reference.

> `--dev` trusts the `X-Actor-*` headers and is meant for localhost only. Anywhere else, use API keys (`--keys keys.json`) or your own identity provider. See [Deploying](#deploying).

## The care structure

Everything is organised around one **patient**: three questions, plus a loop back from the last.

| Question | Concepts |
|---|---|
| **How are they?** | Observations, conditions, medications, clinical states, health profiles |
| **What can be done?** | Cohorts, protocols |
| **What is being done?** | Careplans (care actions the patient does, tasks the team does), regimens, routines, journeys |
| **Did it work?** | Outcomes. They feed back into clinical states, by proposal. |

```
 Observations ─► Clinical state ─► Cohort ─► Protocol ─► Careplan ─► Outcome
        ▲                                                              │
        └───────────────────────── proposes a new state ◄─────────────┘
```

When a record changes state it can **react**: it can create or move other records. Some reactions are built in; for example, assigning a protocol instantiates its careplan, assessments and tasks. Others you configure as `reaction` definitions. A reaction that would make a clinical change produces a proposal instead.

## Workflows and care over time

A `journey` definition is the workflow language. It names the states and allowed moves, and runs effects when a state is entered or left. Journeys attached to an encounter can also set:

| Option | What it does |
|---|---|
| `requires` | What must be done before leaving a state |
| `gates` | Which operations a state allows: `case_sheet`, `finalize_record`, `close_encounter` |
| `advance_on` | Moves the journey when a record reaches a state |
| `stages` | Board labels and SLA minutes, shown at `GET /v1/workflows/{key}/board` |

Care-team staff can force a move with a reason, and it is recorded as forced.

Schedules track what is due, overdue and missed, and calculate adherence. Trajectories report the direction of a measure. Each record stores the definition version it was made from and keeps using it, so a new version applies to new enrolments only.

See [Longitudinal care](docs/longitudinal_care.md).

## What is not built yet

| Area | Not yet available |
|---|---|
| Time | Event-relative schedules, windows, sequences. Rules see only the latest value of each measure. |
| Protocol change | Moving a patient already in care from one version to another |
| Episodes | Pause, relapse, transfer and close semantics |
| Populations | Denominators, measures, care gaps |
| Links | A general way to relate any record to any other |
| Evidence | Evidence per recommendation, and a recorded deviation with a reason |
| Execution | Conflict handling between concurrent protocols |

## The concepts

Every concept has the same interface: `create`, `get`, `list`, `update`, `transition` and `delete`, plus the rules that govern it. All of it is generated from one registry, `clinestra/spec.py`.

| Group | Concepts |
|---|---|
| **People** | `patient`, `relationship`, `provider`, `care_team_member`, `care_team`, `clinical_setting` |
| **How they are** | `observation`, `condition`, `medication`, `clinical_state`, `outcome`, `health_profile` |
| **What can be done** | `cohort_membership`, `protocol_assignment` |
| **What is being done** | `careplan`, `care_action`, `task`, `routine`, `regimen`, `journey` |
| **Interactions and capture** | `encounter`, `clinical_record`, `note_page`, `assessment`, `clinical_document`, `journal_entry`, `conversation` |
| **Governance** | `consent`, `approval`, `proposal`, `definition` |

Each concept declares:

- **States and moves:** for example, `active → inactive`.
- **Clinical** changes, which only a clinician may make. Automatic reactions and AI agents can only propose them.
- **Frozen** states, which refuse edits and deletes.
- **Gated** states, which need an approval from someone other than the requester.
- **Who may write** each concept, by role, and **what an agent may do** with it.
- **Deletability:** clinical data is never deleted. It moves to `entered_in_error` or a closed state.

Print the registry with `clinestra concepts`.

## Safety rules enforced in code

These rules are not guidelines. The harness checks them on every call, whether the call comes from Python, HTTP or an AI tool.

| Rule | In practice |
|---|---|
| Only the moves in a concept's state machine are allowed | A completed careplan cannot quietly become active again. |
| Clinical changes need a clinician | Nurses, CHWs, coordinators, reactions and agents cannot declare a state, prescribe or order. |
| Automation never makes a clinical decision | Clinical effects from reactions, cohorts and AI become proposals. |
| AI agents need `ai` consent | An agent cannot even read a patient's records without it. |
| Approvals gate | Gated orders, regimens and note pages wait for approval, and nobody approves their own request. |
| History is never overwritten | States supersede, signed pages take addenda, and stopped regimens stay stopped. |
| Frozen means frozen | Finalised pages, published definitions and completed plans refuse edits. |
| Complete before final | Required note-page blocks and all assessment answers are needed before finalising. |
| Consent governs sharing | Sharing a document needs `data_sharing` consent. Guardians act only within delegated scopes. |
| Every write is attributed and hash-chained | `verify_timeline` detects tampering. |
| Sites are isolated | Patient data is visible only within its site. Library content is shared and read-only. |
| Reactions are bounded | Chains stop at `max_reaction_depth`. |

## Definitions: templates for everything

Most concepts are created from versioned **definitions**. A definition is a template with a `def_kind`, a `key`, a `version` and a `config`. A record made from one keeps the exact `definition_key`, `definition_id` and `definition_version` it was made with, so upgrading content never rewrites history.

There are 18 definition kinds:

| Kind | Defines |
|---|---|
| `health_profile` | The structured picture of a patient for a condition or speciality |
| `cohort` | Who belongs (a rule), and what happens on entry and exit |
| `protocol` | Criteria, plus the careplan, assessments, orders, education and outcomes it sets up |
| `careplan` | Care actions and tasks |
| `regimen` | A medication or therapy plan, with dosing |
| `task`, `routine` | Team work, one-off or recurring (for example, ward vitals every 4 hours) |
| `journey` | The workflow: states, moves, requirements, gates and automatic moves (an OPD flow, a care programme) |
| `encounter` | Visit types (OPD, teleconsult), and the workflow each one starts |
| `note_page` | Case sheets and charts, with required blocks |
| `assessment` | Questionnaires and calculators (for example, BMI) |
| `clinical_document` | Prescriptions, reports, consent forms |
| `clinical_setting` | Any place care happens: hospitals, wards, clinics, primary and community health centres, homes, camps, workplaces, virtual care |
| `care_team` | Roles, including the `primary` provider |
| `journal_plugin` | Patient-logged data (vitals, medication taken) |
| `conversation` | Structures for AI conversations (clinician copilot, patient coach) |
| `patient_fields` | Site-specific patient fields |
| `reaction` | "When X changes state and condition Y holds, do Z" |

You can print each kind's config schema with `h.definition_schema("<kind>")`.

## Packs

A **pack** is a folder of definitions with a manifest. Packs are content, never code: Clinestra interprets them and nothing in a pack is executed.

One pack ships with the library, **core**:

| Pack | Contents |
|---|---|
| **core** | General and surgery health profiles, a standard care team, OPD and teleconsult encounters, a general case sheet, a vitals chart, prescription, report and consent documents, a clinic setting, vitals and medication journals, a BMI calculator, a ward vitals routine, a follow-up task, an OPD flow journey, a hypertension careplan, three conversation structures and an "outcome achieved" reaction |

The repository also has a complete worked example, [examples/packs/mdd](examples/packs/mdd), covering a depression cohort, protocol, PHQ-9 and GAD-7. It is not installed with the library. It is unreviewed example content.

```bash
clinestra packs                              # list the built-in packs
clinestra serve --packs core,./my_pack       # load them into the server
```

```python
h = Harness(packs=["core", "./my_pack"])
```

Pack definitions are published, shared by every site and read-only. A site can add its own version of any definition through `POST /v1/definitions`. A site definition with the same key takes precedence over the library one.

## Adding your own content

### 1. Write a pack

```
htn_pack/
  pack.json
  cohort_htn.json
  protocol_htn.json
```

```json
// pack.json
{
  "pack_id": "htn",
  "name": "Hypertension",
  "version": "0.1.0",
  "licence": "CC-BY-4.0",
  "definitions": ["cohort_htn.json", "protocol_htn.json"],
  "checksums": {"cohort_htn.json": "<sha256>", "protocol_htn.json": "<sha256>"}
}
```

```json
// cohort_htn.json
{
  "def_kind": "cohort",
  "key": "cohort_htn",
  "version": 1,
  "name": "Hypertension",
  "config": {
    "rule": "cond_htn or obs_bp_systolic >= 140",
    "protocols": ["proto_htn"],
    "entry_actions": [
      {"op": "create", "kind": "protocol_assignment",
       "data": {"protocol_key": "proto_htn", "cohort_membership_id": "$id"}}
    ]
  }
}
```

Rules and conditions run in a sandboxed evaluator, not in `eval`. They can read these facts:

- `age`, `gender`, `country`, `is_guardian`
- `cond_<code>`, `state_<state>`
- `obs_<code>`, which is the latest numeric value

In these names, `:` and `-` in a code become `_`, so `loinc:8480-6` is `obs_loinc_8480_6`. Expressions can use arithmetic, comparisons, `and`, `or`, `not`, `x if test else y`, and `abs`, `min`, `max`, `round`, `pow`, `sqrt`, `exp`, `log`, `log10`, `floor` and `ceil`.

In effects, `$field` reads a field from the source record and `patient_id` is filled in for you.

### 2. Review it

```bash
clinestra pack review-sheet ./htn_pack       # writes a CSV for clinical sign-off; changes nothing
```

In production mode (`--mode production`), the server refuses definitions that have not been approved.

### 3. Add a site definition at runtime

```bash
curl -X POST localhost:8000/v1/definitions -H 'Authorization: Bearer <admin key>' \
  -H 'Content-Type: application/json' \
  -d '{"def_kind": "task", "key": "task_bp_recheck", "version": 1, "name": "BP recheck", "config": {...}}'
```

### 4. Plug in providers

| Provider | Interface | Used for |
|---|---|---|
| LLM | `complete(system, prompt, json_schema=None)` | Extraction, summaries, reasoning, drafting definitions |
| Evidence | `EvidenceProvider` | `fetch_evidence` |
| Storage | The `Store` protocol (`MemoryStore`, `SQLiteStore` and `PostgresStore` are included) | Bring your own database |
| Identity | A callable `request → Actor` | Your SSO or JWT |

### 5. Share it on Clinestra Hub

For content you want other care providers to install, Clinestra Hub uses a signed pack format, `clinestra.pack/v1`. You write YAML; Hub validates every coded concept, runs the pack's evals, signs the result and publishes it. Hub is not public yet.

## The clinical engine

`clinestra.engine` is pure, deterministic Python. It has no I/O and no AI. Calculators are configured from reference rows, never `eval`, and are built on six kernels:

| Kernel | For |
|---|---|
| `weighted_point_sum` | Scored instruments (PHQ-9, GAD-7), with safety items |
| `weight_banded_dose` | Weight- and renal-banded dosing, rounded to tablet strengths |
| `eval_reference_equation` | Formulas (eGFR, BMI, predicted values) with reference ranges |
| `logistic_risk` | Risk models |
| `exposure_integral` | Cumulative exposure, for example occupational |
| `boolean_gate` | Decision rules and triage |

The engine also covers units, cadence (what is due and when), trajectories over time and overlays. It is available over HTTP too: `POST /v1/calculators/{kernel}`.

## AI harness

The AI tools need `clinestra[full]`. `registry(h)` turns the Clinical API into tools an AI agent can call. The tools use the common `{name, description, input_schema}` shape, so they work with Claude, MCP and most agent frameworks.

- **Scoped contexts:** patient, encounter, setting or general. Pinned ids are removed from the schema and injected on every call, so a tool can never reach another patient's records.
- **What an agent gets:** read tools, draft tools for note pages, documents and conversations, and `_propose` for everything else.
- **Harness tools:**
  - `summarize_patient`
  - `extract_observations`
  - `clinical_reasoning`
  - `generate_definition`
  - `fetch_clinical_data`
  - `fetch_evidence`
  - `pending_actions`
- **Works without a model:** each tool has a deterministic fallback, such as structured summaries or vocabulary-based extraction. Connecting an LLM makes them richer.

Whatever an agent extracts or suggests becomes a proposal. Nothing it produces is written straight to the record.

```python
from clinestra.server.agentic.tools import registry

agent = Actor(id="copilot", site="clinic_1", role="clinician", actor_type="agent")
ctx = {"scope": "patient", "patient_id": p.id}

tools = registry(h)
definitions = tools.definitions(ctx, actor=agent)  # hand these to your model
result = tools.call(agent, "summarize_patient", {}, ctx)
```

Over HTTP the tools are at `GET /v1/tools?scope=patient&patient_id=…` and `POST /v1/tools/call`. See [docs/ai_harness.md](docs/ai_harness.md).

## AI runtime, admin and patient portal

`pip install "clinestra[ai]"` mounts three more route groups on the same server:

| Prefix | What |
|---|---|
| `/v1/ai` | Streamed AI features and configurable agents that run as an AI identity |
| `/v1/admin` | Model providers with **your own keys** (Anthropic API, Amazon Bedrock, Google Vertex AI, Microsoft Foundry), model routing, policy and budgets, prompts, the usage and AI audit log, staff keys and agents |
| `/v1/portal` | The patient app: OTP sign-in, site configuration and a voice intake interview |

Provider keys are encrypted at rest and write-only. They are encrypted with `CLINESTRA_MASTER_KEY` if it is set, otherwise with a key file next to the database. Set `CLINESTRA_AI=0` to turn the AI routes off. See [docs/ai_runtime.md](docs/ai_runtime.md).

## The Clinical API

`clinestra serve` generates REST routes for every concept from the same registry:

```
GET    /v1/{concept}            list (filtered by patient, status, …)
POST   /v1/{concept}            create
GET    /v1/{concept}/{id}       read
PATCH  /v1/{concept}/{id}       update
POST   /v1/{concept}/{id}/...   transition
DELETE /v1/{concept}/{id}       delete (only where the concept allows it)
```

It also has routes for proposals (accept or reject), approvals, timelines and verification, calculators, definitions and tools. Each route runs the same rules as the library. The full list is generated in [docs/api.md](docs/api.md), and it is live at `/docs`.

## Standards and interoperability

| Area | How Clinestra handles it |
|---|---|
| **Coding** | Observations carry a `code` and a `system` (`loinc`, `snomed` or `local`), for example `loinc:8480-6` for systolic BP. Conditions and assessments are coded the same way. |
| **Identifiers** | `patient.identifiers` holds any national or local id, for example `{"abha": "…", "mrn": "…"}`. |
| **Concept model** | Typed resources and 18 definition kinds with typed fields, generated documentation and one registry. FHIR handles exchange with other systems; Clinestra models the care itself. |
| **Content** | Local packs use `pack.json` with checksums. Hub publishes signed `clinestra.pack/v1` packs. The runtime does not yet verify those signatures, and local codes are not mapped between sites. |
| **AI tools** | Tool definitions follow the Anthropic and MCP `input_schema` shape. |
| **Audit** | A hash-chained per-patient timeline (`/v1/…/timeline`, `verify_timeline`). |
| **Data residency** | Self-hosted. Your database and your model keys stay in your infrastructure, which suits data-protection requirements such as India's DPDP Act. |

## Deploying

Create a project, pointing at your own Postgres database or at SQLite:

```bash
clinestra init --yes --mode production \
  --database-url postgresql://clinestra:YOUR_PASSWORD@db.example.org:5432/clinestra
clinestra db check
clinestra db migrate
clinestra doctor --production
clinestra serve --host 0.0.0.0
```

`init` writes `.env` and a hashed keys file, and prints the first admin key once. You can also set `CLINESTRA_DATABASE_URL` in `.env` yourself.

Authentication works in one of three ways:

- **API keys:** clients send `Authorization: Bearer <key>`. Manage them with `clinestra keys`.
- **Your identity provider:** `create_app(harness, auth=my_auth)`.
- **`--dev` headers:** for localhost only.

In `pilot` mode unapproved content is allowed. `production` mode refuses it.

See [Setup](docs/setup.md), [Configuration](docs/configuration.md) and [Deployment](docs/deployment.md).

## CLI reference

| Area | Commands |
|---|---|
| Setup | `init`, `doctor`, `config`, `env show`, `env check` |
| Database | `db url`, `db check`, `db migrate`, `db info`, `db copy` |
| Server | `serve`, `demo` |
| API keys | `keys create`, `keys list`, `keys revoke`, `keys import`, `keys generate` |
| Packs | `packs`, `pack list`, `pack info`, `pack new`, `pack validate`, `pack install`, `pack review-sheet` |
| Reference | `concepts`, `schema`, `version` |
| Audit | `audit verify` |

See the [CLI guide](docs/cli.md).

## Documentation

| Doc | Covers |
|---|---|
| [longitudinal_care.md](docs/longitudinal_care.md) | The care model: elements, workflows, time and versioning |
| [quickstart.md](docs/quickstart.md) | Install to first API call |
| [guides.md](docs/guides.md) | How to do one task at a time |
| [flows.md](docs/flows.md) | Complete example scenarios |
| [api_basics.md](docs/api_basics.md) | The pattern every route follows |
| [cli.md](docs/cli.md) | Every command |
| [clinical_overview.md](docs/clinical_overview.md) | Clinical use, for care teams and clinical leads |

## Contributing

Issues and pull requests are welcome. Clinical content (packs) and code are reviewed separately: content changes need a named clinical reviewer.

```bash
pip install -e ".[dev]"
pytest
python scripts/gen_docs.py      # regenerate concepts, definitions and API docs after changing spec.py
```

To report a security issue, follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

## Licence

Clinestra is licensed under **AGPL-3.0-or-later**; see [LICENSE](LICENSE) and [LICENSE-NOTICE.md](LICENSE-NOTICE.md). The PHQ-9 and GAD-7 item text is free to use. The example MDD pack in this repository must be clinically reviewed before use with patients.

Built by [Augsidius](https://augsidius.com).
