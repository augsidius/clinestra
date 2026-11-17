# Quickstart

This guide covers installation through the first API call. It requires Python 3.9 or later.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "clinestra[full]"
```

Try the guided tour:

```bash
clinestra demo
```

## 2. Set up a project

```bash
clinestra init --yes --database sqlite --site clinic_1
clinestra db migrate
```

This writes `.env` and `keys.json`, and prints the first admin key once. Save that key.

To use your own Postgres database, see [Setup](setup.md).

## 3. Start the server

```bash
clinestra serve
```

The interactive API reference is at <http://127.0.0.1:8000/docs>.

## 4. Make your first call

In a second terminal, create a clinician key and use it:

```bash
clinestra keys create --id dr_rao --site clinic_1 --role clinician

export AUTH="Authorization: Bearer <the key it printed>"
curl -s localhost:8000/v1/patients -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34}'
```

The response is the new patient record.

## Next

- [Setup](setup.md): every setup option, including Postgres
- [How-to guides](guides.md): one task at a time
- [Example flows](flows.md): complete scenarios
- [CLI](cli.md): every command
- [API conventions](api_basics.md): the pattern every route follows
