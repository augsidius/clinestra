"""The Clinestra 2.0 canvas on note pages: sections seed from a template, values round-trip through blocks."""
from clinestra.emr import canvas as cv
from clinestra.emr.emru import emru_syntax_parser

DEF = {"key": "dm", "name": "Diabetes review", "config": {
    "blocks": [{"key": "glycaemia__fbs"}, {"key": "notes"}, {"key": "meds"}, {"key": "additional_sections"}],
    "sections": [
        {"section_id": "glycaemia", "section_name": "Glycaemia", "section_type": "key_value",
         "config": {"fields": [{"key": "fbs", "label": "Fasting glucose", "type": "number", "unit": "mg/dL"}]}},
        {"section_id": "notes", "section_name": "Notes", "section_type": "text_area", "config": {}},
        {"section_id": "meds", "section_name": "Medications", "section_type": "table_form", "config": {"columns": [{"name": "medication_name"}]}},
    ]}}
KNOWN = {b["key"] for b in DEF["config"]["blocks"]}


def test_sections_seed_from_template():
    secs = cv.sections_from_definition(DEF)
    assert [s["section_type"] for s in secs] == ["regular_form", "text_area", "table_form"]
    assert secs[0]["data"][0]["field_name"] == "fbs" and secs[0]["data"][0]["type"] == "numerical"
    assert secs[2]["config"]["columns"][0]["label"] == "Medication name"


def test_socket_ops_land_in_blocks():
    secs = cv.sections_from_definition(DEF)
    form = [{**secs[0]["data"][0], "value": "132"}]
    ops = [{"type": "editor_update", "section_id": "glycaemia", "content": form},
           {"type": "editor_update", "section_id": "notes", "content": "<p>Fit for <b>duty</b></p>"},
           {"type": "editor_update", "section_id": "meds", "content": [{"medication_name": "Metformin"}]},
           {"type": "editor_new_section", "section_id": "x", "content": '{"section_id": "x", "section_name": "Counselling", "section_type": "text_area", "data": "Diet"}'}]
    blocks = cv.blocks_from_sections(cv.apply_ops(secs, ops), KNOWN)
    assert blocks["glycaemia__fbs"] == "132" and blocks["notes"] == "Fit for **duty**"
    assert blocks["meds"] == [{"medication_name": "Metformin"}]
    assert blocks["additional_sections"][0]["section"]["section_name"] == "Counselling"


def test_blocks_written_by_the_standard_ui_show_in_the_canvas():
    secs = cv.overlay_blocks(cv.sections_from_definition(DEF), {"glycaemia__fbs": 140, "notes": "ok",
                                                                 "additional_sections": [{"section": {"section_id": "y", "section_name": "Plan", "section_type": "text_area"}, "value": "Review"}]})
    assert secs[0]["data"][0]["value"] == 140 and secs[1]["data"] == "ok"
    assert secs[-1]["section_id"] == "y" and secs[-1]["data"] == "Review"


def test_delete_and_reorder():
    secs = cv.apply_ops(cv.sections_from_definition(DEF), [{"type": "delete_section", "section_id": "notes"}])
    assert "notes" not in cv.blocks_from_sections(secs, KNOWN)
    assert [s["section_id"] for s in cv.reorder(secs, ["meds", "glycaemia"])][:2] == ["meds", "glycaemia"]


def test_emru_parser():
    c = emru_syntax_parser("Metformin::dose:1 g//meds//medication_name//add")
    assert c["section_id"] == "meds" and c["update_type"] == "add" and c["properties"] == {"dose": "1 g"}
