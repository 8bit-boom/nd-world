"""Per-world, GM-authored STANDING instructions for how the AI should
behave across every AI-answering surface (AI Chat — both GM and player,
the per-entity "Ask AI" panel, and Chronicler) — e.g. "always answer in a
noir detective's voice", "never reveal the killer's identity before Act 3
is played". See app.models.AiInstruction for the full contrast with
this world's Entities/Notes/Rules/hybrid-RAG vault: those are CONTENT the
AI answers FROM, filtered by relevance to each question; every enabled
AiInstruction row is unconditionally appended to the relevant system
prompt on EVERY question regardless of what was actually asked.

Its own leaf module (not app.retrieval, which is squarely about
relevance-filtered RAG content, and not app.main, which every router
would then need to avoid importing from) so all of app.main and every
router that builds one of the four system prompts above can import this
one place directly.
"""
from sqlalchemy.orm import Session

from .models import AiInstruction


def enabled_instructions_text(db: Session, world_id: int, for_players: bool = False) -> str:
    """Every enabled AiInstruction for this world a caller with this
    viewer's access should see, oldest first, each under its own "##
    title" heading — or "" when there are none (a world that's never used
    this feature, or has switched every uploaded document off, builds
    exactly the system prompt it always has). Callers append this to
    their own surface's existing system prompt/grounding clause, never
    replace it — see each of the four call sites (app.main.ai_chat_page,
    app.main.player_ai_chat's ai_chat_player.html template context,
    app.main.detail()'s entities/detail.html template context,
    app.routers.chronicler.build_chronicler_system_prompt).

    `for_players`, when True, restricts to rows with
    applies_to_players=True — see that column's own docstring on
    AiInstruction for why this is opt-in, not opt-out: an instruction like
    "never reveal the killer's identity before Act 3" would itself be the
    spoiler if its text reached a player. Every caller building a
    player-reachable prompt (player AI Chat, an entity panel a PLAYER is
    viewing, Chronicler answering a non-GM) MUST pass for_players=True; a
    GM viewing any of those same surfaces still gets every enabled
    instruction regardless of this flag."""
    q = db.query(AiInstruction).filter(AiInstruction.world_id == world_id, AiInstruction.enabled.is_(True))
    if for_players:
        q = q.filter(AiInstruction.applies_to_players.is_(True))
    rows = q.order_by(AiInstruction.created_at).all()
    parts = [f"## {r.title}\n\n{r.content.strip()}" for r in rows if (r.content or "").strip()]
    if not parts:
        return ""
    return (
        "Additional GM-authored instructions for how you should behave — "
        "follow these in addition to everything above, not instead of it:\n\n"
        + "\n\n".join(parts)
    )
