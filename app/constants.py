KINDS = ["character", "location", "organization", "creature", "event", "item", "feat", "note"]

SUBTYPES = {
    "character": ["NPC", "PC", "villain", "ally", "neutral"],
    "location": ["district", "city", "country", "void station", "moon", "ruin", "corp facility"],
    "organization": ["megacorp", "syndicate", "government", "cult", "secret society", "gang", "AI entity", "family"],
    "creature": ["mutant", "animal", "abomination", "corp-enhanced", "ice creature", "undead"],
    "event": ["corporate war", "outbreak", "disaster", "political", "yellow corruption", "discovery"],
    "item": ["weapon", "armor", "augment", "bio-augmentation", "drone", "husk", "vehicle", "oddity", "metal", "item"],
    "feat": ["common feat", "origin feat", "profession feat", "profession ability", "psy power", "race feat"],
    "note": ["lore", "session note", "rumor", "prophecy", "theory"],
}

KIND_ICONS = {
    "character": "👤", "location": "🗺", "organization": "🏢",
    "creature": "☠", "event": "⚡", "item": "⚙", "feat": "✦", "note": "📄",
}

# Skills: (id, governing_stat_label)
SKILLS = [
    ("athletics",       "STR"),
    ("acrobatics",      "DEX"),
    ("sleight_of_hand", "DEX"),
    ("stealth",         "DEX"),
    ("arcana",          "INT"),
    ("history",         "INT"),
    ("investigation",   "INT"),
    ("lore",            "INT"),
    ("technology",      "INT"),
    ("hacking",         "INT"),
    ("insight",         "WIS"),
    ("medicine",        "WIS"),
    ("perception",      "WIS"),
    ("survival",        "WIS"),
    ("deception",       "CHA"),
    ("intimidation",    "CHA"),
    ("performance",     "CHA"),
    ("persuasion",      "CHA"),
    ("streetwise",      "CHA"),
]

SAVING_THROWS = ["str", "dex", "con", "int", "wis", "cha"]

XP_THRESHOLDS = [
    0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
    85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
]

ALIGNMENTS = [
    "Lawful Good", "Neutral Good", "Chaotic Good",
    "Lawful Neutral", "True Neutral", "Chaotic Neutral",
    "Lawful Evil", "Neutral Evil", "Chaotic Evil",
]
