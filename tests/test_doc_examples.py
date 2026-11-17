"""Run the Python in each docs/example_*.md page, so the published examples keep working."""
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
PAGES = sorted(DOCS.glob("example_*.md"))


def test_all_six_examples_exist():
    assert len(PAGES) == 6


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_example_runs(page):
    text = page.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert blocks, f"{page.name} has no Python example"
    exec(compile("\n".join(blocks), str(page), "exec"), {})


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_example_has_screenshot_and_prompt(page):
    text = page.read_text(encoding="utf-8")
    image = re.search(r"!\[[^\]]*\]\((img/[^)]+)\)", text)
    assert image and (DOCS / image[1]).is_file()
    assert "## Prompt" in text
