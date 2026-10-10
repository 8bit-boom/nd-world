"""Spending XP on a Neon & Dragons character (Player's Guide, "Progression & XP"): a stat goes up for NEW RANK x 2 XP, a
Race or Profession feat costs Rank x 4, a Common feat Rank x 3; you can only buy feats of a Rank you have unlocked - you start
with Rank 1, and owning 2 feats of your highest unlocked Rank unlocks the next (Ranks 1-3).

The sheet's `xp` is what the character has EARNED (it also drives the level); what has been spent is kept apart (the character's
`xp_spent` preference), so available = earned - spent. Pure functions over the sheet's JSON - the route does the saving."""
import re
from typing import Optional

STATS = (("str", "Strength"), ("dex", "Dexterity"), ("bod", "Body"), ("per", "Perception"),
         ("wil", "Willpower"), ("int", "Intellect"), ("cha", "Charisma"), ("itu", "Intuition"))
FEAT_MULT = {"Race": 4, "Profession": 4, "Common": 3}
MAX_RANK = 3
MAX_STAT = 10          # a table sanity cap: the guide's stats top out well below this


def rank_num(rank) -> int:
    m = re.search(r"(\d)", str(rank or ""))
    return int(m.group(1)) if m else 0


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def unlocked_rank(owned: list) -> int:
    """Highest Rank the character may buy from: 1, +1 for every Rank of which they own two feats (capped at 3)."""
    ranks = [rank_num(f.get("rank")) for f in owned if isinstance(f, dict)]
    top = 1
    while top < MAX_RANK and sum(1 for r in ranks if r == top) >= 2:
        top += 1
    return top


def stat_cost(current: int) -> int:
    return (int(current) + 1) * 2


def feat_cost(category: str, rank: int) -> int:
    return FEAT_MULT.get(category, 0) * rank


PHYS = ("str", "dex", "bod", "per")
MENT = ("wil", "int", "cha", "itu")


def stat_effect(pc, stats: dict, sid: str) -> str:
    """What one more point in `sid` changes on the sheet, from the same rules as pc_stats.pc_maxima (PP = STR+DEX+BOD+PER,
    MP = WIL+INT+CHA+ITU, HP = PP + 10 unless the sheet stores its own, Shock = MP unless it stores its own, Speed = DEX+ITU)."""
    bits = []
    phys = sum(stats.get(k, 0) for k in PHYS)
    ment = sum(stats.get(k, 0) for k in MENT)
    if sid in PHYS:
        bits.append(f"PP {phys}\u2192{phys + 1}")
        if not (getattr(pc, "max_hp", 0) or 0) > 0:
            bits.append(f"HP {phys + 10}\u2192{phys + 11}")
    elif sid in MENT:
        bits.append(f"MP {ment}\u2192{ment + 1}")
        if not (getattr(pc, "shock_max", 0) or 0) > 0:
            bits.append(f"Shock {ment}\u2192{ment + 1}")
    if sid in ("dex", "itu"):
        sp = stats.get("dex", 0) + stats.get("itu", 0)
        bits.append(f"Speed {sp}\u2192{sp + 1}")
    return ", ".join(bits)


def unlock_hint(owned: list, category_rank: int) -> str:
    """'' or a note that this purchase is the second feat of its Rank, which unlocks the next Rank."""
    if category_rank >= MAX_RANK:
        return ""
    ranks = [rank_num(f.get("rank")) for f in owned if isinstance(f, dict)]
    if unlocked_rank(owned) == category_rank and sum(1 for r in ranks if r == category_rank) == 1:
        return f"unlocks Rank {category_rank + 1} feats"
    return ""


def _owned_keys(owned: list) -> set:
    keys = set()
    for f in owned:
        if isinstance(f, dict):
            keys.add(str(f.get("id") or "").lower())
            keys.add(str(f.get("name") or "").lower())
    keys.discard("")
    return keys


def options(pc, catalog: list, earned: int, spent: int) -> dict:
    """Everything the character could buy right now (and what blocks the rest)."""
    import json
    try:
        stats = {str(s.get("id")): int(s.get("value") or 0) for s in json.loads(pc.stats_json or "[]") if isinstance(s, dict)}
        owned = json.loads(pc.feats_json or "[]")
    except (ValueError, TypeError):
        stats, owned = {}, []
    available = max(0, earned - spent)
    top = unlocked_rank(owned)
    race = pc.race_id or slug(pc.race)
    prof = pc.profession_id or slug(pc.char_class)
    have = _owned_keys(owned)
    stat_rows = [{"id": sid, "label": label, "value": stats.get(sid, 0), "cost": stat_cost(stats.get(sid, 0)),
                  "can": stats.get(sid, 0) < MAX_STAT and stat_cost(stats.get(sid, 0)) <= available,
                  "effect": stat_effect(pc, stats, sid), "left": max(0, available - stat_cost(stats.get(sid, 0)))} for sid, label in STATS]
    feats = []
    for f in catalog:
        cat, rk = f.get("category"), rank_num(f.get("rank"))
        if cat not in FEAT_MULT or not (1 <= rk <= MAX_RANK):
            continue
        if cat == "Race" and f.get("associatedRace") != race:
            continue
        if cat == "Profession" and f.get("associatedProfession") != prof:
            continue
        if str(f.get("id") or "").lower() in have or str(f.get("name") or "").lower() in have:
            continue
        cost = feat_cost(cat, rk)
        why = "" if rk <= top else f"Rank {rk} unlocks after you own two Rank {rk - 1} feats"
        if not why and cost > available:
            why = f"needs {cost - available} more XP"
        feats.append({"id": f["id"], "name": f["name"], "category": cat, "rank": rk, "cost": cost,
                      "description": str(f.get("description") or "")[:400].replace("\n---", "").strip(),
                      "can": not why, "why": why, "effect": unlock_hint(owned, rk), "left": max(0, available - cost)})
    feats.sort(key=lambda x: (x["rank"], x["category"], x["name"]))
    return {"earned": earned, "spent": spent, "available": available, "unlocked_rank": top, "stats": stat_rows, "feats": feats,
            "race": race, "profession": prof}


def find_feat(catalog: list, feat_id: str) -> Optional[dict]:
    return next((f for f in catalog if str(f.get("id")) == str(feat_id)), None)
