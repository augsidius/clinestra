# Contributing to Clinestra

Issues and pull requests are welcome.

## Development setup

```bash
pip install -e ".[dev]"
pytest
python scripts/gen_docs.py      # after changing spec.py: regenerates concepts, definitions and API docs
```

## Contributor agreement

Every contribution needs a signed contributor agreement before it can be merged.

> **[PLACEHOLDER: contributor agreement text and signing link, pending legal review]**

## Clinical content

Packs, definitions, calculators, reference ranges and any other clinical content need a **named clinical reviewer**. State the name and qualification of the reviewer and the sources relied on in the pull request. Code and clinical content are reviewed separately.

## Pull requests

- Keep each pull request to one change, with tests.
- Safety rules live in the harness. Do not bypass them in routes or tools.
- Never include patient data, real credentials or keys, in code, tests or issues.

## Security

Report vulnerabilities as described in [SECURITY.md](SECURITY.md), not in a public issue.
