"""
Neon & Dragons — Lore Importer
Reads markdown files from the project and POSTs them to the running TrueNAS app.

Usage:
    python import_lore.py
    python import_lore.py --host http://192.168.1.216:8087 --dry-run
"""

import argparse
import re
import sys
import json
import uuid
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent.parent  # NeonDragonsPortable root

EQUIPMENT_BASE = BASE.parent / "equipment" if (BASE.parent / "equipment").exists() else BASE / "../equipment"
CC_BASE = BASE.parent / "character creation" if (BASE.parent / "character creation").exists() else BASE / "../character creation"

LORE_MAPPINGS = [
    ("lore/characters",               "character",    None),
    ("lore/creatures/abominations",   "creature",     "abomination"),
    ("lore/creatures/animals",        "creature",     "animal"),
    ("lore/creatures/corp-enhanced",  "creature",     "corp-enhanced"),
    ("lore/creatures/ice creatures",  "creature",     "ice creature"),
    ("lore/creatures/mutants",        "creature",     "mutant"),
    ("lore/creatures/plants",         "creature",     "animal"),
    ("lore/creatures/thugs",          "creature",     "mutant"),
    ("lore/creatures/undead",         "creature",     "undead"),
    ("lore/creatures",                "creature",     None),
    ("lore/organisations/mega corps", "organization", "megacorp"),
    ("lore/organisations/criminal syndicates", "organization", "syndicate"),
    ("lore/organisations/governments","organization", "government"),
    ("lore/organisations/religious",  "organization", "cult"),
    ("lore/organisations/secret societies", "organization", "secret society"),
    ("lore/organisations/ai entities","organization", "AI entity"),
    ("lore/organisations/outsiders",  "organization", "gang"),
    ("lore/organisations/music bands","organization", "gang"),
    ("lore/organisations",            "organization", None),
    ("lore/locations/cities",         "location",     "city"),
    ("lore/locations/countries",      "location",     "country"),
    ("lore/locations/void locations", "location",     "void station"),
    ("lore/locations",                "location",     None),
    ("lore/events/corpo wars",         "event",        "corporate war"),
    ("lore/events/world events",       "event",        "political"),
    ("lore/events",                    "event",        None),
    ("lore/diseases",                 "note",         "lore"),
    ("lore/necro viruses",            "note",         "lore"),
]

# Equipment: resolved against project root (parent of NeonDragonsWorld)
EQUIPMENT_MAPPINGS = [
    ("armor",           "item", "armor"),
    ("augments",        "item", "augment"),
    ("base of operations", "item", "item"),
    ("bio augmentation","item", "bio-augmentation"),
    ("drone",           "item", "drone"),
    ("husks",           "item", "husk"),
    ("items",           "item", "item"),
    ("metals",          "item", "metal"),
    ("oddities",        "item", "oddity"),
    ("vehicles",        "item", "vehicle"),
    ("weapons/handguns","item", "weapon"),
    ("weapons/rifles",  "item", "weapon"),
    ("weapons/shotguns","item", "weapon"),
    ("weapons/sniper rifles", "item", "weapon"),
    ("weapons/heavy",   "item", "weapon"),
    ("weapons/melee",   "item", "weapon"),
    ("weapons/exotic",  "item", "weapon"),
]

FEAT_MAPPINGS = [
    ("common feats/origin",   "feat", "origin feat"),
    ("common feats/rank 1",   "feat", "common feat"),
    ("common feats/rank 2",   "feat", "common feat"),
    ("common feats/rank 3",   "feat", "common feat"),
    ("professions/profession feats/charlatan",  "feat", "profession feat"),
    ("professions/profession feats/cyberdoc",   "feat", "profession feat"),
    ("professions/profession feats/hacker",     "feat", "profession feat"),
    ("professions/profession feats/merc",       "feat", "profession feat"),
    ("professions/profession feats/psyonic",    "feat", "profession feat"),
    ("professions/profession feats/ronin",      "feat", "profession feat"),
    ("professions/profession feats/scoundrel",  "feat", "profession feat"),
    ("professions/profession feats/sentinel",   "feat", "profession feat"),
    ("professions/profession feats/techie",     "feat", "profession feat"),
    ("professions/profession feats/warlock",    "feat", "profession feat"),
    ("professions/profession special abilities","feat", "profession ability"),
    ("professions/psy powers",                  "feat", "psy power"),
]

# ── Parser ────────────────────────────────────────────────────────────────────

def slug_to_name(stem: str) -> str:
    name = re.sub(r"_\d+$", "", stem)          # remove trailing _ID
    name = name.replace("-", " ").replace("_", " ")
    name = re.sub(r"&amp;", "&", name)
    return name.strip().title()

IMAGE_EXTS = {".webp", ".png", ".jpg", ".jpeg", ".gif"}

def find_image(md_path: Path) -> Path | None:
    for ext in IMAGE_EXTS:
        img = md_path.with_suffix(ext)
        if img.exists():
            return img
    # also check ![avatar](filename) reference in file
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"!\[.*?\]\(([^)]+)\)", text)
        if m:
            ref = md_path.parent / m.group(1)
            if ref.exists():
                return ref
    except Exception:
        pass
    return None

def upload_image(host: str, img_path: Path) -> str | None:
    url = f"{host.rstrip('/')}/api/upload-image"
    boundary = "----FormBoundary" + uuid.uuid4().hex
    data = img_path.read_bytes()
    mime = "image/webp" if img_path.suffix == ".webp" else "image/jpeg"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{img_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("url")
    except Exception:
        return None

def parse_md(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    # Extract H1 name
    name = None
    for ln in lines:
        m = re.match(r"^#\s+(.+)", ln)
        if m:
            name = m.group(1).strip()
            break
    if not name:
        name = slug_to_name(path.stem)

    # Extract **Type:** and **Tags:**
    subtype = None
    tags = []
    for ln in lines[:20]:
        m = re.match(r"\*\*Type:\*\*\s*(.+)", ln)
        if m:
            subtype = m.group(1).strip()
        m = re.match(r"\*\*Tags:\*\*\s*(.+)", ln)
        if m:
            raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", m.group(1))
            tags = [t.strip() for t in re.split(r"[,;]", raw) if t.strip()]

    # Extract ## Entry section as summary
    summary = ""
    in_entry = False
    entry_lines = []
    for ln in lines:
        if re.match(r"^##\s+Entry", ln, re.IGNORECASE):
            in_entry = True
            continue
        if in_entry:
            if re.match(r"^##", ln):
                break
            if ln.strip() and not ln.startswith("---"):
                entry_lines.append(ln.strip())
    if entry_lines:
        raw_summary = " ".join(entry_lines)
        raw_summary = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw_summary)
        raw_summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw_summary)
        summary = raw_summary[:300]

    # Clean body: strip image lines, resolve internal links to plain text
    body_lines = []
    for ln in lines:
        if re.match(r"^!\[", ln):          # skip image embeds
            continue
        ln = re.sub(r"\[([^\]]+)\]\([^)]+\.md[^)]*\)", r"\1", ln)  # md links → text
        body_lines.append(ln)
    body = "\n".join(body_lines).strip()

    return {
        "name": name,
        "subtype": subtype,
        "tags": ", ".join(tags) if tags else None,
        "summary": summary or None,
        "body": body or None,
    }

# ── Collect ───────────────────────────────────────────────────────────────────

def scan_folder(folder: Path, kind: str, forced_subtype: str, seen: set, entities: list):
    if not folder.exists():
        return
    for md in sorted(folder.glob("*.md")):
        if md in seen:
            continue
        seen.add(md)
        try:
            parsed = parse_md(md)
        except Exception as e:
            print(f"  SKIP {md.name}: {e}")
            continue
        if forced_subtype:
            parsed["subtype"] = forced_subtype
        parsed["kind"] = kind
        parsed["_image_path"] = find_image(md)
        entities.append(parsed)

def collect_entities() -> list[dict]:
    seen_paths = set()
    entities = []

    for rel_dir, kind, forced_subtype in LORE_MAPPINGS:
        scan_folder(BASE / rel_dir, kind, forced_subtype, seen_paths, entities)

    proj_root = BASE.parent
    for rel_dir, kind, forced_subtype in EQUIPMENT_MAPPINGS:
        scan_folder(proj_root / "equipment" / rel_dir, kind, forced_subtype, seen_paths, entities)

    for rel_dir, kind, forced_subtype in FEAT_MAPPINGS:
        folder = proj_root / "character creation" / rel_dir
        # profession feats have one more level of subdirs per profession
        if folder.exists() and any(folder.iterdir()):
            # check if it has sub-profession folders
            has_subdirs = any(f.is_dir() for f in folder.iterdir())
            if has_subdirs:
                for subdir in sorted(folder.iterdir()):
                    if subdir.is_dir():
                        scan_folder(subdir, kind, forced_subtype, seen_paths, entities)
            else:
                scan_folder(folder, kind, forced_subtype, seen_paths, entities)
        else:
            scan_folder(folder, kind, forced_subtype, seen_paths, entities)

    return entities

# ── Import ────────────────────────────────────────────────────────────────────

def post_batch(host: str, batch: list[dict]) -> int:
    url = f"{host.rstrip('/')}/api/import"
    payload = json.dumps({"entities": batch}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("created", 0)

def post_import(host: str, entities: list[dict], batch_size: int = 10) -> int:
    total = 0
    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]
        try:
            created = post_batch(host, batch)
            total += created
            print(f"  [{i + len(batch)}/{len(entities)}] +{created}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}: {e.read().decode()}")
            sys.exit(1)
        except Exception as e:
            print(f"  Error on batch {i//batch_size + 1}: {e}")
            sys.exit(1)
    return total

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://192.168.1.216:8087", help="App base URL")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't import")
    args = parser.parse_args()

    print("Scanning lore files…")
    entities = collect_entities()
    print(f"Found {len(entities)} entries\n")

    by_kind: dict[str, int] = {}
    for e in entities:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind:15s} {count}")

    if args.dry_run:
        print("\n[dry-run] Not importing.")
        return

    print(f"\nUploading images to {args.host} …")
    imgs_uploaded = 0
    for e in entities:
        img_path = e.pop("_image_path", None)
        if img_path:
            url = upload_image(args.host, img_path)
            if url:
                e["image_url"] = url
                imgs_uploaded += 1
    print(f"  {imgs_uploaded} images uploaded")

    print(f"Importing entities …")
    created = post_import(args.host, entities)
    print(f"Done — {created} new entries created, images linked to existing entries.")

if __name__ == "__main__":
    main()
