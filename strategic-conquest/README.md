# Strategic Conquest (browser replica)

A single-file browser homage to *Strategic Conquest*, the turn-based war game
Peter Merrill wrote for the original Macintosh in 1984 (published by PBI
Software, later maintained by Delta Tao Software into the late '90s).

## Running it

No build step, no dependencies. Either open `index.html` directly in a
browser, or serve the folder:

```
cd strategic-conquest
python3 -m http.server 8000
```

then visit `http://localhost:8000`.

## What it plays like

- Pick a map size (small/medium/large, matching the original's 48x32 / 96x64
  / 124x96 presets), a terrain setting (wet/normal/dry, controlling land
  ratio), and an opponent: a computer AI (with easy/normal/hard difficulty)
  or hotseat (two humans passing the same screen back and forth).
- The map is randomly generated every game and starts almost entirely hidden
  behind fog of war. Each side begins with one city and one Army; explore by
  moving units. Enemy units are only visible while adjacent to one of yours.
- Cities build one unit at a time; production accumulates automatically each
  turn. Move an Army or Artillery onto an undefended neutral or enemy city to
  capture it. Capture every enemy city to win.
- Click a unit to select it, then click a tile to move, attack, board a
  Transport/Carrier, or disembark. Click your own (garrisoned or empty) city
  to manage its production queue. Right-drag or WASD/arrow keys pan the
  camera; click the minimap to jump.

## Accuracy notes / where this deviates from the original

Research for this project (see conversation history) confirmed the broad
strokes of the original reliably: square grid, non-wrapping map, the three
size presets, fog of war revealed by exploration, one home city per side,
cities building a single unit type over several turns, capturing cities by
moving an Army onto them, fuel-limited aircraft, ship repair in port, veteran
promotion for planes, and a roster of Army/Artillery/Fighter/Helicopter/
Bomber/Destroyer/Submarine/Transport/Carrier/Battleship.

What the original **did not** have, documented anywhere reachable during
research, was the actual numeric balance table: exact production costs,
attack/defense integers, movement points, or the precise combat-resolution
formula. Fan sites, Wikipedia summaries, and even the scanned manual PDF were
unreachable from this environment, and no secondary source quoted hard
numbers. Rather than guess at "the real numbers," this implementation uses
an original, self-consistent stat table (see the in-game Unit Guide) tuned
for reasonable balance and playability. A few mechanics are original
simplifications made explicit here:

- Every city tile can be entered/repaired-at by ships ("every city is a
  port"), rather than only coastal ones.
- Only one unit may occupy a map tile; Transports/Carriers hold cargo
  internally instead of stacking units on a tile.
- Combat is a single probabilistic roll (`attack / (attack + defense)`)
  rather than the original's undocumented formula.
- The computer opponent is a simple heuristic (attack anything in range,
  otherwise path land units toward the nearest reachable neutral/enemy city,
  route ships/aircraft toward the nearest untaken city) — it does not
  attempt to ferry stranded armies across water to a second landmass, which
  the original's AI may or may not have done.
- The original was strictly 1v1 (AI, hotseat, or old AppleTalk LAN play);
  this keeps that same two-side structure and drops networked play (not
  meaningful for a single static HTML file) in favor of local hotseat.
