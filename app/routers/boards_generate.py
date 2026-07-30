"""Two InvestBoard generators ported from NeonDragonsWorld: an auto-laid-out
faction/organization relationship graph built from existing entities and
entity_links, and a fixed 50-location Dreamlands atlas (bundled reference
content, not derived from world data). Both just build a nodes/edges payload
and write it into a normal InvestBoard row — no new model or storage needed.
"""
import json
import math
import re
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, InvestBoard, entity_links

router = APIRouter()

_ORG_KINDS = {"organization"}
_SUBTYPE_COLOR = {
    "megacorp": "#00f0ff", "corporation": "#00f0ff", "government": "#3b82f6",
    "syndicate": "#ff2d78", "cult": "#a78bfa", "religion": "#a78bfa",
    "culture": "#22c55e", "gang": "#f97316", "secret society": "#8b0000",
    "ai entity": "#e879f9", "family": "#fbbf24",
}
_DEFAULT_COLOR = "#6b7280"


def _build_faction_graph(db: Session, world_id: int):
    """Return (nodes_payload, edges_list) for all organization entities: a
    radial cluster layout by subtype, plus edges from both explicit
    entity_links and keyword-classified mentions in entity text."""
    org_entities = (
        db.query(Entity)
        .filter(Entity.world_id == world_id, Entity.kind.in_(_ORG_KINDS))
        .order_by(Entity.subtype, Entity.name)
        .all()
    )
    if not org_entities:
        return {"nodes": [], "groups": []}, []

    clusters: dict = {}
    for e in org_entities:
        key = (e.subtype or e.kind or "other").lower()
        clusters.setdefault(key, []).append(e)

    cx, cy = 1400, 900
    cluster_r = 700
    cluster_keys = list(clusters.keys())
    node_id_map: dict = {}
    nodes = []
    for ci, ckey in enumerate(cluster_keys):
        angle = (2 * math.pi * ci / len(cluster_keys)) - math.pi / 2
        ccx = cx + cluster_r * math.cos(angle)
        ccy = cy + cluster_r * math.sin(angle)
        members = clusters[ckey]
        inner_r = max(80, min(180, 30 * len(members)))
        color = _SUBTYPE_COLOR.get(ckey, _DEFAULT_COLOR)
        for ni, e in enumerate(members):
            nangle = 2 * math.pi * ni / max(len(members), 1)
            nx = ccx + inner_r * math.cos(nangle)
            ny = ccy + inner_r * math.sin(nangle)
            nid = f"fn-{e.id}"
            node_id_map[e.id] = nid
            nodes.append({
                "id": nid, "type": "faction", "title": e.name, "body": e.summary or "",
                "color": color, "image_url": e.image_url or "", "entity_id": e.id,
                "status": "", "tags": e.tags or "", "x": round(nx), "y": round(ny),
            })

    org_ids = set(node_id_map.keys())
    links = db.execute(
        entity_links.select().where(
            entity_links.c.source_id.in_(org_ids), entity_links.c.target_id.in_(org_ids),
        )
    ).fetchall()
    edges = []
    seen_pairs = set()
    for row in links:
        src, tgt = row[0], row[1]
        pair = (min(src, tgt), max(src, tgt))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edges.append({
            "id": f"fe-{src}-{tgt}", "from": node_id_map[src], "to": node_id_map[tgt],
            "label": "", "color": "#6b7280", "style": "solid", "kind": "link", "direction": "fwd",
        })

    _RELATION_KEYWORDS = {
        "allies": ["allied", "allies", "alliance", "partner", "coalition", "cooperat", "friend"],
        "enemies": ["enemy", "enemies", "hostile", "war", "conflict", "fights", "against", "oppose", "oppos"],
        "controls": ["controls", "commands", "dominates", "puppet", "directs", "governs", "runs"],
        "subsidiary": ["subsidiary", "branch", "division", "arm of", "owned by", "sub-unit", "founded by"],
        "rivals": ["rival", "rivals", "competing", "competition", "compete", "contend", "contest"],
    }
    _EDGE_COLORS = {
        "allies": "#22c55e", "enemies": "#ef4444", "controls": "#00f0ff",
        "subsidiary": "#a78bfa", "rivals": "#f97316", "neutral": "#6b7280",
    }
    name_to_id = {e.name.lower(): e.id for e in org_entities}
    for e in org_entities:
        full_text = ((e.body or "") + " " + (e.summary or "")).lower()
        for other_name, other_id in name_to_id.items():
            if other_id == e.id or other_name not in full_text:
                continue
            pair = (min(e.id, other_id), max(e.id, other_id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pos = full_text.find(other_name)
            context = full_text[max(0, pos - 120): pos + len(other_name) + 120]
            kind = "neutral"
            for rel, kws in _RELATION_KEYWORDS.items():
                if any(kw in context for kw in kws):
                    kind = rel
                    break
            edges.append({
                "id": f"fe-{e.id}-{other_id}", "from": node_id_map[e.id], "to": node_id_map[other_id],
                "label": kind if kind != "neutral" else "", "color": _EDGE_COLORS[kind],
                "style": "dashed" if kind in ("subsidiary", "neutral") else "solid",
                "kind": kind, "direction": "fwd",
            })

    return {"nodes": nodes, "groups": []}, edges


def _upsert_board(db: Session, world_id: int, replace: Optional[str], name: str, base_slug: str,
                   description: str, nodes_payload: dict, edges: list) -> str:
    b = db.query(InvestBoard).filter(InvestBoard.slug == replace).first() if replace else None
    if b:
        b.nodes_json = json.dumps(nodes_payload)
        b.edges_json = json.dumps(edges)
        db.commit()
        return b.slug

    slug = base_slug
    i = 2
    while db.query(InvestBoard).filter(InvestBoard.slug == slug).first():
        slug = f"{base_slug}-{i}"
        i += 1
    db.add(InvestBoard(
        world_id=world_id, name=name, slug=slug, description=description,
        canvas_bg="dark", nodes_json=json.dumps(nodes_payload), edges_json=json.dumps(edges),
    ))
    db.commit()
    return slug


@router.post("/boards/generate-orgs")
def board_generate_orgs(request: Request, replace: Optional[str] = None,
                         db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    nodes_payload, edges = _build_faction_graph(db, world_id)
    world_name = world.name if world else "World"
    name = f"Factions — {world_name}"
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "factions"
    slug = _upsert_board(db, world_id, replace, name, base_slug,
                          "Auto-generated faction & organization relationship board.", nodes_payload, edges)
    return RedirectResponse(f"/boards/{slug}", status_code=303)


@router.get("/api/orgs/graph")
def orgs_graph_api(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Faction graph data without saving, for external use."""
    world, _ = get_world_ctx(request, db, active_world)
    nodes_payload, edges = _build_faction_graph(db, world.id if world else 1)
    return {"nodes": nodes_payload["nodes"], "edges": edges}


# ── Dreamlands atlas: fixed bundled reference content, not derived from world data ──

def _build_dreamlands_map():
    """Build a geographic board of the 50 canonical Dreamlands locations."""
    C_NW, C_ER, C_WS, C_SS = "#7c8fa0", "#3b82f6", "#22c55e", "#00c8ff"
    C_CX, C_DB, C_VD, C_FC = "#b44fff", "#ff6b00", "#ffe600", "#ef4444"

    RAW_NODES = [
        ("dl-moon", "Moon", "Reachable by Dreamlands ship, sailing upward where sky meets sea. Inhabited by toad-like moon-beasts allied with Nyarlathotep. Hughes Industries has not yet catalogued this world.", C_VD, 1300, 60),
        ("dl-prison-worlds-edge", "The Prison at the World's Edge", "A vast silent fortress of pale stone at the far northern edge of the Dreamlands. Built to contain Lady Adriana — Carcosian aristocrat, former Royal Huntress, imprisoned for heresy against the King in Yellow.", C_NW, 1300, 170),
        ("dl-igguzqaiz", "Igguzqaiz", "A northern city on the rim of the frozen wastes where the sky is permanently pre-dawn blue-grey. Its scholars study non-Euclidean stellar cartography.", C_NW, 810, 250),
        ("dl-inquanok", "Inquanok", "A cold twilight city carved entirely from black onyx, ruled by the enigmatic Veiled King. Its sixteen-sided Temple of the Elder Gods opens onto different dream-paths at certain celestial alignments.", C_NW, 1090, 310),
        ("dl-plateau-of-leng", "Plateau of Leng", "A bleak, wind-scoured plateau of cracked grey rock, home to the spider-like Men of Leng and their trading caravans. Night-gaunts patrol its edges.", C_NW, 1500, 260),
        ("dl-cold-waste", "Cold Waste", "A frozen, lightless wilderness stretching north of Leng toward Kadath. Time does not behave here: a three-day journey can consume three weeks of dreaming.", C_NW, 1310, 390),
        ("dl-lomar", "Lomar", "An ancient arctic realm overrun by the Inutos. Its ruined cities stand half-buried in glacier and forgetting. Architectural forms appear beneath Inquanok — Inquanok was built on Lomar's foundations.", C_FC, 610, 200),
        ("dl-sarkomand", "Sarkomand", "The ancient abandoned capital of the Men of Leng, lying in ruins on a black basalt plain. Featured in The Dream-Quest as the place where Carter and the ghouls land when traveling from the moon.", C_FC, 610, 340),
        ("dl-kadath", "Kadath", "A vast black mountain and ancient castle hidden at the top of the world, home of the Great Ones. Unreachable by any direct route. Its position mirrors Carcosa's role as a reality anchor — the two sites may be aspects of the same impossible geography.", C_CX, 1300, 510),
        ("dl-enchanted-wood", "Enchanted Wood", "The forest through which most dreamers first enter the Dreamlands, home to the Zoogs. The Zoogs have an ancient uneasy treaty with the cats of Ulthar.", C_WS, 350, 510),
        ("dl-ulthar", "Ulthar", "A modest town beyond the River Skai where an ancient law forbids any man from killing a cat. Something vast and feline-shaped — Yib-Tsathoqa — dreams beneath its oldest quarter.", C_WS, 290, 650),
        ("dl-river-skai", "River Skai", "The principal river of the west, flowing past Ulthar toward the sea. Those who drink its water report unusually vivid dreams.", C_WS, 240, 790),
        ("dl-oukranos-river", "Oukranos River", "A wide bright river ferrying barges of goods, flowers, and occasionally dream-visions downstream toward the coast.", C_WS, 600, 690),
        ("dl-ngranek", "Ngranek", "A dark volcanic mountain whose face bears a titanic carved visage — the face of a god. Night-gaunts roost along its upper ridges. Carter studies it to discover his divine ancestry.", C_WS, 690, 830),
        ("dl-hatheg-kla", "Hatheg-Kla", "A high rocky mountain where the gods of Earth once danced and sometimes still return. Barzai the Wise climbed it to see the Earth's gods and was never seen again in recognisable form.", C_WS, 510, 960),
        ("dl-dylath-leen", "Dylath-Leen", "The largest port of the Dreamlands, a city of black basalt wharfs. Dark ships dock here regularly, selling rubies at prices that should raise every alarm. Moon-beasts buy slaves here.", C_WS, 120, 910),
        ("dl-ib", "Ib", "An ancient city once ruled by grey toad-like beings who worshipped the idol of Bokrug. Empty now, but the lake beside it churns green on certain nights.", C_WS, 270, 1060),
        ("dl-hatheg", "Hatheg", "Staging point for pilgrims attempting the climb to Hatheg-Kla. Its guides are expensive, experienced, and reluctant to go all the way to the summit.", C_WS, 450, 1140),
        ("dl-mir", "Mir", "Sister town of Hatheg, hosting a weekly dream-market where travelers buy and sell visions sealed in dark glass. A truly exceptional sealed dream can sell for the price of a ship.", C_WS, 280, 1260),
        ("dl-hlanith", "Hlanith", "A city of trade on the western coast at the edge of a wide jungle. Its mercantile culture makes it one of the most tolerant cities — almost anything is available for the right price.", C_WS, 120, 1390),
        ("dl-ilarnek", "Ilarnek", "A sprawling desert trading capital ringed by bazaars and camel yards. Maintains trading relations with almost every major Dreamlands power.", C_WS, 690, 1300),
        ("dl-black-lake", "Black Lake", "A perfectly still dark lake whose surface never shows quite the right reflection — always slightly older, slightly wrong. Three fishing villages around its shore prohibit looking at reflections after dark.", C_WS, 330, 1450),
        ("dl-irem", "Irem", "A city of tent-libraries and wandering scholars, trading maps of places that may not yet exist. Its Bazaar of Uncertain Geographies moves ten yards east each morning.", C_WS, 570, 1520),
        ("dl-bnazic-desert", "Bnazic Desert", "A broad desert of bronze-coloured sand and half-buried caravan markers, where the wind uncovers and covers ruins on its own schedule.", C_WS, 750, 1540),
        ("dl-plaza-of-bones", "Plaza of Bones", "A wide public square paved with old bones worn smooth by centuries of foot traffic. At the hour before dawn the dead walk its perimeter in overlapping voices.", C_WS, 120, 1540),
        ("dl-six-kingdoms", "The Six Kingdoms", "Six neighboring western realms whose dream-courts wax and wane depending on how many sleepers dream of them each night. A kingdom that goes undreamed for too long quietly fades from the map.", C_WS, 510, 1660),
        ("dl-ooth-nargai", "Ooth-Nargai", "The valley beside the Cerenerian Sea cradling Celephaïs. Strong emotions here spontaneously manifest as temporary structures.", C_ER, 1900, 510),
        ("dl-celephais", "Celephaïs", "A golden city dreamed into existence by the dreamer-king Kuranes, set in Ooth-Nargai beside the Cerenerian Sea. Kuranes became its immortal ruler, trapped forever in his own creation.", C_ER, 2110, 390),
        ("dl-serannian", "Serannian", "A city-state floating in the cloud-banks above the Cerenerian Sea, trading in weather, wind-patterns, and high-altitude dreams. Built on cloud-stone solid only to those who believe in it firmly enough.", C_ER, 2410, 290),
        ("dl-cerenerian-sea", "Cerenerian Sea", "A vast luminous sea connecting the eastern and northern Dreamlands. Its horizon merges with the sky so ships sailing far enough begin to ascend.", C_ER, 2520, 560),
        ("dl-kiran", "Kiran", "A border town where traders, smugglers, and lost dreamers pass under lanterns burning with a pale moonlike flame. Its waystation register reveals certain travelers appearing centuries apart without apparent aging.", C_ER, 1760, 670),
        ("dl-abbey-green", "Abbey Green", "A secluded monastery where robed scholars copy forbidden dream-texts by candlelight. Its archives hold maps that shift with each dreaming cycle. Its deepest vault allegedly contains a complete Pnakotic Manuscripts text.", C_ER, 1960, 770),
        ("dl-charnal-garden", "Charnal Garden of Zura", "A lurid garden where flesh-like flowers pulse with a slow heartbeat rhythm. Those who linger too long recall deaths they never lived. The blooms lean toward dreamers as if listening.", C_ER, 2320, 780),
        ("dl-crystilan", "Crystilan", "A crystalline city whose towers bend light into impossible rainbows while mirrored streets reflect a slightly different version of each traveler.", C_ER, 2110, 940),
        ("dl-pool-of-night", "The Pool of Night", "A silent black pool whose surface never reflects the sky — instead it shows the watcher's deepest fear or most likely fate. Many do not return with their original sense of self intact.", C_ER, 2470, 940),
        ("dl-humbrecht-university", "Humbrecht University", "A great university dedicated to dream-physics, mythic cartography, and how Dreamlands topology shifts based on collective belief. Studies whether a location can be intentionally created by a group of dreamers working in unison.", C_ER, 1860, 1100),
        ("dl-garden-lands", "The Garden Lands", "A wide eastern region of orchards and terraced gardens where the land seems almost aware of being looked at. Fruit here tastes faintly of memories.", C_ER, 2320, 1100),
        ("dl-khanas", "Khanas", "A fog-shrouded port city where dream-contraband changes hands in dim dockside taverns. The customs office inspects the contents of the mind. The primary black market hub of the eastern Dreamlands.", C_ER, 2570, 1220),
        ("dl-klausener", "Klausener", "A scholarly city where dream-mathematicians argue whether a location can exist before anyone believes in it. Bitter rival of Humbrecht University.", C_ER, 2110, 1320),
        ("dl-dreadfields", "The Dreadfields", "A haunted marshy territory where the ground glows with pale phosphorescent fungi. Once a battlefield between rival dream-cults. Fear-echoes ambush visitors without warning.", C_SS, 810, 1730),
        ("dl-sona-nyl", "Sona-Nyl", "The Land of Fancy — a luminous southern realm where the grass is a shade of green not seen in waking life and architecture follows no rules of gravity.", C_SS, 1110, 1830),
        ("dl-thalarion", "Thalarion", "The City of a Thousand Wonders, rising in spires of impossible architecture. Beauty so extreme it becomes unsettling. Some of its wonders watch you back.", C_SS, 1460, 1730),
        ("dl-xura", "Xura", "The Land of Pleasures Unattained — everything desired is perpetually visible but perpetually just out of reach. Its pleasures are fundamentally incomplete, designed to be pursued rather than achieved.", C_SS, 1820, 1830),
        ("dl-zar", "Zar", "The Land of Forgotten Dreams — a misty territory made entirely of memories no dreamer could hold onto upon waking. Contains half-formed cities and partial landscapes.", C_SS, 920, 1970),
        ("dl-isle-of-oriab", "Isle of Oriab", "A remote southern island with ports, jungles, and the great volcanic peak of Ngranek. Its sculptors depict faces they have never seen with uncanny accuracy.", C_SS, 1620, 2010),
        ("dl-grucian-garden", "Grucian Garden", "A lush southern garden where time is entirely unreliable. The garden's keeper has tended it for what they believe is thirty years. Outside estimates range from three hundred to three thousand.", C_SS, 2120, 1960),
        ("dl-night-ocean", "The Night Ocean", "A vast southern ocean that remains perfectly black even under the full Dreamlands sun. Ships crossing it report slow, patient breathing from beneath the hull.", C_SS, 1360, 2160),
        ("dl-sarnath", "Sarnath", "Once the greatest city of the Land of Mnar. On the thousandth anniversary of its conquest of Ib, something rose from the lake and Sarnath was simply gone.", C_FC, 350, 1870),
        ("dl-land-of-mnar", "Land of Mnar", "A storied land whose grey-etched stones predate most recorded dreaming history. Associated with the rise and fall of Sarnath. Its remaining cities maintain careful religious observances.", C_FC, 560, 2000),
        ("dl-underworld-abyss", "Underworld / Abyss", "A vast subterranean realm beneath the Dreamlands' surface, inhabited by gugs, ghasts, and ghoul-kin. The Vale of Pnath at the bottom contains titanic shapes never fully described. Psy-reactive stone shares signatures with N&D's Deepbelow Core Veins.", C_DB, 1300, 2380),
    ]

    nodes = [
        {"id": nid, "type": "location", "title": title, "body": body, "color": color,
         "image_url": "", "entity_id": None, "status": "", "tags": [], "x": x, "y": y}
        for nid, title, body, color, x, y in RAW_NODES
    ]

    groups = [
        {"id": "dg-void", "name": "The Void", "x": 1140, "y": 20, "w": 320, "h": 110},
        {"id": "dg-nw", "name": "Northern Wastes", "x": 560, "y": 140, "w": 1110, "h": 310},
        {"id": "dg-cx", "name": "Carcosa Nexus", "x": 1120, "y": 460, "w": 360, "h": 180},
        {"id": "dg-ws", "name": "Western Settlements", "x": 60, "y": 460, "w": 820, "h": 1280},
        {"id": "dg-er", "name": "Eastern Reaches", "x": 1690, "y": 230, "w": 990, "h": 1180},
        {"id": "dg-ss", "name": "Southern Seas", "x": 700, "y": 1680, "w": 1600, "h": 570},
        {"id": "dg-db", "name": "Deepbelow", "x": 1100, "y": 2320, "w": 400, "h": 160},
        {"id": "dg-fc-n", "name": "Fallen Civilisations", "x": 490, "y": 155, "w": 255, "h": 260},
        {"id": "dg-fc-s", "name": "Fallen Civilisations", "x": 230, "y": 1820, "w": 460, "h": 270},
    ]

    RAW_EDGES = [
        ("de-1", "dl-river-skai", "dl-ulthar", "flows past", "#3b82f6", "solid"),
        ("de-2", "dl-river-skai", "dl-dylath-leen", "flows to", "#3b82f6", "solid"),
        ("de-3", "dl-river-skai", "dl-ib", "near", "#3b82f6", "dashed"),
        ("de-4", "dl-oukranos-river", "dl-hlanith", "flows to", "#3b82f6", "solid"),
        ("de-5", "dl-cerenerian-sea", "dl-celephais", "borders", "#00f0ff", "solid"),
        ("de-6", "dl-cerenerian-sea", "dl-serannian", "above", "#00f0ff", "solid"),
        ("de-7", "dl-cerenerian-sea", "dl-ooth-nargai", "borders", "#00f0ff", "dashed"),
        ("de-8", "dl-night-ocean", "dl-isle-of-oriab", "borders", "#00f0ff", "solid"),
        ("de-9", "dl-night-ocean", "dl-sona-nyl", "borders", "#00f0ff", "dashed"),
        ("de-10", "dl-enchanted-wood", "dl-ulthar", "adjacent", "#6b7280", "dashed"),
        ("de-11", "dl-plateau-of-leng", "dl-inquanok", "adjacent", "#6b7280", "dashed"),
        ("de-12", "dl-cold-waste", "dl-inquanok", "south of", "#6b7280", "dashed"),
        ("de-13", "dl-cold-waste", "dl-prison-worlds-edge", "north of", "#6b7280", "dashed"),
        ("de-14", "dl-kadath", "dl-cold-waste", "approached via", "#6b7280", "dashed"),
        ("de-15", "dl-hatheg", "dl-mir", "sister town", "#6b7280", "dashed"),
        ("de-16", "dl-hatheg", "dl-hatheg-kla", "pilgrimage", "#6b7280", "dashed"),
        ("de-17", "dl-ooth-nargai", "dl-celephais", "contains", "#6b7280", "dashed"),
        ("de-18", "dl-isle-of-oriab", "dl-ngranek", "peak on", "#6b7280", "dashed"),
        ("de-19", "dl-dreadfields", "dl-night-ocean", "coastal", "#6b7280", "dashed"),
        ("de-20", "dl-six-kingdoms", "dl-ulthar", "western", "#6b7280", "dashed"),
        ("de-21", "dl-enchanted-wood", "dl-oukranos-river", "near", "#6b7280", "dashed"),
        ("de-22", "dl-lomar", "dl-inquanok", "built upon", "#ef4444", "dashed"),
        ("de-23", "dl-sarkomand", "dl-plateau-of-leng", "capital of", "#ef4444", "dashed"),
        ("de-24", "dl-sarnath", "dl-ib", "destroyed", "#ef4444", "dashed"),
        ("de-25", "dl-land-of-mnar", "dl-sarnath", "contained", "#ef4444", "dashed"),
        ("de-26", "dl-moon", "dl-sarkomand", "landed at", "#ffe600", "dashed"),
        ("de-27", "dl-moon", "dl-dylath-leen", "trades via", "#ffe600", "dashed"),
        ("de-28", "dl-underworld-abyss", "dl-kadath", "below", "#b44fff", "dashed"),
        ("de-29", "dl-night-ocean", "dl-underworld-abyss", "above", "#b44fff", "dashed"),
        ("de-30", "dl-kadath", "dl-cold-waste", "beyond", "#b44fff", "dashed"),
    ]
    edges = [
        {"id": eid, "from": frm, "to": to, "label": lbl, "color": col, "style": sty,
         "kind": "link", "direction": "fwd"}
        for eid, frm, to, lbl, col, sty in RAW_EDGES
    ]

    return {"nodes": nodes, "groups": groups}, edges


@router.post("/boards/generate-dreamlands")
def board_generate_dreamlands(request: Request, replace: Optional[str] = None,
                               db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    nodes_payload, edges = _build_dreamlands_map()
    slug = _upsert_board(
        db, world_id, replace, "Atlas of Dreams — Dreamlands Map", "atlas-of-dreams",
        "Geographic map of the 50 canonical Dreamlands locations, colour-coded by region.",
        nodes_payload, edges,
    )
    return RedirectResponse(f"/boards/{slug}", status_code=303)
