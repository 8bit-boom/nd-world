"""The session cookie, re-sent only when it changed or is getting old (efficiency plan, step 10).

Starlette's SessionMiddleware signs and sends `Set-Cookie` on EVERY response that has a session - every page, every poll, every API call, about
300 bytes each and a response no cache could ever treat as unchanged. This one is the same middleware (same cookie name, format, signing and
flags, so existing sessions keep working) with one difference: the cookie goes out when the session's contents changed, when it was cleared, or
when the copy the browser holds is more than REFRESH_SECONDS old - so an active person's 14-day expiry still slides forward as before."""
import json
from base64 import b64decode, b64encode

from itsdangerous.exc import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection

REFRESH_SECONDS = 6 * 60 * 60


class QuietSessionMiddleware(SessionMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):  # pragma: no cover
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        initial_session_was_empty = True
        initial = None
        stale = True
        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                data, signed_at = self.signer.unsign(data, max_age=self.max_age, return_timestamp=True)
                scope["session"] = json.loads(b64decode(data))
                initial = json.dumps(scope["session"], sort_keys=True)
                initial_session_was_empty = False
                import time
                stale = (time.time() - signed_at.timestamp()) > REFRESH_SECONDS
            except BadSignature:
                scope["session"] = {}
        else:
            scope["session"] = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                if scope["session"]:
                    changed = initial is None or json.dumps(scope["session"], sort_keys=True) != initial
                    if changed or stale:
                        data = self.signer.sign(b64encode(json.dumps(scope["session"]).encode("utf-8")))
                        headers = MutableHeaders(scope=message)
                        headers.append("Set-Cookie", "{c}={d}; path={p}; {m}{f}".format(
                            c=self.session_cookie, d=data.decode("utf-8"), p=self.path,
                            m=f"Max-Age={self.max_age}; " if self.max_age else "", f=self.security_flags))
                elif not initial_session_was_empty:
                    headers = MutableHeaders(scope=message)
                    headers.append("Set-Cookie", "{c}=null; path={p}; expires=Thu, 01 Jan 1970 00:00:00 GMT; {f}".format(
                        c=self.session_cookie, p=self.path, f=self.security_flags))
            await send(message)

        await self.app(scope, receive, send_wrapper)
