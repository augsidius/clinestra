# Examples

These examples show how to build six kinds of application on Clinestra. All six use the same resources and the same API. They differ in who uses them and which screens they show.

| Example | Users | Main resources |
|---|---|---|
| [Outpatient clinic](example_outpatient.md) | Clinicians | `encounter`, `note_page`, `proposal`, `regimen` |
| [Inpatient ward board](example_inpatient.md) | Ward teams | `encounter`, `observation`, `reaction` definition, `task` |
| [Nurse assistant](example_nurse_assistant.md) | Nurses | `routine`, `task`, `observation` |
| [Digital therapeutics](example_digital_therapeutics.md) | Patients | `careplan`, `care_action`, `journal_entry`, `outcome` |
| [Health journal](example_health_journal.md) | Patients | `journal_entry`, `consent`, `observation` |
| [Care team dashboard](example_care_team_dashboard.md) | Coordinators | `cohort_membership`, `proposal`, `task` |

## How to use these pages

Each page has:

- a screenshot of the target screen
- a Python script that creates the data the screen needs
- the API routes and UI components to build the screen
- a prompt you can give to a coding agent

The Python scripts run in Clinestra's test suite. To run one, install Clinestra and save the script to a file:

```bash
pip install "clinestra[full]"
python example.py
```

The screenshots are mock-ups drawn with sample data. Clinestra provides the API and the [UI components](ui_components.md). It does not include these applications. Layout, navigation and any data the resources do not hold are yours to build.

The example definitions are for illustration only. Thresholds, schedules and clinical content must be reviewed by a clinician before use with patients.

## Using the prompts

The prompts assume a running Clinestra API and a coding agent that can read these docs. Before using one:

1. Follow the [Quickstart](quickstart.md) to start the API.
2. Create an empty project for the application.
3. Add the API address and development key to the project's environment.
4. Give the prompt to the agent, and review what it builds.

Every prompt includes the same rule: the application must not compute or decide anything the API is responsible for.
