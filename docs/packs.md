# Packs

## Built-in operational packs

These packs are included in the library and can be enabled without a download.

| Pack | Purpose | Contents |
|---|---|---|
| `general_opd` | Outpatient visits from check-in to checkout | New and follow-up visits, triage and consultation pages, visit workflow, care team, tasks |
| `surgical_ward` | Elective surgical admissions from admission to discharge | Admission, pre-operative, theatre checklist, operative, observation and discharge pages, surgical pathway, care team, routines, tasks |
| `medical_ward` | Inpatient medical admissions | Admission, ward round, observation and discharge pages, ward pathway, care team, routines, tasks |
| `home_care` | Scheduled home visits | Home visit page and workflow, daily patient log, care team, routine, escalation task |

Enable a pack, then restart the server:

```bash
clinestra pack install general_opd --enable
```

These packs define operational structure only. They contain no cohorts, protocols, regimens, reactions, drug content or clinical thresholds. They have not been clinically reviewed, so an organisation should [review them](clinical_review.md) and adapt them before use with patients. Export a review sheet with `clinestra pack review-sheet general_opd --out review.csv`.

## Pack format

A pack is a folder of definitions with a manifest:

```
my_pack/
  pack.json
  cohort_htn.json
  protocol_htn.json
```

```json
// pack.json
{"pack_id": "htn", "name": "Hypertension", "version": "0.1.0", "licence": "…",
 "definitions": ["cohort_htn.json", "protocol_htn.json"],
 "checksums": {"cohort_htn.json": "<sha256>"}}
```

Each file holds one definition, or a list of them:

```json
{"def_kind": "cohort", "key": "cohort_htn", "version": 1, "name": "Hypertension",
 "config": {"rule": "cond_htn or obs_bp_systolic >= 140", "manual_only": false,
            "protocols": ["proto_htn"],
            "entry_actions": [{"op": "create", "kind": "protocol_assignment",
                               "data": {"protocol_key": "proto_htn", "cohort_membership_id": "$id"}}]}}
```

Load it with `Harness(packs=["core", "./my_pack"])` or `clinestra serve --packs core,./my_pack`.

Pack definitions are published, shared by every site and read-only. A site can add its own versions through `POST /v1/definitions`. A site definition with the same key wins over the library one.

To see the config fields of a kind, run `clinestra schema KIND`. The built-in pack, and the worked example in the repository (`examples/packs/mdd`, not installed), are:

- **core:** general and surgery health profiles, the standard care team, OPD/teleconsult encounters, a general case sheet, a vitals chart, prescription/report/consent documents, a clinic setting, vitals and medication journals, a BMI calculator, a ward vitals routine, a follow-up task, an OPD flow journey, a hypertension careplan, conversation structures (clinician copilot, patient coach, general assistant) and an "outcome achieved" reaction.
- **mdd:** PHQ-9, GAD-7, the MDD cohort (entry creates a depression profile and proposes the acute-phase protocol), the protocol (careplan, assessments, orders, diary, education, behavioural activation, SSRI regimen, remission outcome, follow-up task, care-program journey), the case sheet, regimens, the mood diary, the journey, and a severe-PHQ-9 reaction.

## Effects

Effects appear in reactions, cohort entry/exit and journey `on_enter`:

```json
{"op": "create", "kind": "task", "data": {"title": "…", "target_id": "$id"}, "rationale": "…"}
{"op": "transition", "kind": "journey", "target_id": "$journey_id", "to": "completed"}
```

- `$field` reads a field from the source record.
- `patient_id` is filled in automatically.
- Effects on clinical concepts become proposals.

## Expressions

Cohort rules, protocol criteria and reaction conditions use the sandboxed evaluator (`engine.safe_eval`):

- Facts available to rules: `age`, `gender`, `country`, `cond_<code>`, `state_<state>` and `obs_<code>` (the latest numeric value).
- Reaction conditions see the record's own fields.
