from pathlib import Path

# The MDD pack is example content: it is not installed with the library.
TEST_PACKS = ("core", str(Path(__file__).resolve().parents[1] / "examples" / "packs" / "mdd"))
