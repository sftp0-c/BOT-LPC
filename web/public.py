"""Публичная витрина «Навигатора ЛПК»: только синтетические данные."""
from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .router import open_router

STATIC = Path(__file__).with_name("static")
SECURITY = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
NOINDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


def page() -> str:
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Навигатор ЛПК — обращения и расписание в MAX</title>
<meta name="description" content="Сервис колледжа в MAX: расписание из PDF, единая очередь обращений, статусы и панель контроля для сотрудников.">
<link rel="canonical" href="https://bot-lpc.ru/"><meta property="og:type" content="website"><meta property="og:locale" content="ru_RU">
<meta property="og:title" content="Навигатор ЛПК — колледж отвечает как единая команда"><meta property="og:description" content="Расписание, частые вопросы и обращения студентов в MAX.">
<meta property="og:image" content="https://bot-lpc.ru/public/social-card.svg"><meta name="theme-color" content="#0f1518">
<link rel="stylesheet" href="/public/landing.css"><script src="/public/landing.js" defer></script></head><body>
<a class="lp-skip" href="#content">Перейти к содержимому</a>
<header class="lp-head"><a class="lp-brand" href="/" aria-label="Навигатор ЛПК"><span class="lp-mark">◇</span><span><b>Навигатор ЛПК</b><small>Колледж ближе</small></span></a>
<nav aria-label="Навигация"><a href="#story">Как работает</a><a href="#leader">Для руководителя</a><a href="#security">Безопасность</a><a href="#pilot">Пилот</a></nav><a class="lp-login" href="/panel/login">Вход для сис-админа →</a></header>
<main id="content"><section class="lp-hero"><div class="lp-hero-copy lp-reveal"><span class="lp-kicker">ЦИФРОВОЙ СЕРВИС КОЛЛЕДЖА В MAX</span><h1>Студент пишет в MAX.<br><em>Колледж отвечает как единая команда.</em></h1><p>«Навигатор ЛПК» объединяет расписание, частые вопросы и обращения студентов. У обращения есть ответственный, статус и история — вместо разрозненных личных чатов.</p><div class="lp-actions"><a class="lp-primary" href="#story">Показать путь обращения ↓</a><a class="lp-secondary" href="/panel/login">Вход в панель</a></div><small>Без отдельного приложения для сервиса. Рабочая панель — для назначенных сис-админов.</small></div>
<div class="lp-hero-demo lp-reveal" aria-label="Демонстрационный путь обращения"><span class="lp-demo">ДЕМО · СИНТЕТИЧЕСКИЕ ДАННЫЕ</span><div class="lp-phone"><b>MAX · Навигатор ЛПК</b><div class="lp-msg lp-user">Нужна справка об обучении</div><div class="lp-msg">Обращение №104 принято<br><small>Ответственный: учебная часть</small></div><div class="lp-state">● В работе</div></div><svg class="lp-route" viewBox="0 0 260 90" aria-hidden="true"><path d="M5 70 C80 10 170 10 255 52"/></svg><div class="lp-queue"><span>ЕДИНАЯ ОЧЕРЕДЬ</span><b>№104 · ДЕМО-21</b><p>Справка об обучении</p><div><i>Ожидает 18 минут</i><strong>Ответить →</strong></div></div></div></section>
<section class="lp-section lp-problem lp-reveal"><span class="lp-kicker">ЗАДАЧА</span><h2>Когда вопрос приходит в личный чат, процесс не виден.</h2><p>Если один вопрос задают по телефону, лично и в переписке, сотруднику приходится вручную помнить, кому уже ответили. Система превращает сообщения в понятный процесс.</p><div class="lp-three"><article><b>Повторяющиеся вопросы</b><p>Расписание, справки, сроки и контакты снова требуют времени.</p></article><article><b>Неясный следующий шаг</b><p>Студент не знает, принято ли обращение и кто отвечает.</p></article><article><b>Нет общей картины</b><p>Сложно увидеть нагрузку, ожидание и незакрытые задачи.</p></article></div></section>
<section id="story" class="lp-story"><div class="lp-story-copy"><article class="lp-step lp-reveal" data-scene="student"><span>ШАГ 01 · СТУДЕНТ</span><h2>Один привычный канал — MAX.</h2><p>Студент выбирает группу, смотрит расписание, находит частый ответ или создаёт обращение. Черновик проверяется до отправки, а статус виден в том же диалоге.</p></article><article class="lp-step lp-reveal" data-scene="staff"><span>ШАГ 02 · СОТРУДНИК</span><h2>Не чат, а рабочая очередь.</h2><p>Видно, кто ждёт ответа. Можно открыть только свои обращения, использовать шаблон, передать задачу коллеге и изменить статус.</p></article><article id="leader" class="lp-step lp-reveal" data-scene="leader"><span>ШАГ 03 · РУКОВОДИТЕЛЬ</span><h2>Картина процесса — без чтения личной переписки.</h2><p>Панель показывает созданные, взятые, отвеченные и закрытые обращения. Аудит хранит тип действия и ID цели, но не текст сообщений.</p></article></div><div class="lp-sticky" aria-label="Синтетическая демонстрация"><span class="lp-demo">ДЕМО · ЧИСЛА НЕ ИЗ ЖИВОЙ БАЗЫ</span><div class="lp-scene is-active" data-panel="student"><b>Обращение №104</b><p>Статус: в работе</p><div class="lp-progress"><i></i></div><small>Ответственный назначен</small></div><div class="lp-scene" data-panel="staff"><b>Рабочее место</b><div class="lp-ticket active">№104 · ждёт 18 мин</div><div class="lp-ticket">№103 · ответ отправлен</div><div class="lp-ticket">№102 · к выдаче</div></div><div class="lp-scene" data-panel="leader"><div class="lp-metrics"><b>12<small>Создано</small></b><b>9<small>Взято</small></b><b>8<small>Отвечено</small></b><b>6<small>Закрыто</small></b></div><div class="lp-bars"><i style="--h:35%"></i><i style="--h:70%"></i><i style="--h:52%"></i><i style="--h:88%"></i><i style="--h:64%"></i></div></div></div></section>
<section class="lp-section lp-self lp-reveal"><span class="lp-kicker">САМООБСЛУЖИВАНИЕ</span><h2>Повторяющийся вопрос снимается ещё до обращения.</h2><div class="lp-two"><article><h3>Расписание из опубликованного PDF</h3><p>Бот показывает неделю файла и предупреждает, если данные устарели.</p><div class="lp-schedule"><b>ДЕМО-21 · Неделя с 09.11.2026</b><span>Пн · Пара 1 · Предмет А · ауд. А-12</span><span>Вт · Пара 2 · Предмет Б · ауд. Б-14</span></div></article><article><h3>Частые вопросы и контакты</h3><p>Содержание редактируется в панели. Если уверенного ответа нет, бот не выдумывает его и предлагает сотрудника.</p><ul><li>Справки и документы</li><li>Стипендия и выплаты</li><li>Контакты колледжа</li></ul></article></div></section>
<section id="security" class="lp-section lp-security lp-reveal"><span class="lp-kicker">БЕЗОПАСНОСТЬ И УПРАВЛЕНИЕ</span><h2>Публичная витрина отдельно.<br>Рабочие данные — за входом.</h2><div class="lp-grid"><span>Панель только назначенным сис-админам</span><span>Подписанная cookie и CSRF</span><span>Отзыв роли при следующем запросе</span><span>Аудит без переписки и кодов</span><span>Согласованные SQLite-снимки</span><span>HTTPS и проверка схемы</span></div><p class="lp-note">Сервер, срок хранения копий и технический ответственный фиксируются до пилота.</p></section>
<section class="lp-section lp-built lp-reveal"><span class="lp-kicker">УЖЕ РЕАЛИЗОВАНО</span><h2>Не концепт на слайде, а собранная система.</h2><div class="lp-six"><article>Бот в MAX<small>Регистрация, профиль, расписание, FAQ, обращения</small></article><article>Рабочее место<small>Очередь, шаблоны, статусы, передача, архив</small></article><article>Управление<small>Сотрудники, группы, права и справочники</small></article><article>Контроль<small>Сводка дня, аудит, CSV и health</small></article><article>Эксплуатация<small>Docker, SQLite и резервные снимки</small></article><article>Интерфейс<small>Телефон, две темы, клавиатура, reduced motion</small></article></div></section>
<section id="pilot" class="lp-pilot lp-reveal"><span class="lp-kicker">ПРЕДЛОЖЕНИЕ</span><h2>Не менять весь процесс сразу.<br>Проверить на одном участке.</h2><p>Двухнедельный пилот с учебной частью: ограниченный набор сотрудников и групп, фактическая нагрузка и решение о дальнейшем расширении.</p><ol><li>Назначить ответственных</li><li>Заполнить сотрудников и группы</li><li>Провести тестовое обращение</li><li>Запустить ограниченный пилот</li><li>Сравнить показатели</li></ol><a class="lp-primary" href="/panel/login">Вход для сис-админа →</a></section>
<section class="lp-section lp-faq lp-reveal"><span class="lp-kicker">ВОПРОСЫ</span><h2>Честно о внедрении.</h2><details><summary>Нужны ли платные сервисы?</summary><p>Отдельная платная SaaS-база не требуется. Сервер, домен, резервное копирование и сопровождение имеют стоимость, которая определяется отдельно.</p></details><details><summary>Кто администрирует систему?</summary><p>Содержание ведёт назначенный сис-админ колледжа. Технический ответственный и регламент обновлений назначаются до пилота.</p></details><details><summary>Расписание обновляется в реальном времени?</summary><p>Бот периодически читает опубликованный колледжем PDF, показывает его неделю и предупреждает об устаревании.</p></details><details><summary>Где хранятся данные?</summary><p>База и вложения находятся на выбранном сервере, без внешней SaaS-БД. Сообщения проходят через инфраструктуру MAX.</p></details></section></main>
<footer class="lp-foot"><a class="lp-brand" href="/"><span class="lp-mark">◇</span><b>Навигатор ЛПК</b></a><span>Меньше рутины. Больше внимания.</span><a href="/panel/login">Вход в панель</a></footer></body></html>'''


@open_router.get("/", include_in_schema=False)
async def landing():
    return HTMLResponse(page(), headers=SECURITY)


@open_router.get("/public/{name}", include_in_schema=False)
async def public_asset(name: str):
    allowed={"landing.css":"text/css; charset=utf-8", "landing.js":"text/javascript; charset=utf-8", "social-card.svg":"image/svg+xml"}
    if name not in allowed:
        return Response(status_code=404)
    return FileResponse(STATIC/name, media_type=allowed[name], headers={"Cache-Control":"public, max-age=3600","X-Content-Type-Options":"nosniff"})


@open_router.get("/robots.txt", include_in_schema=False)
async def robots():
    return PlainTextResponse("User-agent: *\nAllow: /$\nDisallow: /panel/\nDisallow: /join/\nDisallow: /health\nSitemap: https://bot-lpc.ru/sitemap.xml\n", headers=SECURITY)


@open_router.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://bot-lpc.ru/</loc></url></urlset>', media_type="application/xml", headers=SECURITY)
