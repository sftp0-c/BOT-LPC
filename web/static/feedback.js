/* Таймер только объясняет ожидание. Пауза и квоты всегда проверяются сервером. */
(function () {
  "use strict";
  var hint = document.querySelector("[data-feedback-wait]");
  if (!hint) { return; }
  var seconds = Number(hint.dataset.waitSeconds);
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 7200) { return; }
  var button = document.querySelector("[data-feedback-submit]");
  var deadline = Date.now() + seconds * 1000;
  var interval = null;
  function update() {
    var left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    if (button) { button.disabled = left > 0; }
    hint.textContent = left > 0 ? "Следующее сообщение можно отправить через " + left + " с." : "Пауза закончилась. Можно отправить новое сообщение.";
    if (!left) {
      hint.setAttribute("role", "status");
      if (interval !== null) { window.clearInterval(interval); interval = null; }
    }
  }
  update();
  interval = window.setInterval(update, 1000);
  window.addEventListener("pageshow", update);
  document.addEventListener("visibilitychange", update);
}());
