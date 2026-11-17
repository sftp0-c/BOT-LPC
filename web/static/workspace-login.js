"use strict";
(function () {
  var form = document.querySelector("form[data-staff-login]");
  if (!form) { return; }

  var input = document.getElementById("workspace-staff");
  var token = document.getElementById("workspace-staff-token");
  var list = document.getElementById("workspace-staff-results");
  var status = document.getElementById("staff-status");
  var submit = document.getElementById("workspace-request-code");
  var csrf = form.querySelector('input[name="csrf"]');
  var timer = null, requestNo = 0, active = -1, options = [];

  function closeList() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function clearList() {
    while (list.firstChild) { list.removeChild(list.firstChild); }
    options = [];
    closeList();
  }

  function clearChoice() {
    token.value = "";
    submit.disabled = true;
    input.removeAttribute("data-selected");
  }

  function choose(index) {
    if (index < 0 || index >= options.length) { return; }
    var option = options[index];
    input.value = option.getAttribute("data-name") || "";
    token.value = option.getAttribute("data-token") || "";
    input.setAttribute("data-selected", "true");
    submit.disabled = !token.value;
    status.textContent = "Выбран сотрудник: " + input.value + ".";
    clearList();
  }

  function paintActive(index) {
    if (!options.length) { return; }
    active = Math.max(0, Math.min(options.length - 1, index));
    for (var i = 0; i < options.length; i++) {
      options[i].classList.toggle("is-active", i === active);
      options[i].setAttribute("aria-selected", i === active ? "true" : "false");
    }
    input.setAttribute("aria-activedescendant", options[active].id);
    options[active].scrollIntoView({block: "nearest"});
  }

  function resultButton(item, index) {
    var button = document.createElement("button");
    var name = document.createElement("strong");
    var position = document.createElement("span");
    button.type = "button";
    button.id = "workspace-staff-option-" + index;
    button.className = "ws-staff-option";
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", "false");
    button.setAttribute("data-name", String(item.name || ""));
    button.setAttribute("data-token", String(item.token || ""));
    name.textContent = String(item.name || "");
    position.textContent = String(item.position || "Должность не указана");
    button.appendChild(name);
    button.appendChild(position);
    button.addEventListener("pointerdown", function (event) { event.preventDefault(); });
    button.addEventListener("click", function () { choose(options.indexOf(button)); });
    return button;
  }

  function showResults(payload) {
    clearList();
    var results = payload && Array.isArray(payload.results) ? payload.results : [];
    for (var i = 0; i < results.length; i++) {
      if (!results[i] || !results[i].name || !results[i].token) { continue; }
      var button = resultButton(results[i], options.length);
      options.push(button);
      list.appendChild(button);
    }
    if (options.length) {
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      status.textContent = "Найдено вариантов: " + options.length + ". Выберите себя из списка.";
    } else {
      status.textContent = payload && payload.message ? String(payload.message) : "Совпадений не найдено.";
    }
  }

  function search(value) {
    var serial = ++requestNo;
    var body = new URLSearchParams();
    body.set("q", value);
    body.set("csrf", csrf ? csrf.value : "");
    status.textContent = "Ищем сотрудника…";
    fetch("/workspace/login/staff-search", {
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "application/json"
      },
      body: body
    }).then(function (response) {
      return response.json().then(function (payload) { return {ok: response.ok, payload: payload}; });
    }).then(function (result) {
      if (serial !== requestNo || input.value.trim() !== value) { return; }
      if (!result.ok && !result.payload.message) { throw new Error("search"); }
      showResults(result.payload);
    }).catch(function () {
      if (serial !== requestNo || input.value.trim() !== value) { return; }
      clearList();
      status.textContent = "Поиск временно недоступен. Попробуйте позже.";
    });
  }

  input.addEventListener("input", function () {
    clearTimeout(timer);
    requestNo++;
    clearChoice();
    clearList();
    var value = input.value.trim();
    if (value.length < 3) {
      status.textContent = "Введите не менее трёх букв.";
      return;
    }
    timer = setTimeout(function () { search(value); }, 220);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      if (options.length) { paintActive(active + 1); event.preventDefault(); }
    } else if (event.key === "ArrowUp") {
      if (options.length) { paintActive(active < 0 ? options.length - 1 : active - 1); event.preventDefault(); }
    } else if (event.key === "Enter") {
      if (!list.hidden && active >= 0) { choose(active); event.preventDefault(); }
    } else if (event.key === "Escape") {
      clearList();
    }
  });

  input.addEventListener("blur", function () {
    window.setTimeout(closeList, 120);
  });

  form.addEventListener("submit", function (event) {
    if (!token.value || input.getAttribute("data-selected") !== "true") {
      event.preventDefault();
      clearChoice();
      status.textContent = "Сначала выберите себя из выпадающего списка.";
      input.setAttribute("aria-invalid", "true");
    } else {
      input.removeAttribute("aria-invalid");
      submit.disabled = true;
    }
  });
}());
