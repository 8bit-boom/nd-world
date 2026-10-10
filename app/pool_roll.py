"""The two dice engines of the supported rulebooks - pure functions, no database, an injectable random source.

* **Stat check** (Neon & Dragons, Chronicles of the Worm): Stat + d10 + points spent (max 2, from the pool matching the stat's
  category: PP for physical stats, MP for mental). A natural 10 is a Critical Success, a natural 1 an Automatic Failure.
  Advantage = roll 2d10 keep the higher, Disadvantage = keep the lower.
* **Success pool** (Hunt in the Moonlight, Game of Gods / Asterion): roll N d10; every die showing 6+ is a success; a 10 is a
  success AND explodes (roll one more die, which may explode again). Bigger pools come from abilities, spent Stamina / Ichor
  and situational dice; a pool is never smaller than 1d10."""
import random
from typing import Optional

PHYSICAL = ("str", "dex", "bod", "per")
MENTAL = ("wil", "int", "cha", "itu")
MAX_BOOST = 2
MAX_POOL = 20
MAX_EXTRA_DICE = 40            # a runaway chain of exploding 10s is cut here (about 1 in 10**40 anyway)


def stat_pool(stat_id: str) -> Optional[str]:
    """Which resource boosts a roll of this stat: "pp" (physical), "mp" (mental) or None for an unknown stat."""
    return "pp" if stat_id in PHYSICAL else "mp" if stat_id in MENTAL else None


def roll_check(value: int, boost: int = 0, mode: str = "normal", rng=None) -> dict:
    rng = rng or random
    n = 2 if mode in ("adv", "dis") else 1
    rolls = [rng.randint(1, 10) for _ in range(n)]
    kept = max(rolls) if mode == "adv" else min(rolls) if mode == "dis" else rolls[0]
    return {"rolls": rolls, "die": kept, "total": int(value) + kept + int(boost), "crit": kept == 10, "fail": kept == 1}


def roll_pool(n: int, threshold: int = 6, explode: bool = True, rng=None) -> dict:
    """-> {"dice": [{"v", "ok", "extra"}...], "successes", "n"}; `extra` marks a die rolled because a 10 exploded."""
    rng = rng or random
    n = max(1, min(MAX_POOL, int(n)))
    dice, extra, pending = [], 0, n
    while pending > 0:
        pending -= 1
        v = rng.randint(1, 10)
        dice.append({"v": v, "ok": v >= threshold, "extra": len(dice) >= n})
        if explode and v == 10 and extra < MAX_EXTRA_DICE:
            extra += 1
            pending += 1
    return {"dice": dice, "successes": sum(1 for d in dice if d["ok"]), "n": n}
