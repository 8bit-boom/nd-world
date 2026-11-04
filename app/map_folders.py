"""Folders for maps and schematics (the /maps page). A folder is a plain path string ("Cities/Hue"), like an entity's folder: an item belongs to one, "" is the top level.
A map (a JSON file) keeps it under the key "folder", a schematic in its `folder` column. The page shows one folder at a time (`/maps?folder=Cities/Hue`): its subfolders as
cards with how many maps and schematics they hold (all the way down), then the items that sit directly in it. rust/nd-gateway/src/maps.rs does the same."""
from typing import Iterable


def norm(raw) -> str:
    """The folder path as stored: slashes folded, empty and "." / ".." parts dropped, at most 256 characters."""
    parts = [p.strip() for p in str(raw or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:256].strip("/")


def view(current, folders: Iterable[str]) -> dict:
    """What the page needs to browse `current`: {current, crumbs [{name, path}], subfolders [{name, path, count}], all [every folder path and its parents]}.
    `folders` = the folder of every visible item (maps and schematics), "" for the top level."""
    current = norm(current)
    counts: dict = {}
    every: set = set()
    for f in folders:
        f = norm(f)
        if not f:
            continue
        parts = f.split("/")
        for i in range(len(parts)):
            every.add("/".join(parts[: i + 1]))
        if current and f != current and not f.startswith(current + "/"):
            continue
        rest = f[len(current) + 1:] if current else f
        if rest:
            seg = rest.split("/")[0]
            counts[seg] = counts.get(seg, 0) + 1
    crumbs = []
    parts = current.split("/") if current else []
    for i, name in enumerate(parts):
        crumbs.append({"name": name, "path": "/".join(parts[: i + 1])})
    subs = [{"name": n, "path": (current + "/" + n) if current else n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (kv[0].lower(), kv[0]))]
    return {"current": current, "crumbs": crumbs, "subfolders": subs, "all": sorted(every, key=lambda p: (p.lower(), p))}


def in_folder(item_folder, current: str) -> bool:
    return norm(item_folder) == current


def moved(item_folder, old: str, new: str):
    """`item_folder` after renaming folder `old` to `new` (everything under it follows); None when it is not under `old`."""
    f = norm(item_folder)
    if f == old:
        return new
    if f.startswith(old + "/"):
        return norm(new + f[len(old):]) if new else norm(f[len(old) + 1:])
    return None
