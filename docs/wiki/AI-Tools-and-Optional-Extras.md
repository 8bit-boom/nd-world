# AI Tools & Optional Extras

*Applies to: GM only.*

All AI features run against a **local Ollama model** you configure — see the
README's [AI Setup](https://github.com/8bit-boom/nd-world/blob/main/README.md#ai-setup) for installing/enabling
Ollama and picking a model. Nothing here calls a hosted AI API. (The
player-facing 📜 Chronicler and AI recap tools live on their own pages — see
[Sessions, Facts & AI Recaps](Sessions-Facts-and-AI-Recaps.md).)

## 🤖 AI chat & world-building assistant

**🤖 AI** is the GM's general chat assistant, with keyword-search RAG context
pulled from your world's entities (not visibility-filtered — this is a
GM-only tool, unlike Chronicler). Also on this page:

- **Models tab** — download/manage Ollama models with live progress bars,
  hide/show built-ins, pull a custom model by ID.
- **Generation helpers** — quick "expand this NPC/location/quest hook"
  prompts you can save straight onto an entity.

## 🎨 Image Studio & AI image generation

If you've enabled the SwarmUI (or ComfyUI) backend, **AI → Image Gen** gives
you a full generation panel: checkpoint/LoRA/VAE selection, samplers,
CFG/seed, batch generation, img2img, upscaling, and a starred-image gallery
with one-click parameter reuse. **🎨 Image Studio** embeds the SwarmUI web UI
directly via iframe for anything the built-in panel doesn't cover.

## Optional lore extras

Two bundled GM-facing extras are **off by default** — they don't show up in
the nav at all until you turn them on, to keep the nav focused for tables
that don't use them. Enable from **⚙ Settings → System → Optional extras**:

### 🌙 Dreamlands

A write-up of the Dreamlands setting (linking to the generated 50-location
atlas board — see [Worldbuilding & Entities](Worldbuilding-and-Entities.md#investigation-boards)).
Bundled reference content, not derived from your world's own data.

### 🎭 King in Yellow

An AI-assisted play generator: pulls public-domain King in Yellow research
from a few sources, optionally references 1-2 of your previously saved
plays for style, and streams a fresh two-act play. Save generations you
like to a library, which then feeds future generations (and can even be
used to fine-tune a dedicated local Ollama model from your saved plays, if
you have a real local Ollama daemon running).

If you visit `/dreamlands` or `/king-in-yellow` directly while the toggle is
off, you'll see a small "this feature is disabled" page linking straight to
the Settings toggle, instead of a dead end.
