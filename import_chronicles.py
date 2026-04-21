"""
Chronicles of the Worm — Kanka JSON importer
Reads a Kanka campaign export and imports into the worldbuilding app.

Usage:
    python import_chronicles.py
    python import_chronicles.py --host http://192.168.1.216:8087 --dry-run
"""

import argparse
import json
import re
import sys
import uuid
import urllib.request
import urllib.error
from pathlib import Path
import html

CHRONICLES_DIR = Path(__file__).parent.parent / "Chronicles of the Worm" / "setting"
HOST_DEFAULT = "http://192.168.1.216:8087"
WORLD_NAME = "Chronicles of the Worm"
WORLD_ACCENT = "#b44fff"  # purple

# Kanka folder → (kind, subtype)
FOLDER_MAP = {
    "characters":    ("character",    None),
    "creatures":     ("creature",     None),
    "locations":     ("location",     None),
    "organisations": ("organization", None),
    "events":        ("event",        None),
    "families":      ("organization", "family"),
    "races":         ("note",         "lore"),
    "settings":      ("note",         "lore"),
}

# Kanka type strings → subtype overrides
CHARACTER_TYPE_MAP = {
    "npc": "NPC", "pc": "PC", "villain": "villain",
    "ally": "ally", "companion": "ally",
}

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

def parse_kanka_json(path: Path, kind: str, forced_subtype: str | None) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # Name
    name = data.get("name") or data.get("entity", {}).get("name")
    if not name:
        return None

    # Subtype from type field
    raw_type = (data.get("type") or "").strip()
    subtype = forced_subtype or CHARACTER_TYPE_MAP.get(raw_type.lower()) or (raw_type if raw_type else None)

    # Summary and body from entry (HTML → plain for summary, keep for body as md)
    entry_html = data.get("entry") or data.get("entity", {}).get("entry") or ""
    summary_plain = strip_html(entry_html)[:300] or None

    # Body: convert entry HTML to simple markdown-ish text
    body = entry_html if entry_html.strip() else None
    if body:
        # Convert basic HTML to markdown
        body = re.sub(r"<h[1-3][^>]*>", "\n## ", body)
        body = re.sub(r"</h[1-3]>", "\n", body)
        body = re.sub(r"<p[^>]*>", "\n", body)
        body = re.sub(r"</p>", "\n", body)
        body = re.sub(r"<br\s*/?>", "\n", body)
        body = re.sub(r"<strong[^>]*>", "**", body)
        body = re.sub(r"</strong>", "**", body)
        body = re.sub(r"<em[^>]*>", "_", body)
        body = re.sub(r"</em>", "_", body)
        body = re.sub(r"<li[^>]*>", "\n- ", body)
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body).strip() or None

    # Tags: age, sex, title for characters
    tag_parts = []
    for field in ("age", "sex", "title", "type"):
        val = data.get(field)
        if val and str(val).strip():
            tag_parts.append(str(val).strip())
    tags = ", ".join(tag_parts) if tag_parts else None

    # Image path: entity.image_path → w/230046/filename.webp
    image_path = None
    entity_block = data.get("entity", {})
    img = entity_block.get("image_path") or data.get("image")
    if img:
        # image_path is like "w/230046/filename.webp"
        img_file = CHRONICLES_DIR / img.replace("\\", "/")
        if img_file.exists():
            image_path = img_file

    return {
        "name": name,
        "kind": kind,
        "subtype": subtype,
        "summary": summary_plain,
        "body": body,
        "tags": tags,
        "_image_path": image_path,
    }

def collect_entities() -> list[dict]:
    entities = []
    seen_names = set()

    for folder_name, (kind, subtype) in FOLDER_MAP.items():
        folder = CHRONICLES_DIR / folder_name
        if not folder.exists():
            continue
        for jf in sorted(folder.glob("*.json")):
            parsed = parse_kanka_json(jf, kind, subtype)
            if not parsed:
                continue
            key = (parsed["name"], parsed["kind"])
            if key in seen_names:
                continue
            seen_names.add(key)
            entities.append(parsed)

    return entities

def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def upload_image(host: str, img_path: Path) -> str | None:
    url = f"{host}/api/upload-image"
    boundary = "----" + uuid.uuid4().hex
    ext = img_path.suffix.lower()
    mime_map = {".webp": "image/webp", ".png": "image/png", ".gif": "image/gif"}
    mime = mime_map.get(ext, "image/jpeg")
    data = img_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{img_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read()).get("url")
    except Exception as e:
        print(f"    Image upload failed: {e}")
        return None

def post_batch(host: str, world_id: int, batch: list[dict]) -> int:
    result = post_json(f"{host}/api/import", {"world_id": world_id, "entities": batch})
    return result.get("created", 0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST_DEFAULT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    host = args.host.rstrip("/")

    print("Scanning Chronicles of the Worm…")
    entities = collect_entities()
    print(f"Found {len(entities)} entries\n")
    by_kind: dict[str, int] = {}
    for e in entities:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    for k, c in sorted(by_kind.items()):
        print(f"  {k:15s} {c}")

    if args.dry_run:
        print("\n[dry-run] Not importing.")
        return

    # Create or get world
    print(f"\nCreating world '{WORLD_NAME}'…")
    try:
        result = post_json(f"{host}/api/worlds", {"name": WORLD_NAME, "accent": WORLD_ACCENT,
                                                    "description": "A dark fantasy campaign of intrigue and ancient horrors."})
        world_id = result["id"]
        print(f"  World ID {world_id} ({'new' if result['created'] else 'existing'})")
    except Exception as e:
        print(f"  Failed to create world: {e}")
        sys.exit(1)

    # Upload images
    print("\nUploading images…")
    uploaded = 0
    for e in entities:
        img = e.pop("_image_path", None)
        if img:
            url = upload_image(host, img)
            if url:
                e["image_url"] = url
                uploaded += 1
    print(f"  {uploaded} images uploaded")

    # Import in batches
    print("Importing entities…")
    total = 0
    batch_size = 10
    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]
        try:
            created = post_batch(host, world_id, batch)
            total += created
            print(f"  [{i + len(batch)}/{len(entities)}] +{created}", flush=True)
        except Exception as e:
            print(f"  Error on batch {i//batch_size + 1}: {e}")
            sys.exit(1)

    print(f"\nDone — {total} new entries imported into '{WORLD_NAME}'.")

if __name__ == "__main__":
    main()
