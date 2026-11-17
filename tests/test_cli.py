import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clinestra.cli import main


def test_pack_new_then_validate(tmp_path, capsys):
    d = tmp_path / "htn"
    assert main(["pack", "new", "htn", "--dir", str(d)]) == 0
    capsys.readouterr()
    assert main(["pack", "validate", str(d), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_validate_reports_bad_config(tmp_path):
    (tmp_path / "pack.json").write_text(json.dumps({"pack_id": "bad", "version": "1", "definitions": ["d.json"]}))
    (tmp_path / "d.json").write_text(json.dumps({"def_kind": "nonsense", "key": "x", "version": 1, "config": {}}))
    assert main(["pack", "validate", str(tmp_path)]) == 1


def test_dev_refused_on_public_host():
    with pytest.raises(SystemExit, match="Refusing"):
        main(["serve", "--dev", "--host", "0.0.0.0"])


def test_doctor_production_fails_without_secrets(monkeypatch, capsys):
    monkeypatch.delenv("CLINESTRA_MASTER_KEY", raising=False)
    assert main(["doctor", "--production", "--json"]) == 1
    assert main(["doctor"]) == 0


def test_audit_verify_detects_tampering(tmp_path, capsys):
    import sqlite3
    from clinestra import Actor, Harness, SQLiteStore
    db = str(tmp_path / "c.db")
    dr = Actor(id="d", site="s", role="clinician")
    p = Harness(store=SQLiteStore(db), packs=("core",)).create(dr, "patient", {"name": "A", "country": "IN", "gender": "other", "age_years": 40})
    assert main(["audit", "verify", "--db", db, "--site", "s"]) == 0
    con = sqlite3.connect(db)
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    assert "docs" in tables
    rows = con.execute("select rowid, data from docs where patient_id=? and kind not in ('patient')", (p.id,)).fetchall()
    assert rows
    for rowid, data in rows:
        con.execute("update docs set data=? where rowid=?", (data.replace('"patient.', '"tampered.').replace('"d"', '"x"'), rowid))
    con.commit(); con.close()
    assert main(["audit", "verify", "--db", db, "--site", "s"]) == 1


def test_errors_are_clean(capsys):
    assert main(["concepts", "nope"]) == 1
    assert "Unknown concept" in capsys.readouterr().err


def test_demo_survives_a_cp1252_console(monkeypatch):
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict", line_buffering=True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    assert main(["demo"]) == 0
    stream.flush()
    text = buf.getvalue().decode("utf-8")
    assert "People" in text
    assert "Timeline valid" in text


def test_demo_cli_survives_cp1252_subprocess():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "0"
    env["PYTHONIOENCODING"] = "cp1252:strict"
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "clinestra.cli", "demo"], capture_output=True, env=env, cwd=root)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "People" in proc.stdout.decode("utf-8")


def test_license_is_the_full_agpl():
    text = (Path(__file__).resolve().parents[1] / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    assert "Copyright (C) the Clinestra contributors" in text


# ── setup commands ──
PG = "postgresql://clin:s3cret-pw@db.example:5432/care"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary project folder; anything loaded from .env is removed afterwards."""
    import os
    before = dict(os.environ)
    for name in [k for k in os.environ if k.startswith("CLINESTRA_") or k == "DATABASE_URL"]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    os.environ.clear()
    os.environ.update(before)


def _init(capsys, *extra):
    assert main(["init", "--yes", *extra]) == 0
    out = capsys.readouterr().out
    return next(line.strip() for line in out.splitlines() if line.startswith("  ") and len(line.strip()) >= 40)


def test_init_key_authenticates_with_server_code(project, capsys):
    from clinestra import cli
    from clinestra.server.auth import HASHED_FORMAT, APIKeyAuth, hash_api_key
    from clinestra.settings import load_env
    assert cli.HASHED_FORMAT == HASHED_FORMAT and cli._hash_key("abc", "p") == hash_api_key("abc", "p")
    raw = _init(capsys, "--site", "c1", "--admin-id", "root")
    load_env(".env")
    actor = APIKeyAuth.from_file("keys.json")._match(raw)
    assert actor.id == "root" and actor.site == "c1" and actor.role == "admin"
    assert raw not in (project / ".env").read_text() and raw not in (project / "keys.json").read_text()


def test_init_writes_valid_master_key(project, capsys):
    from cryptography.fernet import Fernet
    from clinestra.settings import load_env
    _init(capsys)
    Fernet(load_env(".env")["CLINESTRA_MASTER_KEY"].encode())
    assert main(["env", "check"]) == 0


def test_init_refuses_to_overwrite_without_force(project, capsys):
    _init(capsys)
    first = (project / ".env").read_text()
    assert main(["init", "--yes"]) == 1
    assert (project / ".env").read_text() == first
    assert main(["init", "--yes", "--force"]) == 0
    assert (project / ".env").read_text() != first


def test_init_postgres_requires_a_real_url(project, capsys):
    assert main(["init", "--yes", "--database", "postgres"]) == 2
    assert main(["init", "--yes", "--database", "postgres", "--database-url", "mysql://u:hideme@h/d"]) == 2
    assert "hideme" not in capsys.readouterr().err
    assert not (project / ".env").exists()


def test_init_without_terminal_needs_yes(project, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["init"]) == 2


def test_postgres_url_with_awkward_characters_round_trips(project, capsys):
    from clinestra.settings import load_env
    url = "postgresql://u:a#b c@h:5432/d"
    _init(capsys, "--database", "postgres", "--database-url", url)
    assert load_env(".env")["CLINESTRA_DATABASE_URL"] == url


def test_init_updates_gitignore_once(project, capsys):
    (project / ".git").mkdir()
    (project / ".gitignore").write_text("node_modules")
    _init(capsys)
    _init(capsys, "--force")
    lines = (project / ".gitignore").read_text().splitlines()
    assert lines.count(".env") == 1 and lines.count("keys.json") == 1 and "node_modules" in lines


def test_env_show_and_config_hide_secrets(project, capsys):
    (project / ".env").write_text(f"CLINESTRA_DATABASE_URL={PG}\nCLINESTRA_KEY_PEPPER=pepper-value-123456\nCLINESTRA_MODE=pilot\n")
    assert main(["env", "show", "--json"]) == 0
    assert main(["config", "--json"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out and "pepper-value" not in out and "db.example" in out


def test_config_reports_sources(project, capsys, monkeypatch):
    (project / ".env").write_text("CLINESTRA_PACKS=core,x\nCLINESTRA_PORT=1\n")
    monkeypatch.setenv("CLINESTRA_PORT", "2")
    assert main(["config", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["CLINESTRA_PACKS"]["source"] == ".env"
    assert rows["CLINESTRA_PORT"]["value"] == "2" and rows["CLINESTRA_PORT"]["source"] == "environment"
    assert rows["CLINESTRA_MODE"]["source"] == "default"


def test_env_check_finds_problems(project, capsys):
    (project / ".env").write_text("CLINESTRA_MODE=live\nCLINESTRA_PORT=abc\nCLINESTRA_MASTER_KEY=short\nCLINESTRA_TYPO=1\nCLINESTRA_DATABASE_URL=mysql://h/d\n")
    assert main(["env", "check", "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert len(res["problems"]) == 4 and any("CLINESTRA_TYPO" in w for w in res["warnings"])


def test_env_commands_without_file(project):
    assert main(["env", "check"]) == 2
    assert main(["env", "show", "--env-file", "nope.env"]) == 2


def test_db_sqlite_lifecycle(project, capsys):
    from clinestra import Actor, Harness, SQLiteStore
    _init(capsys)
    assert main(["db", "check"]) == 1          # not created yet, and check must not create it
    assert not (project / "clinestra.db").exists()
    assert main(["db", "migrate"]) == 0
    assert main(["db", "check"]) == 0
    dr = Actor(id="d", site="s", role="clinician")
    Harness(store=SQLiteStore("clinestra.db")).create(dr, "patient", {"name": "A", "country": "IN", "gender": "other", "age_years": 40})
    capsys.readouterr()
    assert main(["db", "info", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["kind"] == "sqlite" and info["counts"]["patient"] == 1
    assert main(["audit", "verify", "--site", "s", "--json"]) == 0


def test_db_plain_sqlite_path_from_old_setting(project, capsys, monkeypatch):
    monkeypatch.setenv("CLINESTRA_DB", "old.db")
    assert main(["db", "url"]) == 0
    assert capsys.readouterr().out.strip() == "sqlite:///old.db"


def test_db_without_configuration(project):
    assert main(["db", "check"]) == 2


class FakeStore:
    fail = False

    def ping(self):
        if self.fail:
            raise RuntimeError(f"could not connect to {PG}")
        return True

    def migrate(self):
        return None


def test_db_postgres_commands_redact(project, capsys, monkeypatch):
    monkeypatch.setattr("clinestra.storage.store_from_url", lambda url: FakeStore())
    (project / ".env").write_text(f"CLINESTRA_DATABASE_URL={PG}\n")
    assert main(["db", "url"]) == 0
    assert main(["db", "check", "--json"]) == 0
    assert main(["db", "migrate"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out and "postgresql" in out


def test_db_failure_is_one_redacted_line(project, capsys, monkeypatch):
    bad = FakeStore()
    bad.fail = True
    monkeypatch.setattr("clinestra.storage.store_from_url", lambda url: bad)
    monkeypatch.setenv("CLINESTRA_DATABASE_URL", PG)
    assert main(["db", "check"]) == 1
    err = capsys.readouterr().err
    assert "s3cret-pw" not in err and len(err.strip().splitlines()) == 1
    assert main(["doctor", "--json"]) == 1
    assert "s3cret-pw" not in capsys.readouterr().out


def test_keys_create_list_revoke(project, capsys):
    from clinestra.server.auth import APIKeyAuth
    from clinestra.settings import load_env
    _init(capsys)
    assert main(["keys", "create", "--id", "dr", "--site", "s", "--role", "clinician"]) == 0
    out = capsys.readouterr().out
    raw = next(line.strip() for line in out.splitlines() if line.startswith("  "))
    load_env(".env")
    assert APIKeyAuth.from_file("keys.json")._match(raw).id == "dr"
    assert main(["keys", "list", "--json"]) == 0
    listed = capsys.readouterr().out
    assert raw not in listed and '"hash"' not in listed
    key_id = next(k["key_id"] for k in json.loads(listed) if k["id"] == "dr")
    assert main(["keys", "revoke", key_id]) == 0
    assert APIKeyAuth.from_file("keys.json")._match(raw) is None
    assert main(["keys", "revoke", "key_missing"]) == 1


def test_keys_validation(project, capsys):
    _init(capsys)
    assert main(["keys", "create", "--id", "x", "--site", "s", "--role", "wizard"]) == 2
    assert main(["keys", "create", "--id", "x", "--site", "s", "--role", "nurse", "--expires", "soon"]) == 2
    assert main(["keys", "create", "--id", "x", "--site", "s", "--role", "nurse", "--expires", "90d"]) == 0
    capsys.readouterr()


def test_keys_refuses_plaintext_file(project):
    (project / "k.json").write_text(json.dumps({"rawkey": {"id": "a", "site": "s", "role": "admin"}}))
    assert main(["keys", "list", "--file", "k.json"]) == 1


def test_pack_install_info_list_enable(project, capsys):
    assert main(["init", "--yes"]) == 0
    assert main(["pack", "new", "htn", "--dir", "src_pack"]) == 0
    assert main(["pack", "install", "src_pack", "--enable"]) == 0
    assert (project / "packs/htn/pack.json").exists()
    assert "packs/htn" in (project / ".env").read_text()
    assert main(["pack", "install", "src_pack"]) == 1
    assert main(["pack", "install", "src_pack", "--force"]) == 0
    capsys.readouterr()
    assert main(["pack", "list", "--json"]) == 0
    assert {"core", "htn"} <= {p["pack_id"] for p in json.loads(capsys.readouterr().out)}
    assert main(["pack", "info", "htn", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pack_id"] == "htn"


def test_pack_install_rejects_paths_outside_the_pack(project):
    src = project / "evil"
    src.mkdir()
    (project / "secret.json").write_text(json.dumps({"def_kind": "task", "key": "x", "version": 1, "config": {}}))
    (src / "pack.json").write_text(json.dumps({"pack_id": "evil", "version": "1", "definitions": ["../secret.json"]}))
    assert main(["pack", "install", "evil"]) == 1
    assert not (project / "packs/evil").exists()


def test_doctor_reads_env_and_gives_fixes(project, capsys):
    _init(capsys, "--mode", "production")
    assert main(["doctor", "--json"]) == 1      # database not created yet
    fixes = [c["fix"] for c in json.loads(capsys.readouterr().out)["checks"] if c["status"] == "fail"]
    assert any("db migrate" in f for f in fixes)
    assert main(["db", "migrate"]) == 0
    assert main(["doctor"]) == 0


def test_serve_explicit_missing_env_file(project):
    assert main(["serve", "--env-file", "missing.env"]) == 2


def test_serve_summary_is_redacted(project, capsys, monkeypatch):
    import uvicorn
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append((app, kw)))
    monkeypatch.setattr("clinestra.server.create_app", lambda: object())
    (project / ".env").write_text(f"CLINESTRA_DATABASE_URL={PG}\nCLINESTRA_PORT=8177\nCLINESTRA_DEV_AUTH=1\n")
    assert main(["serve", "--reload"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out and "db.example" in out and runs[0][1]["port"] == 8177 and runs[0][1]["reload"] is True
    assert runs[0][0] == "clinestra.server:create_app" and runs[0][1]["factory"] is True


def test_every_command_has_help():
    import argparse
    from clinestra.cli import _build_parser

    def walk(p, path):
        yield path
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for name, child in a.choices.items():
                    yield from walk(child, path + [name])
    paths = list(walk(_build_parser(), []))
    assert len(paths) > 25
    for path in paths:
        with pytest.raises(SystemExit) as e:
            main(path + ["--help"])
        assert e.value.code == 0


# ── database paths, key expiry, master key ──
def _seed(path):
    from clinestra import Actor, Harness, SQLiteStore
    store = SQLiteStore(str(path))
    p = Harness(store=store, packs=("core",)).create(Actor(id="d", site="s", role="clinician"), "patient",
                                                     {"name": "A", "country": "IN", "gender": "other", "age_years": 40})
    return p.id


def test_db_copy_sqlite_to_sqlite_and_refuses_non_empty(tmp_path, capsys):
    import json as j
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _seed(a)
    assert main(["db", "copy", "--from", str(a), "--to", str(b), "--json"]) == 0
    res = j.loads(capsys.readouterr().out)
    assert res["ok"] and res["counts_match"] and res["timelines_checked"] == 1
    assert main(["db", "copy", "--from", str(a), "--to", str(b)]) == 1
    assert "already holds" in capsys.readouterr().err
    assert main(["audit", "verify", "--db", str(b), "--site", "s"]) == 0


def test_db_copy_needs_a_source(tmp_path):
    assert main(["db", "copy", "--from", str(tmp_path / "no.db"), "--to", str(tmp_path / "b.db")]) == 1


def test_doctor_db_accepts_a_sqlite_url(tmp_path, capsys):
    import json as j
    a = tmp_path / "a.db"
    _seed(a)
    main(["doctor", "--db", f"sqlite:///{a.as_posix()}", "--json"])
    assert [c for c in j.loads(capsys.readouterr().out)["checks"] if c["check"] == "database"][0]["status"] == "ok"


def test_serve_db_beats_env_database_url(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    import uvicorn
    import clinestra.server as server
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CLINESTRA_DATABASE_URL=postgresql://u:p@nowhere/x\n")
    seen = {}
    monkeypatch.setattr(server, "create_app", lambda: seen.setdefault("url", __import__("os").environ["CLINESTRA_DATABASE_URL"]))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    for name in ("CLINESTRA_DATABASE_URL", "DATABASE_URL", "CLINESTRA_DB"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    assert main(["serve", "--db", "care.db", "--dev"]) == 0
    assert seen["url"] == "sqlite:///care.db"
    import os
    for name in ("CLINESTRA_DATABASE_URL", "CLINESTRA_DB", "CLINESTRA_DEV_AUTH", "CLINESTRA_ENV"):
        os.environ.pop(name, None)


@pytest.mark.parametrize("value,expired", [(None, False), ("2999-01-01", False), ("2000-01-01", True),
                                           ("2000-01-01T00:00:00Z", True), ("2999-01-01T00:00:00+00:00", False),
                                           ("garbage", True)])
def test_key_expiry_values(value, expired):
    pytest.importorskip("fastapi")
    from clinestra.server.auth import _expired
    assert _expired(value) is expired


def test_expired_key_is_refused():
    pytest.importorskip("fastapi")
    from clinestra.server.auth import APIKeyAuth, HASHED_FORMAT, hash_api_key
    raw = "k" * 40
    entry = {"key_id": "a", "prefix": raw[:6], "hash": hash_api_key(raw), "revoked": False,
             "actor": {"id": "d", "site": "s", "role": "clinician"}}
    live = APIKeyAuth({"format": HASHED_FORMAT, "keys": [{**entry, "expires_at": "2999-01-01"}]})
    dead = APIKeyAuth({"format": HASHED_FORMAT, "keys": [{**entry, "expires_at": "2000-01-01"}]})
    assert live._match(raw) is not None and dead._match(raw) is None


def test_master_key_needs_a_home_on_postgres(tmp_path, monkeypatch):
    from clinestra import secrets as s
    monkeypatch.delenv("CLINESTRA_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("CLINESTRA_DATABASE_URL", "postgresql://u:p@h/x")
    with pytest.raises(s.SecretError, match="CLINESTRA_MASTER_KEY"):
        s._key_file()
    monkeypatch.setenv("CLINESTRA_MASTER_KEY_FILE", str(tmp_path / "m.key"))
    assert s._key_file() == tmp_path / "m.key"
    monkeypatch.delenv("CLINESTRA_MASTER_KEY_FILE")
    monkeypatch.setenv("CLINESTRA_DATABASE_URL", f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    assert s._key_file().parent == tmp_path.resolve()


def test_core_import_needs_no_server_or_driver():
    import subprocess, sys
    code = "import sys, clinestra; clinestra.PostgresStore; print([m for m in ('fastapi','psycopg','anthropic') if m in sys.modules])"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "[]"


def _pg():
    import os
    url = os.environ.get("CLINESTRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("CLINESTRA_TEST_POSTGRES_URL is not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(url, autocommit=True) as con:
        con.execute("DROP TABLE IF EXISTS docs")
    return url


def test_postgres_copy_audit_doctor(tmp_path, capsys):
    import json as j
    url = _pg()
    a = tmp_path / "a.db"
    _seed(a)
    assert main(["db", "copy", "--from", str(a), "--to", url, "--json"]) == 0
    out = capsys.readouterr().out
    res = j.loads(out)
    assert res["ok"] and res["timelines_checked"] == 1
    assert main(["db", "copy", "--from", str(a), "--to", url]) == 1
    capsys.readouterr()
    assert main(["audit", "verify", "--db", url, "--site", "s", "--json"]) == 0
    assert j.loads(capsys.readouterr().out)["checked"] == 1
    main(["doctor", "--db", url, "--json"])
    assert [c for c in j.loads(capsys.readouterr().out)["checks"] if c["check"] == "database"][0]["status"] == "ok"
    import psycopg
    with psycopg.connect(url, autocommit=True) as con:
        con.execute("DROP TABLE docs")
