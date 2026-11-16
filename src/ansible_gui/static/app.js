// Small client behaviours: sidebar search and tabs, variable filter, busy buttons.
// Re-run after every htmx swap, since hx-boost replaces the body.
(function () {
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
      btn.addEventListener("click", () => {
        toggles.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        lists.forEach((l) => (l.hidden = l.dataset.list !== btn.dataset.show));
        filterSidebar();
      });
    });
    if (find) {
      find.addEventListener("input", filterSidebar);
      find.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const first = visibleList()?.querySelector("li:not([hidden]) a");
        if (first) first.click();
      });
    }
    root.querySelector("[data-clear]")?.addEventListener("click", () => {
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
    varfilter?.addEventListener("input", filterVars);
    root.querySelector("[data-varclear]")?.addEventListener("click", () => {
      varfilter.value = "";
      filterVars();
      varfilter.focus();
    });

    root.querySelectorAll("form").forEach((form) => {
      form.addEventListener("submit", () => {
        const btn = form.querySelector("[data-busy-label]");
        if (!btn) return;
        btn.setAttribute("aria-busy", "true");
        btn.textContent = btn.dataset.busyLabel;
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

  document.addEventListener("DOMContentLoaded", () => setup(document));
  document.addEventListener("htmx:afterSettle", () => setup(document));
})();
