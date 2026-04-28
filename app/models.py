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

class World(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    description = Column(String(512), nullable=True)
    accent = Column(String(16), default="#00f0ff")
    created_at = Column(DateTime, default=datetime.utcnow)

    entities = relationship("Entity", back_populates="world", cascade="all, delete-orphan")

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
