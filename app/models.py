from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Table, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

entity_links = Table(
    "entity_links",
    Base.metadata,
    Column("source_id", Integer, ForeignKey("entities.id"), primary_key=True),
    Column("target_id", Integer, ForeignKey("entities.id"), primary_key=True),
)

# Which specific player accounts can see an entity that's marked hidden
# (visible_to_players=False). Only consulted when the entity is hidden — an
# entity with visible_to_players=True is visible to every world member and
# never needs rows here.
entity_player_access = Table(
    "entity_player_access",
    Base.metadata,
    Column("entity_id", Integer, ForeignKey("entities.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
)

class World(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    description = Column(String(512), nullable=True)
    accent = Column(String(16), default="#00f0ff")
    # Whether player members can see each other's Player Characters (read-only) or only their own.
    players_see_party = Column(Boolean, default=True)
    # Per-world rules text (Markdown) shown on the /rules page. NULL = fall back
    # to the bundled Neon & Dragons core_rules.md, for worlds actually running
    # N&D and any world created before this field existed.
    rules_md = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    entities = relationship("Entity", back_populates="world", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(256), default="")
    # GM accounts have full access to every world; player accounts only see worlds
    # they've been invited to (WorldMembership), and only their own character(s).
    is_gm = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class WorldMembership(Base):
    """A player's invited access to a specific World. GM accounts don't need a row
    here — is_gm implies access to every world."""
    __tablename__ = "world_memberships"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    world = relationship("World")
    user = relationship("User")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    max_uses = Column(Integer, nullable=True)   # NULL = unlimited
    uses_count = Column(Integer, default=0)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    world = relationship("World")


class PrivateNote(Base):
    """A private note from the GM to one specific player about their character/arc —
    visible to the GM and that one player only, never to other party members."""
    __tablename__ = "private_notes"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    player_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # the GM who wrote it
    title = Column(String(256), default="")
    content = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Entity(Base):
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, default=1, index=True)
    kind = Column(String(32), nullable=False, index=True)
    subtype = Column(String(64), nullable=True)
    name = Column(String(256), nullable=False)
    folder = Column(String(256), nullable=True, index=True)
    tags = Column(String(512), nullable=True)
    image_url = Column(String(512), nullable=True)
    summary = Column(String(512), nullable=True)
    body = Column(Text, nullable=True)
    # GM can hide spoilers/secrets from invited players; defaults visible so existing content is unaffected.
    visible_to_players = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    world = relationship("World", back_populates="entities")
    related = relationship(
        "Entity",
        secondary=entity_links,
        primaryjoin=id == entity_links.c.source_id,
        secondaryjoin=id == entity_links.c.target_id,
        backref="referenced_by",
    )

class EntityNote(Base):
    """A discrete note attached to an entity, separate from its main body —
    the GM can jot several of these and hide/un-hide each independently of
    the entity's own visibility (e.g. reveal one detail about a location
    while keeping others secret)."""
    __tablename__ = "entity_notes"

    id = Column(Integer, primary_key=True, index=True)
    entity_id = Column(Integer, ForeignKey("entities.id"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    content = Column(Text, nullable=False)
    # GM notes are hidden by default — un-hide to reveal to the party.
    visible_to_players = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    entity = relationship("Entity", backref="notes")


class PlayerCharacter(Base):
    __tablename__ = "player_characters"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, default=1, index=True)
    # The player account that owns/manages this character (NULL = GM-managed, e.g. NPCs
    # or characters created before player accounts existed). One character per player per world.
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Identity
    name         = Column(String(256), nullable=False)
    player_name  = Column(String(256), default="")
    race         = Column(String(128), default="")
    race_id      = Column(String(128), default="")  # catalog id (races.json), set by creation wizard
    char_class   = Column(String(128), default="")
    profession_id = Column(String(128), default="")  # catalog id (professions.json), set by creation wizard
    subclass     = Column(String(128), default="")
    level        = Column(Integer, default=1)
    xp           = Column(Integer, default=0)
    background   = Column(String(128), default="")
    alignment    = Column(String(64),  default="")
    portrait_url = Column(String(512), default="")

    # Ability scores
    str_score = Column(Integer, default=10)
    dex_score = Column(Integer, default=10)
    con_score = Column(Integer, default=10)
    int_score = Column(Integer, default=10)
    wis_score = Column(Integer, default=10)
    cha_score = Column(Integer, default=10)

    # HP & combat
    max_hp     = Column(Integer, default=10)
    current_hp = Column(Integer, default=10)
    temp_hp    = Column(Integer, default=0)
    armor_class = Column(Integer, default=10)
    speed       = Column(Integer, default=30)
    hit_dice    = Column(String(32), default="1d8")
    death_saves_success = Column(Integer, default=0)
    death_saves_failure = Column(Integer, default=0)

    # Proficiencies (JSON arrays of ids)
    saving_throw_profs = Column(Text, default="[]")
    skill_profs        = Column(Text, default="[]")
    skill_expertise    = Column(Text, default="[]")
    armor_profs        = Column(String(256), default="")
    weapon_profs       = Column(String(256), default="")
    tool_profs         = Column(String(256), default="")
    languages          = Column(String(256), default="")

    # Equipment JSON: [{name, qty, weight, equipped, notes}]
    equipment_json = Column(Text, default="[]")
    currency_cp    = Column(Integer, default=0)
    currency_sp    = Column(Integer, default=0)
    currency_ep    = Column(Integer, default=0)
    currency_gp    = Column(Integer, default=0)
    currency_pp    = Column(Integer, default=0)

    # Features JSON: [{name, source, description}]
    feats_json = Column(Text, default="[]")
    # Attacks JSON: [{name, bonus, damage, dmg_type, notes}]
    attacks_json = Column(Text, default="[]")

    # Personality
    personality_traits = Column(Text, default="")
    ideals             = Column(Text, default="")
    bonds              = Column(Text, default="")
    flaws              = Column(Text, default="")
    backstory          = Column(Text, default="")
    notes              = Column(Text, default="")

    # Appearance
    age        = Column(String(32), default="")
    height     = Column(String(32), default="")
    weight_app = Column(String(32), default="")
    eyes       = Column(String(64), default="")
    skin       = Column(String(64), default="")
    hair       = Column(String(64), default="")

    # Universal stats/skills/currency (replaces D&D-specific fixed columns)
    stats_json    = Column(Text, default='[]')  # [{id,label,abbr,value}]
    skills_json   = Column(Text, default='[]')  # [{id,label,stat_id,value}]
    currency_json = Column(Text, default='[]')  # [{label,abbr,value}]

    # Optional secondary resource (mana, energy, shields, etc.)
    secondary_resource_name    = Column(String(128), default='')
    secondary_resource_max     = Column(Integer, default=0)
    secondary_resource_current = Column(Integer, default=0)

    # N&D specific resources
    shock_max     = Column(Integer, default=0)
    shock_current = Column(Integer, default=0)
    pp_current    = Column(Integer, default=0)
    mp_current    = Column(Integer, default=0)
    minor_edge    = Column(Text, default='')
    major_edge    = Column(Text, default='')
    minor_edge_count = Column(Integer, default=0)  # Major/Minor Edge counts for .ndc export
    major_edge_count = Column(Integer, default=0)
    cyberware_json  = Column(Text, default='[]')  # [{name, ca_cost, notes}]
    conditions_json = Column(Text, default='[]')  # list of active condition strings
    sheet_template_id  = Column(Integer, ForeignKey("sheet_templates.id"), nullable=True)
    custom_fields_json = Column(Text, default="{}")   # {field_id: value or [ {...}, ... ] for list fields}

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    world = relationship("World")
    sheet_template = relationship("SheetTemplate")


class SheetTemplate(Base):
    __tablename__ = "sheet_templates"

    id          = Column(Integer, primary_key=True, index=True)
    world_id    = Column(Integer, ForeignKey("worlds.id"), nullable=True)  # NULL = global/built-in
    name        = Column(String(256), nullable=False)
    slug        = Column(String(64), unique=True, nullable=False)
    description = Column(Text, default="")
    is_builtin  = Column(Boolean, default=False)
    # "nd" (default): the full N&D sheet (stats/HP/Shock/PP-MP/edges/cyberware/feats)
    # plus these fields layered on top. "custom": an entirely different system —
    # the N&D sheet is not shown at all, the character *is* these fields.
    sheet_mode  = Column(String(16), default="nd")
    # [{id, label, type, section, default_value}] for simple fields, or
    # [{id, label, type:"list", section, item_fields:[{id,label,type}]}] for a
    # repeatable group (e.g. a list of abilities, each with name/tier/effect).
    # type: number | resource | text | textarea | table | list
    # section: freeform label used to group fields on the sheet
    fields_json = Column(Text, default="[]")
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StarredImage(Base):
    __tablename__ = "starred_images"

    id          = Column(Integer, primary_key=True, index=True)
    url         = Column(String(512), nullable=False, unique=True)
    prompt      = Column(Text, default="")
    negative    = Column(Text, default="")
    model       = Column(String(256), default="")
    seed        = Column(Integer, default=-1)
    params_json = Column(Text, default="{}")
    created_at  = Column(DateTime, default=datetime.utcnow)


class MapOverlay(Base):
    __tablename__ = "map_overlays"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False)
    custom_markers_json = Column(Text, default="[]")
    custom_regions_json = Column(Text, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class InvestBoard(Base):
    __tablename__ = "invest_boards"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, default=1, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    description = Column(String(512), nullable=True)
    nodes_json = Column(Text, default="[]")
    edges_json = Column(Text, default="[]")
    canvas_bg = Column(String(32), default="cork")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Schematic(Base):
    __tablename__ = "schematics"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, default=1, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    description = Column(String(512), nullable=True)
    is_html = Column(Boolean, default=False)
    html_file = Column(String(128), nullable=True)
    image_url = Column(String(512), nullable=True)
    markers_json = Column(Text, nullable=True)
    # SVG editor fields
    canvas_width = Column(Integer, default=2000)
    canvas_height = Column(Integer, default=1500)
    canvas_bg = Column(String(32), default="dark")
    elements_json = Column(Text, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
