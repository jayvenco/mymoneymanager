// Chart.js-helper: <canvas data-chart='{"type":"line","labels":[...],"datasets":[...]}'>
(function () {
  var nf = new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR" });
  function dark() { return document.documentElement.classList.contains("dark"); }

  function build(canvas) {
    if (!window.Chart || canvas._chart) return;
    var cfg;
    try { cfg = JSON.parse(canvas.dataset.chart); } catch (e) { return; }
    var grid = dark() ? "rgba(148,163,184,0.15)" : "rgba(100,116,139,0.15)";
    var text = dark() ? "#cbd5e1" : "#475569";
    var money = cfg.currency !== false;
    var isCircular = cfg.type === "doughnut" || cfg.type === "pie";
    canvas._chart = new Chart(canvas, {
      type: cfg.type || "line",
      data: { labels: cfg.labels, datasets: cfg.datasets },
      options: Object.assign({
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: cfg.legend !== false && (cfg.datasets.length > 1 || isCircular), labels: { color: text } },
          tooltip: { callbacks: { label: function (ctx) {
            var v = isCircular ? ctx.parsed : ctx.parsed.y;
            return (ctx.dataset.label ? ctx.dataset.label + ": " : (isCircular ? ctx.label + ": " : "")) + (money ? nf.format(v) : v);
          } } },
        },
        scales: isCircular ? {} : {
          x: { stacked: !!cfg.stacked, grid: { display: false }, ticks: { color: text, maxTicksLimit: 12 } },
          y: { stacked: !!cfg.stacked, grid: { color: grid }, ticks: { color: text, callback: function (v) { return money ? nf.format(v) : v; } } },
        },
      }, cfg.options || {}),
    });
  }

  function init(root) { (root || document).querySelectorAll("canvas[data-chart]").forEach(build); }
  document.addEventListener("DOMContentLoaded", function () { init(); });
  document.body && document.body.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
  window.mmmInitCharts = init;
})();
