"""Built-in per-model prompt templates for the Image Gen tab.

Different model families want wildly different prompting (Danbooru tags
vs natural language vs LLM-style system prefixes vs JSON) and different
sampler settings (a distilled 8-step Turbo model wants CFG 1 while a
regular checkpoint wants 4–7) — and the failure mode of getting it wrong
is exactly the "prompt did nothing, output looks random" report that
motivated these. Each template bundles the recommended prompt scaffolding
and generation settings so picking a model family is one click.

Settings follow SwarmUI's own Model Support documentation
(docs/Model Support.md in the SwarmUI repo) as of September 2026, plus
each model's official prompting guidance where it differs. Individual
finetunes can want their own settings — a template is a starting point,
not gospel; the note shown after applying says so.

Static, versioned with the app (no DB rows) — the list is served by
GET /api/ai/imagegen/model-templates and applied client-side by
ai-chat-image.js's igApplyTemplate; the GM's own saved presets (per-world,
DB) remain a separate mechanism. When a backend model's filename matches
a template's `match` keywords, the picker marks it "(suggested)".

Note: nd-world's SwarmUI integration generates IMAGES only. Video models
(Hunyuan Video, LTX, Wan, MiniMax H3) and audio/music models (Ace Step,
MiniMax Music) are driven from SwarmUI's own UI and deliberately have no
template here.
"""

# Each template:
#   id/label        — stable id + display name.
#   match           — lowercase substrings; if the selected backend model's
#                     name contains any, this template is the suggested one.
#   prefix/suffix   — comma-separated style tags merged around the GM's own
#                     prompt text (only tags not already present). A prefix
#                     may also be prose (Lumina's LLM system prefix).
#   example_prompt  — fills an empty prompt box so the shape is obvious.
#   negative        — replaces the negative-prompt box ("" clears it — a
#                     family like Flux genuinely wants NO negative).
#   steps/cfg       — recommended generation settings (applied to sliders).
#   sampler/scheduler — applied when the backend offers them ("" = leave).
#   note            — one line shown after applying.

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
        "note": "Baseline SDXL: CFG 5–8, steps 20+. Natural language or tags both work; finetunes may want their own tags. Swarm adds enhanced inference settings automatically.",
    },
    {
        "id": "sd35", "label": "Stable Diffusion 3.5 (Large/Medium)",
        "match": ["sd3.5", "sd_3.5", "sd35", "sd3", "stable-diffusion-3"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "An astronaut botanist tending glowing alien orchids inside a cracked space-station greenhouse, Earth visible through the broken glass, cinematic soft light",
        "negative": "worst quality, low quality, watermark, text, deformed, blurry",
        "steps": 28, "cfg": 4.5, "sampler": "euler", "scheduler": "normal",
        "note": "CFG ~4.5, steps 28–40, natural language. Sigma Shift defaults to 3. For upscaling use Refiner Do Tiling (SD3 responds badly to untiled upscale).",
    },
    {
        "id": "flux1", "label": "Flux.1 (dev/schnell — CFG 1, no negatives)",
        "match": ["flux.1", "flux1", "flux_dev", "flux_schnell", "flux-dev", "flux-schnell"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A medieval marketplace at golden hour rendered as a detailed ink-and-watercolor illustration, banners in a light wind, merchants and townsfolk mid-motion, warm long shadows",
        "negative": "",
        "steps": 20, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "note": "CFG 1 and negative prompts DON'T work (adherence comes from embedded Flux Guidance, a separate SwarmUI param). Dev: 20+ steps; Schnell: 4. Euler + Simple.",
    },
    {
        "id": "flux2", "label": "Flux.2 / Klein (natural language, no negatives)",
        "match": ["flux.2", "flux2", "flux-2", "flux_2", "klein"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "Editorial photograph of a dragonfly resting on a brass astrolabe, morning light through a greenhouse window, condensation on glass, extremely fine detail, natural depth of field",
        "negative": "",
        "steps": 24, "cfg": 1.0, "sampler": "euler", "scheduler": "flux2",
        "note": "CFG 1; officially NO negative prompts — describe the scene directly (setting, light, mood). Dev: 50 steps ideal, 20 works; the Turbo variant uses 8 steps + 'Align Your Steps'. Klein distilled: 8 steps / CFG 1; Klein base: high steps / CFG ~7. Flux Guidance Scale 3.5–4.",
    },
    {
        "id": "chroma", "label": "Chroma (Flux-derived — REGULAR CFG ~3.5)",
        "match": ["chroma"],
        "exclude": ["radiance", "zeta"],
        "prefix": "",
        "suffix": "highly detailed",
        "example_prompt": "A lighthouse keeper's daughter feeding a gull mid-flight on a windswept pier, overcast North-Sea light, muted palette, candid documentary feel, highly detailed",
        "negative": "",
        "steps": 26, "cfg": 3.5, "sampler": "euler", "scheduler": "beta",
        "note": "Unlike Flux, Chroma uses STANDARD CFG (~3.5) — official workflow: 26 steps, 'Align Your Steps' (or Beta) scheduler. Works better with LONGER prompts ('prompt fluff' at the end helps clean it up).",
    },
    {
        "id": "zimage", "label": "Z-Image (Turbo: 8 steps CFG 1 / Base: normal)",
        "match": ["z-image", "z_image", "zimage"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "Shot on 85mm lens, shallow depth of field: an old cartographer's workshop at night, candlelight over inked maps of imaginary coastlines, dust motes in the air, warm and moody",
        "negative": "",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "note": "Turbo: CFG 1, 8 steps (4 minimum). Base: CFG 4–7, 20+ steps. Any prompt format works (English + Chinese; camera/lighting-first prose shines). Euler Ancestral + Beta scheduler can add photoreal detail. Hard prompts may want up to 20 steps even on Turbo.",
    },
    {
        "id": "anima", "label": "Anima (2B anime — booru tags or prose, CFG ~4)",
        "match": ["anima"],
        "exclude": ["animagine"],
        "prefix": "masterpiece, best quality, very aesthetic,",
        "suffix": "detailed background",
        "example_prompt": "masterpiece, best quality, very aesthetic, 1girl, traveler, moonlit rooftop, coat, lantern, detailed background",
        "negative": "lowres, worst quality, low quality, bad anatomy, bad hands, jpeg artifacts, signature, username, watermark, blurry",
        "steps": 24, "cfg": 4.0, "sampler": "er_sde", "scheduler": "simple",
        "note": "Regular CFG (~4) — Anima is NOT a distilled model despite being tiny/fast. Booru tags AND natural language are both trained. ER-SDE-Solver default (Euler Ancestral / DPM++ 2M SDE also good); stay at ~1024 res (higher corrupts).",
    },
    {
        "id": "pony", "label": "Pony / Illustrious (score tags, Danbooru)",
        "match": ["pony", "illustrious", "noobai"],
        "prefix": "score_9, score_8_up, score_7_up,",
        "suffix": "",
        "example_prompt": "score_9, score_8_up, score_7_up, 1girl, rogue, hooded cloak, neon-lit alley, cyberpunk city, looking at viewer",
        "negative": "score_6, score_5, score_4, worst quality, low quality, watermark, blurry, deformed, bad anatomy",
        "steps": 28, "cfg": 7.0, "sampler": "euler_a", "scheduler": "normal",
        "note": "The score_9/8/7 tags are REQUIRED (they carry the style), plus Danbooru tags for content; optional source_* tags for fandom styles.",
    },
    {
        "id": "qwen", "label": "Qwen Image (natural language, strong text)",
        "match": ["qwen"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A chalkboard tavern menu in crisp hand-lettered English listing three fantastical drinks, hanging on a timber wall beside a lantern, photorealistic, warm evening light",
        "negative": "",
        "steps": 30, "cfg": 4.0, "sampler": "euler", "scheduler": "normal",
        "note": "Slow-but-smart: CFG 4 + steps 30+ for quality (CFG 1 is a pure speed trade-off; distilled/Lightning variants: CFG 1, 8–15 steps). Excellent in-image TEXT rendering — put the exact words in quotes. 1328x1328 native. Very RAM-hungry.",
    },
    {
        "id": "hunyuan", "label": "Hunyuan Image 2.1 (2K-native)",
        "match": ["hunyuanimage", "hunyuan_image", "hunyuan-img", "hunyuanimage2"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A rain-slicked cyberpunk street market at midnight reflected in puddles, holographic signage in two languages, a lone figure under a translucent umbrella, cinematic wide shot",
        "negative": "",
        "steps": 20, "cfg": 3.5, "sampler": "euler", "scheduler": "normal",
        "note": "CFG ~3.5, steps ~20, targets 2048x2048 (usable lower). Distilled variant: CFG 1. Wants its refiner model for fine detail — SwarmUI's Refine group with Refiner CFG 1 / 4 steps.",
    },
    {
        "id": "lumina", "label": "Lumina 2.0 (LLM-style prompt prefix!)",
        "match": ["lumina"],
        "prefix": "You are an assistant designed to generate high-quality images with the highest degree of image-text alignment based on textual prompts. <Prompt Start>",
        "suffix": "",
        "example_prompt": "You are an assistant designed to generate high-quality images with the highest degree of image-text alignment based on textual prompts. <Prompt Start> a floating archipelago of small islands connected by rope bridges at dawn, waterfalls falling into a sea of clouds, soft painterly light",
        "negative": "",
        "steps": 32, "cfg": 4.0, "sampler": "euler", "scheduler": "normal",
        "note": "It has an LLM (Gemma 2) input: prompts MUST start with the system-style prefix (applied) — a bare 'a cat' generates badly. CFG 4; ~30–40 steps cuts noise artifacts (Sigma Shift 6).",
    },
    {
        "id": "kandinsky", "label": "Kandinsky 5 Image Lite",
        "match": ["kandinsky"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A constructivist poster of a whale swimming through a city skyline at night, bold geometric shapes, limited palette of deep blues and ochre",
        "negative": "",
        "steps": 20, "cfg": 5.0, "sampler": "euler", "scheduler": "normal",
        "note": "Regular CFG ~5, 20+ steps, 1024 side length. (Video variants exist too — nd-world generates images only.)",
    },
    {
        "id": "hidream", "label": "HiDream O1 (Dev: CFG 1, 28 steps / Base: CFG 5, 50)",
        "match": ["hidream"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A quiet monastery library where one book glows faintly gold on a lectern, dust in shafts of morning light, photorealistic, reverent mood",
        "negative": "",
        "steps": 28, "cfg": 1.0, "sampler": "euler", "scheduler": "normal",
        "note": "Dev distill: CFG 1, 28 steps (settings applied). Base model instead: CFG ~5, 50 steps. Any prompt format; designed for LLM-written prompts. Side length 2048 standard (1024 looks worse).",
    },
    {
        "id": "ideogram", "label": "Ideogram 4 (JSON prompts, quality: ~48 steps)",
        "match": ["ideogram"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A minimal vintage-style travel poster for a moon-colony resort, bold typography reading 'VISIT TRANQUILLITY', muted retro palette, screen-print texture",
        "negative": "",
        "steps": 32, "cfg": 7.0, "sampler": "euler", "scheduler": "ideogram4_default",
        "note": "12 steps = Turbo speed, 48 = quality. Official prompting guide is long-form JSON (it understands bounding boxes); plain prompts often trigger its built-in censorship — pair with the optional Unconditional model as Advanced > Negative Model.",
    },
    {
        "id": "krea2", "label": "Krea 2 Turbo (8 steps, CFG 1 — SwarmUI's 2026 pick)",
        "match": ["krea"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A gothic cathedral fused with alien technology on a moonlit night, heavy fog, desaturated blues and violets with gold accents, cinematic composition, dramatic chiaroscuro lighting, shot on 35mm film",
        "negative": "",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "normal",
        "note": "Turbo: CFG 1, 8 steps (4 minimum); built-in text-refiner censors NSFW terms. Base/Raw: 20+ steps, CFG 4+. Sigma Shift 1.15. Text rendering is a strength; long natural-language prompts.",
    },
    {
        "id": "boogu", "label": "Boogu (Turbo: LCM/SGM, 4 steps, CFG 1)",
        "match": ["boogu"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A smuggler's skiff skimming between icebergs under auroras, fast and dynamic, cinematic action still",
        "negative": "",
        "steps": 4, "cfg": 1.0, "sampler": "lcm", "scheduler": "sgm_uniform",
        "note": "Turbo (applied): LCM sampler + SGM Uniform scheduler, CFG 1, 4 steps. Base/Edit models instead: CFG ~4, 20 steps, DPM++ 2M. Sigma Shift 3.16.",
    },
    {
        "id": "sdturbo", "label": "SD / SDXL Turbo or LCM (distilled)",
        "match": ["turbo", "lcm", "lightning", "hyper", "turbox"],
        "prefix": "",
        "suffix": "",
        "example_prompt": "A fox curled asleep on a windowsill, morning light through frosted glass",
        "negative": "",
        "steps": 4, "cfg": 1.0, "sampler": "lcm", "scheduler": "turbo",
        "note": "Distilled variants: CFG 1 always. Turbo: 1 step + Scheduler 'Turbo'; LCM: 4 steps + Sampler 'lcm' (set both if the result looks off); Lightning/Hyper: 4–8 steps. (SD3.5-L TurboX: CFG 1, 8 steps, Sigma Shift 5.)",
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
