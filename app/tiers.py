"""Grouping a world's races / professions into the Standard · Advanced · Exceptional tabs of their catalog pages.

An entity's tier is its `subtype`. The generic entity form lets that be blank ("— none —"), and imports, the MCP tools and
hand-typed values can leave it capitalised, padded or free text. Tabs that matched the three tier names exactly dropped
every such entity: it was in the entity list and the folder counts and in NO tab of the catalog. So the match ignores
case and padding, and whatever still doesn't fit goes to an extra "unsorted" group that the page shows as its own tab."""
from typing import Dict, Iterable, List, Optional, Sequence

UNSORTED = "unsorted"


def tier_of(subtype: Optional[str], tiers: Sequence[str]) -> str:
    """The tier `subtype` names (ignoring case and surrounding space), or UNSORTED."""
    key = (subtype or "").strip().lower()
    return key if key in tiers else UNSORTED


def group_by_tier(entities: Iterable, tiers: Sequence[str]) -> Dict[str, List]:
    """{tier: [entity…]} for every tier (always present, possibly empty) plus UNSORTED; each group sorted by name."""
    groups: Dict[str, List] = {t: [] for t in (*tiers, UNSORTED)}
    for e in entities:
        groups[tier_of(getattr(e, "subtype", None), tiers)].append(e)
    for g in groups.values():
        g.sort(key=lambda e: (e.name or "").lower())
    return groups
