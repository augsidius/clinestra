# Longitudinal care

Longitudinal care is care that continues over time, such as a chronic disease programme. In Clinestra, a programme is a set of versioned definitions that run on a fixed set of resources. This page describes that model.

## The elements of care

| Element | Question | Concepts |
|---|---|---|
| Person and team | Who is this, and who is responsible? | `patient`, `relationship`, `provider`, `care_team_member`, `care_team`, `clinical_setting` |
| Clinical picture | How are they now? | `condition`, `clinical_state`, `observation`, `medication`, `health_profile` |
| Membership | Which programme, and why? | `cohort_membership` |
| Programme | Which care model applies? | `protocol_assignment`, `careplan` |
| Commitments | What needs to be done, by whom, how often? | `care_action`, `task`, `routine`, `regimen`, `outcome` |
| Path | Where are they in the process? | `journey` |
| Contact and capture | What happened? | `encounter`, `clinical_record`, `note_page`, `assessment`, `journal_entry`, `clinical_document`, `conversation` |
| Decision and permission | Who decided, and were they allowed to? | `proposal`, `approval`, `consent`, the timeline |
| Care model | What are the rules of this kind of care? | `definition`, delivered in packs |

## The care loop

1. **Identify.** A cohort rule is evaluated against patient facts.
2. **Enrol.** A membership opens and runs entry actions.
3. **Instantiate.** A protocol creates or proposes its care plan, commitments and journey.
4. **Deliver.** Schedules come due, routines raise tasks, the journey moves.
5. **Capture.** Notes, assessments and journals write observations.
6. **Evaluate.** Trajectories, outcomes and reactions read what changed.
7. **Decide.** Clinical changes wait as proposals for a clinician.
8. **Exit.** The membership closes and runs its exit actions.

## Workflows

A `journey` definition names `states`, `initial`, `transitions` and `terminal`, and runs `on_enter` and `on_exit` effects. On a move, `on_exit` runs first. Clinical effects become proposals.

An encounter definition with `workflow` starts that journey with every encounter. These options apply to encounter journeys:

| Option | Meaning |
|---|---|
| `requires` | Records that must exist, optionally in a state, before a state may be left |
| `gates` | Operations a state allows: `case_sheet`, `finalize_record`, `close_encounter`. A state with gates blocks anything it does not list. |
| `advance_on` | Moves the journey when a record of the encounter enters a state. Without `from_states`, it only moves forward. |

`stages` sets the label, category, color and `sla_minutes` shown on `GET /v1/workflows/{key}/board`.

Care-team staff may force a move with a reason. Patients, agents and system actors may not. If an automatic move is refused, the journey stays put and the timeline records `journey.advance_skipped`.

## Time

A schedule has `every_days`, `anchor`, `until` and `grace_days`. Each occurrence is done, due, overdue or missed, and adherence comes from those. `POST /v1/routines/run` raises tasks without duplicating an occurrence.

Rules read the current picture only. For each observation code they see the latest numeric value.

## Versions

A published definition is frozen. A change is a new version of the same key.

| Behaviour | Version used |
|---|---|
| Cohort entry and exit actions | The version the patient enrolled under, even if retired |
| Journey moves and effects | The version the journey started under |
| New enrolments | The latest published version |
| Records from before versions were stored | The latest published version |

There is no operation yet to move a patient already in care to a newer version. Close the old membership or assignment and open a new one.

## Installing a care model

A care model adds definitions, not concepts. Typically it contains:

| You decide | Definition |
|---|---|
| Who is in | `cohort` |
| The programme and its evidence | `protocol` |
| What needs to be done | `careplan`, `regimen`, `task`, `routine` |
| The path care follows | `journey`, `encounter` |
| What is captured | `note_page`, `assessment`, `journal_plugin` |
| What happens when data changes | `reaction` |

Protocols, regimens, reactions and cohorts need a citation and an approval from someone other than the author before they can be published. In production mode, unapproved definitions are refused.

## Not built yet

Event-relative schedules, windows and sequences; moving patients between versions; episode pause, relapse and transfer; population measures and care gaps; a general link between records; evidence per recommendation and recorded deviations; conflict handling between concurrent protocols; mapping local codes between sites.
