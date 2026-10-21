"""Handing a file to the Rust gateway to send (docs/RUST_REWRITE_PLAN.md, phase 1).

Behind the gateway (`ND_ACCEL_TOKEN` set by docker/entrypoint.sh, the same random value in both processes) a route that has decided a request
may have a file does not read and stream it itself: it answers with `X-Nd-Accel-Path: <absolute path>` and the headers it wants, and the gateway
sends the file (ranges, validators, no Python thread held during a big download). Every check - login, world access, the live-recording and
strict-map guards - has already happened in Python by then. Without the token, or when the request does not carry it, a normal FileResponse is
returned, so the app behaves the same with the gateway switched off.
"""
import hmac
import mimetypes
import os
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import FileResponse, Response


def _token() -> str:
    return os.environ.get("ND_ACCEL_TOKEN", "")


def file_response(request: Request, path: Path, headers: Optional[dict] = None, media_type: Optional[str] = None) -> Response:
    token = _token()
    given = request.headers.get("x-nd-accel", "")
    if token and given and hmac.compare_digest(given.encode(), token.encode()):
        out = dict(headers or {})
        out["X-Nd-Accel-Path"] = str(path)
        out["Content-Type"] = media_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return Response(status_code=200, headers=out)
    return FileResponse(path, headers=headers, media_type=media_type)
