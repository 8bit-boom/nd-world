from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Table, ForeignKey, Float, Index
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
    # A GM-importable visual palette/font preset — JSON object with any of
    # bg/bg2/bg3/border/neon2/neon3/yellow/text/text_dim (hex colors),
    # font/font_heading (CSS font-family strings), google_fonts_url, and
    # name — see _sanitize_theme() in app/main.py for the whitelist that
    # validates a file before it's stored here, and base.html for how each
    # field maps onto static/style.css's existing CSS custom properties
    # (the same ones the accent/dark-dim-light theme system above already
    # uses everywhere, so setting this reskins the whole app for this
    # world with no per-template changes). NULL/"" = no override; every
    # field is independently optional, so a theme can override just the
    # palette, just the fonts, or both. `accent` alone (this world's
    # existing single-color field) intentionally isn't duplicated in here
    # — a theme file's own "accent" key, if present, is applied straight
    # to World.accent on import instead of being stored twice.
    #
    # Also carries a few hero-banner-only overrides, independent of the
    # regular font/color fields above: font_display (a fancier font for
    # just the big hero title, distinct from font_heading which applies to
    # every other heading app-wide), hero_letter_spacing (a CSS
    # letter-spacing value — deliberately exposed since a decorative
    # display font's glyphs touching/overlapping, like linked "O"s, usually
    # needs tight or negative spacing to happen at all), hero_glow_color
    # (defaults to accent when unset), and hero_graphic ("moon" | "circle"
    # | "none", default "none" — a small radial-gradient sphere rendered
    # above the hero title; see static/style.css's .hero-graphic rules,
    # which derive their look from --hero-glow via color-mix() so they
    # adapt to any accent color rather than being hardcoded to one theme).
    # Where the hero itself appears at all is NOT part of this JSON — see
    # World.hero_style below, a plain per-world setting a GM toggles
    # directly rather than re-uploading a theme file for.
    theme_json = Column(Text, nullable=True)
    # Where the hero banner (big title + optional graphic — see index.html/
    # _hero.html) shows up: "off" (nowhere, just the plain topbar — the
    # pre-hero-toggle look), "home" (only on the world's home page — the
    # default, and the ONLY behavior that existed before this column, so
    # this default preserves every existing world's current appearance
    # unchanged), or "everywhere" (base.html renders it above the topbar on
    # every page; index.html then skips its own copy to avoid a double
    # hero on the home page specifically — see both templates).
    hero_style = Column(String(16), default="home")
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
    # Campaign vocabulary (NPC names, places, invented terms) fed to Whisper
    # as an initial-prompt hint on every session-recording transcription, so
    # e.g. "Elyndra" doesn't come back as "Elandra" or "a lender". Per-world,
    # not instance-wide (unlike ai_models.json's GM-personal-toolkit
    # settings) since it's campaign content. NULL/"" = no hint sent.
    whisper_glossary = Column(Text, nullable=True)
    # ISO-639-1 code (e.g. "ru", "es") a GM can pin so Whisper decodes every
    # session-recording transcription (background job, one-shot upload, and
    # live recording) as that language instead of auto-detecting per clip —
    # faster and more accurate for a table that's consistently non-English.
    # NULL/"" = auto-detect (transcribe_audio's own default when this isn't
    # set is "auto", never Whisper's server-side "en" fallback — see its
    # docstring in app/ai.py). Same per-world scope as whisper_glossary;
    # one-off chat attachments don't use this, matching that field's own
    # established scope.
    whisper_language = Column(String(16), nullable=True)
    # Run a real speech-enhancement model (DeepFilterNet) over each
    # session-recording chunk before it reaches Whisper — meaningfully
    # better than a browser's own noise suppression against sustained
    # background audio (music, hum) since it's a proper ML model, not just
    # echo/noise-gate heuristics. Off by default: torch/deepfilternet are
    # NOT in the base image (torch alone is hundreds of MB) — a deployment
    # has to opt in at build time too (see requirements-denoise.txt, the
    # Dockerfile's INSTALL_DENOISE build arg, and app.ai.
    # speech_enhancement_available()). POST /api/ai/whisper/denoise refuses
    # to set this True when the current container doesn't actually have it
    # installed, so this being True is a reliable signal the feature is
    # both wanted AND available — but see app.ai's own transcribe_audio
    # docstring for what happens if a deployment is later rebuilt WITHOUT
    # it while this is still True on an existing World (graceful skip, not
    # a hard failure).
    whisper_denoise = Column(Boolean, default=False)
    # Free-text steering for the recap-writing step specifically (not
    # transcription — Whisper itself has no notion of "instructions", only
    # the glossary hint above) — e.g. "Write summaries in Spanish" or "Use a
    # dry, sarcastic tone." Applied to every session-recording recap
    # (background job, one-shot upload, and re-summarize), same per-world
    # scope as whisper_glossary. NULL/"" = no extra instructions.
    recap_instructions = Column(Text, nullable=True)
    # Video Library space-saving: when enabled, every future upload to
    # /video is re-encoded to AV1 (see app/routers/video.py's
    # _convert_video) before being stored — off by default, since AV1
    # encoding is CPU-heavy and not every deployment even has ffmpeg.
    # Gracefully keeps the ORIGINAL file un-converted if the encode fails
    # for any reason (no ffmpeg, no AV1 encoder in this ffmpeg build, a
    # crash) — same graceful-degradation contract as poster generation;
    # never blocks the upload itself.
    video_convert_enabled = Column(Boolean, default=False)
    # Downscale cap in pixels (video height) applied during conversion —
    # NULL/0 means re-encode at the source resolution, no resizing.
    video_convert_max_height = Column(Integer, nullable=True)
    # Target average video bitrate in kbps for the AV1 encode. NULL uses
    # _DEFAULT_VIDEO_BITRATE_KBPS (app/routers/video.py) rather than
    # ffmpeg's own AV1 encoder default, which is surprisingly high.
    video_convert_bitrate_kbps = Column(Integer, nullable=True)
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
    # "player" (default — join a world, see what players see) or "assistant"
    # (a trusted table helper: player visibility — existing visibility filters
    # stay keyed on is_gm — but may create/edit world CONTENT; the route-level
    # half of that promise lives in _is_assistant_safe in app/main.py).
    # server_default matters alongside the Python default: it's what makes a
    # fresh create_all() schema carry DEFAULT 'player' in the DDL itself,
    # matching the ALTER TABLE _migrate() issues for existing installs (so a
    # row written by anything that skips the ORM still lands as a player).
    role = Column(String(20), default="player", nullable=False, server_default="player")

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

    # Almost every entity query filters on both columns at once (/kind/{kind},
    # search, RAG retrieval, the home page's per-kind counts) — world_id and
    # kind already each have their own single-column index above, which
    # SQLite can combine via index intersection, but a composite index lets
    # a (world_id, kind) lookup resolve as one index scan instead of two.
    __table_args__ = (Index("ix_entities_world_id_kind", "world_id", "kind"),)

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


class DiceRoll(Base):
    """One shared dice roll — the table's roll log. World-scoped so players
    only ever see rolls made in worlds they belong to; every member of the
    world can roll and read (players included — see _is_player_safe), with
    the roller's display name denormalized onto the row so the log stays
    readable even if the account is later deleted."""
    __tablename__ = "dice_rolls"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user_name = Column(String(120), nullable=False, default="")
    notation = Column(String(120), nullable=False)
    # JSON array of per-term results, e.g. [{"term":"2d6","rolls":[3,5],"sum":8},{"term":"+1","sum":1}]
    breakdown = Column(Text, nullable=False, default="[]")
    total = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # The roll-log page reads "newest 50 in this world" on every visit.
    __table_args__ = (Index("ix_dice_rolls_world_created", "world_id", "created_at"),)


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


class VideoAlbum(Base):
    """A GM-created folder for organizing VideoClips into albums and nested
    sub-albums — same self-referential parent_id tree as AudioAlbum. NULL
    parent_id means top-level. See app/routers/video.py for breadcrumb/
    cascade-delete handling."""
    __tablename__ = "video_albums"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("video_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class VideoClip(Base):
    """A GM-uploaded video file (a recorded cutscene, a handout clip, an
    NPC video message) for the /video library — see app/routers/video.py.
    Same ownership/visibility shape as AudioClip: each row owns exactly one
    file, deleting the row deletes the file, visible_to_players defaults
    True (a GM only has to act to hide a clip). poster_url is a best-effort
    ffmpeg-generated thumbnail frame — nullable, since ffmpeg is optional
    (see app.ai's identical graceful-degradation pattern for Whisper audio
    splitting) and a player's browser still shows a native video-element
    frame if it's missing. album_id is NULL for a top-level (unfiled) clip."""
    __tablename__ = "video_clips"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    description = Column(String(512), default="")
    file_url = Column(String(512), nullable=False)  # "/uploads/video/<file>"
    poster_url = Column(String(512), nullable=True)  # "/uploads/video/<file>.jpg", best-effort
    visible_to_players = Column(Boolean, default=True)
    album_id = Column(Integer, ForeignKey("video_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PageAlbum(Base):
    """A GM-created folder for organizing PageDocs into albums and nested
    sub-albums — same self-referential parent_id tree as AudioAlbum/
    VideoAlbum. NULL parent_id means top-level. See app/routers/pages.py."""
    __tablename__ = "page_albums"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("page_albums.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PageDoc(Base):
    """A GM-uploaded standalone .html document (a styled calendar, a
    handout, a reference page with its own embedded CSS/fonts) for the
    /pages library — see app/routers/pages.py. Same ownership/visibility
    shape as AudioClip/VideoClip: each row owns exactly one file, deleting
    the row deletes the file, visible_to_players defaults True. Rendered
    via a sandboxed <iframe> — see main.py's serve_upload for the
    CSP/X-Frame-Options this relies on, since unlike audio/video an HTML
    document can carry its own <script>. album_id is NULL for a top-level
    (unfiled) page."""
    __tablename__ = "page_docs"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    description = Column(String(512), default="")
    file_url = Column(String(512), nullable=False)  # "/uploads/pages/<file>"
    visible_to_players = Column(Boolean, default=True)
    album_id = Column(Integer, ForeignKey("page_albums.id"), nullable=True, index=True)
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
    browser) via the recent-jobs list. Survives the server process itself
    restarting mid-job: audio_path/checkpoint_json let a routine restart
    resume from the last completed chunk instead of losing the work — see
    app/job_shutdown.py for the shutdown-side mechanism and
    app/audio_jobs.py's resume_interrupted_jobs for the boot-side one."""
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
    # Which Ollama model summarized this job's transcript (purpose=
    # "session_recap" only — blank/None means "whatever the instance
    # default was at the time"). Set at job creation from the picker on the
    # session's audio upload panel, and updated whenever a GM re-summarizes
    # with a different model — see POST /api/audio-jobs/{id}/resummarize.
    model = Column(String(128), nullable=True)
    # One-off notes for this specific run's summarization — set at job
    # creation from the upload panel's optional instructions field, and
    # updatable whenever a GM re-summarizes with different notes (POST
    # /api/audio-jobs/{id}/resummarize, same idea as `model` above). Combined
    # with the world's own persistent recap_instructions (World.
    # recap_instructions, always applied) rather than replacing it — see
    # _combined_recap_instructions in app/audio_jobs.py. purpose=
    # "session_recap" only; ignored for "attachment", which never summarizes.
    extra_instructions = Column(Text, nullable=True)
    # Absolute path of the working audio file, persisted so a resume after a
    # server restart can find it again — _run_job used to only ever see this
    # as a function argument, which a fresh process obviously doesn't have.
    audio_path = Column(String(1024), default="")
    # Whether _run_job owns this file and should delete it when done (True
    # for purpose="session_recap" — working storage, not the artifact) or it
    # IS the artifact the caller keeps (False for purpose="attachment").
    delete_after = Column(Boolean, default=True)
    # Set by app.ai.transcribe_audio's/summarize_transcript's on_checkpoint
    # callback after each chunk — see their docstrings for the exact shape.
    # Cleared once the job reaches a terminal status or moves to the next
    # phase (transcribe -> summarize). Lets a resume skip chunks already
    # done instead of redoing the whole job from scratch.
    checkpoint_json = Column(Text, default="")
    # How many times this job has auto-resumed itself after being
    # interrupted by a server restart — capped at job_shutdown.
    # MAX_AUTO_RESUMES to avoid an infinite crash-loop-and-retry if the
    # interruption is actually caused by something that crashes the process
    # itself (e.g. a pathological input), not a routine deploy.
    resumed_count = Column(Integer, default=0)
    # Real chunk progress during status="summarizing" OR "transcribing" —
    # only set when the transcript/audio is long enough to need chunking
    # (see app.ai.summarize_transcript's and transcribe_audio's on_progress
    # callbacks respectively); NULL the rest of the time, including once
    # the job finishes.
    chunk_current = Column(Integer, nullable=True)
    chunk_total = Column(Integer, nullable=True)
    # Distinct from created_at (which never changes once the row exists,
    # and is used everywhere as "when was this job started"): run_started_at
    # marks the start of the CURRENT run specifically, reset every time
    # start_resummarize_job kicks off a new attempt — so a job resummarized
    # days after its first run doesn't report a multi-day "duration" that's
    # mostly idle time between runs. finished_at is set once that run reaches
    # a terminal status (done/error/cancelled). Both NULL until the job's
    # first run actually starts (they're set at the top of _run_job, not at
    # create_job's row-insert, so a still-"pending" job has neither yet).
    run_started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # Whether the summarization step (purpose="session_recap" or "condense")
    # runs with the model's "thinking"/reasoning mode on — a GM-facing
    # "Thinking" checkbox on the upload panel / Retry-summary row, checked
    # by default. Persisted (not just a call-time argument) so a resume
    # after a server restart or an explicit re-summarize uses the same
    # setting the GM chose, same as `model`/`extra_instructions` already
    # are. NULL (the pre-migration default) is treated as True — see
    # app.audio_jobs.create_job's docstring.
    think = Column(Boolean, nullable=True)
    # Set when this job's summarize/condense phase failed on its first
    # attempt because thinking burned the whole output budget (see
    # app.ai.is_thinking_starved_sentinel), and _run_job auto-retried once
    # with think=False — which also flips `think` above to False, so the
    # Retry-summary UI's Thinking checkbox reflects what actually produced
    # the recap. False (not just falsy/unset) for every job that never hit
    # this path, so a template can render a note without an `is not True`
    # check. See app.audio_jobs._run_job's own docstring for the retry.
    think_fallback = Column(Boolean, default=False)
    # Set when this job's model outright doesn't support thinking mode —
    # Ollama rejected think=true with "does not support thinking" (see
    # app.ai.generate_chat/stream_chat's own internal think=False retry,
    # and app.ai.model_rejected_thinking) rather than the budget-starvation
    # case think_fallback above covers. Also flips `think` to False, same
    # reasoning as think_fallback. Distinct from think_fallback because the
    # two need different GM-facing guidance: this one means "this model
    # can't do it, full stop" (untick its Settings > System override), not
    # "give it a bigger budget." False for every job that never hit this.
    think_rejected = Column(Boolean, default=False)
    # Set alongside think_rejected when the rejection was Ollama's missing
    # capability tag on an hf.co-imported GGUF (ollama#16936) that nd-world
    # worked around by sending the <|think|> prompt token instead (see
    # app.ai.model_thinks_via_prompt_token). The recap in that case WAS
    # produced with reasoning, so `think` deliberately stays True (unlike a
    # bare think_rejected, which flips it to False) and the Background Jobs
    # note differs: informational "reasoning ran via the token", not
    # "written with Thinking off / untick the override". False for every
    # job that never hit this. Existing installs get the column via
    # database._migrate's _heal_table_from_model pass, same as every other
    # late-added audio_jobs column.
    think_token_fallback = Column(Boolean, default=False)
    # Set when the auto-retry ladder above ever had to climb into an
    # EXPANDED budget rung (see app.ai.expanded_thinking_options and
    # app.audio_jobs._run_job's attempt_plans) — i.e. even the normal
    # thinking headroom wasn't enough. Distinct from think_fallback: this
    # can be True while think_fallback stays False (the expanded rung
    # succeeded without ever needing to flip think off). False for every
    # job that never climbed the ladder at all.
    expanded_thinking = Column(Boolean, default=False)
    # purpose="condense" only: whether the job's num_ctx was sized to fit
    # the whole input text for this one call (see app.ai.
    # context_sized_options) rather than using the GM's configured/default
    # context — set once at job creation from the "Condense (fit context)"
    # button, replayed identically on a resume.
    fit_context = Column(Boolean, default=False)
    # purpose="condense" only: soft length targets (in tokens) for the
    # condensed OUTPUT — see app.ai.condense_recap's own docstring for how
    # each is actually enforced (max_tokens sets Ollama's real num_predict
    # cap; min_tokens is prompt guidance only, Ollama has no native
    # minimum-output-length option). NULL means "no target set" for
    # either. `extra_instructions` (already a column above, shared with
    # session_recap) doubles as Condense's own one-off GM steering — no
    # separate column needed for that.
    min_tokens = Column(Integer, nullable=True)
    max_tokens = Column(Integer, nullable=True)
    # purpose="condense" only: persisted condense strictness
    # ("guideline"|"firm"|"strict") steering how hard min/max/
    # extra-instructions are enforced in the prompt and whether a strict
    # out-of-band auto-retry runs (see app.audio_jobs._run_job) —
    # "guideline" is today's best-effort behavior. Persisted (not just a
    # call-time argument) so a resume/redo reuses the same strictness the
    # GM originally set, same as min_tokens/max_tokens above. Existing
    # installs get the column via database._migrate's _heal_table_from_model
    # pass, same as every other late-added audio_jobs column.
    condense_strictness = Column(String, default="guideline")
    # purpose="condense"/"session_recap" only: opt this run into RAG —
    # retrieving relevant World entities/notes and prepending them to the
    # summarize/condense system prompt for accuracy (spelling names,
    # remembering established places) — see app.audio_jobs._build_rag_context
    # and app.ai._with_world_context. Off by default (a GM opts in per run,
    # same pattern as fit_context above). rag_entity_limit/rag_notes_limit
    # cap how many entities/notes get pulled in; NULL means "use
    # app.audio_jobs._DEFAULT_RAG_ENTITY_LIMIT/_DEFAULT_RAG_NOTES_LIMIT" —
    # distinct from 0, which means "retrieve none of that category."
    use_rag = Column(Boolean, default=False)
    rag_entity_limit = Column(Integer, nullable=True)
    rag_notes_limit = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ImageJob(Base):
    """A durable background job for image generation — see app/image_jobs.py.
    Same rationale as AudioJob: an opt-in "process in background" alternative
    to Image Studio's default direct/blocking generate button, for a
    generation slow enough (large batch, hires-fix, a big upscale) that
    waiting on one HTTP request isn't practical. The actual work runs as a
    background asyncio task in the server process, independent of any one
    HTTP connection, so it survives the browser tab that started it being
    closed. A server restart mid-job restarts the generation from scratch
    on the next boot (up to job_shutdown.MAX_AUTO_RESUMES times, tracked by
    resumed_count below) rather than truly resuming it — image generation is
    one opaque remote call to SwarmUI/ComfyUI with no intermediate state to
    checkpoint, unlike AudioJob's per-chunk transcription/summarization."""
    __tablename__ = "image_jobs"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    prompt = Column(Text, default="")
    # The full ImagegenBody params (app/routers/ai.py) as submitted, so
    # _run_job can call app.ai.imagegen_generate identically to the direct
    # route — one JSON blob rather than duplicating every field as a column.
    params_json = Column(Text, default="{}")
    # pending -> generating -> done, or -> error/cancelled at any point.
    status = Column(String(32), default="pending")
    error = Column(Text, default="")
    result_urls_json = Column(Text, default="[]")
    # How many times this job has auto-restarted itself after being
    # interrupted by a server restart — see ImageJob's own docstring and
    # job_shutdown.MAX_AUTO_RESUMES.
    resumed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatJob(Base):
    """A durable background job for a single non-streaming chat completion —
    see app/chat_jobs.py. Same rationale as AudioJob/ImageJob: an opt-in
    "process in background" alternative to the main AI Chat page's default
    live-streamed reply, for a generation slow enough (a big local model, a
    long context) that keeping the tab open and connected isn't practical.
    The actual work runs as a background asyncio task in the server process,
    independent of any one HTTP connection, so it survives the browser tab
    that started it being closed. A server restart mid-job restarts the
    completion from scratch on the next boot (up to job_shutdown.
    MAX_AUTO_RESUMES times, tracked by resumed_count below) rather than
    truly resuming it — this is one opaque non-streaming generate_chat call
    with no intermediate state to checkpoint, unlike AudioJob's per-chunk
    transcription/summarization."""
    __tablename__ = "chat_jobs"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # A short label for the jobs list — the last user message's text,
    # truncated. The full exchange (with any injected lore context/
    # attachments) is in messages_json.
    prompt = Column(Text, default="")
    messages_json = Column(Text, default="[]")
    system = Column(Text, default="")
    model = Column(String(128), nullable=True)
    options_json = Column(Text, default="{}")
    # pending -> generating -> done, or -> error/cancelled at any point.
    status = Column(String(32), default="pending")
    error = Column(Text, default="")
    result = Column(Text, default="")
    # How many times this job has auto-restarted itself after being
    # interrupted by a server restart — see ChatJob's own docstring and
    # job_shutdown.MAX_AUTO_RESUMES.
    resumed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatSession(Base):
    """A saved AI Chat conversation (app/templates/ai_chat.html's History
    sidebar) — one row per conversation, upserted on every completed
    assistant turn (see the client's autoSave(), which POSTs the full
    messages array with session_id=None the first time, then the returned
    id on every save after). GM-only for now, same as the /ai page itself —
    see app.routers.ai's chat-session routes. `surface` mirrors ChatBody's
    own field (currently only "chat" is used; reserved for a future AI
    surface — e.g. per-entity "talk to this NPC" — reusing the same table)."""
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    surface = Column(String(32), default="chat")
    title = Column(String(256), default="")
    messages_json = Column(Text, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromptPreset(Base):
    """A GM-editable, per-world named prompt — feeds AI Chat's Quick Prompts
    sidebar (scope="chat", clicking one inserts `text` into the input for
    the GM to edit/send, it does not send immediately) and Image Studio's
    Prompt Presets (scope="image", `text`/`negative` loaded straight into
    the generation form) — replacing the previous hardcoded generic-fantasy
    quick prompts and the image presets' localStorage-only storage (which
    vanished on a different browser, unlike everything else in this app)."""
    __tablename__ = "prompt_presets"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    scope = Column(String(16), nullable=False)  # "chat" | "image"
    label = Column(String(128), nullable=False)
    icon = Column(String(8), default="")  # chat only — an emoji on the quick-prompt button
    text = Column(Text, default="")  # chat: inserted prompt text; image: the positive prompt
    negative = Column(Text, default="")  # image only
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    lore Entity (covers characters/NPCs, locations, creatures, items, and
    other events — see KINDS in constants.py; e.g. an NPC's birthday, a
    holiday tied to a location), a GameSession (marking the in-world day a
    real session's events took place on — session_date on GameSession
    itself is the real-world date it was played, a separate axis from
    this), a PlayerCharacter, or a Party. All four link columns are
    independent and optional — an event can carry any combination (or
    none), same as the original entity_id."""
    __tablename__ = "calendar_events"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    day = Column(Integer, nullable=False)
    title = Column(String(256), nullable=False)
    notes = Column(Text, default="")
    entity_id = Column(Integer, ForeignKey("entities.id"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=True, index=True)
    character_id = Column(Integer, ForeignKey("player_characters.id"), nullable=True, index=True)
    party_id = Column(Integer, ForeignKey("parties.id"), nullable=True, index=True)
    color = Column(String(16), default="#4488ff")
    created_at = Column(DateTime, default=datetime.utcnow)

    entity = relationship("Entity")
    session = relationship("GameSession")
    character = relationship("PlayerCharacter")
    party = relationship("Party")


class CalendarDayIcon(Base):
    """A small uploaded image pinned to one calendar day — used like an
    emoji sticker (a holiday marker, a weather icon, an in-world symbol)
    but backed by a real uploaded image rather than a Unicode character.
    Several can be pinned to the same day, unlike a portrait/single image
    field elsewhere in this app."""
    __tablename__ = "calendar_day_icons"

    id = Column(Integer, primary_key=True, index=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), nullable=False, index=True)
    day = Column(Integer, nullable=False, index=True)
    image_url = Column(String(512), nullable=False)
    label = Column(String(120), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


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
    # Per-request Ollama generation options (see app.ai.effective_ollama_options()
    # and set_ollama_generation_overrides()) — all nullable/blank so an unset
    # field just omits that key from the options= dict passed to the Ollama
    # client, letting Ollama/the model's own Modelfile default apply. These
    # tune inference quality/speed/VRAM use per chat call. Two buckets live on
    # this app now (see app/ollama_tuning.py's module docstring): these
    # per-request fields apply live on the very next call, no restart; the
    # genuinely server-process-level knobs (OLLAMA_FLASH_ATTENTION,
    # OLLAMA_KV_CACHE_TYPE, etc.) are ollama_server_env_json below.
    ollama_temperature = Column(Float, nullable=True)
    ollama_top_p = Column(Float, nullable=True)
    ollama_top_k = Column(Integer, nullable=True)
    ollama_repeat_penalty = Column(Float, nullable=True)
    ollama_num_predict = Column(Integer, nullable=True)
    ollama_num_ctx = Column(Integer, nullable=True)
    ollama_seed = Column(Integer, nullable=True)
    # Deprecated: current Ollama (verified against api/types.go) has no
    # mirostat/mirostat_tau/mirostat_eta fields on its Options struct at all
    # — an unknown option key just logs "invalid option provided" server-side
    # and is otherwise ignored. Kept (not sent — see effective_ollama_options())
    # so an older Ollama server still honors a GM's saved value, and so
    # upgrading this app never silently discards one.
    ollama_mirostat = Column(Integer, nullable=True)
    ollama_mirostat_tau = Column(Float, nullable=True)
    ollama_mirostat_eta = Column(Float, nullable=True)
    # Passed as the separate keep_alive= kwarg (not nested in options=) on
    # .chat()/.generate() — how long Ollama keeps the model loaded in VRAM
    # after this request, e.g. "5m", "1h", "-1" (forever), "0" (unload now).
    ollama_keep_alive = Column(String(32), default="")
    ollama_num_gpu = Column(Integer, nullable=True)
    # Bucket-C additions — every one of these is a real field of Ollama's
    # api.Options/api.Runner today (verified against api/types.go), so like
    # the fields above they apply live on the next request with no restart.
    # The Runner ones (num_batch/num_thread/main_gpu/use_mmap, same as
    # num_ctx/num_gpu above) are applied at model-load time, so changing one
    # costs a model reload on the next call — still never a container restart.
    ollama_min_p = Column(Float, nullable=True)
    ollama_typical_p = Column(Float, nullable=True)
    ollama_repeat_last_n = Column(Integer, nullable=True)
    ollama_presence_penalty = Column(Float, nullable=True)
    ollama_frequency_penalty = Column(Float, nullable=True)
    ollama_num_keep = Column(Integer, nullable=True)
    ollama_num_batch = Column(Integer, nullable=True)
    ollama_num_thread = Column(Integer, nullable=True)
    ollama_main_gpu = Column(Integer, nullable=True)
    # Tri-state as a string ("", "1", "0"), not Boolean, to keep this app's
    # established "blank means unset, let Ollama decide" convention for
    # optional Ollama options — use_mmap is a real bool in Ollama itself,
    # where False is meaningfully different from "not sent".
    ollama_use_mmap = Column(String(8), default="")
    # Bucket A — the ollama SERVER process's own environment, which Ollama
    # only reads at process start (no runtime API exists to change it). One
    # JSON bag keyed by env-var name (same pattern as World.theme_json/
    # home_sections_json) rather than one column per var, since it's an
    # opaque allowlisted key/value set — app/ollama_tuning.py's
    # SERVER_ENV_SPEC is the schema, so adding a new knob there never needs
    # a migration. Written out to a shared volume for the ollama service's
    # entrypoint to source at start; see app/ollama_tuning.py and
    # docs/DEPLOYMENT.md.
    ollama_server_env_json = Column(Text, default="{}")
    # GM-supplied total VRAM in MB, used when nd-world's own container can't
    # see the GPU (the normal case — only the ollama/swarmui services get GPU
    # passthrough in docker-compose.yml). Blank/None = rely on auto-detection.
    ollama_vram_override_mb = Column(Integer, nullable=True)
    # Per-model overrides for the core "Bucket C" fields above — a GM-editable
    # JSON dict {model_id: {field: value, ...}} keyed by exactly the same
    # option names effective_ollama_options() builds (see
    # _refresh_settings_overrides in main.py), plus "keep_alive". Scoped to
    # just the fields on Settings > System's main Generation tuning panel
    # (temperature/top_p/top_k/repeat_penalty/num_predict/num_ctx/seed/
    # num_gpu/keep_alive) — the "Advanced sampling & runtime" extras stay
    # global-only, the same "not every knob needs every axis" boundary this
    # app already draws elsewhere (e.g. OLLAMA_LLM_LIBRARY's exclusion in
    # ollama_tuning.py). Same JSON-bag-not-a-column-per-field reasoning as
    # ollama_server_env_json above — an unbounded, GM-chosen set of model
    # ids can't be columns. A model with no entry here just uses the plain
    # instance-wide fields above, unchanged.
    ollama_model_overrides_json = Column(Text, default="{}")
    # ── Upload size limits (Settings > System's "Upload limits" section) ──
    # Stored in MB — the unit every limit label in the UI already uses — as
    # nullable Integer columns. NULL (the state on any existing install the
    # migration adds the columns to, and any blank form field) means "use
    # the MAX_UPLOAD_BYTES / MAX_GALLERY_UPLOAD_BYTES / MAX_VIDEO_UPLOAD_BYTES
    # / MAX_AUDIO_UPLOAD_BYTES environment default", so a GM raising a limit
    # here never has to touch docker-compose, and an env-var deployment keeps
    # working unchanged. The effective bytes value is (value * 1 MB), computed
    # per request by effective_upload_bytes() (app/uploads.py) plus each
    # enforcement site's own _effective_*_bytes() helper — so a saved value
    # applies to new uploads immediately, no restart. One column per
    # enforcement site (not one global knob) because the limits exist for
    # different reasons at different magnitudes — a 20 MB portrait cap and a
    # 2 GB video cap shouldn't move together.
    # General copy_upload_bounded/read_upload_bounded default
    # (app/uploads.py MAX_UPLOAD_BYTES): portraits, maps, schematic
    # embeds, calendar icons, home background, bulk import — the
    # small-file cap that keeps unbounded uploads from filling /data.
    max_upload_mb = Column(Integer, nullable=True)
    # Gallery album image uploads (app/routers/gallery.py
    # _MAX_GALLERY_UPLOAD_BYTES, env MAX_GALLERY_UPLOAD_BYTES, 500 MB) —
    # big animated art legitimately exceeds the general cap.
    max_gallery_upload_mb = Column(Integer, nullable=True)
    # /video clips (app/routers/video.py _MAX_VIDEO_BYTES, env
    # MAX_VIDEO_UPLOAD_BYTES, 2048 MB) — shown on the video page's
    # "Up to N MB each" hint.
    max_video_mb = Column(Integer, nullable=True)
    # /audio clips (app/routers/audio.py _MAX_AUDIO_BYTES, env
    # MAX_AUDIO_UPLOAD_BYTES, 1024 MB) — shown on the audio page's
    # "Up to N MB each" hint.
    max_audio_mb = Column(Integer, nullable=True)
    # AI chat/Ask AI voice-memo attachments (app/routers/ai.py
    # _MAX_ATTACHMENT_AUDIO_BYTES, same env var as the audio library but
    # a separate enforcement site — image/document attachments keep their
    # own _MAX_ATTACHMENT_BYTES/MAX_AI_ATTACHMENT_BYTES env cap, which
    # deliberately isn't a column: a smaller voice-memo limit shouldn't
    # silently change what a dropped PDF may weigh).
    max_ai_attachment_mb = Column(Integer, nullable=True)
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
