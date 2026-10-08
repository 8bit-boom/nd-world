# Loading the exports into the real tools (what has and has not been verified)

**Verified here (no running VTT was available):** the Foundry scene type-checks against Foundry v13's own schema types
(`export-check/`), passes the exporter's own `validateScene` rules (integer wall coordinates, `move` only 0 or 20, door 0/1/2, grid
size >= 20, light fields), and the Universal VTT file is read back correctly by this app's importer (`tests/test_map_export.py`).

**Not verified:** that Foundry VTT, Roll20 and Owlbear actually open the files and the walls, doors and lights behave. Each takes
about five minutes to try with the sample files below; if something is off, send what the tool says and the fix is usually one
field.

Get the samples: open a map in the editor, then **↓ VTT** (Universal VTT, `.dd2vtt`) and **↓ Foundry** (a `.json` scene + picture).
`docs/map-creator-research/` also holds `hall.dd2vtt` style samples made the same way.

## Foundry VTT (v13)
1. Create a world, open **Scenes**, and import the exported scene (Scene Directory > Import Data, or paste the JSON through a module such as the scene importer).
2. Put the exported picture where the scene's `background.src` points (or re-pick it in Scene Configuration > Basics).
3. Check: the grid lines up with the picture's squares; walls sit on the picture's walls; the door is a door (click it to open); the window lets light through but blocks walking; the secret door looks like a wall to a player; lights glow where the picture's torches are.

## Universal VTT (Roll20, Owlbear Rodeo via an importer, Foundry via a UVTT importer module)
1. Roll20 (Pro): Page Settings > import a Universal VTT file; Owlbear Rodeo: the Dungeon Alchemist/UVTT importer extension; Foundry: the Universal Battlemap Importer module.
2. Check: picture and grid align, walls block vision for a token, the door opens, lights (if any) appear.

## What to report
The tool and its version, what you did, what you expected, what happened (a screenshot of the error or the misaligned grid is enough).
