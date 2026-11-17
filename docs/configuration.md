# Configuration

Clinestra reads settings from environment variables. `clinestra serve` and the other commands also load a `.env` file from the current folder. Use `--env-file` to read a different one. A variable set in the real environment wins over `.env`.

To see what is in effect:

```bash
clinestra config
```

## Sample `.env`

`clinestra init` writes a file like this.

```bash
CLINESTRA_MODE=production
CLINESTRA_DATABASE_URL=postgresql://clinestra:YOUR_PASSWORD@db.example.org:5432/clinestra
CLINESTRA_PACKS=core
CLINESTRA_HOST=127.0.0.1
CLINESTRA_PORT=8000
CLINESTRA_API_KEYS=keys.json
CLINESTRA_KEY_PEPPER=<generated>
CLINESTRA_MASTER_KEY=<generated>
```

## Core

| Variable | Default | Purpose | In production |
|---|---|---|---|
| `CLINESTRA_MODE` | `pilot` | `production` refuses unapproved definitions and plaintext API keys | Set to `production` |
| `CLINESTRA_DATABASE_URL` | none | `postgresql://...`, `sqlite:///path` or a file path | Required |
| `DATABASE_URL` | none | Used if `CLINESTRA_DATABASE_URL` is not set | Alternative |
| `CLINESTRA_DB` | none | SQLite file, used if neither URL is set | Not needed |
| `CLINESTRA_PACKS` | `core` | Comma-separated built-in names or folders | As needed |
| `CLINESTRA_HOST` | `127.0.0.1` | Address to bind | As needed |
| `CLINESTRA_PORT` | `8000` | Port | As needed |
| `CLINESTRA_CORS_ORIGINS` | none | Comma-separated browser origins allowed to call the API | If a browser app calls it |

## Authentication and secrets

| Variable | Default | Purpose | In production |
|---|---|---|---|
| `CLINESTRA_API_KEYS` | none | Path to the API keys file | Required unless you use your own authenticator |
| `CLINESTRA_KEY_PEPPER` | a development value | Secret mixed into API key hashes | Required |
| `CLINESTRA_MASTER_KEY` | none | Encrypts stored provider keys | Required |
| `CLINESTRA_MASTER_KEY_FILE` | `clinestra_master.key` next to `CLINESTRA_DB` | Key file used when `CLINESTRA_MASTER_KEY` is not set | Don't use |
| `CLINESTRA_DEV_AUTH` | off | `1` trusts `X-Actor-*` headers | Never |
| `CLINESTRA_ENV` | none | `development` allows development OTP providers | Leave unset |

## Clinical safety

| Variable | Default | Purpose | In production |
|---|---|---|---|
| `CLINESTRA_ADMIN_ALL_ROLES` | off | `1` lets admins act as clinicians, which removes separation of duties | Leave off |
| `CLINESTRA_REGIMEN_COUNTER_SIGN` | `required` | `off` removes the second-clinician signature on regimens | Keep `required` |
| `CLINESTRA_ALLOW_UNREVIEWED_PACKS` | off | `1` reports `unreviewed_content` on health; packs still load | Leave off |

## AI and accounts

| Variable | Default | Purpose | In production |
|---|---|---|---|
| `CLINESTRA_AI` | on | `0` turns off the AI, admin and portal routes | As needed |
| `CLINESTRA_ADMINS` | none | Comma-separated first AI admins; ignored once a key has the admin role | Optional |
| `CLINESTRA_SITE` | the first staff key's site | Site for accounts and AI settings | Optional |
| `CLINESTRA_BOOTSTRAP_USER` | `admin` | Username of the first account | Optional |
| `CLINESTRA_PATIENT_GRAPH` | `off` | Value reported by `/v1/health` | Optional |
| `CLINESTRA_REMOTE_URL` | none | Address of a hosted provider service; the interface is defined but no service exists yet | Leave unset |
| `CLINESTRA_REMOTE_KEY` | none | Key for that service | Leave unset |
