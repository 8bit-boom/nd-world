"use strict";
// The calendar's Week view (templates/calendar/month.html, section "calendar_view"): the month grid is the page's own; Week shows one of its
// rows - the week holding today (else the first week with a day in it) - with ◀ / ▶ to step through the month's weeks. Month shows them all.
// Pure helpers exported for tests/test_calendar_week.py.

(function (root) {
  const rowCount = (cells, perWeek) => (perWeek > 0 ? Math.ceil(cells / perWeek) : 0);
  // the row to open on: today's row, else the row of the first real day, else 0
  function startRow(todayIdx, firstDayIdx, perWeek) {
    if (perWeek <= 0) return 0;
    const i = todayIdx >= 0 ? todayIdx : Math.max(0, firstDayIdx);
    return Math.floor(i / perWeek);
  }
  const inRow = (idx, row, perWeek) => Math.floor(idx / perWeek) === row;
  const core = { rowCount, startRow, inRow };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.calendarWeekCore = core;
  if (typeof document === "undefined") return;

  const grid = document.getElementById("cal-grid");
  if (!grid || !grid.dataset.pl) return;
  const perWeek = parseInt(grid.dataset.perWeek, 10) || 7;
  const cells = () => [...grid.children].filter((c) => c.classList.contains("cal-cell"));
  const all = cells();
  let row = startRow(all.findIndex((c) => c.classList.contains("today")), all.findIndex((c) => !c.classList.contains("cal-cell-pad")), perWeek);
  const nav = document.createElement("div");
  nav.className = "cal-weeknav";
  nav.innerHTML = '<button type="button" class="cal-nav-btn" aria-label="Previous week">◀</button><span class="cal-weeknav-label" aria-live="polite"></span><button type="button" class="cal-nav-btn" aria-label="Next week">▶</button>';
  const [prev, label, next] = nav.children;
  grid.parentNode.insertBefore(nav, grid);
  function apply() {
    const week = grid.dataset.view === "week", n = rowCount(all.length, perWeek);
    row = Math.max(0, Math.min(n - 1, row));
    nav.hidden = !week;
    grid.classList.toggle("cal-week", week);
    all.forEach((c, i) => { c.hidden = week && !inRow(i, row, perWeek); });
    label.textContent = "Week " + (row + 1) + " of " + n;
    prev.disabled = row <= 0; next.disabled = row >= n - 1;
  }
  prev.addEventListener("click", () => { row--; apply(); });
  next.addEventListener("click", () => { row++; apply(); });
  grid.addEventListener("nd-pl-ready", apply);
  grid.addEventListener("nd-pl-change", apply);
})(typeof window !== "undefined" ? window : globalThis);
