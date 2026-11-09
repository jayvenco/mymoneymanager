// Kopieert alle front-end bibliotheken naar app/static/vendor (alles lokaal, geen CDN).
import { copyFileSync, mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";

const out = "app/static/vendor";
mkdirSync(out, { recursive: true });

copyFileSync("node_modules/htmx.org/dist/htmx.min.js", `${out}/htmx.min.js`);
copyFileSync("node_modules/alpinejs/dist/cdn.min.js", `${out}/alpine.min.js`);
copyFileSync("node_modules/chart.js/dist/chart.umd.js", `${out}/chart.umd.js`);

// SVG-sprite met alleen de gebruikte Lucide-iconen.
const icons = [
  "bar-chart-3", "arrow-left-right", "wallet", "gauge", "piggy-bank", "settings", "plus",
  "sun", "moon", "monitor", "log-out", "user", "users", "chevron-left", "chevron-right",
  "chevron-down", "x", "check", "trash-2", "pencil", "copy", "search", "filter", "download",
  "upload", "home", "utensils", "shirt", "ticket", "trending-up", "shopping-bag", "banknote",
  "plus-circle", "coins", "scale", "tag", "tags", "repeat", "target", "alert-triangle",
  "info", "lock", "eye", "eye-off", "calendar", "refresh-cw", "shield-check", "database",
  "menu", "more-horizontal", "undo-2", "arrow-up", "arrow-down", "arrow-up-down",
  "grip-vertical", "archive", "file-text", "file-spreadsheet", "printer", "link", "landmark",
];
// Nieuwe bestandsnamen in recente Lucide-versies; de oude id's blijven in de templates bruikbaar.
const aliases = {
  "bar-chart-3": "chart-column",
  home: "house",
  "plus-circle": "circle-plus",
  "alert-triangle": "triangle-alert",
  "more-horizontal": "ellipsis",
};
let sprite = '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">';
for (const name of icons) {
  const file = `node_modules/lucide-static/icons/${aliases[name] ?? name}.svg`;
  if (!existsSync(file)) {
    console.warn(`icoon ontbreekt: ${name}`);
    continue;
  }
  const svg = readFileSync(file, "utf8");
  const inner = svg.replace(/^[\s\S]*?<svg[^>]*>/, "").replace(/<\/svg>\s*$/, "");
  sprite += `<symbol id="${name}" viewBox="0 0 24 24">${inner}</symbol>`;
}
sprite += "</svg>";
writeFileSync(`${out}/icons.svg`, sprite);
console.log("vendor-bestanden geschreven naar", out);
