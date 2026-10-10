// Load the next page while the finger or pointer is on its way, so the tap feels instant.
// Browsers that read speculation rules (Chrome, Edge) do this from the rules in base.html;
// this is the fallback for the others. Same-site links only; never a download, never a link
// marked data-no-prefetch or opening a new tab (a receipt, a file), each page at most once.
// Safari does not act on rel=prefetch, so on an iPhone this does nothing yet; it helps Firefox.
(function () {
  if (window.HTMLScriptElement && HTMLScriptElement.supports && HTMLScriptElement.supports("speculationrules")) return;
  var done = {};
  function wanted(a) {
    if (!a || !a.href || a.origin !== location.origin || a.hasAttribute("download") || a.hasAttribute("data-no-prefetch") || a.target === "_blank") return false;
    if (a.pathname.slice(-4) === ".csv" || a.href === location.href || (a.hash && a.pathname === location.pathname)) return false;
    return !done[a.href];
  }
  function prefetch(a) {
    done[a.href] = true;
    var l = document.createElement("link");
    l.rel = "prefetch";
    l.href = a.href;
    document.head.appendChild(l);
  }
  var timer = null;
  document.addEventListener("mouseover", function (e) {
    var a = e.target.closest && e.target.closest("a");
    if (!wanted(a)) return;
    clearTimeout(timer);
    timer = setTimeout(function () { prefetch(a); }, 65);
  }, { passive: true });
  document.addEventListener("mouseout", function () { clearTimeout(timer); }, { passive: true });
  document.addEventListener("touchstart", function (e) {
    var a = e.target.closest && e.target.closest("a");
    if (wanted(a)) prefetch(a);
  }, { passive: true });
})();
