"""Подпись кнопки подгоняется под ширину ряда: 2 кнопки — 16 символов, 1 — 26.

Замер калибровочным сообщением в MAX: в ряду из двух кнопок на компьютере целиком
видно 16 символов, третья подпись в 18 символов уже обрезается многоточием.
Значит один предел на всю клавиатуру неверен - он зависит от числа кнопок в ряду.
"""
import max_api


def buttons_of(keyboard):
    return [[b["text"] for b in row] for row in max_api.fit_keyboard(keyboard)]


# ── пределы ─────────────────────────────────────────────────────────────────
def test_row_limits_follow_the_measurement():
    assert max_api.row_limit(1) == max_api.BUTTON_TEXT == 26
    assert max_api.row_limit(2) == 16
    assert max_api.row_limit(3) == 14
    assert max_api.row_limit(7) == 12


def test_one_button_row_keeps_long_label():
    """Одна кнопка в ряду получает максимум: 26 символов показываются целиком."""
    label = "🗑 Удалить вместе с обращ.."     # ровно 26 символов
    assert len(label) == max_api.BUTTON_TEXT, len(label)
    row = max_api.fit_keyboard([[max_api.btn(label, "x")]])
    assert row[0][0]["text"] == label


def test_names_need_single_button_rows():
    """«Фамилия И. О.» не влезает в ряд из двух кнопок - такие списки делаем в одну кнопку.

    Именно поэтому списки людей в боте будут по одной кнопке на ряд, а не по три.
    """
    name = "Ковалевский К. П."
    assert len(name) > max_api.row_limit(2)
    tight = max_api.fit_keyboard([[max_api.btn(name, "a"), max_api.btn("Вперёд", "b")]])
    assert tight[0][0]["text"].endswith("…")
    roomy = max_api.fit_keyboard([[max_api.btn(name, "a")]])
    assert roomy[0][0]["text"] == name


def test_two_buttons_row_is_cut():
    row = max_api.fit_keyboard([[
        max_api.btn("Ковалевский Константин Петрович", "a"),
        max_api.btn("Соколова Мария Сергеевна", "b"),
    ]])
    assert all(len(b["text"]) <= 16 for b in row[0])


def test_short_labels_fit_two_button_row():
    """Короткие подписи в ряду из двух кнопок остаются целыми."""
    row = max_api.fit_keyboard([[
        max_api.btn("Ковалевский", "a"),
        max_api.btn("Соколова", "b"),
    ]])
    assert [b["text"] for b in row[0]] == ["Ковалевский", "Соколова"]


def test_seven_buttons_row_is_strict():
    row = max_api.fit_keyboard([[max_api.btn("Очень длинная подпись кнопки", str(i))
                                for i in range(7)]])
    assert all(len(b["text"]) <= 12 for b in row[0])


def test_payload_and_type_are_preserved():
    row = max_api.fit_keyboard([[max_api.btn("Длинная подпись для кнопки", "payload-1"),
                                 max_api.link_btn("Сайт колледжа", "https://example.org")]])
    assert row[0][0]["payload"] == "payload-1"
    assert row[0][0]["type"] == "callback"
    assert row[0][1]["type"] == "link"
    assert row[0][1]["url"] == "https://example.org"


def test_original_keyboard_is_not_mutated():
    """Подгонка не трогает исходный список: им пользуются тесты и вызовы кода."""
    before = max_api.btn("Длинная подпись для кнопки", "a")
    original = [[before]]
    max_api.fit_keyboard(original)
    assert before["text"] == "Длинная подпись для кнопки"
    assert original[0][0] is before


def test_empty_rows_are_dropped():
    assert max_api.fit_keyboard([[], None, [max_api.btn("Меню", "home")]]) == [
        [{"type": "callback", "text": "Меню", "payload": "home"}]
    ]


def test_none_keyboard_is_safe():
    assert max_api.fit_keyboard(None) == []


# ── сквозная проверка: ни одна подпись не выходит за предел своего ряда ─────
def test_no_label_exceeds_its_row_limit():
    keyboard = [
        [max_api.btn("Ковалевский Константин Петрович", "a"), max_api.btn("Соколова М. С.", "b")],
        [max_api.btn("🗑 Удалить вместе с обращениями (12)", "c")],
        [max_api.btn("Первый", "1"), max_api.btn("Второй", "2"), max_api.btn("Третий", "3")],
        [max_api.btn("Короткая", "s")],
    ]
    for row in max_api.fit_keyboard(keyboard):
        limit = max_api.row_limit(len(row))
        assert all(len(b["text"]) <= limit for b in row), [b["text"] for b in row]


def test_cut_marks_the_loss():
    """Обрезанная подпись обязана заканчиваться многоточием - иначе потеря текста незаметна."""
    row = max_api.fit_keyboard([[max_api.btn("Очень длинная подпись", "a"),
                                 max_api.btn("Ещё длинная подпись", "b")]])
    assert all(b["text"].endswith("…") for b in row[0])
