# CLI

Run `clinestra <command> --help` for every option. Commands that read data accept `--json`.

Exit codes are `0` for success, `1` when a check or action failed, and `2` for a usage error or missing configuration.

## Typical setup

```bash
clinestra init --yes --database sqlite --site clinic_1
clinestra doctor
clinestra db migrate
clinestra serve
```

## Project

| Command | Does |
|---|---|
| `clinestra init` | Creates `.env`, a hashed keys file and the first admin key |
| `clinestra doctor` | Checks the install and suggests fixes. Changes nothing. |
| `clinestra doctor --production` | Treats unsafe settings as failures |
| `clinestra config` | Shows each setting and where it came from |
| `clinestra env show` | Prints `.env` with secrets hidden |
| `clinestra env check` | Validates `.env` |
| `clinestra demo` | Walks one patient through the system |
| `clinestra version` | Prints the version |

Useful `init` options are `--dir`, `--force`, `--mode`, `--database`, `--database-url`, `--packs`, `--host`, `--port`, `--cors-origin`, `--site` and `--admin-id`. `--force` replaces existing files, and existing API keys stop working.

## Server

```bash
clinestra serve
clinestra serve --env-file prod.env --port 9000
clinestra serve --packs core,./my_pack
```

| Option | Does |
|---|---|
| `--env-file` | Reads another env file |
| `--host`, `--port` | Sets the address |
| `--db`, `--database-url` | Sets the database |
| `--packs` | Sets packs, as built-in names or folders |
| `--keys` | Sets the API keys file |
| `--mode` | `pilot` or `production` |
| `--cors-origin` | Allows a browser origin. Repeatable. |
| `--allow-unreviewed-packs` | Allows unreviewed content, and reports it on health |
| `--reload` | Restarts on code changes |
| `--dev` | Trusts `X-Actor-*` headers. Local development only. |

## Database

These work on SQLite and Postgres.

| Command | Does |
|---|---|
| `clinestra db url` | Prints the URL with the password hidden |
| `clinestra db check` | Tests the connection |
| `clinestra db migrate` | Creates or updates tables. Safe to run again. |
| `clinestra db info` | Shows the database type and record counts |
| `clinestra db copy --from SOURCE --to TARGET` | Copies everything into an empty database and verifies it |

## Keys

```bash
clinestra keys create --id dr_rao --site clinic_1 --role clinician --expires 90d
```

| Command | Does |
|---|---|
| `clinestra keys create` | Adds a key and shows it once |
| `clinestra keys list` | Lists keys without secrets |
| `clinestra keys revoke KEY_ID` | Revokes a key |
| `clinestra keys import --file IN --out OUT` | Hashes a plaintext keys file |
| `clinestra keys generate` | Prints a random key without storing it |

## Packs

| Command | Does |
|---|---|
| `clinestra packs` | Lists built-in and project packs |
| `clinestra pack info PACK` | Shows what a pack contains |
| `clinestra pack new ID` | Creates a starter pack |
| `clinestra pack validate PACK` | Checks a pack, and exits `1` on errors |
| `clinestra pack install PATH` | Validates and copies a pack to `packs/`. Add `--enable` to turn it on. For a built-in pack, pass its name. |
| `clinestra pack review-sheet PACK --out FILE` | Writes a CSV for clinical review |

## Reference and audit

| Command | Does |
|---|---|
| `clinestra concepts` | Lists resources and states. Add a name to see its fields. |
| `clinestra schema KIND` | Prints the config schema of a definition kind |
| `clinestra audit verify --site SITE` | Verifies timelines, and exits `1` if any was altered. Add `--patient ID` for one patient. |
