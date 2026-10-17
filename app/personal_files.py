"""A player's own files on a character (hub > Kept > "My own files"): how many of each kind a character may hold.

Separate from what the table was shown (CharacterKeepsake.own False). The GM sets the three numbers per world (Edit World); NULL means the
default, 0 turns a kind off. The count is per CHARACTER. A leaf module: no router imports."""
DEFAULTS = {"image": 30, "audio": 10, "video": 5}
HARD_MAX = 500
_COLUMNS = {"image": "personal_images_max", "audio": "personal_audio_max", "video": "personal_video_max"}


def clamp(raw):
    """A typed limit as 0..HARD_MAX, or None (blank / junk -> the default)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(0, min(HARD_MAX, n))


def limit_for(world, kind: str) -> int:
    stored = clamp(getattr(world, _COLUMNS[kind], None)) if kind in _COLUMNS else None
    return DEFAULTS[kind] if stored is None else stored


def limits(world) -> dict:
    return {k: limit_for(world, k) for k in DEFAULTS}


def set_limit(world, kind: str, raw) -> None:
    setattr(world, _COLUMNS[kind], clamp(raw))
