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
      undo: function (t) {
        // Binnen x-for is `this` een samengestelde scope; werk daarom via de component zelf.
        var self = Alpine.$data(document.getElementById("toasts"));
        var url = t.undoUrl;
        self.dismiss(t.id);
        fetch(url, {
          method: "POST",
          headers: JSON.parse(document.body.getAttribute("hx-headers")),
          credentials: "same-origin",
        }).then(function (r) {
          var trigger = r.headers.get("HX-Trigger");
          if (trigger) {
            try { var d = JSON.parse(trigger); if (d.toast) self.add(d.toast); } catch (e) {}
          }
          document.body.dispatchEvent(new CustomEvent("tx-changed"));
        });
      },
      dismiss: function (id) {
        this.items = this.items.filter(function (t) { return t.id !== id; });
      },
    };
  });

  // Zijbalk (ingeklapt/uitgeklapt, onthouden per browser).
  Alpine.data("appShell", function () {
    var stored = false;
    try { stored = localStorage.getItem("mmm-nav") === "1"; } catch (e) {}
    return {
      collapsed: stored,
      toggle: function () {
        this.collapsed = !this.collapsed;
        try { localStorage.setItem("mmm-nav", this.collapsed ? "1" : "0"); } catch (e) {}
      },
    };
  });

  // Tag-invoer met chips, suggesties en 'aanmaken tijdens het typen'.
  Alpine.data("tagInput", function (initial, all) {
    return {
      tags: (initial || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      all: all || [],
      text: "",
      focused: false,
      get suggestions() {
        var t = this.text.trim().toLowerCase();
        var have = this.tags.map(function (x) { return x.toLowerCase(); });
        return this.all.filter(function (n) {
          return have.indexOf(n.toLowerCase()) < 0 && (!t || n.toLowerCase().indexOf(t) >= 0);
        }).slice(0, 6);
      },
      add: function (name) {
        name = (name || this.text).replace(/,/g, " ").trim();
        if (name && this.tags.map(function (x) { return x.toLowerCase(); }).indexOf(name.toLowerCase()) < 0) this.tags.push(name);
        this.text = "";
      },
      remove: function (i) { this.tags.splice(i, 1); },
      get value() { return this.tags.concat(this.text.trim() ? [this.text.trim()] : []).join(", "); },
    };
  });

  // Drag & drop sorteren: <ul data-sortable="/url"> met kinderen data-id="...".
  Alpine.directive("sortable", function (el, directive) {
    var url = directive.expression ? el.dataset.sortable : el.dataset.sortable;
    var dragged = null;
    el.addEventListener("dragstart", function (e) {
      dragged = e.target.closest("[data-id]");
      if (dragged) { e.dataTransfer.effectAllowed = "move"; dragged.classList.add("opacity-50"); }
    });
    el.addEventListener("dragover", function (e) {
      if (!dragged) return;
      e.preventDefault();
      var over = e.target.closest("[data-id]");
      if (over && over !== dragged && over.parentNode === dragged.parentNode) {
        var rect = over.getBoundingClientRect();
        over.parentNode.insertBefore(dragged, e.clientY > rect.top + rect.height / 2 ? over.nextSibling : over);
      }
    });
    el.addEventListener("dragend", function () {
      if (!dragged) return;
      dragged.classList.remove("opacity-50");
      var list = dragged.parentNode;
      var body = new URLSearchParams();
      Array.prototype.forEach.call(list.children, function (c) { if (c.dataset.id) body.append("ids", c.dataset.id); });
      dragged = null;
      fetch(url, {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/x-www-form-urlencoded" }, JSON.parse(document.body.getAttribute("hx-headers"))),
        body: body, credentials: "same-origin",
      });
    });
  });

  // Bulkselectie in de transactielijst.
  Alpine.data("bulkSelect", function () {
    return {
      selected: [],
      toggleAll: function (checked) {
        var self = this;
        self.selected = checked ? Array.prototype.map.call(document.querySelectorAll("[data-row-check]"), function (c) { return c.value; }) : [];
      },
      clear: function () { this.selected = []; },
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

// HX-Trigger-events van de server (toast, tx-changed, close-modal, ...) bubbelen vanzelf naar window.
function mmmReload(target) {
  var el = document.querySelector(target);
  if (el && window.htmx) {
    htmx.ajax("GET", location.pathname + location.search, { target: target, swap: "innerHTML" });
  } else {
    location.reload();
  }
}
document.body.addEventListener("tx-changed", function () {
  if (document.querySelector("#tx-results")) mmmReload("#tx-results"); else location.reload();
});
["accounts-changed", "reload-page"].forEach(function (name) {
  document.body.addEventListener(name, function () { location.reload(); });
});
document.body.addEventListener("close-modal", function () { window.mmmCloseModal(); });
window.mmmCloseModal = function () {
  var root = document.getElementById("modal-root");
  if (root) root.innerHTML = "";
};

// 422 (validatiefouten) wel in de pagina plaatsen, zodat formulieren hun fouten tonen.
document.body.addEventListener("htmx:beforeSwap", function (event) {
  if (event.detail.xhr.status === 422) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});

// Sneltoets N: nieuwe transactie (wanneer de modal beschikbaar is).
document.addEventListener("keydown", function (event) {
  var tag = (event.target.tagName || "").toLowerCase();
  if (event.key.toLowerCase() === "n" && !event.metaKey && !event.ctrlKey && !event.altKey &&
      !["input", "textarea", "select"].includes(tag) && !event.target.isContentEditable) {
    var trigger = document.querySelector("[data-new-transaction]");
    if (trigger) { event.preventDefault(); trigger.click(); }
  }
});
