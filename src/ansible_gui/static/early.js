// Loaded before the stylesheet applies, without defer, so a collapsed nav rail doesn't flash open.
// A file rather than an inline script: the Content-Security-Policy allows scripts from /static only.
try { if (localStorage.getItem("rail") === "icon") document.documentElement.dataset.rail = "icon"; } catch (e) {}

// No field here is the person's own login: secrets typed here are for Ansible (become passwords, keys, vault
// passwords, variables), and sign-in goes through the identity provider. Password managers would offer to save
// or fill them, and 1Password ignores autocomplete="off", so every field opts out the way each one documents,
// as it is parsed or swapped in, before the manager looks at it.
(function () {
  var OPT_OUT = [["data-1p-ignore", ""], ["data-lpignore", "true"], ["data-bwignore", ""], ["data-form-type", "other"]];
  function mark(el) {
    if (el.hasAttribute("data-1p-ignore")) return;
    for (var i = 0; i < OPT_OUT.length; i++) el.setAttribute(OPT_OUT[i][0], OPT_OUT[i][1]);
  }
  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.matches("input, textarea, select")) mark(node);
    var found = node.querySelectorAll("input, textarea, select");
    for (var i = 0; i < found.length; i++) mark(found[i]);
  }
  new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      for (var j = 0; j < records[i].addedNodes.length; j++) scan(records[i].addedNodes[j]);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
