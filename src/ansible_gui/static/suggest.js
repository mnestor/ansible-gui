// The app's own list of suggestions under a text box: the inventory's groups and hosts under Targets, the
// playbook's tags under Only tags and Skip tags, the saved secrets under Environment secrets. The browser's
// datalist can't say which name is a group and which a host, looks like no other dropdown here, and only ever
// matches the whole field: nothing for the second name of a pattern. This list has a heading for each kind, a
// second column for what a name is (a group's host count, a secret's variable), and follows the name being
// typed, replacing that name alone. The <datalist> stays in the page as the source of the names, read each time
// the list opens (the tags arrive with every preview), and as the list itself on touch screens, where the
// browser's own is the better one.
//
// In a chip field (app.js) the text box sits inside the field's box beside the chips: the list then hangs
// under that box, and leaves out what is already a chip.
(function () {
  const coarse = window.matchMedia("(pointer: coarse)");
  const HEADINGS = { group: "Groups", host: "Hosts" };
  let seq = 0;

  function enhance(input, anchor) {
    anchor = anchor || input.suggestAnchor;  // the chip field's box, when the input is its entry
    const data = document.getElementById(input.getAttribute("list") || "");
    if (input.dataset.enhanced || coarse.matches || !data) return;
    input.dataset.enhanced = "1";
    input.removeAttribute("list");  // or the browser's list opens over this one

    const id = `${input.id || "suggest-" + ++seq}-list`;
    const box = anchor || document.createElement("div");
    box.classList.add("picker", "picker-select", "picker-combo");
    if (!anchor && input.classList.contains("input-medium")) box.classList.add("picker-medium");
    const list = document.createElement("ul");
    list.className = "picker-list";
    list.id = id;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", input.dataset.suggestLabel || "Suggestions");
    list.hidden = true;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", id);
    if (anchor) anchor.append(list);
    else {
      input.before(box);
      box.append(input, list);
    }

    function names() {
      return [...data.options].map((o) => ({
        value: o.value, kind: o.dataset.kind || "",
        // What a name is: said by the option, or for a group how many hosts it reaches.
        about: o.dataset.about || (o.dataset.count ? `${o.dataset.count} host${o.dataset.count === "1" ? "" : "s"}` : ""),
      }));
    }
    // The name the caret is in: a pattern's names are set apart by : or , and may start with ! or &.
    function term() {
      const v = input.value;
      const at = input.selectionStart == null ? v.length : input.selectionStart;
      let start = Math.max(v.lastIndexOf(":", at - 1), v.lastIndexOf(",", at - 1)) + 1;
      while (start < v.length && "!& ".includes(v[start])) start++;
      let end = Math.max(at, start);
      while (end < v.length && v[end] !== ":" && v[end] !== ",") end++;
      return { start, end, text: v.slice(start, end).trim().toLowerCase() };
    }
    function options() {
      return [...list.querySelectorAll('[role="option"]')];
    }
    function active(li) {
      options().forEach((o) => o.classList.toggle("is-active", o === li));
      if (li) {
        input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }
    function close() {
      if (list.hidden) return;
      list.hidden = true;
      box.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    // The names that have what is typed in them, those that start with it first, under a heading per kind
    // that has one. What the field already holds as a chip isn't offered again.
    function open() {
      const q = term().text;
      const taken = new Set((input.suggestExclude ? input.suggestExclude() : []).map((v) => v.toLowerCase()));
      const rank = (n) => (n.value.toLowerCase().startsWith(q) ? 0 : 1);
      const found = names().filter((n) => n.value.toLowerCase().includes(q) && !taken.has(n.value.toLowerCase()));
      list.replaceChildren();
      for (const kind of new Set(found.map((n) => n.kind))) {
        const some = found.filter((n) => n.kind === kind).sort((a, b) => rank(a) - rank(b));
        if (HEADINGS[kind]) {
          const head = document.createElement("li");
          head.className = "picker-group";
          head.setAttribute("role", "presentation");
          head.textContent = HEADINGS[kind];
          list.append(head);
        }
        some.forEach((n) => {
          const li = document.createElement("li");
          li.className = "picker-option";
          li.id = `${id}-${list.children.length}`;
          li.setAttribute("role", "option");
          li.setAttribute("aria-selected", "false");
          li.dataset.value = n.value;
          const body = document.createElement("span");
          body.className = "picker-option-body";
          const title = document.createElement("span");
          title.className = "picker-title" + (input.dataset.suggestPlain === undefined ? " mono" : "");
          title.textContent = n.value;
          body.append(title);
          // A host says it is one to a screen reader, which is not read the headings.
          const about = n.about || (kind === "host" ? ", host" : "");
          if (about) {
            const said = document.createElement("span");
            said.className = n.about ? "picker-about" : "visually-hidden";
            said.textContent = about;
            body.append(said);
          }
          li.append(body);
          list.append(li);
        });
      }
      if (!found.length) return close();
      list.hidden = false;
      box.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      active(null);
    }
    // Put the chosen name where the one being typed is, and leave the rest of the pattern as it is.
    function choose(li) {
      if (!li) return;
      const t = term();
      input.value = input.value.slice(0, t.start) + li.dataset.value + input.value.slice(t.end);
      const caret = t.start + li.dataset.value.length;
      input.setSelectionRange(caret, caret);
      close();
      // The form's preview listens for it; a chip field takes it as its next chip.
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    input.addEventListener("input", open);
    input.addEventListener("click", open);
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (e) => {
      const all = options();
      const at = all.indexOf(list.querySelector(".is-active"));
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const cur = list.hidden ? -1 : at;  // opening it draws a new list, with nothing picked out yet
        if (list.hidden) open();
        const now = options();
        if (list.hidden || !now.length) return;
        active(now[e.key === "ArrowDown" ? Math.min(cur + 1, now.length - 1) : (cur < 0 ? now.length - 1 : Math.max(cur - 1, 0))]);
      } else if (e.key === "Enter" && !list.hidden && at >= 0) choose(all[at]);
      else if (e.key === "Escape" && !list.hidden) close();
      else {
        if (e.key === "Tab") close();
        return;
      }
      e.preventDefault();
      e.stopPropagation();
    });
    // mousedown, so the field keeps the focus and what was typed stays selected as it was.
    list.addEventListener("mousedown", (e) => e.preventDefault());
    list.addEventListener("click", (e) => choose(e.target.closest('[role="option"]')));
    list.addEventListener("mousemove", (e) => {
      const li = e.target.closest('[role="option"]');
      if (li && !li.classList.contains("is-active")) active(li);
    });
    box.closeList = close;  // select.js closes every open list on a click outside it
  }

  // A chip field's own input is left alone: app.js puts an entry in its place, and that is what gets the list.
  function all(root) {
    (root || document).querySelectorAll("input[data-suggest]:not([data-chip-field])").forEach((input) => enhance(input));
  }
  window.agSuggest = enhance;
  document.addEventListener("DOMContentLoaded", () => all(document));
  document.addEventListener("htmx:afterSettle", (e) => all(e.target));
})();
