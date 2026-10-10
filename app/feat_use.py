"""Reading a feat's rules text for what it costs and how often it can be used (Neon & Dragons feat descriptions say things
like "Cost: 2 MP — Action", "Cost: PP, Health", "Once per session"). The text is free-form and not always numeric, so these
are SUGGESTIONS the player confirms before spending: a resource with no number means 1."""
import re

RESOURCES = {"pp": "pp", "mp": "mp", "shock": "shock", "health": "hp", "hp": "hp"}
_TOKEN = re.compile(r"(?:(\d+)\s*)?\b(PP|MP|Shock|Health|HP)\b", re.I)
_LIMIT = re.compile(r"\bonce\s+(?:per|a|each)\s+(rest|session|day|scene)\b", re.I)


def parse_cost(description: str) -> dict:
    """{"pp": n, "mp": n, "shock": n, "hp": n} for the resources the first "Cost:" line names ({} when it names none)."""
    text = str(description or "").replace("*", "")
    m = re.search(r"\bCost:\s*([^\n]{0,80})", text, re.I)
    if not m:
        return {}
    out = {}
    for num, res in _TOKEN.findall(m.group(1)):
        key = RESOURCES[res.lower()]
        out[key] = out.get(key, 0) + (int(num) if num else 1)
    return {k: min(v, 10) for k, v in out.items()}


def parse_limit(description: str):
    """"rest" / "session" for a feat that can be used once per Rest / session (a day or a scene is treated as a Rest), else None."""
    m = _LIMIT.search(str(description or ""))
    if not m:
        return None
    return "session" if m.group(1).lower() == "session" else "rest"


def feat_key(feat: dict) -> str:
    return str(feat.get("id") or feat.get("name") or "").lower()
