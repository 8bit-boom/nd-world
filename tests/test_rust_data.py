"""rust/nd-render/data/app_data.json is the app's current data (scripts/gen_rust_data.py writes it)."""
import importlib.util
from pathlib import Path


def test_the_data_the_rust_side_reads_is_current():
    spec = importlib.util.spec_from_file_location("gen", Path(__file__).resolve().parent.parent / "scripts" / "gen_rust_data.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.OUT.read_text() == gen.text(), "run python scripts/gen_rust_data.py"
