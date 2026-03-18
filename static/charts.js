/**
 * charts.js — Observable Plot charts for the home page.
 * Loaded via CDN: https://cdn.jsdelivr.net/npm/@observablehq/plot
 * Reads window.__SECTOR_COUNTS and window.__CONFLICT_COUNTS injected by index.html.
 */
(async function () {
  "use strict";

  // Load Observable Plot dynamically from CDN
  const Plot = (await import("https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6/+esm")).default
    || (await import("https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6/+esm"));

  const SECTOR_LABELS = {
    agriculture:          "Agriculture",
    property:             "Property",
    finance:              "Finance",
    legal:                "Legal",
    tourism_hospitality:  "Tourism / Hospitality",
    transport:            "Transport",
    construction:         "Construction",
    energy:               "Energy",
    health:               "Health",
    media:                "Media",
  };

  function toBarData(countsObj) {
    return Object.entries(countsObj || {})
      .map(([sector, count]) => ({
        sector: SECTOR_LABELS[sector] || sector.replace(/_/g, " "),
        count,
      }))
      .sort((a, b) => b.count - a.count);
  }

  function renderBarChart(containerId, data, title) {
    const el = document.getElementById(containerId);
    if (!el || !data.length) return;

    const chart = Plot.plot({
      title,
      marginLeft: 160,
      height: Math.max(140, data.length * 28 + 40),
      x: { label: "Number of TDs", grid: true },
      y: { label: null },
      marks: [
        Plot.barX(data, { x: "count", y: "sector", sort: { y: "-x" }, fill: "#000" }),
        Plot.ruleX([0]),
        Plot.text(data, { x: "count", y: "sector", text: d => String(d.count), dx: 5, fontSize: 11 }),
      ],
      style: { fontFamily: "'Inclusive Sans', sans-serif", fontSize: "13px" },
    });

    el.appendChild(chart);
  }

  const sectorData   = toBarData(window.__SECTOR_COUNTS   || {});
  const conflictData = toBarData(window.__CONFLICT_COUNTS || {});

  renderBarChart("chart-sectors",   sectorData,   "TDs with interests by sector");
  renderBarChart("chart-conflicts", conflictData, "Conflict overlaps by sector");

})().catch(function (err) {
  // Chart load failed (offline, CDN down, etc.) — fail silently
  console.warn("Charts could not load:", err.message);
});
