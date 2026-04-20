from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Table, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

entity_links = Table(
    "entity_links",
    Base.metadata,
    Column("source_id", Integer, ForeignKey("entities.id"), primary_key=True),
    Column("target_id", Integer, ForeignKey("entities.id"), primary_key=True),
)

class Entity(Base):
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(32), nullable=False, index=True)   # character, location, org, creature, event, note
    subtype = Column(String(64), nullable=True)             # e.g. megacorp, district, mutant
    name = Column(String(256), nullable=False)
    tags = Column(String(512), nullable=True)               # comma-separated
    image_url = Column(String(512), nullable=True)
    summary = Column(String(512), nullable=True)
    body = Column(Text, nullable=True)                      # markdown
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    related = relationship(
        "Entity",
        secondary=entity_links,
        primaryjoin=id == entity_links.c.source_id,
        secondaryjoin=id == entity_links.c.target_id,
        backref="referenced_by",
    )
