"""TOTP-based two-step authentication (RFC 6238) — opt-in per account from
/account/2fa. Verification (used at login) lives in app/routers/auth.py;
setup/disable (used from the account page) lives in app/routers/account.py.

Kept as its own module rather than folded into app/auth.py: app/auth.py is
imported by nearly every router for password hashing and the
require_login/require_gm dependencies, and deliberately has no third-party
dependencies beyond the stdlib — pyotp/qrcode/Pillow stay isolated to the
one module that actually needs them.
"""
import base64
import hashlib
import io
import json
import secrets

import pyotp
import qrcode

_ISSUER = "N&D World"
_BACKUP_CODE_COUNT = 8
_BACKUP_CODE_BYTES = 5  # secrets.token_hex(5) -> 10 hex chars, 40 bits of entropy


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """otpauth:// URI encoded into the setup QR code — this is what makes
    an authenticator app label the entry with both the account email and
    the "N&D World" issuer name."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=_ISSUER)


def qr_code_data_uri(uri: str) -> str:
    """Renders entirely server-side (via the qrcode package's PIL image
    factory — Pillow is already a dependency) into an inline data: URI, so
    the setup page never has to fetch a QR-code image from any third-party
    service — consistent with this app's self-hosted, no-external-calls
    posture."""
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def verify_code(secret: str, code: str) -> bool:
    """valid_window=1 also accepts the previous/next 30-second step, to
    tolerate modest clock drift between the server and the user's phone —
    same tradeoff every TOTP implementation makes."""
    if not secret or not code:
        return False
    try:
        return pyotp.totp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def generate_backup_codes(n: int = _BACKUP_CODE_COUNT) -> list:
    """Each code is short enough to type by hand but still has 40 bits of
    entropy — plenty against online guessing once combined with the login
    2FA step's own rate limiting (see app/routers/auth.py)."""
    return [secrets.token_hex(_BACKUP_CODE_BYTES) for _ in range(n)]


def hash_backup_code(code: str) -> str:
    """sha256, not PBKDF2 — like ApiToken's token_hash, this is already a
    high-entropy generated value (not a user-chosen password), so a fast
    exact-match hash is the right tool, not a slow KDF."""
    return hashlib.sha256(code.strip().lower().encode("utf-8")).hexdigest()


def load_backup_code_hashes(user) -> list:
    try:
        data = json.loads(user.totp_backup_codes_json or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def consume_backup_code(user, code: str) -> bool:
    """If `code` matches one of `user`'s remaining backup codes, removes it
    (single use) and returns True — caller is responsible for db.commit().
    Returns False (no mutation) for a blank/non-matching code."""
    if not code:
        return False
    hashes = load_backup_code_hashes(user)
    target = hash_backup_code(code)
    if target in hashes:
        hashes.remove(target)
        user.totp_backup_codes_json = json.dumps(hashes)
        return True
    return False
