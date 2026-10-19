"use strict";
// The character page's layout decisions, kept pure so they can be tested under Node (tests/test_hub_layout.py); the page itself
// (templates/characters/_player_hub.html) only applies them.
//
//   pinsFor      - which tabs the phone's bottom bar shows: the player's pins (Sheet is always one), or the default four, at most `max`
//   swipePage    - a finger drag over the sheet in the Play layout -> -1 (previous page), +1 (next page) or 0 (not a swipe)
//   stepPage     - the sheet page to show after a swipe, clamped (the "All" page is not part of the swipe order)
//   scrollHints  - which edges of a scrolling tab strip still have tabs hidden behind them (for the fade and the arrows)

(function (root) {
  const DEFAULT_PINS = ["sheet", "journey", "quests", "notes"];

  function pinsFor(chosen, tabs, max) {
    max = max || 4;
    const have = (id) => tabs.indexOf(id) >= 0;
    let picked = (Array.isArray(chosen) ? chosen : []).filter((t, i, a) => have(t) && a.indexOf(t) === i);
    if (!picked.length) picked = DEFAULT_PINS.filter(have);
    picked = picked.filter((t) => t !== "sheet");
    return ["sheet"].concat(picked).slice(0, max);
  }

  // dx / dy in px, dt in ms. A swipe is mostly horizontal, long enough, and quick.
  function swipePage(dx, dy, dt) {
    if (!(Math.abs(dx) >= 60) || !(Math.abs(dx) >= 1.8 * Math.abs(dy)) || !(dt <= 700)) return 0;
    return dx < 0 ? 1 : -1;
  }

  function stepPage(ids, current, dir) {
    const order = ids.filter((id) => id !== "all");
    const at = order.indexOf(current);
    if (at < 0 || !dir) return current;
    return order[Math.max(0, Math.min(order.length - 1, at + dir))];
  }

  function scrollHints(scrollLeft, clientWidth, scrollWidth) {
    const slack = 4;
    return { left: scrollLeft > slack, right: scrollLeft + clientWidth < scrollWidth - slack };
  }

  const core = { DEFAULT_PINS, pinsFor, swipePage, stepPage, scrollHints };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.hubLayout = core;
})(typeof window !== "undefined" ? window : globalThis);
