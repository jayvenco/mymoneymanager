// Past het thema toe vóór het renderen (voorkomt flits). Voorkeur komt van de server (data-theme)
// en valt terug op localStorage / systeeminstelling.
(function () {
  var root = document.documentElement;
  function stored() {
    try { return localStorage.getItem("mmm-theme"); } catch (e) { return null; }
  }
  function apply(pref) {
    var dark = pref === "dark" || (pref !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.toggle("dark", dark);
    root.dataset.theme = pref;
  }
  var pref = root.dataset.theme && root.dataset.theme !== "system" ? root.dataset.theme : stored() || "system";
  apply(pref);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    if ((root.dataset.theme || "system") === "system") apply("system");
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
