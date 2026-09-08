"""Built-in per-model prompt templates for the Image Gen tab.

Different model families want wildly different prompting (Danbooru tags
vs natural language vs LLM-style system prefixes vs JSON captions) and
different sampler settings (a distilled 8-step Turbo model wants CFG 1
while a regular checkpoint wants 4–7) — and the failure mode of getting
it wrong is exactly the "prompt did nothing, output looks random" report
that motivated these. Each template bundles the family's prompt
scaffolding and recommended generation settings so picking a model family
is one click.

Sources, per family (September 2026):
- Sampler/settings: SwarmUI's own "Model Support" doc
  (docs/Model Support.md in the SwarmUI repo).
- Prompting: each model's OFFICIAL prompting guide where one exists —
  Anima's HF README #prompting section, Ideogram's docs/prompting.md
  (JSON caption schema), BFL's FLUX.2 prompting guide, the Z-Image
  prompting guide, and the Krea 2 community guide (style-LoRA triggers).
- Families without a usable guide say so in their `guide` text and carry
  conservative defaults.

Each template field:
  id/label        — stable id + display name.
  match/exclude   — lowercase substrings matched against the selected
                    backend model's filename for the "(suggested)"
                    marker (exclude wins over match).
  prefix/suffix   — comma-separated style tags merged around the GM's
                    own prompt text (deduped; a prefix may be prose —
                    Lumina's LLM system prefix).
  example_prompt  — fills an empty prompt box so the shape is obvious
                    (Ideogram's is a real JSON caption, because plain
                    text doesn't work there).
  negative        — replaces the negative box ("" clears it — Flux and
                    the distillates genuinely want NO negative).
  steps/cfg       — recommended generation settings (applied to sliders).
  sampler/scheduler — applied when the backend offers that option.
  note            — one line: the settings story.
  guide           — how to PROMPT this family (word order, tag style,
                    quirks). Shown under the picker after applying.

Custom (GM-made) templates are separate: PromptPreset rows with
scope="image_template" (see routers/ai.py's
/api/ai/imagegen/model-templates* routes) — same shape, merged into the
picker client-side. Video/audio model families are deliberately absent:
nd-world's SwarmUI integration generates images only.
"""

TEMPLATES = [
    {
        "id": "sdxl", "label": "SDXL (general, 2023-era)",
        "match": ["sdxl", "sd_xl", "juggernaut", "dreamshaper"],
        "exclude": ["turbo", "lcm", "lightning", "hyper"],
        "prefix": "",
        "suffix": "highly detailed, sharp focus",
        "example_prompt": "A weathered lighthouse on a storm-wrapped cliff, waves exploding against the rocks at dusk, moody cinematic lighting, highly detailed, sharp focus",
        "negative": "worst quality, low quality, watermark, text, signature, jpeg artifacts, deformed, blurry",
        "steps": 25, "cfg": 7.0, "sampler": "euler_a", "scheduler": "normal",
        "note": "CFG 5–8, steps 20+. Swarm adds enhanced inference settings automatically.",
        "guide": "Natural language or tags both work. Checkpoint finetunes each have their own favorite quality tags — check the model card. Negatives work.",
    },
    {
        "id": "sd35", "label": "Stable Diffusion 3.5 (Large/Medium)",
        "match": ["sd3.5", "sd_3.5", "sd35", "sd3", "stable-diffusion-3"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "An astronaut botanist tending glowing alien orchids inside a cracked space-station greenhouse, Earth visible through the broken glass, cinematic soft light",
        "negative": "worst quality, low quality, watermark, text, deformed, blurry",
        "steps": 28, "cfg": 4.5, "sampler": "euler", "scheduler": "normal",
        "note": "CFG ~4.5, steps 28–40. Sigma Shift 3. For upscaling enable Refiner Do Tiling (SD3 hates untiled upscales).",
        "guide": "Natural language, full sentences. More prompt-following than SDXL but still benefits from a clear subject-first sentence.",
    },
    {
        "id": "flux1", "label": "Flux.1 (dev/schnell — CFG 1, no negatives)",
        "match": ["flux.1", "flux1", "flux_dev", "flux_schnell", "flux-dev", "flux-schnell"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A medieval marketplace at golden hour rendered as a detailed ink-and-watercolor illustration, banners stirring in a light wind, a wooden sign reading 'Spice & Cloth' in carved letters, warm long shadows",
        "negative": "",
        "steps": 20, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "note": "CFG 1; negatives DON'T work. Dev: 20+ steps; Schnell: 4. Euler + Simple. Guidance comes from the separate 'Flux Guidance' param.",
        "guide": "Natural language, describe what you WANT (no negatives — they're ignored). Put text to render in quotes: a sign reading 'open'. Word order matters: subject first.",
    },
    {
        "id": "flux2", "label": "Flux.2 / Klein (Subject+Action+Style+Context)",
        "match": ["flux.2", "flux2", "flux-2", "flux_2", "klein"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "Fishermen mending nets on a mossy stone quay at dawn, sea fog softening the harbor cranes behind them, shot on Sony A7IV, clean sharp, high dynamic range, a hanging wooden sign reading 'Harbor Watch' in weathered carved letters",
        "negative": "",
        "steps": 28, "cfg": 1.0, "sampler": "euler", "scheduler": "flux2",
        "note": "CFG 1; officially NO negative prompts. Dev: 50 steps ideal (20 works); Turbo variant: 8 steps + 'Align Your Steps'. Klein distilled: 8/CFG1; Klein base: high steps/CFG ~7. Flux Guidance 3.5–4.",
        "guide": "Official structure: Subject + Action + Style + Context, most important words FIRST (FLUX.2 weights the front of the prompt). 30–80 words is the sweet spot. Describe what you want, never what you don't. Camera styles work ('2000s digicam', '80s film grain'). Text rendering is excellent — put exact words in quotes.",
    },
    {
        "id": "chroma", "label": "Chroma (Flux-derived — REGULAR CFG ~3.5)",
        "match": ["chroma"],
        "exclude": ["radiance", "zeta"],
        "prefix": "",
        "suffix": "highly detailed",
        "example_prompt": "A lighthouse keeper's daughter feeding a gull mid-flight on a windswept pier, overcast North-Sea light, muted palette, candid documentary feel, highly detailed, textured clouds over a lead-grey sea",
        "negative": "",
        "steps": 26, "cfg": 3.5, "sampler": "euler", "scheduler": "beta",
        "note": "Unlike Flux: STANDARD CFG ~3.5, official workflow 26 steps + 'Align Your Steps' (or Beta) scheduler. 1024 res (HD variants like 1152).",
        "guide": "Works better with LONGER prompts — 'prompt fluff' at the end genuinely cleans up the output (it's a beta model with an odd training set). No distilled CFG-1 behavior despite the Flux lineage.",
    },
    {
        "id": "chroma_radiance", "label": "Chroma Radiance (WIP pixel-space — needs its negative)",
        "match": ["radiance"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A quiet canal at dusk, narrow boats with lit windows, reflections rippling in dark water, muted colors, painterly detail",
        "negative": "This low quality greyscale unfinished sketch is inaccurate and flawed. The image is very blurred and lacks detail with excessive chromatic aberrations and artifacts. The image is overly saturated with excessive bloom. It has a toony aesthetic with bold outlines and flat colors.",
        "steps": 30, "cfg": 3.5, "sampler": "euler", "scheduler": "beta",
        "note": "Work-in-progress pixel-space model — SwarmUI's own doc rates quality 'Bad (WIP)'. The long negative above is the OFFICIAL one and is essential here.",
        "guide": "Long, detailed prompts; the official negative prompt (applied) is required for usable output. Expect artifacts; prefer regular Chroma unless you specifically want this variant.",
    },
    {
        "id": "zimage", "label": "Z-Image (camera-first prose, bilingual)",
        "match": ["z-image", "z_image", "zimage"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A medium shot of an adult cartographer hunched over candlelit maps, wearing a patched traveling coat, in a cluttered workshop at night, warm candlelight with deep shadows, focused and tired expression, realistic photography, 50mm lens, shallow depth of field, plain background, no logos, no watermark",
        "negative": "",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "note": "Turbo: CFG 1, 8 steps (4 min; hard prompts up to 20). Base: CFG 4–7, 20+ steps. Euler Ancestral + Beta can add photoreal detail. Sigma Shift 3 (6 for stronger coherence).",
        "guide": "No CFG at inference and negatives are IGNORED entirely — control everything positively. Scaffold: [shot type] + [subject & appearance] + [clothing] + [environment] + [lighting] + [mood] + [style/medium] + [technical notes] + [constraints like 'plain background, no logos']. Strong instruction following; English or Chinese both native. If you say it vaguely, it improvises.",
    },
    {
        "id": "anima", "label": "Anima (2B anime — official prefix/negative)",
        "match": ["anima"],
        "exclude": ["animagine"],
        "prefix": "masterpiece, best quality, score_7, safe,",
        "suffix": "",
        "example_prompt": "masterpiece, best quality, score_7, safe, year 2025, newest, highres, 1girl, traveler, moonlit rooftop, long coat, lantern, wind, detailed background, looking at viewer",
        "negative": "worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration",
        "steps": 26, "cfg": 4.0, "sampler": "er_sde", "scheduler": "simple",
        "note": "Regular CFG ~4 (NOT distilled). ER-SDE-Solver default; Euler Ancestral / DPM++ 2M SDE also good. Stay ~1024 res (higher corrupts); upscales need tiling.",
        "guide": "Official prompting: lowercase tags with SPACES (not underscores); tag order [quality/meta/year/safety] → [1girl/1boy] → [character] → [series] → [@artist] → [general]. Artists MUST have @ (e.g. '@nnn yryr') or barely work; prefer Gelbooru spellings. Weighting needs stronger values than SDXL: '(chibi:2)'. Natural language also works (2+ sentences). The Aesthetic finetune: drop the score_ tags.",
    },
    {
        "id": "pony", "label": "Pony / Illustrious (score tags + Danbooru)",
        "match": ["pony", "illustrious", "noobai"],
        "prefix": "score_9, score_8_up, score_7_up,",
        "suffix": "",
        "example_prompt": "score_9, score_8_up, score_7_up, source_dnd, 1girl, rogue, hooded cloak, neon-lit alley, cyberpunk city, looking at viewer, detailed background",
        "negative": "score_6, score_5, score_4, worst quality, low quality, watermark, blurry, deformed, bad anatomy",
        "steps": 28, "cfg": 7.0, "sampler": "euler_a", "scheduler": "normal",
        "note": "CFG ~7, steps 25–30. SDXL-family, so negatives work.",
        "guide": "score_9/8/7 tags are REQUIRED — they carry the aesthetic. Danbooru tags for content, underscores as-is. Fandom styles via source_* tags (e.g. source_dnd); rating tags (rating_safe/general/questionable/explicit) control content tier.",
    },
    {
        "id": "qwen", "label": "Qwen Image (natural language, best-in-class text)",
        "match": ["qwen"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A chalkboard tavern menu in crisp hand-lettered English listing 'Moonshine, Mead, Stormcloud Tea', hanging on a timber wall beside a lantern, photorealistic, warm evening light, shallow depth of field",
        "negative": "",
        "steps": 30, "cfg": 4.0, "sampler": "euler", "scheduler": "normal",
        "note": "Slow-but-smart: CFG 4 + steps 30–50 for quality (CFG 1 = fast mode). Distilled/Lightning: CFG 1, 8–15 steps. 1328x1328 native; very RAM-hungry.",
        "guide": "Natural language with LONG, LLM-style descriptions; both plain prose and booru tags work. Text rendering is the headline feature — put exact strings in quotes ('sign reads \"Closed\"'). Chinese works natively.",
    },
    {
        "id": "hunyuan", "label": "Hunyuan Image 2.1 (2K-native)",
        "match": ["hunyuanimage", "hunyuan_image", "hunyuan-img", "hunyuanimage2"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A rain-slicked cyberpunk street market at midnight reflected in puddles, holographic signage in two languages, a lone figure under a translucent umbrella, cinematic wide shot",
        "negative": "",
        "steps": 20, "cfg": 3.5, "sampler": "euler", "scheduler": "normal",
        "note": "CFG ~3.5, ~20 steps, targets 2048x2048 (its 32x VAE makes 2048 the latent equivalent of most models' 512!). Distilled variant: CFG 1. Sigma Shift 5.",
        "guide": "Natural language. Without its refiner model fine detail is soft — SwarmUI's Refine group with the official Hunyuan refiner (Refiner CFG 1, 4 steps, Control 1) is the intended pipeline.",
    },
    {
        "id": "lumina", "label": "Lumina 2.0 (LLM system-prefix REQUIRED)",
        "match": ["lumina"],
        "prefix": "You are an assistant designed to generate high-quality images with the highest degree of image-text alignment based on textual prompts. <Prompt Start>",
        "suffix": "",
        "example_prompt": "You are an assistant designed to generate high-quality images with the highest degree of image-text alignment based on textual prompts. <Prompt Start> a floating archipelago of small islands connected by rope bridges at dawn, waterfalls falling into a sea of clouds, soft painterly light",
        "negative": "",
        "steps": 32, "cfg": 4.0, "sampler": "euler", "scheduler": "normal",
        "note": "Its input is an LLM (Gemma 2) — the system-style prefix (applied automatically) is effectively required; a bare 'a cat' generates badly. CFG 4; 30–40 steps cuts Sigma-Shift-6 noise artifacts.",
        "guide": "Keep the prefix that the template adds, then write your prompt after <Prompt Start>. Longer prompts need the prefix less, but short ones fail without it. Other valid prefixes exist (aesthetics-focused, 2x2 grids — see Lumina's repo).",
    },
    {
        "id": "kandinsky", "label": "Kandinsky 5 Image Lite",
        "match": ["kandinsky"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A constructivist poster of a whale swimming through a city skyline at night, bold geometric shapes, limited palette of deep blues and ochre",
        "negative": "",
        "steps": 20, "cfg": 5.0, "sampler": "euler", "scheduler": "normal",
        "note": "Regular CFG ~5, 20+ steps, 1024 side length.",
        "guide": "Natural language. No official prompting guide — keep prompts descriptive and expect okay-not-great prompt adherence (SwarmUI rates it 'Decent Quality').",
    },
    {
        "id": "hidream", "label": "HiDream O1 (Dev: CFG 1/28 steps)",
        "match": ["hidream"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A quiet monastery library where one book glows faintly gold on a lectern, dust in shafts of morning light, photorealistic, reverent mood",
        "negative": "",
        "steps": 28, "cfg": 1.0, "sampler": "euler", "scheduler": "normal",
        "note": "Dev distill: CFG 1, 28 steps (4 works for simple images). Base: CFG ~5, 50 steps. Side length 2048 standard (its aggressive patch scaling makes 2048 look like most models' 1024).",
        "guide": "Any prompt format; DESIGNED for LLM-written prompts — longer, structured descriptions play to its strengths. Accepts up to 10 reference images in the prompt (single-image obeyed strongest).",
    },
    {
        "id": "ideogram", "label": "Ideogram 4 (JSON captions REQUIRED)",
        "match": ["ideogram"],
        "prefix": "",
        "suffix": "",
        "example_prompt": '{"high_level_description":"A dragonborn innkeeper polishing a tankard behind a torchlit bar","style_description":{"aesthetics":"warm, painterly, cozy","lighting":"firelight glow, soft shadows","medium":"digital painting","color_palette":["#8B3A2E","#F5C542","#2E2E2E"]},"compositional_deconstruction":{"background":"A crowded fantasy tavern at night, wooden beams, hanging herbs, blurred patrons","elements":[{"type":"obj","bbox":[200,250,800,900],"desc":"A scaled dragonborn innkeeper with kind eyes and a brass tankard"}]}}',
        "negative": "",
        "steps": 48, "cfg": 7.0, "sampler": "euler", "scheduler": "ideogram4_default",
        "note": "12 steps = Turbo, 48 = quality (applied). Built-in censorship rejects plain text — JSON captions are how it was trained. Pair with the optional 'Unconditional' model as Negative Model for quality.",
        "guide": "Trained EXCLUSIVELY on structured JSON captions (official schema applied as the example): high_level_description, style_description (aesthetics/lighting/photo/medium/color_palette as hex), compositional_deconstruction (background + elements with bbox [x1,y1,x2,y2] on the 1024 canvas). Plain prose 'will not work and will likely trigger a safety warning' — the example is your scaffold.",
    },
    {
        "id": "krea2", "label": "Krea 2 Turbo (sentences, 8 steps — SwarmUI's 2026 pick)",
        "match": ["krea"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A gothic cathedral fused with alien technology on a moonlit night, heavy fog swallowing the flying buttresses, desaturated blues and violets with gold accents, dramatic chiaroscuro lighting, shot on 35mm film",
        "negative": "",
        "steps": 8, "cfg": 1.0, "sampler": "er_sde", "scheduler": "normal",
        "note": "Turbo: CFG 1 (guidance off), 8 steps (4 minimum). Raw/Base: ~52 steps, CFG 3.5 — for LoRA training and variation re-rolls. Sigma Shift 1.15; 2048-native. Internal text refiner strips NSFW terms.",
        "guide": "SENTENCES beat tags — write it like a caption for a photo you want. Strong text rendering (quote the exact words). The 9 official style LoRAs fire on unguessable trigger phrases: 'monochrome ink wash style'=Darkbrush, 'monochrome stippling style'=Dotmatrix, 'naive expressive sketch style'=Kidsdrawing, 'textured abstract style'=Neondrip, 'rainy window style'=Rainywindow, 'purple retro anime style'=Retroanime, 'art deco watercolor style'=Softwatercolor, 'ethereal motion blur style'=Sunsetblur, 'vintage tarot style'=Vintagetarot.",
    },
    {
        "id": "boogu", "label": "Boogu (Turbo: LCM/SGM, 4 steps)",
        "match": ["boogu"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A smuggler's skiff skimming between icebergs under auroras, spray frozen mid-air, fast and dynamic, cinematic action still",
        "negative": "",
        "steps": 4, "cfg": 1.0, "sampler": "lcm", "scheduler": "sgm_uniform",
        "note": "Turbo (applied): LCM + SGM Uniform, CFG 1, 4 steps. Base/Edit: CFG ~4, 20 steps, DPM++ 2M. Sigma Shift 3.16. Edit variant takes a reference image via the prompt.",
        "guide": "Natural language; OmniGen lineage so it likes instruction-style phrasing ('make it...', scene descriptions). No official prompting guide — keep prompts concrete.",
    },
    {
        "id": "sdturbo", "label": "SD / SDXL Turbo or LCM (distilled)",
        "match": ["turbo", "lcm", "lightning", "hyper", "turbox"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A fox curled asleep on a windowsill, morning light through frosted glass",
        "negative": "",
        "steps": 4, "cfg": 1.0, "sampler": "lcm", "scheduler": "turbo",
        "note": "CFG 1 always. Turbo: 1 step + Scheduler 'Turbo'; LCM: 4 steps + Sampler 'lcm'; Lightning/Hyper: 4–8 steps. (SD3.5-L TurboX: CFG 1, 8 steps, Sigma Shift 5.)",
        "guide": "Short, single-concept prompts — distilled few-step models burn their steps on the subject, not fine prompt detail. Negatives do nothing at CFG 1.",
    },
]


def suggest_template(model_name: str) -> dict | None:
    """The template whose `match` keywords hit the given backend model
    name — without hitting any of its `exclude` keywords (first hit wins,
    list order = priority), or None. Used for the picker's "(suggested)"
    marker — the GM's explicit pick always wins. Excludes keep substring
    collisions honest: "animagine" must not suggest the unrelated Anima
    template, and a "*_turbo" variant of a specific family (krea2_turbo,
    z_image_turbo, ...) must suggest that family, never the generic
    SD-Turbo/LCM template."""
    name = (model_name or "").lower()
    for t in TEMPLATES:
        if any(k in name for k in (t.get("exclude") or [])):
            continue
        if any(k in name for k in t["match"]):
            return t
    return None
