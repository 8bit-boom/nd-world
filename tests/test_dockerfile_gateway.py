"""The Python-free image build (Dockerfile.gateway): its JavaScript is minified by esbuild in a throw-away stage and the Rust nd-assets does the rest; no Python stage remains."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_python_free_image_has_no_python_stage_and_minifies_javascript_without_renaming():
    d = (ROOT / "Dockerfile.gateway").read_text()
    assert "FROM python" not in d and "pip install" not in d
    assert "AS minify" in d and "--minify-whitespace" in d and "--minify-identifiers" not in d and "--minify-syntax" not in d
    assert "COPY --from=minify /w/static/ /assets/static/" in d
    assert d.index("AS minify") < d.index("AS gateway")
    assert "ND_MODE=rust" in d and "ND_PYTHON_FREE=1" in d
