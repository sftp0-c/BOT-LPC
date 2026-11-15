/* theme boot */
(function(){try{var t=localStorage.getItem('panel-theme');
if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}
else if(window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches){
document.documentElement.setAttribute('data-theme','light');}}catch(e){}}());

/* theme toggle */
(function () {
  var KEY = "panel-theme";
  var root = document.documentElement;

  function read() {
    try { return localStorage.getItem(KEY) || ""; } catch (err) { return ""; }
  }
  function remember(theme) {
    try { localStorage.setItem(KEY, theme); } catch (err) {}
  }
  function systemLight() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
  }
  function current() {
    var chosen = root.getAttribute("data-theme");
    if (chosen === "light" || chosen === "dark") { return chosen; }
    return systemLight() ? "light" : "dark";
  }
  function paint() {
    var now = current();
    var buttons = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].innerHTML = (now === "dark" ? '<svg class="ico ico-moon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20.4 14.6A8.6 8.6 0 0 1 9.4 3.6a8.6 8.6 0 1 0 11 11Z"/></svg>' : '<svg class="ico ico-sun" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="4.1"/><path d="M12 2.8v2.4"/><path d="M12 18.8v2.4"/><path d="M2.8 12h2.4"/><path d="M18.8 12h2.4"/><path d="M5.5 5.5 7.3 7.3"/><path d="M16.7 16.7 18.5 18.5"/><path d="M18.5 5.5 16.7 7.3"/><path d="M7.3 16.7 5.5 18.5"/></svg>');
      buttons[i].setAttribute("aria-label",
          now === "dark" ? "Включить светлую тему" : "Включить тёмную тему");
      buttons[i].setAttribute("title",
          now === "dark" ? "Светлая тема" : "Тёмная тема");
    }
  }
  function apply(theme) {
    if (theme === "light" || theme === "dark") { root.setAttribute("data-theme", theme); }
    else { root.removeAttribute("data-theme"); }
    paint();
  }
  /* подсветка строк, в которых есть набранный текст */
  function markRows(box) {
    var needle = (box.value || "").trim().toLowerCase();
    var rows = document.querySelectorAll("main table tbody tr");
    for (var i = 0; i < rows.length; i++) {
      var hit = needle.length > 1 &&
          rows[i].textContent.toLowerCase().indexOf(needle) > -1;
      if (rows[i].classList.contains("is-found") !== hit) {
        rows[i].classList.toggle("is-found", hit);
      }
    }
    box.classList.toggle("is-filled", needle.length > 0);
  }
  function start() {
    apply(read());
    var boxes = document.querySelectorAll(".gsearch input");
    for (var i = 0; i < boxes.length; i++) {
      (function (box) {
        box.addEventListener("input", function () { markRows(box); });
      })(boxes[i]);
    }
    document.addEventListener("click", function (event) {
      var target = event.target;
      var button = target && target.closest ? target.closest(".theme-toggle") : null;
      if (!button) { return; }
      event.preventDefault();
      var next = current() === "dark" ? "light" : "dark";
      remember(next);
      apply(next);
    });
    /* слэш фокусирует строку поиска - как в браузере */
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/") { return; }
      var target = event.target || {};
      var tag = target.tagName || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || target.isContentEditable) { return; }
      var box = document.querySelector(".gsearch input");
      if (box) { event.preventDefault(); box.focus(); box.select(); }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
}());

/* toast */
window.panelToast = window.panelToast || function (text, kind) {
  var stack = document.querySelector(".toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toast-stack";
    stack.setAttribute("aria-live", kind === "bad" ? "assertive" : "polite");
    stack.setAttribute("aria-atomic", "true");
    document.body.appendChild(stack);
  }
  var found = null;
  for (var i = 0; i < stack.children.length; i++) {
    if (stack.children[i].getAttribute("data-toast") === text) { found = stack.children[i]; }
  }
  if (!found) {
    found = document.createElement("div");
    found.setAttribute("data-toast", text);
    found.className = "toast";
    stack.appendChild(found);
  }
  found.className = "toast" + (kind ? " toast-" + kind : "");
  found.setAttribute("role", kind === "bad" ? "alert" : "status");
  found.innerHTML = (kind === "bad" ? "/*ICON_WARN*/" : "/*ICON_OK*/") +
      '<span></span><button type="button" class="toast-close" aria-label="Закрыть">×</button>';
  found.querySelector("span").textContent = text;
  found.addEventListener("mouseenter", function () { clearTimeout(found._panelTimer); });
  found.addEventListener("focusin", function () { clearTimeout(found._panelTimer); });
  found.querySelector("button").addEventListener("click", function () {
    if (found.parentNode) { found.parentNode.removeChild(found); }
  });
  if (found._panelTimer) { clearTimeout(found._panelTimer); }
  found._panelTimer = setTimeout(function () {
    found.classList.add("is-out");
    setTimeout(function () { if (found.parentNode) { found.parentNode.removeChild(found); } },
        /*DELAY*/);
  }, 3200);
};
(function () {
  function showFlash() {
    var flash = document.querySelector("[data-flash]");
    if (!flash) { return; }
    var text = (flash.textContent || "").trim();
    if (text) { panelToast(text, flash.getAttribute("data-kind") || null); }
    flash.classList.add("msg-fallback-hidden");
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", showFlash); }
  else { showFlash(); }
}());

/* actions */
(function () {
  /* ── копирование значения: MAX ID, код группы, код приглашения, ссылка ── */
  function put(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "readonly");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(box);
      if (ok) { resolve(); } else { reject(new Error("copy")); }
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("[data-copy]") : null;
    if (!button) { return; }
    event.preventDefault();
    var note = button.getAttribute("data-copy-note") || "Скопировано";
    put(button.getAttribute("data-copy") || "").then(function () {
      button.classList.add("is-done");
      panelToast(note, "ok");
    }, function () {
      panelToast("Браузер не дал скопировать - выделите значение вручную", "bad");
    });
  });

  /* ── живой возраст обращения: обновляется без перезагрузки страницы ───── */
  function ageText(node) {
    var raw = node.getAttribute("data-time-ms");
    var ms = raw ? Number(raw) : Date.parse(node.getAttribute("data-time") || "");
    if (!isFinite(ms)) { return ""; }
    var minutes = Math.max(0, Math.floor((Date.now() - ms) / 60000));
    if (minutes < 1) { return "меньше минуты"; }
    if (minutes < 60) { return minutes + " мин"; }
    if (minutes < 1440) { return Math.floor(minutes / 60) + " ч " + (minutes % 60) + " мин"; }
    return Math.floor(minutes / 1440) + " дн " + Math.floor((minutes % 1440) / 60) + " ч";
  }
  function paintAges() {
    var timers = document.querySelectorAll(".live-age[data-time]");
    for (var i = 0; i < timers.length; i++) {
      var age = ageText(timers[i]);
      var prefix = timers[i].getAttribute("data-prefix") || "";
      if (age) { timers[i].textContent = prefix ? prefix + " · " + age : age; }
    }
  }
  paintAges();
  if (document.querySelector(".live-age")) { window.setInterval(paintAges, 60000); }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { paintAges(); } });

  /* ── шаблон ответа подставляется прямо в поле ─────────────────────────────
     Подстановку делает и сервер - она уходит студенту с подстановками <ФИО> и
     номера обращения, и это главный путь. Но сотрудник правит текст здесь, на
     странице, и ждать перезагрузки, чтобы увидеть выбранный шаблон, неудобно:
     список выбора сам себе ничего не делал. Поэтому текст шаблона (он лежит в
     data-text пункта списка) кладём в поле рядом и сразу ставим туда курсор.

     Написанное руками не затираем молча - спрашиваем. Поле помечается, что оно
     заполнено из шаблона, и тогда выбор другого шаблона не спрашивает заново;
     первая же правка руками пометку снимает, и вопрос возвращается. Пометка
     живёт в data-атрибутах, а не в классе: печать и тёмная тема её не видят,
     и в разметку ничего не добавляется. */
  var TEMPLATE_FIELD = "template";
  function replyField(select) {
    var owner = select.form || (select.closest ? select.closest("form") : null);
    return owner ? owner.querySelector('textarea[name="text"]') : null;
  }
  function applyTemplate(select) {
    var option = select.options[select.selectedIndex];
    var text = option ? (option.getAttribute("data-text") || "") : "";
    if (!text) { return; }
    var field = replyField(select);
    if (!field) { return; }
    var own = (field.value || "").trim();
    if (own && field.getAttribute("data-from-template") !== "1" &&
        !window.confirm("Заменить набранный ответ текстом шаблона «" +
                        option.textContent.trim() + "»?")) {
      return;
    }
    field.value = text;
    field.setAttribute("data-from-template", "1");
    field.setAttribute("data-template-text", text);
    field.focus();
    try { field.setSelectionRange(text.length, text.length); } catch (err) {}
  }
  /* Слушатель на документе, а не на сам список: форму ответа может
     перерисовать другой раздел, и тогда подписка на элемент молча отвалилась
     бы вместе с ним. */
  document.addEventListener("change", function (event) {
    var node = event.target;
    if (node && node.tagName === "SELECT" && node.getAttribute("name") === TEMPLATE_FIELD) {
      applyTemplate(node);
    }
  });
  document.addEventListener("input", function (event) {
    var node = event.target;
    if (node && node.tagName === "TEXTAREA" && node.getAttribute("data-from-template") &&
        node.value !== node.getAttribute("data-template-text")) {
      node.removeAttribute("data-from-template");
      node.removeAttribute("data-template-text");
    }
  });

  document.addEventListener("submit", function (event) {
    var target = event.target;
    if (target && target.getAttribute("action") === "/panel/logout") {
      try {
        Object.keys(localStorage).forEach(function (key) {
          if (key.indexOf("panel-filters:") === 0) { localStorage.removeItem(key); }
        });
      } catch (err) {}
    }
  });

  /* ── сохранённые фильтры рабочего места ───────────────────────────────── */
  var form = document.querySelector("[data-filters]");
  if (!form) { return; }
  var KEY = "panel-filters:" + (form.getAttribute("data-filter-key") || form.getAttribute("action") || "common");
  var names = (form.getAttribute("data-filters") || "status,category,q").split(",");
  function read() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (err) { saved = {}; }
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  }
  function write() {
    var data = {};
    for (var i = 0; i < names.length; i++) {
      var box = form.querySelector('[name="' + names[i] + '"]');
      if (box && box.value) { data[names[i]] = box.value; }
    }
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (err) {}
  }
  function apply(saved) {
    var changed = false;
    for (var i = 0; i < names.length; i++) {
      var box = form.querySelector('[name="' + names[i] + '"]');
      if (box && !box.value && typeof saved[names[i]] === "string" && saved[names[i]]) {
        box.value = saved[names[i]];
        if (box.value) { changed = true; }
      }
    }
    return changed;
  }
  /* восстанавливаем только когда в адресе фильтров не было: ссылка с фильтром
     должна побеждать, иначе «/?scope=waiting» открывался бы чужим фильтром */
  if (form.getAttribute("data-filters-saved") === "1" && !window.location.search) {
    if (apply(read())) { form.submit(); return; }
  }
  if (window.location.search) { write(); }
  form.addEventListener("change", write);
  form.addEventListener("submit", write);
  var reset = document.querySelector("[data-filters-reset]");
  if (reset) {
    reset.addEventListener("click", function () {
      try { localStorage.removeItem(KEY); } catch (err) {}
      panelToast("Фильтры сброшены", null);
    });
  }
}());

/* nav scroll */
(function () {
  var nav = document.querySelector(".app-nav");
  var user = document.body && document.body.dataset.panelUser;
  if (!nav || typeof user !== "string" || !user.trim()) { return; }
  var KEY = "panel-nav-scroll:" + user.trim();
  var positions = {};
  var mode = layoutMode();
  var geometry = null;
  var saveFrame = null;

  function layoutMode() { return window.innerWidth > 1000 ? "desktop" : "mobile"; }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function clamp(value, maximum) { return Math.min(Math.max(0, value), maximum); }
  function read() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(KEY) || "{}");
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) { return; }
      ["desktop", "mobile"].forEach(function (axis) {
        if (finite(saved[axis])) { positions[axis] = Math.max(0, saved[axis]); }
      });
    } catch (err) {}
  }
  function write() {
    try { sessionStorage.setItem(KEY, JSON.stringify(positions)); } catch (err) {}
  }
  function measure() {
    var vertical = mode === "desktop";
    var size = vertical ? nav.clientHeight : nav.clientWidth;
    var extent = vertical ? nav.scrollHeight : nav.scrollWidth;
    if (!finite(size) || !finite(extent) || size <= 0) { return null; }
    return {property: vertical ? "scrollTop" : "scrollLeft", size: size,
            maximum: Math.max(0, extent - size), vertical: vertical};
  }
  function remember() {
    /* CSS уже мог сменить ось и обнулить старую до события resize. */
    if (layoutMode() !== mode) { return; }
    var box = measure();
    if (!box || box.maximum <= 0 || !geometry ||
        box.size !== geometry.size || box.maximum !== geometry.maximum) { return; }
    var value = nav[box.property];
    if (finite(value)) { positions[mode] = clamp(value, box.maximum); }
  }
  function revealActive(box) {
    var active = nav.querySelector('a[aria-current="page"], a.on');
    if (!active) { return; }
    var frame = nav.getBoundingClientRect();
    var link = active.getBoundingClientRect();
    var start = box.vertical ? frame.top + nav.clientTop : frame.left + nav.clientLeft;
    var end = start + box.size;
    var first = box.vertical ? link.top : link.left;
    var last = box.vertical ? link.bottom : link.right;
    if (![start, end, first, last].every(finite)) { return; }
    var delta = 0;
    /* Ближайший край; пункт шире окна уже может занимать всё видимое меню. */
    if (first < start && last <= end) { delta = Math.max(first - start, last - end); }
    else if (last > end && first >= start) { delta = Math.min(first - start, last - end); }
    var value = nav[box.property] + delta;
    if (delta && finite(value)) { nav[box.property] = clamp(value, box.maximum); }
  }
  function restore() {
    mode = layoutMode();
    /* Измерение вызывает layout: позиция возвращается до первого кадра. */
    var box = measure();
    if (!box) { return; }
    geometry = box;
    if (finite(positions[mode])) {
      nav[box.property] = clamp(positions[mode], box.maximum);
    }
    revealActive(box);
    remember();
  }
  function flush() {
    if (layoutMode() !== mode) { restore(); }
    remember();
    if (saveFrame !== null) { window.cancelAnimationFrame(saveFrame); saveFrame = null; }
    write();
  }
  read();
  restore();
  nav.addEventListener("scroll", function () {
    if (layoutMode() !== mode) { return; }
    /* Число снимаем сразу: к следующему кадру другая ось может стать нулевой. */
    remember();
    if (saveFrame === null) {
      saveFrame = window.requestAnimationFrame(function () { saveFrame = null; write(); });
    }
  }, {passive: true});
  nav.addEventListener("click", flush, true);
  window.addEventListener("pagehide", flush);
  window.addEventListener("resize", restore);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { read(); restore(); }
  });
}());

/* hotkeys */
(function () {
  var SECTIONS = /*SECTIONS*/;
  var RESULT_ICONS = /*RESULT_ICONS*/;
  var HINT = /*HINT*/;

  function editing(target) {
    if (!target) { return false; }
    var tag = target.tagName || "";
    return /^(INPUT|TEXTAREA|SELECT|OPTION)$/.test(tag) || target.isContentEditable === true;
  }
  function clickable(target) {
    var tag = (target && target.tagName) || "";
    return /^(BUTTON|A|SUMMARY|LABEL)$/.test(tag);
  }
  function rows() { return document.querySelectorAll("[data-hk]"); }
  function quiet() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function mark(node) {
    var items = rows();
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle("hk-on", items[i] === node);
    }
    if (node && node.scrollIntoView) {
      try { node.scrollIntoView({block: "nearest", behavior: quiet() ? "auto" : "smooth"}); }
      catch (err) { node.scrollIntoView(false); }
    }
  }
  function picked() {
    var items = rows();
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { return items[i]; }
    }
    return null;
  }
  function move(step) {
    var items = rows();
    if (!items.length) { return; }
    var index = -1;
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { index = i; break; }
    }
    mark(items[Math.min(items.length - 1, Math.max(0, index + step))]);
  }
  function openRow(node) {
    if (!node) { return; }
    var link = node.querySelector("a[href]");
    var href = node.getAttribute("data-hk-open") || (link ? link.getAttribute("href") : "");
    if (href) { window.location.href = href; }
  }
  function send(action) {
    var form = document.querySelector('.wb-card form[action$="/' + action + '"]');
    if (form) { form.submit(); }
  }
  function focusField(name) {
    var field = document.querySelector('.wb-card [name="' + name + '"]');
    if (field) { field.focus(); if (field.select) { try { field.select(); } catch (err) {} } }
  }
  function help() {
    var parts = [];
    for (var i = 0; i < HINT.length; i++) { parts.push(HINT[i][0] + " — " + HINT[i][1]); }
    panelToast(parts.join(" · "), null);
  }

  var palette = null, list = null, search = null, chosen = 0, shown = [], ALL = [];
  var requestNo = 0, waitTimer = null, priorFocus = null;
  function collect(items, group, out, nested) {
    for (var i = 0; i < items.length; i++) {
      out.push([group, String(items[i][0]), String(items[i][1]),
                String(items[i][2] || ""), nested ? 1 : 0]);
      if (items[i][3] && items[i][3].length) { collect(items[i][3], group, out, true); }
    }
  }
  function build() {
    if (palette) { return; }
    palette = document.createElement("div");
    palette.className = "palette";
    palette.setAttribute("role", "dialog");
    palette.setAttribute("aria-modal", "true");
    palette.setAttribute("aria-label", "Поиск по панели");
    palette.innerHTML = '<div class="palette-box"><input type="text" placeholder="Раздел, обращение, человек или группа" ' +
        'autocomplete="off" aria-label="Поиск по панели"><ul class="palette-list"></ul>' +
        '<div class="palette-foot">↑ и ↓ — выбор, Enter — открыть, Esc — закрыть</div></div>';
    document.body.appendChild(palette);
    for (var g = 0; g < SECTIONS.length; g++) {
      if (SECTIONS[g] && SECTIONS[g].length) { collect(SECTIONS[g][1], String(SECTIONS[g][0]), ALL, false); }
    }
    search = palette.querySelector("input");
    list = palette.querySelector("ul");
    palette.addEventListener("click", function (event) { if (event.target === palette) { closePalette(); } });
    search.addEventListener("input", function () {
      requestNo++;
      paintSections(search.value);
      clearTimeout(waitTimer);
      var value = search.value.trim();
      if (value.length >= 2 || /^[0-9]+$/.test(value)) {
        waitTimer = setTimeout(function () { remoteSearch(value); }, 180);
      }
    });
    palette.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") { return; }
      var focusable = [search].concat(shown);
      var index = focusable.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) {
        event.preventDefault(); focusable[focusable.length - 1].focus();
      } else if (!event.shiftKey && index === focusable.length - 1) {
        event.preventDefault(); search.focus();
      }
    });
    search.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { closePalette(); return; }
      if (event.key === "ArrowDown") { choose(1); event.preventDefault(); return; }
      if (event.key === "ArrowUp") { choose(-1); event.preventDefault(); return; }
      if (event.key === "Enter") {
        event.preventDefault();
        if (shown[chosen]) { window.location.href = shown[chosen].getAttribute("href"); }
      }
    });
  }
  function addGroup(name) {
    var head = document.createElement("li");
    head.className = "pal-group";
    head.textContent = name;
    list.appendChild(head);
  }
  function addResult(group, label, href, iconHtml, nested) {
    var link = document.createElement("a");
    link.href = href;
    link.innerHTML = (iconHtml || "") + "<span></span>";
    link.lastChild.textContent = label;
    if (nested) { link.className = "pal-sub"; link.setAttribute("data-group", group); }
    var item = document.createElement("li");
    item.appendChild(link);
    list.appendChild(item);
    shown.push(link);
  }
  function paintSections(needle) {
    var want = (needle || "").trim().toLowerCase();
    list.innerHTML = "";
    shown = [];
    var group = "";
    for (var i = 0; i < ALL.length; i++) {
      var row = ALL[i];
      if (want && row[1].toLowerCase().indexOf(want) < 0) { continue; }
      if (row[0] !== group) { group = row[0]; addGroup(group); }
      addResult(row[0], row[1], row[2], row[3], row[4]);
    }
    chosen = 0;
    paintChoice();
  }
  function loading() {
    addGroup("Ищем в данных");
    var item = document.createElement("li");
    item.className = "pal-loading";
    item.innerHTML = '<span class="skel skel-line"></span><span class="skel skel-line"></span>';
    list.appendChild(item);
  }
  function iconFor(name) { return RESULT_ICONS[name] || RESULT_ICONS.people; }
  function csrfToken() {
    var field = document.querySelector('form[action="/panel/logout"] input[name="csrf"]');
    return field ? field.value : "";
  }
  function remoteSearch(value) {
    var serial = ++requestNo;
    loading();
    var body = new URLSearchParams();
    body.set("q", value);
    body.set("csrf", csrfToken());
    fetch("/panel/search/quick", {method: "POST", credentials: "same-origin", body: body,
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}})
      .then(function (response) { if (!response.ok) { throw new Error("search"); } return response.json(); })
      .then(function (payload) {
        if (serial !== requestNo || search.value.trim() !== value) { return; }
        paintSections(value);
        var loadingNode = list.querySelector(".pal-loading");
        if (loadingNode) { loadingNode.remove(); }
        var results = payload.results || [], group = "";
        for (var i = 0; i < results.length; i++) {
          if (results[i].kind !== group) { group = results[i].kind; addGroup(group); }
          if (!results[i].href || results[i].href.indexOf("/panel/") !== 0) { continue; }
          addResult(group, results[i].label, results[i].href, iconFor(results[i].icon));
        }
        if (!shown.length) { addGroup("Ничего не нашлось"); }
        chosen = 0; paintChoice();
      }).catch(function () {
        if (serial !== requestNo || search.value.trim() !== value) { return; }
        var loadingNode = list.querySelector(".pal-loading");
        if (loadingNode) { loadingNode.remove(); }
        panelToast("Поиск временно недоступен", "bad");
      });
  }
  function paintChoice() {
    for (var i = 0; i < shown.length; i++) { shown[i].classList.toggle("on", i === chosen); }
  }
  function choose(step) {
    if (!shown.length) { return; }
    chosen = Math.min(shown.length - 1, Math.max(0, chosen + step));
    paintChoice();
    try { shown[chosen].scrollIntoView({block: "nearest"}); } catch (err) {}
  }
  function openPalette() {
    priorFocus = document.activeElement; build(); search.value = ""; requestNo++;
    paintSections(""); palette.classList.add("is-open"); search.focus();
  }
  function closePalette() {
    requestNo++; clearTimeout(waitTimer);
    if (palette) { palette.classList.remove("is-open"); }
    if (priorFocus && priorFocus.focus) { priorFocus.focus(); }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target && event.target.closest ? event.target.closest("[data-palette-open]") : null;
    if (trigger) { event.preventDefault(); openPalette(); }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "k" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (palette && palette.classList.contains("is-open")) { closePalette(); } else { openPalette(); }
      return;
    }
    if (palette && palette.classList.contains("is-open")) {
      if (event.key === "Escape") { event.preventDefault(); closePalette(); }
      return;
    }
    if (editing(event.target) || event.ctrlKey || event.metaKey || event.altKey) { return; }
    var key = event.key;
    if (key === "/") { event.preventDefault(); openPalette(); return; }
    if (key === "?") { event.preventDefault(); help(); return; }
    if (key === "j") { event.preventDefault(); move(1); return; }
    if (key === "k") { event.preventDefault(); move(-1); return; }
    if (key === "a") { event.preventDefault(); send("archive"); return; }
    if (key === "r") { event.preventDefault(); focusField("text"); return; }
    if (key === "e") { event.preventDefault(); focusField("text_content"); return; }
    if (key === "Enter" && !clickable(event.target)) { event.preventDefault(); openRow(picked()); }
  });
}());

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
    var details = root.querySelectorAll(".upd-release:not([hidden]) details");
    for (var i = 0; i < details.length; i++) { details[i].open = open; }
  });
}());
