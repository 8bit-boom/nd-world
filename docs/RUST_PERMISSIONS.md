# The permission rules in Rust

The layer every Rust-served write (and every signed-in page) needs first (`docs/RUST_WRITES.md`, "not done", item 1). `rust/nd-perm` is a
pure library - no database, no HTTP - holding a port of each rule, and a small checker binary (`nd-perm-check`) the Python tests talk to.

| Rule | Python original | Rust |
|---|---|---|
| Route allowlists: what a player, a GM-assistant and a world owner may reach at all | `app/main.py` `_is_player_safe`, `_is_assistant_safe`, `_is_owner_safe` | `allow.rs` |
| The decision `auth_gate` makes with them once the session is valid | `app/main.py` `_auth_gate_rest` | `allow::gate_allows` |
| Section matrix per world (none / read / edit, defaults, floors, custom kinds) | `app/deps.py` `world_section_level`, `world_section_access`, `load_custom_kinds` | `sections.rs` |
| Row ownership (edit any row as GM / assistant, only your own as a player) | `app/deps.py` `world_can_edit_row` | `sections::can_edit_row` |
| `[gmonly]`, `[for=IDs]`, `[reveal=DATE]` stripping for a non-GM viewer | `app/rendering.py` `strip_gm_only` | `gmonly.rs` |

The regular expressions are Python's, unchanged, through `fancy-regex` (it has the look-arounds Python's patterns use); Python's `$`, which also
matches before a final newline, is translated so that `/boards/new\n` is judged the same on both sides.

## Fail closed where the semantics are Python's

Where an answer depends on a Python detail that is not worth reproducing, the Rust function answers **None** ("not sure") and the caller lets
Python decide: digits from other scripts in a tag (`[for=٣]`), JSON Python parses and serde does not (`NaN`, lone surrogates), labels with
characters Python counts as whitespace and Rust does not. In the differential tests this happens for well under 1 % of the inputs.

## How parity is checked - `tests/test_rust_permissions.py`

Differential, not hand-picked: both sides answer the same questions and every difference fails the test.

- **Allowlists**: every route of the app, each path filled 15+ ways (`1`, `new`, `abc`, `٣`, `1\n`, `%2F`, `1/2`, ...) plus prefixes, trailing
  slashes, upper case and suffixes, x 7 methods x player / assistant / owner of two worlds: about 290,000 questions.
- **The gate**: the real `_auth_gate_rest` is run (with a stand-in for the route) for every other corpus path x 3 methods x 6 kinds of viewer
  (GM, no membership, player, assistant, owner of the active world, owner of another world).
- **Section levels**: 300+ random section matrices (valid, wrong-case, wrong-type and missing levels, junk JSON, `NaN`) x custom-kind lists x
  section ids (static, kinds, custom kinds, unknown) x viewer.
- **Row ownership**: every combination.
- **`strip_gm_only`**: 3,000+ random texts built from the tags (nested, unclosed, stray closers, impossible dates, past / future / this minute,
  upper case) x 4 viewers.

The test was checked against itself: changing one allowlist entry in the Rust code made 14 of 288,596 answers differ, and changing `<=` to `<` in
the reveal rule made 48 differ - both failed the test.

It runs wherever the checker is built (CI's `gateway` job builds it; locally `cd rust && cargo build -p nd-perm`), and is skipped otherwise.

## Used by

`POST /api/dice/roll` in the gateway (`writes.rs`) now takes its gate and section decisions from `nd-perm`.

## Not done

- Per-route handler checks beyond these shared rules (a route's own "owner or GM" check, visibility filters on lists, `entity_player_access`,
  `visible_to_players`) - each is ported with the route that needs it, with that route's own refusal-parity test (`docs/RUST_WRITES.md`).
- Anything that needs the database (memberships, the user row) is the caller's job; `nd-perm` only decides from what it is given.
- The failed-login limiter (`docs/RUST_MIGRATION_STATE.md`) is still per process; auth POSTs stay in Python.
