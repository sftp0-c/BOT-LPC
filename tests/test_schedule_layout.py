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
def test_lesson_block_shows_subject_and_room():
    """Блок занятия: предмет, ниже кабинет с преподавателем.

    Номера уроков и времени урока в виде больше нет: пара опознаётся по времени
    в заголовке, а время урока повторяло его мельче - это владелец и назвал
    тавтологией.
    """
    text = tt.format_day(make_day(1, subject="Математика", room="204",
                                  teacher="Иванов И. И."))
    assert "    Математика" in text, text
    assert "ауд. 204 · Иванов И. И." in text, text
    assert "08:00–09:35" in text, "время пары должно быть в заголовке"
    assert "08:00–08:45" not in text, "время урока не должно дублировать заголовок"


def test_room_is_marked_as_room():
    """«305-1» без пометки неотличимо от времени или номера пары."""
    text = tt.format_day(make_day(1, subject="Предмет", room="305-1", teacher="Морозова Ю. С."))
    assert "ауд. 305-1 · Морозова Ю. С." in text, text


def test_teacher_surname_and_initials_are_shown():
    text = tt.format_day(make_day(1, subject="Предмет", room="204",
                                  teacher="Иванова А. А."))
    assert "Иванова А. А." in text, text


# ── разделение по парам и схлопывание ───────────────────────────────────────
def заголовки_пар(текст: str) -> list[str]:
    """Строки-заголовки пар: отступ 2 и время вида «08:00–09:35»."""
    return [s.strip() for s in текст.splitlines()
            if s.startswith("  ") and not s.startswith("   ")
            and "–" in s and ":" in s and s.strip()[0].isdigit()]


def предметы_дня(текст: str) -> list[str]:
    """Строки с предметами: отступ 4."""
    return [s.strip() for s in текст.splitlines()
            if s.startswith("    ") and not s.startswith("      ")]


def test_day_title_is_first_line():
    assert tt.format_day(make_day(1)).startswith("📅 Понедельник")


def test_pair_header_is_time():
    """У пары заголовок со временем - по нему видно границы."""
    текст = tt.format_day(make_day(1, 2, 3, 4))
    заголовки = заголовки_пар(текст)
    assert len(заголовки) == 2, заголовки
    for строка in заголовки:
        assert "–" in строка, строка


def test_no_gap_between_first_and_second_lesson():
    """Внутри пары отдельных строк нет: пара показывается одним блоком."""
    текст = tt.format_day(make_day(1, 2, subject="Математика", room="204",
                                   teacher="Иванов И. И."))
    куски = chunks(текст)
    assert len(куски) == 1, куски
    assert предметы_дня(текст) == ["Математика", "ауд. 204 · Иванов И. И."], предметы_дня(текст)


def test_third_lesson_starts_new_pair():
    """Три урока - это две пары, значит два блока."""
    куски = chunks(tt.format_day(make_day(1, 2, 3)))
    assert len(куски) == 2
    assert предметы_дня(куски[0]) and предметы_дня(куски[1])


def test_six_lessons_split_in_three_pairs():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5, 6)))
    assert len(куски) == 3, [len(k) for k in куски]


def test_gap_before_fifth_lesson():
    куски = chunks(tt.format_day(make_day(1, 2, 3, 4, 5)))
    assert len(куски) == 3, [len(k) for k in куски]


def test_no_gap_at_the_start_and_at_the_end_of_the_day():
    text = tt.format_day(make_day(1, 2, 3))
    assert not text.startswith("\n")
    assert text.splitlines()[0].startswith("📅 Понедельник")
    assert not text.endswith("\n")
    assert text.splitlines()[1].strip(), "под заголовком дня зияет пустая строка"


def test_identical_lessons_of_a_pair_collapse():
    """Обе части одной пары не печатаются дважды.

    Пара в этом колледже - одно занятие, разбитое звонком: у 661 пары из 687 оба
    урока совпадают по предмету, кабинету и преподавателю. Повтор занимал четыре
    строки и читался как второе занятие.
    """
    текст = tt.format_day(make_day(1, 2, subject="Математика", room="204",
                                   teacher="Иванов И. И."))
    assert текст.count("Математика") == 1, текст
    assert текст.count("Иванов И. И.") == 1, текст
    assert текст.count("204") == 1, текст


def test_different_lessons_are_not_collapsed():
    """Разные уроки внутри пары остаются оба - иначе пропадёт половина расписания.

    Таких пар сейчас нет ни одной, но колледж может их напечатать.
    """
    день = tt.DaySchedule(weekday=1, lessons=[
        tt.Lesson(1, "Физика", "Крылова В. И.", "222-1", "08:00", "08:45"),
        tt.Lesson(2, "Химия", "Иванова А. А.", "222-1", "08:50", "09:35"),
    ])
    текст = tt.format_day(день)
    assert "Физика" in текст and "Химия" in текст, текст
    assert текст.count("08:00–09:35") == 1, "пара показана дважды"


def test_same_subject_different_room_is_not_collapsed():
    """Предмет тот же, кабинет разный - это два разных занятия.

    Так выглядят подгруппы: «ИнЯзык в ПрофД» в первую пару идёт в 310-1, во
    вторую - в 311-1, и ведёт его другой преподаватель. Если схлопнуть такую пару
    по одному предмету, студент увидит один кабинет вместо двух и придёт не туда.
    """
    день = tt.DaySchedule(weekday=1, lessons=[
        tt.Lesson(1, "ИнЯзык в ПрофД", "Аврамова Л. А.", "310-1", "08:00", "08:45"),
        tt.Lesson(2, "ИнЯзык в ПрофД", "Аврамова Л. А.", "311-1", "08:50", "09:35"),
    ])
    текст = tt.format_day(день)
    assert "310-1" in текст and "311-1" in текст, текст


def test_same_subject_different_teacher_is_not_collapsed():
    """Тот же предмет и кабинет, но разные преподаватели - тоже два занятия."""
    день = tt.DaySchedule(weekday=1, lessons=[
        tt.Lesson(1, "ИнЯзык в ПрофД", "Аврамова Л. А.", "310-1", "08:00", "08:45"),
        tt.Lesson(2, "ИнЯзык в ПрофД", "Мухамадеева Л. Р.", "310-1", "08:50", "09:35"),
    ])
    текст = tt.format_day(день)
    assert "Аврамова Л. А." in текст and "Мухамадеева Л. Р." in текст, текст


def test_pair_same_by_all_three_fields_is_collapsed():
    """Контроль к двум проверкам выше: одинаковая пара схлопывается."""
    день = tt.DaySchedule(weekday=1, lessons=[
        tt.Lesson(1, "ИнЯзык в ПрофД", "Аврамова Л. А.", "310-1", "08:00", "08:45"),
        tt.Lesson(2, "ИнЯзык в ПрофД", "Аврамова Л. А.", "310-1", "08:50", "09:35"),
    ])
    текст = tt.format_day(день)
    assert текст.count("ИнЯзык в ПрофД") == 1, текст
    assert текст.count("Аврамова Л. А.") == 1, текст


def test_lesson_time_is_not_repeated_under_the_pair_time():
    """Время урока не печатается: его покрывает время пары над блоком.

    Владелец назвал это тавтологией: «08:00–08:45» прямо под заголовком
    «08:00–09:35» повторяет то же самое числами мельче.
    """
    текст = tt.format_day(make_day(1, 2, 3, 4))
    for интервал in ("08:00–08:45", "08:50–09:35"):
        assert интервал not in текст, текст
    assert "08:00–09:35" in текст


def test_lesson_numbers_are_gone():
    """Номеров уроков нет: пара опознаётся по времени."""
    текст = tt.format_day(make_day(1, 2, 3))
    for номер in ("1.", "2.", "3."):
        assert номер not in текст, текст


def test_pair_header_has_no_word_pair():
    """В заголовке нет слова «пара»: владелец просил убрать его из номеров."""
    текст = tt.format_day(make_day(1, 2, 3))
    for строка in заголовки_пар(текст):
        assert "Пара" not in строка and "пара" not in строка, строка


def test_indent_is_shallow():
    """Отступ не глубже шести пробелов.

    До переделки строки подгруппы уходили на десять пробелов, и на телефоне это
    съедало ширину: владелец указал на «слишком много пробелов».
    """
    текст = tt.format_day(make_day(1, 2, 3))
    глубина = max((len(s) - len(s.lstrip())) for s in текст.splitlines())
    assert глубина <= 6, f"отступ {глубина}"


def test_gap_sits_between_pairs_only():
    """Пустая строка - только между парами, внутри пары её нет."""
    text = tt.format_day(make_day(1, 2, 3, 4, 5, 6, 7))
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        previous = lines[index - 1] if index else ""
        gap = bool(index and not previous.strip())
        if gap:
            assert line.startswith("  ") and not line.startswith("   "), (
                f"после пустой строки идёт не заголовок пары: {line!r}")


def test_day_without_pair_times_has_no_headers():
    """Время пары неизвестно - заголовка нет, но разделение остаётся."""
    настоящая = tt.LESSON_TIMES
    try:
        tt.LESSON_TIMES = ()
        текст = tt.format_day(make_day(1, 2, 3))
        assert заголовки_пар(текст) == []
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
    assert "подгруппы:" in текст, текст
    assert "ауд. 105-2 · Сабирьянова А. Р." in текст
    assert "ауд. 306-1 · Нуриева С. Р." in текст
    # обе подгруппы на своих строках, не слиты в одну через «·» между предметами
    for кусок in ("1.Экономика (лабораторная)", "2.Устройство (лабораторная)"):
        строка = next(s for s in текст.splitlines() if s.strip().startswith(кусок))
        assert строка.startswith("      "), f"подгруппа без отступа: {строка!r}"


def test_plain_lesson_has_no_subgroup_mark():
    assert "подгруппы" not in tt.format_day(make_day(1))


# ── ближайшие занятия ────────────────────────────────────────────────────────
def test_upcoming_does_not_repeat_a_pair():
    """Пара не показывается дважды: одна запись на пару."""
    расписание = tt.GroupSchedule(group="24-23П", days={
        0: tt.DaySchedule(weekday=0, lessons=[
            tt.Lesson(1, "Математика", "Иванов И. И.", "204", "08:00", "08:45"),
            tt.Lesson(2, "Математика", "Иванов И. И.", "204", "08:50", "09:35"),
            tt.Lesson(3, "Физика", "Петров П. П.", "222-1", "09:50", "10:35"),
            tt.Lesson(4, "Физика", "Петров П. П.", "222-1", "10:55", "11:40"),
        ])})
    tt.apply_lesson_times(расписание)
    текст = tt.format_upcoming(расписание, limit=3)
    assert текст.count("Математика") == 1, текст
    assert текст.count("Физика") == 1, текст


def test_upcoming_keeps_the_requested_count():
    """После схлопывания остаётся ровно столько записей, сколько просили.

    Если бы просили ровно limit до схлопывания, студент увидел бы две пары вместо
    трёх: места занимает одна половина пары.
    """
    расписание = tt.GroupSchedule(group="24-23П", days={
        0: tt.DaySchedule(weekday=0, lessons=[
            tt.Lesson(n, f"Предмет {n}", "Иванов И. И.", "204", t, t)
            for n, t in ((1, "08:00"), (2, "08:50"), (3, "09:50"), (4, "10:55"),
                         (5, "12:00"), (6, "13:05"))])})
    tt.apply_lesson_times(расписание)
    текст = tt.format_upcoming(расписание, limit=3)
    заголовки = [s for s in текст.splitlines()
                 if "·" in s and s.strip()[:2].isdigit()]
    assert len(заголовки) == 3, заголовки


