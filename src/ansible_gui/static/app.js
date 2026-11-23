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
        list.querySelectorAll("[data-name]").forEach((li) => {
          li.hidden = term !== "" && !li.dataset.name.toLowerCase().includes(term);
        });
      });
      const list = visibleList();
      const any = list && [...list.querySelectorAll("[data-name]")].some((li) => !li.hidden);
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
        const first = visibleList()?.querySelector("[data-name]:not([hidden]) a");
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
        btn.dataset.original = btn.innerHTML;
        btn.textContent = btn.dataset.confirmLabel;
        setTimeout(() => {
          delete btn.dataset.armed;
          if (btn.dataset.original) btn.innerHTML = btn.dataset.original;
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

    // Tag fields: a chip per tag, and a box that suggests the playbook's tags one at a time (a datalist on
    // the comma list itself only completes the first). The comma list stays in the original input, which is
    // what the form sends; without script that input is all there is.
    root.querySelectorAll("input[data-tag-field]").forEach((input) => {
      if (!once(input, "Tags")) return;
      const box = document.createElement("div");
      box.className = "tag-field input input-medium";
      const chips = document.createElement("ul");
      chips.className = "chips";
      const entry = document.createElement("input");
      entry.className = "tag-entry";
      entry.id = input.id;
      input.id = input.id + "-value";
      for (const attr of ["list", "aria-describedby"]) {
        if (input.hasAttribute(attr)) entry.setAttribute(attr, input.getAttribute(attr));
      }
      input.removeAttribute("list");
      Object.assign(entry, { autocomplete: "off", spellcheck: false, placeholder: "Type or pick a tag" });
      entry.setAttribute("autocapitalize", "off");
      entry.setAttribute("enterkeyhint", "done");
      input.hidden = true; // hidden, still submitted
      box.append(chips, entry);
      input.after(box);

      let tags = input.value.split(",").map((t) => t.trim()).filter(Boolean);
      function sync(changed) {
        chips.replaceChildren(...tags.map((t) => {
          const li = document.createElement("li");
          const chip = document.createElement("span");
          chip.className = "chip mono";
          chip.textContent = t;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "chip-remove";
          remove.textContent = "×";
          remove.setAttribute("aria-label", `Remove ${t}`);
          remove.addEventListener("click", () => {
            tags = tags.filter((x) => x !== t);
            sync(true);
            entry.focus();
          });
          chip.append(remove);
          li.append(chip);
          return li;
        }));
        input.value = tags.join(",");
        // The preview listens for change on the form: it refreshes per tag, not per keystroke.
        if (changed) input.dispatchEvent(new Event("change", { bubbles: true }));
      }
      function add(text) {
        for (const t of text.split(",").map((s) => s.trim()).filter(Boolean)) if (!tags.includes(t)) tags.push(t);
        entry.value = "";
        sync(true);
      }
      entry.addEventListener("keydown", (e) => {
        if ((e.key === "Enter" || e.key === ",") && entry.value.trim()) {
          e.preventDefault();
          add(entry.value);
        } else if (e.key === "Enter") {
          e.preventDefault(); // Enter in the tag box never starts a run
        } else if (e.key === "Backspace" && !entry.value && tags.length) {
          tags.pop();
          sync(true);
        }
      });
      // A pick from the suggestions arrives as an input event with no inputType (Firefox) or a replacement.
      entry.addEventListener("input", (e) => {
        if (!e.inputType || e.inputType === "insertReplacementText" || entry.value.includes(",")) add(entry.value);
      });
      entry.addEventListener("change", (e) => e.stopPropagation());
      entry.addEventListener("blur", () => { if (entry.value.trim()) add(entry.value); });
      box.addEventListener("click", (e) => { if (e.target === box || e.target === chips) entry.focus(); });
      sync(false);
    });

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

  // htmx validates a form before any request from it, GETs included, and halts silently on an empty required
  // field: a new schedule's or template's empty Name froze the host preview. The preview only reads the form;
  // Save is still validated by the browser.
  document.addEventListener("htmx:configRequest", (e) => {
    if (e.detail.verb === "get" && e.detail.elt.matches?.("form.launcher")) e.detail.errors = [];
  });

  // The Variables section is redrawn only when the playbook or its Fields/YAML view changes: a redraw on every change would close
  // "more optional variables" while someone fills it in.
  // Names that must be capitals (environment variables): typed in any case, shown as capitals at once, caret kept.
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!el.matches || !el.matches("input[data-upper]")) return;
    const up = el.value.toUpperCase().replace(/[\s-]/g, "_");
    if (up === el.value) return;
    const at = el.selectionStart;
    el.value = up;
    el.setSelectionRange(at, at);
  });

  // Playbook picker: choosing closes the list and shows the choice; Escape or a click elsewhere closes it.
  document.addEventListener("change", (e) => {
    const picker = e.target.closest && e.target.closest("[data-picker]");
    if (!picker || e.target.name !== "playbook") return;
    const body = e.target.closest(".picker-option").querySelector(".picker-option-body");
    picker.querySelector("[data-picker-current]").innerHTML = body.innerHTML;
    picker.open = false;
    picker.querySelector("summary").focus();
  });
  document.addEventListener("keydown", (e) => {
    const picker = e.key === "Escape" && e.target.closest && e.target.closest("[data-picker][open]");
    if (picker && picker.querySelector("input:checked")) { picker.open = false; picker.querySelector("summary").focus(); }
  });
  document.addEventListener("click", (e) => {
    document.querySelectorAll("[data-picker][open]").forEach((p) => {
      if (!p.contains(e.target) && p.querySelector("input:checked")) p.open = false;
    });
  });

  document.addEventListener("change", (e) => {
    if (e.target.name !== "playbook" && e.target.name !== "vars_mode") return;
    const vars = e.target.form && e.target.form.querySelector("#run-vars");
    if (vars && window.htmx) htmx.trigger(vars, "playbook-changed");
  });

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
    if (id !== "poller" && id !== "home-active" && id !== "repos-pulling") return;
    const banner = document.getElementById("poll-lost");
    if (banner) banner.hidden = !lost;
  }
  document.addEventListener("htmx:sendError", (e) => pollTrouble(e, true));
  document.addEventListener("htmx:responseError", (e) => pollTrouble(e, true));
  document.addEventListener("htmx:afterRequest", (e) => { if (e.detail.successful) pollTrouble(e, false); });
})();
