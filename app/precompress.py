"""Pre-compress the static text assets (performance plan, step 6): `python -m app.precompress [static_dir]`.

Run once at image build (see the Dockerfile). For every .js / .css / .json / .svg / .html / .txt / .map file of at least 1 KB it writes name.ext.br
(if the `brotli` package is installed) and name.ext.gz beside it, so app.http_perf can answer a request straight from the compressed twin
instead of compressing the same bytes again on every page load. A twin is only written when it is smaller than the original and only
re-written when the original is newer; app.http_perf ignores a twin older than its source, so a stale one can never be served."""
import gzip
import sys
from pathlib import Path

EXTS = {".js", ".css", ".json", ".svg", ".html", ".txt", ".map", ".mjs"}
MIN_BYTES = 1024

try:
    import brotli
except ImportError:                                  # gzip twins only
    brotli = None


def compress_tree(root: Path) -> dict:
    """Returns {"files": n, "br": n, "gz": n, "bytes_in": n, "bytes_gz": n}."""
    stats = {"files": 0, "br": 0, "gz": 0, "bytes_in": 0, "bytes_gz": 0}
    for src in sorted(root.rglob("*")):
        if not src.is_file() or src.suffix not in EXTS or src.stat().st_size < MIN_BYTES:
            continue
        data = src.read_bytes()
        stats["files"] += 1
        stats["bytes_in"] += len(data)
        mtime = src.stat().st_mtime_ns
        outs = [(".gz", lambda d: gzip.compress(d, compresslevel=9, mtime=0))]
        if brotli is not None:
            outs.insert(0, (".br", lambda d: brotli.compress(d, quality=11)))
        for ext, fn in outs:
            twin = src.with_name(src.name + ext)
            if twin.exists() and twin.stat().st_mtime_ns >= mtime:
                continue
            blob = fn(data)
            if len(blob) >= len(data):
                twin.unlink(missing_ok=True)
                continue
            twin.write_bytes(blob)
            stats["br" if ext == ".br" else "gz"] += 1
            if ext == ".gz":
                stats["bytes_gz"] += len(blob)
    return stats


def minify_tree(root: Path) -> dict:
    """Minify every .js (not *.min.js) and .css under `root` IN PLACE - for the image build only (efficiency plan, step 9): comments and
    white space go, nothing is renamed or rewritten, so behaviour is unchanged (every file is checked with `node --check` in the tests).
    The repository keeps the readable sources. Returns {"files", "bytes_in", "bytes_out"}."""
    import rcssmin
    import rjsmin
    st = {"files": 0, "bytes_in": 0, "bytes_out": 0}
    for src in sorted(root.rglob("*")):
        if not src.is_file() or src.name.endswith(".min.js") or src.suffix not in (".js", ".css"):
            continue
        text = src.read_text(encoding="utf-8")
        out = rjsmin.jsmin(text) if src.suffix == ".js" else rcssmin.cssmin(text)
        if len(out) < len(text):
            src.write_text(out, encoding="utf-8")
        st["files"] += 1
        st["bytes_in"] += len(text.encode())
        st["bytes_out"] += len(out.encode())
    return st


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "static"
    if "--minify" in sys.argv:
        print("minify:", minify_tree(target))
    print("precompress:", compress_tree(target))
