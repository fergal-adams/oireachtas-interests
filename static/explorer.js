/**
 * explorer.js — client-side filter for the Interest Explorer table.
 * No framework. Works on the server-rendered table using data-* attributes.
 */
(function () {
  "use strict";

  const table        = document.getElementById("explorer-table");
  const countEl      = document.getElementById("explorer-count");
  const searchInput  = document.getElementById("filter-search");
  const sectorSelect = document.getElementById("filter-sector");
  const partySelect  = document.getElementById("filter-party");
  const conflictsCb  = document.getElementById("filter-conflicts");

  if (!table) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));

  function filterRows() {
    const search    = (searchInput  ? searchInput.value.trim().toLowerCase()  : "");
    const sector    = (sectorSelect ? sectorSelect.value                       : "");
    const party     = (partySelect  ? partySelect.value                        : "");
    const conflicts = (conflictsCb  ? conflictsCb.checked                      : false);

    let visible = 0;

    rows.forEach(function (row) {
      const name    = row.dataset.name    || "";
      const rowParty   = row.dataset.party   || "";
      const sectors = row.dataset.sectors || "";
      const hasConflicts = row.dataset.conflicts === "yes";
      const text    = row.dataset.text    || "";

      let show = true;

      if (search && !name.includes(search) && !text.includes(search)) {
        show = false;
      }
      if (sector && !sectors.split(" ").includes(sector)) {
        show = false;
      }
      if (party && rowParty !== party) {
        show = false;
      }
      if (conflicts && !hasConflicts) {
        show = false;
      }

      row.classList.toggle("hidden", !show);
      if (show) visible++;
    });

    if (countEl) {
      countEl.textContent = "Showing " + visible + " of " + rows.length + " TDs";
    }
  }

  // Attach listeners
  if (searchInput)  searchInput .addEventListener("input",  filterRows);
  if (sectorSelect) sectorSelect.addEventListener("change", filterRows);
  if (partySelect)  partySelect .addEventListener("change", filterRows);
  if (conflictsCb)  conflictsCb .addEventListener("change", filterRows);

  // Initial run
  filterRows();
})();
