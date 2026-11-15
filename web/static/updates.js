/* История выпусков: прогрессивный локальный поиск и управление details. */
(function () {
  "use strict";
  var root = document.querySelector("[data-updates-root]");
  if (!root) { return; }
  var input = document.getElementById("updates-filter");
  var items = root.querySelectorAll(".upd-release");
  var result = root.querySelector("[data-updates-results]");
  function filter() {
    var query = input ? input.value.trim().toLocaleLowerCase("ru") : "";
    var shown = 0;
    for (var i = 0; i < items.length; i++) {
      var hit = !query || items[i].textContent.toLocaleLowerCase("ru").indexOf(query) !== -1;
      items[i].hidden = !hit;
      if (hit) { shown++; }
      var details = items[i].querySelector("details");
      if (details && query && hit) { details.open = true; }
    }
    if (result) { result.textContent = query ? "Найдено выпусков: " + shown : ""; }
  }
  if (input) { input.addEventListener("input", filter); }
  root.addEventListener("click", function (event) {
    var button = event.target && event.target.closest
      ? event.target.closest("[data-updates-expand],[data-updates-collapse]") : null;
    if (!button) { return; }
    var open = button.hasAttribute("data-updates-expand");
    var details = root.querySelectorAll(".upd-release details");
    for (var i = 0; i < details.length; i++) { details[i].open = open; }
  });
}());
