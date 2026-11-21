// Rendered dropdowns over native <select>s: a button showing the choice (and a second line from the option's
// data-desc), and a listbox of the options. The <select> stays in the form and is what's submitted; choosing
// sets it and fires its change event, so htmx and the rest of the page see an ordinary select.
// Touch screens keep the native picker: it's better there.
(function () {
  const coarse = window.matchMedia("(pointer: coarse)");
  let seq = 0;

  function body(opt, cls) {
    const wrap = document.createElement("span");
    wrap.className = cls;
    const title = document.createElement("span");
    title.className = "picker-title" + (opt && opt.closest("select").classList.contains("mono") ? " mono" : "");
    title.textContent = opt ? opt.textContent.trim() : "";
    wrap.append(title);
    if (opt && opt.dataset.desc) {
      const d = document.createElement("span");
      d.className = "picker-about";
      d.textContent = opt.dataset.desc;
      wrap.append(d);
    }
    return wrap;
  }

  function enhance(sel) {
    if (sel.dataset.enhanced || sel.multiple || coarse.matches) return;
    sel.dataset.enhanced = "1";
    const id = sel.id || `sel-${++seq}`;
    sel.id = id;
    const box = document.createElement("div");
    box.className = "picker picker-select" + (sel.classList.contains("input-small") ? " picker-small" : "");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "picker-current";
    btn.id = `${id}-btn`;
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (label) {
      label.htmlFor = btn.id;
      label.id = label.id || `${id}-label`;
    }
    const named = sel.getAttribute("aria-label");
    if (named) btn.setAttribute("aria-label", named);
    if (sel.getAttribute("aria-describedby")) btn.setAttribute("aria-describedby", sel.getAttribute("aria-describedby"));
    const list = document.createElement("ul");
    list.className = "picker-list";
    list.id = `${id}-list`;
    list.setAttribute("role", "listbox");
    list.tabIndex = -1;
    list.hidden = true;
    if (label) list.setAttribute("aria-labelledby", label.id);
    else if (named) list.setAttribute("aria-label", named);
    btn.setAttribute("aria-controls", list.id);

    function options() {
      return [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
    }
    function render() {
      list.replaceChildren();
      [...sel.children].forEach((node) => {
        const group = node.tagName === "OPTGROUP" ? node : null;
        if (group) {
          const head = document.createElement("li");
          head.className = "picker-group";
          head.setAttribute("role", "presentation");
          head.textContent = group.label;
          list.append(head);
        }
        (group ? [...group.children] : [node]).forEach((opt) => {
          const li = document.createElement("li");
          li.className = "picker-option";
          li.id = `${id}-opt-${opt.index}`;
          li.setAttribute("role", "option");
          li.dataset.index = opt.index;
          li.setAttribute("aria-selected", String(opt.selected));
          if (opt.disabled) li.setAttribute("aria-disabled", "true");
          li.append(body(opt, "picker-option-body"));
          list.append(li);
        });
      });
      btn.replaceChildren(body(sel.selectedOptions[0], "picker-current-body"));
      const chev = document.createElement("span");
      chev.className = "picker-chevron";
      chev.setAttribute("aria-hidden", "true");
      chev.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
      btn.append(chev);
    }
    function active(li) {
      options().forEach((o) => o.classList.toggle("is-active", o === li));
      if (li) {
        list.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      }
    }
    function open() {
      render();
      list.hidden = false;
      box.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      active(list.querySelector('[aria-selected="true"]:not([aria-disabled="true"])') || options()[0]);
      list.focus();
    }
    function close(focus) {
      if (list.hidden) return;
      list.hidden = true;
      box.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
      if (focus) btn.focus();
    }
    function choose(li) {
      if (!li || li.getAttribute("aria-disabled") === "true") return;
      const idx = Number(li.dataset.index);
      if (sel.selectedIndex !== idx) {
        sel.selectedIndex = idx;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      close(true);
      render();
    }

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      list.hidden ? open() : close(true);
    });
    btn.addEventListener("keydown", (e) => {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        open();
      }
    });
    let typed = "", typedAt = 0;
    list.addEventListener("keydown", (e) => {
      const all = options();
      const cur = list.querySelector(".is-active");
      const i = all.indexOf(cur);
      if (e.key === "ArrowDown") active(all[Math.min(i + 1, all.length - 1)]);
      else if (e.key === "ArrowUp") active(all[Math.max(i - 1, 0)]);
      else if (e.key === "Home") active(all[0]);
      else if (e.key === "End") active(all[all.length - 1]);
      else if (e.key === "Enter" || e.key === " ") choose(cur);
      else if (e.key === "Escape") close(true);
      else if (e.key === "Tab") { close(false); return; }
      else if (e.key.length === 1) {
        typed = (Date.now() - typedAt > 700 ? "" : typed) + e.key.toLowerCase();
        typedAt = Date.now();
        const hit = all.find((o) => o.textContent.trim().toLowerCase().startsWith(typed));
        if (hit) active(hit);
        return;
      } else return;
      e.preventDefault();
    });
    list.addEventListener("click", (e) => {
      e.preventDefault();  // inside a <label>, a click would also "click" the button and reopen the list
      choose(e.target.closest('[role="option"]'));
    });
    list.addEventListener("mousemove", (e) => {
      const li = e.target.closest('[role="option"]:not([aria-disabled="true"])');
      if (li && !li.classList.contains("is-active")) active(li);
    });
    box.closeList = () => close(false);
    sel.addEventListener("change", render);  // set by code, or by a reset

    sel.classList.add("visually-hidden");
    sel.tabIndex = -1;
    sel.setAttribute("aria-hidden", "true");
    sel.before(box);
    box.append(btn, list, sel);
    render();
  }

  // One listener for every dropdown: a click outside an open one closes it.
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".picker-select.is-open").forEach((b) => { if (!b.contains(e.target)) b.closeList(); });
  });

  function all(root) {
    (root || document).querySelectorAll("select.input").forEach(enhance);
  }
  document.addEventListener("DOMContentLoaded", () => all(document));
  document.addEventListener("htmx:afterSettle", (e) => all(e.target));
})();
