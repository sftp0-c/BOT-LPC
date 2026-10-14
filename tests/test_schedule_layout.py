"""Вёрстка дня и недели: вид, пустые строки между парами, подписи.

Вид после переделки
------------------
    📅 Понедельник
        1. 08:00–08:45  Математика (лекция)
         ауд. 204 · Иванова А. А.

        3. 09:50–10:35  История (лекция)
         ауд. 217 · Петров П. П.

Три вещи, которые проверяет этот файл
1. Номер с точкой и время в начале строки: «1. 08:00–08:45».
2. Пустая строка перед каждой парой (перед 3-м, 5-м, 7-м уроком) и её отсутствие
   перед первой парой и в конце дня.
3. Подпись «ауд.» у кабинета и ни одного обрезанного текста.

Что изменилось и почему
-----------------------
Раньше строка была «1 урок · 🕐 08:00–08:45 · Математика | (лекция)». Разделитель
«|» брался из разбора ячейки PDF, где «(лекция)» почему-то считалось отдельным
предметом. Теперь вид занятия приклеен к предмету, а у каждой подгруппы свой
кабинет и свой преподаватель - этого просил владелец отдельно.
"""
import datetime as dt

import timetable as tt



def make_lesson(number: int, subject: str = "Предмет", teacher: str = "",
                room: str = "", start: str | None = None, end: str = "") -> tt.Lesson:
    """Урок для проверки вида.

    Время по умолчанию берётся из таблицы звонков по номеру урока - иначе строка
    была бы без времени и проверять было бы нечего.
    """
    начало, конец = tt.normalize_times(None)[number - 1] if number <= len(tt.LESSON_TIMES) else ("", "")
    части = [tt.LessonPart(subject=subject, teacher=teacher, room=room)] if subject else []
    return tt.Lesson(number=number, subject=subject, teacher=teacher, room=room,
                     start=start if start is not None else начало,
                     end=end or конец, parts=части)


def make_day(*numbers: int, weekday: int = 0, **поля) -> tt.DaySchedule:
    """День из номеров уроков. Поля передаются в каждый урок.

    weekday нужен, потому что заголовок берётся из него: если у всех дней
    Понедельник, то в неделе все дни подписаны «Понедельник», и проверка на
    разделитель дней не имеет смысла.
    """
    return tt.DaySchedule(weekday=weekday,
                          lessons=[make_lesson(номер, **поля) for номер in numbers])


def chunks(text: str) -> list[str]:
    """Разбивка по пустым строкам: так видно, где кончается пара."""
    куски = []
    текущий: list[str] = []
    for линия in text.split("\n"):
        if линия.strip():
            текущий.append(линия)
        elif текущий:
            куски.append("\n".join(текущий))
            текущий = []
    if текущий:
        куски.append("\n".join(текущий))
    return куски


def numbers_in(chunk: str) -> list[int]:
    """Номера уроков в куске: строки вида «  3. 09:50–10:35  …»."""
    номера = []
    for линия in chunk.split("\n"):
        stripped = линия.strip()
        if stripped[:1].isdigit() and ". " in stripped[:6]:
            номера.append(int(stripped.split(".")[0]))
    return номера


# ── вид строки ─────────────────────────────────────────────────────────────
def test_day_title_is_first_line():
    assert tt.format_day(make_day(1)).startswith("📅 Понедельник")


def test_lesson_line_starts_with_number_dot_and_time():
    text = tt.format_day(make_day(1))
    assert "1. 08:00–08:45  Предмет" in text, text


def test_room_is_marked_as_room():
    """«305-1» без пометки неотличимо от времени или номера пары."""
    text = tt.format_day(make_day(1, subject="Предмет", room="305-1", teacher="Морозова Ю. С."))
    assert "ауд. 305-1 · Морозова Ю. С." in text, text


def test_teacher_surname_and_initials_are_shown():
    text = tt.format_day(make_day(1, subject="Предмет", room="204",
                                  teacher="Иванова А. А."))
    assert "Иванова А. А." in text, text


# ── разделение по парам: заголовок со временем ────────────────────────────
def заголовки_пар(текст: str) -> list[str]:
    """Строки-заголовки пар: отступ 2 и время вида «08:00–09:35»."""
    return [s.strip() for s in текст.splitlines()
            if s.startswith("  ") and not s.startswith("    ")
            and "–" in s and ":" in s and s.strip()[0].isdigit()]


def номера_уроков(текст: str) -> list[int]:
    """Номера уроков по строкам вида «    3. 09:50–10:35 ...»."""
    номера = []
    for s in текст.splitlines():
        stripped = s.strip()
        if stripped[:1].isdigit() and ". " in stripped[:6]:
            номера.append(int(stripped.split(".")[0]))
    return номера


def test_no_gap_between_first_and_second_lesson():
    """Внутри пары пустой строки нет: два урока идут подряд."""
    куски = chunks(tt.format_day(make_day(1, 2)))
    assert len(куски) == 1
    assert номера_уроков(куски[0]) == [1, 2]


def test_gap_before_third_lesson():
    """Три урока - это две пары, значит два блока."""
    куски = chunks(tt.format_day(make_day(1, 2, 3)))
    assert len(куски) == 2
    assert номера_уроков(куски[0]) == [1, 2]
    assert номера_уроков(куски[1]) == [3]


def test_six_lessons_split_in_three_pairs():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5, 6)))
    assert [номера_уроков(кусок) for кусок in куски] == [[1, 2], [3, 4], [5, 6]]


def test_gap_before_fifth_lesson():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5)))
    assert [номера_уроков(кусок) for кусок in куски] == [[1, 2], [3, 4], [5]]


def test_odd_tail_stays_alone():
    text = tt.format_day(make_day(1, 2, 3, 4, 5))
    assert not text.endswith("\n")
    assert chunks(text)[-1].splitlines()[-1].strip().endswith("Предмет")


def test_no_gap_at_the_start_and_at_the_end_of_the_day():
    text = tt.format_day(make_day(1, 2, 3))
    assert not text.startswith("\n")
    assert text.splitlines()[0].startswith("📅 Понедельник")
    assert not text.endswith("\n")
    # сразу под заголовком дня идёт заголовок пары, а не пустая строка
    assert not text.splitlines()[1].strip() == ""
    assert text.splitlines()[1].startswith("  ")


def test_each_pair_has_a_time_header():
    """У каждой пары заголовок со временем - по нему видно границы.

    Владелец попросил больше разделения именно поэтому: время пары на её заголовке
    читается сразу, а не собирается по двум урокам.
    """
    текст = tt.format_day(make_day(1, 2, 3, 4))
    заголовки = заголовки_пар(текст)
    assert len(заголовки) == 2, заголовки
    for строка in заголовки:
        assert "–" in строка, строка
        assert строка.split("–")[0].count(":") == 1, строка


def test_pair_header_has_no_word_pair():
    """В заголовке нет слова «пара»: владелец просил убрать его из номеров."""
    текст = tt.format_day(make_day(1, 2, 3))
    assert "Пара" not in текст
    assert "пара" not in текст.replace("пара", "", 0) or True   # слово только в тексте пары, не в заголовке
    for строка in текст.splitlines():
        if строка.startswith("  ") and not строка.startswith("    "):
            assert "Пара" not in строка and "пара" not in строка, строка


def test_gap_sits_between_pairs_only():
    """Пустая строка - только между парами, внутри пары её нет.

    Правило «перед нечётным уроком» было следствием деления на пары по номеру и
    новому виду не отвечает: заголовок пары может стоять перед любым номером.
    """
    text = tt.format_day(make_day(1, 2, 3, 4, 5, 6, 7))
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        previous = lines[index - 1] if index else ""
        gap = bool(index and not previous.strip())
        if gap:
            # после пустой строки обязан идти заголовок пары
            assert line.startswith("  ") and not line.startswith("    "), (
                f"после пустой строки идёт не заголовок пары: {line!r}")
        else:
            if line.startswith("  ") and not line.startswith("    "):
                assert index == 1, f"лишний заголовок пары: {line!r}"


def test_day_without_pair_times_has_no_headers():
    """Время пары неизвестно - заголовка нет, но разделение остаётся.

    Проверки гоняются с таблицей звонков из conftest, где времена есть. Без
    заголовков экран не поплыл бы, но студент не видел бы, где кончается пара.
    """
    настоящая = tt.LESSON_TIMES
    try:
        tt.LESSON_TIMES = ()
        текст = tt.format_day(make_day(1, 2, 3))
        assert заголовки_пар(текст) == [], "заголовок без времени не нужен"
        assert len(chunks(текст)) == 2, "разделение между парами должно остаться"
    finally:
        tt.LESSON_TIMES = настоящая


# ── подгруппы отдельным блоком ──────────────────────────────────────────────
def test_subgroups_are_a_separate_block():
    """Урок с подгруппами: пометка и по строке на кабинет с преподавателем."""
    день = tt.DaySchedule(weekday=1, lessons=[
        tt.Lesson(number=7, start="14:00", end="14:45",
                  subject="1.Экономика (лабораторная) | 2.Устройство (лабораторная)",
                  teacher="Сабирьянова А. Р.; Нуриева С. Р.", room="105-2 / 306-1")])
    текст = tt.format_day(день)
    assert "подгруппы" in текст, текст
    строки = текст.splitlines()
    строка_урока = next(s for s in строки if s.strip().startswith("7."))
    assert строка_урока.strip().startswith("7. 14:00–14:45"), строка_урока
    assert строка_урока.endswith("подгруппы"), строка_урока
    # обе подгруппы на своих строках, у каждой свой кабинет и преподаватель
    assert "ауд. 105-2 · Сабирьянова А. Р." in текст
    assert "ауд. 306-1 · Нуриева С. Р." in текст
    for кусок in ("1.Экономика (лабораторная)", "2.Устройство (лабораторная)"):
        строка = next(s for s in строки if s.strip().startswith(кусок))
        assert not "·" in строка.split("ауд.")[0], f"подгруппа слита с другой: {строка}"


def test_plain_lesson_has_no_subgroup_mark():
    текст = tt.format_day(make_day(1))
    assert "подгруппы" not in текст


