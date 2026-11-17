"""The ``clinestra`` command. Run ``clinestra --help`` for the list.

Exit codes: 0 success; 1 a check failed or the command could not finish; 2 usage or missing configuration.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__

LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Must match clinestra.server.auth, which cannot be imported without the server extra. tests/test_cli.py checks they agree.
HASHED_FORMAT = "hashed-v1"
DEV_PEPPER = "clinestra-dev-pepper"
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
DB_VARS = ("CLINESTRA_DATABASE_URL", "DATABASE_URL", "CLINESTRA_DB")

# name, default, description
SETTINGS = (
    ("CLINESTRA_MODE", "pilot", "pilot allows unapproved content; production does not"),
    ("CLINESTRA_DATABASE_URL", None, "postgresql://... or sqlite:///file.db"),
    ("DATABASE_URL", None, "Used when CLINESTRA_DATABASE_URL is not set"),
    ("CLINESTRA_DB", None, "SQLite file (older setting)"),
    ("CLINESTRA_PACKS", "core", "Comma-separated packs"),
    ("CLINESTRA_HOST", "127.0.0.1", "Address to bind"),
    ("CLINESTRA_PORT", "8000", "Port to bind"),
    ("CLINESTRA_CORS_ORIGINS", None, "Browser origins allowed to call the API"),
    ("CLINESTRA_API_KEYS", None, "Path to the API keys file"),
    ("CLINESTRA_KEY_PEPPER", None, "Secret mixed into stored API key hashes"),
    ("CLINESTRA_MASTER_KEY", None, "Encrypts stored provider keys"),
    ("CLINESTRA_MASTER_KEY_FILE", None, "File holding the master key"),
    ("CLINESTRA_ADMIN_ALL_ROLES", "0", "1 lets admins act as clinicians (pilots only)"),
    ("CLINESTRA_ADMINS", None, "Bootstrap admin ids"),
    ("CLINESTRA_DEV_AUTH", None, "1 trusts identity headers (local only)"),
    ("CLINESTRA_ENV", None, "development enables local-only conveniences"),
    ("CLINESTRA_AI", None, "0 turns the AI routes off"),
    ("CLINESTRA_ALLOW_UNREVIEWED_PACKS", None, "1 reports unreviewed content but allows it"),
    ("CLINESTRA_REGIMEN_COUNTER_SIGN", None, "Regimen counter-signing policy"),
    ("CLINESTRA_PATIENT_GRAPH", None, "Patient graph setting"),
    ("CLINESTRA_SITE", None, "Default site"),
    ("CLINESTRA_BOOTSTRAP_USER", None, "First user created on an empty install"),
    ("CLINESTRA_REMOTE_URL", None, "Hosted provider address"),
    ("CLINESTRA_REMOTE_KEY", None, "Hosted provider key"),
)
KNOWN = {s[0] for s in SETTINGS}
NOT_SECRET = {"CLINESTRA_API_KEYS", "CLINESTRA_MASTER_KEY_FILE"}


# ── helpers ──
class Stop(Exception):
    """A clean, one-line failure with an exit code."""

    def __init__(self, message: str, code: int = EXIT_FAIL):
        super().__init__(message)
        self.code = code


def redact_url(text) -> str:
    """Hide passwords in connection strings wherever they appear, including in driver error messages."""
    out = re.sub(r"(://[^:/@\s]*:)[^@\s]*(@)", r"\1***\2", str(text))
    return re.sub(r"(?i)(password=)[^\s&'\"]+", r"\1***", out)


def _is_secret(name: str) -> bool:
    return name not in NOT_SECRET and bool(re.search(r"KEY|PEPPER|SECRET|PASSWORD|TOKEN", name))


def _shown(name: str, value):
    if value is None or value == "":
        return value
    return "***" if _is_secret(name) else redact_url(value)


def _dump(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _read_env(path) -> dict:
    """Parse a .env file for display and checks. Loading into the process is clinestra.settings.load_env."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = re.sub(r"^export\s+", "", name.strip())
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[name] = value
    return out


def _env_path(args) -> Path:
    return Path(getattr(args, "env_file", None) or ".env")


def _load(args) -> None:
    """Load the .env file. Variables already set in the process win."""
    path = _env_path(args)
    if not path.is_file():
        if getattr(args, "env_file", None):
            raise Stop(f"No env file at {path}", EXIT_USAGE)
        return
    from .settings import load_env
    load_env(str(path), override=False)


def _db_url():
    from .settings import database_url
    url = database_url() or None
    return f"sqlite:///{Path(url).as_posix()}" if url and "://" not in url else url  # CLINESTRA_DB is a plain path


def _as_url(value: str) -> str:
    """A connection URL from a URL or a plain SQLite path."""
    return value if "://" in value else f"sqlite:///{Path(value).as_posix()}"


def _db_kind(url: str) -> str:
    scheme = urlsplit(url).scheme.split("+")[0]
    return {"postgres": "postgresql"}.get(scheme, scheme)


def _sqlite_path(url: str):
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else None


def _open_store(url: str):
    from .storage import store_from_url
    return store_from_url(url)


def _require_existing_sqlite(url: str) -> None:
    path = _sqlite_path(url)
    if path and path != ":memory:" and not Path(path).is_file():
        raise Stop(f"No database at {path}. Create it with: clinestra db migrate")


def _counts(url: str) -> dict:
    sql = "SELECT kind, COUNT(*) FROM docs GROUP BY kind ORDER BY kind"
    path = _sqlite_path(url)
    if path:
        con = sqlite3.connect(path)
        try:
            return dict(con.execute(sql).fetchall())
        finally:
            con.close()
    import psycopg  # installed with the Postgres extra
    with psycopg.connect(url) as con:
        return dict(con.execute(sql).fetchall())


def _write_private(path: Path, text: str) -> None:
    """Write through a temporary file, readable by the owner where the platform allows it."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _env_value(value: str) -> str:
    if not re.search(r"[\s#'\"]", value):
        return value
    return f"'{value}'" if "'" not in value else '"' + value.replace('"', '\\"') + '"'


def _new_key() -> str:
    return secrets.token_urlsafe(32)


def _hash_key(raw: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _key_entry(raw: str, actor: dict, pepper: str, expires=None) -> dict:
    return {"key_id": "key_" + secrets.token_hex(8), "prefix": raw[:6], "hash": _hash_key(raw, pepper), "actor": actor,
            "created_at": datetime.now(timezone.utc).isoformat(), "expires_at": expires, "last_used_at": None,
            "revoked": False}


def _keys_path(args) -> Path:
    path = getattr(args, "file", None) or os.environ.get("CLINESTRA_API_KEYS")
    if not path:
        raise Stop("No keys file. Pass --file, or set CLINESTRA_API_KEYS (clinestra init does this).", EXIT_USAGE)
    return Path(path)


def _read_keys(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and data.get("format") == HASHED_FORMAT):
        raise Stop(f"{path} holds plaintext keys. Hash it first: clinestra keys import --file {path} --out keys.hashed.json")
    return data


def _pepper() -> str:
    pepper = os.environ.get("CLINESTRA_KEY_PEPPER")
    if pepper:
        return pepper
    if os.environ.get("CLINESTRA_MODE") == "production":
        raise Stop("Production needs CLINESTRA_KEY_PEPPER. Generate an environment with: clinestra init", EXIT_USAGE)
    print("Warning: CLINESTRA_KEY_PEPPER is not set; using the development pepper.", file=sys.stderr)
    return DEV_PEPPER


def _expiry(text):
    if not text:
        return None
    match = re.fullmatch(r"(\d+)d", text)
    if match:
        return (datetime.now(timezone.utc) + timedelta(days=int(match[1]))).isoformat()
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        raise Stop("--expires takes a number of days such as 90d, or an ISO date such as 2027-01-31.", EXIT_USAGE)


def _valid_master_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value.encode())) == 32
    except (ValueError, TypeError):
        return False


def _valid_db_url(url: str) -> bool:
    return _db_kind(url) in ("postgresql", "sqlite") and (bool(urlsplit(url).hostname) or url.startswith("sqlite:///"))


def _resolve_pack(name: str) -> str:
    local = Path("packs") / name
    return str(local) if not (Path(name) / "pack.json").exists() and (local / "pack.json").exists() else name


# ── serve and basics ──
def _serve(args):
    _load(args)
    host = args.host or os.environ.get("CLINESTRA_HOST") or "127.0.0.1"
    try:
        port = int(args.port or os.environ.get("CLINESTRA_PORT") or 8000)
    except ValueError:
        raise Stop("CLINESTRA_PORT must be a number.", EXIT_USAGE)
    if args.dev and host not in LOOPBACK:
        sys.exit(f"Refusing --dev on {host}: it trusts identity headers. Use --keys, or bind to 127.0.0.1.")
    try:
        import uvicorn
    except ImportError:
        sys.exit("Serving needs the server extra: pip install 'clinestra[server]'")
    if args.db or args.database_url:
        for name in DB_VARS:
            os.environ.pop(name, None)
    if args.db:
        os.environ["CLINESTRA_DATABASE_URL"] = _as_url(args.db)
        if _sqlite_path(_as_url(args.db)):
            os.environ["CLINESTRA_DB"] = args.db  # the master key file is kept next to this
    if args.database_url:
        os.environ["CLINESTRA_DATABASE_URL"] = args.database_url
    if args.packs:
        os.environ["CLINESTRA_PACKS"] = args.packs
    if args.keys:
        os.environ["CLINESTRA_API_KEYS"] = args.keys
    if args.cors_origin:
        os.environ["CLINESTRA_CORS_ORIGINS"] = ",".join(args.cors_origin)
    if args.mode:
        os.environ["CLINESTRA_MODE"] = args.mode
    if args.allow_unreviewed_packs:
        os.environ["CLINESTRA_ALLOW_UNREVIEWED_PACKS"] = "1"
    if args.dev:
        os.environ["CLINESTRA_DEV_AUTH"] = "1"
        os.environ.setdefault("CLINESTRA_ENV", "development")
        print("Development auth is ON: identity is taken from X-Actor-* headers. Never expose this beyond localhost.")
    from .server import create_app
    try:
        app = create_app()
    except RuntimeError as exc:
        sys.exit(redact_url(exc))
    url = _db_url()
    auth = "development headers" if os.environ.get("CLINESTRA_DEV_AUTH") else f"API keys ({os.environ.get('CLINESTRA_API_KEYS')})"
    print(f"Clinestra Clinical API {__version__} on http://{host}:{port}  (docs at /docs)")
    print(f"  mode      {os.environ.get('CLINESTRA_MODE', 'pilot')}")
    print(f"  database  {redact_url(url) if url else 'in memory (data is lost on exit)'}")
    print(f"  packs     {os.environ.get('CLINESTRA_PACKS', 'core')}")
    print(f"  auth      {auth}")
    if args.reload:
        here = str(Path(__file__).resolve().parent)
        uvicorn.run("clinestra.server:create_app", factory=True, host=host, port=port, log_level="info",
                    reload=True, reload_dirs=[here])
    else:
        uvicorn.run(app, host=host, port=port, log_level="info")


def _demo(_args):
    from .examples_demo import run
    run()


def _concepts(args):
    from .errors import NotFound
    from .spec import SPECS
    specs = list(SPECS.values())
    if args.kind:
        if args.kind not in SPECS:
            raise NotFound(f"Unknown concept '{args.kind}'. Run: clinestra concepts")
        specs = [SPECS[args.kind]]
    if args.group:
        specs = [s for s in specs if s.group.lower() == args.group.lower()]
    if args.json:
        _dump([{"kind": s.kind, "group": s.group, "path": f"/v1/{s.plural}", "moves": s.transitions,
                "clinical": s.clinical if isinstance(s.clinical, bool) else "by_state"} for s in specs])
        return
    if args.kind:
        _dump(specs[0].model.model_json_schema())
        return
    for s in specs:
        moves = "; ".join(f"{a}->{'/'.join(b)}" for a, b in s.transitions.items()) or "-"
        flag = "clinical" if s.clinical is True else ("clinical*" if callable(s.clinical) else "")
        print(f"{s.kind:20} {s.group:20} {flag:9} {moves}")


def _schema(args):
    from .harness import Harness
    _dump(Harness(packs=()).definition_schema(args.kind))


# ── init ──
def _ask(label: str, default=None, choices=None, secret=False) -> str:
    hint = f" [{default}]" if default not in (None, "") else ""
    options = f" ({'/'.join(choices)})" if choices else ""
    while True:
        prompt = f"{label}{options}{hint}: "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip() or (default or "")
        if value and (not choices or value in choices):
            return value
        print("Please enter a valid value.")


def _init(args):
    root = Path(args.dir)
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        raise Stop("No terminal to ask questions on. Pass --yes and the options you want.", EXIT_USAGE)
    mode, database, url, sqlite_path = args.mode, args.database, args.database_url, args.sqlite_path
    packs, host, port, cors, admin, site = args.packs, args.host, args.port, args.cors_origin, args.admin_id, args.site
    if url and not database:
        database = "postgres" if _db_kind(url) == "postgresql" else "sqlite"
    if interactive:
        print("Clinestra setup. Press Enter to accept a default.\n")
        mode = _ask("Mode", mode or "pilot", ("pilot", "production"))
        database = _ask("Database", database or "sqlite", ("sqlite", "postgres"))
        if database == "postgres" and not url:
            url = _ask("Postgres URL, postgresql://user:password@host:5432/db (hidden)", secret=True)
        elif database == "sqlite":
            sqlite_path = _ask("SQLite file", sqlite_path or "clinestra.db")
        packs = _ask("Packs", packs or "core")
        host = _ask("Host", host or "127.0.0.1")
        port = int(_ask("Port", str(port or 8000)))
        if not cors:
            answer = input("Browser origins allowed to call the API, comma-separated (optional): ").strip()
            cors = [c.strip() for c in answer.split(",") if c.strip()]
        site = _ask("Site id", site or "main")
        admin = _ask("First admin id", admin or "admin")
    mode, database, sqlite_path = mode or "pilot", database or "sqlite", sqlite_path or "clinestra.db"
    packs, host, port, site, admin = packs or "core", host or "127.0.0.1", port or 8000, site or "main", admin or "admin"
    if database == "postgres":
        if not url:
            raise Stop("Postgres needs --database-url postgresql://user:password@host:5432/db", EXIT_USAGE)
        if _db_kind(url) != "postgresql" or not urlsplit(url).hostname:
            raise Stop(f"Not a Postgres URL: {redact_url(url)}", EXIT_USAGE)
    elif not url:
        url = f"sqlite:///{Path(sqlite_path).as_posix()}"
    env_file, keys_file = root / args.env_name, root / args.keys_name
    existing = [str(p) for p in (env_file, keys_file) if p.exists()]
    if existing and not args.force:
        raise Stop(f"Already exists: {', '.join(existing)}. Pass --force to replace it; existing API keys stop working.")
    root.mkdir(parents=True, exist_ok=True)
    pepper, raw = secrets.token_urlsafe(32), _new_key()
    master = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    values = [("CLINESTRA_MODE", mode), ("CLINESTRA_DATABASE_URL", url), ("CLINESTRA_PACKS", packs),
              ("CLINESTRA_HOST", host), ("CLINESTRA_PORT", str(port)), ("CLINESTRA_API_KEYS", args.keys_name),
              ("CLINESTRA_KEY_PEPPER", pepper), ("CLINESTRA_MASTER_KEY", master)]
    if cors:
        values.append(("CLINESTRA_CORS_ORIGINS", ",".join(cors)))
    body = "# Created by clinestra init. Keep this file secret and out of version control.\n"
    body += "".join(f"{k}={_env_value(v)}\n" for k, v in values)
    entry = _key_entry(raw, {"id": admin, "site": site, "role": "admin"}, pepper)
    _write_private(env_file, body)
    _write_private(keys_file, json.dumps({"format": HASHED_FORMAT, "keys": [entry]}, indent=2) + "\n")
    ignored = _update_gitignore(root, [args.env_name, args.keys_name] +
                                ([Path(sqlite_path).name] if _sqlite_path(url) else []))
    print(f"Wrote {env_file} and {keys_file}.")
    if ignored:
        print("Added them to .gitignore.")
    print(f"\nFirst admin key for {admin}. It is shown once, so store it now:\n\n  {raw}\n")
    print("Next:\n  clinestra doctor\n  clinestra db migrate\n  clinestra serve")
    if database == "postgres":
        print('\nPostgres also needs its driver: pip install "clinestra[postgres]"')
    print("\nBack up CLINESTRA_MASTER_KEY: encrypted provider keys cannot be recovered without it.")


def _update_gitignore(root: Path, names) -> bool:
    here = root.resolve()
    if not any((p / ".git").exists() for p in (here, *here.parents)):
        return False
    path = root / ".gitignore"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [n for n in names if n not in current.splitlines()]
    if missing:
        prefix = current + ("" if not current or current.endswith("\n") else "\n")
        path.write_text(prefix + "".join(n + "\n" for n in missing), encoding="utf-8")
    return True


# ── env and config ──
def _env_show(args):
    path = _env_path(args)
    if not path.is_file():
        raise Stop(f"No env file at {path}. Create one with: clinestra init", EXIT_USAGE)
    shown = {k: _shown(k, v) for k, v in _read_env(path).items()}
    if args.json:
        _dump(shown)
    else:
        for k, v in shown.items():
            print(f"{k}={v}")


def _env_check(args):
    path = _env_path(args)
    if not path.is_file():
        raise Stop(f"No env file at {path}. Create one with: clinestra init", EXIT_USAGE)
    env = _read_env(path)
    problems, warnings = [], []
    if env.get("CLINESTRA_MODE", "pilot") not in ("pilot", "production"):
        problems.append("CLINESTRA_MODE must be pilot or production")
    if "CLINESTRA_PORT" in env and not (env["CLINESTRA_PORT"].isdigit() and 0 < int(env["CLINESTRA_PORT"]) < 65536):
        problems.append("CLINESTRA_PORT must be a port number")
    url = env.get("CLINESTRA_DATABASE_URL") or env.get("DATABASE_URL")
    if url and not _valid_db_url(url):
        problems.append(f"Database URL is not valid: {redact_url(url)}")
    if not url and not env.get("CLINESTRA_DB"):
        warnings.append("No database is set, so data stays in memory")
    if "CLINESTRA_MASTER_KEY" in env and not _valid_master_key(env["CLINESTRA_MASTER_KEY"]):
        problems.append("CLINESTRA_MASTER_KEY must be a Fernet key (32 url-safe base64 bytes)")
    if "CLINESTRA_KEY_PEPPER" in env and len(env["CLINESTRA_KEY_PEPPER"]) < 16:
        problems.append("CLINESTRA_KEY_PEPPER must be at least 16 characters")
    keys = env.get("CLINESTRA_API_KEYS")
    if keys and not (path.parent / keys).is_file() and not Path(keys).is_file():
        problems.append(f"CLINESTRA_API_KEYS points to a missing file: {keys}")
    if env.get("CLINESTRA_MODE") == "production":
        for name in ("CLINESTRA_KEY_PEPPER", "CLINESTRA_MASTER_KEY", "CLINESTRA_API_KEYS"):
            if not env.get(name):
                problems.append(f"Production needs {name}")
        if env.get("CLINESTRA_DEV_AUTH"):
            problems.append("Remove CLINESTRA_DEV_AUTH in production")
    for name in env:
        if name.startswith("CLINESTRA_") and name not in KNOWN:
            warnings.append(f"Unknown setting {name}; check the spelling")
    if args.json:
        _dump({"ok": not problems, "file": str(path), "problems": problems, "warnings": warnings})
    else:
        print(f"{path}: {len(problems)} problems, {len(warnings)} warnings")
        for p in problems:
            print(f"  error: {p}")
        for w in warnings:
            print(f"  warning: {w}")
    return EXIT_OK if not problems else EXIT_FAIL


def _config(args):
    path = _env_path(args)
    if getattr(args, "env_file", None) and not path.is_file():
        raise Stop(f"No env file at {path}", EXIT_USAGE)
    from_file = _read_env(path) if path.is_file() else {}
    rows = []
    for name, default, description in SETTINGS:
        if name in os.environ:
            value, source = os.environ[name], "environment"
        elif name in from_file:
            value, source = from_file[name], str(path)
        elif default is not None:
            value, source = default, "default"
        else:
            continue
        rows.append({"name": name, "value": _shown(name, value), "source": source, "description": description})
    if args.json:
        _dump(rows)
    else:
        for r in rows:
            print(f"{r['name']:34} {str(r['value']):44} {r['source']}")


# ── database ──
def _need_url(args) -> str:
    _load(args)
    url = _db_url()
    if not url:
        raise Stop("No database configured. Set CLINESTRA_DATABASE_URL in .env, or run: clinestra init", EXIT_USAGE)
    return url


def _db_url_cmd(args):
    url = _need_url(args)
    if args.json:
        _dump({"url": redact_url(url), "kind": _db_kind(url)})
    else:
        print(redact_url(url))


def _db_check(args):
    url = _need_url(args)
    _require_existing_sqlite(url)
    _open_store(url).ping()
    if args.json:
        _dump({"ok": True, "kind": _db_kind(url), "url": redact_url(url)})
    else:
        print(f"Connected to {_db_kind(url)}: {redact_url(url)}")


def _db_migrate(args):
    url = _need_url(args)
    _open_store(url).migrate()
    if args.json:
        _dump({"ok": True, "kind": _db_kind(url), "url": redact_url(url)})
    else:
        print(f"Database is up to date: {redact_url(url)}")


def _db_info(args):
    url = _need_url(args)
    _require_existing_sqlite(url)
    _open_store(url).ping()
    counts = _counts(url)
    if args.json:
        _dump({"kind": _db_kind(url), "url": redact_url(url), "total": sum(counts.values()), "counts": counts})
        return
    print(f"{_db_kind(url)}: {redact_url(url)}")
    print(f"{sum(counts.values())} records")
    for kind, n in counts.items():
        print(f"  {kind:28} {n}")


def _rows(url: str) -> list:
    """Every stored record as (kind, doc), in the order it was written."""
    path = _sqlite_path(url)
    if path:
        con = sqlite3.connect(path)
        try:
            return [(k, json.loads(d)) for k, d in con.execute("SELECT kind, data FROM docs ORDER BY rowid")]
        finally:
            con.close()
    import psycopg
    with psycopg.connect(url) as con:
        return [(k, json.loads(d) if isinstance(d, str) else d)
                for k, d in con.execute("SELECT kind, data FROM docs ORDER BY seq")]


def _timelines(store, patients) -> dict:
    from .base import Actor
    from .harness import Harness
    h = Harness(store=store, packs=())
    out = {}
    for doc in patients:
        actor = Actor(id="cli_copy", site=doc["site"], role="clinician")
        try:
            out[doc["id"]] = bool(h.verify_timeline(actor, doc["id"])["valid"])
        except Exception:
            out[doc["id"]] = False
    return out


def _db_copy(args):
    src, dst = _as_url(args.source), _as_url(args.target)
    if src == dst:
        raise Stop("The source and target are the same database.", EXIT_USAGE)
    _require_existing_sqlite(src)
    source = _open_store(src)
    target = _open_store(dst)
    target.migrate()
    existing = sum(_counts(dst).values())
    if existing:
        raise Stop(f"The target already holds {existing} records. Copy only into an empty database: {redact_url(dst)}")
    rows = _rows(src)
    for kind, doc in rows:
        target.put(kind, doc)
    before, after = _counts(src), _counts(dst)
    patients = [d for k, d in rows if k == "patient"]
    src_ok, dst_ok = _timelines(source, patients), _timelines(target, patients)
    damaged = sorted(i for i, ok in dst_ok.items() if not ok and src_ok[i])
    already = sorted(i for i, ok in src_ok.items() if not ok)
    ok = before == after and not damaged
    result = {"ok": ok, "from": redact_url(src), "to": redact_url(dst), "records": sum(after.values()),
              "counts_match": before == after, "timelines_checked": len(patients),
              "timelines_damaged_by_copy": damaged, "timelines_already_broken_in_source": already}
    if args.json:
        _dump(result)
    else:
        print(f"Copied {result['records']} records from {result['from']} to {result['to']}")
        print(f"  record counts   {'match' if before == after else 'DO NOT MATCH'}")
        print(f"  timelines       {len(patients)} checked, {len(damaged)} damaged by the copy")
        if already:
            print(f"  warning: {len(already)} timelines were already broken in the source: {', '.join(already)}")
        if not ok:
            print("The copy failed verification. Do not use the target; empty it before trying again.")
    return EXIT_OK if ok else EXIT_FAIL


# ── keys ──
def _keys_import(args):
    _load(args)
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get("format") == HASHED_FORMAT:
        sys.exit("That file is already hashed.")
    pepper = os.environ.get("CLINESTRA_KEY_PEPPER") or DEV_PEPPER
    entries = [_key_entry(raw, actor, pepper) for raw, actor in data.items()]
    for e in entries:
        e["created_at"] = None
    short = [raw for raw in data if len(raw) < 32]
    _write_private(Path(args.out), json.dumps({"format": HASHED_FORMAT, "keys": entries}, indent=2))
    print(f"Wrote {len(entries)} hashed keys to {args.out}.")
    print("Set CLINESTRA_KEY_PEPPER to the pepper used for this import, then delete the plaintext file.")
    if short:
        print(f"Warning: {len(short)} keys are shorter than 32 characters. Rotate them.")


def _keys_generate(_args):
    print(_new_key())


def _keys_create(args):
    from .base import ROLES
    _load(args)
    if args.role not in ROLES:
        raise Stop(f"Unknown role '{args.role}'. Roles: {', '.join(ROLES)}", EXIT_USAGE)
    path = _keys_path(args)
    data = _read_keys(path) if path.exists() else {"format": HASHED_FORMAT, "keys": []}
    expires = _expiry(args.expires)
    raw = _new_key()
    entry = _key_entry(raw, {"id": args.id, "site": args.site, "role": args.role}, _pepper(), expires)
    data["keys"].append(entry)
    _write_private(path, json.dumps(data, indent=2) + "\n")
    print(f"Created {entry['key_id']} for {args.id} ({args.role}, {args.site}). It is shown once:\n\n  {raw}\n")


def _keys_list(args):
    _load(args)
    path = _keys_path(args)
    if not path.is_file():
        raise Stop(f"No keys file at {path}. Create one with: clinestra init", EXIT_USAGE)
    rows = [{"key_id": e["key_id"], "prefix": e.get("prefix"), **{k: e["actor"].get(k) for k in ("id", "site", "role")},
             "created_at": e.get("created_at"), "expires_at": e.get("expires_at"), "revoked": bool(e.get("revoked"))}
            for e in _read_keys(path)["keys"]]
    if args.json:
        _dump(rows)
        return
    for r in rows:
        state = "revoked" if r["revoked"] else "active"
        print(f"{r['key_id']:22} {r['prefix']:8} {r['id']:18} {r['role']:12} {r['site']:12} {state}")


def _keys_revoke(args):
    _load(args)
    path = _keys_path(args)
    if not path.is_file():
        raise Stop(f"No keys file at {path}", EXIT_USAGE)
    data = _read_keys(path)
    entry = next((e for e in data["keys"] if e["key_id"] == args.key_id), None)
    if not entry:
        raise Stop(f"No key with id {args.key_id}. See: clinestra keys list")
    entry["revoked"] = True
    _write_private(path, json.dumps(data, indent=2) + "\n")
    print(f"Revoked {args.key_id}.")


# ── packs ──
def _packs(args):
    from .packs_loader import builtin_packs, load_pack
    rows = [{**load_pack(p).summary(), "source": "built-in"} for p in builtin_packs()]
    local = Path("packs")
    if local.is_dir():
        rows += [{**load_pack(str(d)).summary(), "source": str(d)} for d in sorted(local.iterdir()) if (d / "pack.json").exists()]
    if args.json:
        _dump(rows)
        return
    for s in rows:
        print(f"{s['pack_id']:16} {s['version']:8} {sum(len(v) for v in s['definitions'].values()):4} definitions  {s['name']}  ({s['source']})")


def _pack_info(args):
    from .packs_loader import load_pack
    summary = load_pack(_resolve_pack(args.pack)).summary()
    if args.json:
        _dump(summary)
        return
    print(f"{summary['pack_id']} {summary['version']}: {summary['name']}")
    print(f"Licence: {summary['licence']}")
    for kind, keys in summary["definitions"].items():
        print(f"  {kind:20} {', '.join(keys)}")


def _pack_install(args):
    from .packs_loader import builtin_packs, load_pack
    source = Path(args.path)
    if args.path in builtin_packs() and not (source / "pack.json").is_file():
        # Built-in packs ship with the library, so enabling one is all an install needs.
        if not args.enable:
            print(f"{args.path} is built in. Enable it with: clinestra pack install {args.path} --enable")
            return EXIT_OK
        path = _env_path(args)
        if not path.is_file():
            raise Stop(f"No env file at {path} to enable the pack in.", EXIT_USAGE)
        _enable_pack(path, args.path)
        print(f"Added {args.path} to CLINESTRA_PACKS in {path}. Restart the server to load it.")
        return EXIT_OK
    if not (source / "pack.json").is_file():
        raise Stop(f"No pack.json in {source}")
    pack = load_pack(str(source))
    if _validate_definitions(pack):
        raise Stop(f"The pack has errors. See: clinestra pack validate {source}")
    target = Path(args.packs_dir) / pack.pack_id
    if target.exists():
        if not args.force:
            raise Stop(f"{target} already exists. Pass --force to replace it.")
        shutil.rmtree(str(target))
    names = ["pack.json", *pack.manifest.get("definitions", [])]
    if any(Path(n).is_absolute() or ".." in Path(n).parts for n in names):
        raise Stop("The pack manifest lists a file outside the pack folder.")
    for name in names:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source / name), str(dest))
    print(f"Installed {pack.pack_id} {pack.version} into {target}.")
    if args.enable:
        path = _env_path(args)
        if not path.is_file():
            raise Stop(f"No env file at {path} to enable the pack in.", EXIT_USAGE)
        _enable_pack(path, target.as_posix())
        print(f"Added it to CLINESTRA_PACKS in {path}.")
    else:
        print(f"Load it with: clinestra serve --packs core,{target.as_posix()}")


def _enable_pack(path: Path, entry: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("CLINESTRA_PACKS="):
            packs = [p for p in line.split("=", 1)[1].strip("\"' ").split(",") if p]
            if entry not in packs:
                packs.append(entry)
            lines[i] = "CLINESTRA_PACKS=" + ",".join(packs)
            break
    else:
        lines.append(f"CLINESTRA_PACKS=core,{entry}")
    _write_private(path, "\n".join(lines) + "\n")


def _validate_definitions(pack) -> list:
    from .harness import Harness
    errors, seen = [], set()
    for d in pack.definitions:
        ref = f"{d['def_kind']}:{d['key']}@{d.get('version', 1)}"
        if ref in seen:
            errors.append(f"{ref}: defined twice")
        seen.add(ref)
        try:
            Harness._validate_config(d["def_kind"], d.get("config") or {})
        except Exception as exc:
            errors.append(f"{ref}: {exc}")
    return errors


def _pack_validate(args):
    from .harness import Harness
    from .packs_loader import load_pack
    source = _resolve_pack(args.pack)
    pack = load_pack(source)
    errors = _validate_definitions(pack)
    if not errors:
        try:
            Harness(packs=("core", source) if pack.pack_id != "core" else ("core",))
        except Exception as exc:
            errors.append(f"Loading failed: {exc}")
    unsummed = [n for n in pack.manifest.get("definitions", []) if n not in (pack.manifest.get("checksums") or {})]
    result = {"ok": not errors, "pack_id": pack.pack_id, "version": pack.version,
              "definitions": len(pack.definitions), "errors": errors, "files_without_checksum": unsummed}
    if args.json:
        _dump(result)
    else:
        print(f"{pack.pack_id} {pack.version}: {len(pack.definitions)} definitions, {len(errors)} errors")
        for e in errors:
            print(f"  error: {e}")
        if unsummed:
            print(f"  note: {len(unsummed)} files have no checksum")
    return EXIT_OK if not errors else EXIT_FAIL


def _pack_new(args):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.pack_id):
        sys.exit("The pack id must be lowercase letters, digits and underscores, starting with a letter.")
    folder = Path(args.dir or args.pack_id)
    if folder.exists() and any(folder.iterdir()):
        sys.exit(f"{folder} already exists and is not empty.")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"task_{args.pack_id}_follow_up.json"
    manifest = {"pack_id": args.pack_id, "name": args.pack_id.replace("_", " ").title(), "version": "0.1.0",
                "licence": "CC-BY-4.0", "definitions": [name]}
    definition = {"def_kind": "task", "key": f"{args.pack_id}_follow_up", "version": 1, "name": "Follow-up", "config": {}}
    (folder / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (folder / name).write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    print(f"Created {folder}. Next:\n  clinestra pack validate {folder}\n  clinestra serve --dev --packs core,{folder}")


def _review_sheet(args):
    from .pack_review import apply_review, write_sheet
    from .packs_loader import load_pack
    if args.apply:
        print(json.dumps(apply_review(args.pack, args.apply)))
        return
    if not args.out:
        sys.exit("Pass --out to write a review sheet.")
    pack = load_pack(args.pack)
    count = write_sheet(pack.definitions, args.out)
    print(f"Wrote {count} rows to {args.out}")


# ── audit and doctor ──
def _audit_verify(args):
    from .base import Actor
    from .harness import Harness
    _load(args)
    url = _as_url(args.db) if args.db else _db_url()
    if not url:
        raise Stop("Pass --db, or set CLINESTRA_DATABASE_URL.", EXIT_USAGE)
    _require_existing_sqlite(url)
    h = Harness(store=_open_store(url), packs=())
    actor = Actor(id="cli_audit", site=args.site, role="clinician")
    ids = [args.patient] if args.patient else [d["id"] for d in h.store.list("patient", args.site)]
    results = [{"patient_id": i, **h.verify_timeline(actor, i)} for i in ids]
    broken = [r for r in results if not r["valid"]]
    if args.json:
        _dump({"ok": not broken, "checked": len(results), "broken": broken})
    else:
        print(f"Checked {len(results)} patient timelines in site {args.site}: {len(broken)} broken")
        for r in broken:
            print(f"  {r['patient_id']}: broken at {r['broken_at']}")
    return EXIT_OK if not broken else EXIT_FAIL


def _doctor(args):
    _load(args)
    checks = []

    def add(name, ok, detail="", warn=False, fix=""):
        checks.append({"check": name, "status": "ok" if ok else ("warn" if warn else "fail"),
                       "detail": detail, "fix": "" if ok else fix})

    prod = args.production or os.environ.get("CLINESTRA_MODE") == "production"
    env = os.environ.get
    add("python", sys.version_info >= (3, 9), platform.python_version(), fix="Install Python 3.9 or later")
    for mod, extra in (("fastapi", "server"), ("uvicorn", "server"), ("anthropic", "ai"), ("cryptography", "ai")):
        found = importlib.util.find_spec(mod) is not None
        add(f"module {mod}", found, "" if found else "not installed", warn=True, fix=f'pip install "clinestra[{extra}]"')
    add("admin separation of duties", env("CLINESTRA_ADMIN_ALL_ROLES", "0") not in ("1", "true", "on"),
        "CLINESTRA_ADMIN_ALL_ROLES is on", warn=not prod, fix="Remove CLINESTRA_ADMIN_ALL_ROLES")
    add("development auth off", not env("CLINESTRA_DEV_AUTH"), "CLINESTRA_DEV_AUTH is set", warn=not prod,
        fix="Remove CLINESTRA_DEV_AUTH")
    if prod:
        add("master key", bool(env("CLINESTRA_MASTER_KEY")), "not set", fix="clinestra init, or set CLINESTRA_MASTER_KEY from a secrets manager")
        add("production mode", env("CLINESTRA_MODE") == "production", "not set", fix="Set CLINESTRA_MODE=production in .env")
        add("key pepper", bool(env("CLINESTRA_KEY_PEPPER")), "not set", fix="clinestra init, or set CLINESTRA_KEY_PEPPER")
    db_url = None
    if args.db and _sqlite_path(_as_url(args.db)):
        db_path = _sqlite_path(_as_url(args.db))
        try:
            if not Path(db_path).is_file():
                raise OSError("file not found")
            con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
            res = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
            add("database", res == "ok", res, fix="Restore the database from a backup")
        except (OSError, sqlite3.Error) as exc:
            add("database", False, str(exc), fix="Check the --db path")
    else:
        db_url = _as_url(args.db) if args.db else _doctor_url()
        if db_url:
            try:
                _require_existing_sqlite(db_url)
                _open_store(db_url).ping()
                add("database", True, redact_url(db_url))
            except Exception as exc:
                add("database", False, redact_url(exc), fix="clinestra db migrate, and check CLINESTRA_DATABASE_URL")
            if prod and _sqlite_path(db_url):
                add("production database", False, "SQLite", warn=True, fix="Use Postgres: set CLINESTRA_DATABASE_URL")
        else:
            add("database", False, "none set, so data stays in memory", warn=not prod, fix="clinestra init, or set CLINESTRA_DATABASE_URL")
    keys = args.keys or env("CLINESTRA_API_KEYS")
    if keys:
        try:
            data = json.loads(Path(keys).read_text(encoding="utf-8"))
            hashed = isinstance(data, dict) and "format" in data and "keys" in data
            add("API keys hashed", hashed, "plaintext", warn=not prod, fix=f"clinestra keys import --file {keys} --out keys.hashed.json")
        except (OSError, ValueError) as exc:
            add("API keys", False, str(exc), fix="clinestra init, or fix CLINESTRA_API_KEYS")
    elif prod:
        add("API keys", False, "no keys file", fix="clinestra init, or set CLINESTRA_API_KEYS")
    packs = args.packs or env("CLINESTRA_PACKS")
    if packs:
        from .packs_loader import load_pack
        for name in filter(None, packs.split(",")):
            try:
                add(f"pack {name}", True, load_pack(name).version)
            except Exception as exc:
                add(f"pack {name}", False, str(exc), fix="clinestra packs")
    failed = sum(c["status"] == "fail" for c in checks)
    if args.json:
        _dump({"version": __version__, "ok": not failed, "checks": checks})
    else:
        print(f"Clinestra {__version__}")
        for c in checks:
            shown = "" if c["status"] == "ok" and c["check"].startswith(("module", "admin", "development", "API keys hashed")) else c["detail"]
            print(f"  [{c['status']:4}] {c['check']}" + (f": {shown}" if shown else ""))
            if c["fix"]:
                print(f"         fix: {c['fix']}")
        print("Problems found." if failed else "No blocking problems.")
    return EXIT_FAIL if failed else EXIT_OK


def _doctor_url():
    try:
        return _db_url()
    except ImportError:
        return None


# ── parser ──
def _build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    env_p = argparse.ArgumentParser(add_help=False)
    env_p.add_argument("--env-file", metavar="PATH", help="Env file to read (default: .env)")
    json_p = argparse.ArgumentParser(add_help=False)
    json_p.add_argument("--json", action="store_true", help="Print JSON")

    def cmd(group, name, summary, fn=None, parents=(), examples=()):
        epilog = "examples:\n" + "\n".join("  " + e for e in examples) if examples else None
        p = group.add_parser(name, help=summary, description=summary, parents=list(parents), epilog=epilog, formatter_class=raw)
        if fn:
            p.set_defaults(fn=fn)
        return p

    ap = argparse.ArgumentParser(
        prog="clinestra", description="Clinestra: a clinical system of record for longitudinal, AI-assisted care.",
        formatter_class=raw,
        epilog="""get started:
  clinestra init                                   create .env, keys and a first admin key
  clinestra doctor                                 check the setup
  clinestra db migrate                             prepare the database
  clinestra serve                                  run the API

exit codes: 0 success, 1 a check or action failed, 2 usage or missing configuration""")
    ap.add_argument("-V", "--version", action="version", version=f"clinestra {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    p = cmd(sub, "init", "Create .env, a hashed keys file and the first admin key", _init, examples=(
        "clinestra init",
        "clinestra init --yes --database sqlite",
        "clinestra init --yes --database-url postgresql://user:pass@db:5432/clinestra --mode production"))
    p.add_argument("--dir", default=".", help="Project folder (default: current folder)")
    p.add_argument("--yes", action="store_true", help="Ask nothing; use options and defaults")
    p.add_argument("--force", action="store_true", help="Replace existing files; existing API keys stop working")
    p.add_argument("--mode", choices=("pilot", "production"))
    p.add_argument("--database", choices=("sqlite", "postgres"))
    p.add_argument("--database-url", metavar="URL", help="Connection URL of your database")
    p.add_argument("--sqlite-path", metavar="PATH", help="SQLite file (default: clinestra.db)")
    p.add_argument("--packs", help="Comma-separated packs (default: core)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--cors-origin", action="append", default=[], metavar="URL", help="Allowed browser origin (repeatable)")
    p.add_argument("--site", help="Site id of the first admin (default: main)")
    p.add_argument("--admin-id", help="Id of the first admin (default: admin)")
    p.add_argument("--env-name", default=".env", help=argparse.SUPPRESS)
    p.add_argument("--keys-name", default="keys.json", help=argparse.SUPPRESS)

    p = cmd(sub, "serve", "Run the Clinical API server; reads .env", _serve, [env_p], (
        "clinestra serve", "clinestra serve --dev --db care.db", "clinestra serve --env-file prod.env --port 9000"))
    p.add_argument("--host", help="Address to bind (default: CLINESTRA_HOST or 127.0.0.1)")
    p.add_argument("--port", type=int, help="Port (default: CLINESTRA_PORT or 8000)")
    p.add_argument("--db", help="SQLite file to persist to")
    p.add_argument("--database-url", metavar="URL", help="Database URL; overrides .env")
    p.add_argument("--packs", help="Comma-separated packs: built-in names or folder paths (default: core)")
    p.add_argument("--keys", help="JSON file of API keys")
    p.add_argument("--dev", action="store_true", help="Trust X-Actor-* headers (local development only)")
    p.add_argument("--reload", action="store_true", help="Restart when library code changes (development)")
    p.add_argument("--mode", choices=("pilot", "production"), help="pilot allows unapproved content; production does not")
    p.add_argument("--allow-unreviewed-packs", action="store_true", help="Health reports unreviewed_content; pack load stays allowed")
    p.add_argument("--cors-origin", action="append", default=[], metavar="URL",
                   help="Browser origin allowed to call the API, e.g. http://localhost:5173 (repeatable)")

    cmd(sub, "demo", "Walk one patient through the whole care structure in the terminal", _demo)

    p = cmd(sub, "doctor", "Check the install and deployment, with fixes (read-only); reads .env", _doctor, [env_p, json_p],
            ("clinestra doctor", "clinestra doctor --production"))
    p.add_argument("--production", action="store_true", help="Treat unsafe settings as failures")
    p.add_argument("--db", help="SQLite file to check instead of the configured database")
    p.add_argument("--keys", help="API keys file to check")
    p.add_argument("--packs", help="Comma-separated packs to load")

    cmd(sub, "config", "Show effective settings and where each came from (secrets hidden)", _config, [env_p, json_p],
        ("clinestra config", "clinestra config --json"))

    env = cmd(sub, "env", "Inspect the .env file (secrets hidden)")
    group = env.add_subparsers(dest="env_cmd", required=True, metavar="action")
    cmd(group, "show", "Print the .env file with secrets and passwords hidden", _env_show, [env_p, json_p], ("clinestra env show",))
    cmd(group, "check", "Validate the .env file; exit 1 on problems", _env_check, [env_p, json_p],
        ("clinestra env check", "clinestra env check --env-file prod.env"))

    db = cmd(sub, "db", "Check and prepare the database (SQLite or Postgres)")
    group = db.add_subparsers(dest="db_cmd", required=True, metavar="action")
    cmd(group, "url", "Print the configured database URL with the password hidden", _db_url_cmd, [env_p, json_p], ("clinestra db url",))
    cmd(group, "check", "Test the connection; exit 1 if it fails", _db_check, [env_p, json_p], ("clinestra db check",))
    cmd(group, "migrate", "Create or update the database tables", _db_migrate, [env_p, json_p], ("clinestra db migrate",))
    p = cmd(group, "copy", "Copy every record into an empty database, then verify it", _db_copy, [json_p],
            ("clinestra db copy --from care.db --to postgresql://user:pass@host/clinestra",))
    p.add_argument("--from", dest="source", required=True, metavar="URL", help="Source URL, or a SQLite file path")
    p.add_argument("--to", dest="target", required=True, metavar="URL", help="Target URL, or a SQLite file path. Must be empty.")
    cmd(group, "info", "Show the database type and record counts", _db_info, [env_p, json_p], ("clinestra db info --json",))

    keys = cmd(sub, "keys", "Create, list, revoke, hash or generate API keys")
    group = keys.add_subparsers(dest="keys_cmd", required=True, metavar="action")
    file_help = "Keys file (default: CLINESTRA_API_KEYS)"
    p = cmd(group, "create", "Add a key for one person or service (shown once)", _keys_create, [env_p],
            ("clinestra keys create --id dr_rao --site clinic_1 --role clinician", "clinestra keys create --id lab --site clinic_1 --role system --expires 90d"))
    p.add_argument("--id", required=True, help="Actor id")
    p.add_argument("--site", required=True)
    p.add_argument("--role", required=True, help="clinician, nurse, chw, coordinator, admin, clinical_lead, patient or system")
    p.add_argument("--expires", help="When the key stops working: 90d or an ISO date")
    p.add_argument("--file", help=file_help)
    p = cmd(group, "list", "List keys without their secrets", _keys_list, [env_p, json_p], ("clinestra keys list",))
    p.add_argument("--file", help=file_help)
    p = cmd(group, "revoke", "Revoke a key by id", _keys_revoke, [env_p], ("clinestra keys revoke key_0123456789abcdef",))
    p.add_argument("key_id")
    p.add_argument("--file", help=file_help)
    p = cmd(group, "import", "Hash a plaintext keys file. Does not delete the source.", _keys_import, [env_p])
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    cmd(group, "generate", "Print one new random key without storing it (shown once)", _keys_generate)

    cmd(sub, "packs", "List built-in and project packs", _packs, [json_p])
    pack = cmd(sub, "pack", "Create, validate, install and review packs")
    group = pack.add_subparsers(dest="pack_cmd", required=True, metavar="action")
    cmd(group, "list", "List built-in and project packs", _packs, [json_p])
    p = cmd(group, "info", "Show what a pack contains", _pack_info, [json_p], ("clinestra pack info core",))
    p.add_argument("pack", help="Folder, project pack or built-in name")
    p = cmd(group, "new", "Create a starter pack folder", _pack_new, examples=("clinestra pack new htn",))
    p.add_argument("pack_id")
    p.add_argument("--dir", help="Folder to create (default: the pack id)")
    p = cmd(group, "validate", "Check a pack's definitions; exit 1 on errors", _pack_validate, [json_p], ("clinestra pack validate ./htn",))
    p.add_argument("pack", help="Folder or built-in pack name")
    p = cmd(group, "install", "Validate a pack and copy it into this project's packs folder", _pack_install, [env_p],
            ("clinestra pack install ./htn --enable",))
    p.add_argument("path", help="Pack folder, or the name of a built-in pack")
    p.add_argument("--packs-dir", default="packs", help="Destination (default: packs)")
    p.add_argument("--enable", action="store_true", help="Add it to CLINESTRA_PACKS in .env")
    p.add_argument("--force", action="store_true", help="Replace an installed copy")
    p = cmd(group, "review-sheet", "Write a review CSV. Does not change clinical values.", _review_sheet)
    p.add_argument("pack")
    p.add_argument("--out", help="CSV to write")
    p.add_argument("--apply", help="CSV of decisions to record. Never edits config.")

    p = cmd(sub, "concepts", "List the concepts and their states; name one to see its fields", _concepts, [json_p],
            ("clinestra concepts", "clinestra concepts patient", 'clinestra concepts --group "People"'))
    p.add_argument("kind", nargs="?")
    p.add_argument("--group", help='Only this group, e.g. "People"')
    p = cmd(sub, "schema", "Print the config schema of a definition kind", _schema, examples=("clinestra schema cohort",))
    p.add_argument("kind", help="e.g. cohort, protocol, note_page")

    audit = cmd(sub, "audit", "Verify the hash-chained patient timelines")
    group = audit.add_subparsers(dest="audit_cmd", required=True, metavar="action")
    p = cmd(group, "verify", "Exit 1 if any timeline has been altered", _audit_verify, [env_p, json_p],
            ("clinestra audit verify --site clinic_1",))
    p.add_argument("--db", help="Database URL or SQLite file (default: the configured database)")
    p.add_argument("--site", required=True)
    p.add_argument("--patient", help="One patient id (default: every patient in the site)")

    cmd(sub, "version", "Print the version", lambda a: print(__version__))
    return ap


def main(argv=None):
    args = _build_parser().parse_args(argv)
    from .errors import ClinestraError
    try:
        if args.fn not in (_init, _config, _env_show, _env_check):  # these read the file itself, or create it
            _load(args)
        return args.fn(args) or EXIT_OK
    except Stop as exc:
        print(f"error: {redact_url(exc)}", file=sys.stderr)
        return exc.code
    except ImportError as exc:
        print(f"error: a required package is missing: {redact_url(exc)}", file=sys.stderr)
        return EXIT_FAIL
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return EXIT_FAIL
    except SystemExit:
        raise
    except (ClinestraError, OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {redact_url(exc)}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # database drivers raise their own types
        print(f"error: {type(exc).__name__}: {redact_url(exc)}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
