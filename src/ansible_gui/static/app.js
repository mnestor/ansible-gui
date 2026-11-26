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

  // Whether a live run's page is kept at its foot (see the live log in setup). Only the reader's own scrolling
  // changes it: up, to the hosts or to Cancel, and the page stays put; back to the foot and it follows again.
  // It is not measured when lines arrive, since by then the page has already grown under the reader.
  let pageFollows = false;
  const pageToFoot = () => window.scrollTo(0, document.documentElement.scrollHeight);
  window.addEventListener("scroll", () => {
    pageFollows = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 24;
  }, { passive: true });

  const coarse = window.matchMedia("(pointer: coarse)");

  // The names in a comma list (tags).
  function listTerms(text) {
    return text.split(",").map((t) => t.trim()).filter(Boolean);
  }
  // The names in a host pattern, split as Ansible splits it: on commas if it has any; otherwise on the colons
  // outside brackets, unless the whole of it is one address (fe80::1), where the colons are the address's own.
  function patternTerms(text) {
    text = text.trim();
    const commas = text.includes(",");
    if (!commas && /^[0-9a-f]*(:[0-9a-f]*){2,}$/i.test(text)) return [text];
    const out = [];
    let depth = 0, term = "";
    for (const ch of text) {
      if (ch === "[") depth++;
      else if (ch === "]") depth = Math.max(0, depth - 1);
      if (ch === (commas ? "," : ":") && depth === 0) {
        out.push(term);
        term = "";
      } else term += ch;
    }
    out.push(term);
    return out.map((t) => t.trim()).filter(Boolean);
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

    // Variables. The filter box reads each row's name, value and file (data-search) across every table of
    // the page; a table's source chips narrow that table to the sources picked (none picked: all of them).
    // A source's heading goes when none of its rows is left, and the count says how many are showing.
    const varfilter = root.querySelector("[data-varfilter]");
    const varblocks = [...root.querySelectorAll("[data-vars]")];
    function filterVars() {
      const typed = varfilter ? varfilter.value.trim() : "";
      const term = typed.toLowerCase();
      varblocks.forEach((block) => {
        const picked = new Set([...block.querySelectorAll('[data-source-chip][aria-pressed="true"]')]
          .map((chip) => chip.dataset.sourceChip));
        const all = block.querySelector("[data-source-all]");
        if (all) all.setAttribute("aria-pressed", String(picked.size === 0));
        const rows = [...block.querySelectorAll("tr[data-var]")];
        let shown = 0;
        rows.forEach((tr) => {
          const hit = (term === "" || (tr.dataset.search || tr.dataset.var.toLowerCase()).includes(term))
            && (picked.size === 0 || picked.has(tr.dataset.source));
          tr.hidden = !hit;
          if (hit) shown++;
        });
        block.querySelectorAll("tbody[data-group]").forEach((group) => {
          group.hidden = !group.querySelector("tr[data-var]:not([hidden])");
        });
        const count = block.querySelector("[data-varcount]");
        if (count) {
          if (!count.dataset.all) count.dataset.all = count.textContent.trim().replace(/\s+/g, " ");
          count.textContent = shown === rows.length ? count.dataset.all
            : `${shown} of ${rows.length} variable${rows.length === 1 ? "" : "s"}`;
        }
        const empty = block.querySelector("[data-varempty]");
        if (empty) {
          empty.hidden = shown > 0 || rows.length === 0;
          empty.querySelector("[data-varterm]").textContent = typed;
          const narrowed = empty.querySelector("[data-varpicked]");
          if (narrowed) narrowed.hidden = picked.size === 0;
        }
      });
    }
    varblocks.forEach((block) => {
      if (!once(block, "Sources")) return;
      block.addEventListener("click", (e) => {
        const chip = e.target.closest("[data-source-chip], [data-source-all]");
        const clear = e.target.closest("[data-varclear]");
        if (!chip && !clear) return;
        if (chip && chip.hasAttribute("data-source-chip")) {
          chip.setAttribute("aria-pressed", String(chip.getAttribute("aria-pressed") !== "true"));
        } else {  // All, or Clear filter: back to every source
          block.querySelectorAll("[data-source-chip]").forEach((c) => c.setAttribute("aria-pressed", "false"));
          if (clear && varfilter) varfilter.value = "";
        }
        filterVars();
      });
    });
    if (once(varfilter, "Filter")) varfilter.addEventListener("input", filterVars);
    const varclear = root.querySelector("[data-varclear]");
    if (once(varclear, "Clear")) varclear.addEventListener("click", () => {
      varfilter.value = "";
      filterVars();
      varfilter.focus();
    });

    // A list searched as you type (the playbooks). A row matches on data-search, which holds its folded notes
    // too; when the match isn't in what the row shows (data-head), the notes unfold so it is on screen, and
    // fold again once the search moves on. Notes the reader opened themselves are left alone.
    const rowfilter = root.querySelector("[data-rowfilter]");
    const rowempty = root.querySelector("[data-rowempty]");
    const rowcount = root.querySelector("[data-rowcount]");
    const rowtotal = rowcount ? rowcount.textContent : "";  // "11 playbooks"
    function filterRows() {
      const term = rowfilter ? rowfilter.value.trim().toLowerCase() : "";
      // The chips narrow the list to the groups picked (repositories); none picked is all of them.
      const picked = new Set([...root.querySelectorAll('[data-rows-chip][aria-pressed="true"]')].map((c) => c.dataset.rowsChip));
      const every = root.querySelector("[data-rows-all]");
      if (every) every.setAttribute("aria-pressed", String(picked.size === 0));
      let shown = 0;
      root.querySelectorAll("tr[data-search]").forEach((tr) => {
        const hit = (term === "" || tr.dataset.search.includes(term)) && (picked.size === 0 || picked.has(tr.dataset.source));
        tr.hidden = !hit;
        if (hit) shown++;
        const notes = tr.querySelector("details.about-more");
        if (!notes) return;
        const unfold = term !== "" && hit && !(tr.dataset.head || "").includes(term);
        if (unfold && !notes.open) {
          notes.open = true;
          notes.dataset.byFilter = "1";
        } else if (!unfold && notes.dataset.byFilter) {
          notes.open = false;
          delete notes.dataset.byFilter;
        }
      });
      // A group's heading goes with the last of its rows.
      root.querySelectorAll("tbody[data-rowgroup]").forEach((group) => {
        group.hidden = !group.querySelector("tr[data-search]:not([hidden])");
      });
      if (rowempty) {
        rowempty.hidden = shown > 0;
        rowempty.querySelector("[data-rowterm]").textContent = rowfilter ? rowfilter.value.trim() : "";
      }
      if (rowcount) rowcount.textContent = term === "" && picked.size === 0 ? rowtotal : `${shown} of ${rowtotal}`;
    }
    if (once(rowfilter, "Filter")) rowfilter.addEventListener("input", filterRows);
    root.querySelectorAll("[data-rows-chip], [data-rows-all]").forEach((chip) => {
      if (!once(chip, "Rows")) return;
      chip.addEventListener("click", () => {
        if (chip.hasAttribute("data-rows-all")) {
          root.querySelectorAll("[data-rows-chip]").forEach((c) => c.setAttribute("aria-pressed", "false"));
        } else chip.setAttribute("aria-pressed", String(chip.getAttribute("aria-pressed") !== "true"));
        filterRows();
      });
    });
    const rowclear = root.querySelector("[data-rowclear]");
    if (once(rowclear, "Clear")) rowclear.addEventListener("click", () => {
      rowfilter.value = "";
      filterRows();
      rowfilter.focus();
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
    // A live run's page follows too (data-follow="page"): the box grows as lines arrive, so its foot, where
    // they land, would slide under the fold. Arriving, by the launcher's redirect or a link, starts at the foot:
    // a frame later, because htmx scrolls a boosted page to its top after this has run.
    const livePage = logEl && logEl.dataset.follow === "page";
    if (livePage && once(logEl, "Follow")) {
      logEl.scrollTop = logEl.scrollHeight;
      requestAnimationFrame(() => { pageFollows = true; pageToFoot(); });
    }
    root.querySelectorAll("template[data-log-append]").forEach((t) => {
      if (logEl) {
        const pinned = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 24;
        logEl.append(t.content);  // the lines as the server marked them up (ok, changed, failed…), not their text alone
        if (pinned) logEl.scrollTop = logEl.scrollHeight;
      }
      t.remove();
    });
    // Every poll, not only one with new lines: the hosts table above the log grows during a run as well.
    if (livePage && pageFollows) pageToFoot();

    // Tag suggestions come back with each preview.
    const tagOptions = root.querySelector("#tag-options");
    const tagList = root.querySelector("#tag-suggestions");
    if (tagOptions && tagList) tagList.replaceChildren(tagOptions.content.cloneNode(true));

    // Chip fields: Targets, Only tags, Skip tags and Environment secrets. One box styled as an input, holding a
    // chip per choice with an × that removes it, and an entry that offers what there is to choose in the app's
    // own list (suggest.js; the browser's on a touch screen). What the form sends stays what it was: the
    // original input, holding the comma list or the pattern, or the secrets' tick boxes. Without script those
    // are all there is.
    function chipField(o) {
      const box = document.createElement("div");
      box.className = "chip-field input input-medium" + (o.plain ? " chip-field-text" : "");
      const chips = document.createElement("ul");
      chips.className = "chips";
      const entry = document.createElement("input");
      entry.className = "chip-entry";
      entry.id = o.id;
      if (o.describedBy) entry.setAttribute("aria-describedby", o.describedBy);
      if (o.labelledBy) entry.setAttribute("aria-labelledby", o.labelledBy);
      if (o.list) {
        entry.setAttribute("list", o.list);
        entry.dataset.suggest = "";
        entry.dataset.suggestLabel = o.label;
        if (o.plain) entry.dataset.suggestPlain = "";
      }
      Object.assign(entry, { autocomplete: "off", spellcheck: false });
      entry.setAttribute("autocapitalize", "off");
      entry.setAttribute("enterkeyhint", "done");
      entry.suggestAnchor = box;  // the list hangs under the whole field, not under the entry
      entry.suggestExclude = () => o.chosen().map((c) => c.match || c.text);
      box.append(chips, entry);
      o.after.after(box);

      function draw() {
        const now = o.chosen();
        chips.replaceChildren(...now.map((c) => {
          const li = document.createElement("li");
          const chip = document.createElement("span");
          chip.className = "chip" + (o.plain ? "" : " mono");
          chip.textContent = c.text;
          if (c.about) {
            const about = document.createElement("span");
            about.className = "chip-about mono";
            about.textContent = c.about;
            chip.append(about);
          }
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "chip-remove";
          remove.textContent = "×";
          remove.setAttribute("aria-label", `Remove ${c.text}`);
          remove.addEventListener("click", () => {
            o.remove(c);
            draw();
            entry.focus();
          });
          chip.append(remove);
          li.append(chip);
          return li;
        }));
        entry.placeholder = now.length ? o.more : o.placeholder;
        if (o.required) entry.required = now.length === 0;  // the hidden original can't be told it is empty
      }
      function take(text) {
        entry.value = "";
        if (text.trim()) o.add(text);
        draw();
      }
      entry.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          if (entry.hasAttribute("aria-activedescendant")) return;  // an option of the list is picked out: Enter is its
          e.preventDefault();  // Enter in a chip field never starts a run
          if (entry.value.trim()) take(entry.value);
        } else if (o.splits && o.splits(e.key, entry.value) && entry.value.trim()) {
          e.preventDefault();
          take(entry.value);
        } else if (e.key === "Backspace" && !entry.value) {
          const now = o.chosen();
          if (!now.length) return;
          o.remove(now[now.length - 1]);
          draw();
        }
      });
      // A pick from the list arrives as a change (suggest.js), or from the browser's own list as an input
      // event with no inputType (Firefox) or a replacement.
      entry.addEventListener("input", (e) => {
        if (!e.inputType || e.inputType === "insertReplacementText") take(entry.value);
      });
      entry.addEventListener("change", (e) => {
        e.stopPropagation();  // the form hears of a chip, not of what is being typed
        if (entry.value.trim()) take(entry.value);
      });
      entry.addEventListener("blur", () => { if (entry.value.trim()) take(entry.value); });
      box.addEventListener("click", (e) => {
        if (e.target !== box && e.target !== chips) return;
        entry.focus();
        entry.click();  // opens the list, as a click on the entry does
      });
      draw();
      if (window.agSuggest) window.agSuggest(entry, box);
    }

    // Targets, and the two tag fields: the original input keeps the text the form sends.
    root.querySelectorAll("input[data-chip-field]").forEach((input) => {
      if (!once(input, "Chips")) return;
      const pattern = input.dataset.chipField === "pattern";
      const read = pattern ? patternTerms : listTerms;
      // A pattern is joined the way it came (web:!web2, or web,!web2), a new one with colons; with commas
      // once a name has a colon of its own (a slice, an IPv6 address).
      const joint = pattern && !input.value.includes(",") ? ":" : ",";
      let terms = read(input.value);
      // Tags: the field beside it holds the tags to skip. They are drawn here as !name, like a host left out
      // of Targets, and that field is put away; each still gets its own names when anything changes.
      const skips = input.dataset.chipSkip ? document.getElementById(input.dataset.chipSkip) : null;
      const bare = (t) => t.replace(/^[!\s]+/, "");
      if (skips) {
        terms = terms.concat(listTerms(skips.value).map((t) => "!" + t));
        const field = skips.closest(".field");
        if (field) field.hidden = true;
      }
      const id = input.id;
      const list = input.getAttribute("list");
      const required = input.required;
      input.id = id + "-value";
      input.removeAttribute("list");
      input.required = false;
      input.hidden = true;  // hidden, still submitted
      function changed() {
        if (skips) {
          input.value = terms.filter((t) => !t.startsWith("!")).join(",");
          skips.value = terms.filter((t) => t.startsWith("!")).map(bare).join(",");
        } else input.value = terms.join(pattern && terms.some((t) => t.includes(":")) ? "," : joint);
        // The preview listens for change on the form: it refreshes per chip, not per keystroke.
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
      chipField({
        after: input, id, list, required, describedBy: input.getAttribute("aria-describedby"),
        label: pattern ? "Groups and hosts" : "Tags",
        placeholder: input.placeholder || (pattern ? "Type or pick a group or host" : "Type or pick a tag"),
        more: pattern ? "Add a group or host" : (skips ? "Add a tag, or !tag to skip one" : "Add a tag"),
        chosen: () => terms.map((t) => ({ text: t, match: t.replace(/^[!&]+/, "") })),
        add(text) {
          let typed = read(text);
          if (skips) typed = typed.map((t) => (t.startsWith("!") ? "!" + bare(t) : t)).filter((t) => bare(t));
          const fresh = typed.filter((t) => !terms.includes(t));
          if (!fresh.length) return;
          // A tag is run or skipped, not both: the one just added takes the place of its opposite.
          if (skips) terms = terms.filter((t) => !fresh.some((f) => bare(f) === bare(t)));
          terms.push(...fresh);
          changed();
        },
        remove(c) {
          terms = terms.filter((t) => t !== c.text);
          changed();
        },
        // A comma ends a name; so does a colon in a pattern, unless it is inside a slice: web[0:2].
        splits: (key, typed) => key === "," || (pattern && key === ":" && !/\[[^\]]*$/.test(typed)),
      });
    });

    // Environment secrets: the tick boxes stay what the form sends, and stay the control on a touch screen.
    root.querySelectorAll("fieldset[data-chip-choices]").forEach((set) => {
      const holder = set.querySelector(".choices");
      if (coarse.matches || !holder || !once(set, "Chips")) return;
      const boxes = [...holder.querySelectorAll('input[type="checkbox"]')];
      const legend = set.querySelector("legend");
      const tick = (box, on) => {
        box.checked = on;
        box.dispatchEvent(new Event("change", { bubbles: true }));
      };
      holder.hidden = true;
      chipField({
        after: holder, id: set.dataset.chipChoices, list: set.dataset.list, plain: true,
        labelledBy: legend && legend.id, describedBy: set.getAttribute("aria-describedby"),
        label: legend ? legend.textContent.trim() : "Choices",
        placeholder: set.dataset.placeholder || "Pick one", more: set.dataset.more || "Add another",
        chosen: () => boxes.filter((b) => b.checked).map((b) => ({ text: b.dataset.name, about: b.dataset.about, box: b })),
        add(text) {
          const box = boxes.find((b) => b.dataset.name.toLowerCase() === text.trim().toLowerCase());
          if (box && !box.checked) tick(box, true);  // only what is on the list: there is nothing to type in
        },
        remove: (c) => tick(c.box, false),
      });
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
    // The notes under the field are the chosen playbook's.
    (e.target.form || document).querySelectorAll("[data-notes-for]").forEach((n) => {
      n.hidden = n.dataset.notesFor !== e.target.value;
    });
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
