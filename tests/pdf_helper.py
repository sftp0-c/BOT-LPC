"""Генератор тестовых PDF для разбора расписания.

Настоящие файлы колледжа в тестах недоступны, а проверять разбор на выдуманных
структурах почти бесполезно: pdfplumber работает с настоящими шрифтами и
координатами. Поэтому здесь собирается минимальный PDF — со шрифтом Courier,
страницами и рамками таблицы, как в реальном файле расписания.

Кириллицу тестовый PDF не поддерживает ( Courier в WinAnsiEncoding её не
содержит), поэтому для проверки самого разбора страниц используется латиница
( sample_week_latin() ), а кириллические тексты проверяются на уровне
build_schedule() с готовыми страницами — см. tests/test_timetable.py.
"""
from pathlib import Path

PAGE_WIDTH, PAGE_HEIGHT = 842, 595  # A4 альбомная, как расписание колледжа
FONT_SIZE = 8

# Настоящая раскладка колледжа: № | «предмет, вид занятия, преподаватель» | аудитория
GRID_COLUMNS = (30, 60, 320, 390)  # четыре линии → три ячейки
ROW_HEIGHT = 40
LINE_GAP = 9  # шаг строки внутри ячейки


def make_pdf(lines: list[str]) -> bytes:
    """Одностраничный PDF из строк текста (для проверки извлечения текста)."""
    parts = [f"BT /F1 {FONT_SIZE} Tf 1 0 0 1 30 {PAGE_HEIGHT - 40} Tm {FONT_SIZE + 2} TL"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj T*")
    parts.append("ET")
    return _build(["\n".join(parts).encode("latin-1", "replace")])


def make_grid_pdf(pages: list) -> bytes:
    """PDF с таблицами в рамках. pages = [[(заголовок дня, строки таблицы)], ...]

    Одна страница — один день недели, как в настоящем файле колледжа. Строка
    таблицы — три ячейки: номер пары, блок «предмет\n(вид)\nпреподаватель»,
    аудитория. Рамки нужны, чтобы разбор шёл тем же путём, что и на настоящем
    файле (стратегия «lines»), а не по колонкам пробелов.
    """
    streams: list[bytes] = []
    for day_tables in pages:
        commands: list[str] = []
        top = PAGE_HEIGHT - 40
        for title, rows in day_tables:
            commands.append(_text(30, top, f"День - {title}, 28.09.2026"))
            table_top = top - 24
            bottom = table_top - ROW_HEIGHT * (len(rows) + 1)
            for x in GRID_COLUMNS:
                commands.append(f"{x} {table_top:.1f} m {x} {bottom:.1f} l S")
            for index in range(len(rows) + 1):
                y = table_top - ROW_HEIGHT * index
                commands.append(f"{GRID_COLUMNS[0]} {y:.1f} m {GRID_COLUMNS[-1]} {y:.1f} l S")
            for index, row in enumerate(rows):
                # текст ячейки начинается сверху своей строки, строки идут вниз
                y = table_top - ROW_HEIGHT * index - 8
                for column, cell in enumerate(row):
                    if not cell or column >= len(GRID_COLUMNS) - 1:
                        continue
                    x = GRID_COLUMNS[column] + 3
                    for line_no, part in enumerate(str(cell).split("\n")):
                        commands.append(_text(x, y - line_no * LINE_GAP, part))
            top = bottom - 30
        streams.append("\n".join(commands).encode("latin-1", "replace"))
    return _build(streams)


def _text(x: float, y: float, value: str) -> str:
    return f"BT /F1 {FONT_SIZE} Tf {x:.1f} {y:.1f} Td ({_escape(value)}) Tj ET"


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build(streams: list[bytes]) -> bytes:
    """Собирает PDF: 1 — каталог, 2 — страницы, 3 — шрифт, дальше пары «страница/содержимое»."""
    count = len(streams)
    page_ids = [4 + index * 2 for index in range(count)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    ]
    for index, stream in enumerate(streams):
        page_id = page_ids[index]
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def write_pdf(path, lines: list[str]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(make_pdf(lines))
    return str(target)


# ── готовые недели для тестов ─────────────────────────────────────────────────
def sample_week() -> list:
    """Понедельник и среда: по одной группе, три и одна пары."""
    return [
        [("Понедельник", [
            ["№", "ИС-21", "204"],
            ["1", "Математика\n(лекция)\nИванова А. А.", "204"],
            ["3", "Физика\n(лаб)\nПетров П. П.", "217"],
        ])],
        [("Среда", [
            ["№", "ИС-21", "101"],
            ["2", "История\n(лекция)\nСидорова М. М.", "101"],
        ])],
    ]


def sample_week_latin() -> list:
    """То же латиницей: тестовый PDF собирается шрифтом без кириллицы."""
    return [
        [("Monday", [
            ["#", "IS-21", "204"],
            ["1", "Math\n(lec)\nIvanova A. A.", "204"],
            ["3", "Physics\n(lab)\nPetrov P. P.", "217"],
        ])],
        [("Wednesday", [
            ["#", "IS-21", "101"],
            ["2", "History\n(lec)\nSidorova M. M.", "101"],
        ])],
    ]


def pages_from_dict(weekdays: dict) -> list:
    """Страницы из описания {день недели: [(пара, предмет, преподаватель, аудитория)]}."""
    titles = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")
    pages = []
    for weekday in sorted(weekdays):
        rows = [["№", "ИС-21", "—"]]
        for number, subject, teacher, room in weekdays[weekday]:
            rows.append([str(number), f"{subject}\n({teacher})", room])
        pages.append([(titles[weekday], rows)])
    return pages
