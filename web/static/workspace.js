/* Прогрессивные улучшения: навигация и отправка остаются обычными HTML-формами. */
(function () {
  "use strict";
  var root = document.querySelector("[data-workspace]");
  if (!root || root.dataset.wsReady === "1") { return; }
  root.dataset.wsReady = "1";
  var user = root.dataset.userId || "";
  var announcer = root.querySelector("[data-ws-announcer]");
  var forms = Array.from(root.querySelectorAll("[data-ws-action-form]"));
  var modes = new Set(["reply", "note", "forward", "status"]);
  var actions = new Set(["reply", "reply_close", "note", "forward", "status", "template_preview"]);
  var navigating = false;
  var states = [];

  function announce(message, state) {
    var output = state && state.form.querySelector("[data-ws-draft-status]");
    if (output && output.textContent !== message) { output.textContent = message; }
    if (announcer && announcer.textContent !== message) { announcer.textContent = message; }
  }

  root.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.classList || !form.classList.contains("ws-bulk-admin")) { return; }
    var target = form.querySelector('input[name="tids"]');
    var checked = Array.from(root.querySelectorAll('input[form="ws-bulk-form"][name="tids"]:checked'));
    if (target) { target.value = checked.map(function (box) { return box.value; }).join(","); }
  }, true);

  root.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target.closest("[data-ws-copy], [data-ws-print]") : null;
    if (!target) { return; }
    if (target.hasAttribute("data-ws-print")) { window.print(); return; }
    var value = target.getAttribute("data-ws-copy") || "";
    if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
      window.navigator.clipboard.writeText(value).then(function () { announce("Ссылка скопирована."); },
        function () { announce("Браузер не дал скопировать. Используйте адрес этой карточки."); });
    } else { announce("Используйте адрес этой карточки в строке браузера."); }
  });

  function refreshAges() {
    root.querySelectorAll(".live-age[data-time-ms]").forEach(function (timer) {
      var stamp = Number(timer.getAttribute("data-time-ms"));
      if (!Number.isFinite(stamp)) { return; }
      var minutes = Math.max(0, Math.floor((Date.now() - stamp) / 60000));
      var age = minutes < 1 ? "меньше минуты" : minutes < 60 ? minutes + " мин" :
        minutes < 1440 ? Math.floor(minutes / 60) + " ч " + minutes % 60 + " мин" :
        Math.floor(minutes / 1440) + " дн " + Math.floor(minutes % 1440 / 60) + " ч";
      var prefix = timer.getAttribute("data-prefix") || "";
      timer.textContent = prefix ? prefix + " · " + age : age;
    });
  }
  refreshAges();
  if (root.querySelector(".live-age[data-time-ms]")) { window.setInterval(refreshAges, 60000); }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { refreshAges(); } });

  function draftKey(ticket, mode) {
    return "ws-draft:v2:" + [root.dataset.basePath || "", user, ticket, mode].map(encodeURIComponent).join(":");
  }

  function readDraft(key) {
    try {
      var raw = window.sessionStorage.getItem(key);
      if (!raw) { return null; }
      var saved = JSON.parse(raw);
      if (!saved || saved.v !== 1 || typeof saved.text !== "string") { return null; }
      return saved;
    } catch (error) { return null; }
  }

  function writeDraft(key, value) {
    try {
      window.sessionStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (error) { return false; }
  }

  function removeDraft(key) {
    try { window.sessionStorage.removeItem(key); } catch (error) { /* Приватный режим допустим. */ }
  }

  function confirmed(saved, pending) {
    return Boolean(saved && saved.confirmed && pending &&
      saved.confirmed.operation === pending.operation && saved.confirmed.action === pending.action);
  }

  /* Успех берётся исключительно из SSR-маркера после PRG, не из query и не из submit. */
  var successfulAction = root.dataset.successAction || "";
  var successfulTicket = root.dataset.successTicket || "";
  var successfulOperation = root.dataset.successOperation || "";
  var successfulMode = successfulAction === "reply_close" ? "reply" : successfulAction;
  if (user && successfulTicket && successfulOperation && modes.has(successfulMode)) {
    var completedKey = draftKey(successfulTicket, successfulMode);
    var completed = readDraft(completedKey);
    if (completed && completed.pending && completed.pending.operation === successfulOperation &&
        completed.pending.action === successfulAction && completed.pending.text === completed.text) {
      // Пустая квитанция нужна для возврата назад из кэша: текст больше не восстанавливается.
      writeDraft(completedKey, {v: 1, text: "", confirmed: {operation: successfulOperation, action: successfulAction}});
    }
  }

  forms.forEach(function (form) {
    var mode = form.dataset.draftMode || "";
    var ticket = form.dataset.ticketId || root.dataset.ticketId || "";
    if (!modes.has(mode) || !ticket) { return; }
    var field = form.querySelector("[data-ws-draft-field]");
    var choice = form.querySelector('select[name="target_id"], select[name="status"]');
    var state = {
      form: form, field: field, choice: choice, initialChoice: choice ? choice.value : "",
      mode: mode, ticket: ticket, key: draftKey(ticket, mode), pending: null,
      lastSubmit: null, previewVersion: 0, controller: null
    };
    states.push(state);
    if (!field || !user) { return; }
    var saved = readDraft(state.key);
    // Серверный текст после ошибки/предпросмотра важнее старого браузерного черновика.
    if (!field.value && saved && saved.text) {
      field.value = saved.text;
      announce("Черновик восстановлен в этой вкладке. Он ещё не отправлен.", state);
    }
    if (saved && saved.pending && saved.text === field.value) { state.pending = saved.pending; }
    field.addEventListener("input", function () {
      navigating = false;
      form.removeAttribute("aria-busy");
      delete form.dataset.submitting;
      state.pending = null;
      save(state, true);
    });
    if (choice) {
      choice.addEventListener("change", function () { navigating = false; });
    }
    // Не меняем курсор и фокус при сохранении, восстановлении и вводе.
  });

  function save(state, notify) {
    if (!state.field || !user) { return; }
    var text = state.field.value;
    if (!text && !state.pending) {
      var previous = readDraft(state.key);
      if (!previous || !previous.confirmed) { removeDraft(state.key); }
      if (notify) { announce("Поле очищено.", state); }
      return;
    }
    var previous = readDraft(state.key);
    var ok = writeDraft(state.key, {v: 1, text: text, pending: state.pending,
      confirmed: previous && previous.confirmed ? previous.confirmed : null});
    if (notify) {
      announce(ok ? "Черновик сохранён только в этой вкладке." :
        "Браузер не разрешил хранить черновик. Не закрывайте страницу до отправки.", state);
    }
  }

  function saveAll() { states.forEach(function (state) { save(state, false); }); }

  function dirty(state) {
    return Boolean((state.field && state.field.value.trim()) ||
      (state.choice && state.choice.value !== state.initialChoice));
  }

  function unsaved(except) { return states.some(function (state) { return state !== except && dirty(state); }); }

  function permit(message) {
    if (!unsaved()) { return true; }
    return window.confirm(message || "Есть неотправленный текст или изменения. Покинуть обращение? Текст не будет отправлен.");
  }

  function allowNavigation() {
    saveAll();
    navigating = true;
    // Если другой обработчик отменил переход, защита не остаётся выключенной.
    window.setTimeout(function () { navigating = false; }, 1500);
  }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) { return; }
    var node = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!node || node.hasAttribute("download") || (node.target && node.target !== "_self")) { return; }
    var href = node.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#") { return; }
    var destination;
    try { destination = new URL(node.href, window.location.href); } catch (error) { return; }
    if (destination.protocol !== "http:" && destination.protocol !== "https:") { return; }
    if (destination.origin === window.location.origin && destination.pathname === window.location.pathname &&
        destination.search === window.location.search && destination.hash) { return; }
    if (!permit()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    allowNavigation();
  }, true);

  function findState(form) { return states.find(function (state) { return state.form === form; }); }

  async function preview(state) {
    var form = state.form;
    var select = form.querySelector("[data-ws-template]");
    var button = form.querySelector("[data-ws-template-preview]");
    if (!state.field || !select || !button || !select.value) {
      announce("Сначала выберите шаблон ответа.", state);
      return;
    }
    if (state.field.value.trim() && !window.confirm("Заменить набранный ответ текстом выбранного шаблона? Отправки пока не будет.")) { return; }
    var endpoint;
    try { endpoint = new URL(form.getAttribute("action"), window.location.href); } catch (error) { return; }
    if (endpoint.origin !== window.location.origin || endpoint.pathname !== root.dataset.basePath + "/" + state.ticket + "/action") {
      announce("Предпросмотр недоступен. Обновите страницу перед повторной попыткой.", state);
      return;
    }
    var csrf = form.querySelector('input[name="csrf"]');
    if (!csrf || !csrf.value) {
      announce("Срок формы истёк. Сохраните текст и обновите страницу.", state);
      return;
    }
    if (state.controller) { state.controller.abort(); }
    state.controller = typeof AbortController === "function" ? new AbortController() : null;
    var version = ++state.previewVersion;
    var before = state.field.value;
    var originalLabel = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    announce("Подставляем данные обращения в шаблон…", state);
    var body = new URLSearchParams();
    ["csrf", "operation_key", "revision", "return"].forEach(function (name) {
      var input = form.querySelector('input[name="' + name + '"]');
      if (input) { body.set(name, input.value); }
    });
    body.set("template_id", select.value);
    body.set("text", state.field.value);
    body.set("action", "template_preview");
    save(state, false);
    try {
      var response = await window.fetch(endpoint.href, {
        method: "POST", body: body, credentials: "same-origin", redirect: "error", cache: "no-store",
        headers: {Accept: "text/plain, application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        signal: state.controller ? state.controller.signal : undefined
      });
      if (version !== state.previewVersion) { return; }
      if (!response.ok) {
        if (response.status === 409) { throw new Error("Обращение изменилось. Ваш текст сохранён; обновите историю перед отправкой."); }
        if (response.status === 403) { throw new Error("Предпросмотр недоступен: проверьте вход и доступ к обращению. Текст не изменён."); }
        throw new Error("Не удалось подставить шаблон. Текст не изменён; попробуйте ещё раз.");
      }
      var contentType = (response.headers.get("Content-Type") || "").toLowerCase();
      var text;
      if (contentType.indexOf("application/json") === 0) {
        var payload = await response.json();
        text = payload && payload.text;
      } else if (contentType.indexOf("text/plain") === 0) {
        text = await response.text();
      } else {
        throw new Error("Сервер не вернул текст шаблона. Ответ оставлен без изменений.");
      }
      if (typeof text !== "string" || text.length > 100000) { throw new Error("Не удалось прочитать шаблон. Ответ не изменён."); }
      if (version !== state.previewVersion) { return; }
      if (state.field.value !== before) {
        announce("Во время загрузки вы изменили ответ. Текст сохранён; подставьте шаблон ещё раз, если нужно.", state);
        return;
      }
      if (state.field.maxLength > 0 && text.length > state.field.maxLength) {
        throw new Error("Не удалось прочитать шаблон. Ответ не изменён.");
      }
      state.field.value = text;
      state.pending = null;
      save(state, false);
      announce("Шаблон подставлен. Проверьте и при необходимости дополните ответ — он ещё не отправлен.", state);
    } catch (error) {
      if (version !== state.previewVersion || (error && error.name === "AbortError")) { return; }
      // Не показываем тело серверной ошибки и технические сведения ответа.
      var safeMessages = [
        "Обращение изменилось. Ваш текст сохранён; обновите историю перед отправкой.",
        "Предпросмотр недоступен: проверьте вход и доступ к обращению. Текст не изменён.",
        "Не удалось подставить шаблон. Текст не изменён; попробуйте ещё раз.",
        "Сервер не вернул текст шаблона. Ответ оставлен без изменений.",
        "Не удалось прочитать шаблон. Ответ не изменён."
      ];
      announce(error && safeMessages.indexOf(error.message) !== -1 ? error.message :
        "Нет связи для предпросмотра. Неотправленный текст оставлен в поле.", state);
    } finally {
      if (version === state.previewVersion) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = originalLabel;
      }
    }
  }

  root.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || !select.matches || !select.matches("[data-ws-template]")) { return; }
    var state = findState(select.form);
    if (state && window.fetch && typeof URLSearchParams === "function") { preview(state); }
  });

  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) { return; }
    var form = event.target;
    var state = findState(form);
    var button = event.submitter;
    var action = button && button.name === "action" ? button.value : "";
    if (state && actions.has(action)) {
      if (action === "template_preview") {
        if (window.fetch && typeof URLSearchParams === "function") {
          event.preventDefault();
          preview(state);
        } else {
          allowNavigation();
        }
        return;
      }
      if (form.dataset.submitting === "1") { event.preventDefault(); return; }
      if (unsaved(state) && !window.confirm("В другом поле остался неотправленный текст. Выполнить действие? Другие черновики не отправятся.")) {
        event.preventDefault();
        return;
      }
      if (state.controller) { state.controller.abort(); }
      ++state.previewVersion;
      var operation = form.querySelector('input[name="operation_key"]');
      if (state.field && operation && operation.value) {
        state.pending = {operation: operation.value, action: action, text: state.field.value};
        state.lastSubmit = state.pending;
      }
      // Не удаляем черновик и не выключаем submitter: его action нужен серверу.
      saveAll();
      form.dataset.submitting = "1";
      form.setAttribute("aria-busy", "true");
      navigating = true;
      window.setTimeout(function () {
        if (!document.hidden) {
          navigating = false;
          delete form.dataset.submitting;
          form.removeAttribute("aria-busy");
        }
      }, 15000);
      return;
    }
    // Сюда попадают поиск, фильтры, выход и формы оболочки сис-админа.
    if (!permit()) { event.preventDefault(); return; }
    allowNavigation();
  }, true);

  window.addEventListener("beforeunload", function (event) {
    saveAll();
    if (!navigating && unsaved()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", saveAll);
  window.addEventListener("pageshow", function (event) {
    navigating = false;
    states.forEach(function (state) {
      delete state.form.dataset.submitting;
      state.form.removeAttribute("aria-busy");
      // Возврат кнопкой «Назад» не должен оживлять уже подтверждённый ответ.
      var saved = readDraft(state.key);
      if (event.persisted && state.field && confirmed(saved, state.lastSubmit) && state.field.value === state.lastSubmit.text) {
        state.field.value = saved.text;
        state.pending = saved.pending || null;
        state.lastSubmit = null;
        announce(saved.text ? "Восстановлен новый неотправленный черновик." : "Этот текст уже отправлен. Поле ответа очищено.", state);
      }
    });
  });
}());
