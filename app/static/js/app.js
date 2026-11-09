// Alpine-componenten en HTMX-hooks.
document.addEventListener("alpine:init", function () {
  Alpine.data("toasts", function () {
    return {
      items: [],
      seq: 0,
      init: function () {
        var self = this;
        document.querySelectorAll("[data-flash]").forEach(function (el) {
          self.add({ message: el.textContent.trim(), category: el.dataset.category });
        });
      },
      add: function (detail) {
        var self = this;
        var id = ++this.seq;
        this.items.push(Object.assign({ id: id, category: "info" }, detail));
        setTimeout(function () { self.dismiss(id); }, detail.undoUrl ? 10000 : 5000);
      },
      dismiss: function (id) {
        this.items = this.items.filter(function (t) { return t.id !== id; });
      },
    };
  });

  Alpine.data("setupWizard", function (initial) {
    var today = new Date().toISOString().slice(0, 10);
    var saved = [];
    try { saved = JSON.parse((initial.form && initial.form.accounts) || "[]"); } catch (e) {}
    var errors = initial.errors || {};
    var step = 0;
    if (errors.start_day) step = 2;
    else if (errors.accounts) step = 3;
    else if (errors.partner_name) step = 1;
    return {
      step: step,
      steps: ["Jouw account", "Huishouden", "Financiële maand", "Rekeningen"],
      accounts: saved.length ? saved : [
        { name: "", bank: "", owner: "admin", type: "checking", opening_balance: "", opening_date: today, visibility: "shared" },
      ],
      addAccount: function () {
        this.accounts.push({ name: "", bank: "", owner: "joint", type: "checking", opening_balance: "", opening_date: today, visibility: "shared" });
      },
      prepare: function () {},
    };
  });
});

// Toon fouten van HTMX-verzoeken als toast.
document.addEventListener("htmx:responseError", function (event) {
  window.dispatchEvent(new CustomEvent("toast", {
    detail: { message: "Er ging iets mis (" + event.detail.xhr.status + ").", category: "error" },
  }));
});

// Server kan een toast triggeren via HX-Trigger: {"toast": {...}}.
document.addEventListener("toast", function (event) { window.dispatchEvent(new CustomEvent("toast", { detail: event.detail })); });

// Sneltoets N: nieuwe transactie (wanneer de modal beschikbaar is).
document.addEventListener("keydown", function (event) {
  var tag = (event.target.tagName || "").toLowerCase();
  if (event.key.toLowerCase() === "n" && !event.metaKey && !event.ctrlKey && !event.altKey &&
      !["input", "textarea", "select"].includes(tag) && !event.target.isContentEditable) {
    var trigger = document.querySelector("[data-new-transaction]");
    if (trigger) { event.preventDefault(); trigger.click(); }
  }
});
