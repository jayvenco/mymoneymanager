// Past het thema toe vóór het renderen (voorkomt flits). Voorkeur komt van de server (data-theme)
// en valt terug op localStorage / systeeminstelling. Thema's: light, dark, ocean, warm, midnight, system.
(function () {
  var root = document.documentElement;
  var DARK = { dark: true, midnight: true };
  var KNOWN = { light: 1, dark: 1, ocean: 1, warm: 1, midnight: 1, system: 1 };
  function stored() {
    try { return localStorage.getItem("mmm-theme"); } catch (e) { return null; }
  }
  function apply(pref) {
    if (!KNOWN[pref]) pref = "system";
    var dark = DARK[pref] || (pref === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.toggle("dark", !!dark);
    // 'system' kleurt als wit of donker, afhankelijk van de voorkeur van het apparaat
    root.dataset.theme = pref === "system" ? (dark ? "dark" : "light") : pref;
    root.dataset.pref = pref;
  }
  var serverPref = root.dataset.theme;
  apply(serverPref && serverPref !== "system" ? serverPref : stored() || "system");
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    if (root.dataset.pref === "system") apply("system");
  });
  window.mmmSetTheme = function (value) {
    try { localStorage.setItem("mmm-theme", value); } catch (e) {}
    apply(value);
    var token = document.querySelector("body").getAttribute("hx-headers");
    try {
      fetch("/settings/theme", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/x-www-form-urlencoded" }, JSON.parse(token)),
        body: "theme=" + encodeURIComponent(value),
        credentials: "same-origin",
      });
    } catch (e) {}
  };
})();
