// Small client behaviours: sidebar search and tabs, variable and fact filters, busy buttons.
// Re-run after every htmx swap, since hx-boost replaces the body.
(function () {
  // setup() runs after every htmx swap (including the 1s run poll), so each
  // element is bound once and remembered.
  function once(el, key) {
    if (!el || el.dataset["bound" + key]) return false;
    el.dataset["bound" + key] = "1";
    return true;
  }

  function setup(root) {
    const find = root.querySelector("[data-find]");
    const lists = root.querySelectorAll("[data-list]");
    const empty = root.querySelector("[data-empty]");
    const toggles = root.querySelectorAll("[data-show]");

    function visibleList() {
      return [...lists].find((l) => !l.hidden);
    }

    function filterSidebar() {
      if (!find) return;
      const term = find.value.trim().toLowerCase();
      lists.forEach((list) => {
        list.querySelectorAll("li").forEach((li) => {
          li.hidden = term !== "" && !li.dataset.name.toLowerCase().includes(term);
        });
      });
      const list = visibleList();
      const any = list && [...list.querySelectorAll("li")].some((li) => !li.hidden);
      if (empty) {
        empty.hidden = any || term === "";
        empty.querySelector("[data-term]").textContent = find.value.trim();
      }
    }

    toggles.forEach((btn) => {
      if (!once(btn, "Toggle")) return;
      btn.addEventListener("click", () => {
        toggles.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        lists.forEach((l) => (l.hidden = l.dataset.list !== btn.dataset.show));
        filterSidebar();
      });
    });
    if (once(find, "Find")) {
      find.addEventListener("input", filterSidebar);
      find.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const first = visibleList()?.querySelector("li:not([hidden]) a");
        if (first) first.click();
      });
    }
    const clear = root.querySelector("[data-clear]");
    if (once(clear, "Clear")) clear.addEventListener("click", () => {
      find.value = "";
      filterSidebar();
      find.focus();
    });

    const varfilter = root.querySelector("[data-varfilter]");
    const varempty = root.querySelector("[data-varempty]");
    function filterVars() {
      const term = varfilter.value.trim().toLowerCase();
      let shown = 0;
      root.querySelectorAll("tr[data-var]").forEach((tr) => {
        const hit = term === "" || tr.dataset.var.toLowerCase().includes(term);
        tr.hidden = !hit;
        if (hit) shown++;
      });
      if (varempty) {
        varempty.hidden = shown > 0;
        varempty.querySelector("[data-varterm]").textContent = varfilter.value.trim();
      }
    }
    if (once(varfilter, "Filter")) varfilter.addEventListener("input", filterVars);
    const varclear = root.querySelector("[data-varclear]");
    if (once(varclear, "Clear")) varclear.addEventListener("click", () => {
      varfilter.value = "";
      filterVars();
      varfilter.focus();
    });

    const factfilter = root.querySelector("[data-factfilter]");
    if (once(factfilter, "Filter")) factfilter.addEventListener("input", () => {
      const term = factfilter.value.trim().toLowerCase();
      let shown = 0;
      root.querySelectorAll("tr[data-fact]").forEach((tr) => {
        const hit = term === "" || tr.dataset.fact.toLowerCase().includes(term);
        tr.hidden = !hit;
        if (hit) shown++;
      });
      const empty = root.querySelector("[data-factempty]");
      if (empty) empty.hidden = shown > 0;
    });

    root.querySelectorAll("form").forEach((form) => {
      if (!once(form, "Busy")) return;
      form.addEventListener("submit", () => {
        const btn = form.querySelector("[data-busy-label]");
        if (!btn) return;
        btn.setAttribute("aria-busy", "true");
        btn.textContent = btn.dataset.busyLabel;
      });
    });

    // Cancel needs a second click within 4s: interrupting a play can leave hosts half-done.
    // The button is looked up on each submit: the command form's preview redraws it, and only a button
    // with a data-confirm-label asks twice.
    root.querySelectorAll("form[data-confirm-twice]").forEach((form) => {
      if (!once(form, "Confirm")) return;
      form.addEventListener("submit", (e) => {
        const btn = form.querySelector("button[data-confirm-label]");
        if (!btn || btn.dataset.armed) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        btn.dataset.armed = "1";
        btn.dataset.original = btn.textContent;
        btn.textContent = btn.dataset.confirmLabel;
        setTimeout(() => {
          delete btn.dataset.armed;
          if (btn.dataset.original) btn.textContent = btn.dataset.original;
        }, 4000);
      }, true);
    });

    // Live log: append new lines; stay pinned to the bottom unless the reader scrolled up.
    const logEl = root.querySelector("#log");
    root.querySelectorAll("template[data-log-append]").forEach((t) => {
      if (logEl) {
        const pinned = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 24;
        logEl.append(t.content.textContent);
        if (pinned) logEl.scrollTop = logEl.scrollHeight;
      }
      t.remove();
    });

    // Tag suggestions come back with each preview.
    const tagOptions = root.querySelector("#tag-options");
    const tagList = root.querySelector("#tag-suggestions");
    if (tagOptions && tagList) tagList.replaceChildren(tagOptions.content.cloneNode(true));

    // Show/hide a password field.
    root.querySelectorAll("[data-reveal]").forEach((btn) => {
      if (!once(btn, "Reveal")) return;
      btn.addEventListener("click", () => {
        const input = document.getElementById(btn.dataset.reveal);
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", String(show));
      });
    });

    // "/" focuses the host search, like most dev tools.
    document.onkeydown = (e) => {
      if (e.key === "/" && find && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        find.focus();
      }
    };
  }

  document.addEventListener("DOMContentLoaded", () => {
    setup(document);
    const logEl = document.querySelector("#log[data-follow]");
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  });
  document.addEventListener("htmx:afterSettle", () => setup(document));

  // Rail width: one choice for every page, remembered in this browser.
  function railState() {
    const icon = document.documentElement.dataset.rail === "icon";
    document.querySelectorAll("[data-rail-toggle]").forEach((b) => {
      b.querySelector(".rail-label").textContent = icon ? "Expand" : "Collapse";
    });
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-rail-toggle]")) return;
    const icon = document.documentElement.dataset.rail !== "icon";
    if (icon) document.documentElement.dataset.rail = "icon";
    else delete document.documentElement.dataset.rail;
    try { localStorage.setItem("rail", icon ? "icon" : "full"); } catch (err) {}
    railState();
  });
  document.addEventListener("htmx:afterSettle", railState);
  document.addEventListener("DOMContentLoaded", railState);

  // Phone menu: a <details> that works without script; this adds Escape and tap-outside to close.
  document.addEventListener("keydown", (e) => {
    const open = document.querySelector("[data-menu][open]");
    if (e.key === "Escape" && open) {
      open.open = false;
      open.querySelector("summary").focus();
    }
  });
  document.addEventListener("pointerdown", (e) => {
    const open = document.querySelector("[data-menu][open]");
    if (open && !open.contains(e.target)) open.open = false;
  });
  // After a boosted page change, start keyboard and screen-reader users at the content, not the top.
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!e.detail.boosted) return;
    const main = document.getElementById("main");
    if (main && document.activeElement === document.body) main.focus({ preventScroll: true });
  });

  // A live poll that fails (server restarting, network gone) says so instead of freezing silently.
  function pollTrouble(e, lost) {
    const id = e.detail.elt && e.detail.elt.id;
    if (id !== "poller" && id !== "home-active") return;
    const banner = document.getElementById("poll-lost");
    if (banner) banner.hidden = !lost;
  }
  document.addEventListener("htmx:sendError", (e) => pollTrouble(e, true));
  document.addEventListener("htmx:responseError", (e) => pollTrouble(e, true));
  document.addEventListener("htmx:afterRequest", (e) => { if (e.detail.successful) pollTrouble(e, false); });
})();
