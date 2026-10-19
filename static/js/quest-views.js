"use strict";
// The quest list's board (templates/quests/list.html, the shared board in static/js/nd-board.js): a column per status, a quest dragged to
// another column changes its status through POST /api/quests/ID/status (the server decides who may: a GM or assistant any quest, a player only
// the quests they made - a refusal is said, nothing moves). The other layouts (list, cards) are CSS from static/js/nd-views.js.
(function () {
  const list = document.getElementById("quest-cards");
  if (!list || !window.ndBoard) return;
  const canEdit = list.dataset.canEdit === "1";
  const groupNow = () => { const st = list.ndViews && list.ndViews.state(); return (st && st.group) || "status"; };
  window.ndBoard.attach(list, {
    mode: "board",
    key: groupNow,
    columns: () => { try { return groupNow() === "status" ? JSON.parse(list.dataset.statuses || "[]") : []; } catch (e) { return []; } },
    editable: () => canEdit && groupNow() === "status",
    idOf: (item) => item.dataset.id,
    label: (v) => (v ? v.charAt(0).toUpperCase() + v.slice(1) : "None"),
    move: async (item, value) => {
      try {
        const res = await fetch("/api/quests/" + encodeURIComponent(item.dataset.id) + "/status", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: value }) });
        if (res.status === 403) { window.ndBoard.say("You can only move quests you made."); return null; }
        if (!res.ok) { window.ndBoard.say("Could not move it (" + res.status + ")."); return null; }
        const d = await res.json();
        return d.status || value;
      } catch (e) { window.ndBoard.say("Could not reach the server."); return null; }
    },
  });
})();
