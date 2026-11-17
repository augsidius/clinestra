"""Store behaviour on every backend. PostgreSQL runs only when CLINESTRA_TEST_POSTGRES_URL is set."""
import os
import threading

import pytest

from clinestra import Actor, Harness
from clinestra.settings import database_url, load_env
from clinestra.storage import MemoryStore, PostgresStore, SQLiteStore, redact_url, store_from_url

PG = os.environ.get("CLINESTRA_TEST_POSTGRES_URL")
DR = Actor(id="d", site="s", role="clinician")


def _reset():
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS docs")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        yield SQLiteStore(str(tmp_path / "t.db"))
        return
    if not PG:
        pytest.skip("CLINESTRA_TEST_POSTGRES_URL is not set")
    _reset()
    s = PostgresStore(PG)
    yield s
    s.close()


def doc(id, site="s", patient="p1", at="2026-01-01", **kw):
    return {"id": id, "site": site, "patient_id": patient, "created_at": at, **kw}


def test_ping_and_migrate_are_idempotent(store):
    assert store.ping()
    store.migrate()
    store.migrate()


def test_round_trip_update_and_site_isolation(store):
    store.put("patient", doc("a", name="Asha", n=1.5, nested={"z": 1, "a": [1, None]}))
    assert store.get("patient", "a", "s")["nested"] == {"z": 1, "a": [1, None]}
    assert store.get("patient", "a", "other") is None
    assert store.site_of("patient", "a") == "s"
    store.put("patient", doc("a", name="Bea"))
    assert store.get("patient", "a", "s")["name"] == "Bea"
    store.put("patient", doc("lib", site="*"))
    assert {d["id"] for d in store.list("patient", "other")} == {"lib"}


def test_list_filters_and_orders(store):
    store.put("obs", doc("2", at="2026-01-02", code="x"))
    store.put("obs", doc("1", at="2026-01-01", code="x"))
    store.put("obs", doc("3", at="2026-01-03", code="y", patient="p2"))
    assert [d["id"] for d in store.list("obs", "s", "p1")] == ["1", "2"]
    assert [d["id"] for d in store.list("obs", "s", code="y")] == ["3"]


def test_delete_respects_site(store):
    store.put("session", doc("a"))
    store.delete("session", "a", "other")
    assert store.get("session", "a", "s")
    store.delete("session", "a", "s")
    assert store.get("session", "a", "s") is None


def test_sql_values_are_parameters(store):
    nasty = "x'); DROP TABLE docs; --"
    store.put("patient", doc(nasty, name=nasty))
    assert store.get("patient", nasty, "s")["name"] == nasty
    assert store.ping()


def test_concurrent_writes(store):
    def work(n):
        for i in range(10):
            store.put("obs", doc(f"{n}-{i}"))
    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.list("obs", "s")) == 80


def test_harness_and_audit_chain(store):
    h = Harness(store=store, packs=("core",))
    p = h.create(DR, "patient", {"name": "A", "country": "IN", "gender": "other", "age_years": 40})
    h.create(DR, "condition", {"patient_id": p.id, "code": "htn"})
    plan = h.create(DR, "careplan", {"patient_id": p.id, "definition_key": "hypertension_basic"})
    h.transition(DR, "careplan", plan.id, "active", "Agreed")
    assert h.verify_timeline(DR, p.id)["valid"] is True
    again = Harness(store=store, packs=("core",))
    assert again.get(DR, "patient", p.id).name == "A"
    assert again.verify_timeline(DR, p.id)["valid"] is True


def test_postgres_rejects_nul_clearly():
    if not PG:
        pytest.skip("CLINESTRA_TEST_POSTGRES_URL is not set")
    s = PostgresStore(PG)
    with pytest.raises(ValueError, match="NUL"):
        s.put("patient", doc("n", name="a\x00b"))
    s.put("patient", doc("ok", name="literal \\u0000 text"))
    assert s.get("patient", "ok", "s")["name"] == "literal \\u0000 text"


def test_connection_error_hides_password():
    pytest.importorskip("psycopg")
    with pytest.raises(RuntimeError) as err:
        PostgresStore("postgresql://user:s3cret-pw@127.0.0.1:1/none")
    assert "s3cret-pw" not in str(err.value)
    assert err.value.__cause__ is None


def test_store_from_url(tmp_path):
    assert isinstance(store_from_url(None), MemoryStore)
    assert isinstance(store_from_url(str(tmp_path / "a.db")), SQLiteStore)
    assert isinstance(store_from_url("sqlite:///" + (tmp_path / "b.db").as_posix()), SQLiteStore)
    with pytest.raises(ValueError) as err:
        store_from_url("mysql://u:hidden@h/d")
    assert "hidden" not in str(err.value)
    assert redact_url("postgresql://u:p@h:5/d") == "postgresql://u:***@h:5/d"


def test_load_env_and_precedence(tmp_path, monkeypatch):
    for k in ("CLINESTRA_DATABASE_URL", "DATABASE_URL", "CLINESTRA_DB", "ZZ_A", "ZZ_B", "ZZ_C"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("# note\nexport ZZ_A=1 # trailing\nZZ_B=\"two words\"\nZZ_C=\nDATABASE_URL=postgresql://u:p#x@h/d\n")
    monkeypatch.setenv("ZZ_A", "real")
    values = load_env(str(f))
    assert values["ZZ_A"] == "1" and os.environ["ZZ_A"] == "real" and os.environ["ZZ_B"] == "two words"
    assert database_url() == "postgresql://u:p#x@h/d"
    load_env(str(f), override=True)
    assert os.environ["ZZ_A"] == "1"
    monkeypatch.setenv("CLINESTRA_DATABASE_URL", "sqlite:///x")
    assert database_url() == "sqlite:///x"
    assert load_env(str(tmp_path / "missing")) == {}
    for k in ("DATABASE_URL", "ZZ_A", "ZZ_B", "ZZ_C"):
        monkeypatch.delenv(k, raising=False)
