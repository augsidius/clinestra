"""UI component library: examples validate cleanly; unknown types, bad enums and extra keys are dropped."""
from clinestra.server.agentic import content_blocks as uic


def test_examples_round_trip():
    for c in uic.catalog():
        assert uic.validate([{"type": c["type"], "props": c["example"]}]) == [{"type": c["type"], "props": c["example"]}], c["type"]


def test_untrusted_blocks_are_trimmed():
    out = uic.validate([
        {"type": "script", "props": {"x": 1}},
        {"type": "alert", "props": {"title": "Low SpO2", "level": "panic", "onclick": "x"}},
        {"type": "stat_grid", "props": {"items": [{"label": "BP"}]}},
        {"type": "score", "props": {"name": "mMRC", "value": "2"}},
    ])
    assert out == [{"type": "alert", "props": {"title": "Low SpO2"}}, {"type": "stat_grid", "props": {"items": []}},
                   {"type": "score", "props": {"name": "mMRC", "value": 2.0}}]
