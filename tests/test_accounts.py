"""Staff usernames replace pasted API keys for people. Device keys still authenticate."""
from conftest import TEST_PACKS
from fastapi.testclient import TestClient

from clinestra import Harness
from clinestra.server.app import create_app
from clinestra.server.auth import APIKeyAuth

KEYS = {"device-key": {"id": "device_gateway", "site": "clinic_1", "role": "system", "actor_type": "device"}}


def client():
    return TestClient(create_app(Harness(packs=TEST_PACKS), APIKeyAuth(KEYS)))


def test_first_superadmin_sets_a_password_then_signs_in():
    c = client()
    setup = c.get("/v1/auth/setup")
    assert setup.status_code == 200
    assert setup.json() == {"username": "admin", "password_unset": True}
    assert c.post("/v1/auth/login", json={"username": "admin", "password": ""}).status_code == 409
    saved = c.post("/v1/auth/password", json={"username": "admin", "new_password": "Chosen-pass-1"})
    assert saved.status_code == 200
    token = saved.json()["token"]
    me = c.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["id"] == "admin"
    assert me.json()["role"] == "admin"
    assert c.get("/v1/me", headers={"Authorization": "Bearer device-key"}).json()["id"] == "device_gateway"


def test_people_added_by_the_superadmin_must_replace_the_temporary_password():
    c = client()
    admin = c.post("/v1/auth/password", json={"username": "admin", "new_password": "Chosen-pass-1"}).json()["token"]
    created = c.post("/v1/admin/accounts", headers={"Authorization": f"Bearer {admin}"},
                     json={"username": "dr_rao", "role": "clinician", "password": "Temp-pass-1"})
    assert created.status_code == 201
    assert created.json()["must_change_password"] is True
    step = c.post("/v1/auth/login", json={"username": "dr_rao", "password": "Temp-pass-1"})
    assert step.status_code == 200
    assert step.json()["must_change_password"] is True
    setup = step.json()["setup_token"]
    assert c.get("/v1/me", headers={"Authorization": f"Bearer {setup}"}).status_code == 401
    same = c.post("/v1/auth/password", headers={"Authorization": f"Bearer {setup}"}, json={"new_password": "Temp-pass-1"})
    assert same.status_code == 422
    done = c.post("/v1/auth/password", headers={"Authorization": f"Bearer {setup}"}, json={"new_password": "Own-pass-1"})
    assert done.status_code == 200
    assert c.get("/v1/me", headers={"Authorization": f"Bearer {done.json()['token']}"}).json()["id"] == "dr_rao"
    assert c.post("/v1/auth/login", json={"username": "dr_rao", "password": "Temp-pass-1"}).status_code == 401
    nurse = done.json()["token"]
    refused = c.post("/v1/admin/accounts", headers={"Authorization": f"Bearer {nurse}"},
                     json={"username": "extra", "role": "nurse", "password": "Temp-pass-1"})
    assert refused.status_code == 403
