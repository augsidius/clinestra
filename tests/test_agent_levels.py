"""Agents at three levels (personal, care team, patient), skills and tool administration - all in the API layer."""
from conftest import TEST_PACKS
import subprocess
import sys

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from clinestra import Actor, Harness, secrets as sec  # noqa: E402
from clinestra.server import APIKeyAuth, create_app  # noqa: E402

S = "site_a"
KEYS = {"k_admin": {"id": "admin_a", "site": S, "role": "admin"}, "k_dr": {"id": "dr_a", "site": S, "role": "clinician"},
        "k_nurse": {"id": "nurse_a", "site": S, "role": "nurse"}}


def auth(k):
    return {"Authorization": f"Bearer {k}"}


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CLINESTRA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CLINESTRA_SITE", S)
    sec.reset()
    yield
    sec.reset()


@pytest.fixture
def h():
    h = Harness(packs=TEST_PACKS)
    admin = Actor(id="admin_a", site=S, role="admin")
    d = h.create(admin, "definition", {"def_kind": "care_team", "key": "ohs_team", "name": "OHS care team",
                                       "config": {"roles": [{"key": "primary", "label": "Medical officer"}]}})
    h.transition(admin, "definition", d.id, "published")
    return h


@pytest.fixture
def c(h):
    return TestClient(create_app(harness=h, auth=APIKeyAuth(KEYS)))


def test_core_library_does_not_load_agents():
    code = "import sys, clinestra; from clinestra import Harness; Harness(); print(any(m.startswith('clinestra.server') for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"


def test_explorer_lists_three_levels_and_the_member_s_agents(c):
    ex = c.get("/v1/ai/explorer", headers=auth("k_dr")).json()
    assert [lv["id"] for lv in ex["levels"]] == ["personal", "team", "patient"]
    by_level = {}
    for a in ex["agents"]:
        by_level.setdefault(a["level"], []).append(a)
    mine = by_level["personal"]
    assert len(mine) == 1 and mine[0]["owner_id"] == "dr_a" and mine[0]["kind"] == "personal"
    assert "ohs_team" in [a["care_team_key"] for a in by_level["team"]]  # added to the care team automatically
    assert any(a["id"] == "clinical_copilot" for a in by_level["patient"])
    assert "my_work" in mine[0]["tool_names"] and not any(t.startswith("observation") for t in mine[0]["tool_names"])
    team = next(a for a in by_level["team"] if a["care_team_key"] == "ohs_team")
    assert "team_board" in team["tool_names"] and "patient_get" not in team["tool_names"]
    assert {s["id"] for s in team["skills_detail"]} >= {"due_list", "handover_note"}
    # each member has their own personal agent
    nurse = c.get("/v1/ai/agents/personal", headers=auth("k_nurse")).json()
    assert nurse["id"] != mine[0]["id"] and nurse["owner_id"] == "nurse_a"


def test_level_rules(c):
    pid = c.post("/v1/patients", json={"name": "Asha Devi", "country": "IN", "gender": "female", "age_years": 34}, headers=auth("k_dr")).json()["id"]
    mine = c.get("/v1/ai/agents/personal", headers=auth("k_dr")).json()["id"]
    # personal: never on a patient, never someone else's
    assert c.post("/v1/ai/agents/personal/chat", json={"message": "hi", "context": {"patient_id": pid}}, headers=auth("k_dr")).status_code == 403
    assert c.post(f"/v1/ai/agents/{mine}/chat", json={"message": "hi"}, headers=auth("k_nurse")).status_code == 403
    r = c.post("/v1/ai/agents/personal/chat", json={"skill_id": "plan_my_day"}, headers=auth("k_dr"))
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "personal" and any(s["tool"] == "my_work" and s["ok"] for s in r.json()["steps"])
    # team: not on one patient; runs on its own, read only
    assert c.post("/v1/ai/agents/team__ohs_team/chat", json={"message": "hi", "context": {"patient_id": pid}}, headers=auth("k_dr")).status_code == 403
    run = c.post("/v1/ai/agents/team__ohs_team/runs", json={"skill_id": "due_list"}, headers=auth("k_dr"))
    assert run.status_code == 200, run.text
    assert all(s["tool"] in ("team_board", "task_list", "routine_list") for s in run.json()["steps"])
    assert c.get("/v1/ai/agents/team__ohs_team/runs", headers=auth("k_dr")).json()[0]["skill_id"] == "due_list"
    # patient: never on its own; templates are not chat targets
    assert c.post("/v1/ai/agents/clinical_copilot/runs", json={"skill_id": "red_flags"}, headers=auth("k_dr")).status_code == 403
    assert c.post("/v1/ai/agents/personal_assistant/chat", json={"message": "hi"}, headers=auth("k_dr")).status_code == 400


def test_skills_and_tool_administration(c):
    skills = c.get("/v1/admin/skills", headers=auth("k_admin")).json()
    assert {s["level"] for s in skills} == {"personal", "team", "patient"}
    bad = {"id": "auto_rx", "name": "Auto prescribe", "level": "patient", "schedule": "daily", "tools": ["medication_propose"]}
    assert c.put("/v1/admin/skills/auto_rx", json=bad, headers=auth("k_admin")).status_code == 400  # nothing patient specific runs on its own
    assert c.get("/v1/admin/skills", headers=auth("k_dr")).status_code == 403

    tools = {t["name"]: t for t in c.get("/v1/admin/tools", headers=auth("k_admin")).json()["tools"]}
    assert tools["my_work"]["levels"] == ["personal"] and tools["patient_get"]["levels"] == ["patient"]
    assert tools["medication_propose"]["kind"] == "propose" and "medication_review" in tools["medication_propose"]["used_by_skills"]
    assert c.put("/v1/admin/tools/team_board", json={"enabled": False}, headers=auth("k_admin")).status_code == 200
    team = next(a for a in c.get("/v1/ai/agents/teams", headers=auth("k_dr")).json() if a["care_team_key"] == "ohs_team")
    assert "team_board" not in team["tool_names"]
    assert c.put("/v1/admin/tools/task_list", json={"levels": ["galaxy"]}, headers=auth("k_admin")).status_code == 400
