// The Export & Backup card for Lore Forge (crate::lore_forge): the trainer answers only in the Python-free image, so
// the card's button appears when /lore-forge/status answers and the card says so otherwise — the same probe the
// teacher card's script makes against /export/teacher/status.
(function () {
  "use strict";
  var open = document.getElementById("lore-forge-open");
  var card = document.getElementById("lore-forge-card");
  if (!open || !card) return;
  fetch("/lore-forge/status", {headers: {"Accept": "application/json"}})
    .then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    })
    .then(function (st) {
      if (st && st.error) throw new Error(st.error);
      open.style.display = "";
    })
    .catch(function () {
      open.style.display = "none";
      var un = document.getElementById("lore-forge-unavailable");
      if (un) un.style.display = "";
    });
})();
