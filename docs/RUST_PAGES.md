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
