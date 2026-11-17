# Deployment

## Authentication

The server needs an authenticator, a callable that turns a request into an `Actor`. Three options:

**API keys** (simple services, integrations):

```bash
clinestra keys create --id dr_rao --site clinic_1 --role clinician
clinestra serve --host 0.0.0.0
```

Clients send `Authorization: Bearer <key>`.

**Your identity provider** (recommended for real users):

```python
from clinestra import Actor, Harness, SQLiteStore
from clinestra.server import create_app

def auth(request) -> Actor:
    claims = verify_my_jwt(request.headers["authorization"])   # your code
    return Actor(id=claims["sub"], site=claims["org"], role=claims["role"])

app = create_app(Harness(store=SQLiteStore("care.db")), auth=auth)
# uvicorn myapp:app
```

**Dev headers** (`--dev`): trusts `X-Actor-*` headers. Anyone can claim any identity, so use it on localhost only.

## Configuration

Every setting is in [Configuration](configuration.md). `clinestra init --mode production` writes a `.env` with generated secrets.

## Storage

| Store | Use |
|---|---|
| Postgres | Production and several server processes. Set `CLINESTRA_DATABASE_URL`. |
| SQLite | One server. It needs no driver. |
| Your own | Implement the `Store` protocol and pass it to `Harness(store=...)`. Keep the `site IN (actor_site, "*")` filter. |

Set up Postgres as described in [Setup](setup.md#use-your-own-postgres), then run `clinestra db migrate` before starting the server.

## Backups

Back up all of these. The audit timeline lives in the database.

| What | How |
|---|---|
| Postgres | `pg_dump --format=custom --file=clinestra.dump "$CLINESTRA_DATABASE_URL"` |
| SQLite | `sqlite3 clinestra.db ".backup backup.db"` |
| `CLINESTRA_MASTER_KEY` | Store it in your secrets manager. Stored provider keys can't be decrypted without it. |
| `CLINESTRA_KEY_PEPPER` and the keys file | Existing API keys stop working without them. |

After a restore, run `clinestra audit verify --site <site>`.

## Production checklist

- Run `clinestra doctor --production` until it passes.
- Set `CLINESTRA_MODE=production`.
- Use Postgres, with backups tested by a restore.
- Use hashed API keys or your identity provider. Never use `--dev`.
- Leave `CLINESTRA_ADMIN_ALL_ROLES` off.
- Put TLS in front of the server.
- Have every loaded pack clinically reviewed.
- Complete the regulatory assessment for your jurisdiction, such as CDSCO software as a medical device in India, and your consent handling under laws such as DPDP.

## Browser front ends (CORS)

A web app on a different origin (for example the Clinestra console on `http://localhost:5173`) needs to be allowed explicitly:

```bash
clinestra serve --keys keys.json --cors-origin http://localhost:5173
```

or `CLINESTRA_CORS_ORIGINS=https://console.example.org,https://other.example.org`. Nothing is allowed by default.

## Providers

Pass these to `Harness(...)` and then `create_app(harness, auth)`: `llm`, `evidence_provider` and `blob_store` (for example an S3 adapter).

## Periodic work

Call `POST /v1/tick` (or `h.tick(actor)`) from a scheduler every few minutes. It:

- creates tasks for due routines;
- expires documents, consents and short-lived health profiles;
- proposes `unachieved` for outcomes past their due date.
