"""Прокрутка меню v1.7: контракт и JS на модели DOM, не замена браузерной проверке."""
import json
import re
import shutil
import subprocess

import pytest

import panel_theme as theme


# Движок настоящий, геометрия синтетическая: проверяем события и сохранение,
# а не выдаём модель DOM за проверку вёрстки в браузере.
_HARNESS = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = JSON.parse(require("node:fs").readFileSync(0, "utf8")).script;
function target() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, fn, options) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push({fn, options});
    },
    fire(type, event = {}) {
      for (const entry of listeners.get(type) || []) entry.fn(event);
    }
  };
}
function page(options = {}) {
  const user = options.user === undefined ? "101" : options.user;
  const key = "panel-nav-scroll:" + user;
  const storage = options.storage || new Map();
  if (options.saved !== undefined) storage.set(key, options.saved);
  let writes = 0;
  const storageApi = {
    getItem(key) {
      if (options.denied === "get") throw new Error("Чтение запрещено");
      return storage.get(key) || null;
    },
    setItem(key, value) {
      if (options.denied === "set") throw new Error("Запись запрещена");
      storage.set(key, value); writes++;
    }
  };
  const frames = new Map();
  let nextFrame = 0;
  const window = Object.assign(target(), {
    innerWidth: options.width || 1280,
    requestAnimationFrame(fn) { frames.set(++nextFrame, fn); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); },
    scrollTo() { throw new Error("Меню прокрутило документ"); },
    scrollBy() { throw new Error("Меню прокрутило документ"); }
  });
  Object.defineProperty(window, "history", {
    get() { throw new Error("Меню обратилось к history"); }
  });
  const sizes = {
    desktop: {height: 300, width: 240, fullHeight: 1800, fullWidth: 240},
    mobile: {height: 90, width: 390, fullHeight: 90, fullWidth: 3600}
  };
  const axis = () => window.innerWidth > 1000 ? "desktop" : "mobile";
  const size = () => sizes[axis()];
  let top = 0, left = 0;
  const active = options.active || null;
  const nav = Object.assign(target(), {
    clientTop: 1, clientLeft: 1,
    getBoundingClientRect() { return {top: 72, left: 0}; },
    querySelector(selector) {
      assert.equal(selector, 'a[aria-current="page"], a.on');
      if (!active) return null;
      return {
        getBoundingClientRect() {
          const position = axis() === "desktop" ? 73 + active.start - top : 1 + active.start - left;
          return axis() === "desktop" ? {top: position, bottom: position + active.size} :
            {left: position, right: position + active.size};
        },
        scrollIntoView() { throw new Error("Пункт прокрутил документ"); }
      };
    }
  });
  for (const [name, dimension] of Object.entries({
    clientHeight: "height", clientWidth: "width", scrollHeight: "fullHeight", scrollWidth: "fullWidth"
  })) Object.defineProperty(nav, name, {get() { return size()[dimension]; }});
  Object.defineProperties(nav, {
    scrollTop: {
      get() { return top; },
      set(value) {
        assert.ok(Number.isFinite(value), "Неконечный scrollTop");
        top = Math.max(0, Math.min(value, size().fullHeight - size().height));
      }
    },
    scrollLeft: {
      get() { return left; },
      set(value) {
        assert.ok(Number.isFinite(value), "Неконечный scrollLeft");
        left = Math.max(0, Math.min(value, size().fullWidth - size().width));
      }
    }
  });
  const document = {
    body: options.body === false ? null : {dataset: user === null ? {} : {panelUser: user}},
    documentElement: new Proxy({scrollTop: 700, scrollLeft: 0}, {
      set() { throw new Error("Изменена прокрутка документа"); }
    }),
    querySelector(selector) {
      assert.equal(selector, ".app-nav");
      return options.nav === false ? null : nav;
    }
  };
  const context = {window, document};
  for (const owner of [context, window]) Object.defineProperty(owner, "sessionStorage", {
    get() {
      if (options.denied === "access") throw new Error("Хранилище недоступно");
      return storageApi;
    }
  });
  vm.runInNewContext(source, context, {timeout: 1000});
  return {
    nav, window, storage, sizes, frames, document,
    saved() { return JSON.parse(storage.get(key) || "{}"); },
    writes() { return writes; },
    frame() {
      const pending = [...frames.values()]; frames.clear();
      for (const fn of pending) fn();
    },
    resize(width) {
      window.innerWidth = width;
      // Браузер обнуляет старую ось ещё до обработчика resize.
      nav.scrollTop = nav.scrollTop; nav.scrollLeft = nav.scrollLeft;
      nav.fire("scroll"); window.fire("resize");
    }
  };
}
"""


def _run_js(checks: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js недоступен; контракт проверяется отдельно")
    result = subprocess.run(
        [node, "-e", _HARNESS + "\n" + checks],
        input=json.dumps({"script": theme.nav_scroll_script()}),
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nav_script_is_standalone_and_never_restores_document():
    script = theme.nav_scroll_script()
    assert script.strip() and script == theme.nav_scroll_script()
    assert script not in theme.actions_script()
    assert ".app-nav" in script and "document.body.dataset.panelUser" in script
    assert "sessionStorage" in script and "localStorage" not in script
    for forbidden in ("<script", "</script", "scrollIntoView", "scrollRestoration",
                      "window.scroll", "document.documentElement", "[data-filters]", "fetch("):
        assert forbidden not in script, forbidden


def test_scroll_writes_once_per_frame_and_keeps_latest_value():
    _run_js("""
const p = page();
for (let value = 200; value < 220; value++) {
  p.nav.scrollTop = value; p.nav.fire("scroll");
}
assert.equal(p.writes(), 0);
assert.equal(p.frames.size, 1);
p.frame();
assert.equal(p.writes(), 1);
assert.equal(p.saved().desktop, 219);
assert.equal(p.nav.listeners.get("scroll")[0].options.passive, true);
""")


@pytest.mark.parametrize("event", ["click", "pagehide"])
def test_leaving_flushes_latest_position_without_waiting_for_frame(event):
    _run_js("""
const p = page();
p.nav.scrollTop = 370; p.nav.fire("scroll");
p.nav.scrollTop = 387;
""" + ("p.nav.fire('click');" if event == "click" else "p.window.fire('pagehide');") + """
assert.equal(p.saved().desktop, 387);
assert.equal(p.frames.size, 0);
assert.equal(p.nav.listeners.get("click")[0].options, true);
""")


def test_positions_are_scoped_by_user_not_by_page():
    _run_js("""
const storage = new Map();
const first = page({user: "101", storage});
first.nav.scrollTop = 480; first.nav.fire("click");
const other = page({user: "202", storage});
assert.equal(other.nav.scrollTop, 0);
other.nav.scrollTop = 90; other.window.fire("pagehide");
assert.equal(page({user: "101", storage}).nav.scrollTop, 480);
assert.equal(page({user: "202", storage}).nav.scrollTop, 90);
assert.deepEqual([...storage.keys()].sort(), ["panel-nav-scroll:101", "panel-nav-scroll:202"]);
""")


def test_resize_keeps_both_axes_even_before_pending_save():
    _run_js("""
const p = page({saved: '{"desktop":480,"mobile":320}'});
assert.equal(p.nav.scrollTop, 480);
p.resize(1000);
assert.equal(p.nav.scrollLeft, 320);
p.nav.scrollLeft = 660; p.nav.fire("scroll");
p.resize(1001); p.frame();
assert.equal(p.nav.scrollTop, 480);
assert.equal(p.saved().desktop, 480);
assert.equal(p.saved().mobile, 660);
p.resize(390);
assert.equal(p.nav.scrollLeft, 660);
p.window.fire("pagehide");
assert.equal(p.saved().desktop, 480);
""")


def test_resize_to_non_scrollable_nav_does_not_forget_desktop():
    _run_js("""
const p = page({saved: '{"desktop":480}'});
p.sizes.desktop.height = 2000; p.resize(1280); p.frame();
assert.equal(p.nav.scrollTop, 0);
assert.equal(p.saved().desktop, 480);
p.sizes.desktop.height = 300; p.resize(1280);
assert.equal(p.nav.scrollTop, 480);
""")


@pytest.mark.parametrize(("saved", "expected"), [
    ('{"desktop":37.5}', 37.5), ('{"desktop":-25}', 0), ('{"desktop":90000}', 1500),
    ('{"desktop":1e999}', 0), ('{"desktop":"Infinity"}', 0), ('{"desktop":"70"}', 0),
    ('{"desktop":true}', 0), ('{"desktop":null}', 0), ('{"desktop":[]}', 0),
    ('{"desktop":{}}', 0), ('[]', 0), ('null', 0), ('не JSON', 0),
])
def test_only_finite_numeric_saved_positions_are_restored(saved, expected):
    _run_js(f"""
const p = page({{saved: {json.dumps(saved)}}});
assert.equal(p.nav.scrollTop, {expected});
""")


@pytest.mark.parametrize(("width", "saved", "start", "size", "expected"), [
    (1280, 200, 240, 40, 200), (1280, 200, 175, 40, 175),
    (1280, 200, 490, 40, 230), (1280, 200, 150, 500, 200),
    (390, 420, 450, 110, 420), (390, 420, 390, 110, 390),
    (390, 420, 760, 100, 470), (390, 420, 400, 700, 420),
])
def test_active_link_moves_only_nav_and_only_to_nearest_edge(width, saved, start, size, expected):
    axis, prop = ("desktop", "scrollTop") if width > 1000 else ("mobile", "scrollLeft")
    options = {"width": width, "saved": json.dumps({axis: saved}),
               "active": {"start": start, "size": size}}
    _run_js(f"""
const p = page({json.dumps(options)});
assert.equal(p.nav.{prop}, {expected});
assert.equal(p.document.documentElement.scrollTop, 700);
""")


def test_bfcache_reads_latest_nav_position_without_touching_document():
    _run_js("""
const storage = new Map();
const cached = page({storage, saved: '{"desktop":230}'});
const next = page({storage});
next.nav.scrollTop = 540; next.window.fire("pagehide");
cached.nav.scrollTop = 0;
cached.window.fire("pageshow", {persisted: true});
assert.equal(cached.nav.scrollTop, 540);
assert.equal(cached.document.documentElement.scrollTop, 700);
""")


@pytest.mark.parametrize("denied", ["get", "set", "access"])
def test_unavailable_storage_is_benign(denied):
    _run_js(f"""
const p = page({{denied: {json.dumps(denied)}, active: {{start: 500, size: 40}}}});
assert.equal(p.nav.scrollTop, 240);
p.nav.scrollTop = 340; p.nav.fire("scroll"); p.frame();
p.nav.fire("click"); p.window.fire("pagehide");
p.window.fire("pageshow", {{persisted: true}});
""")


@pytest.mark.parametrize("options", [{"user": ""}, {"user": "  "}, {"user": None},
                                     {"nav": False}, {"body": False}])
def test_missing_user_or_nav_has_no_shared_storage_or_listeners(options):
    _run_js(f"""
const p = page({json.dumps(options)});
assert.equal(p.storage.size, 0);
assert.equal(p.nav.listeners.size, 0);
assert.equal(p.window.listeners.size, 0);
""")


def test_light_palette_and_compact_beta_use_existing_tokens():
    assert theme.TOKENS["light.surface"] == "#ffffff"
    assert theme.TOKENS["light.acc"] == "#1d4ed8"
    pill = re.search(r"\.panel-beta\{([^}]+)\}", theme.STYLESHEET).group(1)
    assert "border-radius:var(--radius-pill)" in pill
    assert "color:var(--acc)" in pill and "background:var(--acc-soft)" in pill
    assert theme.ICON_NAMES_BY_PATH["/feedback"] == "reply"
    bodies = [theme.ICONS[name] for name in theme.ICON_NAMES_BY_PATH.values()]
    assert len(bodies) == len(set(bodies))
