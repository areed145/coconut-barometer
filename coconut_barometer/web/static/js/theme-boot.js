// Runs before the page draws (a plain script in <head>), so a chosen theme never flashes
// the other one first. The choice lives in this browser only: "light", "dark", or none (Auto).
(function () {
  try {
    var t = localStorage.getItem("cb-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the device */ }
})();
