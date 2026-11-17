// Loaded before the stylesheet applies, without defer, so a collapsed nav rail doesn't flash open.
// A file rather than an inline script: the Content-Security-Policy allows scripts from /static only.
try { if (localStorage.getItem("rail") === "icon") document.documentElement.dataset.rail = "icon"; } catch (e) {}
