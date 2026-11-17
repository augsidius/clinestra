"""The operational packs shipped with the library load, validate and run from admission to the end state."""
import pytest

from clinestra import Actor, Harness
from clinestra.cli import _validate_definitions, main
from clinestra.errors import InvariantViolation
from clinestra.packs_loader import builtin_packs, load_pack

DR = Actor(id="dr_rao", site="s1", role="clinician")
REVIEWER = Actor(id="dr_iyer", site="s1", role="clinician")
CASES = [("general_opd", "opd_new_visit", "completed"), ("surgical_ward", "surg_admission_episode", "discharged"),
         ("medical_ward", "ward_admission_episode", "discharged"), ("home_care", "home_visit", "completed")]


def fill(h, page, pack):
    cfg = next(d for d in pack.definitions if d["def_kind"] == "note_page" and d["key"] == page.definition_key)["config"]
    values = {}
    for b in cfg["blocks"]:
        if b.get("required"):
            kind = b.get("input_type")
            values[b["key"]] = True if kind == "boolean" else b["options"][0] if kind == "choice" else "Sample"
    h.update(DR, "note_page", page.id, {"blocks": values})
    h.transition(DR, "note_page", page.id, "finalized", "Signed")


@pytest.mark.parametrize("pack_id,_enc,_end", CASES)
def test_pack_is_valid_and_operational(pack_id, _enc, _end):
    assert pack_id in builtin_packs()
    pack = load_pack(pack_id)
    assert _validate_definitions(pack) == []
    assert not {"protocol", "regimen", "cohort", "reaction"} & {d["def_kind"] for d in pack.definitions}
    Harness(packs=("core", pack_id))


def test_all_packs_load_together():
    Harness(packs=("core", "general_opd", "surgical_ward", "medical_ward", "home_care"))


@pytest.mark.parametrize("pack_id,encounter,end", CASES)
def test_pathway_runs_to_the_end(pack_id, encounter, end):
    h = Harness(packs=("core", pack_id))
    pack = load_pack(pack_id)
    p = h.create(DR, "patient", {"name": "A", "country": "IN", "gender": "female", "age_years": 50})
    h.create(DR, "encounter", {"patient_id": p.id, "definition_key": encounter})
    journey = h.list(DR, "journey", p.id)[0]
    cfg = next(d for d in pack.definitions if d["def_kind"] == "journey")["config"]

    # Leaving a state with unmet requirements is refused without force.
    first_required = next(i for i, s in enumerate(cfg["states"]) if cfg["requires"].get(s))
    for state in cfg["states"][1:first_required + 1]:
        h.move_journey(DR, journey.id, state, "Next step")
    with pytest.raises(InvariantViolation):
        h.move_journey(DR, journey.id, cfg["states"][first_required + 1], "Too early")
    pages = h.list(DR, "note_page", p.id)
    order = [d["key"] for d in pack.definitions if d["def_kind"] == "note_page"]
    for key in order:
        page = next(x for x in pages if x.definition_key == key)
        moves = cfg["states"]
        cur = h.get(DR, "journey", journey.id).state
        # Walk forward through states that need a person to move them, before this page is signed.
        for rule in cfg["advance_on"]:
            if rule["definition_key"] == key:
                target = moves.index(rule["to"]) - 1
                while moves.index(cur) < target:
                    nxt = moves[moves.index(cur) + 1]
                    h.move_journey(DR, journey.id, nxt, "Next step")
                    cur = nxt
        fill(h, page, pack)
    final = h.get(DR, "journey", journey.id)
    if final.state != end:
        h.move_journey(DR, journey.id, end, "Done")
    assert h.get(DR, "journey", journey.id).state == end


def test_install_enables_a_built_in_pack(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CLINESTRA_PACKS=core\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["clinestra", "pack", "install", "surgical_ward", "--enable"])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert "CLINESTRA_PACKS=core,surgical_ward" in env.read_text()
