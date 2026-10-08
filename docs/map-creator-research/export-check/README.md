# Type-checking the Foundry export against Foundry's own schema types

`static/js/map-export.js` writes a Foundry v13 scene. This checks that file against `foundry-vtt-types` (the community TypeScript
definitions generated from Foundry's data models): every key we write must exist in the schema, and every value must have the
right shape (the enum fields are branded numbers in the types, so `Loose<>` in `check.ts` compares them as plain numbers - the
allowed VALUES are checked by the exporter's own `validateScene`, which the tests run).

```
npm init -y && npm i -D typescript @league-of-foundry-developers/foundry-vtt-types@13.346.0-beta.20250812191140
node make_scene.js
npx tsc -p tsconfig.json          # no output = the scene fits the schema
```

Last run (October 2026): no errors; adding an unknown key to a wall, the grid or the scene is reported (negative control).
This is a schema check, not a load into Foundry: see ../EXPORT_LIVE_CHECKLIST.md for what only a running Foundry/Roll20/Owlbear can tell us.
