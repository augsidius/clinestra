# Setup

## Choose an install

| Command | Adds |
|---|---|
| `pip install clinestra` | The core library only. No server or AI. |
| `pip install "clinestra[server]"` | The Clinical API |
| `pip install "clinestra[postgres]"` | The Postgres driver, `psycopg` |
| `pip install "clinestra[ai]"` | AI runtime, admin and patient portal APIs, with the server |
| `pip install "clinestra[full]"` | All of the above |

`psycopg` is needed only for Postgres. SQLite needs no driver.

## Create a project

Run the wizard in an empty folder:

```bash
clinestra init
```

Or answer everything with flags, for scripts:

```bash
clinestra init --yes --database sqlite --site clinic_1
```

`init` creates two files:

| File | Contents |
|---|---|
| `.env` | Mode, database, packs, host, port, and generated `CLINESTRA_MASTER_KEY` and `CLINESTRA_KEY_PEPPER` |
| `keys.json` | Hashed API keys, including the first admin key |

The first admin key is printed once. `init` refuses to replace existing files unless you pass `--force`, and `--force` stops existing API keys from working.

Keep `.env` and `keys.json` out of version control. Back up `CLINESTRA_MASTER_KEY`: encrypted provider keys can't be recovered without it.

## Choose a database

| Database | Use it for | Setting |
|---|---|---|
| SQLite | One server, local work, small sites | `CLINESTRA_DATABASE_URL=sqlite:///clinestra.db` |
| Postgres | Production and several server processes | `CLINESTRA_DATABASE_URL=postgresql://...` |

If no database is configured, data is held in memory and lost on restart.

### Use your own Postgres

1. Install the driver:

   ```bash
   pip install "clinestra[postgres]"
   ```

2. Create an empty database and a user who can create tables in it.

3. Add its connection string to `.env`:

   ```bash
   CLINESTRA_DATABASE_URL=postgresql://clinestra:YOUR_PASSWORD@db.example.org:5432/clinestra
   ```

   Or set it when you create the project:

   ```bash
   clinestra init --yes --database-url postgresql://clinestra:YOUR_PASSWORD@db.example.org:5432/clinestra
   ```

4. Check the connection and create the tables:

   ```bash
   clinestra db check
   clinestra db migrate
   clinestra db info
   ```

5. Start the server:

   ```bash
   clinestra serve
   ```

Clinestra uses the first of these that is set: `CLINESTRA_DATABASE_URL`, `DATABASE_URL`, `CLINESTRA_DB`. Variables already set in the real environment win over `.env`. Passwords are hidden in command output and error messages.

**Postgres notes**

- Postgres can't store the NUL character in text, so a record containing one is refused with a clear error.
- To move existing SQLite data, run `clinestra db copy --from care.db --to postgresql://...`. The target must be empty.
- Set `CLINESTRA_MASTER_KEY` with Postgres. Without it, outside production, a key file is created in the current folder. `clinestra init` sets it for you.

## Check the setup

```bash
clinestra doctor
clinestra doctor --production
clinestra config
```

`doctor` reports problems and how to fix each. `--production` treats unsafe settings as failures. `config` shows each effective setting and where it came from, with secrets hidden.

## Add people and services

```bash
clinestra keys create --id dr_rao --site clinic_1 --role clinician
clinestra keys list
clinestra keys revoke key_0123456789abcdef
```

Restart the server after changing keys, because it reads the file when it starts.

## Add content

```bash
clinestra pack new htn
clinestra pack validate htn
clinestra pack install ./htn --enable
```

See [Packs](packs.md).

## Next

- [Configuration](configuration.md): every setting
- [Deployment](deployment.md): production
