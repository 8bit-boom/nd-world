"use strict";
// The calendar agenda as an ERAS TIMELINE (templates/calendar/agenda.html, section "calendar_agenda"): the same rows, under a heading for each year
// of the calendar's era (a year header row is added whenever the year changes), drawn as a vertical line of dated entries. The filter box keeps
// working, and a year heading hides itself when none of its days match. Pure helper exported for tests/test_calendar_week.py.

(function (root) {
  // years of consecutive rows -> indexes of the rows that start a new year
  function yearStarts(years) {
    const out = [];
    years.forEach((y, i) => { if (i === 0 || y !== years[i - 1]) out.push(i); });
    return out;
  }
  const core = { yearStarts };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined") return;

  const table = document.getElementById("agenda-table");
  if (!table) return;
  const body = table.tBodies[0];
  const rows = Array.from(body.rows);
  const cols = (table.tHead && table.tHead.rows[0] ? table.tHead.rows[0].cells.length : 3);
  yearStarts(rows.map((r) => r.dataset.year || "")).forEach((i) => {
    const tr = document.createElement("tr"); tr.className = "ag-year"; tr.dataset.year = rows[i].dataset.year || "";
    const td = document.createElement("td"); td.colSpan = cols; td.textContent = rows[i].dataset.year || "";
    tr.append(td); body.insertBefore(tr, rows[i]);
  });
  const input = document.getElementById("agenda-filter");
  function heads() {
    let cur = null, any = false;
    Array.from(body.rows).forEach((r) => {
      if (r.classList.contains("ag-year")) { if (cur) cur.style.display = any ? "" : "none"; cur = r; any = false; }
      else if (r.style.display !== "none") any = true;
    });
    if (cur) cur.style.display = any ? "" : "none";
  }
  if (input) input.addEventListener("input", () => setTimeout(heads, 0));
})(typeof window !== "undefined" ? window : globalThis);
