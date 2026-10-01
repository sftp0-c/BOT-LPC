"""Главное меню студента: обращение впереди, второстепенные разделы — в «Ещё».

Задача владельца: «сделать кнопку обращения, чтобы не было на главном экране
разбросано там бухгалтерия и тд». Поэтому проверяем три вещи: обращение видно
сразу, второстепенных разделов на первом экране нет, и - обязательно - они из
«Ещё» открываются. Без третьей проверки разделы можно убрать и не вернуть,
не сломав ничего: это и случилось, пока подменю не существовало.

И второе: как MAX нарисует эти кнопки. Предел подписи зависит от числа кнопок
в ряду (измерено: одна — 20 ячеек, две — 16, три и больше — 9), поэтому ряд
из трёх кнопок обрезается многоточием почти всегда. В меню студента ряды
строятся по две кнопки, и это проверяется здесь же, а не на глаз.
"""
import max_api
from conftest import press, register
from handlers import menus
from handlers.registry import CALLBACKS

STUDENT = "300"

# разделы, которые не должны вернуться на главный экран студента
ВТОРОСТЕПЕННЫЕ = ("sub:cert", "sub:acc", "sub:fb", "faq", "college", "bugreport")


def payloads() -> list:
    return [b["payload"] for row in menus.student_menu() for b in row]


# ── структура: обращение впереди, «Ещё» вместо россыпи ───────────────────────
def test_menu_is_three_rows_and_four_buttons():
    rows = menus.student_menu()
    assert [len(row) for row in rows] == [1, 2, 1]
    assert payloads() == ["tickets", "sched", "profile", "student_more"]


def test_ticket_button_is_the_first_thing_on_screen():
    """Обращение — главное действие, и оно видно сразу, а не после «Ещё»."""
    first = menus.student_menu()[0]
    assert len(first) == 1, "главная кнопка должна быть одна и самой первой"
    assert first[0]["payload"] == "tickets"
    assert "Обращения" in first[0]["text"]


def test_secondary_sections_are_not_on_the_first_screen():
    """Разброса на главном экране быть не должно — это и просил владелец."""
    on_screen = payloads()
    for buried in ВТОРОСТЕПЕННЫЕ:
        assert buried not in on_screen, f"{buried} не должен быть на главном экране"


def test_more_button_is_a_live_callback():
    """«Ещё» не должна быть мёртвой кнопкой.

    Неизвестный payload бот не рисует вовсе: он только пишет в журнал, и
    человек нажимает в пустоту. Поэтому payload из меню обязан быть
    зарегистрированным обработчиком.
    """
    assert "student_more" in payloads()
    assert CALLBACKS.get("student_more") is menus.cb_student_more, \
        "«Ещё» ведёт не в то подменю"


# ── вёрстка: подписи помещаются в ряд, многоточия нет ───────────────────────
def test_no_row_is_wider_than_two_buttons():
    """В ряду из трёх и более подпись режется многоточием — это измерено."""
    assert all(len(row) <= 2 for row in menus.student_menu())


def test_two_button_row_fits_measured_sixteen_cells():
    """16 ячеек в ряду из двух кнопок — измеренная величина со скриншотов MAX."""
    for row in menus.student_menu():
        if len(row) == 2:
            for button in row:
                width = max_api.display_width(button["text"])
                assert width <= 16, f"«{button['text']}» — {width} ячеек"


def test_labels_survive_fit_keyboard():
    """Ровно то, что рисует MAX: подгонка по ширине ряда, без многоточия."""
    for row in max_api.fit_keyboard(menus.student_menu()):
        limit = max_api.row_limit(len(row))
        for button in row:
            label = button["text"]
            assert not label.endswith("…"), f"подпись обрезана: «{label}»"
            assert max_api.display_width(label) <= limit, (
                f"«{label}» — {max_api.display_width(label)} ячеек, "
                f"в ряду из {len(row)} помещается {limit}")


def test_menu_fits_max_keyboard_limit():
    assert len(menus.student_menu()) <= max_api.MAX_ROWS


# ── живой бот: те же кнопки и тот же экран ───────────────────────────────────
async def test_home_sends_exactly_those_buttons(api):
    """Через бота, а не через student_menu(): человек должен реально увидеть их."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "home")
    assert api.payloads(STUDENT) == ["tickets", "sched", "profile", "student_more"]


async def test_more_button_opens_its_own_screen(api):
    """Нажатие «Ещё» открывает подменю, а не молчит и не уводит в никуда."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "student_more")
    assert api.last(STUDENT)[0] == STUDENT
    assert api.last(STUDENT)[1].strip(), "после «Ещё» должен открыться экран"


async def test_buried_sections_are_reachable_from_more(api):
    """Главное: разделы, убранные с первого экрана, из «Ещё» открываются.

    Раньше этого не проверял никто - и потому разделы можно было убрать и не
    вернуть, не сломав ни одного теста. Теперь такое поломалось бы сразу.
    """
    await register(STUDENT)
    await press(STUDENT, "home")
    assert not (set(ВТОРОСТЕПЕННЫЕ) & set(api.payloads(STUDENT))), \
        "второстепенные разделы вернулись на главный экран"
    await press(STUDENT, "student_more")
    найдено = set(api.payloads(STUDENT))
    for раздел in ("sub:cert", "sub:acc", "sub:fb", "faq"):
        assert раздел in найдено, f"раздел {раздел} потерян при переносе в «Ещё»"
    assert "home" in найдено, "из «Ещё» нельзя вернуться в меню"


async def test_more_rows_also_fit_the_keyboard(api):
    """Подменю «Ещё» рисуется теми же правилами: ряды по две, многоточия нет."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "student_more")
    for _to, text, keyboard in api.sent[-1:]:
        for row in max_api.fit_keyboard(keyboard or []):
            assert len(row) <= 2, f"широкий ряд на экране {text[:40]!r}"
            limit = max_api.row_limit(len(row))
            for button in row:
                label = button["text"]
                assert not label.endswith("…"), f"«{label}» обрезано"
                assert max_api.display_width(label) <= limit, f"«{label}»"


async def test_home_screens_are_not_cut(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "home")
    for _to, text, keyboard in api.sent:
        for row in max_api.fit_keyboard(keyboard or []):
            assert len(row) <= 2, f"широкий ряд на экране {text[:40]!r}"
            limit = max_api.row_limit(len(row))
            for button in row:
                label = button["text"]
                assert not label.endswith("…"), f"«{label}» обрезано"
                assert max_api.display_width(label) <= limit, f"«{label}»"
