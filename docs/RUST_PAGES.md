# Pages built by the gateway (Rust)

Phase 2 of `docs/RUST_REWRITE_PLAN.md`. Which pages the gateway builds itself, how it is switched, and what stands between this and the other
~130 pages. The gateway (`docs/RUST_GATEWAY.md`) now has a `pages` module and a `nd-render` crate: MiniJinja over the app's own
`app/templates/`, so a template edit changes both servers.

## Switching

`ND_RUST_ROUTES` (comma separated, default `login`; empty = Python builds every page). A page answers only when the gateway is sure it
produces what Python would: it needs `SECRET_KEY` in its environment (to read the session cookie - without it every page goes to Python) and
a `GET`. Everything else, the POST of a form included, is passed on. Rollback of a page is removing its name and restarting; rollback of
all of it is `ND_RUST_ROUTES=` or `ND_GATEWAY=0`.

## Built so far

| Route | Notes |
|---|---|
| `GET /dice` (`dice.html` in `base.html`) | the first signed-in page; switched on with `pages` in `ND_RUST_ROUTES` (opt-in). Built from the layout context (below) plus the world's last 50 rolls. Same HTML as Python for a GM, a player and an assistant, with a custom kind, custom section access, a theme, a saved dice layout, `?embed=1`, `?error=`/`?n=`, `?w=` and `?q=` (`tests/test_gateway_golden.py`). 84 -> 237 pages/s, p50 165 -> 58 ms. |
| `/chronicler`, `/background-jobs`, `/tools/bulk-edit`, `/system`, `/boards` | batch 2: the layout plus a section check and at most one list - one row each in `SIMPLE` (`rust/nd-gateway/src/pages.rs`); same HTML or Python's refusal for three roles |
| `GET /login` (`auth/login.html`) | no database: the session cookie is read (signed in = 303 to `next`), `next` is clamped like `auth.safe_next_url`, the form is rendered. The POST (rate limiter, passwords, 2FA) stays in Python. |

## Differences between MiniJinja and Jinja2 that the work found (and how each is handled)

| | |
|---|---|
| All 128 templates **compile** in MiniJinja | `nd-render/tests/compat.rs` (compile only: a missing filter or a Python-only method fails at render time, not before) |
| Escaping: MiniJinja writes `&#x2f;` for `/` and `&quot;`, Jinja2 `/` and `&#34;` | same HTML, different bytes; `nd-render` installs Jinja2's (markupsafe's) escaping so pages compare byte for byte |
| `{{ none }}` / booleans print as `none` / `true` in MiniJinja, `None` / `True` in Jinja2 | not handled yet: each page that prints one needs a check; the golden test finds it |
| Python methods on values (`.items()`, `.get()`, `.startswith()`, `loop.changed`, ...) | MiniJinja has a `pycompat` add-on; not enabled yet, needed from the first page that uses them |
| 20 filters / globals written in Python (`md`, `thumb`, `display`, `wq`, `asset_v`, `can_edit`, `site_layout`, `view_spec`, `saved_view`, ...) | only `asset_v` is ported. `md` needs a markdown renderer that matches `markdown2` + extras + `nh3` sanitising |

## Why the other pages are not done yet (measured on the code)

135 `TemplateResponse` calls in 78 modules; 92 templates extend `base.html`. A signed-in page is not just its template: the layout around it
(`base.html`) takes its context from code that has to be ported first, and that code is where the permission rules are:

| Needed by every signed-in page | Python today | Size |
|---|---|---|
| Who is signed in, banned/stale-session checks, GM / assistant / world-owner role | `auth_gate`, `_auth_lookup`, `_active_membership`, `_is_player_safe` / `_is_assistant_safe` / `_is_owner_safe` (`main.py`) | the allowlists are the riskiest code in the app |
| The active world and the worlds this person may see | `deps.get_world_ctx`, `auth.accessible_world_ids` | small |
| Section access per world (`world_can_view_section`) | `deps.py` | the permission matrix |
| Navigation menus, custom kinds, theme | `nav_menus.py` (299 lines), `deps.effective_kinds`, `theme_presets.py` (367), world settings JSON | medium |
| Site layout and saved view layouts per person | `site_layout.py`, `view_prefs.py` (323), `UserViewPref` rows | medium |
| App settings flags | `database.get_app_settings_flags_cached` | small |

Then each page's own data and logic (entity lists with visibility filtering, `[gmonly]` stripping, rules rendering with 555 lines of its own
markdown handling, search, maps, calendar ...). The plan's estimate for this phase (8-12 weeks full time) stands; one page needs the whole
table above first, and every page after it is cheaper.

## Where to go next

1. Port the table above as a Rust `layout` module, with the Python code as the specification and a **permission matrix test** (every route x
   role x section access, run against both servers) written *before* the code.
2. First authenticated page: a small one (`/dice`: one table, `world_can_view_section`) in "shadow" mode - the gateway renders it, compares with
   Python's answer, sends Python's, and logs differences - before it is switched on.
3. Then the big read pages (entity lists, entity detail, rules, search), where the speed gain is.

## The layout context (batch 1 of the full port)

Every signed-in page sits in `base.html`, which needs: the user and their session (`session_version`, the site layout the session carries), the
active world and the worlds the person may use, their role there, the route allowlist (`rust/nd-perm`), the world's theme, the navigation menus
(custom kinds, conditions, per-role section access), the hover-preview settings and the person's saved view layouts. `rust/nd-gateway/src/layout.rs`
builds all of it; `rust/nd-render/src/app.rs` holds the two pure pieces, checked against Python with random inputs (`tests/test_rust_layout.py`:
~7,000 layout choices, 1,200 navigation cases). The app's data tables (layout sections, nav catalog, kinds) are copied from Python by
`scripts/gen_rust_data.py` (`tests/test_rust_data.py` fails when stale). MiniJinja got Python's methods on values (`.get`, `.startswith`, ...),
Jinja2's exact `tojson` and `urlencode`, the `wq` filter and the per-page `site_layout` / `can_edit` / `saved_view` functions.

The gateway leaves a page to Python when: the session's site-layout copy is missing or older than 15 minutes (Python refreshes it in the cookie),
the cookie is older than 6 hours, the world shows its hero banner on every page (`_hero.html` is not ported), or anything else is unusual.

## Markdown (`|md`) - a decision, not yet made

`rust/nd-render/src/md.rs` is `render_md` with pulldown-cmark for the markdown step and a line-by-line port of everything after it (the app's own
tags, media titles, prose code blocks). `scripts/compare_markdown.py` feeds every markdown file of the repository (cut into sections, 984 texts)
to both: **73 % render the same**. The differences are markdown2's own: it turns underscores inside words into italics (`AI_ENTITY_GUIDE.md` ->
"AI*ENTITY*GUIDE.md"), puts no language class on fenced code, and reads some fenced blocks inside lists differently. So pages with written
content (entities, notes, facts, recaps, rules) would look different in places once Rust builds them - mostly better. Not used by any page
yet; the choice (accept CommonMark, imitate markdown2, or keep a Python helper for it) is the owner's.

Python dates in templates: database DATETIME columns reach templates as objects with Python's `strftime`, `isoformat`, `date()` and `str()`.
