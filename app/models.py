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
    # Whether players may use the "Download as .md" button on the Rules page.
    # Off by default — a GM must opt in per world. Independent of whether the
    # rules content itself is visible (it always is; /rules is player-safe).
    players_can_download_rules = Column(Boolean, default=False)
    # Whether players may download an entity (or a kind's entities in bulk)
    # as .md/.zip files. A separate axis from visible_to_players/
    # _entity_view_gate — that still governs whether an entity can be VIEWED
    # at all; this only governs whether a visible entity can be extracted as
    # a file. Off by default.
    players_can_download_entities = Column(Boolean, default=False)
    # Whether players may use the "Ask AI" panel on an entity's page (POST
    # /api/ai/stream). Off by default — a GM must opt in per world. The
    # dedicated GM "/ai" World Chat page/tools stay GM-only regardless of
    # this toggle; it only governs the focused per-entity chat panel.
    players_can_ask_ai = Column(Boolean, default=False)
    # Per-world rules text (Markdown) shown on the /rules page. NULL = fall back
    # to the bundled Neon & Dragons core_rules.md, for worlds actually running
    # N&D and any world created before this field existed.
    rules_md = Column(Text, nullable=True)
    # GM-authored blurb shown at the top of the world's home page (/),
    # Markdown, rendered with the same `md` Jinja filter as rules_md/entity
    # bodies. NULL = no blurb.
    home_welcome_md = Column(Text, nullable=True)
    # Ordered list of GM-defined home page tabs/sections, each holding its
    # own ordered list of curated links:
    # [{name, visible_to_players, links: [{label, icon, target_type,
    #   target_ref, visible_to_players}, ...]}, ...].
    # See app/routers/home_content.py for validation and
    # app/main.py's _resolve_home_sections for how it's rendered. Link
    # entries are a snapshot — label/icon are GM-set at add-time, not
    # re-derived from the target on every render — matching
    # Quest.linked_entities_json / GameSession.npcs_json. A link/section is
    # only shown to players if both its own visible_to_players and its
    # parent section's are true.
    home_sections_json = Column(Text, default="[]")
    # GM-defined custom entity categories on top of the fixed built-ins in
    # app/constants.py's KINDS — each gets its own nav tab and home stat
    # tile, exactly like a built-in kind. Ordered list, array order IS
    # display order:
    # [{id, label, icon, subtypes: [str,...], created_at}, ...].
    # `id` is permanent once created (renaming only changes label/icon/
    # subtypes) and always "custom_" + a slugified label (see
    # app/deps.py's CUSTOM_KIND_PREFIX), so it can never collide with a
    # built-in kind added by a future app update. See app/deps.py's
    # effective_kinds()/load_custom_kinds() for parsing/merging and
    # app/routers/kinds_admin.py for validation on write.
    custom_kinds_json = Column(Text, default="[]")
    # Home page hero customization. NULL for either text field falls back to
    # the bundled "WORLD DATABASE" / "Neon & Dragons worldbuilding codex"
    # default in index.html, so existing worlds render unchanged until a GM
    # opts in. Rendered as plain (auto-escaped) text, never markdown/HTML —
    # unlike home_welcome_md, this sits inside the page's <h1>/<p>, and a
    # custom title losing its two-tone "WORLD\nDATABASE" styling is an
    # acceptable trade for not having to trust GM-authored HTML there.
    home_title = Column(String(200), nullable=True)
    home_subtitle = Column(String(300), nullable=True)
    # /uploads/... path (or GM-pasted http(s) URL) shown as the hero
    # section's background-image. Same upload path as any other image in
    # this app (see app/routers/home_content.py's _upload_home_background).
    home_background_url = Column(String(512), nullable=True)
    # Extra tiles pinned onto the home page's stat-tile dashboard, alongside
    # the built-in per-kind counters — the drag-a-nav-tab-onto-the-dashboard
    # interaction (index.html) appends here via
    # POST /api/worlds/{id}/home/pinned-tile, reusing the exact same link
    # shape (and sanitizer) as one entry in home_sections_json's `links`:
    # [{label, icon, target_type, target_ref, visible_to_players}, ...].
    # Kept as a flat list (not sections) since these render inline in one
    # grid, not tabbed panes like Quick Links.
    home_pinned_tiles_json = Column(Text, default="[]")
    # Kind ids (built-in or custom) the GM has chosen to hide from the home
    # page's default stat-tile dashboard — e.g. Races/Professions in a world
    # that isn't using them. A hover ✕ on each tile (index.html, GM view
    # only) appends here via POST /api/worlds/{id}/home/hidden-kinds; the
    # Default Tiles checklist on home_edit.html manages the full set,
    # including restoring one. Just a list of ids: ["races", "professions"].
    home_hidden_kinds_json = Column(Text, default="[]")
    # GM-defined grouping of the top-nav's GM-only utility pages (Boards,
    # Quests, AI Chat, ...) into dropdown menus. Deliberately nullable with
    # NO string default (unlike every other *_json column on this model) —
    # NULL means "never customized," which app/nav_menus.py's
    # load_nav_menus() takes as a signal to fall back to the shipped
    # Tools/AI Tools grouping, while an explicitly-saved "[]" (the GM wants
    # everything as flat tabs) is honored as real. See
    # app/routers/nav_menus_admin.py for the sanitizer and the Navigation
    # tab on /settings for the editor.
    nav_menus_json = Column(Text, nullable=True)
    # The image currently pushed to players as a full-screen popup
    # ("Spotlight"), or NULL when nothing is being shown. Set by
    # POST /images/spotlight, cleared by POST /images/spotlight/clear (both
    # GM-only, see app/routers/gallery.py). spotlight_version increments on
    # every set/clear so the player-side poller (GET /api/spotlight,
    # app/templates/base.html) can tell "new broadcast" apart from "still
    # the same one, don't re-open the popup" — same distinct-change-
    # detection idea as the schematic view.json poller, just via a version
    # counter instead of a content diff.
    spotlight_image_url = Column(String(512), nullable=True)
    spotlight_label = Column(String(256), nullable=True)
    spotlight_version = Column(Integer, default=0)
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
    # Bumped whenever a password change should invalidate every other session for
    # this user (see /account/password); compared against a value stashed in the
    # signed session cookie at login. No server-side session store otherwise exists.
    session_version = Column(Integer, default=1, nullable=False)
    # Optional TOTP-based two-step authentication (see app/totp.py). totp_secret
    # is only meaningful once totp_enabled is True — a GM/player can start setup
    # (POST /account/2fa/setup GET) without committing to it; the secret is
    # stashed in the session, not here, until the confirmation code is verified.
    # totp_backup_codes_json holds sha256 hashes of unused single-use backup
    # codes (see app/totp.py's hash_backup_code/consume_backup_code) — never the
    # raw codes, same rationale as ApiToken.token_hash.
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False)
    totp_backup_codes_json = Column(Text, default="[]")


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
    # Optional structured fields (age/title/gender/status, stat blocks, etc.) on
    # top of the free-text body — see EntityTemplate for the field definitions.
    template_id = Column(Integer, ForeignKey("entity_templates.id"), nullable=True)
    custom_fields_json = Column(Text, default="{}")  # {field_id: value or [ {...}, ... ] for list fields}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    world = relationship("World", back_populates="entities")
    template = relationship("EntityTemplate")
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
    # True only for a note imported from .html/.htm with "preserve original
    # formatting" checked — content is then already-sanitized HTML (see
    # rendering.sanitize_note_html) and must be rendered with the `safe`
    # filter directly, NOT re-run through the `md` filter like every other
    # note's plain markdown content. False (the default) covers every
    # existing note plus every other import path (typed text, markdown/text
    # file, PDF text, or the HTML-to-markdown conversion mode).
    content_is_html = Column(Boolean, default=False)
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
    app_extra_json = Column(Text, default="{}")  # passthrough for mobile-app-only fields not modeled here (see character-sync API)

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


class EntityTemplate(Base):
    """Reusable custom-field / stat-block definitions for lore Entities (NPCs,
    creatures, locations, etc.) — separate from SheetTemplate, which is only
    for Player Character sheets."""
    __tablename__ = "entity_templates"

    id          = Column(Integer, primary_key=True, index=True)
    world_id    = Column(Integer, ForeignKey("worlds.id"), nullable=True)  # NULL = global/built-in
    name        = Column(String(256), nullable=False)
    slug        = Column(String(64), unique=True, nullable=False)
    kind        = Column(String(32), nullable=True)  # one of KINDS, or NULL = usable on any kind
    description = Column(Text, default="")
    is_builtin  = Column(Boolean, default=False)
    # [{id, label, type, section, default_value}] for simple fields,
    # [{id, label, type:"select", section, options:["Alive","Dead",...]}] for a dropdown,
    # or [{id, label, type:"list", section, item_fields:[{id,label,type}]}] for a
    # repeatable group (e.g. a stat block's list of abilities).
    # type: number | text | textarea | select | list
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

class RandomTable(Base):
    """A weighted roll table (encounters, loot, NPCs, etc). world_id=NULL is a
    global/built-in table available to every world, same pattern as
    EntityTemplate/SheetTemplate."""
    __tablename__ = "random_tables"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=True, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    category = Column(String(64), default="general")
    description = Column(Text, default="")
    is_builtin = Column(Boolean, default=False)
    entries_json = Column(Text, default="[]")  # [{label, weight}]
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CombatSession(Base):
    """A live encounter: initiative order, HP/Shock/conditions per combatant."""
    __tablename__ = "combat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    # [{id, name, source:"pc"|"entity"|"manual", pc_id, entity_id, initiative,
    #   max_hp, hp, max_shock, shock, armor, conditions:[str], notes}]
    combatants_json = Column(Text, default="[]")
    round_num = Column(Integer, default=1)
    active_idx = Column(Integer, default=0)
    game_session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Party(Base):
    """A named group of PlayerCharacters (and optionally GM-run companion
    Entities) — shared loot ledger and a one-click launch into combat."""
    __tablename__ = "parties"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    member_pc_ids_json = Column(Text, default="[]")      # [PlayerCharacter.id, ...]
    member_entity_ids_json = Column(Text, default="[]")  # [Entity.id, ...] companions/hirelings
    loot_json = Column(Text, default="[]")                # [{name, qty, notes}]
    notes = Column(Text, default="")
    # Where this party currently is, for GM tracking. {} = not placed anywhere.
    # Map:       {"kind": "map", "slug": <map slug>, "lat": .., "lng": ..}
    # Schematic: {"kind": "schematic", "slug": <schematic slug>, "x": .., "y": ..}
    location_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Quest(Base):
    """A structured quest/plot log entry, with optional sub-quests, linked
    lore entities, and an assigned party."""
    __tablename__ = "quests"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    status = Column(String(32), default="active")   # active/complete/failed/secret (freeform)
    category = Column(String(32), default="main")   # main/side/personal (freeform)
    summary = Column(String(512), default="")
    body = Column(Text, default="")                  # markdown
    linked_entities_json = Column(Text, default="[]")  # [{entity_id, role}]
    parent_id = Column(Integer, ForeignKey("quests.id"), nullable=True, index=True)
    assigned_party_id = Column(Integer, ForeignKey("parties.id"), nullable=True, index=True)
    # GM can hide a quest from players entirely (e.g. a plot thread they haven't
    # discovered yet) — same field name/semantics as Entity.visible_to_players.
    visible_to_players = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent = relationship("Quest", remote_side=[id], backref="sub_quests")
    party = relationship("Party")


class ImageAlbum(Base):
    """A GM-curated named collection of images for the /images gallery tab —
    just an ordered list of /uploads/... URLs, not a copy of the files
    themselves. An image can belong to any number of albums (or none); an
    image already used somewhere in the world (an entity portrait, an
    inline body/note embed, a character portrait) can be added here, and an
    album can also receive brand-new uploads not used anywhere else yet.

    parent_id lets an album nest inside another (a "folder" is just an
    album used purely for organization) — self-referential, so albums form
    an arbitrarily deep tree per world. NULL means top-level. See
    app/routers/gallery.py for breadcrumb/cascade-delete handling."""
    __tablename__ = "image_albums"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    image_urls_json = Column(Text, default="[]")  # ordered list of "/uploads/..." strings
    parent_id = Column(Integer, ForeignKey("image_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AudioAlbum(Base):
    """A GM-created folder for organizing AudioClips into albums and nested
    sub-albums — the same self-referential parent_id tree as ImageAlbum,
    but a clip points back at its album directly (AudioClip.album_id)
    rather than the album holding a list of ids: an audio clip, unlike a
    shared image URL, only ever lives in one place. NULL parent_id means
    top-level. See app/routers/audio.py for breadcrumb/cascade-delete
    handling."""
    __tablename__ = "audio_albums"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("audio_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AudioClip(Base):
    """A GM-uploaded audio file (ambiance, a sound effect, an NPC voice
    line, a recorded handout) for the /audio library — see
    app/routers/audio.py. Unlike ImageAlbum, each row owns exactly one file
    (no sharing/reuse across rows), so deleting the row always deletes the
    file too. visible_to_players mirrors Entity's own default-visible
    convention: a GM only has to act to hide a clip, not to reveal one.
    album_id is NULL for a top-level (unfiled) clip."""
    __tablename__ = "audio_clips"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    description = Column(String(512), default="")
    file_url = Column(String(512), nullable=False)  # "/uploads/audio/<file>"
    visible_to_players = Column(Boolean, default=True)
    album_id = Column(Integer, ForeignKey("audio_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class GameSession(Base):
    """A per-session prep/recap log: prep checklist, NPCs featured, loot
    given, XP awarded. Named GameSession (not Session) to avoid colliding
    with SQLAlchemy's own Session type used throughout the app."""
    __tablename__ = "game_sessions"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    session_num = Column(Integer, default=1)
    session_date = Column(String(32), nullable=True)
    summary = Column(Text, default="")    # recap, markdown
    prep_json = Column(Text, default="[]")   # [{task, done}]
    npcs_json = Column(Text, default="[]")   # [{entity_id, name}]
    loot_json = Column(Text, default="[]")   # [{name, qty, notes}]
    xp_awarded = Column(Integer, default=0)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=True, index=True)
    # Raw Whisper transcript accumulated live during a session recording,
    # one short chunk (see MAX_LIVE_CHUNK_SECONDS in routers/sessions.py) at
    # a time via /api/sessions/{id}/live-transcript/append — persisted to
    # the DB immediately after every chunk (not held in browser memory
    # until the end) specifically so a multi-hour recording survives a
    # crashed tab or dropped connection with at most one chunk's worth of
    # audio lost. Separate from `summary`, which is the polished recap a GM
    # writes/applies an AI draft into — this is the messy raw material that
    # feeds it, via summarize_transcript.
    live_transcript = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    party = relationship("Party")


class AudioJob(Base):
    """A durable background job for transcribing (and, for
    purpose="session_recap", also summarizing) an uploaded audio file — see
    app/audio_jobs.py. Unlike the direct one-shot upload routes (still the
    default — this is an opt-in "process in background" alternative), the
    actual work runs as a background asyncio task in the server process,
    independent of any one HTTP connection, so it survives the browser tab
    that started it being closed; every state transition is persisted here
    so a GM can navigate away and check back later (even from a different
    browser) via the recent-jobs list. Does NOT survive the server process
    itself restarting mid-job — see app/audio_jobs.py's startup sweep,
    which marks any job still in progress at boot as failed rather than
    leaving it stuck."""
    __tablename__ = "audio_jobs"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # "session_recap" (transcribe + summarize, feeds a GameSession's recap
    # draft) or "attachment" (transcribe only — the Whisper Test tab and an
    # AI Chat/Ask AI voice-memo attachment are mechanically identical on the
    # backend; only what the client does with a finished job differs).
    purpose = Column(String(32), nullable=False)
    # Only set for purpose="session_recap" when started from an existing
    # session's page — this whole flow is otherwise session-independent,
    # same as the direct /api/sessions/ai/summarize-from-audio route.
    game_session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=True, index=True)
    filename = Column(String(256), default="")
    # pending -> transcribing -> [summarizing ->] done, or -> error at any point.
    status = Column(String(32), default="pending")
    error = Column(Text, default="")
    transcript = Column(Text, default="")
    recap = Column(Text, default="")  # only populated for purpose="session_recap"
    # Only populated for purpose="attachment" once done — where the
    # uploaded audio ended up, so it can be attached to a chat message
    # without re-uploading (same shape as /api/ai/attachments/upload's own
    # "url" field).
    attachment_url = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Fact(Base):
    """A single, discrete piece of what-happened-in-play — unlike GameSession.summary
    (one free-text recap blob), each Fact is small enough to have its own
    visible_to_players flag, so a GM can record a scene the players witnessed
    right next to the secret truth behind it without exposing the secret.
    Optionally tied to the GameSession it came from."""
    __tablename__ = "facts"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    game_session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    visible_to_players = Column(Boolean, default=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    world = relationship("World")
    game_session = relationship("GameSession")
    author = relationship("User")


class ApiToken(Base):
    """A personal access token for the MCP server (see app/mcp_server.py) —
    lets a GM or player update/query their world from an MCP client (e.g. a
    phone) without a browser session cookie. Any user may generate one; MCP
    tools that require GM access still check user.is_gm at call time, exactly
    like the web UI, so a player's token can never do more than the player
    already could."""
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)  # sha256 hex digest
    label = Column(String(256), default="")
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class TrustedDevice(Base):
    """A "trust this device for 30 days" 2FA opt-out, set from the
    /login/2fa checkbox — see app/routers/auth.py's _begin_2fa_or_login and
    app/totp.py's trust-token helpers. Only the sha256 hash of the raw
    cookie value is ever stored (same pattern as ApiToken), so a stolen DB
    row alone can't be replayed as a working cookie. Deleting the row (via
    expiry, or an explicit revoke from /account) makes this device prompt
    for a 2FA code again on its next login."""
    __tablename__ = "trusted_devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)  # sha256 hex digest
    label = Column(String(256), default="")  # raw User-Agent at creation, so a GM can tell devices apart
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class WorldCalendar(Base):
    """One custom in-world calendar per world — era name, current day, and
    configurable month lengths. Lazily created on first /calendar visit."""
    __tablename__ = "world_calendars"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, unique=True, index=True)
    config_json = Column(Text, default="{}")  # {era_name, current_day, months:[{name,days}]}
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CalendarEvent(Base):
    """A single pinned event on a world's calendar, optionally linked to a
    lore Entity (e.g. an NPC's birthday, a holiday tied to a location)."""
    __tablename__ = "calendar_events"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    day = Column(Integer, nullable=False)
    title = Column(String(256), nullable=False)
    notes = Column(Text, default="")
    entity_id = Column(Integer, ForeignKey("entities.id"), nullable=True, index=True)
    color = Column(String(16), default="#4488ff")
    created_at = Column(DateTime, default=datetime.utcnow)

    entity = relationship("Entity")


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
    # Battle-map grid overlay — "none" | "hex" | "square". config: {cell_size,
    # offset_x, offset_y, orientation:"pointy"|"flat" (hex only)}. Purely a
    # rendering/snapping aid — tokens (elements_json entries with type:"token")
    # still store raw x/y, not grid coordinates.
    grid_type = Column(String(16), default="none")
    grid_config_json = Column(Text, default="{}")
    combat_session_id = Column(Integer, ForeignKey("combat_sessions.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppSettings(Base):
    """Single-row, instance-wide settings — not per-world, since this
    controls how the upload pipeline itself behaves. Looked up/created
    lazily via database.get_app_settings() rather than seeded, so it
    doesn't need any special-casing in _seed()."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, default=1)
    static_format = Column(String(16), default="avif")    # "none" | "avif" | "webp"
    animated_format = Column(String(16), default="avif")  # "none" | "avif" | "webp"
    # Blank = fall back to the OLLAMA_MODEL/OLLAMA_URL/SWARMUI_EXTERNAL_URL env vars
    # (see app.ai.effective_ollama_*() and main.py's imagestudio()).
    ollama_model = Column(String(256), default="")
    ollama_url = Column(String(512), default="")
    swarmui_external_url = Column(String(512), default="")
    # Same idea as swarmui_external_url, for the embedded Android app viewer
    # at /androidapp — see app.main's ANDROID_EMULATOR_URL and docs/DEPLOYMENT.md.
    android_emulator_url = Column(String(512), default="")
    # Same idea again, for the embedded Content Editor viewer at /editor —
    # see app.main's EDITOR_EXTERNAL_URL and docs/DEPLOYMENT.md.
    editor_external_url = Column(String(512), default="")
    # Same idea again, for the optional whisper.cpp server that transcribes
    # an AI chat audio attachment into text — see app.ai's WHISPER_URL and
    # docs/DEPLOYMENT.md's "Optional: audio transcription (Whisper)".
    whisper_url = Column(String(512), default="")
    # Hover-a-link-for-N-seconds entity preview popup (base.html's global
    # mouseover handler + GET /api/entity/{id}/preview) — instance-wide like
    # everything else on this row, editable from Settings > Options.
    hover_preview_enabled = Column(Boolean, default=True)
    hover_preview_delay_ms = Column(Integer, default=5000)
    # Grace period after the pointer leaves the link before the popup
    # actually closes, so there's time to move the mouse onto the popup
    # itself. Popup card size is otherwise fixed by CSS; these override it.
    hover_preview_hide_delay_ms = Column(Integer, default=400)
    hover_preview_width_px = Column(Integer, default=340)
    hover_preview_max_height_px = Column(Integer, default=420)
    # Optional GM-facing lore extras (see app/routers/lore_extras.py) — off by
    # default so they don't clutter the nav for tables that don't want them,
    # toggle on from Settings > System.
    dreamlands_enabled = Column(Boolean, default=False)
    king_in_yellow_enabled = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
