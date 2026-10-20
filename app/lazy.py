"""A module that is imported the first time one of its attributes is used (efficiency plan, step 7).

`ollama`, `qrcode` and the MCP server are only needed by a few features, but importing them at startup cost ~20 MB of memory and a quarter of
a second for every server, whether or not anyone uses those features."""
import importlib


class LazyModule:
    def __init__(self, name: str):
        self._name = name
        self._mod = None

    def _load(self):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return self._mod

    def __getattr__(self, attr):
        return getattr(self._load(), attr)
