"""Разбор ответа opencode: на нём держится доставка ответа в MAX.

Почему здесь тесты. Между программой-мостом и opencode стоит разбор вывода.
Формат вывода за этот приём уже менялся: сначала был один объект с ключом
`response`, потом - поток событий, где ответ разложен по событиям типа `text`.
Когда это произошло, программа честно сказала «ответ пустой», но понять, что
именно сломалось, можно было только чтением исходников opencode.

Значит, разбор - не деталь, а точка отказа, которую надо держать проверками.

Что проверяется.
* поток событий собирается в ответ и части идут по порядку;
* объект с ключом response тоже понимается (на случай возврата формата);
* мусор, пустые строки и неполный json не ломают разбор;
* при неизвестном формате в сообщении об ошибке видно, какие события были -
  иначе следующая смена формата снова будет выглядеть как поломка.
"""
import json

import bridge_worker as worker

# Настоящий вывод, который программа получила от opencode в первый раз.
ПОТОК = "\n".join([
    json.dumps({"type": "step_start", "timestamp": 1790780564876,
                "sessionID": "ses_x", "part": {"id": "prt_1", "type": "step-start"}}),
    json.dumps({"type": "text", "timestamp": 1790780566300, "sessionID": "ses_x",
                "part": {"id": "prt_2", "type": "text", "text": "Да."}}),
])

ЧАСТИ = "\n".join(json.dumps({"type": "text", "part": {"text": piece}})
                 for piece in ("Сделал: ", "поправил ", "мост.", " Готово."))


# ── поток событий ──────────────────────────────────────────────────────────
def test_text_stream_becomes_the_answer():
    assert worker._answer_from(ПОТОК) == "Да."


def test_parts_are_joined_in_order():
    assert worker._answer_from(ЧАСТИ) == "Сделал: поправил мост. Готово."


def test_parts_are_not_reordered():
    """Порядок частей - это порядок ответа; перестановка делает его бессвязным.

    Части различимы по содержанию, а не по пробелам: в прошлой версии проверки
    пробел стоял в конце первой части, и обратный порядок давал «второепервое» -
    правильный результат склейки, но ожидание было невнятным и сбивало с толку.
    Теперь каждая часть различима сама по себе.
    """
    one = json.dumps({"type": "text", "part": {"text": "раз один"}})
    two = json.dumps({"type": "text", "part": {"text": "раз два"}})
    assert worker._answer_from(one + "\n" + two) == "раз одинраз два"
    assert worker._answer_from(two + "\n" + one) == "раз двараз один"


def test_parts_are_joined_without_inventing_anything():
    """Склейка идёт как есть: разбор не вставляет пробелы и не правит слова.

    Если модель вернула куски без разделителей, добавлять их - значит менять её
    слова. Склеили и обрезали края, и всё.
    """
    вывод = "\n".join(json.dumps({"type": "text", "part": {"text": piece}})
                     for piece in ("без", "пробелов"))
    assert worker._answer_from(вывод) == "безпробелов"
    с_пробелами = "\n".join(json.dumps({"type": "text", "part": {"text": piece}})
                            for piece in ("с ", "пробелами"))
    assert worker._answer_from(с_пробелами) == "с пробелами"
    края = json.dumps({"type": "text", "part": {"text": "  по краям  "}})
    assert worker._answer_from(края) == "по краям"


# ── другой формат ──────────────────────────────────────────────────────────
def test_plain_response_key_is_understood():
    """Если opencode вернёт объект с ключом response - ответ не потеряется."""
    вывод = "\n".join([
        json.dumps({"type": "step_start", "part": {}}),
        json.dumps({"response": "Готово, коммит abc123"}),
    ])
    assert worker._answer_from(вывод) == "Готово, коммит abc123"


def test_stream_wins_over_an_empty_response_key():
    """Пустой ключ response не должен затирать разобранный текст."""
    вывод = json.dumps({"response": "  "}) + "\n" + \
        json.dumps({"type": "text", "part": {"text": "настоящий ответ"}})
    assert worker._answer_from(вывод) == "настоящий ответ"


# ── мусор не должен ломать ─────────────────────────────────────────────────
def test_garbage_does_not_break_parsing():
    """Служебные строки, пустые строки и неполный json - обычное дело."""
    вывод = "\n".join([
        "",
        "  ",
        "> building",
        "[0m",
        "{это не json",
        json.dumps({"type": "text", "part": {"text": "ответ"}}),
        '{"type":"text","part":{',                      # обрыв строки
    ])
    assert worker._answer_from(вывод) == "ответ"


def test_empty_output_gives_empty_answer():
    assert worker._answer_from("") == ""
    assert worker._answer_from("\n\n") == ""


def test_event_without_text_is_skipped():
    вывод = json.dumps({"type": "text", "part": {}}) + "\n" + \
        json.dumps({"type": "text"}) + "\n" + \
        json.dumps({"type": "text", "part": {"text": "есть"}})
    assert worker._answer_from(вывод) == "есть"


def test_list_instead_of_object_is_skipped():
    assert worker._answer_from("[1, 2, 3]") == ""


# ── диагностика при смене формата ──────────────────────────────────────────
def test_unknown_format_reports_which_events_were_seen():
    """Ключевое. Без этого смена формата выглядит как «мост сломался».

    Программа сообщает, какие типы событий встретились, - и по этому видно,
    что вывод изменился, а не что модель молчит.
    """
    вывод = "\n".join([
        json.dumps({"type": "step_start", "part": {}}),
        json.dumps({"type": "какое-то_новое_событие", "part": {}}),
    ])
    assert worker._answer_from(вывод) == ""
    типы = worker.seen_event_types()
    assert "какое-то_новое_событие" in типы, f"в подсказке нет типа события: {типы}"
    assert "step_start" in типы


def test_event_types_change_between_runs():
    """Подсказка не должна застревать на прошлом запуске."""
    worker._answer_from(json.dumps({"type": "первый_формат"}))
    assert "первый_формат" in worker.seen_event_types()
    worker._answer_from(json.dumps({"type": "второй_формат"}))
    типы = worker.seen_event_types()
    assert "второй_формат" in типы and "первый_формат" not in типы, \
        f"подсказка хранит старый формат: {типы}"


def test_seen_types_is_not_stuck_when_nothing_was_parsed():
    """Разбор без событий не должен оставлять прошлую подсказку."""
    worker._answer_from(json.dumps({"type": "старый"}))
    worker._answer_from("просто текст без json")
    assert worker.seen_event_types() == "ни одного", \
        "подсказка показывает события прошлого разбора"
