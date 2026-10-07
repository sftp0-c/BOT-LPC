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


# ── пустые строки между парами ─────────────────────────────────────────────
def test_no_gap_between_first_and_second_lesson():
    куски = chunks(tt.format_day(make_day(1, 2)))
    assert len(куски) == 1
    assert numbers_in(куски[0]) == [1, 2]


def test_gap_before_third_lesson():
    куски = chunks(tt.format_day(make_day(1, 2, 3)))
    assert len(куски) == 2
    assert numbers_in(куски[0]) == [1, 2]
    assert numbers_in(куски[1]) == [3]


def test_six_lessons_split_in_three_pairs():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5, 6)))
    assert [numbers_in(кусок) for кусок in куски] == [[1, 2], [3, 4], [5, 6]]


def test_gap_before_fifth_lesson():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5)))
    assert [numbers_in(кусок) for кусок in куски] == [[1, 2], [3, 4], [5]]


def test_odd_tail_stays_alone():
    text = tt.format_day(make_day(1, 2, 3, 4, 5))
    assert not text.endswith("\n")
    assert chunks(text)[-1].splitlines()[0].endswith("Предмет")


def test_no_gap_at_the_start_and_at_the_end_of_the_day():
    text = tt.format_day(make_day(1, 2, 3))
    assert not text.startswith("\n")
    assert text.splitlines()[0].startswith("📅 Понедельник")
    assert text.splitlines()[1].strip().startswith("1.")
    assert not text.endswith("\n")


def test_gap_sits_before_the_odd_lesson_only():
    text = tt.format_day(make_day(1, 2, 3, 4, 5, 6, 7))
    lines = text.splitlines()
    for index, линия in enumerate(lines):
        if линия.strip()[:1].isdigit() and ". " in линия.strip()[:6]:
            номер = int(линия.strip().split(".")[0])
            gap = bool(index and not lines[index - 1].strip())
            assert gap == (номер % 2 == 1 and номер > 1), (
                f"перед {номер}-м уроком пустая строка {'есть' if gap else 'нет'}")


def test_day_with_one_lesson_has_no_gap():
    текст = tt.format_day(make_day(1))
    assert chunks(текст) == [текст]


# ── неделя ─────────────────────────────────────────────────────────────────
def test_days_in_a_week_are_separated_by_one_blank_line():
    неделя = tt.GroupSchedule(group="ИС-21", week=dt.date(2026, 9, 28), days={
        0: make_day(1, 2, weekday=0),
        2: make_day(1, 2, weekday=2),
    })
    text = tt.format_schedule(неделя)
    assert f"\n\n📅 {tt.WEEKDAYS_FULL[2].capitalize()}" in text
    assert "\n\n\n📅" not in text
    assert text.count("📅") == 2


def test_bells_line_lists_pairs_from_the_official_schedule(frozen_college_clock):
    """Строка звонков - по парам, а не по урокам.

    Раньше _bells_line получал номер урока и делал 2 * number, то есть путал
    уроки с парами. Из-за этого выходило «1 — 08:00–08:45 / 08:50–09:35,
    2 — 08:50–09:35 / 10:55–11:40»: времена пересекались. Официальный график
    владельца: 7 пар, из них первые четыре по два урока.
    """
    неделя = tt.GroupSchedule(group="ИС-21", week=dt.date(2026, 9, 28), days={0: make_day(1)})
    text = tt.format_schedule(неделя)
    строка = [x for x in text.splitlines() if x.startswith("Пары по звонкам:")]
    assert строка, text
    ожидаемое = ", ".join(f"{n} — {tt.pair_time(n)}" for n in range(1, tt.PAIR_COUNT + 1))
    assert строка[0] == f"Пары по звонкам: {ожидаемое}"


def test_bells_line_had_no_repeating_times():
    """Времена пар не должны повторяться: пара 1 и пара 2 разные."""
    времена = [tt.pair_time(n) for n in range(1, tt.PAIR_COUNT + 1)]
    assert len(set(времена)) == len(времена), f"времена пар повторяются: {времена}"


def test_pair_of_lesson_matches_official_schedule():
    assert [tt.pair_of_lesson(n) for n in range(1, 12)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 6, 7]
